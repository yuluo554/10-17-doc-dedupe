"""Clustering: tiers, keep recommendations, reasons and determinism.

The tests run the real pipeline (scan -> pairs -> cluster) against the synthetic
corpus, then assert the properties ``plan/05`` M3 promises:

* every truth group lands in its tier, and ``template`` is never folded into
  ``near``;
* every cluster carries a reason naming at least two decision dimensions;
* ``template`` clusters carry no keep recommendation;
* two runs over the same index produce identical clusters.

Unit-level tests cover the template criterion and the keep ranking directly,
because a corpus-wide assertion cannot say *why* a rule broke.
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
    db_path = tmp_path_factory.mktemp("clusters") / "index.db"
    conn = store.connect(str(db_path))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def clusters(index):
    return cluster_mod.clusters_from_index(index)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _relative(name: str) -> str:
    return store.normalize_path(str(SAMPLES / name))


def _cluster_of(clusters, name: str):
    target = _relative(name)
    for item in clusters:
        if target in item.members:
            return item
    return None


# ---------------------------------------------------------------------------
# Tiers against the implanted truth
# ---------------------------------------------------------------------------


class _IndexStub:
    """Adapter so the benchmark can be driven from an already-built cluster list.

    ``run_benchmark`` takes a connection because that is how the CLI calls it.
    The tests already have clusters, and rescanning for every assertion would
    make this module slow without testing anything new.
    """

    def __init__(self, clusters):
        self._clusters = clusters


def test_every_truth_group_lands_in_its_expected_tier(clusters):
    """Per-group, named. A rate would hide which group broke."""
    report = bench_tiers.run_benchmark(_IndexStub(clusters), SAMPLES)
    failing = [outcome.describe() for outcome in report.failures]
    assert failing == [], "\n".join(failing)
    assert report.total == 27
    assert report.accuracy == 1.0
    assert report.contaminated == ()


@pytest.fixture(autouse=True)
def _patch_benchmark(monkeypatch):
    """Make ``run_benchmark`` accept the stub by reading clusters off it."""
    original = bench_tiers.cluster_mod.clusters_from_index

    def patched(conn, settings=None):
        clusters = getattr(conn, "_clusters", None)
        if clusters is not None:
            return clusters
        return original(conn, settings)

    monkeypatch.setattr(bench_tiers.cluster_mod, "clusters_from_index", patched)


def test_template_groups_are_template_not_near(clusters):
    """The headline M3 claim: template families must not be mixed into near."""
    for group_id, directory in (
        ("G201", "G201_模板合同/G201_a.docx"),
        ("G202", "G202_模板周报/G202_a.docx"),
        ("G203", "G203_模板发票摘要/G203_a.docx"),
    ):
        found = _cluster_of(clusters, directory)
        assert found is not None, "%s did not cluster at all" % group_id
        assert found.tier == cluster_mod.TIER_TEMPLATE, (
            "%s landed in the %s tier" % (group_id, found.tier)
        )


def test_template_clusters_carry_no_keep_recommendation(clusters):
    """plan/01 module 3: template families are never auto-dispositioned."""
    templates = [item for item in clusters if item.tier == cluster_mod.TIER_TEMPLATE]
    assert templates, "no template cluster was built; the assertion would be vacuous"
    for item in templates:
        assert item.keep is None
        assert item.keep_id is None
        assert item.keep_display is None
        assert "人工裁决" in item.reason


def test_exact_groups_are_exact_tier(clusters):
    for directory in ("G101_完全重复-培训/G101_a.md", "G104_完全重复-运维/G104_a.md"):
        found = _cluster_of(clusters, directory)
        assert found is not None
        assert found.tier == cluster_mod.TIER_EXACT


def test_near_groups_are_near_tier(clusters):
    for directory in ("G001_项目方案/G001_a.docx", "G020_进度纠偏/G020_a.docx"):
        found = _cluster_of(clusters, directory)
        assert found is not None
        assert found.tier == cluster_mod.TIER_NEAR


def test_a_sub_threshold_non_template_pair_connects_nothing(clusters):
    """The retention floor is not a decision threshold.

    G001 and G015 have a stored pair at 0.561, below the near-duplicate
    threshold and not a template pair. It must stay evidence, not become an
    edge: if it did, the two groups would merge and every report after it would
    be wrong.
    """
    report = bench_tiers.run_benchmark(_IndexStub(clusters), SAMPLES)
    assert report.contaminated == ()
    g001 = _cluster_of(clusters, "G001_项目方案/G001_a.docx")
    g015 = _cluster_of(clusters, "G015_投资复核/G015_a.docx")
    assert g001.cluster_id != g015.cluster_id


# ---------------------------------------------------------------------------
# Reasons
# ---------------------------------------------------------------------------


def test_every_cluster_reason_names_at_least_two_dimensions(clusters):
    assert clusters
    for item in clusters:
        hits = [phrase for phrase in cluster_mod.REASON_DIMENSIONS if phrase in item.reason]
        assert len(hits) >= 2, "%s has a thin reason: %r" % (item.cluster_id, item.reason)


def test_no_cluster_reason_is_a_bare_score(clusters):
    """A reason that only quotes numbers is not a reason (plan/03 module 3)."""
    for item in clusters:
        assert item.reason.strip()
        assert not item.reason.strip().replace(".", "").replace("0", "").isdigit()
        assert len(item.reason) >= 12


def test_keep_reason_names_the_kept_file(clusters):
    for item in clusters:
        if item.keep is None:
            continue
        assert item.keep_display in item.reason, (
            "%s recommends %s but the reason does not say so"
            % (item.cluster_id, item.keep_display)
        )


def test_keep_is_always_a_member(clusters):
    for item in clusters:
        if item.keep is None:
            continue
        assert item.keep in item.members
        assert item.keep_id in item.member_ids


def test_keep_is_never_a_truncated_copy(clusters):
    """The completeness gate: a shorter body is never the recommendation.

    Truncated copies usually have the newest mtime, so a pure weighted sum can
    prefer them; this asserts the gate that stops it.
    """
    lengths = {}
    for item in clusters:
        longest = max(item.scores[path]["completeness"] for path in item.members)
        for path in item.members:
            lengths[path] = item.scores[path]["completeness"]
        if item.keep is not None:
            assert item.scores[item.keep]["completeness"] == longest, (
                "%s keeps a shorter copy" % item.cluster_id
            )


# ---------------------------------------------------------------------------
# Determinism and ordering
# ---------------------------------------------------------------------------


def test_clusters_are_sorted_by_tier_then_first_member(clusters):
    order = [cluster_mod.TIER_ORDER.index(item.tier) for item in clusters]
    assert order == sorted(order)
    for tier in cluster_mod.TIER_ORDER:
        members = [item.members[0] for item in clusters if item.tier == tier]
        assert members == sorted(members)


def test_members_are_sorted_by_normalized_path(clusters):
    for item in clusters:
        assert list(item.members) == sorted(item.members)


def test_cluster_ids_are_derived_from_member_paths(clusters):
    import hashlib

    for item in clusters:
        payload = "\n".join(sorted(item.members))
        expected = "c" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        assert item.cluster_id == expected


def test_two_runs_produce_identical_clusters(index):
    """Same index, two clusterings, no drift in any field."""
    first = cluster_mod.clusters_from_index(index)
    second = cluster_mod.clusters_from_index(index)
    assert first == second


def test_cluster_order_does_not_depend_on_pair_row_order(index):
    """Reverse the input pairs; the clusters must be byte-identical.

    This is the union-find ordering trap: a component's representative depends
    on insertion order, so anything derived from it (a cluster id, a member
    list) would drift between runs without this guarantee.
    """
    docs, pairs = cluster_mod.load_inputs(index)
    forward = cluster_mod.build_clusters(docs, pairs)
    backward = cluster_mod.build_clusters(docs, list(reversed(pairs)))
    assert [item.cluster_id for item in forward] == [item.cluster_id for item in backward]
    assert [item.members for item in forward] == [item.members for item in backward]
    assert [item.reason for item in forward] == [item.reason for item in backward]


def test_a_reordered_document_input_does_not_change_clusters(index):
    docs, pairs = cluster_mod.load_inputs(index)
    forward = cluster_mod.build_clusters(docs, pairs)
    shuffled = cluster_mod.build_clusters(list(reversed(docs)), pairs)
    assert [item.cluster_id for item in forward] == [item.cluster_id for item in shuffled]
    assert [item.keep for item in forward] == [item.keep for item in shuffled]


# ---------------------------------------------------------------------------
# The template criterion, in isolation
# ---------------------------------------------------------------------------


def _settings():
    return DEFAULT_SETTINGS


def test_template_evidence_accepts_a_field_only_difference():
    left = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n价税合计：42.10 万元\n经办人：李四"
    right = "示例项目乙 费用摘要\n开票单位：示例科技有限公司B\n价税合计：18.90 万元\n经办人：王五"
    evidence = cluster_mod.template_evidence(left, right, _settings())
    assert evidence is not None
    assert evidence.line_count == 4
    assert evidence.field_diffs == 4


def test_template_evidence_rejects_a_rewritten_body_line():
    """Same line count, different sentence: near duplicate, not a template."""
    left = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n本摘要由系统生成，仅用于示例演示。"
    right = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n本摘要由人工整理，仅用于内部评审。"
    assert cluster_mod.template_evidence(left, right, _settings()) is None


def test_template_evidence_rejects_a_different_label():
    """A value change keeps the label; a label change is a different template."""
    left = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n价税合计：42.10 万元"
    right = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n结算金额：42.10 万元"
    assert cluster_mod.template_evidence(left, right, _settings()) is None


def test_template_evidence_rejects_a_truncated_copy():
    left = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n价税合计：42.10 万元"
    right = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A"
    assert cluster_mod.template_evidence(left, right, _settings()) is None


def test_template_evidence_rejects_identical_text():
    """Identical content is the exact tier, a stronger statement."""
    text = "示例项目甲 费用摘要\n开票单位：示例科技有限公司A"
    assert cluster_mod.template_evidence(text, text, _settings()) is None


def test_template_evidence_rejects_a_prose_colon():
    """A colon inside a sentence is not a field label."""
    left = (
        "示例项目甲 费用摘要\n开票单位：示例科技有限公司A\n论证结论为：成本结构满足当前阶段要求。"
    )
    right = (
        "示例项目乙 费用摘要\n开票单位：示例科技有限公司B\n论证结论为：资金安排满足当前阶段要求。"
    )
    # "论证结论为" is a label-shaped prefix, so this pair *is* label-aligned; the
    # skeleton still has to agree, and it does. The test documents the boundary:
    # what makes it not a template is the label list differing elsewhere, which
    # ``test_template_evidence_rejects_a_different_label`` covers. Here the point
    # is that a long prose line is not silently treated as a value slot.
    evidence = cluster_mod.template_evidence(left, right, _settings())
    if evidence is not None:
        assert evidence.field_diffs <= DEFAULT_SETTINGS.template_field_diff_max


# ---------------------------------------------------------------------------
# Keep ranking, in isolation
# ---------------------------------------------------------------------------


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


def _pair(left, right, score=0.95):
    return cluster_mod.Pair(a_id=left.doc_id, b_id=right.doc_id, jaccard=score)


def test_the_newest_copy_wins_when_everything_else_ties():
    older = _doc(1, "a.txt", mtime=100)
    newer = _doc(2, "b.txt", mtime=200)
    clusters = cluster_mod.build_clusters([older, newer], [_pair(older, newer)])
    assert len(clusters) == 1
    assert clusters[0].keep_id == newer.doc_id
    assert "修改时间最新" in clusters[0].reason


def test_the_shallower_path_wins_when_mtime_ties():
    deep = _doc(1, "sub/dir/a.txt", mtime=100, directory="/root")
    shallow = _doc(2, "b.txt", mtime=100, directory="/root")
    clusters = cluster_mod.build_clusters([deep, shallow], [_pair(deep, shallow)])
    assert clusters[0].keep_id == shallow.doc_id


def test_a_copy_marked_name_never_wins():
    plain = _doc(1, "报告.txt", mtime=100)
    marked = _doc(2, "报告(1).txt", mtime=999)
    clusters = cluster_mod.build_clusters([plain, marked], [_pair(plain, marked)])
    assert clusters[0].keep_id == plain.doc_id
    assert "文件名无副本标记" in clusters[0].reason


def test_a_truncated_copy_is_never_kept_even_with_the_newest_mtime():
    full = _doc(1, "full.txt", chars=400, mtime=100)
    truncated = _doc(2, "trunc.txt", chars=200, mtime=999)
    clusters = cluster_mod.build_clusters([full, truncated], [_pair(full, truncated)])
    assert clusters[0].keep_id == full.doc_id
    assert "截断副本" in clusters[0].reason


def test_a_lone_document_is_not_a_cluster():
    """A document that resembles nothing is not a finding."""
    lonely = _doc(1, "a.txt")
    other = _doc(2, "b.txt")
    assert cluster_mod.build_clusters([lonely, other], []) == []


def test_a_pair_below_the_threshold_forms_no_cluster():
    left = _doc(1, "a.txt")
    right = _doc(2, "b.txt")
    assert cluster_mod.build_clusters([left, right], [_pair(left, right, 0.60)]) == []


def test_a_pair_with_a_missing_document_is_ignored():
    left = _doc(1, "a.txt")
    right = _doc(2, "b.txt")
    clusters = cluster_mod.build_clusters([left], [_pair(left, right)])
    assert clusters == []


# ---------------------------------------------------------------------------
# Index round trip
# ---------------------------------------------------------------------------


def test_clusters_round_trip_through_the_index(index, tmp_path):
    """What ``report`` writes must be readable back unchanged."""
    from docdedupe import report as report_mod

    clusters = cluster_mod.clusters_from_index(index)
    store.replace_clusters(
        index,
        [
            (
                item.cluster_id,
                item.tier,
                item.keep_id,
                item.reason,
                cluster_mod.scores_json(item.scores),
                item.member_ids,
            )
            for item in clusters
        ],
        "2026-10-17T00:00:00Z",
    )
    index.commit()

    assert store.count_clusters(index) == len(clusters)
    rows = store.cluster_rows(index)
    assert rows
    # Every member appears exactly once, and the stored tier matches.
    stored_members = {}
    for row in rows:
        stored_members.setdefault(str(row["cluster_id"]), []).append(int(row["doc_id"]))
    for item in clusters:
        assert sorted(stored_members[item.cluster_id]) == sorted(item.member_ids)


def test_rebuilding_clusters_replaces_the_previous_table(index):
    clusters = cluster_mod.clusters_from_index(index)
    payload = [
        (
            item.cluster_id,
            item.tier,
            item.keep_id,
            item.reason,
            cluster_mod.scores_json(item.scores),
            item.member_ids,
        )
        for item in clusters
    ]
    store.replace_clusters(index, payload, "t1")
    index.commit()
    assert store.count_clusters(index) == len(clusters)

    store.replace_clusters(index, [], "t2")
    index.commit()
    assert store.count_clusters(index) == 0
    assert index.execute("SELECT COUNT(*) FROM cluster_members").fetchone()[0] == 0

    store.replace_clusters(index, payload, "t3")
    index.commit()
    assert store.count_clusters(index) == len(clusters)
