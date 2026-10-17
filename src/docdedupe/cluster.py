"""Clustering, tier assignment and keep recommendations (module 3).

The input is the whole ``pairs`` table, not only the rows above the
near-duplicate threshold. That distinction is the whole point of this module:
template families (the same contract/report/invoice skeleton with different
field values) score around 0.57-0.80, so a 0.80 pre-filter would make the
``template`` tier permanently empty. ``pairs`` keeps everything down to
``pair_store_threshold`` (``data/README.md`` 4.5) precisely so this module can
see those edges.

Four ideas, each with one job
-----------------------------
* **Edge classification.** Every stored pair is either ``exact`` (same
  ``norm_sha256``), ``template`` (isomorphic skeleton, differences confined to
  field positions) or ``near`` (at or above the near-duplicate threshold).
  Anything else is evidence, not an edge: two documents can be 0.56 similar and
  still be unrelated (the corpus has such a pair between two different groups).
* **Union-find over qualified edges.** Clusters are connected components.
* **Tier by the weakest edge.** A cluster containing any ``near`` edge is
  ``near``; otherwise any ``template`` edge makes it ``template``; otherwise it
  is ``exact``. ``near`` wins over ``template`` because "these two copies differ
  in the body" is the stronger, riskier statement.
* **Scores and reasons.** The keep recommendation is a weighted sum of five
  documented dimensions, and the reason string names the dimensions that
  actually decided it. This module is the only place either is computed
  (``plan/03`` module 3); the report layer renders and never re-derives.

Determinism
-----------
* ``cluster_id`` is derived from the sorted member paths, so it does not depend
  on the order pairs were read in.
* Members are sorted by ``path_norm``; clusters are sorted by tier then first
  member.
* Ties in the keep score are broken by ``path_norm`` ascending.
* Nothing here reads the clock. ``built_at`` is supplied by the caller when the
  clusters are written to the index.

Thresholds are not defined here: ``data/README.md`` section 4 owns them and
:class:`docdedupe.config.Settings` carries the defaults.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import fingerprint as fingerprint_mod
from . import normalize as normalize_mod
from . import store
from .config import DEFAULT_SETTINGS, Settings

TIER_EXACT = "exact"
TIER_NEAR = "near"
TIER_TEMPLATE = "template"

# Report order: the two actionable tiers first, the "needs a human" tier last.
TIER_ORDER: Tuple[str, ...] = (TIER_EXACT, TIER_NEAR, TIER_TEMPLATE)

# ---------------------------------------------------------------------------
# Keep-score dimensions
# ---------------------------------------------------------------------------
#
# The five dimensions come from plan/01 module 3 ("mtime 更新、路径更浅、文件名
# 规范、元数据更完整、目录归属"). The weights are a policy choice, not a
# measurement: they decide which copy a human is pointed at first, and every
# one of them is restated in the reason string so the recommendation can be
# audited without reading this file. They live here and nowhere else.
SCORE_WEIGHTS: Mapping[str, float] = {
    "completeness": 0.30,  # a truncated copy is the classic wrong answer
    "recency": 0.25,
    "path_depth": 0.20,
    "name_clean": 0.15,
    "directory": 0.10,
}

# Phrases a reason may use to name a decision dimension. The test suite asserts
# that every cluster's reason contains at least two of them: a reason that says
# only "0.93" is not a reason. The first group states why the members belong
# together; the second states why one copy was preferred; the last is the rule
# that decides a fully tied cluster.
REASON_DIMENSIONS: Tuple[str, ...] = (
    # tier criteria
    "归一化正文逐字节一致",
    "正文相似度达到近重复阈值",
    "同构骨架",
    "字段位",
    # completeness gate
    "截断副本",
    # keep dimensions
    "修改时间最新",
    "路径层级最浅",
    "文件名无副本标记",
    "所在目录为多数副本归属",
    # deterministic tie-break
    "按规范化路径排序",
)

# Copy-suffix markers. The name dimension only asks "does this look like the
# file someone saved twice", so the list stays short and specific: a year in
# parentheses ("报告(2026)") is not a copy marker, a one- or two-digit one is.
_COPY_MARKER_RE = re.compile(r"(副本|拷贝|复制|copy|\(\d{1,2}\)|（\d{1,2}）|~\$)", re.IGNORECASE)

# ``label：value`` / ``label: value``. The label is what a template keeps and the
# value is what changes between family members. 24 characters is generous for a
# real label ("项目名称", "开票单位") and short enough that a prose sentence with
# a colon does not pass as one.
_LABEL_RE = re.compile(r"^([^：:]{1,24})[：:](.+)$")

_DIGITS_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# Input records
# ---------------------------------------------------------------------------


class Document(NamedTuple):
    """One usable document, as this module needs it.

    ``text`` is the extracted text (not the normalized form): the template test
    works on line structure and labels, which normalization deliberately
    removes. It may be empty for a document that appears in no pair, because
    such a document is never tested.
    """

    doc_id: int
    path_abs: str
    path_norm: str
    ext: str
    text_sha256: str
    norm_sha256: str
    norm_chars: int
    mtime_ns: int
    text: str = ""


class Pair(NamedTuple):
    """One row of ``pairs``, reduced to what clustering consumes."""

    a_id: int
    b_id: int
    jaccard: float


class TemplateEvidence(NamedTuple):
    """Why a pair was judged a template-family pair."""

    line_count: int
    field_diffs: int
    skeleton_similarity: float


class _Edge(NamedTuple):
    a_id: int
    b_id: int
    kind: str
    jaccard: float
    line_count: int = 0
    field_diffs: int = 0


@dataclass(frozen=True)
class Cluster:
    """One connected component of qualified edges.

    ``members`` / ``member_ids`` / ``displays`` are parallel tuples, ordered by
    ``path_norm``. ``keep`` is the recommended file for ``exact`` and ``near``
    clusters and ``None`` for ``template`` clusters, which are never
    auto-dispositioned (``data/README.md`` 4.5, ``plan/01`` module 3).
    """

    cluster_id: str
    members: Tuple[str, ...]
    member_ids: Tuple[int, ...]
    displays: Tuple[str, ...]
    tier: str
    keep: Optional[str]
    keep_id: Optional[int]
    keep_display: Optional[str]
    reason: str
    scores: Mapping[str, Mapping[str, float]]


# ---------------------------------------------------------------------------
# Text shape helpers (template test)
# ---------------------------------------------------------------------------


def _content_lines(text: str) -> List[str]:
    """Non-empty stripped lines; the unit the template test compares."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _fold(text: str) -> str:
    """Comparison form of a short span (label, line).

    Deliberately delegates to :func:`docdedupe.normalize.normalize` instead of
    lower-casing here: the five-step normalization has exactly one
    implementation (K22), and a second copy would be free to drift.
    """
    return normalize_mod.normalize(text)


