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

This module does not touch the clock for content: ``scanned_at`` is passed in by
the caller, and tests inject a fixed value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import extract as extract_mod
from . import store
from .config import DEFAULT_SETTINGS, Settings

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
        store.upsert_document(
            conn,
            path_abs=store.absolute_path(str(path)),
            path_norm=path_norm,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            ext=path.suffix.lower(),
            extract_status=result.status,
            extractor=result.extractor,
            text=result.text,
            norm_text="",  # filled by module 2; module 1 stores the raw text only
            cache_key_value=key,
            scanned_at=scanned_at,
        )
        summary.files_indexed += 1
        summary.record(result.status)
        if result.status != store.STATUS_OK:
            summary.skipped.append((str(path), result.status))

    summary.elapsed_seconds = (clock() - started) if clock else 0.0
    return summary
