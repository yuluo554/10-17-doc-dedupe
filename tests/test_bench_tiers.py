"""Benchmark B4: tier accuracy against the implanted truth, plus injections.

B3 (``test_recall.py``) proves the duplicate *pairs* were found. B4 proves the
next claim, which is the one M3 actually delivers: each group landed in the tier
the documented definitions imply, template families did not get mixed into
near-duplicates, and every keep recommendation names a real decision.

The injection tests at the bottom are the point of the file. A benchmark that
cannot fail is not a gate, so each one breaks the pipeline in a way a plausible
bug would and asserts the benchmark reports it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from docdedupe import cluster as cluster_mod
from docdedupe import scanner, store
from docdedupe.config import DEFAULT_SETTINGS

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
MANIFEST = SAMPLES / "truth" / "manifest.json"

sys.path.insert(0, str(REPO_ROOT / "tools"))

import bench_tiers  # noqa: E402

pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(), reason="sample corpus is not generated"
)


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("b4") / "index.db"
    conn = store.connect(str(db_path))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
        yield conn
    finally:
        conn.close()


def _report(conn, **kwargs):
    return bench_tiers.run_benchmark(conn, SAMPLES, **kwargs)


# ---------------------------------------------------------------------------
# The gate holds
# ---------------------------------------------------------------------------


def test_every_group_passes_and_none_are_contaminated(index):
    report = _report(index)
    failing = [outcome.describe() for outcome in report.failures]
    assert failing == [], "\n".join(failing)
    assert report.contaminated == ()
    assert report.total == 27
    assert report.passed == 27


def test_the_gate_names_the_tier_it_expected_not_the_manifest_tier(index):
    """G005/G010/G011 are generated as variants but normalize to identical text.

    Their manifest ``tier`` says ``near`` (they are variants of one document);
    the documented definition of ``exact`` is "normalized bodies byte-identical"
    (data/README.md 4.5), which is what they are. The benchmark derives the
    expectation from the members' ``variant`` labels, so it cannot agree with
    the engine by construction.
    """
    report = _report(index)
    outcomes = {outcome.group_id: outcome for outcome in report.outcomes}
    for group_id in ("G005", "G010", "G011"):
        assert outcomes[group_id].truth_tier == "near"
        assert outcomes[group_id].expected_tier == "exact"
        assert outcomes[group_id].found_tiers == ("exact",)
        assert outcomes[group_id].passed


def test_every_template_group_is_template(index):
    report = _report(index)
    outcomes = {outcome.group_id: outcome for outcome in report.outcomes}
    for group_id in ("G201", "G202", "G203"):
        assert outcomes[group_id].found_tiers == ("template",)
        assert outcomes[group_id].keep is None


def test_no_group_is_left_unclustered(index):
    report = _report(index)
    for outcome in report.outcomes:
        assert outcome.complete, outcome.describe()


def test_the_reason_gate_counts_dimensions_from_the_cluster_module(index):
    """The benchmark must count the same phrases the builder writes.

    A benchmark with its own private list of "dimension words" could drift from
    the reason builder and start passing reasons the product would reject.
    """
    assert bench_tiers.cluster_mod.REASON_DIMENSIONS is cluster_mod.REASON_DIMENSIONS
    report = _report(index)
    assert all(outcome.dimensions >= bench_tiers.MIN_REASON_DIMENSIONS for outcome in report.outcomes)


# ---------------------------------------------------------------------------
# The gate can fail
# ---------------------------------------------------------------------------


def test_the_gate_fails_when_template_clusters_are_relabelled_near(index):
    """The headline defect: template families mixed into near duplicates."""
    report = _report(index, inject_clusters="template-as-near")
    ok, failures = bench_tiers.evaluate(report)
    assert not ok
    assert report.accuracy < 1.0
    assert any("G201" in failure or "accuracy" in failure for failure in failures)


def test_the_gate_fails_when_near_clusters_are_relabelled_template(index):
    """The opposite defect: the tier that is never dispositioned swallows real copies."""
    report = _report(index, inject_clusters="near-as-template")
    ok, failures = bench_tiers.evaluate(report)
    assert not ok
    assert report.accuracy < 1.0


def test_the_gate_fails_when_the_near_threshold_is_lowered(index):
    """0.5 merges G001 with G015 through their 0.561 pair.

    This is the injection that caught a hole in the first version of this
    benchmark: per-group tier labels stayed correct while the cluster those
    groups sat in was wrong. The contamination gate exists because of it.
    """
    settings = DEFAULT_SETTINGS.with_overrides(near_duplicate_threshold=0.5)
    report = _report(index, settings=settings)
    ok, failures = bench_tiers.evaluate(report)
    assert not ok
    assert report.contaminated
    assert any("mix truth groups" in failure for failure in failures)


def test_the_gate_fails_on_an_empty_index(tmp_path):
    """No clusters must not read as success."""
    conn = store.connect(str(tmp_path / "empty.db"))
    try:
        report = _report(conn)
    finally:
        conn.close()
    assert report.passed == 0
    assert report.total == 27
    ok, failures = bench_tiers.evaluate(report)
    assert not ok
    assert any("no clusters" in failure for failure in failures)


def test_the_keep_gate_fails_when_a_template_gets_a_keep(tmp_path):
    """A relabel is not needed to trip this one: build the bad state directly."""
    left = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n价税合计：42.10 万元\n经办人：李四"
    right = "示例项目乙 费用摘要\n开票单位：示例科技有限公司B\n价税合计：18.90 万元\n经办人：王五"
    root = tmp_path / "g"

    def doc(doc_id, name, text):
        path = store.normalize_path(str(root / name))
        return cluster_mod.Document(
            doc_id=doc_id,
            path_abs=str(root / name),
            path_norm=path,
            ext=".docx",
            text_sha256="t%d" % doc_id,
            norm_sha256="n%d" % doc_id,
            norm_chars=len(text),
            mtime_ns=doc_id,
            text=text,
        )

    docs = [doc(1, "a.docx", left), doc(2, "b.docx", right)]
    built = cluster_mod.build_clusters(
        docs, [cluster_mod.Pair(a_id=1, b_id=2, jaccard=0.7)]
    )
    assert built and built[0].tier == "template"
    assert built[0].keep is None

    # A cluster with a keep on the template tier is what a bug would produce.
    bad = built[0].__class__(
        cluster_id=built[0].cluster_id,
        members=built[0].members,
        member_ids=built[0].member_ids,
        displays=built[0].displays,
        tier="template",
        keep=built[0].members[0],
        keep_id=built[0].member_ids[0],
        keep_display=built[0].displays[0],
        reason=built[0].reason,
        scores=built[0].scores,
    )
    outcome = bench_tiers.evaluate_groups(
        [
            {
                "group_id": "GXXX",
                "tier": "template",
                "files": ["a.docx", "b.docx"],
                "members": [{"variant": "template"}, {"variant": "template"}],
            }
        ],
        [bad],
        root,
    )[0]
    assert outcome.keep_ok is False
    assert "keep=" in outcome.describe()


def test_the_reason_gate_fails_on_a_bare_score(tmp_path):
    left = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n价税合计：42.10 万元"
    right = "示例项目乙 费用摘要\n开票单位：示例科技有限公司B\n价税合计：18.90 万元"
    root = tmp_path / "g"

    def doc(doc_id, name, text):
        path = store.normalize_path(str(root / name))
        return cluster_mod.Document(
            doc_id=doc_id,
            path_abs=str(root / name),
            path_norm=path,
            ext=".docx",
            text_sha256="t%d" % doc_id,
            norm_sha256="n%d" % doc_id,
            norm_chars=len(text),
            mtime_ns=doc_id,
            text=text,
        )

    built = cluster_mod.build_clusters(
        [doc(1, "a.docx", left), doc(2, "b.docx", right)],
        [cluster_mod.Pair(a_id=1, b_id=2, jaccard=0.7)],
    )[0]
    bare = built.__class__(
        cluster_id=built.cluster_id,
        members=built.members,
        member_ids=built.member_ids,
        displays=built.displays,
        tier=built.tier,
        keep=built.keep,
        keep_id=built.keep_id,
        keep_display=built.keep_display,
        reason="相似度 0.70",
        scores=built.scores,
    )
    assert bench_tiers.count_dimensions(bare.reason) == 0
    outcome = bench_tiers.evaluate_groups(
        [
            {
                "group_id": "GTMPL",
                "tier": "template",
                "files": ["a.docx", "b.docx"],
                "members": [{"variant": "template"}, {"variant": "template"}],
            }
        ],
        [bare],
        root,
    )[0]
    assert outcome.reason_ok is False


# ---------------------------------------------------------------------------
# The truth-derivation rule itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variants, expected",
    [
        (("original", "reformat"), "exact"),
        (("original", "fullwidth"), "exact"),
        (("original", "header_footer"), "exact"),
        (("original", "rewrite"), "near"),
        (("original", "truncate"), "near"),
        (("original", "rewrite", "truncate"), "near"),
    ],
)
def test_expected_tier_derivation(variants, expected):
    group = {
        "tier": "near",
        "members": [{"variant": variant} for variant in variants],
    }
    assert bench_tiers.expected_tier(group) == expected


def test_expected_tier_keeps_explicit_tiers():
    for tier in ("exact", "template"):
        group = {"tier": tier, "members": [{"variant": "original"}]}
        assert bench_tiers.expected_tier(group) == tier


def test_the_manifest_carries_no_scores():
    """Truth records the facts, never the engine's answers (data/README.md 2.3)."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    text = json.dumps(manifest)
    for forbidden in ("jaccard", "similarity", "score", "observed_"):
        assert forbidden not in text, "truth manifest carries %r" % forbidden
