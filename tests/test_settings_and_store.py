"""Settings and schema tests.

Two properties matter here and both are falsifiable:

* the settings validator rejects broken values (otherwise the constants are
  decorative);
* the cache key changes when *any* component named in ``data/README.md`` 4.8
  changes (otherwise a stale index silently produces stale clusters).
"""

from __future__ import annotations

import pytest

from docdedupe import config
from docdedupe.config import DEFAULT_SETTINGS, Settings, cache_key_fields
from docdedupe.store import (
    STATUS_ENCRYPTED,
    STATUS_NO_TEXT_LAYER,
    STATUS_OK,
    SKIP_STATUSES,
    SchemaVersionError,
    cache_key,
    connect,
    get_meta,
    normalize_path,
    set_meta,
)


def test_defaults_match_the_documented_algorithm():
    assert DEFAULT_SETTINGS.simhash_bits == 64
    assert DEFAULT_SETTINGS.shingle_size == 3
    assert DEFAULT_SETTINGS.lsh_bands * DEFAULT_SETTINGS.lsh_rows == (
        DEFAULT_SETTINGS.minhash_num_perm
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"shingle_size": 4},
        {"simhash_bits": 128},
        {"lsh_bands": 16},  # breaks num_perm == bands * rows
    ],
)
def test_settings_reject_values_that_contradict_the_documented_algorithm(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_from_mapping_rejects_unknown_keys():
    with pytest.raises(ValueError) as exc:
        Settings.from_mapping({"shingle_sizes": 3})
    assert "shingle_sizes" in str(exc.value)


def test_with_overrides_revalidates():
    settings = DEFAULT_SETTINGS.with_overrides(minhash_num_perm=64, lsh_bands=16, lsh_rows=4)
    assert settings.minhash_num_perm == 64
    with pytest.raises(ValueError):
        DEFAULT_SETTINGS.with_overrides(lsh_bands=1)


def test_cache_key_fields_covers_versions_and_ngram_parameters():
    fields = cache_key_fields(DEFAULT_SETTINGS)
    assert len(fields) == 5
    assert all(isinstance(value, int) for value in fields)


def test_cache_key_documents_exactly_the_six_components():
    """Guard the definition itself, not just its behaviour.

    ``data/README.md`` 4.8 lists six components: path, size, mtime_ns, extractor
    version, normalization version, n-gram parameter. If one is dropped from the
    docstring that documents the key, this fails.
    """
    doc = cache_key.__doc__ or ""
    for component in (
        "absolute_path",
        "size",
        "mtime_ns",
        "extractor_version",
        "normalize_version",
        "ngram_parameter",
    ):
        assert component in doc, "cache key docstring lost component %r" % component


def test_cache_key_changes_for_every_component(monkeypatch):
    base = cache_key("C:/docs/a.txt", 100, 1_000, DEFAULT_SETTINGS)

    assert cache_key("C:/docs/b.txt", 100, 1_000, DEFAULT_SETTINGS) != base
    assert cache_key("C:/docs/a.txt", 101, 1_000, DEFAULT_SETTINGS) != base
    assert cache_key("C:/docs/a.txt", 100, 1_001, DEFAULT_SETTINGS) != base

    for name in ("EXTRACTOR_VERSION", "NORMALIZE_VERSION", "FINGERPRINT_VERSION"):
        original = getattr(config, name)
        monkeypatch.setattr(config, name, original + 1)
        assert cache_key("C:/docs/a.txt", 100, 1_000, DEFAULT_SETTINGS) != base, (
            "cache key ignored %s" % name
        )
        monkeypatch.undo()

    # n-gram parameter
    changed = DEFAULT_SETTINGS.with_overrides(minhash_num_perm=64, lsh_bands=16)
    assert cache_key("C:/docs/a.txt", 100, 1_000, changed) != base


def test_cache_key_is_stable_for_the_same_input():
    first = cache_key("C:/docs/a.txt", 100, 1_000, DEFAULT_SETTINGS)
    second = cache_key("C:/docs/a.txt", 100, 1_000, DEFAULT_SETTINGS)
    assert first == second


def test_cache_key_path_separators_do_not_change_the_key():
    """The same file reached via a different separator must not rescan."""
    win = cache_key("C:\\docs\\a.txt", 1, 2, DEFAULT_SETTINGS)
    posix = cache_key("C:/docs/a.txt", 1, 2, DEFAULT_SETTINGS)
    assert win == posix


def test_normalize_path_is_absolute_and_uses_forward_slashes():
    normalized = normalize_path(".")
    assert "\\" not in normalized
    assert normalized.endswith("/") or ":" in normalized


def test_connect_creates_every_expected_table(tmp_path):
    conn = connect(str(tmp_path / "index.db"))
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"documents", "fingerprints", "pairs", "clusters", "cluster_members"} <= tables


def test_connect_rejects_a_database_from_a_newer_schema(tmp_path):
    db_path = str(tmp_path / "index.db")
    conn = connect(db_path)
    set_meta(conn, "schema_version", "999")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError) as exc:
        connect(db_path)
    assert "999" in str(exc.value)


def test_meta_round_trip(tmp_path):
    conn = connect(str(tmp_path / "index.db"))
    try:
        assert get_meta(conn, "missing") is None
        set_meta(conn, "k", "v1")
        conn.commit()
        assert get_meta(conn, "k") == "v1"
        set_meta(conn, "k", "v2")
        conn.commit()
        assert get_meta(conn, "k") == "v2"
    finally:
        conn.close()


def test_status_vocabulary_is_exhaustive_and_skip_statuses_are_marked():
    assert STATUS_OK not in SKIP_STATUSES
    assert STATUS_NO_TEXT_LAYER in SKIP_STATUSES
    assert STATUS_ENCRYPTED in SKIP_STATUSES
    for status in SKIP_STATUSES:
        assert status.startswith("skipped:")


def test_pairs_table_rejects_self_pairs(tmp_path):
    """``CHECK (a_id < b_id)`` makes the symmetric matrix impossible to corrupt."""
    import sqlite3

    conn = connect(str(tmp_path / "index.db"))
    try:
        conn.execute(
            "INSERT INTO documents (path_abs, path_norm, size, mtime_ns, ext, "
            "extract_status, cache_key, scanned_at) VALUES ('a','a',1,1,'.txt','ok','k','t')"
        )
        doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO pairs (a_id, b_id, jaccard, hamming, score_kind) "
                "VALUES (?, ?, 0.9, 1, 'minhash')",
                (doc_id, doc_id),
            )
    finally:
        conn.close()
