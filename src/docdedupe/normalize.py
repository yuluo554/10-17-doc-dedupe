"""Chinese-friendly text normalization (module 2, stage 1).

The output of :func:`normalize` is what every similarity score is computed on,
so the order of the steps is a contract, not an implementation detail. The
canonical definition is ``data/README.md`` section 4.3; this module is its only
implementation. Changing a step means bumping ``NORMALIZE_VERSION`` and editing
that section in the same commit, because the version participates in the cache
key (``store.cache_key``).

Why the pipeline is shaped the way it is
----------------------------------------
Chinese has no spaces, so a normalizer that keeps word boundaries is useless
here; whitespace is removed outright. But page numbers and running heads can
only be recognised *as lines*, so the line break survives as structure until the
line-based stages have run and is removed at the end. The observable order of
the five documented steps is unchanged: NFKC, then whitespace/invisibles, then
page numbers, then running heads, then Latin lower-casing.

Running heads are the hard case. The extractors in :mod:`docdedupe.extract` hand
back a plain text stream with no page geometry: a PDF header is just another
line. The only usable evidence is "short line repeated within the document",
which is what ``data/README.md`` 4.3 calls 跨页高频短行 and what
:func:`_stage_running_heads` implements.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable, List, Optional, Sequence, Tuple

from .config import DEFAULT_SETTINGS, Settings
from .config import NORMALIZE_VERSION as _CONFIG_NORMALIZE_VERSION

# Re-exported so callers can ``from .normalize import NORMALIZE_VERSION`` as the
# module interface in plan/03 promises. The literal lives only in config.py.
NORMALIZE_VERSION = _CONFIG_NORMALIZE_VERSION

# The five documented steps, in order. Tests assert against this tuple so a
# reordering cannot happen quietly, and so the docstring order stays executable.
STAGE_ORDER: Tuple[str, ...] = (
    "nfkc",
    "strip_whitespace",
    "drop_page_numbers",
    "drop_running_heads",
    "lower_latin",
)

# Control characters and zero-width marks: they carry no visible content, and a
# stray BOM would otherwise make two identical documents hash differently.
_INVISIBLE_RE = re.compile(
    "["
    "\u0000-\u0008"
    "\u000b\u000c"
    "\u000e-\u001f"
    "\u007f-\u009f"
    "\u00ad"
    "\u200b-\u200f"
    "\u202a-\u202e"
    "\u2060-\u2064"
    "\ufeff"
    "\ufff9-\ufffb"
    "]"
)

# All whitespace, including the ideographic space U+3000 that NFKC turns into a
# normal space first.
_WHITESPACE_RE = re.compile(r"\s+")

# Page-number lines, matched against a whole line *after* whitespace removal.
# A bare 1-3 digit line is the usual "page 7" footer. Four digits are not
# accepted bare on purpose: "2026" is a year far more often than a page number.
_PAGE_NUMBER_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^第\d{1,4}页$",
        r"^第\d{1,4}页[／/]共\d{1,4}页$",
        r"^共\d{1,4}页$",
        r"^-\d{1,3}-$",
        r"^\d{1,3}[／/]\d{1,3}$",
        r"^(?:page|p\.?)\d{1,4}(?:[／/]\d{1,4})?$",
        r"^\d{1,3}$",
    )
)
_PAGE_NUMBER_CASELESS = re.compile(
    r"^(?:page|p\.?)\d{1,4}(?:[／/]\d{1,4})?$", re.IGNORECASE
)


def _build_latin_lower_table():
    """Map Latin capital letters to lower case, and nothing else.

    ``str.lower`` would also fold Greek and Cyrillic, which would silently
    change non-Latin content; the contract says "Latin only". The table covers
    ASCII plus Latin-1/Extended-A/B, which is where mixed Chinese/English
    documents actually live.
    """
    table = {}
    for code in range(0x41, 0x250):
        char = chr(code)
        if unicodedata.name(char, "").startswith("LATIN CAPITAL LETTER"):
            lower = char.lower()
            if lower != char:
                table[code] = lower
    return table


_LATIN_LOWER_TABLE = _build_latin_lower_table()


# ---------------------------------------------------------------------------
# Individual stages
# ---------------------------------------------------------------------------


def _stage_nfkc(text: str) -> str:
    """Step 1: compatibility normalization (full-width forms become half-width)."""
    return unicodedata.normalize("NFKC", text)


def _stage_strip_whitespace(text: str) -> str:
    """Step 2: drop invisible characters and every whitespace run.

    Line breaks are kept: the next two stages need line structure to recognise
    page numbers and running heads. They are removed by :func:`normalize` once
    those stages are done.
    """
    cleaned = _INVISIBLE_RE.sub("", text)
    return "\n".join(
        _WHITESPACE_RE.sub("", line) for line in cleaned.splitlines()
    )


def _stage_drop_page_numbers(text: str) -> str:
    """Step 3: remove lines that are nothing but a page number."""
    kept = []
    for line in text.splitlines():
        if not line:
            continue
        if _PAGE_NUMBER_CASELESS.match(line):
            continue
        if any(pattern.match(line) for pattern in _PAGE_NUMBER_PATTERNS):
            continue
        kept.append(line)
    return "\n".join(kept)


def _stage_running_heads(text: str, settings: Settings) -> str:
    """Step 4: remove short lines repeated within the document.

    A header/footer repeats on every page; with no page geometry available, a
    short line that occurs at least ``header_footer_min_repeats`` times is the
    proxy. The length cap is what keeps a repeated *body* sentence (rare but
    possible) out of the removal set.
    """
    lines = [line for line in text.splitlines() if line]
    counts = Counter(lines)
    kept = [
        line
        for line in lines
        if len(line) > settings.header_footer_max_chars
        or counts[line] < settings.header_footer_min_repeats
    ]
    return "\n".join(kept)


def _stage_lower_latin(text: str) -> str:
    """Step 5: lower-case Latin letters only."""
    return text.translate(_LATIN_LOWER_TABLE)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def normalize(text: str, settings: Optional[Settings] = None) -> str:
    """Return the comparison form of ``text``.

    The result contains no whitespace, no page numbers, no repeated running
    heads, and no upper-case Latin letters. Two documents that differ only in
    those respects normalize to the same string, which is exactly the claim
    benchmark B2 checks.
    """
    cfg = settings or DEFAULT_SETTINGS
    value = _stage_nfkc(text)
    value = _stage_strip_whitespace(value)
    value = _stage_drop_page_numbers(value)
    value = _stage_running_heads(value, cfg)
    value = _stage_lower_latin(value)
    # Line breaks were structure for steps 3-4; the comparison form is one
    # stream so that a shingle never straddles a removed line boundary.
    return "".join(value.splitlines())


def normalized_length(text: str, settings: Optional[Settings] = None) -> int:
    """Convenience wrapper used by candidate pre-filtering."""
    return len(normalize(text, settings))


__all__ = [
    "NORMALIZE_VERSION",
    "STAGE_ORDER",
    "normalize",
    "normalized_length",
]
