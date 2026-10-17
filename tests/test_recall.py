"""Benchmark B3: recall and false positives against the implanted truth.

The two gates are both-sided on purpose (``plan/04`` §五):

* every near group must be recalled, **named individually** — a summary rate
  hides which group broke, and with twenty groups a single failure is the whole
  signal;
* every negative must stay alone — an engine that connects everything to
  everything passes the recall gate and fails here.

Both tests run the real pipeline (scan -> normalize -> fingerprint -> pairs ->
recall) rather than calling the scorer directly, so the gate covers the wiring
too. A defect injection at the end proves the gate can fail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from docdedupe import scanner, store
from docdedupe.config import DEFAULT_SETTINGS
from docdedupe.recall import evaluate_recall

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
MANIFEST = SAMPLES / "truth" / "manifest.json"

sys.path.insert(0, str(REPO_ROOT / "tools"))

import bench_recall  # noqa: E402

pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(), reason="sample corpus is not generated"
)

# data/README.md 4.7: 20 constructed groups, recall floor 90%.
RECALL_FLOOR = 0.90


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    """Scan the corpus once and hand the connection to every test in the module."""
    db_path = tmp_path_factory.mktemp("recall") / "index.db"
    conn = store.connect(str(db_path))
    try:
        summary = scanner.scan(
            conn,
            [SAMPLES],
            scanned_at="2026-10-17T00:00:00Z",
            clock=lambda: 0.0,
        )
        conn.commit()
        yield conn, summary
    finally:
        conn.close()


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _report(conn):
    return bench_recall.run_benchmark(conn, SAMPLES)


# ---------------------------------------------------------------------------
# The scan writes what the benchmark reads
# ---------------------------------------------------------------------------


def test_scan_writes_a_fingerprint_for_every_usable_document(index):
    conn, summary = index
    usable = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE extract_status = ?", (store.STATUS_OK,)
    ).fetchone()[0]
    assert store.count_fingerprints(conn) == usable
    assert summary.fingerprinted == usable


def test_scan_writes_normalized_text_for_every_usable_document(index):
    conn, _ = index
    rows = conn.execute(
        "SELECT norm_text, norm_sha256 FROM documents WHERE extract_status = ?",
        (store.STATUS_OK,),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["norm_text"], "normalized text is still empty"
        assert row["norm_sha256"]

    # The empty string is what module 1 wrote; its sha256 would be the hash of
    # nothing, so a row that still carries that value was never normalized.
    import hashlib

    empty_sha = hashlib.sha256(b"").hexdigest()
    assert all(row["norm_sha256"] != empty_sha for row in rows)


def test_skipped_documents_get_no_fingerprint(index):
    """A skipped file has no text; a fingerprint for one would be a fiction."""
    conn, _ = index
    skips = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE extract_status != ?", (store.STATUS_OK,)
    ).fetchone()[0]
    assert skips > 0
    joined = conn.execute(
        "SELECT COUNT(*) FROM fingerprints AS f JOIN documents AS d ON d.id = f.doc_id "
        "WHERE d.extract_status != ?",
        (store.STATUS_OK,),
    ).fetchone()[0]
    assert joined == 0


def test_every_pair_row_records_the_score_kind(index):
    conn, _ = index
    rows = store.pair_rows(conn)
    assert rows
    for row in rows:
        assert row["score_kind"] == "exact_jaccard"
        assert 0.0 <= row["jaccard"] <= 1.0
        assert 0.0 <= row["jaccard_estimate"] <= 1.0
        assert row["a_id"] < row["b_id"]


# ---------------------------------------------------------------------------
# Gate 1: recall, per group
# ---------------------------------------------------------------------------


def test_every_near_group_is_recalled_and_named(index):
    """The failure message lists the groups that broke, not just a percentage."""
    conn, _ = index
    report = _report(conn)

    assert report.near_total >= 20
    failed = [group for group in report.groups if group.tier == "near" and not group.passed]
    detail = "\n".join(group.describe() for group in report.groups if group.tier == "near")
    assert failed == [], "near group(s) not recalled:\n%s" % detail
    assert report.rate == 1.0
    assert report.rate >= RECALL_FLOOR


def test_the_per_group_report_names_all_twenty_groups(index):
    """A summary rate can hide a group that was never evaluated at all."""
    conn, _ = index
    report = _report(conn)
    named = {group.group_id for group in report.groups if group.tier == "near"}
    expected = {group["group_id"] for group in _manifest()["groups"] if group["tier"] == "near"}
    assert named == expected
    assert len(named) == 20


# ---------------------------------------------------------------------------
# Gate 2: false positives
# ---------------------------------------------------------------------------


def test_no_negative_group_shares_a_cluster_with_another_group(index):
    conn, _ = index
    report = _report(conn)
    offenders = [group.describe() for group in report.negative_violations]
    assert offenders == [], "negative groups merged with a foreign group:\n%s" % "\n".join(offenders)


def test_negative_groups_are_all_evaluated(index):
    """Zero violations is only meaningful if the negatives were actually checked."""
    conn, _ = index
    report = _report(conn)
    negatives = {group.group_id for group in report.groups if group.tier == "negative"}
    expected = {group["group_id"] for group in _manifest()["groups"] if group["tier"] == "negative"}
    assert negatives == expected
    assert len(negatives) >= 5


def test_g010_is_joined_after_normalization(index):
    """The full-width twin must be recalled, which is the B2 claim end to end."""
    conn, _ = index
    report = _report(conn)
    group = next(item for item in report.groups if item.group_id == "G010")
    assert group.components == 1 and group.passed


def test_g011_is_joined_despite_header_footer_and_page_numbers(index):
    conn, _ = index
    report = _report(conn)
    group = next(item for item in report.groups if item.group_id == "G011")
    assert group.components == 1 and group.passed


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_two_scans_produce_identical_fingerprints_and_pairs(tmp_path):
    """Re-running the pipeline must not drift a single stored field.

    ``scanned_at`` is excluded: it records *when*, not *what*. Every other column
    of ``fingerprints`` and ``pairs`` is content-derived and must match.
    """
    snapshots = []
    for run in ("first", "second"):
        conn = store.connect(str(tmp_path / ("%s.db" % run)))
        try:
            scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
            conn.commit()
            fingerprints = [
                dict(row)
                for row in conn.execute(
                    "SELECT doc_id, simhash, minhash_json, shingle_count, algo_version, "
                    "shingle_hashes FROM fingerprints ORDER BY doc_id"
                )
            ]
            pairs = [dict(row) for row in store.pair_rows(conn)]
        finally:
            conn.close()
        snapshots.append((fingerprints, pairs))

    assert snapshots[0][0] == snapshots[1][0]
    assert snapshots[0][1] == snapshots[1][1]


def test_a_second_scan_reuses_fingerprints_without_reopening_files(index, tmp_path):
    """The cache hit path must not re-extract, and must not lose the fingerprint."""
    conn, _ = index
    before = store.count_fingerprints(conn)
    summary = scanner.scan(conn, [SAMPLES], scanned_at="2026-10-18T00:00:00Z", clock=lambda: 0.0)
    assert summary.cache_hits == summary.files_indexed
    assert summary.extracted == 0
    # Nothing was stale, so nothing needed recomputing.
    assert summary.fingerprinted_from_cache == 0
    assert store.count_fingerprints(conn) == before


def test_a_missing_fingerprint_is_rebuilt_from_stored_text(tmp_path):
    """An index with text but no fingerprint must heal without reopening a file."""
    conn = store.connect(str(tmp_path / "index.db"))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
        conn.execute("DELETE FROM fingerprints")
        conn.commit()

        summary = scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
        usable = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE extract_status = ?", (store.STATUS_OK,)
        ).fetchone()[0]
        assert store.count_fingerprints(conn) == usable
        assert summary.fingerprinted_from_cache == usable
        assert summary.extracted == 0
    finally:
        conn.close()


def test_pair_rows_are_replaced_not_merged(tmp_path):
    """``pairs`` is derived state: a pair that no longer exists must disappear."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("示例项目甲\n" + "正文内容。" * 60, encoding="utf-8")
    (root / "b.txt").write_text("示例项目甲\n" + "正文内容。" * 60, encoding="utf-8")

    conn = store.connect(str(tmp_path / "index.db"))
    try:
        scanner.scan(conn, [root], scanned_at="t", clock=lambda: 0.0)
        conn.commit()
        assert store.count_pairs(conn) == 1

        (root / "b.txt").write_text("完全不同的另一份文档。" * 60, encoding="utf-8")
        scanner.scan(conn, [root], scanned_at="t2", clock=lambda: 0.0)
        conn.commit()
        assert store.count_pairs(conn) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The recall skip is evidence-based, not a flag
