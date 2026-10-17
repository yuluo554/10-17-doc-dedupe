"""Directory walk and index update for the ``scan`` command.

The scan is where every downstream promise starts, so the invariants live here
rather than in the CLI:

* **Traversal order is a sorted ``path_norm``**, never the order the filesystem
  returned. Otherwise two runs over the same tree could number documents
  differently and every report would show spurious churn.
* **A cache hit must skip extraction.** The hit is decided before the file is
  opened, and the stored text is reused verbatim. Re-extracting on a hit would
  make the documented second-scan speed-up (``data/README.md`` 4.9)
  unmeasurable.
* **Nothing is silently dropped.** Every skipped file is counted per status and
  listed in the summary, so "not compared" is always visible.
* **Normalized text and fingerprints are written in the same pass.** A cache hit
  reuses the stored text, so the fingerprint row is reused too; a row missing at
  the current algorithm version is rebuilt from the stored normalized text
  without reopening the file.
* **Recall is skipped only with evidence.** When no fingerprint changed and the
  stored ``pairs`` table still hashes to the value recorded when it was built,
  re-running the same all-pairs pass would recompute an identical answer. The
  check reads the index, not a flag, so a deleted or hand-edited ``pairs`` table
  is still noticed and rebuilt.

This module does not touch the clock for content: ``scanned_at`` is passed in by
the caller, and tests inject a fixed value.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import extract as extract_mod
from . import fingerprint as fingerprint_mod
from . import normalize as normalize_mod
from . import store
from .config import DEFAULT_SETTINGS, FINGERPRINT_VERSION, Settings

META_RECALL_INPUT = "recall_input_digest"
META_RECALL_RESULT = "recall_result_digest"

# Directory names skipped by default when walking. ``.dedupe-quarantine`` is
# here because a quarantined file is already known to be a duplicate: rescanning
# it would re-report the file the user just set aside.
DEFAULT_IGNORE = DEFAULT_SETTINGS.ignore_dirs


@dataclass
class ScanSummary:
    """Everything the CLI prints, and everything the tests assert on."""

    files_seen: int = 0
    files_indexed: int = 0
    cache_hits: int = 0
    extracted: int = 0
    by_status: Dict[str, int] = field(default_factory=dict)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    roots: List[str] = field(default_factory=list)

    # --- module 2 ---
    fingerprinted: int = 0
    fingerprinted_from_cache: int = 0
    # Total fingerprint rows in the index after this pass. Without it, a second
    # scan that recomputed nothing would print "fingerprinted 0" and read like
    # the fingerprints had been lost.
    fingerprints_stored: int = 0
    recall_documents: int = 0
    recall_skipped_short: int = 0
    band_candidates: int = 0
    rejected_by_hamming: int = 0
    rejected_by_score: int = 0
    pairs: int = 0
    pairs_above_threshold: int = 0
    # True when the recall pass was reused because no fingerprint changed and
    # the stored pair table still matched its recorded digest.
    recall_reused: bool = False

    @property
    def usable(self) -> int:
        return self.by_status.get(store.STATUS_OK, 0)

    @property
    def skipped_total(self) -> int:
        return sum(count for status, count in self.by_status.items() if status != store.STATUS_OK)

    def record(self, status: str) -> None:
        self.by_status[status] = self.by_status.get(status, 0) + 1


def iter_files(
    roots: Sequence[Path],
    ignore: Iterable[str] = DEFAULT_IGNORE,
    suffixes: Optional[Iterable[str]] = None,
) -> List[Tuple[Path, str]]:
    """Return ``(absolute path, path_norm)`` for every candidate file, sorted.

    Sorting happens on ``path_norm`` so the order is identical on Windows and
    POSIX and independent of how ``os.scandir`` happens to enumerate entries.

    Only document-like suffixes are returned. An index that counted every
    ``.json`` and ``.zip`` as "skipped: unsupported" would bury the files the
    user actually needs to know about.
    """
    ignore_set = {name.casefold() for name in ignore}
    suffix_set = {
        suffix.lower()
        for suffix in (DEFAULT_SETTINGS.scan_candidate_suffixes if suffixes is None else suffixes)
    }
    found: Dict[str, Path] = {}

    for root in roots:
        root = Path(root)
        if root.is_file():
            if root.suffix.lower() in suffix_set:
                key = store.normalize_path(str(root))
                found.setdefault(key, root)
            continue
        for dirpath, dirnames, filenames in os.walk(str(root)):
            dirnames[:] = sorted(
                name for name in dirnames if name.casefold() not in ignore_set
            )
            for name in filenames:
                candidate = Path(dirpath) / name
                if candidate.name.casefold() in ignore_set:
                    continue
                if candidate.suffix.lower() not in suffix_set:
                    continue
                key = store.normalize_path(str(candidate))
                found[key] = candidate

    return [(found[key], key) for key in sorted(found)]


def scan(
    conn,
    roots: Sequence[Path],
    *,
    settings: Optional[Settings] = None,
    ignore: Optional[Iterable[str]] = None,
    scanned_at: str = "",
    clock=None,
) -> ScanSummary:
    """Walk ``roots`` and bring the index up to date.

    ``clock`` is a callable returning a monotonic float, passed in so a test can
    measure the cache effect without depending on wall-clock resolution.
    """
    cfg = settings or DEFAULT_SETTINGS
    ignore_names = list(DEFAULT_IGNORE if ignore is None else ignore)
    summary = ScanSummary(roots=[str(root) for root in roots])

    started = clock() if clock else 0.0
    entries = iter_files(roots, ignore_names, cfg.scan_candidate_suffixes)
    summary.files_seen = len(entries)

    for path, path_norm in entries:
        try:
            stat = path.stat()
        except OSError:
            summary.record(store.STATUS_READ_ERROR)
            summary.skipped.append((str(path), store.STATUS_READ_ERROR))
            continue

        key = store.cache_key(store.absolute_path(str(path)), stat.st_size, stat.st_mtime_ns, cfg)
        existing = store.find_document(conn, path_norm)

        if existing is not None and existing["cache_key"] == key:
            # Cache hit: reuse stored text, do not open the file. This branch is
            # the entire performance story of the second scan.
            store.touch_document(conn, int(existing["id"]), scanned_at)
            summary.cache_hits += 1
            summary.files_indexed += 1
            summary.record(str(existing["extract_status"]))
            continue

        try:
            result = extract_mod.extract(path, settings=cfg)
        except extract_mod.DependencyMissing:
            raise
        except Exception:  # noqa: BLE001 - one bad file must not abort the scan
            result = extract_mod.ExtractResult(store.STATUS_READ_ERROR, "", "")

        summary.extracted += 1
        norm_text = (
            normalize_mod.normalize(result.text, cfg)
            if result.status == store.STATUS_OK and result.text
            else ""
        )
        outcome = store.upsert_document(
            conn,
            path_abs=store.absolute_path(str(path)),
            path_norm=path_norm,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            ext=path.suffix.lower(),
            extract_status=result.status,
            extractor=result.extractor,
            text=result.text,
            norm_text=norm_text,
            cache_key_value=key,
            scanned_at=scanned_at,
        )
        if norm_text:
            _write_fingerprint(conn, outcome.doc_id, norm_text, cfg)
            summary.fingerprinted += 1
        summary.files_indexed += 1
        summary.record(result.status)
        if result.status != store.STATUS_OK:
            summary.skipped.append((str(path), result.status))

    # A cache hit copies stored text but says nothing about whether a fingerprint
    # exists at the *current* algorithm version (an old index, or a fingerprint
    # bribe that failed halfway). This pass closes that gap using stored text
    # only, so it never reopens a file.
    stale = store.stale_fingerprint_documents(conn, FINGERPRINT_VERSION)
    if stale:
        ids = ", ".join("?" for _ in stale)
        rows = conn.execute(
            "SELECT id, norm_text FROM documents WHERE id IN (%s) ORDER BY path_norm" % ids,
            tuple(stale),
        ).fetchall()
        for row in rows:
            norm_text = str(row["norm_text"])
            if not norm_text:
                # The row predates normalization. Drop it from the fingerprint
                # set rather than inventing a fingerprint for empty text; the
                # next content change rebuilds it.
                store.delete_fingerprints_for_documents(conn, [int(row["id"])])
                continue
            _write_fingerprint(conn, int(row["id"]), norm_text, cfg)
            summary.fingerprinted += 1
            summary.fingerprinted_from_cache += 1

    _run_recall(conn, summary, cfg)

    summary.elapsed_seconds = (clock() - started) if clock else 0.0
    return summary


def _write_fingerprint(conn, doc_id: int, norm_text: str, cfg: Settings) -> None:
    """Compute and store one document's fingerprint row."""
    fingerprint = fingerprint_mod.fingerprint_text(norm_text, cfg)
    store.upsert_fingerprint(
        conn,
        doc_id=doc_id,
        simhash=fingerprint.simhash,
        minhash_json=fingerprint_mod.signature_to_json(fingerprint.signature),
        shingle_count=fingerprint.shingle_count,
        algo_version=FINGERPRINT_VERSION,
        shingle_hashes=fingerprint_mod.hashes_to_blob(fingerprint.hashes),
    )


