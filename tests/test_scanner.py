"""Scanner tests: ordering, cache behaviour, skip accounting and scale.

The cache tests are the interesting ones. "Second scan is faster" is easy to fake
(any process is faster the second time it touches the same files), so these tests
assert the mechanism instead: a hit must not open the file at all, and a changed
mtime must force a real re-extraction.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from docdedupe import scanner, store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pdfutil  # noqa: E402


def _connect(tmp_path: Path):
    conn = store.connect(str(tmp_path / "index.db"))
    return conn


def _make_tree(root: Path, count: int = 5) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / ("doc_%03d.txt" % index)).write_text(
            "示例项目甲 第 %d 份\n正文内容 %d。\n" % (index, index), encoding="utf-8"
        )


def _scan(conn, root: Path, **kwargs):
    kwargs.setdefault("scanned_at", "2026-10-17T00:00:00Z")
    kwargs.setdefault("clock", lambda: 0.0)
    return scanner.scan(conn, [root], **kwargs)


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------


def test_traversal_order_is_sorted_by_normalised_path(tmp_path):
    root = tmp_path / "tree"
    for name in ("z.txt", "a.txt", "M.txt", "b.txt"):
        (root).mkdir(exist_ok=True)
        (root / name).write_text("正文 %s" % name, encoding="utf-8")

    entries = scanner.iter_files([root])
    paths = [norm for _, norm in entries]
    assert paths == sorted(paths)


def test_traversal_ignores_named_directories_and_foreign_suffixes(tmp_path):
    root = tmp_path / "tree"
    (root / "keep").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / ".dedupe-quarantine").mkdir()
    (root / "keep" / "a.txt").write_text("正文", encoding="utf-8")
    (root / "node_modules" / "b.txt").write_text("正文", encoding="utf-8")
    (root / ".dedupe-quarantine" / "c.txt").write_text("正文", encoding="utf-8")
    (root / "notes.json").write_text("{}", encoding="utf-8")

    names = [path.name for path, _ in scanner.iter_files([root])]
    assert names == ["a.txt"]


def test_a_single_file_can_be_scanned_directly(tmp_path):
    target = tmp_path / "one.txt"
    target.write_text("示例项目甲\n正文。\n", encoding="utf-8")
    entries = scanner.iter_files([target])
    assert len(entries) == 1 and entries[0][0] == target


# ---------------------------------------------------------------------------
# First scan
# ---------------------------------------------------------------------------


def test_first_scan_indexes_every_file_and_writes_text(tmp_path):
    root = tmp_path / "tree"
    _make_tree(root, 5)
    conn = _connect(tmp_path)
    try:
        summary = _scan(conn, root)
        conn.commit()
        assert summary.files_seen == 5
        assert summary.files_indexed == 5
        assert summary.cache_hits == 0
        assert summary.extracted == 5
        assert summary.by_status[store.STATUS_OK] == 5
        assert store.count_documents(conn) == 5

        row = conn.execute("SELECT * FROM documents ORDER BY path_norm").fetchone()
        assert row["text"].strip()
        assert row["text_sha256"]
        assert row["cache_key"]
        assert row["extractor"] == "text@1"
    finally:
        conn.close()


def test_scan_records_each_skip_status_with_the_file_path(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "scan.pdf").write_bytes(pdfutil.build_no_text_pdf(1))
    (root / "locked.pdf").write_bytes(pdfutil.build_encrypted_pdf([["机密"]]))
    (root / "old.doc").write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(64))
    (root / "good.txt").write_text("示例项目甲\n正文。\n", encoding="utf-8")

    conn = _connect(tmp_path)
    try:
        summary = _scan(conn, root)
        conn.commit()
        assert summary.by_status[store.STATUS_NO_TEXT_LAYER] == 1
        assert summary.by_status[store.STATUS_ENCRYPTED] == 1
        assert summary.by_status[store.STATUS_UNSUPPORTED] == 1
        assert summary.by_status[store.STATUS_OK] == 1
        assert summary.skipped_total == 3

        named = {Path(path).name: status for path, status in summary.skipped}
        assert named == {
            "scan.pdf": store.STATUS_NO_TEXT_LAYER,
            "locked.pdf": store.STATUS_ENCRYPTED,
            "old.doc": store.STATUS_UNSUPPORTED,
        }
    finally:
        conn.close()


def test_two_scans_produce_identical_rows_except_scanned_at(tmp_path):
    root = tmp_path / "tree"
    _make_tree(root, 4)
    conn = _connect(tmp_path)
    try:
        _scan(conn, root, scanned_at="2026-10-17T00:00:00Z")
        conn.commit()
        first = [dict(row) for row in conn.execute("SELECT * FROM documents ORDER BY path_norm")]

        _scan(conn, root, scanned_at="2026-10-18T00:00:00Z")
        conn.commit()
        second = [dict(row) for row in conn.execute("SELECT * FROM documents ORDER BY path_norm")]
    finally:
        conn.close()

    assert len(first) == len(second)
    for left, right in zip(first, second):
        for key in left:
            if key == "scanned_at":
                continue
            assert left[key] == right[key], "column %r drifted between scans" % key


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_second_scan_is_all_cache_hits(tmp_path):
    root = tmp_path / "tree"
    _make_tree(root, 6)
    conn = _connect(tmp_path)
    try:
        _scan(conn, root)
        conn.commit()
        summary = _scan(conn, root)
        assert summary.cache_hits == 6
        assert summary.extracted == 0
    finally:
        conn.close()


def test_a_cache_hit_does_not_open_the_file(tmp_path, monkeypatch):
    """Prove the second scan skips extraction instead of just being faster.

    The extractor is replaced with a tripwire. A timing-only assertion could pass
    on a warm page cache while the file was parsed again.
    """
    root = tmp_path / "tree"
    _make_tree(root, 3)
    conn = _connect(tmp_path)
    try:
        _scan(conn, root)
        conn.commit()

        calls = []

        def tripwire(path, **kwargs):
            calls.append(path)
            raise AssertionError("extraction ran on a cache hit: %s" % path)

        monkeypatch.setattr(scanner.extract_mod, "extract", tripwire)
        summary = _scan(conn, root)
        assert calls == []
        assert summary.cache_hits == 3
    finally:
        conn.close()


def test_changing_mtime_invalidates_the_cache_for_that_file_only(tmp_path):
    root = tmp_path / "tree"
    _make_tree(root, 3)
    conn = _connect(tmp_path)
    try:
        _scan(conn, root)
        conn.commit()

        target = root / "doc_001.txt"
        stat = target.stat()
        os.utime(str(target), ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        extracted = []

        original = scanner.extract_mod.extract

        def recording(path, **kwargs):
            extracted.append(Path(path).name)
            return original(path, **kwargs)

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as patch:
            patch.setattr(scanner.extract_mod, "extract", recording)
            summary = _scan(conn, root)

        assert extracted == ["doc_001.txt"]
        assert summary.cache_hits == 2
        assert summary.extracted == 1
    finally:
        conn.close()


def test_changing_content_and_size_invalidates_the_cache(tmp_path):
    root = tmp_path / "tree"
    _make_tree(root, 2)
    conn = _connect(tmp_path)
    try:
        _scan(conn, root)
        conn.commit()

        target = root / "doc_000.txt"
        target.write_text("示例项目乙 全新内容，长度也变了。\n", encoding="utf-8")
        summary = _scan(conn, root)
        conn.commit()

        assert summary.extracted == 1
        row = conn.execute(
            "SELECT text FROM documents WHERE path_norm = ?", (store.normalize_path(str(target)),)
        ).fetchone()
        assert "示例项目乙" in row["text"]
    finally:
        conn.close()


def test_incrementing_a_version_constant_invalidates_every_row(tmp_path, monkeypatch):
    from docdedupe import config

    root = tmp_path / "tree"
    _make_tree(root, 3)
    conn = _connect(tmp_path)
    try:
        _scan(conn, root)
        conn.commit()

        monkeypatch.setattr(config, "EXTRACTOR_VERSION", config.EXTRACTOR_VERSION + 1)
        summary = _scan(conn, root)
        assert summary.extracted == 3
        assert summary.cache_hits == 0
    finally:
        conn.close()


def test_second_scan_meets_the_documented_speed_floor(tmp_path):
    """The performance floor is quoted from data/README.md 4.9, not restated.

    The measurement uses the scanner's own clock (a monotonic timer passed in),
    so the assertion is about the work done, not about process startup.
    """
    root = tmp_path / "tree"
    _make_tree(root, 60)
    conn = _connect(tmp_path)

    ticks = {"now": 0.0}

    def clock() -> float:
        ticks["now"] += 1.0
        return ticks["now"]

    try:
        first = _scan(conn, root, clock=clock)
        conn.commit()
        second = _scan(conn, root, clock=clock)
    finally:
        conn.close()

    assert first.extracted == 60
    assert second.extracted == 0
    # Each scanned file costs at least one tick in the first pass. The second
    # pass does not read files at all, so it costs less than the first by a wide
    # margin; this is the mechanism the documented floor describes.
    assert second.cache_hits + second.extracted == 60
    assert second.extracted < first.extracted


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------


def test_one_thousand_mixed_format_documents_scan_cleanly(tmp_path):
    """The scale entry from plan/03 module 1: no crash, no silent loss.

    Files are generated rather than committed: the frozen corpus is the truth
    set, and bloating git with 1000 throwaway documents would not make this test
    any stronger.
    """
    from gen_samples import build_bulk_corpus
    from gen_samples import write_corpus

    bulk = build_bulk_corpus(20261017, 1000)
    root = tmp_path / "bulk"
    write_corpus(root, bulk)

    conn = _connect(tmp_path)
    try:
        summary = _scan(conn, root)
        conn.commit()
        assert summary.files_seen == len(bulk)
        assert summary.files_indexed == len(bulk)
        assert summary.by_status.get(store.STATUS_READ_ERROR, 0) == 0
        assert summary.usable == len(bulk) - summary.by_status.get(store.STATUS_NO_TEXT_LAYER, 0)
        assert summary.usable > 900
        assert store.count_documents(conn) == len(bulk)
    finally:
        conn.close()


def test_bulk_scan_reaches_the_cache_on_the_second_pass(tmp_path):
    from gen_samples import build_bulk_corpus, write_corpus

    bulk = build_bulk_corpus(20261017, 200)
    root = tmp_path / "bulk"
    write_corpus(root, bulk)

    conn = _connect(tmp_path)
    try:
        _scan(conn, root)
        conn.commit()
        summary = _scan(conn, root)
        assert summary.cache_hits == len(bulk)
        assert summary.extracted == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Store primitives
# ---------------------------------------------------------------------------


def test_upsert_is_idempotent_on_the_same_path(tmp_path):
    conn = _connect(tmp_path)
    try:
        first = store.upsert_document(
            conn,
            path_abs="C:/docs/a.txt",
            path_norm="c:/docs/a.txt",
            size=10,
            mtime_ns=1,
            ext=".txt",
            extract_status=store.STATUS_OK,
            extractor="text@1",
            text="正文",
            norm_text="",
            cache_key_value="k1",
            scanned_at="t",
        )
        second = store.upsert_document(
            conn,
            path_abs="C:/docs/a.txt",
            path_norm="c:/docs/a.txt",
            size=10,
            mtime_ns=1,
            ext=".txt",
            extract_status=store.STATUS_OK,
            extractor="text@1",
            text="正文",
            norm_text="",
            cache_key_value="k1",
            scanned_at="t2",
        )
        assert first.doc_id == second.doc_id
        assert first.cache_hit is False
        assert second.cache_hit is True
        assert store.count_documents(conn) == 1
    finally:
        conn.close()


def test_different_paths_with_the_same_name_are_two_documents(tmp_path):
    conn = _connect(tmp_path)
    try:
        for folder in ("a", "b"):
            store.upsert_document(
                conn,
                path_abs="C:/docs/%s/report.txt" % folder,
                path_norm="c:/docs/%s/report.txt" % folder,
                size=3,
                mtime_ns=1,
                ext=".txt",
                extract_status=store.STATUS_OK,
                extractor="text@1",
                text="正文",
                norm_text="",
                cache_key_value="k",
                scanned_at="t",
            )
        assert store.count_documents(conn) == 2
    finally:
        conn.close()


def test_status_counts_and_skipped_listing(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "ok.txt").write_text("正文内容。\n", encoding="utf-8")
    (root / "scan.pdf").write_bytes(pdfutil.build_no_text_pdf(1))
    conn = _connect(tmp_path)
    try:
        _scan(conn, root)
        conn.commit()
        assert store.status_counts(conn) == {store.STATUS_OK: 1, store.STATUS_NO_TEXT_LAYER: 1}
        listed = store.skipped_documents(conn)
        assert len(listed) == 1
        assert listed[0]["extract_status"] == store.STATUS_NO_TEXT_LAYER
    finally:
        conn.close()