def _label_parts(line: str) -> Optional[Tuple[str, str]]:
    match = _LABEL_RE.match(line)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _skeleton_line(line: str, index: int) -> str:
    """The line with its field value blanked out.

    Line 0 is the title. It carries the project/period identity, which is a
    field value like any other, so the leading token is masked and digits are
    zeroed; whatever remains ("第 0 周工作周报", "费用摘要") is skeleton.
    """
    if index == 0:
        parts = line.split(None, 1)
        tail = parts[1] if len(parts) > 1 else ""
        return "<title> " + _DIGITS_RE.sub("0", tail)
    label = _label_parts(line)
    if label is not None:
        return _fold(label[0]) + "：*"
    return _fold(line)


def _skeleton_shingles(text: str, settings: Settings) -> frozenset:
    skeleton = "\n".join(
        _skeleton_line(line, index) for index, line in enumerate(_content_lines(text))
    )
    return frozenset(fingerprint_mod.shingle_set(skeleton, settings.shingle_size))


def template_evidence(
    left: str,
    right: str,
    settings: Optional[Settings] = None,
    *,
    left_shingles: Optional[frozenset] = None,
    right_shingles: Optional[frozenset] = None,
) -> Optional[TemplateEvidence]:
    """Return evidence when two texts are members of one template family.

    The criterion is structural, not a score (``data/README.md`` 4.5: "成员间
    差异集中于字段位、且存在同构骨架"):

    1. same number of non-empty lines — a truncation or an added paragraph is
       not a field change;
    2. at most ``template_field_diff_max`` differing lines;
    3. every differing line other than the title is a ``label：value`` line on
       both sides **with the same label** — this is what separates "same
       template, different 甲方" from "same document, one sentence rewritten";
    4. the skeleton (values blanked, title token and digits masked) is still at
       least ``template_threshold`` similar.

    Returns ``None`` when the pair is not a template pair, including when the
    two texts are identical: identical content is the ``exact`` tier, a stronger
    statement than "same skeleton".
    """
    cfg = settings or DEFAULT_SETTINGS
    left_lines = _content_lines(left)
    right_lines = _content_lines(right)
    if len(left_lines) != len(right_lines):
        return None
    if len(left_lines) < 2:
        # A one-line document has no skeleton to speak of, and "same label
        # sequence" would degenerate to "same single line".
        return None

    field_diffs = 0
    for index, (left_line, right_line) in enumerate(zip(left_lines, right_lines)):
        if left_line == right_line:
            continue
        if index == 0:
            if max(len(left_line), len(right_line)) > cfg.signature_max_chars:
                return None
            field_diffs += 1
            continue
        left_label = _label_parts(left_line)
        right_label = _label_parts(right_line)
        if left_label is None or right_label is None:
            return None
        if _fold(left_label[0]) != _fold(right_label[0]):
            return None
        field_diffs += 1

    if field_diffs == 0:
        return None
    if field_diffs > cfg.template_field_diff_max:
        return None

    left_set = (
        left_shingles
        if left_shingles is not None
        else _skeleton_shingles(left, cfg)
    )
    right_set = (
        right_shingles
        if right_shingles is not None
        else _skeleton_shingles(right, cfg)
    )
    similarity = fingerprint_mod.jaccard_from_hash_sets(set(left_set), set(right_set))
    if similarity < cfg.template_threshold:
        return None
    return TemplateEvidence(
        line_count=len(left_lines),
        field_diffs=field_diffs,
        skeleton_similarity=similarity,
    )


