"""Byte-deterministic PDF writer for synthetic samples.

Only the few PDF features the sample set needs are implemented: one font
(STSong-Light, a standard CJK font name that viewers substitute), a text-only
content stream, an optional no-text page, and optional RC4 encryption.

Determinism is the point. ``pypdf`` writes an ``/ID`` derived from the clock and
uses ``os.urandom`` for encryption, so a PDF it produces differs on every run
and ``gen_samples --check`` could never be green. Everything here is derived
from the input alone: the file id is a hash of a fixed label, the encryption key
comes from fixed passwords, and no timestamp is written.

The generator and the tests both use this module, so a sample that pypdf can
read is proven at build time rather than assumed.
"""

from __future__ import annotations

import hashlib
import io
import zlib
from typing import Iterable, List, Optional, Sequence, Tuple

# PDF's standard 32-byte padding string (ISO 32000-1, algorithm 2).
_PAD = bytes(
    [
        0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56,
        0xFF, 0xFA, 0x01, 0x08, 0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
        0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
    ]
)

# User permission bits: allow print/copy, deny modification and annotation.
# Written as an unsigned 32-bit value; PDF viewers interpret it as one.
_PERMISSIONS = -3904


def _rc4(key: bytes, data: bytes) -> bytes:
    """RC4 is required by the PDF standard security handler (revision 2)."""
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]

    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def _pad_password(password: bytes) -> bytes:
    return (password + _PAD)[:32]


def _object_key(key: bytes, number: int, generation: int = 0) -> bytes:
    """Per-object RC4 key (ISO 32000-1, algorithm 1)."""
    material = key + number.to_bytes(3, "little") + generation.to_bytes(2, "little")
    return hashlib.md5(material).digest()[: min(len(key) + 5, 16)]


def _hex_text(text: str) -> bytes:
    return b"<" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _content_stream(lines: Sequence[str], header: str, footer: str) -> bytes:
    parts = ["BT", "/F1 11 Tf", "14 TL", "56 800 Td"]
    emitted: List[str] = []
    if header:
        emitted.append(header)
    emitted.extend(lines)
    if footer:
        emitted.append(footer)
    for line in emitted:
        parts.append("%s Tj T*" % _hex_text(line).decode("ascii"))
    parts.append("ET")
    return ("\n".join(parts)).encode("ascii")


def _to_unicode_cmap(characters: Iterable[str]) -> bytes:
    chars = sorted(set(characters))
    blocks: List[str] = []
    for start in range(0, len(chars), 100):
        chunk = chars[start : start + 100]
        entries = [
            "<%s> <%s>" % (ch.encode("utf-16-be").hex().upper(), ch.encode("utf-16-be").hex().upper())
            for ch in chunk
        ]
        blocks.append("%d beginbfchar\n%s\nendbfchar" % (len(entries), "\n".join(entries)))
    body = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        "%s\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\nend\n" % "\n".join(blocks)
    )
    return body.encode("ascii")


def _serialise(objects: List[bytes], *, encrypt_ref: Optional[Tuple[int, int]] = None) -> bytes:
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    out.write(b"%\xe2\xe3\xcf\xd3\n")

    offsets: List[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(("%d 0 obj\n" % number).encode("ascii"))
        out.write(body)
        out.write(b"\nendobj\n")

    xref_at = out.tell()
    out.write(("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(("%010d 00000 n \n" % offset).encode("ascii"))

    trailer = "<< /Size %d /Root 1 0 R" % (len(objects) + 1)
    if encrypt_ref is not None:
        trailer += " /Encrypt %d %d R" % encrypt_ref
        file_id = _file_id_hex()
        trailer += " /ID [<%s> <%s>]" % (file_id, file_id)
    trailer += " >>"
    out.write(("trailer\n%s\nstartxref\n%d\n%%%%EOF\n" % (trailer, xref_at)).encode("ascii"))
    return out.getvalue()


def _file_id_hex() -> str:
    # Derived from a fixed label, never from the clock or the machine.
    return hashlib.md5(b"docdedupe-synthetic-pdf").hexdigest()


def build_text_pdf(
    pages: Sequence[Sequence[str]],
    *,
    header: str = "",
    footer_template: str = "",
    compress: bool = True,
) -> bytes:
    """Build a text-only PDF from a list of pages, each a list of lines.

    ``footer_template`` may contain ``{page}`` and is filled with the 1-based
    page number. Repeating a header and a page number on every page is exactly
    the noise module 2's normalizer has to remove, so the sample PDF carries it
    in its real extracted-text position rather than as a DOCX section property
    that no extractor would surface.
    """
    if not pages:
        raise ValueError("a PDF needs at least one page")

    page_count = len(pages)
    # Object numbering: 1 catalog, 2 pages tree, 3 page tree nodes, then one
    # content stream per page, then the font objects.
    first_page = 3
    first_content = first_page + page_count
    font_number = first_content + page_count
    cidfont_number = font_number + 1
    descriptor_number = font_number + 2
    cmap_number = font_number + 3

    children = " ".join("%d 0 R" % (first_page + index) for index in range(page_count))
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [%s] /Count %d >>" % (children, page_count)).encode("ascii"),
    ]

    all_characters: List[str] = []
    streams: List[bytes] = []
    for index, lines in enumerate(pages):
        footer = footer_template.replace("{page}", str(index + 1)) if footer_template else ""
        stream = _content_stream(lines, header, footer)
        streams.append(stream)
        all_characters.extend(header)
        all_characters.extend(footer)
        for line in lines:
            all_characters.extend(line)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (font_number, first_content + index)
            ).encode("ascii")
        )

    for index, stream in enumerate(streams):
        if compress:
            payload = zlib.compress(stream, 9)
            objects.append(
                b"<< /Length "
                + str(len(payload)).encode("ascii")
                + b" /Filter /FlateDecode >>\nstream\n"
                + payload
                + b"\nendstream"
            )
        else:
            objects.append(
                b"<< /Length "
                + str(len(stream)).encode("ascii")
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )

    objects.append(
        (
            "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
            "/Encoding /UniGB-UCS2-H /DescendantFonts [%d 0 R] "
            "/ToUnicode %d 0 R >>" % (cidfont_number, cmap_number)
        ).encode("ascii")
    )
    objects.append(
        (
            "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
            "/FontDescriptor %d 0 R /DW 1000 >>" % descriptor_number
        ).encode("ascii")
    )
    objects.append(
        b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 "
        b"/FontBBox [-25 -254 1000 880] /ItalicAngle 0 /Ascent 880 "
        b"/Descent -120 /CapHeight 880 /StemV 80 >>"
    )
    cmap = _to_unicode_cmap(all_characters)
    objects.append(
        b"<< /Length "
        + str(len(cmap)).encode("ascii")
        + b" >>\nstream\n"
        + cmap
        + b"endstream"
    )

    return _serialise(objects)


