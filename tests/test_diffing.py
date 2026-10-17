"""Module 4's difference engine: segments, character highlight, summary sentence.

Three things are under test here, and they are the three the review page depends
on:

1. **The segments reproduce both documents.** Concatenating one side's segments
   must give back that document's paragraphs, in order, with nothing dropped.
   Without this, a diff could look plausible while silently losing a paragraph.
2. **The changed paragraph numbers are the changed positions.** The reviewer is
   pointed at specific lines; highlighting the wrong line while quoting the right
   count is worse than showing nothing.
3. **The summary sentence follows the definitions once.** ``data/README.md`` 4.10
   defines the sentences, and this module is the only place they are composed —
   asserted by walking the source tree, the same way "normalization has one
   implementation" is asserted.

The ``差一页`` sentence is deliberately *not* implemented (no page segmentation
exists in the extraction layer) and the tests below pin that down: it must never
be emitted, and the downgrade has to stay visible rather than becoming an
unnoticed gap.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from docdedupe import diffing
from docdedupe.config import DEFAULT_SETTINGS

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "docdedupe"
WEB_STATIC = SRC / "web" / "static"

BODY_A = "示例项目甲 · 投资可行性研究\n编制单位：示例科技有限公司A\n本段正文用于构成足够长的比较内容。\n"
BODY_B = "示例项目甲 · 投资可行性研究\n编制单位：示例科技有限公司A\n本段正文用于构成足够长的比较内容，但这一句被改写了。\n"


def _python_sources():
    return sorted(path for path in SRC.rglob("*.py") if "__pycache__" not in path.parts)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def test_identical_bodies_report_no_difference():
    result = diffing.diff_documents(BODY_A, BODY_A)

    assert result.identical is True
    assert result.stats.differing == 0
    assert result.summary == diffing.SUMMARY_IDENTICAL
    assert all(segment.kind == diffing.KIND_SAME for segment in result.left_segments)


def test_container_level_difference_is_named_instead_of_counted():
    """A full-width twin differs in the raw text but not in the document.

    ``data/README.md`` 4.3 normalizes full-width forms away, which is why the
    same cluster is an ``exact`` duplicate. Counting those paragraphs as
    differences would make the page contradict the tier it is displaying.
    """
    left = "示例项目甲 · 投资可行性研究\n编号 HT-12860\n正文段落。\n"
    right = "示例项目甲 · 投资可行性研究\n编号 ＨＴ-１２８６０\n正文段落。\n"

    result = diffing.diff_documents(left, right)

    assert result.identical is False
    assert result.normalization_equivalent is True
    assert result.stats.differing > 0
    assert result.summary == diffing.SUMMARY_CONTAINER


def test_page_number_lines_are_a_container_difference_not_a_content_difference():
    """Page numbers are a normalization concern (``data/README.md`` 4.3 step 3)."""
    left = "正文第一段。\n正文第二段。\n"
    right = "正文第一段。\n第 1 页\n正文第二段。\n第 1 页\n"

    result = diffing.diff_documents(left, right)

    assert result.stats.added == 2
    assert result.stats.differing == 2
    assert result.summary == diffing.SUMMARY_CONTAINER


def test_running_head_difference_is_named_instead_of_counted():
    """A repeated short line is a running head (``data/README.md`` 4.3 step 4)."""
    left = "正文第一段。\n正文第二段。\n"
    right = (
        "示例科技有限公司A · 内部资料\n"
        "正文第一段。\n"
        "正文第二段。\n"
        "示例科技有限公司A · 内部资料\n"
    )

    result = diffing.diff_documents(left, right)

    assert result.stats.added == 2
    assert result.summary == diffing.SUMMARY_CONTAINER


def test_truncation_counts_deletions_only():
    left = "标题\n第一段。\n第二段。\n第三段。\n"
    right = "标题\n第一段。\n第二段。\n"

    result = diffing.diff_documents(left, right)

    assert result.stats.deleted == 1
    assert result.stats.added == 0
    assert result.stats.replaced == 0
    assert result.summary == diffing.COUNT_TEMPLATE % 1


def test_rewrite_counts_replacements_only():
    left = "标题\n本阶段围绕敏感性分析开展复核。\n结尾。\n"
    right = "标题\n现阶段围绕敏感性分析开展复核。\n结尾。\n"

    result = diffing.diff_documents(left, right)

    assert result.stats.same == 2
    assert result.stats.replaced == 1
    assert result.stats.added == 0 and result.stats.deleted == 0
    assert result.stats.differing == 2
    assert result.summary == "仅差 2 段"


def test_an_inserted_paragraph_is_counted_as_one_differing_segment():
    left = "标题\n第一段。\n"
    right = "标题\n新插入的一段。\n第一段。\n"

    result = diffing.diff_documents(left, right)

    assert result.stats.added == 1
    assert result.stats.deleted == 0
    assert result.summary == "仅差 1 段"


def test_segments_reproduce_both_documents_in_order():
    """Nothing may be dropped, reordered or invented by the alignment."""
    left = "标题\n只差这一段的版本甲。\n共同的结尾。\n"
    right = "标题\n以及一个额外段落。\n只差这一段的版本乙。\n共同的结尾。\n"

    result = diffing.diff_documents(left, right)

    assert [segment.text for segment in result.left_segments] == diffing.paragraphs(left)
    assert [segment.text for segment in result.right_segments] == diffing.paragraphs(right)
    assert result.stats.total_left == len(diffing.paragraphs(left))
    assert result.stats.total_right == len(diffing.paragraphs(right))


def test_changed_segment_numbers_are_the_positions_that_differ():
    left = "标题\n第一段保持不变。\n第二段被改写了。\n第三段保持不变。\n"
    right = "标题\n第一段保持不变。\n第二段换了个说法。\n第三段保持不变。\n"

    result = diffing.diff_documents(left, right)

    changed = [
        number
        for number, segment in enumerate(result.left_segments, start=1)
        if segment.kind != diffing.KIND_SAME
    ]
    assert changed == [3]
    assert result.summary == "仅差 2 段"


def test_a_replace_block_with_unequal_sides_pairs_what_it_can():
    """Two paragraphs replaced by one: one pair plus one deletion."""
    left = "标题\n版本甲第一段。\n版本甲第二段。\n结尾。\n"
    right = "标题\n版本乙第一段。\n结尾。\n"

    result = diffing.diff_documents(left, right)

    assert result.stats.deleted == 1
    assert result.stats.replaced == 1
    assert result.stats.added == 0
    assert [segment.text for segment in result.left_segments] == diffing.paragraphs(left)
    assert [segment.text for segment in result.right_segments] == diffing.paragraphs(right)


def test_the_quoted_count_always_matches_the_blocks_it_summarizes():
    """The sentence and the blocks come from the same numbers, by construction."""
    bodies = [
        (BODY_A, BODY_A),
        (BODY_A, BODY_B),
        ("标题\n第一段。\n", "标题\n第一段。\n第二段。\n"),
        ("标题\n第一段。\n第二段。\n", "标题\n第一段。\n"),
    ]
    for left, right in bodies:
        result = diffing.diff_documents(left, right)
        if result.summary.startswith("仅差"):
            assert result.summary == diffing.COUNT_TEMPLATE % result.stats.differing


# ---------------------------------------------------------------------------
# The signature sentence
# ---------------------------------------------------------------------------


def test_an_attribution_only_difference_gets_its_own_sentence():
    result = diffing.diff_documents("签署人：张三", "签署人：王五")

    assert result.signature_only is True
    assert result.stats.replaced == 1
    assert result.summary == diffing.SUMMARY_SIGNATURE


def test_an_attribution_change_inside_a_body_is_still_a_signature_change():
    left = "编制人：李四\n本阶段围绕总投资额开展估算。\n"
    right = "编制人：王五\n本阶段围绕总投资额开展估算。\n"

    result = diffing.diff_documents(left, right)

    assert result.summary == diffing.SUMMARY_SIGNATURE


@pytest.mark.parametrize(
    "left,right,reason",
    [
        (
            "编制单位：示例科技有限公司A",
            "编制单位：示例建设集团C",
            "an organisation line is not an attribution to a person",
        ),
        (
            "本阶段围绕总投资额开展估算。",
            "现阶段围绕总投资额开展估算。",
            "a rewritten sentence is not an attribution",
        ),
    ],
)
def test_short_line_that_is_not_a_signature_is_counted_normally(left, right, reason):
    result = diffing.diff_documents(left, right)

    assert result.summary == "仅差 2 段", reason


def test_a_long_attribution_line_is_not_called_short():
    filler = "，说明以工作日为准"
    left = "签署人：张三"
    right = "签署人：王五"
    while len(left) < DEFAULT_SETTINGS.signature_max_chars:
        left += filler
        right += filler

    result = diffing.diff_documents(left, right)

    assert result.summary == "仅差 2 段"


def test_a_signature_change_alongside_another_change_is_not_a_signature_change():
    """Two differences must not be reported as one: the other one would vanish."""
    left = "签署人：张三\n本阶段围绕总投资额开展估算。\n"
    right = "签署人：王五\n现阶段围绕总投资额开展估算。\n"

    result = diffing.diff_documents(left, right)

    assert result.signature_only is False
    assert result.summary == "仅差 4 段"


def test_an_inserted_paragraph_next_to_a_signature_is_not_a_signature_change():
    left = "签署人：张三\n正文。\n"
    right = "签署人：王五\n正文。\n新插入的段落。\n"

    result = diffing.diff_documents(left, right)

    assert result.summary != diffing.SUMMARY_SIGNATURE


def test_an_added_paragraph_alone_is_never_a_signature_change():
    result = diffing.diff_documents("正文。\n", "正文。\n签署人：王五\n")

    assert result.summary == "仅差 1 段"


# ---------------------------------------------------------------------------
# Character-level highlight
# ---------------------------------------------------------------------------


def test_character_highlight_hugs_the_edit():
    spans_left, spans_right = diffing.changed_spans(
        "本阶段围绕敏感性分析开展复核", "现阶段围绕敏感性分析开展复核"
    )

    assert spans_left == ((0, 1),)
    assert spans_right == ((0, 1),)


def test_character_highlight_separates_two_edits_in_one_paragraph():
    """One changed paragraph can hold several changed runs; each is its own span."""
    left = "本阶段围绕总投资额开展估算，按既定口径取值，并同步更新。"
    right = "现阶段围绕总投资额开展估算，依统一口径取值，并一并修订。"

    spans_left, spans_right = diffing.changed_spans(left, right)

    assert spans_left == ((0, 1), (14, 17), (23, 28))
    assert spans_right == ((0, 1), (14, 17), (23, 28))
    # The periods travel with the third edit: their surrounding n-grams are gone
    # too, and a highlight that stopped one character early would claim the
    # sentence end was unchanged.
    assert [left[start:end] for start, end in spans_left] == ["本", "按既定", "同步更新。"]
    assert [right[start:end] for start, end in spans_right] == ["现", "依统一", "一并修订。"]


def test_character_highlight_falls_back_to_the_whole_paragraph_when_too_short():
    """A paragraph shorter than the shingle size has no n-grams to compare."""
    spans_left, spans_right = diffing.changed_spans("李四", "王五")

    assert spans_left == ((0, 2),)
    assert spans_right == ((0, 2),)


def test_character_highlight_is_empty_for_identical_text():
    assert diffing.changed_spans("同样的正文。", "同样的正文。") == ((), ())


def test_spans_stay_inside_the_text_and_are_ordered():
    left = "本阶段围绕总投资额开展估算，按既定口径取值。"
    right = "现阶段围绕总投资额开展估算，依统一口径取值。"
    spans_left, spans_right = diffing.changed_spans(left, right)

    for text, spans in ((left, spans_left), (right, spans_right)):
        assert spans, "a difference must produce at least one span"
        previous_end = -1
        for start, end in spans:
            assert 0 <= start < end <= len(text)
            assert start >= previous_end
            previous_end = end


def test_every_changed_paragraph_carries_highlight_spans():
    result = diffing.diff_documents(BODY_A, BODY_B)

    for segment in result.left_segments + result.right_segments:
        if segment.kind == diffing.KIND_SAME:
            assert segment.spans == ()
        elif segment.kind == diffing.KIND_REPLACE:
            assert segment.spans
        else:
            # A whole added/deleted paragraph is the difference.
            assert segment.spans == ((0, len(segment.text)),)


def test_segments_serialize_with_spans_as_pairs():
    result = diffing.diff_documents("标题\n本阶段围绕敏感性分析开展复核。\n", "标题\n现阶段围绕敏感性分析开展复核。\n")
    payload = [segment.as_dict() for segment in result.left_segments]

    assert payload[1]["kind"] == diffing.KIND_REPLACE
    assert payload[1]["spans"] == [[0, 1]]


# ---------------------------------------------------------------------------
# Determinism and symmetry
# ---------------------------------------------------------------------------


def test_the_same_input_produces_the_same_diff():
    first = diffing.diff_documents(BODY_A, BODY_B)
    second = diffing.diff_documents(BODY_A, BODY_B)

    assert first == second


def test_swapping_the_sides_keeps_the_counts_and_the_sentence():
    forward = diffing.diff_documents(BODY_A, BODY_B)
    backward = diffing.diff_documents(BODY_B, BODY_A)

    assert forward.summary == backward.summary
    assert forward.stats == backward.stats
    assert forward.left_segments == backward.right_segments
    assert forward.right_segments == backward.left_segments


# ---------------------------------------------------------------------------
# One implementation, and the deliberate downgrade
# ---------------------------------------------------------------------------


def test_the_summary_sentences_are_composed_in_exactly_one_module():
    """The page shows the sentence it is given; it must not compose its own.

    Checked by looking for the wording across the package, so a second
    implementation cannot appear quietly in a later edit — the same guard style
    the project uses for the normalization rules.
    """
    sentences = (
        diffing.SUMMARY_IDENTICAL,
        diffing.SUMMARY_CONTAINER,
        diffing.SUMMARY_SIGNATURE,
        diffing.COUNT_TEMPLATE % 1,
    )
    holders = {}
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for sentence in sentences:
            if sentence in text:
                holders.setdefault(sentence, []).append(path.relative_to(REPO_ROOT).as_posix())

    for sentence, found in holders.items():
        assert found == ["src/docdedupe/diffing.py"], "%r appears in %s" % (sentence, found)


def test_the_page_contains_no_summary_wording_of_its_own():
    html = (WEB_STATIC / "index.html").read_text(encoding="utf-8")

    for sentence in (
        diffing.SUMMARY_IDENTICAL,
        diffing.SUMMARY_CONTAINER,
        diffing.SUMMARY_SIGNATURE,
        "仅差",
        "差一页",
        "signature_max_chars",
    ):
        assert sentence not in html, "the page composes its own wording: %r" % sentence


def test_the_page_contains_no_highlighting_logic_of_its_own():
    """Spans arrive precomputed; a page that rebuilt them could disagree."""
    html = (WEB_STATIC / "index.html").read_text(encoding="utf-8")

    assert "shingle" not in html
    assert "jaccard" not in html


def test_no_page_level_claim_can_be_made():
    """``差一页`` is not implemented, so no sentence may claim it.

    The extraction layer yields one text stream with no page boundary, so a
    page-level threshold would be a made-up number. The sentence is parked as an
    extension item instead, and the B5 benchmark prints that downgrade on every
    run. These assertions fail the moment the decision is undone without a
    definition to undo it with.
    """
    sentences = (
        diffing.SUMMARY_IDENTICAL,
        diffing.SUMMARY_CONTAINER,
        diffing.SUMMARY_SIGNATURE,
        diffing.COUNT_TEMPLATE,
    )
    assert not any("页" in sentence for sentence in sentences)
    # And there is no page threshold sitting in the settings waiting to be used.
    from docdedupe.config import Settings

    page_fields = [name for name in Settings.__dataclass_fields__ if "page" in name]
    assert page_fields == []

    bodies = [
        (BODY_A, BODY_A),
        (BODY_A, BODY_B),
        ("签署人：张三", "签署人：王五"),
        ("第 1 页\n正文。\n第 2 页\n更多正文。\n", "第 1 页\n正文。\n"),
    ]
    for left, right in bodies:
        summary = diffing.diff_documents(left, right).summary
        assert "页" not in summary, "a page-level claim was made: %r" % summary


def test_only_the_diff_module_decides_paragraph_equality_for_reporting():
    """``diffing`` must not grow its own copies of other modules' rules.

    The template test, the tier thresholds and the scoring formula belong to
    ``cluster.py``; the diff module reads text and counts paragraphs. This walks
    the AST for the call targets that would mean it had started deciding things.
    """
    tree = ast.parse((SRC / "diffing.py").read_text(encoding="utf-8"), filename="diffing.py")
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)

    for forbidden in (
        "build_clusters",
        "clusters_from_index",
        "template_evidence",
        "shingle_hashes",
        "jaccard_from_hashes",
        "near_duplicate_threshold",
    ):
        assert forbidden not in called, "diffing.py calls %r" % forbidden