def _recall_input_digest(conn, cfg: Settings) -> str:
    """Cheap, exact fingerprint of every input the recall pass reads.

    Covers the document set that participates (``ok`` rows and their normalized
    text length), their fingerprints, and the algorithm parameters that shape a
    pair. If this digest is unchanged, the all-pairs answer cannot change.
    """
    digest = hashlib.sha256()
    digest.update(
        ("%d|%d|%d|%s|%s|%s"
         % (
             FINGERPRINT_VERSION,
             cfg.shingle_size,
             cfg.minhash_num_perm,
             cfg.lsh_bands,
             cfg.lsh_rows,
             repr(cfg.pair_store_threshold),
         )).encode("utf-8")
    )
    for row in conn.execute(
        "SELECT d.path_norm, d.norm_sha256, f.simhash, f.minhash_json, f.shingle_count "
        "FROM documents AS d LEFT JOIN fingerprints AS f ON f.doc_id = d.id "
        "WHERE d.extract_status = ? ORDER BY d.path_norm",
        (store.STATUS_OK,),
    ):
        digest.update(
            ("\x1f%s\x1e%s\x1e%s\x1e%s\x1e%s"
             % (
                 row["path_norm"],
                 row["norm_sha256"],
                 row["simhash"] if row["simhash"] is not None else "",
                 row["minhash_json"] if row["minhash_json"] is not None else "",
                 row["shingle_count"] if row["shingle_count"] is not None else "",
             )).encode("utf-8")
        )
    return digest.hexdigest()


