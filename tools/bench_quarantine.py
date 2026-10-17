"""Benchmark B6: quarantine and rollback, against the filesystem.

Run it on its own copy of a corpus — it moves files, so it must never touch a
corpus it did not make::

    python tools/bench_quarantine.py --samples data/samples --work <dir>

B3 asks "were the duplicates found", B4 "were they classified correctly", and B5
"does the page describe a pair correctly". B6 asks the question module 5 exists
to answer: **is the action reversible, byte for byte, and does it refuse when it
cannot be?**

The truth is computed here, from the files themselves: the benchmark hashes every
member *before* anything moves, and compares that hash with what comes back. It
never asks the manifest whether the manifest was right — the manifest's own
``sha256`` is checked against the benchmark's hash as a separate gate.

Every scenario runs on its own copy of the corpus with its own index, so nothing
one scenario leaves behind can affect the next (copies are made, never deleted:
the package and this tool have no deletion path, and that is checked statically):

=====================  ========================================================
``round-trip``         quarantine every cluster's movable members, verify the
                       copies, then undo and require the corpus back exactly
``interrupted-resume`` kill the run between two moves, then re-run the same
                       batch id and require the batch to finish
``tampered-copy``      rewrite a quarantined file (same size, different bytes) and
                       require ``undo`` to refuse and restore *nothing*
``occupied-original``  put a different file back at an original path and require
                       ``undo`` to refuse without overwriting it
=====================  ========================================================

``--inject`` damages the execution layer on purpose; the scenarios must then
report failure. A gate that stays green under an injection is a fake gate, and
:data:`INJECTIONS` spells out what each one breaks and which check notices.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from docdedupe import cli, quarantine, store

import bench_common

SCENARIOS = (
    "round-trip",
    "interrupted-resume",
    "tampered-copy",
    "occupied-original",
)

# What each injection breaks, and which check is expected to notice:
#
# ``skip-sha-check``       the hash function reports whatever the manifest already
#                          says, so ``undo`` puts back a file it never verified.
#                          Noticed by hashing the restored file here.
# ``skip-pending``         a move reports success without moving anything. Noticed
#                          by "the original is gone and the quarantine holds a
#                          byte-identical copy".
# ``copy-instead-of-move`` the file is copied rather than moved, so two copies
#                          exist at once. Noticed by "exactly one copy".
INJECTIONS = ("none", "skip-sha-check", "skip-pending", "copy-instead-of-move")


class BenchmarkError(RuntimeError):
    """The benchmark could not set up a scenario (not a verdict about the tool)."""


# ---------------------------------------------------------------------------
# Truth, computed without the module under test
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def key(path) -> str:
    """The comparison key: the same normalized form the index uses.

    Windows paths differ by case and by separators between spellings, so an
    equality test on raw strings would be about spelling rather than about files.
    """
    return store.normalize_path(str(path))


def snapshot(root: Path) -> Dict[str, str]:
    """Every corpus file, keyed by normalized path, mapped to its own hash.

    The quarantine directory is excluded: it is the tool's record, not part of the
    corpus, and it is expected to survive an undo.
    """
    result: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if quarantine.QUARANTINE_DIR_NAME in relative.parts:
            continue
        result[key(path)] = sha256_of(path)
    return result


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """One checked scenario: what was required, and what went wrong."""

    scenario: str
    subject: str
    problems: Tuple[str, ...]
    truth: str

    @property
    def passed(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        if self.passed:
            return "PASS %-19s %-34s %s" % (self.scenario, self.subject, self.truth)
        shown = "; ".join(short(problem) for problem in self.problems[:3])
        if len(self.problems) > 3:
            shown += " …(+%d more)" % (len(self.problems) - 3)
        return "FAIL %-19s %-34s %s" % (self.scenario, self.subject, shown)


def short(text: str, limit: int = 120) -> str:
    """Trim a message for the one-line verdict.

    Every problem names the file it is about, and a corpus path is long; the point
    of the line is which check failed, not the full path (the JSON-free output is
    a summary, and ``--inject`` runs are read by a person).
    """
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class QuarantineReport:
    outcomes: Tuple[Outcome, ...]
    coverage: Dict[str, int]
    batches: int = 0
    items: int = 0
    collisions: int = 0

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.passed)

    @property
    def failures(self) -> Tuple[Outcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.passed)

    def describe(self) -> List[str]:
        lines = [outcome.describe() for outcome in self.outcomes]
        lines.append(
            "scenarios: %s"
            % ", ".join(
                "%s %d" % (name, self.coverage[name]) for name in sorted(self.coverage)
            )
        )
        lines.append(
            "batches %d, items moved %d, same-basename collisions %d"
            % (self.batches, self.items, self.collisions)
        )
        lines.append(
            "checks %d/%d = %.1f%% (round trip byte-identical, resume, and two refusals)"
            % (
                self.passed,
                self.total,
                100.0 * (self.passed / self.total if self.total else 0.0),
            )
        )
        return lines


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------


@contextmanager
def injection(mode: str):
    """Damage the execution layer for the duration of the block.

    The patches are made on the module, so the package needs no injection switch of
    its own: the seams a fault has to cross are the same ones the package uses
    (``file_sha256``, ``write_manifest``, ``move_file``) plus one internal decision
    function, ``_settle_pending``.
    """
    if mode not in INJECTIONS:
        raise ValueError("unknown injection %r" % mode)
    saved: List[Tuple[str, object]] = []

    def patch(name: str, value: object) -> None:
        saved.append((name, getattr(quarantine, name)))
        setattr(quarantine, name, value)

    try:
        if mode == "skip-sha-check":
            real_hash = quarantine.file_sha256
            real_write = quarantine.write_manifest
            claimed: Dict[str, str] = {}

            def write_and_remember(path: str, text: str) -> None:
                real_write(path, text)
                # Learn what the manifest claims, so the lying hash function can
                # repeat it back: that is exactly the bug "no verification at all".
                for item in quarantine.read_manifest(path).get("items", []):
                    claimed[key(item["quarantine_path"])] = str(item["sha256"])
                    claimed[key(item["original_path"])] = str(item["sha256"])

            def lying_hash(path: str) -> str:
                return claimed.get(key(path), real_hash(path))

            patch("file_sha256", lying_hash)
            patch("write_manifest", write_and_remember)

        elif mode == "skip-pending":

            def pretend_moved(item, problems):
                return item.sha256

            patch("_settle_pending", pretend_moved)

        elif mode == "copy-instead-of-move":

            def copy_file(src: str, dst: str, **kwargs) -> int:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(quarantine.windows_extended_path(parent), exist_ok=True)
                shutil.copy2(
                    quarantine.windows_extended_path(src),
                    quarantine.windows_extended_path(dst),
                )
                return 1

            patch("move_file", copy_file)

        yield mode
    finally:
        while saved:
            name, value = saved.pop()
            setattr(quarantine, name, value)


# ---------------------------------------------------------------------------
# Setting up a scenario
# ---------------------------------------------------------------------------


@dataclass
class ClusterInfo:
    cluster_id: str
    tier: str
    keep: str
    members: List[str]  # sorted absolute paths
    movable: List[str]  # members this benchmark will quarantine
    unlisted: List[str]  # members that must not move


@dataclass
class Corpus:
    """One scenario's private copy of the corpus, its index and its clusters."""

    root: Path
    db_path: Path
    clusters: List[ClusterInfo]
    truth: Dict[str, str] = field(default_factory=dict)


