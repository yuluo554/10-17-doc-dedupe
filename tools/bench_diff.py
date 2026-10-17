"""Benchmark B5: difference segments and the summary sentence, against the truth.

Run against an index produced by ``doc-dedupe scan``::

    python tools/bench_diff.py --db <index.db> --samples data/samples

B3 asks "were the duplicates found" and B4 asks "were they classified
correctly". B5 asks the question the review page exists to answer: **when the
page puts two documents side by side, does it say the right thing about them?**
The claim under test is the pair (difference segments, summary sentence).

The truth is derived here, from the corpus itself, without asking the module
under test anything:

* paragraph counts come from splitting the two extracted texts on line breaks
  (a three-line local helper, deliberately not ``diffing.paragraphs``);
* positions where the two texts differ are compared index by index;
* for ``fullwidth`` / ``header_footer`` members the truth is "the difference is
  container-level", which is checked with :func:`docdedupe.normalize.normalize`
  — the documented rule (``data/README.md`` 4.3) — and not with the diff engine.

The expectations come from the manifest's own ``variant`` labels, never from the
engine's output, so the benchmark cannot agree with the implementation by
construction. Each variant has a *shape* the diff must have:

===================  ============================================================
``original`` /        the two paragraph lists are equal; summary "正文逐段一致"
``reformat``
``fullwidth`` /       the raw texts differ but normalize to the same string;
``header_footer``     summary names the container difference, **not** a count
``truncate``          nothing was added, exactly as many paragraphs were deleted
                      as the paragraph-count difference says
``rewrite`` /         paragraph count unchanged, replacement rows only, one row
``template``          per position that genuinely differs
===================  ============================================================

"差一个署名" has no instance in the synthetic corpus: every rewritten paragraph
in it differs by a synonym, and the template families differ in several fields at
once. Rather than leave the sentence untested, this tool carries a handful of
crafted pairs for it, each with the wording ``data/README.md`` 4.10 prescribes —
including two near-misses (a ``编制单位`` attribution line and an over-long
attribution line) that must *not* be called a signature change.

``--inject-summary`` breaks the pipeline on purpose; the benchmark must then
fail. A gate that stays green under an injection is a fake gate.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from docdedupe import diffing
from docdedupe import normalize as normalize_mod
from docdedupe import store
from docdedupe.config import DEFAULT_SETTINGS, Settings

import bench_common

# One expectation per manifest ``variant`` label. Every label the manifest can
# produce must appear here: a new variant silently falling through to "whatever
# the engine said" is exactly the kind of hole B5 exists to close.
VARIANT_EXPECTATIONS: Dict[str, str] = {
    "original": "no-change",
    "reformat": "no-change",
    "fullwidth": "container-only",
    "header_footer": "container-only",
    "truncate": "deletion",
    "rewrite": "replacement",
    "template": "replacement",
    "noise": "negative",
    "encrypted": "skipped",
    "no_text_layer": "skipped",
}

COUNT_RE = re.compile(r"^仅差 (\d+) 段$")

# Crafted truth for the sentences the corpus does not contain. Each row is
# (expected summary, left text, right text, why). The two near-misses are the
# interesting ones: a short attribution line that is *not* a signature
# ("编制单位" names an organisation, not a person) and an attribution line that is
# too long to count as the "短段落" the definition asks for.
CRAFTED_CASES: Tuple[Tuple[str, str, str, str], ...] = (
    ("差一个署名", "签署人：张三", "签署人：王五", "single attribution paragraph"),
    (
        "差一个署名",
        "编制人：李四\n本阶段围绕总投资额开展估算。",
        "编制人：王五\n本阶段围绕总投资额开展估算。",
        "attribution differs inside an otherwise identical document",
    ),
    (
        "正文逐段一致",
        "签署人：张三",
        "签署人：张三",
        "identical bodies are not a signature difference",
    ),
    (
        "仅差 2 段",
        "编制单位：示例科技有限公司A",
        "编制单位：示例建设集团C",
        "an organisation line is not an attribution to a person",
    ),
    (
        "仅差 4 段",
        "签署人：张三\n本阶段围绕总投资额开展估算。",
        "签署人：王五\n现阶段围绕总投资额开展估算。",
        "an attribution change plus a body change is two differences",
    ),
)


def over_long_attribution(settings: Settings) -> Tuple[str, str]:
    """An attribution pair padded past ``signature_max_chars``.

    Grown from the configured bound rather than hard-coded: raising the bound
    would otherwise turn this boundary case into a test of nothing, and the case
    would keep passing while no longer crossing the line it is about.
    """
    filler = "，说明以工作日为准并顺延"
    left = "签署人：张三"
    right = "签署人：王五"
    while len(left) < settings.signature_max_chars:
        left += filler
        right += filler
    return left, right


def crafted_cases(settings: Settings) -> Tuple[Tuple[str, str, str, str], ...]:
    left, right = over_long_attribution(settings)
    return CRAFTED_CASES + (
        (
            "仅差 2 段",
            left,
            right,
            "an over-long attribution line exceeds the short-paragraph bound",
        ),
    )


# ---------------------------------------------------------------------------
# Truth, computed without the module under test
# ---------------------------------------------------------------------------


def naive_paragraphs(text: str) -> List[str]:
    """Local line splitter used for the truth side of every comparison."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def naive_truth(left_text: str, right_text: str) -> dict:
    """What the two bodies say about themselves, with no diff engine involved."""
    left = naive_paragraphs(left_text)
    right = naive_paragraphs(right_text)
    same_length = len(left) == len(right)
    changed_positions = (
        [index for index in range(len(left)) if left[index] != right[index]]
        if same_length
        else []
    )
    return {
        "equal": left == right,
        "left_count": len(left),
        "right_count": len(right),
        "removed": len(left) - len(right),
        "same_length": same_length,
        "changed_positions": changed_positions,
    }