def _recall_result_digest(conn) -> str:
    """Digest of the stored ``pairs`` table, in a canonical order."""
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT a_id, b_id, jaccard, jaccard_estimate, hamming, score_kind "
        "FROM pairs ORDER BY a_id, b_id"
    ):
        digest.update(
            ("%d,%d,%.17g,%.17g,%d,%s\n"
             % (
                 row["a_id"],
                 row["b_id"],
                 row["jaccard"],
                 row["jaccard_estimate"],
                 row["hamming"],
                 row["score_kind"],
             )).encode("utf-8")
        )
    return digest.hexdigest()


def _run_recall(conn, summary: ScanSummary, cfg: Settings) -> None:
    """Score band candidates over the whole index and replace ``pairs``.

    Recall runs over the complete index rather than only the files touched in
    this pass: a newly added document has to be compared with every document
    already indexed, not just with its neighbours in the walk.

    The skip is conditional and verifiable. It requires two independent facts:

    * the digest of every recall input (document set, normalized text, all
      fingerprints, parameters) matches the digest recorded when the table was
      built, and
    * the stored ``pairs`` table still digests to the value recorded for it.

    Either mismatch means the table is stale or was altered, and the pass runs.
    """
    input_digest = _recall_input_digest(conn, cfg)
    if (
        store.get_meta(conn, META_RECALL_INPUT) == input_digest
        and store.get_meta(conn, META_RECALL_RESULT) == _recall_result_digest(conn)
    ):
        summary.recall_reused = True
        summary.recall_documents = int(
            conn.execute(
                "SELECT COUNT(*) FROM documents WHERE extract_status = ?",
                (store.STATUS_OK,),
            ).fetchone()[0]
        )
        summary.fingerprints_stored = store.count_fingerprints(conn)
        summary.pairs = store.count_pairs(conn)
        summary.pairs_above_threshold = store.count_pairs(
            conn, min_jaccard=cfg.near_duplicate_threshold
        )
        return

    rows = store.fingerprint_rows(conn)
    entries = [
        (
            int(row["doc_id"]),
            len(str(row["norm_text"])),
            store.simhash_from_signed(int(row["simhash"])),
            fingerprint_mod.signature_from_json(str(row["minhash_json"])),
            fingerprint_mod.hashes_from_blob(bytes(row["shingle_hashes"])),
        )
        for row in rows
    ]
    candidates, stats = fingerprint_mod.build_candidates(entries, settings=cfg)
    store.replace_pairs(
        conn,
        [
            (
                candidate.a_id,
                candidate.b_id,
                candidate.jaccard,
                candidate.jaccard_estimate,
                candidate.hamming,
                candidate.score_kind,
            )
            for candidate in candidates
        ],
    )
    summary.recall_documents = stats.documents
    summary.fingerprints_stored = store.count_fingerprints(conn)
    summary.recall_skipped_short = stats.skipped_short
    summary.band_candidates = stats.band_candidates
    summary.rejected_by_hamming = stats.rejected_by_hamming
    summary.rejected_by_score = stats.rejected_by_score
    summary.pairs = stats.pairs
    summary.pairs_above_threshold = store.count_pairs(
        conn, min_jaccard=cfg.near_duplicate_threshold
    )

    # Record the digests only after the table is written, so an interrupted pass
    # leaves the previous (still valid) digests in place and is simply redone.
    store.set_meta(conn, META_RECALL_INPUT, input_digest)
    store.set_meta(conn, META_RECALL_RESULT, _recall_result_digest(conn))
