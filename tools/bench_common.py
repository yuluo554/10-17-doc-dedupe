"""Shared helpers for the four benchmark tools (B3-B6).

The benchmarks take their truth from ``--samples`` and their documents from an
index written by ``doc-dedupe scan``. When those two describe *different*
corpora every gate fails at once and the output reads like a real regression:
B3 reports ``recall 0.0% (0/20)``, B4 reports ``0/27``, B5 reports that no pair
covered ``container-only``. Measured during M5: that was a path mix-up, not a
defect, so the helpers below turn the whole class of user error into one
readable line.

Nothing here changes a gate. A hint is printed next to the verdict; the exit
code still describes the checks themselves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from docdedupe import store

MISMATCH_HINT = (
    "no truth-manifest file is present in the index (%d manifest file(s) under %s, "
    "%d document(s) in the index); check that --samples is the corpus this index "
    "was scanned from"
)


def manifest_path(samples_root: Path) -> Path:
    return Path(samples_root) / "truth" / "manifest.json"


def load_manifest_files(samples_root: Path) -> Optional[List[str]]:
    """Normalized absolute paths of every file the truth manifest names.

    Returns ``None`` when there is no manifest at all — that is a different
    mistake (``--samples`` does not point at the synthetic corpus) and gets its
    own hint.
    """
    path = manifest_path(samples_root)
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    root = Path(samples_root)
    files = []
    for group in manifest.get("groups", []):
        for name in group.get("files", []):
            files.append(store.normalize_path(str((root / name).resolve())))
    return files


def index_document_paths(conn) -> List[str]:
    return [str(row["path_norm"]) for row in conn.execute("SELECT path_norm FROM documents")]


def corpus_index_mismatch_hint(conn, samples_root: Path) -> Optional[str]:
    """One line when the index and the truth manifest share no file at all.

    Returns ``None`` when the two sides overlap (or when there is nothing to
    compare), so a healthy run prints nothing extra.
    """
    root = Path(samples_root).resolve()
    named = load_manifest_files(root)
    if named is None:
        return (
            "no truth manifest at %s; --samples does not look like the synthetic "
            "corpus (it needs a truth/manifest.json)" % manifest_path(root)
        )
    if not named:
        return None
    indexed = index_document_paths(conn)
    if not indexed:
        return (
            "the index has no documents; run 'doc-dedupe scan %s --db <index.db>' first"
            % root
        )
    if not set(named) & set(indexed):
        return MISMATCH_HINT % (len(named), root, len(indexed))
    return None


def corpus_truth_hint(samples_root: Path) -> Optional[str]:
    """One line when a truth manifest is present but describes nothing on disk.

    B6 copies ``--samples`` and scans the copy, so it cannot mix up two indexes;
    what it can do is run against the wrong directory. Unlike B3-B5 it does not
    need a manifest at all — it derives its truth from the files themselves, and
    the test suite runs it on corpora that have none — so a missing manifest is
    not a hint, while a manifest naming files that are all absent is.
    """
    root = Path(samples_root).resolve()
    named = load_manifest_files(root)
    if not named:
        return None
    present = sum(1 for path in named if Path(path).is_file())
    if present == 0:
        return (
            "none of the %d file(s) named by the truth manifest exist under %s; "
            "check that --samples is the corpus the manifest describes" % (len(named), root)
        )
    return None