# ---------------------------------------------------------------------------


def test_recall_is_reused_on_an_unchanged_second_scan(tmp_path):
    """The skip has to be observable, not just an optimisation nobody can see.

    A separate database keeps the module-level fixture free of ordering effects.
    """
    conn = store.connect(str(tmp_path / "reuse.db"))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="t", clock=lambda: 0.0)
        conn.commit()
        summary = scanner.scan(conn, [SAMPLES], scanned_at="t2", clock=lambda: 0.0)
        assert summary.recall_reused is True
        # The counters still describe the stored table, not an empty one.
        assert summary.pairs == store.count_pairs(conn)
        assert summary.pairs > 0
    finally:
        conn.close()


def test_a_tampered_pair_table_forces_a_recompute(tmp_path):
    """The skip must verify the result, not only the input.

    A hand-edited or truncated table with unchanged inputs would otherwise be
    trusted forever, and every report after it would be wrong.
    """
    conn = store.connect(str(tmp_path / "tamper.db"))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="t", clock=lambda: 0.0)
        conn.commit()
        baseline = store.count_pairs(conn)

        # Drop one edge while leaving every input untouched.
        conn.execute("DELETE FROM pairs WHERE rowid = (SELECT MIN(rowid) FROM pairs)")
        conn.commit()
        assert store.count_pairs(conn) == baseline - 1

        summary = scanner.scan(conn, [SAMPLES], scanned_at="t", clock=lambda: 0.0)
        conn.commit()
        assert summary.recall_reused is False
        assert store.count_pairs(conn) == baseline
    finally:
        conn.close()