def build_no_text_pdf(pages: int = 1) -> bytes:
    """Build a PDF with no text operators at all (stands in for a scan)."""
    if pages < 1:
        raise ValueError("page count must be positive")
    children = " ".join("%d 0 R" % (3 + index) for index in range(pages))
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [%s] /Count %d >>" % (children, pages)).encode("ascii"),
    ]
    for index in range(pages):
        stream = b"0.88 0.88 0.88 rg 56 60 483 720 re f\n"
        payload = zlib.compress(stream, 9)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                "/Resources << >> /Contents %d 0 R >>" % (3 + pages + index)
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(payload)).encode("ascii")
            + b" /Filter /FlateDecode >>\nstream\n"
            + payload
            + b"\nendstream"
        )
    return _serialise(objects)


def build_encrypted_pdf(
    pages: Sequence[Sequence[str]],
    *,
    user_password: bytes = b"sample-user",
    owner_password: bytes = b"sample-owner",
    header: str = "",
    footer_template: str = "",
) -> bytes:
    """Build a text PDF that requires a password to open.

    The user password is intentionally non-empty, so a reader without it reports
    ``is_encrypted`` and cannot fetch the text. An empty user password would be
    transparently decryptable and would not exercise the ``skipped:encrypted``
    path at all.
    """
    if not pages:
        raise ValueError("a PDF needs at least one page")
    if not user_password:
        raise ValueError("the encrypted sample must have a non-empty user password")

    page_count = len(pages)
    first_page = 3
    first_content = first_page + page_count
    font_number = first_content + page_count
    cidfont_number = font_number + 1
    descriptor_number = font_number + 2
    cmap_number = font_number + 3
    encrypt_number = font_number + 4

    file_id = bytes.fromhex(_file_id_hex())
    owner_key = hashlib.md5(_pad_password(owner_password)).digest()[:5]
    owner_entry = _rc4(owner_key, _pad_password(user_password))
    encryption_key = hashlib.md5(
        _pad_password(user_password)
        + owner_entry
        + _PERMISSIONS.to_bytes(4, "little", signed=True)
        + file_id
    ).digest()[:5]
    user_entry = _rc4(encryption_key, _PAD)

    children = " ".join("%d 0 R" % (first_page + index) for index in range(page_count))
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [%s] /Count %d >>" % (children, page_count)).encode("ascii"),
    ]

    all_characters: List[str] = []
    raw_streams: List[bytes] = []
    for index, lines in enumerate(pages):
        footer = footer_template.replace("{page}", str(index + 1)) if footer_template else ""
        raw_streams.append(_content_stream(lines, header, footer))
        all_characters.extend(header)
        all_characters.extend(footer)
        for line in lines:
            all_characters.extend(line)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (font_number, first_content + index)
            ).encode("ascii")
        )

    for index, raw in enumerate(raw_streams):
        number = first_content + index
        payload = zlib.compress(raw, 9)
        payload = _rc4(_object_key(encryption_key, number), payload)
        objects.append(
            b"<< /Length "
            + str(len(payload)).encode("ascii")
            + b" /Filter /FlateDecode >>\nstream\n"
            + payload
            + b"\nendstream"
        )

    objects.append(
        (
            "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
            "/Encoding /UniGB-UCS2-H /DescendantFonts [%d 0 R] "
            "/ToUnicode %d 0 R >>" % (cidfont_number, cmap_number)
        ).encode("ascii")
    )
    objects.append(
        (
            "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
            "/FontDescriptor %d 0 R /DW 1000 >>" % descriptor_number
        ).encode("ascii")
    )
    objects.append(
        b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 "
        b"/FontBBox [-25 -254 1000 880] /ItalicAngle 0 /Ascent 880 "
        b"/Descent -120 /CapHeight 880 /StemV 80 >>"
    )
    cmap = _to_unicode_cmap(all_characters)
    cmap_encrypted = _rc4(_object_key(encryption_key, cmap_number), cmap)
    objects.append(
        b"<< /Length "
        + str(len(cmap_encrypted)).encode("ascii")
        + b" >>\nstream\n"
        + cmap_encrypted
        + b"endstream"
    )
    objects.append(
        (
            "<< /Filter /Standard /V 1 /R 2 /Length 40 /P %d /O <%s> /U <%s> >>"
            % (_PERMISSIONS, owner_entry.hex(), user_entry.hex())
        ).encode("ascii")
    )

    return _serialise(objects, encrypt_ref=(encrypt_number, 0))