# ---------------------------------------------------------------------------
# Edge classification and components
# ---------------------------------------------------------------------------


def _edge_kind(
    left: Document,
    right: Document,
    jaccard: float,
    settings: Settings,
    skeleton_cache: Dict[int, frozenset],
) -> Optional[Tuple[str, TemplateEvidence]]:
    """Classify one stored pair, or ``None`` when it is not an edge.

    Order matters. ``exact`` is checked first because identical content must
    never be reported as a template family; ``template`` is checked before
    ``near`` because a template pair can score above the near-duplicate
    threshold (G202's first pair does, at 0.801) and the tier must not flip on
    that accident.
    """
    if left.norm_sha256 and left.norm_sha256 == right.norm_sha256:
        return TIER_EXACT, TemplateEvidence(0, 0, 1.0)

    left_shingles = skeleton_cache.get(left.doc_id)
    if left_shingles is None:
        left_shingles = _skeleton_shingles(left.text, settings)
        skeleton_cache[left.doc_id] = left_shingles
    right_shingles = skeleton_cache.get(right.doc_id)
    if right_shingles is None:
        right_shingles = _skeleton_shingles(right.text, settings)
        skeleton_cache[right.doc_id] = right_shingles

    evidence = template_evidence(
        left.text,
        right.text,
        settings,
        left_shingles=left_shingles,
        right_shingles=right_shingles,
    )
    if evidence is not None:
        return TIER_TEMPLATE, evidence

    if jaccard >= settings.near_duplicate_threshold:
        return TIER_NEAR, TemplateEvidence(0, 0, 0.0)
    return None


