"""Paragraph-level diff and the difference summary sentence (module 4).

The review page shows two documents side by side and says, in one sentence,
what separates them. Both come from here: :func:`diff_documents` aligns the
paragraphs, and :func:`summarize` turns the alignment into the single sentence
``data/README.md`` 4.10 defines. The page renders what it is given and never
builds a sentence of its own, so the two views of "how different are these" can
never drift apart.

Why the extracted text and not the normalized text
--------------------------------------------------
The input is ``documents.text`` — the extracted body — not ``documents.norm_text``.
Normalization (``data/README.md`` 4.3) deliberately erases line structure: it
joins the body into one string and drops running heads, page numbers and
full-width spellings. That is exactly what makes two copies compare equal, and
exactly what makes them impossible to *show*: a reviewer looking at a diff needs
the paragraphs, the running heads and the page numbers back.

The normalization result is still used, once, as a *classification* signal: when
the two bodies normalize to the same string, the visible differences are
container-level noise (full-width digits, a running head, page numbers) rather
than a content change, and the summary says so instead of counting those
paragraphs as differences. That check calls :mod:`docdedupe.normalize`, the one
implementation of the rule, rather than restating it.

Character-level highlight
-------------------------
Inside a replaced paragraph, the characters that actually changed are found with
the character n-grams the rest of the project already uses
(:func:`docdedupe.fingerprint.shingle_set`): a character is "changed" when every
n-gram covering it is missing from the other side. One n-gram that survives
means the character still has context in common, so it stays unhighlighted. That
is what keeps ``本阶段…按既定口径`` vs ``现阶段…依统一口径`` from lighting up the
whole paragraph.

What is deliberately *not* here
-------------------------------
``data/README.md`` 4.10 lists three sentences; this module implements two of
them and says so out loud. "差一页" ("differs by a page") needs a page boundary
to compare against, and the extraction layer produces a single text stream with
no page segmentation — there is no honest way to decide where a page starts, and
inventing a threshold for an undefined quantity would be a made-up number. It is
parked as an extension item (``plan/03`` section 6) instead. The summary
function therefore has no page branch at all, and
``tests/test_diffing.py`` asserts it never emits a page-level claim.

Determinism
-----------
``difflib.SequenceMatcher`` is deterministic for a given input, and
``autojunk`` is disabled so that behaviour cannot change with the paragraph
count. Nothing here reads the clock or iterates a set in output order.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import fingerprint as fingerprint_mod
from . import normalize as normalize_mod
from .config import DEFAULT_SETTINGS, Settings

# Segment kinds. The page renders one row per row-*pair*; the two sides are in
# row order, so a client walks them with two cursors (see the module docstring
# of ``web/server.py``). ``replace`` means both sides have a paragraph here,
# ``add`` / ``del`` mean only one side does.
KIND_SAME = "same"
KIND_ADD = "add"
KIND_DEL = "del"
KIND_REPLACE = "replace"
SEGMENT_KINDS: Tuple[str, ...] = (KIND_SAME, KIND_ADD, KIND_DEL, KIND_REPLACE)

# The sentences of data/README.md 4.10. They live here and nowhere else: the
# page, the benchmark and the tests all consume this module's output instead of
# composing their own wording, which is what "one statistics function" means in
# practice.
SUMMARY_IDENTICAL = "正文逐段一致"
SUMMARY_CONTAINER = "差异仅来自版式或编码（归一化后逐字节一致）"
SUMMARY_SIGNATURE = "差一个署名"
COUNT_TEMPLATE = "仅差 %d 段"

# Labels that mark a line as an attribution ("署名") line rather than content.
# The list stays narrow on purpose: a false positive turns a real content change
# into a reassuring "差一个署名". Every entry names a person or a signature act;
# "编制单位" is deliberately not matched by "编制人", and a bare "审核意见"
# (a prose field) is not matched by "审核人".
SIGNATURE_MARKERS: Tuple[str, ...] = (
    "签署",
    "签名",
    "签字",
    "签发",
    "编制人",
    "编撰人",
    "审核人",
    "审批人",
    "批准人",
    "核准人",
    "负责人",
    "经办人",
    "承办人",
    "拟稿人",
    "联系人",
)

_SIGNATURE_LABEL_RE = re.compile(r"^([^：:]{1,24})[：:]\s*(.+)$")


def paragraphs(text: str) -> List[str]:
    """The comparison unit: non-empty stripped lines.

    Same unit the template test in :mod:`docdedupe.cluster` uses, so "a line" and
    "a paragraph" mean the same thing across the project. Blank lines are layout,
    not content, and stripping keeps a Markdown paragraph and its DOCX twin on
    the same footing.
    """
    return [line.strip() for line in text.splitlines() if line.strip()]


@dataclass(frozen=True)
class Segment:
    """One paragraph contribution to one side of the diff.

    ``spans`` are half-open ``(start, end)`` character ranges to highlight inside
    ``text``. They are computed here rather than in the page so the highlight
    rule has a single implementation; a test asserts that the page contains no
    highlighting logic of its own.
    """

    kind: str
    text: str
    spans: Tuple[Tuple[int, int], ...] = ()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "spans": [[start, end] for start, end in self.spans],
        }


@dataclass(frozen=True)
class DiffStats:
    """Paragraph counts of one alignment.

    ``differing`` is the number the summary sentence quotes: every added
    paragraph counts as one differing segment, every deleted one counts as one,
    and a replaced pair counts as two (one per side). ``data/README.md`` 4.10
    defines "仅差 X 段" that way, and a test asserts the sentence always quotes
    this exact number — a summary that disagrees with the blocks it summarizes is
    the drift this field exists to prevent.
    """

    same: int
    added: int
    deleted: int
    replaced: int

    @property
    def differing(self) -> int:
        return self.added + self.deleted + 2 * self.replaced

    @property
    def total_left(self) -> int:
        return self.same + self.deleted + self.replaced

    @property
    def total_right(self) -> int:
        return self.same + self.added + self.replaced

    def as_dict(self) -> dict:
        return {
            "same": self.same,
            "added": self.added,
            "deleted": self.deleted,
            "replaced": self.replaced,
            "differing": self.differing,
            "differing_paragraphs": self.differing,
        }


@dataclass(frozen=True)
class DocumentDiff:
    """The whole comparison of two documents."""

    left_segments: Tuple[Segment, ...]
    right_segments: Tuple[Segment, ...]
    stats: DiffStats
    summary: str
    identical: bool
    normalization_equivalent: bool
    signature_only: bool


# ---------------------------------------------------------------------------
# Character-level highlight
# ---------------------------------------------------------------------------


def _masked_runs(
    text: str, other_grams: set, n: int
) -> Tuple[Tuple[int, int], ...]:
    """Runs of characters whose covering n-grams are all absent from the other side.

    A character counts as changed only when *every* n-gram covering it is
    missing: one surviving n-gram means the character still has context in
    common. Runs are maximal, so a paragraph with two separate edits yields two
    spans instead of one span over everything between them.
    """
    spans: List[Tuple[int, int]] = []
    start: Optional[int] = None
    last_start = len(text) - n
    for index in range(len(text)):
        covering = [
            text[offset : offset + n]
            for offset in range(max(0, index - n + 1), min(index, last_start) + 1)
        ]
        changed = bool(covering) and all(gram not in other_grams for gram in covering)
        if changed and start is None:
            start = index
        elif not changed and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(text)))
    return tuple(spans)


def changed_spans(
    left: str, right: str, n: int = 3
) -> Tuple[Tuple[Tuple[int, int], ...], Tuple[Tuple[int, int], ...]]:
    """Character ranges to highlight on each side of a replaced paragraph.

    Falls back to the whole paragraph when the n-gram test cannot see the
    difference (a paragraph shorter than ``n`` has no n-grams at all, so
    ``李四`` vs ``王五`` highlights both names entirely rather than nothing).
    """
    if left == right:
        return (), ()
    left_spans = _masked_runs(left, fingerprint_mod.shingle_set(right, n), n)
    right_spans = _masked_runs(right, fingerprint_mod.shingle_set(left, n), n)
    if not left_spans:
        left_spans = ((0, len(left)),)
    if not right_spans:
        right_spans = ((0, len(right)),)
    return left_spans, right_spans


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def _align(
    left: Sequence[str], right: Sequence[str], n: int
) -> Tuple[List[Segment], List[Segment], DiffStats]:
    """Align two paragraph lists into two row-ordered segment lists.

    A ``replace`` block whose two sides have different lengths is decomposed:
    the paragraphs that pair up stay ``replace`` rows and the leftovers become
    ``del`` / ``add`` rows. That keeps every row one-sided-or-paired, which is
    what lets a client render both columns from two cursors.
    """
    left_segments: List[Segment] = []
    right_segments: List[Segment] = []
    counters = {"same": 0, "added": 0, "deleted": 0, "replaced": 0}

    matcher = difflib.SequenceMatcher(a=list(left), b=list(right), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                text = left[i1 + offset]
                left_segments.append(Segment(KIND_SAME, text))
                right_segments.append(Segment(KIND_SAME, text))
                counters["same"] += 1
            continue
        if tag == "delete":
            for index in range(i1, i2):
                text = left[index]
                left_segments.append(Segment(KIND_DEL, text, ((0, len(text)),)))
                counters["deleted"] += 1
            continue
        if tag == "insert":
            for index in range(j1, j2):
                text = right[index]
                right_segments.append(Segment(KIND_ADD, text, ((0, len(text)),)))
                counters["added"] += 1
            continue

        left_indexes = list(range(i1, i2))
        right_indexes = list(range(j1, j2))
        paired = min(len(left_indexes), len(right_indexes))
        for offset in range(paired):
            left_text = left[left_indexes[offset]]
            right_text = right[right_indexes[offset]]
            left_spans, right_spans = changed_spans(left_text, right_text, n)
            left_segments.append(Segment(KIND_REPLACE, left_text, left_spans))
            right_segments.append(Segment(KIND_REPLACE, right_text, right_spans))
            counters["replaced"] += 1
        for index in left_indexes[paired:]:
            text = left[index]
            left_segments.append(Segment(KIND_DEL, text, ((0, len(text)),)))
            counters["deleted"] += 1
        for index in right_indexes[paired:]:
            text = right[index]
            right_segments.append(Segment(KIND_ADD, text, ((0, len(text)),)))
            counters["added"] += 1

    return left_segments, right_segments, DiffStats(**counters)


# ---------------------------------------------------------------------------
# The summary sentence
# ---------------------------------------------------------------------------


def is_signature_line(text: str, settings: Optional[Settings] = None) -> bool:
    """True when ``text`` looks like an attribution line ("署名").

    Two conditions, both from the definition: a ``label：value`` shape whose
    label names a person or a signing act, and a length below
    ``signature_max_chars`` (``data/README.md`` 4.10: "单一短段落且长度低于配置
    阈值"). The label test is what keeps a rewritten body sentence that merely
    happens to be short from being reported as an attribution change.
    """
    cfg = settings or DEFAULT_SETTINGS
    if len(text) >= cfg.signature_max_chars:
        return False
    match = _SIGNATURE_LABEL_RE.match(text)
    if match is None:
        return False
    label = match.group(1)
    return any(marker in label for marker in SIGNATURE_MARKERS)


def _is_signature_only(
    stats: DiffStats,
    left_replacements: Sequence[str],
    right_replacements: Sequence[str],
    settings: Settings,
) -> bool:
    """True when the *only* difference is one short attribution paragraph.

    Requires a single replaced pair and nothing else — an added paragraph next to
    a changed signature is two differences, and calling it "差一个署名" would hide
    the insertion.
    """
    if stats.replaced != 1 or stats.added or stats.deleted:
        return False
    if len(left_replacements) != 1 or len(right_replacements) != 1:
        return False
    return is_signature_line(
        left_replacements[0], settings
    ) and is_signature_line(right_replacements[0], settings)


def summarize(
    stats: DiffStats,
    *,
    identical: bool,
    normalization_equivalent: bool,
    signature_only: bool,
) -> str:
    """The one sentence describing a difference (``data/README.md`` 4.10).

    This is the only place the sentences are produced. The page displays the
    string it is given; the B5 benchmark compares it with the truth, so a second
    implementation of the wording — or a second threshold — would be visible
    immediately rather than silently disagreeing with the report.

    Order matters. "identical" wins over everything: the paragraph lists are
    equal, so there is nothing to count. The container check comes next because a
    difference that normalization erases is not a content difference at all;
    reporting "仅差 9 段" for a full-width twin would contradict the ``exact``
    tier the same index reports for that cluster.
    """
    if identical or stats.differing == 0:
        return SUMMARY_IDENTICAL
    if normalization_equivalent:
        return SUMMARY_CONTAINER
    if signature_only:
        return SUMMARY_SIGNATURE
    return COUNT_TEMPLATE % stats.differing


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def diff_documents(
    left_text: str, right_text: str, settings: Optional[Settings] = None
) -> DocumentDiff:
    """Compare two extracted document bodies.

    ``left_text`` / ``right_text`` are ``documents.text`` values, not normalized
    text (see the module docstring). The comparison is symmetric in the sense
    that swapping the two arguments swaps the two columns and leaves the counts
    and the sentence unchanged.
    """
    cfg = settings or DEFAULT_SETTINGS
    left = paragraphs(left_text)
    right = paragraphs(right_text)

    left_segments, right_segments, stats = _align(left, right, cfg.shingle_size)
    identical = left == right
    normalization_equivalent = (
        not identical
        and normalize_mod.normalize(left_text, cfg) == normalize_mod.normalize(right_text, cfg)
    )
    signature_only = _is_signature_only(
        stats,
        [segment.text for segment in left_segments if segment.kind == KIND_REPLACE],
        [segment.text for segment in right_segments if segment.kind == KIND_REPLACE],
        cfg,
    )
    return DocumentDiff(
        left_segments=tuple(left_segments),
        right_segments=tuple(right_segments),
        stats=stats,
        summary=summarize(
            stats,
            identical=identical,
            normalization_equivalent=normalization_equivalent,
            signature_only=signature_only,
        ),
        identical=identical,
        normalization_equivalent=normalization_equivalent,
        signature_only=signature_only,
    )


__all__ = [
    "COUNT_TEMPLATE",
    "DiffStats",
    "DocumentDiff",
    "KIND_ADD",
    "KIND_DEL",
    "KIND_REPLACE",
    "KIND_SAME",
    "SEGMENT_KINDS",
    "SIGNATURE_MARKERS",
    "SUMMARY_CONTAINER",
    "SUMMARY_IDENTICAL",
    "SUMMARY_SIGNATURE",
    "Segment",
    "changed_spans",
    "diff_documents",
    "is_signature_line",
    "paragraphs",
    "summarize",
]
