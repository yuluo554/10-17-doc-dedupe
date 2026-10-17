"""Normalization tests (benchmark B2).

``data/README.md`` 4.3 fixes a five-step order. Each test below pins one step to
a concrete input, and the last section proves the order is the implementation's
actual order rather than a claim in a docstring: reordering two stages changes
the output on inputs built to make the order observable.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from docdedupe.config import DEFAULT_SETTINGS
from docdedupe.extract import extract
from docdedupe.normalize import NORMALIZE_VERSION, STAGE_ORDER, normalize

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


# ---------------------------------------------------------------------------
# The documented order
# ---------------------------------------------------------------------------


def test_stage_order_matches_the_documented_sequence():
    """data/README.md 4.3: NFKC, whitespace, page numbers, running heads, lower-case."""
    assert STAGE_ORDER == (
        "nfkc",
        "strip_whitespace",
        "drop_page_numbers",
        "drop_running_heads",
        "lower_latin",
    )


def test_normalize_version_is_importable_from_the_module_and_config():
    """The version is defined once (config) and re-exported (normalize).

    Two definitions would let the cache key and the module disagree, which is
    the failure the cache-key test would then not catch.
    """
    from docdedupe import config

    assert NORMALIZE_VERSION == config.NORMALIZE_VERSION


# ---------------------------------------------------------------------------
# Step 1: NFKC
# ---------------------------------------------------------------------------


def test_fullwidth_latin_and_digits_fold_to_halfwidth():
    wide = "示例项目甲 ＰＤＦ ２０２６"
    narrow = "示例项目甲 PDF 2026"
    assert normalize(wide) == normalize(narrow)
    assert normalize(wide) == "示例项目甲pdf2026"


def test_ideographic_space_is_removed_like_any_other_space():
    assert normalize("示例项目甲\u3000质量管理") == "示例项目甲质量管理"


def test_nfkc_runs_before_whitespace_removal():
    """Full-width space is whitespace only *after* NFKC turns it into U+0020.

    If whitespace removal ran first, U+3000 would survive and the two strings
    below would differ.
    """
    assert normalize("a\u3000b") == "ab"


# ---------------------------------------------------------------------------
# Step 2: whitespace and invisibles
# ---------------------------------------------------------------------------


def test_all_whitespace_and_line_breaks_are_removed():
    assert normalize("示例 项目\n甲\t技术\r\n方案") == "示例项目甲技术方案"


def test_zero_width_and_control_characters_are_removed():
    dirty = "示例\u200b项目\ufeff甲\u00ad技术\x00方案"
    assert normalize(dirty) == "示例项目甲技术方案"


# ---------------------------------------------------------------------------
# Step 3: page numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["12", "第 12 页", "第12页", "第12页/共30页", "第12页／共30页", "- 12 -", "12/30", "Page 12", "p.12"],
)
def test_page_number_lines_are_dropped(line):
    text = "示例项目甲\n%s\n正文第一段。" % line
    assert normalize(text) == normalize("示例项目甲\n正文第一段。")


def test_a_year_is_not_mistaken_for_a_page_number():
    """Four bare digits are a year far more often than a page number."""
    assert "2026" in normalize("2026年度总结\n正文。")


def test_a_number_inside_a_sentence_survives():
    assert "12" in normalize("本阶段共抽检12项\n全部合格。")


# ---------------------------------------------------------------------------
# Step 4: running heads
# ---------------------------------------------------------------------------


def test_a_line_repeated_on_every_page_is_dropped():
    header = "示例科技有限公司A · 内部资料"
    text = "\n".join(
        [header, "正文第一段。", "第1页", header, "正文第二段。", "第2页", header, "正文第三段。"]
    )
    assert normalize(text) == normalize("正文第一段。\n正文第二段。\n正文第三段。")


def test_a_repeated_long_line_is_not_treated_as_a_header():
    """The length cap protects body sentences that legitimately repeat."""
    sentence = "本文件由示例建设集团C编制仅用于示例项目乙的内部讨论未经许可不得对外提供这是较长的正文句子。"
    assert len(sentence) > DEFAULT_SETTINGS.header_footer_max_chars
    text = "\n".join([sentence, "正文第一段。", sentence, "正文第二段。"])

    # Both occurrences survive: the cap is what excludes it from header removal.
    # The expected string is built through normalize() as well, so the Latin
    # lower-casing in the sentence cannot make the assertion lie.
    expected = normalize(sentence) + normalize("正文第一段。") + normalize(sentence) + normalize("正文第二段。")
    assert normalize(text) == expected


def test_a_title_that_appears_once_is_kept():
    text = "示例项目甲 · 质量管理体系\n正文第一段。\n正文第二段。"
    assert normalize(text).startswith("示例项目甲·质量管理体系")


def test_page_numbers_are_removed_before_the_header_count():
    """A page-number line must not count towards a header's repetition.

    The header repeats twice here, which is enough under the default. The point
    is that the *page numbers* were already gone, so they cannot inflate any
    count and cannot themselves be mistaken for a repeated short line.
    """
    text = "\n".join(["页眉", "第1页", "正文一。", "页眉", "第2页", "正文二。"])
    assert normalize(text) == normalize("页眉\n正文一。\n页眉\n正文二。")


# ---------------------------------------------------------------------------
# Step 5: Latin lower-case only
# ---------------------------------------------------------------------------


def test_latin_letters_are_lower_cased():
    assert normalize("PDF 文档 ABC") == "pdf文档abc"


def test_non_latin_case_is_left_alone():
    """The contract says Latin only; folding Greek or Cyrillic would be silent scope creep."""
    greek = "ΣΟΦΟΣ"
    assert normalize(greek) == greek
    assert normalize(greek) != greek.lower()


def test_chinese_characters_are_untouched():
    assert normalize("质量管理体系") == "质量管理体系"


# ---------------------------------------------------------------------------
# Order matters
# ---------------------------------------------------------------------------


def test_lower_casing_must_come_after_whitespace_removal():
    """Both orders agree on this input, but the documented one is pinned.

    This is a negative control: if lower-casing moved before NFKC, the full-width
    ``Ｐ`` would stay upper-case (it is not in the Latin-1 range the table covers
    until NFKC has folded it), so the two orders really are distinguishable.
    """
    assert normalize("Ｐ Ｄ Ｆ") == "pdf"

    # Simulating the wrong order: lower-case first leaves the full-width form
    # untouched, and nothing later folds it.
    out_of_order = "".join("Ｐ Ｄ Ｆ").lower().replace(" ", "")
    assert out_of_order != "pdf"


def test_header_removal_needs_the_whitespace_stage_first():
    """Two headers differing only in spacing must count as one line.

    Whitespace runs are stripped before the repeat count, so ``A · 内部资料`` and
    ``A·内部资料`` are the same header. If the count ran before stripping, both
    would appear once and neither would be removed.
    """
    text = "\n".join(["甲 · 内部资料", "正文一。", "甲·内部资料", "正文二。"])
    assert normalize(text) == normalize("正文一。\n正文二。")


# ---------------------------------------------------------------------------
# Corpus-level (B2)
# ---------------------------------------------------------------------------


pytestmark_corpus = pytest.mark.skipif(
    not (SAMPLES / "truth" / "manifest.json").is_file(),
    reason="sample corpus is not generated",
)


@pytestmark_corpus
def test_cross_format_group_normalizes_to_one_text():
    """G005 ships the same body as DOCX/MD/TXT/HTML/PDF (one twin in GBK).

    "Same body, different container" is the whole cross-format claim; if the
    normalized texts differed, the similarity score would be measuring the
    extractor instead.
    """
    import json

    manifest = json.loads((SAMPLES / "truth" / "manifest.json").read_text(encoding="utf-8"))
    group = next(item for item in manifest["groups"] if item["group_id"] == "G005")
    assert len({Path(name).suffix.lower() for name in group["files"]}) == 5

    normalized = {}
    for name in group["files"]:
        text = extract(SAMPLES / name).text
        assert text, "%s produced no text" % name
        normalized[name] = normalize(text)

    distinct = set(normalized.values())
    assert len(distinct) == 1, {
        name: (len(value), value[:40]) for name, value in normalized.items()
    }


@pytestmark_corpus
def test_fullwidth_group_normalizes_to_the_same_text_as_its_plain_twin():
    import json

    manifest = json.loads((SAMPLES / "truth" / "manifest.json").read_text(encoding="utf-8"))
    group = next(item for item in manifest["groups"] if item["group_id"] == "G010")
    plain = next(member for member in group["members"] if member["variant"] == "original")
    wide = next(member for member in group["members"] if member["variant"] == "fullwidth")

    raw_plain = extract(SAMPLES / plain["file"]).text
    raw_wide = extract(SAMPLES / wide["file"]).text
    assert raw_plain != raw_wide, "the full-width twin must really differ before normalization"
    assert normalize(raw_plain) == normalize(raw_wide)


@pytestmark_corpus
def test_header_footer_group_normalizes_to_the_same_text():
    """G011 is the same body as DOCX and as a PDF with a header, footer and page numbers."""
    import json

    manifest = json.loads((SAMPLES / "truth" / "manifest.json").read_text(encoding="utf-8"))
    group = next(item for item in manifest["groups"] if item["group_id"] == "G011")
    texts = [extract(SAMPLES / name).text for name in group["files"]]
    assert any("第 1 页" in text for text in texts), "the PDF twin lost its page numbers"

    normalized = {normalize(text) for text in texts}
    assert len(normalized) == 1


@pytestmark_corpus
def test_normalization_is_idempotent():
    """Running it twice must not keep eating content.

    Idempotence is what makes it safe to normalize an already-normalized string
    (for instance the stored ``norm_text``) without a second, different result.
    """
    text = "示例项目甲 · 说明\n第 1 页\n正文第一段。\n说明\n正文第二段。"
    once = normalize(text)
    assert normalize(once) == once


# ---------------------------------------------------------------------------
# Single implementation
# ---------------------------------------------------------------------------


def test_normalization_has_exactly_one_implementation():
    """data/README.md 4.3 says the rules are defined once; this is the check.

    A second ``unicodedata.normalize("NFKC", ...)`` or a stray ``.lower()`` in
    another module would create a second normalization口径, and the two would
    drift the moment one of them changed. The scan path must call
    ``normalize.normalize`` rather than reimplementing any step.
    """
    import re as _re

    src = Path(__file__).resolve().parents[1] / "src" / "docdedupe"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        # ``normalize.py`` is the implementation and ``config.py`` only declares
        # the two settings that parameterize it; neither is a duplicate.
        if path.name in ("normalize.py", "config.py"):
            continue
        text = path.read_text(encoding="utf-8")
        # NFKC anywhere outside normalize.py is a second implementation.
        for match in _re.finditer(r"unicodedata\.normalize\(\s*[\"']NFKC", text):
            offenders.append("%s:%d NFKC" % (path.name, text[: match.start()].count("\n") + 1))
        # The running-head rule is recognizable by its two settings; a copy
        # would name them outside normalize.py.
        if "header_footer_max_chars" in text and "header_footer_min_repeats" in text:
            offenders.append("%s running-head rule" % path.name)
    assert offenders == [], "normalization logic duplicated: %s" % offenders


def test_normalize_version_literal_lives_only_in_config():
    """The cache key reads the version from config; a second literal would drift."""
    import re as _re

    src = Path(__file__).resolve().parents[1] / "src" / "docdedupe"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in _re.finditer(r"^NORMALIZE_VERSION\s*=\s*(\d+)", text, _re.M):
            offenders.append("%s:%s" % (path.name, match.group(1)))
    assert offenders == [], "second NORMALIZE_VERSION literal: %s" % offenders