class _UnionFind:
    """Disjoint sets with path compression and union by size."""

    def __init__(self) -> None:
        self._parent: Dict[int, int] = {}
        self._size: Dict[int, int] = {}

    def find(self, node: int) -> int:
        parent = self._parent.setdefault(node, node)
        if parent == node:
            return node
        root = self.find(parent)
        self._parent[node] = root
        return root

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        # Union by size keeps the trees shallow; the resulting components do not
        # depend on which side wins, only the internal representative does, and
        # that never leaves this class.
        if self._size.get(root_left, 1) < self._size.get(root_right, 1):
            root_left, root_right = root_right, root_left
        self._parent[root_right] = root_left
        self._size[root_left] = self._size.get(root_left, 1) + self._size.get(root_right, 1)

    def components(self) -> List[List[int]]:
        grouped: Dict[int, List[int]] = {}
        for node in self._parent:
            grouped.setdefault(self.find(node), []).append(node)
        return [sorted(members) for members in grouped.values() if len(members) > 1]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _path_depth(path_norm: str) -> int:
    return path_norm.count("/")


def _has_copy_marker(path_norm: str) -> bool:
    return _COPY_MARKER_RE.search(posixpath.basename(path_norm)) is not None


def _score_members(members: Sequence[Document]) -> Dict[str, Dict[str, float]]:
    """Score every member of one cluster on the five documented dimensions.

    Every dimension is normalized to 0..1 *within the cluster*, so the numbers
    are comparable inside a cluster and meaningless across clusters. That is
    intentional: the question is "which of these copies should I keep", not
    "how good is this file in absolute terms".
    """
    distinct_mtimes = sorted({member.mtime_ns for member in members}, reverse=True)
    mtime_span = max(1, len(distinct_mtimes) - 1)
    recency = {
        value: 1.0 - (index / float(mtime_span))
        for index, value in enumerate(distinct_mtimes)
    }

    depths = {member.path_norm: _path_depth(member.path_norm) for member in members}
    depth_min = min(depths.values())
    depth_span = max(1, max(depths.values()) - depth_min)

    longest = max([member.norm_chars for member in members] + [1])

    directory_counts = Counter(
        posixpath.dirname(member.path_norm) for member in members
    )
    most_copies = max(directory_counts.values())
    modal_directories = sorted(
        directory for directory, count in directory_counts.items() if count == most_copies
    )
    modal_directory = modal_directories[0]

    scores: Dict[str, Dict[str, float]] = {}
    for member in members:
        dimensions = {
            "completeness": member.norm_chars / float(longest),
            "recency": recency[member.mtime_ns],
            "path_depth": 1.0 - ((depths[member.path_norm] - depth_min) / float(depth_span)),
            "name_clean": 0.0 if _has_copy_marker(member.path_norm) else 1.0,
            "directory": (
                1.0
                if posixpath.dirname(member.path_norm) == modal_directory
                else 0.0
            ),
        }
        total = sum(
            SCORE_WEIGHTS[name] * value for name, value in dimensions.items()
        )
        dimensions["total"] = total
        scores[member.path_norm] = {
            name: round(value, 6) for name, value in dimensions.items()
        }
    return scores


_DIMENSION_PHRASES: Tuple[Tuple[str, str], ...] = (
    ("recency", "修改时间最新"),
    ("path_depth", "路径层级最浅"),
    ("name_clean", "文件名无副本标记"),
    ("directory", "所在目录为多数副本归属"),
)
# ``completeness`` is deliberately absent: it is a *gate* (a truncated copy is
# never eligible), so among the candidates this function compares it is always
# 1.0 and can never be the dimension that decided anything. It stays in the
# score table so a reader can see the gate's input, and the reason string names
# it separately ("先排除 N 份正文较短的截断副本").