def _quiet():
    return contextlib.redirect_stdout(io.StringIO())


def prepare_corpus(samples: Path, work: Path, name: str) -> Corpus:
    """Copy the corpus into a directory of its own, scan it, and cluster it."""
    target = Path(tempfile.mkdtemp(prefix="%s-" % name, dir=str(work)))
    root = target / "corpus"
    shutil.copytree(str(samples), str(root))
    db_path = target / "index.db"

    with _quiet():
        scan_code = cli.main(["scan", str(root), "--db", str(db_path)])
        if scan_code != 0:
            raise BenchmarkError(
                "the copy of %s could not be scanned (exit %d)" % (samples, scan_code)
            )
        # ``report`` exits 1 when it found clusters — the normal outcome here.
        report_code = cli.main(
            [
                "report",
                "--db",
                str(db_path),
                "--format",
                "json",
                "--out",
                str(target / "report.json"),
            ]
        )
    if report_code not in (0, 1):
        raise BenchmarkError("'report' failed on the corpus copy (exit %d)" % report_code)

    conn = store.connect_readonly(str(db_path))
    try:
        rows = store.cluster_rows(conn)
    finally:
        conn.close()

    grouped: Dict[str, List] = {}
    for row in rows:
        grouped.setdefault(str(row["cluster_id"]), []).append(row)

    clusters: List[ClusterInfo] = []
    for cluster_id, cluster_rows in sorted(grouped.items()):
        members = sorted(str(row["path_abs"]) for row in cluster_rows)
        if len(members) < 2:
            continue
        keep_id = cluster_rows[0]["keep_id"]
        keep = next(
            (
                str(row["path_abs"])
                for row in cluster_rows
                if keep_id is not None and int(row["doc_id"]) == int(keep_id)
            ),
            members[0],  # a template cluster carries no recommendation
        )
        others = [path for path in members if path != keep]
        # Leave at least one member in place, so "the unlisted files did not move"
        # is a real check rather than a tautology.
        movable = others[:2] if len(others) > 2 else others
        clusters.append(
            ClusterInfo(
                cluster_id=cluster_id,
                tier=str(cluster_rows[0]["tier"]),
                keep=keep,
                members=members,
                movable=movable,
                unlisted=[path for path in members if path not in movable],
            )
        )
    if not clusters:
        raise BenchmarkError("the corpus copy has no cluster with two members")
    return Corpus(root=root, db_path=db_path, clusters=clusters, truth=snapshot(root))


