"""Text extraction for the formats the first release supports.

Two rules drive this module:

* **No silent empties.** A file that cannot yield text is *skipped with a named
  reason* and never enters the candidate set as an empty document. The three
  reasons are locked by ``test_status_vocabulary_*`` and by
  ``data/README.md`` 4.5.
* **Format is decided by two independent signals.** A ``.pdf`` file whose bytes
  are not a PDF is ``skipped:unsupported``; a file with no extension whose bytes
  are a DOCX is extracted. Trusting the extension alone means a renamed file
  either crashes the scan or gets read as the wrong format.

The optional dependencies (``pypdf``, ``python-docx``) are imported lazily and
their absence surfaces as ``DependencyMissing``, which the CLI turns into exit
code 3 rather than a silently empty index.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import NamedTuple, Optional

from . import store


class ExtractResult(NamedTuple):
    """Outcome of one extraction attempt.

    ``status`` is always one of the values in ``store``'s vocabulary. ``text`` is
    empty for every skipped status; a caller must not treat "no text" as
    "no duplicate".
    """

    status: str
    text: str
    extractor: str


class DependencyMissing(RuntimeError):
    """An optional extraction dependency is not installed."""

    def __init__(self, package: str, milestone: str = "M1") -> None:
        super().__init__(
            "missing dependency %r required for scanning (planned from %s); "
            "install it with: pip install -e \".[extract]\"" % (package, milestone)
        )
        self.package = package


# The extractor identity is recorded per document *and* participates in the
# cache key, so bumping a number invalidates cached text for that format only
# after the version constant in config is bumped.
EXTRACTOR_TEXT = "text@1"
EXTRACTOR_MARKDOWN = "markdown@1"
EXTRACTOR_HTML = "html@1"
EXTRACTOR_DOCX = "python-docx@1"
EXTRACTOR_PDF = "pypdf@1"
EXTRACTOR_NONE = ""

_MAGIC_PDF = b"%PDF-"
_MAGIC_ZIP = b"PK\x03\x04"
_MAGIC_OLE = bytes.fromhex("D0CF11E0A1B11AE1")

# Encodings tried in order for plain text. utf-8-sig first so a BOM is stripped;
# GBK covers Chinese files written by Windows tools; latin-1 is the final
# fallback and cannot fail.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "latin-1")

# An HTML tag, used to strip markup. Deliberately not a full parser: this runs on
# text that html.parser already produced, and the goal is text, not structure.
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")


class ExtractedHTMLParser:
    """Fallback HTML text extractor built on the standard library.

    ``html.parser`` is used directly instead of a third-party renderer: the goal
    is the visible text, and a renderer would add dependencies plus its own idea
    of what "text" means. ``script``/``style`` content is discarded because it is
    not document body text.
    """

    def __init__(self) -> None:
        from html.parser import HTMLParser

        self._chunks = []
        outer = self

        class _Parser(HTMLParser):
            _SKIP = {"script", "style", "head", "noscript", "template"}
            _BLOCK = {
                "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                "section", "article", "table", "thead", "tbody", "ul", "ol", "pre",
            }

            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self._skip_depth = 0

            def handle_starttag(self, tag, attrs):
                if tag in self._SKIP:
                    self._skip_depth += 1
                elif tag in self._BLOCK:
                    outer._chunks.append("\n")

            def handle_endtag(self, tag):
                if tag in self._SKIP and self._skip_depth:
                    self._skip_depth -= 1
                elif tag in self._BLOCK:
                    outer._chunks.append("\n")

            def handle_data(self, data):
                if not self._skip_depth and data.strip():
                    outer._chunks.append(data)

        self._parser_class = _Parser

    def feed(self, text: str) -> str:
        parser = self._parser_class()
        parser.feed(text)
        parser.close()
        raw = "".join(self._chunks)
        lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def extract(path: Path, *, settings=None) -> ExtractResult:
    """Extract the body text of ``path`` or report why it cannot be read."""
    from .config import DEFAULT_SETTINGS

    cfg = settings or DEFAULT_SETTINGS
    path = Path(path)

    try:
        size = path.stat().st_size
    except OSError:
        return ExtractResult(store.STATUS_READ_ERROR, EXTRACTOR_NONE, "")
    if size > cfg.max_document_bytes:
        return ExtractResult(store.STATUS_TOO_LARGE, EXTRACTOR_NONE, "")

    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return ExtractResult(store.STATUS_READ_ERROR, EXTRACTOR_NONE, "")

    kind = detect_format(path, head)
    if kind is None:
        return ExtractResult(store.STATUS_UNSUPPORTED, EXTRACTOR_NONE, "")

    try:
        if kind == "pdf":
            return _extract_pdf(path)
        if kind == "docx":
            return _extract_docx(path)
        if kind == "html":
            return _extract_html(path)
        if kind == "markdown":
            return _extract_text_like(path, EXTRACTOR_MARKDOWN)
        if kind == "text":
            return _extract_text_like(path, EXTRACTOR_TEXT)
    except DependencyMissing:
        raise
    except Exception:  # noqa: BLE001 - a malformed file must not abort the scan
        # A corrupt or truncated file is a read error, not a reason to stop the
        # whole scan; the summary reports the count so it is never silent.
        return ExtractResult(store.STATUS_READ_ERROR, EXTRACTOR_NONE, "")

    return ExtractResult(store.STATUS_UNSUPPORTED, EXTRACTOR_NONE, "")


def detect_format(path: Path, head: bytes) -> Optional[str]:
    """Identify the format from the extension *and* the magic bytes.

    The extension is a hint; the magic is the evidence. When they disagree the
    magic wins, so a ``.pdf`` containing a DOCX is read as a DOCX rather than
    failing later, and a ``.md`` that is really a PDF is read as a PDF.
    """
    suffix = path.suffix.lower()

    if head.startswith(_MAGIC_PDF):
        return "pdf"
    if head.startswith(_MAGIC_ZIP):
        return "docx" if _zip_is_docx(path) else None
    if head.startswith(_MAGIC_OLE):
        # Legacy .doc/.xls/.ppt: recognised, deliberately unsupported.
        return None

    if suffix == ".pdf":
        # Extension claims PDF but the magic agrees with nothing above.
        return None
    if suffix in (".docx",):
        return None
    if suffix in (".html", ".htm"):
        return "html"
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix == ".txt":
        return "text"
    return None


def _zip_is_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return "word/document.xml" in names


def _extract_pdf(path: Path) -> ExtractResult:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - environment without the extra
        raise DependencyMissing("pypdf")

    try:
        reader = PdfReader(str(path))
    except Exception:
        # A file that starts with %PDF- but cannot be parsed is unreadable, not
        # unsupported: the format was recognised, the content was not.
        return ExtractResult(store.STATUS_READ_ERROR, EXTRACTOR_NONE, "")

    if reader.is_encrypted:
        # An empty user password decrypts transparently and is not a problem.
        # Anything else means we cannot read the body, and calling it "empty"
        # would let a password-protected duplicate hide from the report.
        try:
            result = reader.decrypt("")
        except Exception:
            result = 0
        if not result:
            return ExtractResult(store.STATUS_ENCRYPTED, EXTRACTOR_NONE, "")

    parts = []
    try:
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append("")
    except Exception:
        return ExtractResult(store.STATUS_READ_ERROR, EXTRACTOR_NONE, "")

    text = "\n".join(part for part in parts if part).strip()
    if not text:
        return ExtractResult(store.STATUS_NO_TEXT_LAYER, EXTRACTOR_NONE, "")
    return ExtractResult(store.STATUS_OK, text, EXTRACTOR_PDF)


def _extract_docx(path: Path) -> ExtractResult:
    try:
        from docx import Document
    except ImportError:  # pragma: no cover - environment without the extra
        raise DependencyMissing("python-docx")

    document = Document(str(path))
    parts = []
    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            parts.append(paragraph.text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            line = "\t".join(cell for cell in cells if cell)
            if line:
                parts.append(line)

    text = "\n".join(parts).strip()
    if not text:
        # A DOCX with no words is not a text document; report it as having no
        # text layer so it stays out of the candidate set explicitly.
        return ExtractResult(store.STATUS_NO_TEXT_LAYER, EXTRACTOR_NONE, "")
    return ExtractResult(store.STATUS_OK, text, EXTRACTOR_DOCX)


def _extract_html(path: Path) -> ExtractResult:
    raw = path.read_bytes()
    text = _decode_text(raw)
    # Strip <head> wholesale before parsing: a title is not body text, and the
    # parser's skip list would otherwise have to track nesting across the head.
    text = re.sub(r"(?is)<head\b.*?</head>", "", text)
    visible = ExtractedHTMLParser().feed(text)
    visible = _TAG_RE.sub("", visible)
    visible = "\n".join(line.strip() for line in visible.splitlines() if line.strip())
    if not visible:
        return ExtractResult(store.STATUS_NO_TEXT_LAYER, EXTRACTOR_NONE, "")
    return ExtractResult(store.STATUS_OK, visible, EXTRACTOR_HTML)


def _extract_text_like(path: Path, extractor: str) -> ExtractResult:
    raw = path.read_bytes()
    text = _decode_text(raw)
    if not text.strip():
        return ExtractResult(store.STATUS_NO_TEXT_LAYER, EXTRACTOR_NONE, "")
    return ExtractResult(store.STATUS_OK, text, extractor)


def _decode_text(raw: bytes) -> str:
    """Decode bytes with the documented encoding order.

    ``latin-1`` cannot fail, so this function always returns a string; a wrong
    guess yields mojibake rather than an exception, which the normalizer and the
    similarity score will treat as a distinct document rather than crash on.
    """
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def sniff_supported(path: Path) -> bool:
    """Cheap pre-check used by the walker to decide whether to open a file.

    Returning ``True`` for a file :func:`detect_format` later rejects is fine:
    the walker's job is to avoid opening binaries, not to duplicate the format
    decision.
    """
    suffix = path.suffix.lower()
    return suffix in {
        ".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm",
    }