def _decisive_dimensions(
    scores: Mapping[str, Mapping[str, float]],
    winner: str,
    others: Sequence[str],
) -> List[str]:
    """Dimensions on which the winner is at least as good as everyone and
    strictly better than someone.

    "At least as good as everyone" stops a reason from claiming a dimension the
    winner did not actually win; "strictly better than someone" stops it from
    listing a dimension that is tied and therefore decided nothing.
    """
    phrases = []
    for name, phrase in _DIMENSION_PHRASES:
        mine = scores[winner][name]
        best_other = max([scores[other][name] for other in others] + [-1.0])
        worst_other = min([scores[other][name] for other in others] + [2.0])
        if mine >= best_other and mine > worst_other:
            phrases.append(phrase)
    return phrases


def _length_parts(
    members: Sequence[Document], winner: Document
) -> List[str]:
    """How the other copies compare in length, one phrase per kind of difference.

    The winner is excluded: "3 份正文等长" in a three-member cluster would
    describe the kept copy as one of its own alternatives, which is both wrong
    and confusing in a sentence that starts with "其余".
    """
    others = [member for member in members if member.doc_id != winner.doc_id]
    shorter = sum(1 for member in others if member.norm_chars < winner.norm_chars)
    longer = sum(1 for member in others if member.norm_chars > winner.norm_chars)
    equal = len(others) - shorter - longer
    parts = []
    if shorter:
        parts.append("%d 份正文较短" % shorter)
    if longer:
        parts.append("%d 份正文更长" % longer)
    if equal:
        parts.append("%d 份正文等长" % equal)
    return parts


def _others_clause(members: Sequence[Document], winner: Document) -> str:
    """One sentence about the copies that are not recommended.

    With a single other copy its own phrase already carries the count, so the
    sentence reads "其余 1 份正文等长" rather than "其余 1 份中 1 份正文等长"; with
    several, the counts go in the contrast ("其余 5 份中 3 份正文较短、2 份正文等长")
    so the number of copies is stated once instead of being implied twice.
    """
    others = [member for member in members if member.doc_id != winner.doc_id]
    parts = _length_parts(members, winner)
    if not parts:  # every member is the winner: cannot happen in a real cluster
        return ""
    if len(others) == 1 and len(parts) == 1:
        return "其余 %s" % parts[0]
    return "其余 %d 份中 %s" % (len(others), "、".join(parts))


def _tie_break_phrase() -> str:
    """What actually decided the keep when no dimension separated the members.

    "Scores are close" is not a decision; this names the real rule so the
    recommendation stays auditable when every dimension ties. It deliberately
    says nothing about body length: when this phrase is used the truncation gate
    has already removed the shorter copies, so length is settled and is not part
    of the tie.
    """
    return (
        "候选副本在修改时间、路径层级、文件名与目录归属上并列，"
        "按规范化路径排序取首份"
    )


def _cluster_id(members: Sequence[str]) -> str:
    payload = "\n".join(sorted(members))
    return "c" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _common_parent(paths: Sequence[str]) -> str:
    try:
        return posixpath.commonpath(list(paths))
    except ValueError:
        # Different drives on Windows; no shared prefix exists.
        return ""


def display_paths(paths: Sequence[str]) -> Tuple[str, ...]:
    """Shorten member paths to their common parent, for a human to read.

    Presentation only, and therefore *not* a decision: the review page calls this
    on paths it read back from the index, so a page and the report describe the
    same file with the same spelling. When there is no common parent — two
    different Windows drives, where ``posixpath.commonpath`` raises — only the
    file names survive.
    """
    parent = _common_parent(paths)
    if not parent:
        return tuple(posixpath.basename(path) for path in paths)
    return tuple(posixpath.relpath(path, parent) for path in paths)


# ---------------------------------------------------------------------------
# Cluster construction
# ---------------------------------------------------------------------------


