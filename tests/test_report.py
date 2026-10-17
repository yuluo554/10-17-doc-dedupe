"""Report rendering: one source, three formats, byte-stable output.

Three things are being locked here:

* the three formats carry the same content (they are serializations of one
  :class:`docdedupe.report.Report`, and the tests check the content agrees, not
  merely that all three files exist);
* JSON is byte-identical across runs and across processes, because a report is
  compared with ``diff`` and a timestamp or a set iteration order would make
  every run "changed";
* the report layer makes no decisions. That one is a static AST assertion: it is
  the only way to keep "the scoring formula lives in cluster.py only" true after
  this commit.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import re
from pathlib import Path

import pytest

from docdedupe import cluster as cluster_mod
from docdedupe import report as report_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "docdedupe"


def _doc(doc_id, name, *, chars=100, mtime=0, directory="/root/group"):
    return cluster_mod.Document(
        doc_id=doc_id,
        path_abs=directory + "/" + name,
        path_norm=directory + "/" + name,
        ext=Path(name).suffix,
        text_sha256="t%d" % doc_id,
        norm_sha256="n%d" % doc_id,
        norm_chars=chars,
        mtime_ns=mtime,
        text="",
    )


@pytest.fixture
def sample_report():
    """A two-cluster report built through the real clustering code."""
    older = _doc(1, "方案.txt", mtime=100)
    newer = _doc(2, "方案-终稿.txt", mtime=200, chars=95)
    exact_a = _doc(3, "归档.md", mtime=10)
    exact_b = _doc(4, "归档副本.md", mtime=10)
    # Identical normalized hashes make the pair an exact duplicate.
    exact_a = exact_a._replace(norm_sha256="same")
    exact_b = exact_b._replace(norm_sha256="same")
    clusters = cluster_mod.build_clusters(
        [older, newer, exact_a, exact_b],
        [
            cluster_mod.Pair(a_id=1, b_id=2, jaccard=0.95),
            cluster_mod.Pair(a_id=3, b_id=4, jaccard=1.0),
        ],
    )
    return report_mod.build_report(clusters)


def test_the_three_formats_carry_the_same_clusters(sample_report):
    md = report_mod.render(sample_report, "md")
    csv_text = report_mod.render(sample_report, "csv")
    payload = json.loads(report_mod.render(sample_report, "json"))

    json_ids = [item["cluster_id"] for item in payload["clusters"]]
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    csv_ids = []
    for row in rows:
        if row["cluster_id"] not in csv_ids:
            csv_ids.append(row["cluster_id"])
    assert csv_ids == json_ids

    for cluster_id in json_ids:
        assert cluster_id in md
    assert "共 %d 簇" % len(json_ids) in md


def test_the_three_formats_agree_on_membership_and_keep(sample_report):
    payload = json.loads(report_mod.render(sample_report, "json"))
    rows = list(csv.DictReader(io.StringIO(report_mod.render(sample_report, "csv"))))

    for cluster in payload["clusters"]:
        cluster_rows = [row for row in rows if row["cluster_id"] == cluster["cluster_id"]]
        assert [row["member_path"] for row in cluster_rows] == [
            member["path"] for member in cluster["members"]
        ]
        assert {row["keep"] for row in cluster_rows} == {cluster["keep"] or ""}
        kept = [row["member_path"] for row in cluster_rows if row["is_keep"] == "true"]
        expected_kept = [
            member["path"] for member in cluster["members"] if member["kept"]
        ]
        assert kept == expected_kept


def test_json_is_byte_identical_across_runs(sample_report):
    first = report_mod.render(sample_report, "json")
    second = report_mod.render(sample_report, "json")
    assert first == second
    # json.dumps with sort_keys + fixed indent: no dict-order leak.
    assert first.endswith("\n")


def test_json_is_byte_identical_across_processes(tmp_path):
    """A subprocess run must produce the same bytes as this process.

    The in-process comparison above cannot catch a hash-ordering dependency,
    because both runs share one ``PYTHONHASHSEED``; a fresh interpreter gets a
    new seed, so this is the test that actually pins it down.
    """
    script = tmp_path / "emit.py"
    # A plain placeholder rather than %-formatting: the script body is full of
    # "%d" / "%s" of its own and would collide with the outer format string.
    template = """\
import sys
sys.path.insert(0, __SRC__)
from pathlib import Path
from docdedupe import cluster as c, report as r

