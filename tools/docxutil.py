"""Write DOCX files with stable zip metadata.

``python-docx`` stamps every zip entry with the current local time, so two runs
one second apart produce different bytes even though the document content is
identical. That alone would make ``gen_samples --check`` fail on the clock.

This module rebuilds the archive with:

* every entry timestamp fixed at ``(1980, 1, 1, 0, 0, 0)`` (the DOS epoch);
* ``create_system = 0`` so the host OS does not leak into the header;
* a fixed external attribute word, so the permission bits are identical on
  Windows and POSIX;
* entry order and compression level preserved.

The result depends only on the document content.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from typing import Optional

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_FIXED_DATE_TIME = datetime(*FIXED_TIMESTAMP)
# 0o644, regular file, expressed the way zipfile wants external_attr.
_FIXED_EXTERNAL_ATTR = 0o600 << 16


def normalise_zip(data: bytes, level: int = 9) -> bytes:
    """Return ``data`` re-zipped with fixed metadata."""
    source = zipfile.ZipFile(io.BytesIO(data))
    try:
        entries = [(info.filename, source.read(info.filename)) for info in source.infolist()]
    finally:
        source.close()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level) as target:
        for name, payload in entries:
            info = zipfile.ZipInfo(filename=name, date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = _FIXED_EXTERNAL_ATTR
            target.writestr(info, payload)
    return out.getvalue()


def pin_core_properties(document, title: str = "", author: str = "") -> None:
    """Replace the package timestamps with a fixed value.

    ``python-docx`` copies ``docProps/core.xml`` from its bundled template, which
    happens to be constant today. Pinning it explicitly means a future template
    update cannot silently break byte-level reproducibility.
    """
    props = document.core_properties
    props.created = _FIXED_DATE_TIME
    props.modified = _FIXED_DATE_TIME
    props.last_printed = _FIXED_DATE_TIME
    props.revision = 1
    props.title = title
    props.author = author
    props.last_modified_by = author
    props.comments = ""
    props.subject = ""
    props.keywords = ""
    props.category = ""


def save_docx(document, path, *, level: int = 9) -> None:
    """Save a ``python-docx`` document to ``path`` with normalised metadata."""
    import os

    buffer = io.BytesIO()
    document.save(buffer)
    payload = normalise_zip(buffer.getvalue(), level=level)
    with open(path, "wb") as handle:
        handle.write(payload)


def is_docx(data: bytes) -> Optional[bool]:
    """Cheap magic check: OOXML packages are zips containing ``[Content_Types].xml``."""
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return False
    with archive:
        names = set(archive.namelist())
    return "[Content_Types].xml" in names and any(name.startswith("word/") for name in names)
