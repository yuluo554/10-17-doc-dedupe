"""Benchmark B3: recall of the implanted truth, per group.

Run against an index produced by ``doc-dedupe scan``::

    python tools/bench_recall.py --db <index.db> --samples data/samples

It prints one line per truth group, then the two gates from ``data/README.md``
4.7: the near-group recall rate and the count of negative groups that picked up a
foreign member. Exit code is 0 when both gates hold and 1 otherwise, so a CI job
can depend on it without parsing prose.

``--inject`` deliberately breaks the pipeline (drop edges above a score, or split
the truth groups) and the benchmark is expected to *fail*. A benchmark that stays
green under an injection is a fake gate; ``--inject`` is how that is checked on
demand, and ``tests/test_recall.py`` asserts it too.

The truth manifest records no scores (``data/README.md`` 4.8), so this tool only
ever reads ``group_id`` / ``tier`` / ``files`` from it.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from docdedupe import store
from docdedupe.config import DEFAULT_SETTINGS
from docdedupe.recall import RecallReport, evaluate_recall

# The recall floor lives in data/README.md 4.7. It is repeated here as the
# benchmark's own gate because a benchmark must be able to fail; the test suite
# asserts the same number, so a change to one without the other is visible.
RECALL_FLOOR = 0.90


def _relative_key(root: Path):
    """Return a key function joining manifest names and index paths.

    The index stores ``store.normalize_path`` forms (absolute, case-folded on
    Windows, forward slashes). The manifest stores paths relative to the samples
    directory. Both sides go through the same absolute form, so the join does not
    depend on which spelling the caller happened to pass.
    """
    root_norm = store.normalize_path(str(root.resolve()))

    def key(value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            path = root / value
        return store.normalize_path(str(path))

    return key


def _relative_name(root: Path, path_norm: str) -> str:
    root_norm = store.normalize_path(str(root.resolve()))
    return posixpath.relpath(path_norm, root_norm)


def load_edges(conn, samples_root: Path) -> List[Tuple[str, str, float]]:
    """Read ``pairs`` joined to document paths, in manifest-relative form.

    The join goes through ``documents.path_norm`` so an edge names files the same
    way the manifest does, whichever platform wrote the index.
    """
    ids = {
        int(row["id"]): str(row["path_norm"])
        for row in conn.execute("SELECT id, path_norm FROM documents")
    }
    nodes = {doc_id: _relative_name(samples_root, path) for doc_id, path in ids.items()}
    return [
        (nodes[int(row["a_id"])], nodes[int(row["b_id"])], float(row["jaccard"]))
        for row in conn.execute("SELECT a_id, b_id, jaccard FROM pairs")
        if int(row["a_id"]) in nodes and int(row["b_id"]) in nodes
    ]


def load_groups(manifest_path: Path) -> List[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(manifest["groups"])


def _inject_edges(edges, mode: str):
    """Perturb the edge set so the benchmark must notice.

    ``drop-above`` removes every edge above 0.99 (a plausible off-by-one in a
    threshold comparison); ``drop-random`` removes one edge in five, which breaks
    a transitive chain without deleting whole groups.
    """
    if mode == "none":
        return list(edges)
    if mode == "drop-above":
        return [edge for edge in edges if edge[2] < 0.99]
    if mode == "drop-random":
        return [edge for index, edge in enumerate(edges) if index % 5 != 0]
    raise ValueError("unknown injection %r" % mode)


def _inject_groups(groups, mode: str):
    """Perturb the truth so an engine that agrees with itself must disagree.

    ``shift-groups`` moves one member of each near group into the next group,
    which no correct engine can reproduce.
    """
    if mode == "none":
        return list(groups)
    if mode != "shift-groups":
        raise ValueError("unknown group injection %r" % mode)
    shifted = [dict(group, files=list(group["files"])) for group in groups]
    near = [group for group in shifted if group["tier"] == "near"]
    for index, group in enumerate(near):
        donor = near[(index + 1) % len(near)]
        if len(group["files"]) > 1:
            group["files"].append(donor["files"][0])
    return shifted


def run_benchmark(
    conn,
    samples_root: Path,
    *,
    threshold: float = DEFAULT_SETTINGS.near_duplicate_threshold,
    inject_edges: str = "none",
    inject_groups: str = "none",
) -> RecallReport:
    groups = _inject_groups(load_groups(samples_root / "truth" / "manifest.json"), inject_groups)
    edges = _inject_edges(load_edges(conn, samples_root), inject_edges)
    # Manifest names are relative; the edge names were made relative too, so the
    # key resolves either form against the same root.
    return evaluate_recall(
        groups,
        edges,
        threshold,
        path_key=_relative_key(samples_root),
    )


def informational_report(report: RecallReport) -> List[str]:
    """Lines for groups outside the B3 gates.

    Template groups are *not* recalled by this stage's similarity gate: their
    members are the same skeleton with different field values, which lands
    around 0.57-0.80 and is exactly why the template tier exists as its own
    decision in module 3. Printing their outcome keeps the corpus accounted for
    without turning a known, deliberate behaviour into a red gate.
    """
    return [
        "INFO %-6s %-9s %s"
        % (
            group.group_id,
            group.tier,
            "joined at the near-duplicate threshold"
            if group.components == 1
            else "not joined (%d components); template tier is decided by module 3"
            % group.components,
        )
        for group in report.groups
        if group.tier not in ("near", "negative")
    ]


def evaluate(
    report: RecallReport, *, floor: float = RECALL_FLOOR
) -> Tuple[bool, List[str]]:
    """Apply both B3 gates and return the verdict plus the reasons it failed."""
    failures = []
    if report.near_total == 0:
        failures.append("no near groups were evaluated; the gate would be vacuous")
    if report.rate < floor:
        failures.append(
            "recall %.1f%% is below the %.0f%% floor (%d/%d near groups)"
            % (100.0 * report.rate, 100.0 * floor, report.near_recalled, report.near_total)
        )
    if report.negative_violations:
        failures.append(
            "%d negative group(s) share a component with another group: %s"
            % (
                len(report.negative_violations),
                ", ".join(group.group_id for group in report.negative_violations),
            )
        )
    if report.missed:
        failures.append(
            "near group(s) not recalled: %s"
            % ", ".join(group.group_id for group in report.missed)
        )
    return not failures, failures


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="index database written by 'doc-dedupe scan'")
    parser.add_argument(
        "--samples",
        default=str(Path(__file__).resolve().parents[1] / "data" / "samples"),
        help="sample corpus root (default: the repository's data/samples)",
    )
    parser.add_argument(
        "--inject-edges",
        default="none",
        choices=("none", "drop-above", "drop-random"),
        help="remove edges to prove the benchmark can fail",
    )
    parser.add_argument(
        "--inject-groups",
        default="none",
        choices=("none", "shift-groups"),
        help="scramble truth groups to prove the benchmark can fail",
    )
    args = parser.parse_args(argv)

    samples = Path(args.samples).resolve()
    conn = store.connect(args.db)
    try:
        report = run_benchmark(
            conn,
            samples,
            inject_edges=args.inject_edges,
            inject_groups=args.inject_groups,
        )
    finally:
        conn.close()

    for line in report.describe():
        print(line)
    for line in informational_report(report):
        print(line)

    ok, failures = evaluate(report)
    if ok:
        print("B3 OK: recall gate and negative gate both hold")
        return 0
    for failure in failures:
        print("B3 FAIL: %s" % failure)
    return 1

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