def batch_id(index: int) -> str:
    """A fixed batch id: the benchmark compares runs, so it must not be random."""
    return "20261017T%06dZ-b6" % index


def manifest_items(db_path: Path, batch_id: str) -> List[dict]:
    conn = store.connect_readonly(str(db_path))
    try:
        manifest, _ = quarantine.load_batch(conn, batch_id)
    finally:
        conn.close()
    return list(manifest.get("items", []))


def quarantine_batch(corpus: Corpus, cluster: ClusterInfo, batch_id: str):
    return quarantine.execute_quarantine(
        str(corpus.db_path),
        cluster_id=cluster.cluster_id,
        keep=cluster.keep,
        move=cluster.movable,
        confirm=True,
        batch_id=batch_id,
        created_at="2026-10-17T10:15:00Z",
    )


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------


def check_moved(
    corpus: Corpus,
    cluster: ClusterInfo,
    result,
    truth: Dict[str, str],
    problems: List[str],
) -> None:
    """Every listed member is in the quarantine, and nothing else moved."""
    if result.pending:
        problems.append("items left pending after quarantine: %s" % ", ".join(result.pending))
    if result.problems:
        problems.append("quarantine reported: %s" % "; ".join(result.problems))
    items = manifest_items(corpus.db_path, result.batch_id)
    if len(items) != len(cluster.movable):
        problems.append(
            "the manifest lists %d item(s), %d were requested"
            % (len(items), len(cluster.movable))
        )
    for item in items:
        original = Path(item["original_path"])
        copy = Path(item["quarantine_path"])
        expected = truth.get(key(original))
        if expected is None:
            problems.append("%s is not part of the corpus this benchmark copied" % original)
            continue
        if item["status"] != quarantine.STATUS_MOVED:
            problems.append("%s is %r, expected 'moved'" % (original, item["status"]))
        if item["sha256"] != expected:
            problems.append("the manifest's sha256 for %s is not the file's own hash" % original)
        if original.exists():
            problems.append("%s is still at its original path" % original)
        if not copy.is_file():
            problems.append("%s is not in the quarantine" % copy)
        elif sha256_of(copy) != expected:
            problems.append("%s does not hold the original bytes" % copy)
    for path in cluster.unlisted:
        if not Path(path).is_file():
            problems.append("unlisted member %s moved" % path)
        elif sha256_of(Path(path)) != truth[key(path)]:
            problems.append("unlisted member %s changed" % path)


def check_restored(corpus: Corpus, batch_id: str, truth: Dict[str, str], problems: List[str]) -> None:
    """Every file of the batch is back, byte for byte, and its copy is gone."""
    for item in manifest_items(corpus.db_path, batch_id):
        original = Path(item["original_path"])
        copy = Path(item["quarantine_path"])
        expected = truth.get(key(original))
        if item["status"] != quarantine.STATUS_RESTORED:
            problems.append("%s is %r, expected 'restored'" % (original, item["status"]))
        if not original.is_file():
            problems.append("%s was not restored" % original)
        elif sha256_of(original) != expected:
            problems.append("%s came back with different bytes" % original)
        if copy.exists():
            problems.append("%s is still in the quarantine after the restore" % copy)