def normalization_equivalent(left_text: str, right_text: str, settings: Settings) -> bool:
    """True when the documented normalization erases the difference.

    This is the truth behind the "container-only" cases, and it is read from
    :mod:`docdedupe.normalize` — the one implementation of ``data/README.md``
    4.3 — rather than inferred from the diff.
    """
    return normalize_mod.normalize(left_text, settings) == normalize_mod.normalize(
        right_text, settings
    )


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass
class PairOutcome:
    """One compared pair: what the engine said, what the truth says."""

    source: str
    variant: str
    expectation: str
    summary: str
    expected_summary: Optional[str]
    problems: Tuple[str, ...]
    truth: str

    @property
    def passed(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        if self.passed:
            return "PASS %-28s %-15s %s" % (self.source, self.expectation, self.summary)
        return "FAIL %-28s %-15s %s" % (
            self.source,
            self.expectation,
            "; ".join(self.problems),
        )


def _count_from_summary(summary: str) -> Optional[int]:
    match = COUNT_RE.match(summary)
    return None if match is None else int(match.group(1))


def _summaries_of(diff: diffing.DocumentDiff) -> Tuple[str, ...]:
    """Every sentence this module is allowed to produce (``data/README.md`` 4.10).

    "差一页" is deliberately absent: it needs a page boundary the extraction
    layer does not provide, so it is parked as an extension item instead of being
    guessed at. A summary outside this set means either a new sentence appeared
    without its definition, or a page-level claim was made anyway.
    """
    return (
        diffing.SUMMARY_IDENTICAL,
        diffing.SUMMARY_CONTAINER,
        diffing.SUMMARY_SIGNATURE,
    )


def evaluate_pair(
    source: str,
    variant: str,
    left_text: str,
    right_text: str,
    diff: diffing.DocumentDiff,
    settings: Settings,
) -> PairOutcome:
    """Apply the B5 gates to one pair."""
    expectation = VARIANT_EXPECTATIONS.get(variant, "unknown")
    truth = naive_truth(left_text, right_text)
    problems: List[str] = []

    # -- gate 0: the sentence is one the definitions allow, and it agrees with
    # the paragraph counts the payload carries. A summary whose number differs
    # from the blocks it summarizes is the drift this gate exists to catch.
    if diff.summary not in _summaries_of(diff) and _count_from_summary(diff.summary) is None:
        problems.append("summary %r is not one of the defined sentences" % diff.summary)
    quoted = _count_from_summary(diff.summary)
    if quoted is not None and quoted != diff.stats.differing:
        problems.append(
            "summary quotes %d differing segment(s) but the blocks hold %d"
            % (quoted, diff.stats.differing)
        )

    # -- gate 1: the segments cover both documents exactly, in order.
    left_joined = [segment.text for segment in diff.left_segments]
    right_joined = [segment.text for segment in diff.right_segments]
    if left_joined != naive_paragraphs(left_text):
        problems.append("left segments do not reproduce the left paragraphs in order")
    if right_joined != naive_paragraphs(right_text):
        problems.append("right segments do not reproduce the right paragraphs in order")

    expected_summary: Optional[str] = None

    if expectation == "no-change":
        if not truth["equal"]:
            problems.append("expected identical bodies, but they differ")
        expected_summary = diffing.SUMMARY_IDENTICAL
        if diff.summary != expected_summary:
            problems.append("summary %r, expected %r" % (diff.summary, expected_summary))
        if diff.stats.differing != 0:
            problems.append("expected no differing segments, got %d" % diff.stats.differing)

    elif expectation == "container-only":
        # Anti-vacuity: the raw texts must really differ, otherwise this case
        # would pass by being about identical documents instead of about
        # differences normalization is documented to erase.
        if truth["equal"]:
            problems.append("raw texts are identical; this case proves nothing")
        if not normalization_equivalent(left_text, right_text, settings):
            problems.append("normalization does not equalize these bodies")
        expected_summary = diffing.SUMMARY_CONTAINER
        if diff.summary != expected_summary:
            problems.append("summary %r, expected %r" % (diff.summary, expected_summary))
        if diff.stats.differing == 0:
            problems.append("raw texts differ, so the diff cannot be empty")

    elif expectation == "deletion":
        removed = truth["removed"]
        if removed <= 0:
            problems.append("expected the right side to be shorter, it is not")
        if diff.stats.added:
            problems.append("a truncation adds paragraphs, but %d were added" % diff.stats.added)
        if diff.stats.deleted != removed:
            problems.append(
                "deleted %d paragraph(s), the bodies differ by %d"
                % (diff.stats.deleted, removed)
            )
        if diff.stats.replaced:
            problems.append("a truncation replaces nothing, but %d pair(s) were replaced" % diff.stats.replaced)
        expected_summary = diffing.COUNT_TEMPLATE % removed
        if diff.summary != expected_summary:
            problems.append("summary %r, expected %r" % (diff.summary, expected_summary))
        _check_changed_positions(problems, diff, truth, expect_positions=False)

    elif expectation == "replacement":
        if not truth["same_length"]:
            problems.append(
                "expected the same paragraph count (%d vs %d)"
                % (truth["left_count"], truth["right_count"])
            )
        changed = truth["changed_positions"]
        if not changed:
            problems.append("no position differs; nothing to replace")
        if diff.stats.deleted + diff.stats.replaced != len(changed):
            problems.append(
                "left side has %d changed paragraph(s), the bodies differ at %d position(s)"
                % (diff.stats.deleted + diff.stats.replaced, len(changed))
            )
        if diff.stats.added + diff.stats.replaced != len(changed):
            problems.append(
                "right side has %d changed paragraph(s), the bodies differ at %d position(s)"
                % (diff.stats.added + diff.stats.replaced, len(changed))
            )
        expected_summary = diffing.COUNT_TEMPLATE % (2 * len(changed))
        if diff.summary != expected_summary:
            problems.append("summary %r, expected %r" % (diff.summary, expected_summary))
        _check_changed_positions(problems, diff, truth, expect_positions=True)

    elif expectation == "negative":
        if not truth["equal"]:
            problems.append("a negative sample must stand alone; its body changed")

    else:
        problems.append("no B5 expectation is defined for variant %r" % variant)

    return PairOutcome(
        source=source,
        variant=variant,
        expectation=expectation,
        summary=diff.summary,
        expected_summary=expected_summary,
        problems=tuple(problems),
        truth="paras %d/%d, differing positions %d, removed %d"
        % (
            truth["left_count"],
            truth["right_count"],
            len(truth["changed_positions"]),
            truth["removed"],
        ),
    )


def _check_changed_positions(
    problems: List[str],
    diff: diffing.DocumentDiff,
    truth: dict,
    *,
    expect_positions: bool,
) -> None:
    """The *segment numbers* the summary refers to must be the true positions.

    Segment order is paragraph order on each side, so the index of a segment is
    its paragraph number. For a same-length pair the set of non-``same``
    paragraph numbers must be exactly the set of positions that differ — a diff
    that highlights the wrong paragraphs would quote a plausible count while
    pointing the reviewer at innocent lines.
    """
    if not expect_positions or not truth["same_length"]:
        return
    found = sorted(
        index
        for index, segment in enumerate(diff.left_segments, start=1)
        if segment.kind != diffing.KIND_SAME
    )
    expected = [position + 1 for position in truth["changed_positions"]]
    if found != expected:
        problems.append(
            "changed paragraph numbers %s, the bodies differ at %s" % (found, expected)
        )


# ---------------------------------------------------------------------------
# Crafted cases
# ---------------------------------------------------------------------------


def evaluate_crafted(settings: Settings) -> List[PairOutcome]:
    outcomes = []
    for expected, left, right, why in crafted_cases(settings):
        diff = diffing.diff_documents(left, right, settings)
        problems = []
        if diff.summary != expected:
            problems.append("summary %r, expected %r" % (diff.summary, expected))
        # The over-long case is only meaningful while it really is over the
        # bound; assert the input still crosses it instead of trusting the label.
        is_over_long = max(len(naive_paragraphs(left)[0]), len(naive_paragraphs(right)[0])) >= (
            settings.signature_max_chars
        )
        if "over-long" in why and not is_over_long:
            problems.append("the case no longer exceeds signature_max_chars")
        outcomes.append(
            PairOutcome(
                source="crafted: %s" % why,
                variant="crafted",
                expectation="crafted",
                summary=diff.summary,
                expected_summary=expected,
                problems=tuple(problems),
                truth="%d -> %d paragraph(s)"
                % (len(naive_paragraphs(left)), len(naive_paragraphs(right))),
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------

INJECTIONS = ("none", "count-drift", "container-blind", "all-signature")


def _inject(diff: diffing.DocumentDiff, mode: str) -> diffing.DocumentDiff:
    """Damage one diff result, so the benchmark can be shown to notice.

    ``count-drift`` shifts the quoted number by one; ``container-blind`` reports
    a container-level difference as a paragraph count (the bug that would make a
    full-width twin look like a rewritten document); ``all-signature`` calls every
    non-identical pair an attribution change.
    """
    if mode == "none":
        return diff
    if mode not in INJECTIONS:
        raise ValueError("unknown summary injection %r" % mode)
    if mode == "count-drift":
        stats = dataclasses.replace(diff.stats, added=diff.stats.added + 1)
        return dataclasses.replace(diff, stats=stats, summary=diffing.COUNT_TEMPLATE % stats.differing)
    if mode == "container-blind":
        if diff.summary == diffing.SUMMARY_CONTAINER:
            return dataclasses.replace(
                diff, summary=diffing.COUNT_TEMPLATE % diff.stats.differing
            )
        return diff
    if diff.summary != diffing.SUMMARY_IDENTICAL:
        return dataclasses.replace(diff, summary=diffing.SUMMARY_SIGNATURE)
    return diff


# ---------------------------------------------------------------------------
# Driving the corpus
# ---------------------------------------------------------------------------


def load_groups(manifest_path: Path) -> List[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(manifest["groups"])


def index_texts(conn, samples_root: Path) -> Dict[str, str]:
    """Extracted text per normalized path, for every usable document."""
    rows = store.cluster_document_rows(conn)
    id_by_path = {str(row["path_norm"]): int(row["id"]) for row in rows}
    texts = store.document_texts(conn, sorted(id_by_path.values()))
    return {path: texts.get(doc_id, "") for path, doc_id in id_by_path.items()}


def _path_key(samples_root: Path):
    root_norm = store.normalize_path(str(samples_root.resolve()))

    def key(value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            path = samples_root / value
        return store.normalize_path(str(path))

    return key


@dataclass
class DiffReport:
    """Full B5 result."""

    outcomes: Tuple[PairOutcome, ...]
    coverage: Dict[str, int]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.passed)

    @property
    def failures(self) -> Tuple[PairOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.passed)

    def describe(self) -> List[str]:
        lines = [outcome.describe() for outcome in self.outcomes]
        lines.append(
            "coverage: %s"
            % ", ".join("%s %d" % (name, self.coverage[name]) for name in sorted(self.coverage))
        )
        lines.append(
            "pairs %d/%d = %.1f%% (segments + summary against the corpus truth and the crafted cases)"
            % (self.passed, self.total, 100.0 * (self.passed / self.total if self.total else 0.0))
        )
        # Stated out loud rather than implied: the third sentence of
        # data/README.md 4.10 is not implemented, because a page boundary is not
        # something the extraction layer produces.
        lines.append(
            "INFO 差一页 is not implemented (no page segmentation in the extraction layer); "
            "parked as an extension item in plan/03 section 6"
        )
        return lines


def run_benchmark(
    conn,
    samples_root: Path,
    *,
    settings: Optional[Settings] = None,
    inject_summary: str = "none",
) -> DiffReport:
    cfg = settings or DEFAULT_SETTINGS
    texts = index_texts(conn, samples_root)
    key = _path_key(samples_root)
    outcomes: List[PairOutcome] = []
    coverage: Dict[str, int] = {}

    for group in load_groups(samples_root / "truth" / "manifest.json"):
        base_text = texts.get(key(group["base"]), "")
        if not base_text:
            continue
        for member in group["members"]:
            file_key = key(member["file"])
            if file_key == key(group["base"]):
                continue
            variant = str(member.get("variant", ""))
            left_text = base_text
            right_text = texts.get(file_key, "")
            diff = diffing.diff_documents(left_text, right_text, cfg)
            outcome = evaluate_pair(
                source="%s %s" % (group["group_id"], Path(member["file"]).name),
                variant=variant,
                left_text=left_text,
                right_text=right_text,
                diff=_inject(diff, inject_summary),
                settings=cfg,
            )
            outcomes.append(outcome)
            coverage[outcome.expectation] = coverage.get(outcome.expectation, 0) + 1

    for outcome in evaluate_crafted(cfg):
        outcomes.append(outcome)
        coverage["crafted"] = coverage.get("crafted", 0) + 1

    return DiffReport(outcomes=tuple(outcomes), coverage=coverage)


def evaluate(report: DiffReport) -> Tuple[bool, List[str]]:
    """Apply the B5 gate and return the verdict plus the reasons it failed."""
    failures: List[str] = []
    if report.total == 0:
        failures.append("no pairs were compared; the gate would be vacuous")
    if report.failures:
        failures.append(
            "%d pair(s) disagreed with the truth: %s"
            % (
                len(report.failures),
                "; ".join(outcome.source for outcome in report.failures),
            )
        )
    # Every expectation shape must actually be exercised, otherwise a whole kind
    # of difference could stop being covered while the gate still reports green.
    for name in ("no-change", "container-only", "deletion", "replacement", "crafted"):
        if not report.coverage.get(name):
            failures.append("no pair exercised the %r expectation" % name)
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
        "--inject-summary",
        default="none",
        choices=INJECTIONS,
        help="damage the summary text to prove the benchmark can fail",
    )
    args = parser.parse_args(argv)

    samples = Path(args.samples).resolve()
    conn = store.connect(args.db)
    try:
        hint = bench_common.corpus_index_mismatch_hint(conn, samples)
        if hint:
            print("HINT: %s" % hint)
        report = run_benchmark(conn, samples, inject_summary=args.inject_summary)
    finally:
        conn.close()

    for line in report.describe():
        print(line)

    ok, failures = evaluate(report)
    if ok:
        print("B5 OK: every pair's segments and summary match the corpus truth")
        return 0
    for failure in failures:
        print("B5 FAIL: %s" % failure)
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
