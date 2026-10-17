"""B6 benchmark: the round trip is byte-identical, and the gates can fail.

Two things are checked here. First, the benchmark itself passes on a corpus it
made for the purpose — including a cluster whose movable members share a base
name, so the "never overwrite, disambiguate instead" path is exercised rather than
merely present. Second, every injection makes it report failure: a gate that stays
green under damage is decoration.

The corpus is built in ``tmp_path`` (the shipped ``data/samples`` is frozen, and
B6 moves files), and it is built out of *near* duplicates — same base name,
different last line — so a copy that overwrote another would be caught by content
and not just by a count.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

import bench_quarantine  # noqa: E402  (needs the tools path above)


BODY = "示例项目甲 正文。\n" + "本段用于构成足够长的正文内容。" * 12 + "\n"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """Two near-duplicate clusters, one of them with colliding base names.

    * ``方案/`` and ``存档/`` both hold ``方案.txt``: the same name in two folders,
      with one line different, so the pair is a near duplicate and the quarantine
      has to disambiguate the two copies without losing either.
    * ``甲/``..``丁/`` hold a four-member near-duplicate family, which gives the
      interruption scenario more than one file to move.
    * ``无关.txt`` is a document that belongs to no cluster.
    """
    root = tmp_path_factory.mktemp("b6-corpus")
    (root / "方案").mkdir()
    (root / "存档").mkdir()
    (root / "方案" / "方案.txt").write_text(BODY + "签署人：张三\n", encoding="utf-8")
    (root / "存档" / "方案.txt").write_text(BODY + "签署人：李四\n", encoding="utf-8")

    family = BODY + "评审结论：通过。\n"
    for name in ("甲", "乙", "丙", "丁"):
        (root / name).mkdir()
        (root / name / "纪要.txt").write_text(
            family + "编制人：%s\n" % name, encoding="utf-8"
        )

    (root / "无关.txt").write_text("完全不同的另一份文档。" * 30, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def baseline(corpus, tmp_path_factory):
    """The clean run: the benchmark must pass, and it must exercise its scenarios.

    Returns the report together with the work directory, because the thing worth
    inspecting afterwards is the benchmark's own copy of the corpus — it never
    touches the corpus it was pointed at.
    """
    work = tmp_path_factory.mktemp("b6-work")
    report = bench_quarantine.run_benchmark(corpus, work, inject="none")
    return report, work


def _relative_hashes(root: Path) -> dict:
    """Corpus files by relative path, quarantine directories excluded."""
    result = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if bench_quarantine.quarantine.QUARANTINE_DIR_NAME in relative.parts:
            continue
        result[relative.as_posix()] = bench_quarantine.sha256_of(path)
    return result


def test_the_clean_benchmark_passes(baseline):
    report, _ = baseline
    ok, failures = bench_quarantine.evaluate(report)
    assert ok, failures
    assert report.passed == report.total
    assert report.items >= 6, "the benchmark should have moved several files"


def test_every_scenario_ran(baseline):
    """Coverage is part of the gate: a scenario that never ran proves nothing."""
    report, _ = baseline
    assert set(report.coverage) == set(bench_quarantine.SCENARIOS)
    assert report.batches >= 4


def test_the_corpus_has_colliding_base_names_inside_one_batch(baseline):
    """Anti-vacuity for the "never overwrite" check.

    Without a collision the disambiguation path is never taken, and the check that
    "two members with the same name both survive" would pass for the wrong reason.
    """
    report, _ = baseline
    assert report.collisions >= 2, report.describe()


def test_the_round_trip_copy_comes_back_byte_identical(baseline, corpus):
    """Checked from outside the benchmark, on the copy it actually moved files in."""
    _, work = baseline
    copies = sorted(work.glob("round-trip-*/corpus"))
    assert len(copies) == 1, copies

    assert _relative_hashes(copies[0]) == _relative_hashes(corpus)
    # Nothing is left in the quarantine but its own record: every moved file went
    # back, and the manifests stay behind on purpose.
    leftovers = [
        path.name
        for path in copies[0].rglob("*")
        if path.is_file()
        and bench_quarantine.quarantine.QUARANTINE_DIR_NAME in path.relative_to(copies[0]).parts
    ]
    assert set(leftovers) == {bench_quarantine.quarantine.MANIFEST_NAME}, leftovers


@pytest.mark.parametrize("mode", [m for m in bench_quarantine.INJECTIONS if m != "none"])
def test_each_injection_makes_the_benchmark_fail(corpus, tmp_path, mode):
    """A gate that stays green under damage is a fake gate."""
    report = bench_quarantine.run_benchmark(corpus, tmp_path / mode, inject=mode)

    ok, failures = bench_quarantine.evaluate(report)

    assert not ok, "the %r injection went unnoticed" % mode
    assert report.failures, mode
    assert failures


def test_the_skip_sha_check_injection_is_caught_by_the_tampered_copy_scenario(
    corpus, tmp_path
):
    """The injection targets hash verification, so name the check that notices it.

    A coarse "something failed" assertion would also pass if the injection broke
    the setup instead of the gate; this pins the failure to the tampered copy.
    """
    report = bench_quarantine.run_benchmark(corpus, tmp_path / "sha", inject="skip-sha-check")

    failed = {outcome.scenario for outcome in report.failures}
    assert "tampered-copy" in failed, [o.describe() for o in report.outcomes]
    tampered = next(
        outcome for outcome in report.outcomes if outcome.scenario == "tampered-copy"
    )
    assert any("changed" in problem or "put back" in problem for problem in tampered.problems)


def test_the_cli_exits_non_zero_for_a_damaged_run(corpus, tmp_path, capsys):
    exit_code = bench_quarantine.main(
        [
            "--samples",
            str(corpus),
            "--work",
            str(tmp_path / "cli"),
            "--inject",
            "skip-pending",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "B6 FAIL" in output
    assert "checks" in output


def test_the_cli_exits_zero_for_a_clean_run(corpus, tmp_path, capsys):
    exit_code = bench_quarantine.main(
        ["--samples", str(corpus), "--work", str(tmp_path / "clean")]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "B6 OK" in output
    assert "FAIL" not in output


def test_a_corpus_without_clusters_is_reported_not_crashed(tmp_path, capsys):
    """A corpus of one unrelated file has nothing to quarantine; say so, exit 1."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "a.txt").write_text("一份没人像的文档。" * 30, encoding="utf-8")

    exit_code = bench_quarantine.main(
        ["--samples", str(lonely), "--work", str(tmp_path / "work")]
    )

    assert exit_code == 1
    assert "B6 FAIL" in capsys.readouterr().out


