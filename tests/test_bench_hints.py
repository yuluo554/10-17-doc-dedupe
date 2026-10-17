"""The benchmarks must tell a corpus/index mix-up apart from a real regression.

Measured during M5: running B3/B4/B5 with ``--samples`` pointing at the repository
corpus while the index had been scanned from a copy produced ``recall 0.0%
(0/20)``, ``0/27`` and "no pair covered container-only" — output that reads like a
broken pipeline but was a path mistake. These tests pin the hint that separates
the two, and pin that a healthy run stays silent.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from docdedupe import scanner, store

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
MANIFEST = SAMPLES / "truth" / "manifest.json"

sys.path.insert(0, str(REPO_ROOT / "tools"))

import bench_common  # noqa: E402
import bench_diff  # noqa: E402
import bench_quarantine  # noqa: E402
import bench_recall  # noqa: E402
import bench_tiers  # noqa: E402

pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(), reason="sample corpus is not generated"
)


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    """An index scanned from the repository corpus, as the README tells users to do."""
    db_path = tmp_path_factory.mktemp("hints") / "index.db"
    conn = store.connect(str(db_path))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
        yield conn, str(db_path)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def elsewhere_corpus(tmp_path_factory):
    """A second copy of the truth manifest, naming files the index never saw."""
    root = tmp_path_factory.mktemp("elsewhere") / "corpus"
    (root / "truth").mkdir(parents=True)
    shutil.copyfile(MANIFEST, root / "truth" / "manifest.json")
    return root


# ---------------------------------------------------------------------------
# Healthy runs stay silent
# ---------------------------------------------------------------------------


def test_a_matching_index_prints_no_hint(index, capsys):
    conn, db_path = index
    assert bench_common.corpus_index_mismatch_hint(conn, SAMPLES) is None
    code = bench_recall.main(["--db", db_path, "--samples", str(SAMPLES)])
    assert code == 0
    assert "HINT" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# A mix-up is named instead of being reported as a regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,extra",
    [
        (bench_recall, ["bench_recall.py"]),
        (bench_tiers, ["bench_tiers.py"]),
        (bench_diff, ["bench_diff.py"]),
    ],
    ids=["B3", "B4", "B5"],
)
def test_a_mismatched_corpus_prints_the_hint(index, elsewhere_corpus, capsys, module, extra):
    conn, db_path = index
    code = module.main(["--db", db_path, "--samples", str(elsewhere_corpus)])
    out = capsys.readouterr().out
    assert "HINT: no truth-manifest file is present in the index" in out, out
    assert "--samples is the corpus this index was scanned from" in out
    # The hint explains the red; it does not turn it green. Mixing corpora is
    # still a failing run — the user has to fix the path, not the gate.
    assert code == 1


def test_the_hint_only_fires_when_nothing_overlaps(index, elsewhere_corpus):
    """One shared file is enough: a partial overlap is a real finding, not a mix-up."""
    conn, _ = index
    assert bench_common.corpus_index_mismatch_hint(conn, elsewhere_corpus) is not None
    indexed = bench_common.index_document_paths(conn)
    assert indexed, "fixture index is empty; the guard would be vacuous"


def test_an_index_without_documents_gets_its_own_hint(tmp_path):
    """Scanning never ran: that is a different sentence from a corpus mix-up."""
    empty = store.connect(str(tmp_path / "empty.db"))
    try:
        hint = bench_common.corpus_index_mismatch_hint(empty, SAMPLES)
    finally:
        empty.close()
    assert hint is not None
    assert "run 'doc-dedupe scan" in hint


def test_a_directory_without_a_manifest_is_named_as_such(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    hint = bench_common.corpus_index_mismatch_hint(None, bare)
    assert hint is not None and "no truth manifest at" in hint
    # B6 has no index to mix up and does not need a manifest at all, so for B6 the
    # same directory is not a hint.
    assert bench_common.corpus_truth_hint(bare) is None


# ---------------------------------------------------------------------------
# B6 takes no --db, so it gets the corpus-side half of the same check
# ---------------------------------------------------------------------------


def test_b6_is_silent_on_the_frozen_corpus_and_speaks_on_a_stranger():
    assert bench_common.corpus_truth_hint(SAMPLES) is None
    named = bench_common.load_manifest_files(SAMPLES)
    assert named and len(named) > 50, "manifest is empty; the guard would be vacuous"


def test_b6_refuses_a_corpus_that_holds_none_of_the_manifest_files(tmp_path, elsewhere_corpus, capsys):
    code = bench_quarantine.main(["--samples", str(elsewhere_corpus)])
    out = capsys.readouterr().out
    assert "HINT:" in out and "none of the" in out, out
    assert code == 1


def test_the_manifest_used_by_the_hint_is_the_frozen_one():
    """The hint reads the same manifest the gates do, not a second copy.

    The manifest names the 94 text-bearing files (the 102-file corpus minus the
    eight skipped ones), and the hint must resolve exactly those, to the same
    normalized form the index uses.
    """
    groups = json.loads(MANIFEST.read_text(encoding="utf-8"))["groups"]
    assert len(groups) == 37
    named = [name for group in groups for name in group["files"]]
    resolved = bench_common.load_manifest_files(SAMPLES)
    assert len(resolved) == len(named) == 94
    assert len(set(resolved)) == 94, "the manifest names the same file twice"
    from docdedupe import store

    assert resolved == [store.normalize_path(str((SAMPLES / name).resolve())) for name in named]