def _build_one(
    doc_ids: Sequence[int],
    edges: Sequence[_Edge],
    by_id: Mapping[int, Document],
    settings: Settings,
) -> Cluster:
    members = sorted((by_id[doc_id] for doc_id in doc_ids), key=lambda doc: doc.path_norm)
    member_paths = tuple(member.path_norm for member in members)
    member_ids = tuple(member.doc_id for member in members)
    displays = display_paths(member_paths)
    member_set = set(member_ids)
    internal = [edge for edge in edges if edge.a_id in member_set and edge.b_id in member_set]

    kinds = {edge.kind for edge in internal}
    if TIER_NEAR in kinds:
        tier = TIER_NEAR
    elif TIER_TEMPLATE in kinds:
        tier = TIER_TEMPLATE
    else:
        tier = TIER_EXACT

    scores = _score_members(members)

    if tier == TIER_TEMPLATE:
        template_edges = sorted(
            (edge for edge in internal if edge.kind == TIER_TEMPLATE),
            key=lambda edge: (edge.a_id, edge.b_id),
        )
        line_count = template_edges[0].line_count if template_edges else 0
        field_diffs = max([edge.field_diffs for edge in template_edges] + [0])
        reason = (
            "模板族：%d 份成员共享同构骨架（%d 行、字段位序列一致），"
            "差异仅出现在 %d 个字段位；不给出保留建议，需人工裁决。"
            % (len(members), line_count, field_diffs)
        )
        return Cluster(
            cluster_id=_cluster_id(member_paths),
            members=member_paths,
            member_ids=member_ids,
            displays=displays,
            tier=tier,
            keep=None,
            keep_id=None,
            keep_display=None,
            reason=reason,
            scores=scores,
        )

    ranked = sorted(
        members, key=lambda doc: (-scores[doc.path_norm]["total"], doc.path_norm)
    )

    # Two dimensions are *gates*, not weights, because they identify a file that
    # is wrong to keep rather than merely less attractive:
    #
    # * **Completeness.** A truncated copy is the classic wrong recommendation:
    #   it usually has the newest mtime (it was saved last) and the same clean
    #   name, so a pure weighted sum can prefer it over the full document.
    # * **Copy-marked name.** ``报告(1).txt`` is the copy by construction; the
    #   marker is worth more than the 0.15 weight it carries, and no combination
    #   of newer mtime and shallower path should ever outvote "this file is the
    #   second save of another one".
    #
    # Gates are applied in order, each only narrowing the candidate set, so the
    # weighted score still ranks whatever survives. If a gate removes nothing it
    # is not mentioned in the reason.
    longest_chars = max(member.norm_chars for member in members)
    eligible = [member for member in ranked if member.norm_chars == longest_chars]
    truncated = len(members) - len(eligible)

    unmarked = [
        member for member in eligible if not _has_copy_marker(member.path_norm)
    ]
    marked = len(eligible) - len(unmarked)
    if unmarked:
        eligible = unmarked

    winner = eligible[0]
    others = [member for member in ranked if member.doc_id != winner.doc_id]
    decisive = _decisive_dimensions(
        scores, winner.path_norm, [doc.path_norm for doc in others]
    )
    keep_display = displays[member_paths.index(winner.path_norm)]
    others_clause = _others_clause(members, winner)

    # The tier statement is the first decision dimension. The second is either
    # the dimensions the winner actually won or, when nothing separated the
    # members, the rule that broke the tie — never a bare score.
    decision = "、".join(decisive) if decisive else _tie_break_phrase()
    gates = []
    if truncated:
        gates.append("先排除 %d 份正文较短的截断副本；" % truncated)
    if marked:
        gates.append("再排除 %d 份文件名带副本标记；" % marked)
    gate = "".join(gates)
    if tier == TIER_EXACT:
        reason = (
            "保留 %s：归一化正文逐字节一致；%s%s。%s。"
            % (keep_display, gate, decision, others_clause)
        )
    else:
        reason = (
            "保留 %s：正文相似度达到近重复阈值；%s%s。%s。"
            % (keep_display, gate, decision, others_clause)
        )

    return Cluster(
        cluster_id=_cluster_id(member_paths),
        members=member_paths,
        member_ids=member_ids,
        displays=displays,
        tier=tier,
        keep=winner.path_norm,
        keep_id=winner.doc_id,
        keep_display=keep_display,
        reason=reason,
        scores=scores,
    )