def check_corpus_intact(before: Dict[str, str], root: Path, problems: List[str], *, label: str) -> None:
    """The corpus is the same set of files with the same bytes as at the start."""
    after = snapshot(root)
    if set(after) != set(before):
        problems.append(
            "%s: corpus file set changed (missing %s, extra %s)"
            % (label, sorted(set(before) - set(after)), sorted(set(after) - set(before)))
        )
    for name, digest in before.items():
        if name in after and after[name] != digest:
            problems.append("%s: %s differs from the original bytes" % (label, name))


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_round_trip(corpus: Corpus) -> List[Outcome]:
    """Quarantine every cluster's movable members, then undo, checking both ways."""
    before = dict(corpus.truth)
    outcomes: List[Outcome] = []
    batches: List[Tuple[str, str]] = []

    for index, cluster in enumerate(corpus.clusters):
        current = batch_id(index)
        batches.append((cluster.cluster_id, current))
        problems: List[str] = []
        result = quarantine_batch(corpus, cluster, current)
        check_moved(corpus, cluster, result, before, problems)
        outcomes.append(
            Outcome(
                scenario="round-trip",
                subject="%s quarantine" % cluster.cluster_id,
                problems=tuple(problems),
                truth="%d of %d member(s) moved"
                % (len(cluster.movable), len(cluster.members)),
            )
        )

    # One undo per batch, after every quarantine: the corpus has to come back
    # exactly as it was, with every copy gone.
    for cluster_id, current in batches:
        problems = []
        result = quarantine.execute_undo(str(corpus.db_path), batch_id=current, confirm=True)
        if not result.complete:
            problems.append("undo reported: %s" % "; ".join(result.pending + result.problems))
        check_restored(corpus, current, before, problems)
        outcomes.append(
            Outcome(
                scenario="round-trip",
                subject="%s undo" % cluster_id,
                problems=tuple(problems),
                truth="%d item(s) restored" % len(result.items),
            )
        )

    intact: List[str] = []
    check_corpus_intact(before, corpus.root, intact, label="after the round trip")
    outcomes.append(
        Outcome(
            scenario="round-trip",
            subject="corpus identical",
            problems=tuple(intact),
            truth="%d file(s) byte-identical" % len(before),
        )
    )
    return outcomes


def scenario_interrupted_resume(corpus: Corpus) -> List[Outcome]:
    """Kill the run between two moves, then require a re-run to finish the batch."""
    cluster = max(corpus.clusters, key=lambda item: len(item.movable))
    before = dict(corpus.truth)
    current = batch_id(90)
    real_move = quarantine.move_file
    calls: List[Tuple[str, str]] = []

    def crashing_move(src: str, dst: str, **kwargs):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError("simulated crash between two moves")
        return real_move(src, dst, **kwargs)

    problems: List[str] = []
    quarantine.move_file = crashing_move
    try:
        first = quarantine_batch(corpus, cluster, current)
    finally:
        quarantine.move_file = real_move

    items = manifest_items(corpus.db_path, current)
    if len(items) != len(cluster.movable):
        problems.append(
            "the interrupted run wrote %d item(s) to the manifest, %d were requested"
            % (len(items), len(cluster.movable))
        )
    moved_when_killed = sum(1 for item in items if item["status"] == quarantine.STATUS_MOVED)
    if len(cluster.movable) > 1 and not first.pending:
        problems.append("the interruption left nothing pending; the scenario proved nothing")
    # "Write the manifest before moving" means every item is named even though the
    # process died, and a pending item is still where its original was.
    for item in items:
        if item["status"] == quarantine.STATUS_PENDING and not Path(item["original_path"]).is_file():
            problems.append(
                "%s is pending in the manifest but no longer at its original path"
                % item["original_path"]
            )

    second = quarantine_batch(corpus, cluster, current)
    check_moved(corpus, cluster, second, before, problems)

    undo = quarantine.execute_undo(str(corpus.db_path), batch_id=current, confirm=True)
    if not undo.complete:
        problems.append("undo reported: %s" % "; ".join(undo.pending + undo.problems))
    check_restored(corpus, current, before, problems)
    check_corpus_intact(before, corpus.root, problems, label="after resume and undo")

    return [
        Outcome(
            scenario="interrupted-resume",
            subject=cluster.cluster_id,
            problems=tuple(problems),
            truth="killed with %d/%d moved, then finished"
            % (moved_when_killed, len(cluster.movable)),
        )
    ]


