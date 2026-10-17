"""Benchmark B5: difference segments and summaries against the truth, plus injections.

B3 proves the duplicates were found and B4 that they were classified correctly.
B5 proves the review page will *say the right thing* about them: the changed
paragraph numbers and the one-sentence summary.

The injection tests at the bottom are the point of the file. Each one damages the
summary in a way a plausible bug would — an off-by-one in the count, a
container-level difference reported as a paragraph count, every difference called
an attribution change — and asserts the benchmark notices.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from docdedupe import scanner, store
from docdedupe.config import DEFAULT_SETTINGS

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
MANIFEST = SAMPLES / "truth" / "manifest.json"

sys.path.insert(0, str(REPO_ROOT / "tools"))

import bench_diff  # noqa: E402

pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(), reason="sample corpus is not generated"
)


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("b5") / "index.db"
    conn = store.connect(str(db_path))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
        yield conn
    finally:
        conn.close()


def _report(index, **kwargs):
    return bench_diff.run_benchmark(index, SAMPLES, **kwargs)


# ---------------------------------------------------------------------------
# The gate holds
# ---------------------------------------------------------------------------


def test_every_pair_matches_the_corpus_truth(index):
    report = _report(index)
    failing = [outcome.describe() for outcome in report.failures]

    assert failing == [], "\n".join(failing)
    assert report.passed == report.total
    assert report.total == 63


def test_the_gate_exercises_every_kind_of_difference(index):
    """A shape that stops being covered would leave a class of bugs unguarded."""
    coverage = _report(index).coverage

    for shape in ("no-change", "container-only", "deletion", "replacement", "crafted"):
        assert coverage.get(shape), "no pair exercised %r" % shape


def test_the_summary_never_makes_a_page_level_claim(index):
    """``差一页`` is not implemented; nothing may claim it, and the gap is printed."""
    report = _report(index)

    for outcome in report.outcomes:
        assert "页" not in outcome.summary
    assert any("差一页" in line and "INFO" in line for line in report.describe())


def test_ok_verdict_is_emitted_for_the_good_run(index):
    report = _report(index)
    ok, failures = bench_diff.evaluate(report)

    assert ok is True
    assert failures == []


def test_the_crafted_cases_cover_the_attribution_sentence():
    """The corpus has no attribution-only pair, so the sentence is covered here."""
    from docdedupe import diffing

    outcomes = bench_diff.evaluate_crafted(DEFAULT_SETTINGS)
    summaries = [outcome.summary for outcome in outcomes]

    assert summaries.count(diffing.SUMMARY_SIGNATURE) >= 2
    assert any(outcome.expected_summary == diffing.SUMMARY_IDENTICAL for outcome in outcomes)
    assert all(outcome.passed for outcome in outcomes), [
        outcome.describe() for outcome in outcomes if not outcome.passed
    ]


def test_the_over_long_case_still_crosses_the_configured_bound():
    """The boundary case must not quietly stop being about the boundary."""
    left, right = bench_diff.over_long_attribution(DEFAULT_SETTINGS)

    assert len(left) >= DEFAULT_SETTINGS.signature_max_chars
    assert len(right) >= DEFAULT_SETTINGS.signature_max_chars
    assert left != right


def test_a_tighter_boundary_makes_the_over_long_case_shorter():
    """The case is grown from the setting, so it tracks the setting."""
    narrow = DEFAULT_SETTINGS.with_overrides(signature_max_chars=20)
    left, _ = bench_diff.over_long_attribution(narrow)

    assert len(left) < len(bench_diff.over_long_attribution(DEFAULT_SETTINGS)[0])


# ---------------------------------------------------------------------------
# Fault injection: the benchmark must fail when the summary is wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["count-drift", "container-blind", "all-signature"])
def test_each_injection_makes_the_benchmark_fail(index, mode):
    report = _report(index, inject_summary=mode)
    ok, failures = bench_diff.evaluate(report)

    assert ok is False, "injection %r was not caught" % mode
    assert report.failures, "injection %r left every pair passing" % mode
    assert any("disagreed with the truth" in failure for failure in failures)


def test_the_injections_fail_for_the_expected_reason(index):
    """A count off by one must be caught by the count gates, not by luck."""
    report = _report(index, inject_summary="count-drift")
    wrong = [outcome for outcome in report.failures if outcome.expected_summary]

    assert wrong, "no expectation-bearing pair failed under count-drift"
    # Every failing pair names the summary it expected: the drift is caught by
    # the sentence comparison, not by some unrelated gate that happened to fire.
    for outcome in wrong:
        assert any("summary" in problem for problem in outcome.problems), outcome.describe()


def test_container_blind_injection_only_breaks_the_container_pairs(index):
    """The injection is targeted, so the failures name the pairs it targeted."""
    report = _report(index, inject_summary="container-blind")
    failing = {outcome.source for outcome in report.failures}

    assert failing == {"G010 G010_b.txt", "G010 G010_c.docx", "G011 G011_b.pdf"}


def test_an_unknown_injection_is_rejected():
    with pytest.raises(ValueError):
        bench_diff._inject(None, "no-such-injection")


def test_the_truth_side_does_not_import_the_module_under_test():
    """The expectations must not be derived from the engine's own behaviour.

    Checked statically: the tool may use ``diffing`` to *produce* a result, but
    its truth helpers must not call into it. A truth function that imported the
    engine would make the benchmark agree with it by construction.
    """
    import ast

    tree = ast.parse(
        (REPO_ROOT / "tools" / "bench_diff.py").read_text(encoding="utf-8"),
        filename="bench_diff.py",
    )
    truth_functions = {"naive_paragraphs", "naive_truth"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in truth_functions:
            called = {
                child.func.attr if isinstance(child.func, ast.Attribute) else getattr(child.func, "id", "")
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
            }
            assert not any(name.startswith("diff") for name in called), (
                "%s calls into the engine: %s" % (node.name, called)
            )