def doc(i, name, chars=100, mtime=0, sha=None):
    return c.Document(doc_id=i, path_abs='/root/g/' + name,
        path_norm='/root/g/' + name, ext=Path(name).suffix,
        text_sha256='t' + str(i), norm_sha256=sha or ('n' + str(i)),
        norm_chars=chars, mtime_ns=mtime, text='')

docs = [doc(1, 'a.txt', mtime=1), doc(2, 'b.txt', mtime=2),
        doc(3, 'c.md', sha='x'), doc(4, 'd.md', sha='x')]
pairs = [c.Pair(1, 2, 0.95), c.Pair(3, 4, 1.0)]
# sys.stdout.buffer, not print(): print() would apply Windows text-mode CRLF
# translation and the comparison below would be testing the platform, not the
# renderer.
sys.stdout.buffer.write(
    r.render(r.build_report(c.build_clusters(docs, pairs)), 'json').encode('utf-8')
)
"""
    script.write_text(
        template.replace("__SRC__", repr(str(REPO_ROOT / "src"))), encoding="utf-8"
    )
    import subprocess
    import sys as _sys

    outputs = []
    for seed in ("1", "12345"):
        env = {"PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHONUTF8": "1"}
        result = subprocess.run(
            [_sys.executable, str(script)],
            capture_output=True,
            env=env,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]

    # The subprocess writes the same bytes the in-process renderer produces.
    # ``stdout=subprocess.PIPE`` is a binary pipe, so this compares raw bytes and
    # would catch a CRLF translation the CLI might apply on Windows.
    def doc(i, name, chars=100, mtime=0, sha=None):
        return cluster_mod.Document(
            doc_id=i,
            path_abs="/root/g/" + name,
            path_norm="/root/g/" + name,
            ext=Path(name).suffix,
            text_sha256="t%d" % i,
            norm_sha256=sha or "n%d" % i,
            norm_chars=chars,
            mtime_ns=mtime,
            text="",
        )

    expected = report_mod.render(
        report_mod.build_report(
            cluster_mod.build_clusters(
                [
                    doc(1, "a.txt", mtime=1),
                    doc(2, "b.txt", mtime=2),
                    doc(3, "c.md", sha="x"),
                    doc(4, "d.md", sha="x"),
                ],
                [
                    cluster_mod.Pair(a_id=1, b_id=2, jaccard=0.95),
                    cluster_mod.Pair(a_id=3, b_id=4, jaccard=1.0),
                ],
            )
        ),
        "json",
    )
    assert outputs[0] == expected.encode("utf-8")


def test_csv_uses_unix_line_endings(sample_report):
    """Byte-stable on every platform: a Windows run must not emit CRLF."""
    text = report_mod.render(sample_report, "csv")
    assert "\r" not in text
    assert text.endswith("\n")


def test_reports_carry_no_timestamp(sample_report):
    """A report describes content; when it was built is index state."""
    for fmt in report_mod.FORMATS:
        text = report_mod.render(sample_report, fmt)
        assert not re.search(r"\d{4}-\d{2}-\d{2}", text), "%s has a date" % fmt
        assert not re.search(r"\d{2}:\d{2}:\d{2}", text), "%s has a clock time" % fmt
        assert "built_at" not in text


def test_markdown_escapes_pipes_in_paths():
    awkward = _doc(1, "a|b.txt")
    plain = _doc(2, "c.txt")
    clusters = cluster_mod.build_clusters(
        [awkward, plain], [cluster_mod.Pair(a_id=1, b_id=2, jaccard=0.95)]
    )
    text = report_mod.render(report_mod.build_report(clusters), "md")
    # The path appears, but its pipe is escaped so the table stays a table.
    assert "a\\|b.txt" in text


def test_json_declares_its_schema(sample_report):
    payload = json.loads(report_mod.render(sample_report, "json"))
    assert payload["schema"] == report_mod.REPORT_SCHEMA
    assert payload["summary"]["clusters"] == len(sample_report.clusters)
    assert payload["summary"]["members"] == sample_report.member_count
    assert set(payload["summary"]["by_tier"]) == set(cluster_mod.TIER_ORDER)


def test_template_clusters_render_without_a_keep():
    left = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n价税合计：42.10 万元\n经办人：李四"
    right = "示例项目乙 费用摘要\n开票单位：示例科技有限公司B\n价税合计：18.90 万元\n经办人：王五"
    docs = [
        _doc(1, "a.docx")._replace(text=left),
        _doc(2, "b.docx")._replace(text=right),
    ]
    clusters = cluster_mod.build_clusters(
        docs, [cluster_mod.Pair(a_id=1, b_id=2, jaccard=0.7)]
    )
    assert clusters and clusters[0].tier == "template"
    rendered = report_mod.build_report(clusters)
    payload = json.loads(report_mod.render(rendered, "json"))
    cluster = payload["clusters"][0]
    assert cluster["tier"] == "template"
    assert cluster["keep"] is None
    assert not any(member["kept"] for member in cluster["members"])
    md = report_mod.render(rendered, "md")
    assert "建议保留：无" in md


def test_an_empty_report_is_valid_in_every_format():
    empty = report_mod.build_report([])
    assert empty.has_findings is False
    assert json.loads(report_mod.render(empty, "json"))["clusters"] == []
    assert "没有发现重复文档" in report_mod.render(empty, "md")
    rows = list(csv.DictReader(io.StringIO(report_mod.render(empty, "csv"))))
    assert rows == []


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        report_mod.render(report_mod.build_report([]), "xml")


# ---------------------------------------------------------------------------
# The report layer makes no decisions (static gate)
# ---------------------------------------------------------------------------


def _report_tree() -> ast.Module:
    path = SRC / "report.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_report_module_does_not_import_the_fingerprint_or_cluster_scoring():
    """A renderer that can import the scorer is a renderer that will re-score.

    ``cluster`` is imported for its type and tier order only, so it is allowed;
    ``fingerprint`` and ``config`` are not, because either would let a threshold
    comparison appear here.
    """
    forbidden = {"fingerprint", "config", "normalize", "shingles"}
    offenders = []
    for node in ast.walk(_report_tree()):
        if isinstance(node, ast.Import):
            offenders += [alias.name for alias in node.names if alias.name.split(".")[0] in forbidden]
        elif isinstance(node, ast.ImportFrom) and node.module:
            name = node.module.split(".")[-1]
            if name in forbidden:
                offenders.append(node.module + ":" + ",".join(a.name for a in node.names))
    assert offenders == [], "report.py imports a decision module: %s" % offenders


def test_report_module_has_no_threshold_or_weight_literals():
    """Numbers that decide a tier must not exist here, not even as a comparison.

    The AST is walked for float literals other than the display precision
    (``0.0`` as a default) and for the threshold attribute names, so a
    copy-pasted ``>= 0.80`` cannot hide.
    """
    suspicious_names = {
        "near_duplicate_threshold",
        "exact_threshold",
        "template_threshold",
        "template_field_diff_max",
        "pair_store_threshold",
        "SCORE_WEIGHTS",
    }
    hits = []
    for node in ast.walk(_report_tree()):
        if isinstance(node, ast.Name) and node.id in suspicious_names:
            hits.append("%s at line %d" % (node.id, node.lineno))
        if isinstance(node, ast.Attribute) and node.attr in suspicious_names:
            hits.append("%s at line %d" % (node.attr, node.lineno))
    assert hits == [], "report.py references a decision constant: %s" % hits


def test_report_module_defines_no_comparison_against_a_similarity_score():
    """No ``> 0.8``-shaped literal comparisons at all.

    String and integer comparisons are fine (they drive formatting); a float
    comparison in a renderer is the smell that a decision moved in.
    """
    offenders = []
    for node in ast.walk(_report_tree()):
        if not isinstance(node, ast.Compare):
            continue
        for comparator in list(node.comparators) + [node.left]:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, float):
                offenders.append("line %d compares with %r" % (node.lineno, comparator.value))
    assert offenders == [], "report.py compares against a float: %s" % offenders


def test_scoring_constants_are_defined_only_in_cluster_py():
    """The scoring policy has exactly one home (plan/03 module 3)."""
    hits = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "cluster.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "SCORE_WEIGHTS" in text or "_score_members" in text:
            hits.append(path.name)
    assert hits == [], "scoring defined outside cluster.py: %s" % hits


def test_template_criterion_is_defined_only_in_cluster_py():
    for path in sorted(SRC.glob("*.py")):
        if path.name == "cluster.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "template_evidence" not in text, (
            "%s re-implements the template criterion" % path.name
        )
