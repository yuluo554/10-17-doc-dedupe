"""Benchmark B4: tier labels and keep recommendations, per truth group.

Run against an index produced by ``doc-dedupe scan``::

    python tools/bench_tiers.py --db <index.db> --samples data/samples

B3 (``tools/bench_recall.py``) asks "were the duplicates found". B4 asks the
next two questions, which B3 cannot answer because it only sees the edge set:

1. **Does every truth group land in the right tier?** A ``near`` group must be
   a ``near`` cluster, an ``exact`` group an ``exact`` cluster, and a
   ``template`` group a ``template`` cluster — not a ``near`` one. This is the
   gate ``plan/05`` M3 names: template families must not be mixed into the
   near-duplicate tier.
2. **Is every keep recommendation a defensible one?** Each non-template cluster
   must name a keep, the keep must be a member, and the reason must name at
   least two decision dimensions (``plan/03`` module 3). A template cluster must
   name *no* keep: the tier exists precisely so those are not auto-dispositioned.

The truth manifest records no scores and no tiers beyond the group's own
``tier`` field, so this tool only reads ``group_id`` / ``tier`` / ``files``.

``--inject`` breaks the pipeline deliberately and the benchmark must then fail;
a benchmark that stays green under an injection is a fake gate. The injections
here are the two mistakes this module is most likely to make: lowering the
near-duplicate threshold until template families qualify as near duplicates, and
swallowing the template tier into ``near`` outright.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from docdedupe import cluster as cluster_mod
from docdedupe import store
from docdedupe.config import DEFAULT_SETTINGS, Settings

import bench_common

# The two gates are defined in plan/05 (M3 DoD) and data/README.md 4.5. Repeated
# here as the benchmark's own numbers so the tool can fail; the test suite
# asserts the same values, so changing one without the other is visible.
TIER_ACCURACY_FLOOR = 1.0  # every truth group, no exceptions
MIN_REASON_DIMENSIONS = 2


@dataclass
class GroupOutcome:
    """What happened to one truth group."""

    group_id: str
    truth_tier: str
    expected_tier: str
    found_tiers: Tuple[str, ...]
    members_clustered: int
    members_total: int
    keep: Optional[str]
    reason: str
    dimensions: int

    @property
    def tier_ok(self) -> bool:
        return self.found_tiers == (self.expected_tier,)

    @property
    def complete(self) -> bool:
        return self.members_clustered == self.members_total

    @property
    def keep_ok(self) -> bool:
        if self.expected_tier == "template":
            # A template family must not carry a keep recommendation.
            return self.keep is None
        return self.keep is not None

    @property
    def reason_ok(self) -> bool:
        return self.dimensions >= MIN_REASON_DIMENSIONS

    @property
    def passed(self) -> bool:
        return self.tier_ok and self.complete and self.keep_ok and self.reason_ok

    def describe(self) -> str:
        if self.passed:
            return "PASS %-6s %-9s %d/%d member(s), keep=%s, dimensions=%d" % (
                self.group_id,
                self.expected_tier,
                self.members_clustered,
                self.members_total,
                self.keep or "-",
                self.dimensions,
            )
        reasons = []
        if not self.complete:
            reasons.append(
                "%d of %d member(s) clustered"
                % (self.members_clustered, self.members_total)
            )
        if not self.tier_ok:
            reasons.append(
                "tier %s, expected %s"
                % (",".join(self.found_tiers) or "none", self.expected_tier)
            )
        if not self.keep_ok:
            reasons.append(
                "keep=%s is wrong for the %s tier"
                % (self.keep or "-", self.expected_tier)
            )
        if not self.reason_ok:
            reasons.append(
                "reason names %d dimension(s), needs %d"
                % (self.dimensions, MIN_REASON_DIMENSIONS)
            )
        return "FAIL %-6s %-9s %s" % (self.group_id, self.expected_tier, "; ".join(reasons))


def _relative_key(root: Path):
    root_norm = store.normalize_path(str(root.resolve()))

    def key(value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            path = root / value
        return store.normalize_path(str(path))

    return key


def _relative_name(root_norm: str, path_norm: str) -> str:
    return posixpath.relpath(path_norm, root_norm)


def load_groups(manifest_path: Path) -> List[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(manifest["groups"])


def count_dimensions(reason: str) -> int:
    """How many documented decision dimensions a reason names.

    Counted against :data:`docdedupe.cluster.REASON_DIMENSIONS`, which is the
    list the reason builder is written against. A reason that only quotes a
    number names zero.
    """
    return sum(1 for phrase in cluster_mod.REASON_DIMENSIONS if phrase in reason)


# Variants whose differences are *container-level*: a different file format, a
# full-width/half-width spelling, or a running head and page numbers. The
# normalization pipeline (data/README.md 4.3) is documented to erase exactly
# these, so a group built entirely from them normalizes to byte-identical text
# and therefore belongs to the ``exact`` tier by the definition in
# data/README.md 4.5 — even though the manifest's ``tier`` records the
# *generation* category ("near", i.e. "these are variants of one document").
#
# The rule is applied from the manifest's own ``variant`` labels, never from
# engine output, so the benchmark cannot agree with the engine by construction.
CONTAINER_VARIANTS = frozenset({"original", "reformat", "fullwidth", "header_footer"})


def expected_tier(group: dict) -> str:
    """The tier the documented definitions imply for one truth group.

    ``template`` and ``exact`` groups keep their manifest tier. A ``near`` group
    whose every member is a container-level variant is ``exact`` after
    normalization (see :data:`CONTAINER_VARIANTS`); anything else stays ``near``.
    """
    tier = str(group["tier"])
    if tier != "near":
        return tier
    variants = {str(member.get("variant", "")) for member in group.get("members", [])}
    if variants and variants <= CONTAINER_VARIANTS:
        return "exact"
    return tier


def evaluate_groups(
    groups: Sequence[dict],
    clusters: Sequence[cluster_mod.Cluster],
    samples_root: Path,
) -> List[GroupOutcome]:
    """Compare each truth group with the clusters that actually formed."""
    root_norm = store.normalize_path(str(samples_root.resolve()))
    key = _relative_key(samples_root)

    cluster_of: Dict[str, cluster_mod.Cluster] = {}
    for cluster in clusters:
        for member in cluster.members:
            cluster_of[member] = cluster

    def keep_label(path: Optional[str]) -> Optional[str]:
        """Manifest-relative spelling, so a passing line names a file the
        reader can find in ``data/samples`` instead of an absolute path."""
        if path is None:
            return None
        return _relative_name(root_norm, path)

    outcomes: List[GroupOutcome] = []
    for group in groups:
        tier = str(group["tier"])
        if tier == "negative":
            # A negative has no expected cluster; B3 owns the false-positive
            # gate. Counting it here would report "0 of 1 clustered" as a
            # failure for a file that is supposed to stand alone.
            continue
        member_keys = [key(name) for name in group["files"]]
        matched = [cluster_of.get(member) for member in member_keys]
        found = tuple(
            sorted({item.tier for item in matched if item is not None})
        )
        clustered = sum(1 for item in matched if item is not None)

        # A group that landed in several clusters has no single reason; report
        # the first matched cluster's keep and take the *weakest* reason among
        # the matched clusters so one bad reason cannot hide behind a good one.
        keeps = sorted({item.keep for item in matched if item is not None and item.keep})
        reasons = [item.reason for item in matched if item is not None]
        dimensions = min(
            (count_dimensions(reason) for reason in reasons), default=0
        )

        outcomes.append(
            GroupOutcome(
                group_id=str(group["group_id"]),
                truth_tier=tier,
                expected_tier=expected_tier(group),
                found_tiers=found,
                members_clustered=clustered,
                members_total=len(group["files"]),
                keep=keep_label(keeps[0]) if len(keeps) == 1 else None,
                reason=reasons[0] if reasons else "",
                dimensions=dimensions,
            )
        )
    return outcomes


@dataclass
class TierReport:
    """Full B4 result."""

    outcomes: Tuple[GroupOutcome, ...]
    clusters: Tuple[cluster_mod.Cluster, ...]
    contaminated: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.passed)

    @property
    def accuracy(self) -> float:
        if not self.total:
            return 0.0
        return self.passed / float(self.total)

    @property
    def failures(self) -> Tuple[GroupOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.passed)

    def tier_counts(self) -> Dict[str, int]:
        counts = {tier: 0 for tier in cluster_mod.TIER_ORDER}
        for cluster in self.clusters:
            counts[cluster.tier] = counts.get(cluster.tier, 0) + 1
        return counts

    def describe(self) -> List[str]:
        lines = [outcome.describe() for outcome in self.outcomes]
        for cluster_id, group_ids in self.contaminated:
            lines.append(
                "FAIL %-14s cluster mixes truth groups: %s"
                % (cluster_id, ", ".join(group_ids))
            )
        counts = self.tier_counts()
        lines.append(
            "tiers: exact %d / near %d / template %d"
            % (counts["exact"], counts["near"], counts["template"])
        )
        lines.append(
            "groups %d/%d = %.1f%% (tier + membership + keep + reason); "
            "contaminated clusters: %d"
            % (self.passed, self.total, 100.0 * self.accuracy, len(self.contaminated))
        )
        return lines


def _inject_clusters(
    clusters: Sequence[cluster_mod.Cluster], mode: str
) -> List[cluster_mod.Cluster]:
    """Relabel clusters to imitate the two likely defects.

    ``template-as-near``: report every template cluster as a near duplicate,
    which is what happens if the template test is dropped. ``near-as-template``:
    the opposite mistake, swallowing real near duplicates into the tier that is
    never dispositioned.
    """
    if mode == "none":
        return list(clusters)
    if mode not in ("template-as-near", "near-as-template"):
        raise ValueError("unknown cluster injection %r" % mode)
    target = "template" if mode == "template-as-near" else "near"
    replacement = "near" if target == "template" else "template"
    injected = []
    for cluster in clusters:
        if cluster.tier != target:
            injected.append(cluster)
            continue
        injected.append(
            cluster_mod.Cluster(
                cluster_id=cluster.cluster_id,
                members=cluster.members,
                member_ids=cluster.member_ids,
                displays=cluster.displays,
                tier=replacement,
                # A near cluster with no keep fails the keep gate as well, and a
                # template cluster carrying a keep fails its own; both are the
                # consequence of the relabel, not extra injections.
                keep=cluster.keep,
                keep_id=cluster.keep_id,
                keep_display=cluster.keep_display,
                reason=cluster.reason,
                scores=cluster.scores,
            )
        )
    return injected


def find_contamination(
    groups: Sequence[dict],
    clusters: Sequence[cluster_mod.Cluster],
    samples_root: Path,
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """Clusters whose members come from more than one truth group.

    Per-group tier labels alone cannot catch this: lowering the near-duplicate
    threshold merges G001 with G015 (they share a 0.56 pair), and every group
    would still be reported with the tier it "should" have while the cluster it
    actually sits in is wrong. A merge is the failure mode this gate exists to
    catch, so it is checked directly.
    """
    key = _relative_key(samples_root)
    group_of: Dict[str, str] = {}
    for group in groups:
        for name in group["files"]:
            group_of[key(name)] = str(group["group_id"])

    contaminated = []
    for cluster in clusters:
        ids = sorted(
            {group_of[member] for member in cluster.members if member in group_of}
        )
        if len(ids) > 1:
            contaminated.append((cluster.cluster_id, tuple(ids)))
    return tuple(contaminated)


def evaluate(
    report: TierReport, *, floor: float = TIER_ACCURACY_FLOOR
) -> Tuple[bool, List[str]]:
    """Apply both B4 gates and return the verdict plus the reasons it failed."""
    failures = []
    if report.total == 0:
        failures.append("no non-negative truth groups were evaluated; the gate would be vacuous")
    if report.accuracy < floor:
        failures.append(
            "group accuracy %.1f%% is below the %.0f%% floor (%d/%d groups); "
            "a group passes only when its tier, membership, keep and reason all hold"
            % (100.0 * report.accuracy, 100.0 * floor, report.passed, report.total)
        )
    if report.failures:
        failures.append(
            "group(s) not matched: %s"
            % ", ".join(outcome.group_id for outcome in report.failures)
        )
    if report.contaminated:
        failures.append(
            "%d cluster(s) mix truth groups: %s"
            % (
                len(report.contaminated),
                "; ".join(
                    "%s = %s" % (cluster_id, "+".join(ids))
                    for cluster_id, ids in report.contaminated
                ),
            )
        )
    # A report with no clusters at all would pass the per-group loop vacuously if
    # the truth groups were also missing; make the "engine produced nothing"
    # case explicit.
    if not report.clusters:
        failures.append("no clusters were built; every tier is empty")
    return not failures, failures


def run_benchmark(
    conn,
    samples_root: Path,
    *,
    settings: Optional[Settings] = None,
    inject_clusters: str = "none",
) -> TierReport:
    clusters = cluster_mod.clusters_from_index(conn, settings)
    clusters = _inject_clusters(clusters, inject_clusters)
    groups = load_groups(samples_root / "truth" / "manifest.json")
    outcomes = evaluate_groups(groups, clusters, samples_root)
    return TierReport(
        outcomes=tuple(outcomes),
        clusters=tuple(clusters),
        contaminated=find_contamination(groups, clusters, samples_root),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="index database written by 'doc-dedupe scan'")
    parser.add_argument(
        "--samples",
        default=str(Path(__file__).resolve().parents[1] / "data" / "samples"),
        help="sample corpus root (default: the repository's data/samples)",
    )
    parser.add_argument(
        "--inject-clusters",
        default="none",
        choices=("none", "template-as-near", "near-as-template"),
        help="relabel clusters to prove the benchmark can fail",
    )
    parser.add_argument(
        "--near-threshold",
        type=float,
        default=DEFAULT_SETTINGS.near_duplicate_threshold,
        help="override the near-duplicate threshold (for fault injection)",
    )
    args = parser.parse_args(argv)

    samples = Path(args.samples).resolve()
    settings = DEFAULT_SETTINGS.with_overrides(
        near_duplicate_threshold=args.near_threshold
    )
    conn = store.connect(args.db)
    try:
        hint = bench_common.corpus_index_mismatch_hint(conn, samples)
        if hint:
            print("HINT: %s" % hint)
        report = run_benchmark(
            conn,
            samples,
            settings=settings,
            inject_clusters=args.inject_clusters,
        )
    finally:
        conn.close()

    for line in report.describe():
        print(line)

    ok, failures = evaluate(report)
    if ok:
        print("B4 OK: every truth group landed in its tier with a defensible keep")
        return 0
    for failure in failures:
        print("B4 FAIL: %s" % failure)
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