def scenario_tampered_copy(corpus: Corpus) -> List[Outcome]:
    """A changed quarantine copy must stop the restore, and nothing may move back.

    The checks are written so that a *broken* tool produces a failed check rather
    than a crash: this scenario has to report the damage an injection causes, and
    a traceback would prove nothing about the gate. Each scenario owns a fresh copy
    of the corpus, so nothing has to be cleaned up afterwards.
    """
    cluster = max(corpus.clusters, key=lambda item: len(item.movable))
    current = batch_id(91)
    problems: List[str] = []
    result = quarantine_batch(corpus, cluster, current)
    check_moved(corpus, cluster, result, corpus.truth, problems)

    items = manifest_items(corpus.db_path, current)
    if not items:
        problems.append("the manifest lists nothing; the scenario could not be set up")
        return [
            Outcome(
                scenario="tampered-copy",
                subject=cluster.cluster_id,
                problems=tuple(problems),
                truth="setup failed",
            )
        ]

    victim = items[0]
    victim_path = Path(victim["quarantine_path"])
    if not victim_path.is_file():
        problems.append(
            "no quarantine copy for %s; the scenario could not be set up"
            % victim["original_path"]
        )
        return [
            Outcome(
                scenario="tampered-copy",
                subject=cluster.cluster_id,
                problems=tuple(problems),
                truth="setup failed",
            )
        ]

    saved = victim_path.read_bytes()
    tampered = b"#" * len(saved)  # same size: only the hash can catch this
    victim_path.write_bytes(tampered)

    undo = quarantine.execute_undo(str(corpus.db_path), batch_id=current, confirm=True)
    if undo.complete:
        problems.append("undo accepted a quarantine copy whose bytes had changed")
    if Path(victim["original_path"]).exists():
        problems.append("the tampered item was put back anyway")
    elif not victim_path.is_file():
        problems.append("the tampered copy vanished without being restored")
    elif victim_path.read_bytes() != tampered:
        problems.append("the tampered quarantine copy was modified by the run")

    for item in items[1:]:
        original = Path(item["original_path"])
        copy = Path(item["quarantine_path"])
        if copy.exists() and original.exists():
            problems.append("%s came back although the batch was aborted" % original)
        elif not copy.exists() and not original.exists():
            problems.append("%s is neither in the quarantine nor back in place" % original)

    return [
        Outcome(
            scenario="tampered-copy",
            subject=cluster.cluster_id,
            problems=tuple(problems),
            truth="undo refused, %d item(s) left in place" % len(items),
        )
    ]


def scenario_occupied_original(corpus: Corpus) -> List[Outcome]:
    """Something else at the original path must stop the restore, not be overwritten."""
    cluster = max(corpus.clusters, key=lambda item: len(item.movable))
    current = batch_id(92)
    problems: List[str] = []
    result = quarantine_batch(corpus, cluster, current)
    check_moved(corpus, cluster, result, corpus.truth, problems)

    items = manifest_items(corpus.db_path, current)
    target = next((item for item in items if not Path(item["original_path"]).exists()), None)
    if target is None:
        problems.append(
            "no original path was left free by the quarantine; the scenario could not be "
            "set up"
        )
        return [
            Outcome(
                scenario="occupied-original",
                subject=cluster.cluster_id,
                problems=tuple(problems),
                truth="setup failed",
            )
        ]

    intruder = "复写过的另一份内容，不该被覆盖。" * 8
    Path(target["original_path"]).write_text(intruder, encoding="utf-8")

    undo = quarantine.execute_undo(str(corpus.db_path), batch_id=current, confirm=True)
    if undo.complete:
        problems.append("undo accepted an occupied original path")
    if Path(target["original_path"]).read_text(encoding="utf-8") != intruder:
        problems.append("the file at the original path was overwritten")
    if not Path(target["quarantine_path"]).is_file():
        problems.append("the quarantined copy disappeared during a refused undo")

    return [
        Outcome(
            scenario="occupied-original",
            subject=cluster.cluster_id,
            problems=tuple(problems),
            truth="undo refused, the foreign file kept its bytes",
        )
    ]


# ---------------------------------------------------------------------------
# Driving everything
# ---------------------------------------------------------------------------


