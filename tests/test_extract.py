"""Extraction tests: every supported format, plus the three skip outcomes.

The skip tests matter more than the happy path. A scanner that returns an empty
string for an unreadable file is worse than one that crashes: the file silently
drops out of the comparison and the report claims "no duplicates" while a whole
class of documents was never looked at.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from docdedupe import store
from docdedupe.extract import detect_format, extract, sniff_supported


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import docxutil  # noqa: E402
import pdfutil  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"

pytestmark = pytest.mark.skipif(not SAMPLES.is_dir(), reason="sample corpus is not generated")


# ---------------------------------------------------------------------------
# Supported formats
# ---------------------------------------------------------------------------


def _text_pdf(*pages):
    return pdfutil.build_text_pdf(pages or [["示例项目甲 可行性研究报告", "正文第一段。"]])


def test_extracts_markdown(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("# 示例项目甲\n\n正文第一段。\n", encoding="utf-8")
    result = extract(path)
    assert result.status == store.STATUS_OK
    assert "示例项目甲" in result.text
    assert result.extractor == "markdown@1"


def test_extracts_plain_text(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("示例项目甲\n正文第一段。\n", encoding="utf-8")
    result = extract(path)
    assert result.status == store.STATUS_OK
    assert "正文第一段" in result.text


def test_extracts_utf8_bom_text_without_leaving_the_bom(tmp_path):
    path = tmp_path / "bom.txt"
    path.write_bytes("\ufeff示例项目甲\n正文。\n".encode("utf-8"))
    result = extract(path)
    assert result.status == store.STATUS_OK
    assert not result.text.startswith("\ufeff")


def test_extracts_gbk_text(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("示例项目甲\n正文第一段。\n".encode("gbk"))
    result = extract(path)
    assert result.status == store.STATUS_OK
    assert "正文第一段" in result.text


def test_extracts_html_without_markup_or_head_content(tmp_path):
    path = tmp_path / "a.html"
    path.write_text(
        "<!DOCTYPE html><html><head><title>不应出现</title>"
        "<script>var x = 1;</script></head>"
        "<body><h1>示例项目甲</h1><p>正文第一段。</p></body></html>",
        encoding="utf-8",
    )
    result = extract(path)
    assert result.status == store.STATUS_OK
    assert "示例项目甲" in result.text
    assert "正文第一段" in result.text
    assert "<h1>" not in result.text
    assert "不应出现" not in result.text
    assert "var x" not in result.text
    assert result.extractor == "html@1"


def test_extracts_docx_paragraphs_and_tables(tmp_path):
    from docx import Document

    document = Document()
    docxutil.pin_core_properties(document)
    document.add_paragraph("示例项目甲 技术方案")
    document.add_paragraph("")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "合同价款"
    table.cell(0, 1).text = "1234.56 万元"
    table.cell(1, 0).text = "履约期限"
    table.cell(1, 1).text = "18 个月"
    path = tmp_path / "a.docx"
    docxutil.save_docx(document, path)

    result = extract(path)
    assert result.status == store.STATUS_OK
    assert "示例项目甲" in result.text
    # Table cells must reach the text; a DOCX whose body is all table would
    # otherwise look like an empty document.
    assert "合同价款" in result.text
    assert "1234.56" in result.text
    assert result.extractor == "python-docx@1"


def test_extracts_chinese_text_from_pdf(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(_text_pdf(["示例项目甲 可行性研究报告", "本报告由示例科技有限公司A 编制。"]))
    result = extract(path)
    assert result.status == store.STATUS_OK
    assert "示例项目甲" in result.text
    assert "示例科技有限公司A" in result.text
    assert result.extractor == "pypdf@1"


def test_extracts_multipage_pdf_in_page_order(tmp_path):
    path = tmp_path / "multi.pdf"
    path.write_bytes(
        pdfutil.build_text_pdf([["第一页正文"], ["第二页正文"], ["第三页正文"]])
    )
    text = extract(path).text
    assert text.index("第一页") < text.index("第二页") < text.index("第三页")


# ---------------------------------------------------------------------------
# Skip outcomes
# ---------------------------------------------------------------------------


def test_pdf_without_text_layer_is_skipped_not_empty(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(pdfutil.build_no_text_pdf(1))
    result = extract(path)
    assert result.status == store.STATUS_NO_TEXT_LAYER
    assert result.text == ""
    assert result.extractor == ""


def test_encrypted_pdf_is_skipped_and_does_not_leak_text(tmp_path):
    path = tmp_path / "locked.pdf"
    path.write_bytes(pdfutil.build_encrypted_pdf([["机密正文", "不应被读出"]]))
    result = extract(path)
    assert result.status == store.STATUS_ENCRYPTED
    assert result.text == ""
    # Without this the "encrypted" path could pass while the text leaked in.
    assert "机密正文" not in result.text


def test_pdf_with_empty_user_password_is_read_not_skipped(tmp_path):
    """An empty user password decrypts transparently; it is not a locked file."""
    from pypdf import PdfWriter

    source = tmp_path / "open.pdf"
    source.write_bytes(_text_pdf(["可以正常读取的正文"]))
    encrypted = tmp_path / "empty_pw.pdf"
    writer = PdfWriter(clone_from=str(source))
    writer.encrypt(user_password="", owner_password="owner")
    with open(str(encrypted), "wb") as handle:
        writer.write(handle)

    result = extract(encrypted)
    assert result.status == store.STATUS_OK
    assert "可以正常读取的正文" in result.text


def test_unknown_extension_is_unsupported(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"not a document at all")
    assert extract(path).status == store.STATUS_UNSUPPORTED


def test_legacy_doc_is_unsupported_not_a_crash(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(600))
    assert extract(path).status == store.STATUS_UNSUPPORTED


def test_pdf_extension_with_non_pdf_magic_is_unsupported(tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_bytes("这只是普通文本，不是 PDF。\n".encode("utf-8"))
    result = extract(path)
    assert result.status == store.STATUS_UNSUPPORTED


def test_missing_file_reports_read_error(tmp_path):
    result = extract(tmp_path / "does-not-exist.txt")
    assert result.status == store.STATUS_READ_ERROR


def test_empty_text_file_is_skipped_not_ok(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("   \n\n", encoding="utf-8")
    assert extract(path).status == store.STATUS_NO_TEXT_LAYER


def test_oversized_file_is_skipped_by_quota(tmp_path):
    from docdedupe.config import DEFAULT_SETTINGS

    path = tmp_path / "big.txt"
    path.write_bytes(b"x" * 64)
    tiny = DEFAULT_SETTINGS.with_overrides(max_document_bytes=8)
    assert extract(path, settings=tiny).status == store.STATUS_TOO_LARGE


def test_every_skip_status_is_in_the_documented_vocabulary(tmp_path):
    produced = {
        extract(tmp_path / "missing.txt").status,
        extract(_write(tmp_path, "a.bin", b"x")).status,
        extract(_write(tmp_path, "b.pdf", pdfutil.build_no_text_pdf(1))).status,
        extract(_write(tmp_path, "c.pdf", pdfutil.build_encrypted_pdf([["x"]]))).status,
    }
    assert produced <= set(store.SKIP_STATUSES)


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_detection_trusts_magic_over_extension(tmp_path):
    docx_bytes = _minimal_docx()
    disguised = _write(tmp_path, "really_a_docx.pdf", docx_bytes)
    assert detect_format(disguised, docx_bytes[:8]) == "docx"

    pdf_bytes = _text_pdf()
    disguised_pdf = _write(tmp_path, "really_a_pdf.md", pdf_bytes)
    assert detect_format(disguised_pdf, pdf_bytes[:8]) == "pdf"


def test_detection_rejects_a_zip_that_is_not_a_docx(tmp_path):
    import zipfile

    path = tmp_path / "archive.docx"
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("readme.txt", "not a word document")
    assert detect_format(path, path.read_bytes()[:8]) is None


def test_sniff_supported_matches_the_documented_suffixes():
    assert sniff_supported(Path("a.pdf"))
    assert sniff_supported(Path("a.DOCX"))
    assert not sniff_supported(Path("a.exe"))


def _minimal_docx() -> bytes:
    from docx import Document
    import io

    document = Document()
    document.add_paragraph("示例项目甲")
    buffer = io.BytesIO()
    document.save(buffer)
    return docxutil.normalise_zip(buffer.getvalue())


# ---------------------------------------------------------------------------
# Corpus-wide expectations
# ---------------------------------------------------------------------------


def test_sample_corpus_matches_the_manifest_expectations():
    """Every sample must land on the status its truth record predicts.

    This is the join between generator and extractor: a change to either that
    silently reclassifies a document breaks here rather than in a report two
    milestones later.
    """
    import json

    manifest = json.loads((SAMPLES / "truth" / "manifest.json").read_text(encoding="utf-8"))
    expected = {}
    for group in manifest["groups"]:
        for member in group["members"]:
            expected[member["file"]] = store.STATUS_OK
    for item in manifest["skipped"] + manifest["unsupported"]:
        expected[item["file"]] = item["extract_status"]
    assert expected, "manifest is empty; the guard would be vacuous"

    mismatches = []
    for rel_path, status in sorted(expected.items()):
        actual = extract(SAMPLES / rel_path).status
        if actual != status:
            mismatches.append((rel_path, status, actual))
    assert mismatches == [], "extraction disagrees with truth: %s" % mismatches


def test_every_supported_format_in_the_corpus_yields_text():
    formats = {}
    for path in SAMPLES.rglob("*"):
        if not path.is_file() or path.suffix.lower() in {".json", ".doc", ".rtf"}:
            continue
        result = extract(path)
        if result.status == store.STATUS_OK:
            formats[path.suffix.lower()] = formats.get(path.suffix.lower(), 0) + 1
            assert result.text.strip(), "%s reported ok with empty text" % path
    for suffix in (".pdf", ".docx", ".md", ".txt", ".html"):
        assert suffix in formats, "corpus has no extractable %s file" % suffix