def test_a_changed_document_forces_a_recompute(tmp_path):
    conn = store.connect(str(tmp_path / "changed.db"))
    try:
        root = tmp_path / "docs"
        root.mkdir()
        (root / "a.txt").write_text("示例项目甲\n" + "正文内容。" * 60, encoding="utf-8")
        (root / "b.txt").write_text("示例项目甲\n" + "正文内容。" * 60, encoding="utf-8")

        scanner.scan(conn, [root], scanned_at="t", clock=lambda: 0.0)
        conn.commit()
        assert store.count_pairs(conn) == 1

        (root / "b.txt").write_text("示例项目甲\n" + "改写后的正文内容。" * 40, encoding="utf-8")
        summary = scanner.scan(conn, [root], scanned_at="t2", clock=lambda: 0.0)
        conn.commit()
        assert summary.recall_reused is False
        assert summary.extracted == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fault injection: the gate must be falsifiable
# ---------------------------------------------------------------------------


def test_the_gate_fails_when_the_edges_are_removed(index):
    """``drop-above`` deletes the strongest edges; the benchmark must notice."""
    conn, _ = index
    report = bench_recall.run_benchmark(conn, SAMPLES, inject_edges="drop-above")
    ok, failures = bench_recall.evaluate(report)
    assert not ok
    assert report.rate < 1.0
    assert any("recall" in failure for failure in failures)


def test_the_negative_gate_fails_when_a_foreign_edge_is_added(index):
    """One injected edge between a negative and a near group must trip gate 2."""
    conn, _ = index
    groups = bench_recall.load_groups(MANIFEST)
    edges = bench_recall.load_edges(conn, SAMPLES)
    near = next(group for group in groups if group["tier"] == "near")
    negative = next(group for group in groups if group["tier"] == "negative")
    edges.append((negative["files"][0], near["files"][0], 0.99))

    report = evaluate_recall(groups, edges, DEFAULT_SETTINGS.near_duplicate_threshold)
    ok, failures = bench_recall.evaluate(report)
    assert not ok
    assert report.negative_violations
    assert any("negative" in failure for failure in failures)


def test_the_gate_fails_when_the_truth_is_scrambled(index):
    """Agreeing with a deliberately wrong truth is impossible for a real engine."""
    conn, _ = index
    report = bench_recall.run_benchmark(conn, SAMPLES, inject_groups="shift-groups")
    ok, failures = bench_recall.evaluate(report)
    assert not ok
    assert report.rate < RECALL_FLOOR
    assert any("not recalled" in failure for failure in failures)


def test_the_benchmark_would_not_pass_on_an_empty_index(tmp_path):
    """A vacuous gate: no documents means no pairs, which must not read as success."""
    conn = store.connect(str(tmp_path / "empty.db"))
    try:
        report = bench_recall.run_benchmark(conn, SAMPLES)
    finally:
        conn.close()
    assert report.near_total == 20
    assert report.rate == 0.0
    ok, _ = bench_recall.evaluate(report)
    assert not ok
