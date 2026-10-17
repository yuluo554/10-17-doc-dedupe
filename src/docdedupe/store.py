"""SQLite index and cache-key definition.

There is exactly one place where the cache key is defined (``cache_key``); the
field order matches ``data/README.md`` section 4.8 verbatim. Any module that
needs a cache key must call this function.

The database is a local runtime artifact: it is git-ignored and never leaves the
machine. An index written by an older schema version is rejected instead of being
silently migrated, so a stale database cannot produce wrong cluster results.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from .config import DB_SCHEMA_VERSION, cache_key_fields
from .config import Settings

SCHEMA_VERSION = DB_SCHEMA_VERSION

# Extraction outcomes. These are exhaustive: extract() may not invent new values
# without adding them here and to the reporting code.
STATUS_OK = "ok"
STATUS_NO_TEXT_LAYER = "skipped:no_text_layer"
STATUS_ENCRYPTED = "skipped:encrypted"
STATUS_UNSUPPORTED = "skipped:unsupported"
STATUS_TOO_LARGE = "skipped:too_large"
STATUS_READ_ERROR = "skipped:read_error"

SKIP_STATUSES = (
    STATUS_NO_TEXT_LAYER,
    STATUS_ENCRYPTED,
    STATUS_UNSUPPORTED,
    STATUS_TOO_LARGE,
    STATUS_READ_ERROR,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    path_abs       TEXT    NOT NULL,
    path_norm      TEXT    NOT NULL UNIQUE,
    size           INTEGER NOT NULL,
    mtime_ns       INTEGER NOT NULL,
    ext            TEXT    NOT NULL,
    extract_status TEXT    NOT NULL,
    extractor      TEXT    NOT NULL DEFAULT '',
    text           TEXT    NOT NULL DEFAULT '',
    norm_text      TEXT    NOT NULL DEFAULT '',
    text_sha256    TEXT    NOT NULL DEFAULT '',
    norm_sha256    TEXT    NOT NULL DEFAULT '',
    cache_key      TEXT    NOT NULL,
    scanned_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(extract_status);
CREATE INDEX IF NOT EXISTS idx_documents_norm_sha ON documents(norm_sha256);

CREATE TABLE IF NOT EXISTS fingerprints (
    doc_id        INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    simhash       INTEGER NOT NULL,
    minhash_json  TEXT    NOT NULL,
    shingle_count INTEGER NOT NULL,
    algo_version  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pairs (
    a_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    b_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    jaccard    REAL    NOT NULL,
    hamming    INTEGER NOT NULL,
    score_kind TEXT    NOT NULL,
    PRIMARY KEY (a_id, b_id),
    CHECK (a_id < b_id)
);

CREATE INDEX IF NOT EXISTS idx_pairs_jaccard ON pairs(jaccard);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT    PRIMARY KEY,
    tier       TEXT    NOT NULL,
    keep_id    INTEGER,
    reason     TEXT    NOT NULL,
    scores_json TEXT   NOT NULL,
    built_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id TEXT    NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, doc_id)
);

CREATE TABLE IF NOT EXISTS quarantine_batches (
    batch_id    TEXT PRIMARY KEY,
    cluster_id  TEXT NOT NULL,
    keep_path   TEXT NOT NULL,
    manifest    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def cache_key(
    path_abs: str,
    size: int,
    mtime_ns: int,
    settings: Settings,
) -> str:
    """Return the cache key for one document.

    Definition (single source, ``data/README.md`` 4.8)::

        sha256(absolute_path | size | mtime_ns | extractor_version
               | normalize_version | ngram_parameter)

    The path is normalized to forward slashes first so the same file produces the
    same key on Windows and POSIX. ``str.casefold`` is deliberately *not* used:
    case-insensitive comparison would merge distinct files on case-sensitive
    filesystems.
    """
    extractor_version, normalize_version, fingerprint_version, shingle_size, num_perm = (
        cache_key_fields(settings)
    )
    norm_path = str(path_abs).replace("\\", "/")
    payload = "|".join(
        [
            norm_path,
            str(int(size)),
            str(int(mtime_ns)),
            str(extractor_version),
            str(normalize_version),
            "%d-%d" % (shingle_size, num_perm),
            str(fingerprint_version),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def absolute_path(path: str) -> str:
    """Absolute path form stored in ``documents.path_abs``.

    Deliberately the same basis as :func:`normalize_path`: ``abspath`` only makes
    the path absolute and lexically normal, it does not resolve symlinks. Using
    ``Path.resolve`` here instead would make ``path_abs`` and ``path_norm``
    describe different files for a symlinked entry, and the cache key (which is
    built from the path) would then disagree with the stored identity.
    """
    return os.path.abspath(path)


def normalize_path(path: str) -> str:
    """Canonical path form used for identity, sorting and cluster ids.

    ``os.path.normcase`` folds case on Windows only, which is exactly the
    filesystem semantics we want; on POSIX it is a no-op.
    """
    absolute = os.path.abspath(path)
    return os.path.normcase(absolute).replace("\\", "/")


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the index database and verify its version."""
    parent = Path(db_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _check_schema_version(conn)
    return conn


def _check_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return
    found = int(row["value"])
    if found != SCHEMA_VERSION:
        raise SchemaVersionError(
            "index was written by schema version %d, this build expects %d; "
            "delete the database or point --db at a new path" % (found, SCHEMA_VERSION)
        )


class SchemaVersionError(RuntimeError):
    """Raised when an existing index cannot be used by this build."""


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


class UpsertOutcome(NamedTuple):
    """Result of writing one document row.

    ``cache_hit`` means the stored text was reused instead of re-extracting the
    file. The scan summary reports it, and the cache benchmark asserts on it:
    a "cache hit" that still read and parsed the document would make the speed-up
    number meaningless.
    """

    doc_id: int
    cache_hit: bool


def find_document(conn: sqlite3.Connection, path_norm: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE path_norm = ?", (path_norm,)
    ).fetchone()


def upsert_document(
    conn: sqlite3.Connection,
    *,
    path_abs: str,
    path_norm: str,
    size: int,
    mtime_ns: int,
    ext: str,
    extract_status: str,
    extractor: str,
    text: str,
    norm_text: str,
    cache_key_value: str,
    scanned_at: str,
) -> UpsertOutcome:
    """Insert or refresh one document row keyed by ``path_norm``.

    The identity of a row is the normalized absolute path, not the file name:
    two files with the same name in different directories are different
    documents, and the same file reached through a different spelling is the same
    document.

    Cached text is reused only when the caller decided the cache key matched; this
    function never inspects the file itself. It does recompute the content
    hashes, because those are cheap compared to extraction and must reflect the
    text actually stored.
    """
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    norm_sha = hashlib.sha256(norm_text.encode("utf-8")).hexdigest() if norm_text else ""

    existing = find_document(conn, path_norm)
    cache_hit = existing is not None and existing["cache_key"] == cache_key_value

    if existing is None:
        cursor = conn.execute(
            "INSERT INTO documents (path_abs, path_norm, size, mtime_ns, ext, "
            "extract_status, extractor, text, norm_text, text_sha256, norm_sha256, "
            "cache_key, scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path_abs,
                path_norm,
                size,
                mtime_ns,
                ext,
                extract_status,
                extractor,
                text,
                norm_text,
                text_sha,
                norm_sha,
                cache_key_value,
                scanned_at,
            ),
        )
        return UpsertOutcome(int(cursor.lastrowid), cache_hit)

    conn.execute(
        "UPDATE documents SET path_abs = ?, size = ?, mtime_ns = ?, ext = ?, "
        "extract_status = ?, extractor = ?, text = ?, norm_text = ?, text_sha256 = ?, "
        "norm_sha256 = ?, cache_key = ?, scanned_at = ? WHERE id = ?",
        (
            path_abs,
            size,
            mtime_ns,
            ext,
            extract_status,
            extractor,
            text,
            norm_text,
            text_sha,
            norm_sha,
            cache_key_value,
            scanned_at,
            int(existing["id"]),
        ),
    )
    return UpsertOutcome(int(existing["id"]), cache_hit)


def count_documents(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])


def status_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT extract_status, COUNT(*) AS n FROM documents GROUP BY extract_status"
    ).fetchall()
    return {str(row["extract_status"]): int(row["n"]) for row in rows}


def skipped_documents(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in SKIP_STATUSES)
    return conn.execute(
        "SELECT path_abs, extract_status FROM documents "
        "WHERE extract_status IN (%s) ORDER BY path_norm" % placeholders,
        SKIP_STATUSES,
    ).fetchall()


def touch_document(conn: sqlite3.Connection, doc_id: int, scanned_at: str) -> None:
    """Record that a cached row was seen again without changing its content."""
    conn.execute("UPDATE documents SET scanned_at = ? WHERE id = ?", (scanned_at, doc_id))