def test_a_missing_corpus_is_reported_not_crashed(tmp_path, capsys):
    exit_code = bench_quarantine.main(
        ["--samples", str(tmp_path / "nope"), "--work", str(tmp_path / "work")]
    )
    assert exit_code == 1
    assert "no corpus" in capsys.readouterr().out


def test_injection_names_are_checked():
    with pytest.raises(ValueError):
        with bench_quarantine.injection("definitely-not-an-injection"):
            pass  # pragma: no cover - the error is raised on entry


def test_the_benchmark_never_deletes_anything():
    """The tool is bound by the same no-deletion rule as the package."""
    import ast

    tree = ast.parse(
        (REPO_ROOT / "tools" / "bench_quarantine.py").read_text(encoding="utf-8")
    )
    forbidden = {"remove", "unlink", "rmtree", "removedirs"}
    offenders = [
        "%s:%d %s" % ("bench_quarantine.py", node.lineno, node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
    ]
    assert offenders == [], offenders


def test_the_verdict_line_carries_the_numbers(baseline):
    """The verdict a hand-off quotes is on stdout, in one line, with the counts."""
    report, _ = baseline
    lines = report.describe()

    assert "checks %d/%d" % (report.passed, report.total) in lines[-1]
    assert any("batches %d" % report.batches in line for line in lines)
    assert any(line.startswith("scenarios:") for line in lines)