def run_benchmark(samples: Path, work: Path, *, inject: str = "none") -> QuarantineReport:
    """Run every scenario on its own corpus copy and collect the outcomes."""
    work.mkdir(parents=True, exist_ok=True)
    outcomes: List[Outcome] = []
    coverage: Dict[str, int] = {}
    batches = 0
    items = 0
    collisions = 0

    runners = {
        "round-trip": scenario_round_trip,
        "interrupted-resume": scenario_interrupted_resume,
        "tampered-copy": scenario_tampered_copy,
        "occupied-original": scenario_occupied_original,
    }

    with injection(inject):
        for name in SCENARIOS:
            corpus = prepare_corpus(samples, work, name)
            for cluster in corpus.clusters:
                names = [os.path.basename(path) for path in cluster.movable]
                collisions += len(names) - len(set(names))
            scenario_outcomes = runners[name](corpus)
            recorded_batches, recorded_items = batch_stats(corpus.db_path)
            batches += recorded_batches
            items += recorded_items
            for outcome in scenario_outcomes:
                outcomes.append(outcome)
                coverage[outcome.scenario] = coverage.get(outcome.scenario, 0) + 1

    return QuarantineReport(
        outcomes=tuple(outcomes),
        coverage=coverage,
        batches=batches,
        items=items,
        collisions=collisions,
    )


def batch_stats(db_path: Path) -> Tuple[int, int]:
    """How many batches this index registered, and how many items they hold.

    Counted from the manifests through the execution layer's own summary, so the
    numbers in the verdict describe what the tool recorded rather than what the
    benchmark intended to do.
    """
    conn = store.connect_readonly(str(db_path))
    try:
        summaries = quarantine.batch_summaries(conn)
    finally:
        conn.close()
    return len(summaries), sum(int(summary["items"]) for summary in summaries)


def evaluate(report: QuarantineReport) -> Tuple[bool, List[str]]:
    """Apply the B6 gate and return the verdict plus the reasons it failed."""
    failures: List[str] = []
    if report.total == 0:
        failures.append("no scenario ran; the gate would be vacuous")
    if report.items == 0:
        failures.append("no file was ever quarantined; nothing was measured")
    if report.failures:
        named = [
            "%s/%s" % (outcome.scenario, outcome.subject) for outcome in report.failures[:6]
        ]
        if len(report.failures) > 6:
            named.append("…(+%d more)" % (len(report.failures) - 6))
        failures.append(
            "%d scenario(s) disagreed with the requirement: %s"
            % (len(report.failures), "; ".join(named))
        )
    # Every scenario must have been exercised, otherwise a whole kind of rollback
    # could stop being tested while the gate still reported green.
    for name in SCENARIOS:
        if not report.coverage.get(name):
            failures.append("no scenario exercised %r" % name)
    return not failures, failures


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--samples",
        default=str(Path(__file__).resolve().parents[1] / "data" / "samples"),
        help="corpus to copy and run the round trip on (default: data/samples)",
    )
    parser.add_argument(
        "--work",
        default=None,
        help="directory for the corpus copies and their indexes (default: a temp dir)",
    )
    parser.add_argument(
        "--inject",
        default="none",
        choices=INJECTIONS,
        help="damage the execution layer to prove the benchmark can fail",
    )
    args = parser.parse_args(argv)

    samples = Path(args.samples).resolve()
    if not samples.is_dir():
        print("B6 FAIL: no corpus at %s" % samples)
        return 1
    hint = bench_common.corpus_truth_hint(samples)
    if hint:
        # Advisory only: B6 does not need a manifest and reports its own failures,
        # but when a manifest is present and nothing it names is on disk, the
        # scenarios below cannot mean what they look like they mean.
        print("HINT: %s" % hint)

    with contextlib.ExitStack() as stack:
        if args.work:
            work = Path(args.work).resolve()
        else:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="b6-"))).resolve()
        try:
            report = run_benchmark(samples, work, inject=args.inject)
        except BenchmarkError as exc:
            print("B6 FAIL: %s" % exc)
            return 1

    for line in report.describe():
        print(line)

    ok, failures = evaluate(report)
    if args.inject != "none":
        # The damaged run is expected to fail: that is what shows the checks are
        # load-bearing. The exit code still describes the checks, like B5.
        print(
            "B6 injection %r: %d of %d check(s) reported failure"
            % (args.inject, len(report.failures), report.total)
        )
    if ok:
        print("B6 OK: the round trip is byte-identical and both refusals hold")
        return 0
    for failure in failures:
        print("B6 FAIL: %s" % failure)
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
