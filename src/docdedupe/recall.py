"""Recall evaluation against the implanted truth (benchmark B3).

The truth file describes *groups*: which files belong together and which tier the
group is. It deliberately records no scores (``data/README.md`` 4.7-4.8), so the
only way to measure recall is to rebuild clusters from the engine's own pairs and
compare the partition with the truth. That comparison lives here, once, so the
benchmark tool and the test suite cannot drift into two different definitions.

Two gates, both from ``data/README.md`` 4.7:

* a **near** group is recalled when its members form one component and that
  component contains no member of another group;
* a **negative** file must stay alone in its component. This is the false-positive
  side of the same gate: a benchmark that only checks "did we find the planted
  duplicates" passes on an engine that connects everything to everything.

Records are path-keyed, not id-keyed: the caller passes the edges from the index
and the truth's relative paths, and this module owns the comparison.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, NamedTuple, Sequence, Tuple


class GroupResult(NamedTuple):
    """Outcome for one truth group."""

    group_id: str
    tier: str
    # A near/template/exact group passes when its members land in one component
    # with no foreign member. A negative has no expected partner, so its passing
    # condition is the opposite: nothing else in its component.
    passed: bool
    # Number of separate components the group's members fell into. ``1`` is the
    # healthy value; anything larger says *how* the group broke apart.
    components: int
    # Groups whose members ended up in the same component. Empty for a clean run.
    foreign_groups: Tuple[str, ...]
    members: Tuple[str, ...]

    def describe(self) -> str:
        """One line a human can read without consulting the rest of the report."""
        if self.passed:
            if self.tier == "negative":
                return "PASS %-6s %-9s no foreign member" % (self.group_id, self.tier)
            return "PASS %-6s %-9s %d member(s) in 1 component" % (
                self.group_id,
                self.tier,
                len(self.members),
            )
        reasons = []
        if self.components > 1:
            reasons.append("split into %d components" % self.components)
        if self.foreign_groups:
            reasons.append("merged with %s" % ", ".join(self.foreign_groups))
        return "FAIL %-6s %-9s %s" % (
            self.group_id,
            self.tier,
            "; ".join(reasons) or "not recalled",
        )


class RecallReport(NamedTuple):
    """Full B3 result: per-group detail plus the two gate outcomes."""

    groups: Tuple[GroupResult, ...]
    near_total: int
    near_recalled: int
    negative_violations: Tuple[GroupResult, ...]

    @property
    def rate(self) -> float:
        if not self.near_total:
            return 0.0
        return self.near_recalled / float(self.near_total)

    @property
    def missed(self) -> Tuple[GroupResult, ...]:
        return tuple(group for group in self.groups if group.tier == "near" and not group.passed)

    def describe(self) -> List[str]:
        """Per-group lines, then the two summary gates.

        Only the gate-bearing tiers are listed as PASS/FAIL. Template groups are
        reported by the caller as information: their members are below the
        near-duplicate threshold by construction, and the template tier is a
        module 3 decision, so calling them FAIL here would misattribute a
        deliberate design point to a broken engine.
        """
        lines = [group.describe() for group in self.groups if group.tier != "template"]
        lines.append(
            "recall %d/%d = %.1f%% over near groups"
            % (self.near_recalled, self.near_total, 100.0 * self.rate)
        )
        lines.append(
            "negative groups with a foreign member: %d" % len(self.negative_violations)
        )
        return lines


def _components(
    edges: Iterable[Tuple[str, str, float]], threshold: float
) -> Dict[str, str]:
    """Map every node to a component representative, over edges at ``threshold``.

    Files that appear in no edge are absent from the map; callers treat a
    missing node as its own component (which is what a lone negative should be).
    """
    parent: Dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        # Path compression keeps repeated lookups cheap on a long chain.
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    for left, right, score in edges:
        if score < threshold:
            continue
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    return {node: find(node) for node in list(parent)}


def evaluate_recall(
    groups: Sequence[dict],
    edges: Iterable[Tuple[str, str, float]],
    threshold: float,
    *,
    tiers: Sequence[str] = ("near", "exact", "template", "negative"),
    path_key=None,
) -> RecallReport:
    """Compare the engine's partition with the truth groups.

    ``groups`` is the manifest's ``groups`` list (``group_id``, ``tier``,
    ``files``). ``edges`` is ``(path_a, path_b, score)``. ``threshold`` is the
    near-duplicate threshold from the settings; edges below it connect nothing.

    ``path_key`` normalizes both sides before they are compared. The two inputs
    come from different places — the truth file holds relative names, the index
    holds absolute normalized paths — and comparing them without a shared key
    silently matches nothing, which looks like "the engine found no duplicates at
    all". Passing ``store.normalize_path`` (or a resolver built on it) makes the
    join explicit and the failure mode impossible.
    """
    key = path_key or (lambda value: str(value))

    root_of = _components(
        ((key(left), key(right), score) for left, right, score in edges), threshold
    )

    members_of: Dict[str, str] = {}
    for group in groups:
        for name in group["files"]:
            members_of[key(name)] = str(group["group_id"])

    results: List[GroupResult] = []
    near_total = 0
    near_recalled = 0
    negative_violations: List[GroupResult] = []

    for group in groups:
        tier = str(group["tier"])
        if tier not in tiers:
            continue
        members = tuple(str(name) for name in group["files"])
        member_keys = tuple(key(name) for name in members)
        roots = {root_of.get(member, member) for member in member_keys}
        component_count = len(roots)

        # Everything reachable from the group's own components, mapped back to
        # truth groups. Using the root map (rather than walking edges) keeps a
        # transitive merge visible no matter how many hops away it happened.
        reachable_groups = {
            members_of[node]
            for node, root in root_of.items()
            if root in roots and node not in member_keys
        }
        foreign_groups = tuple(sorted(reachable_groups))

        if tier == "negative":
            passed = not foreign_groups
        else:
            passed = component_count == 1 and not foreign_groups

        result = GroupResult(
            group_id=str(group["group_id"]),
            tier=tier,
            passed=passed,
            components=component_count,
            foreign_groups=foreign_groups,
            members=members,
        )
        results.append(result)

        if tier == "near":
            near_total += 1
            if passed:
                near_recalled += 1
        elif tier == "negative" and not passed:
            negative_violations.append(result)

    return RecallReport(
        groups=tuple(results),
        near_total=near_total,
        near_recalled=near_recalled,
        negative_violations=tuple(negative_violations),
    )


__all__ = ["GroupResult", "RecallReport", "evaluate_recall"]