def build_clusters(
    docs: Iterable[Document],
    pairs: Iterable[Pair],
    settings: Optional[Settings] = None,
) -> List[Cluster]:
    """Build clusters from documents and the stored pair evidence.

    Only pairs that classify as ``exact``, ``template`` or ``near`` become
    edges. A pair below the near-duplicate threshold that is not a template pair
    stays evidence in the index and connects nothing — otherwise the retention
    floor would silently become the decision threshold, which is exactly the
    mistake ``data/README.md`` 4.5 warns about.

    The result contains only clusters of two or more members; a document that
    resembles nothing is not a finding.
    """
    cfg = settings or DEFAULT_SETTINGS
    by_id = {doc.doc_id: doc for doc in docs}
    skeleton_cache: Dict[int, frozenset] = {}

    edges: List[_Edge] = []
    for pair in pairs:
        left = by_id.get(pair.a_id)
        right = by_id.get(pair.b_id)
        if left is None or right is None or left.doc_id == right.doc_id:
            continue
        classified = _edge_kind(left, right, pair.jaccard, cfg, skeleton_cache)
        if classified is None:
            continue
        kind, evidence = classified
        edges.append(
            _Edge(
                a_id=left.doc_id,
                b_id=right.doc_id,
                kind=kind,
                jaccard=pair.jaccard,
                line_count=evidence.line_count,
                field_diffs=evidence.field_diffs,
            )
        )

    union_find = _UnionFind()
    for edge in edges:
        union_find.union(edge.a_id, edge.b_id)

    clusters = [
        _build_one(component, edges, by_id, cfg)
        for component in union_find.components()
    ]
    clusters.sort(key=lambda cluster: (TIER_ORDER.index(cluster.tier), cluster.members[0]))
    return clusters


# ---------------------------------------------------------------------------
# Index adapter
# ---------------------------------------------------------------------------


def load_inputs(conn) -> Tuple[List[Document], List[Pair]]:
    """Read the documents and pairs this module consumes from the index.

    Extracted text is fetched only for documents that appear in at least one
    pair: the template test needs it, and a document with no pair can never be
    part of a cluster. That keeps the read proportional to the candidate set
    rather than to the corpus.
    """
    pair_rows = store.pair_rows(conn)
    participants = set()
    for row in pair_rows:
        participants.add(int(row["a_id"]))
        participants.add(int(row["b_id"]))
    texts = store.document_texts(conn, sorted(participants))

    docs = [
        Document(
            doc_id=int(row["id"]),
            path_abs=str(row["path_abs"]),
            path_norm=str(row["path_norm"]),
            ext=str(row["ext"]),
            text_sha256=str(row["text_sha256"]),
            norm_sha256=str(row["norm_sha256"]),
            norm_chars=int(row["norm_chars"]),
            mtime_ns=int(row["mtime_ns"]),
            text=texts.get(int(row["id"]), ""),
        )
        for row in store.cluster_document_rows(conn)
    ]
    pairs = [
        Pair(
            a_id=int(row["a_id"]),
            b_id=int(row["b_id"]),
            jaccard=float(row["jaccard"]),
        )
        for row in pair_rows
    ]
    return docs, pairs


def clusters_from_index(conn, settings: Optional[Settings] = None) -> List[Cluster]:
    """Build clusters from a scanned index."""
    docs, pairs = load_inputs(conn)
    return build_clusters(docs, pairs, settings)


def scores_json(scores: Mapping[str, Mapping[str, float]]) -> str:
    """Canonical JSON for a cluster's score table.

    Sorted keys and no spaces: the stored value has to be byte-stable, because
    the determinism test compares two independently built indexes.
    """
    return json.dumps(scores, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "Cluster",
    "Document",
    "Pair",
    "REASON_DIMENSIONS",
    "SCORE_WEIGHTS",
    "TIER_EXACT",
    "TIER_NEAR",
    "TIER_ORDER",
    "TIER_TEMPLATE",
    "TemplateEvidence",
    "build_clusters",
    "clusters_from_index",
    "display_paths",
    "load_inputs",
    "scores_json",
    "template_evidence",
]
