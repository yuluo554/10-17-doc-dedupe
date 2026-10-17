"""Single source of truth for numeric settings.

Every threshold used by fingerprinting, clustering, or reporting lives here and
nowhere else. The authoritative *definitions* (what each number means) are in
``data/README.md`` section 4; this module only holds the default values and the
plumbing to load them. Code, reports and documentation must reference that
section instead of restating the rules.

Changing a default here changes cache behaviour: ``CACHE_SCHEMA_VERSION`` and
``NORMALIZE_VERSION`` participate in the cache key, so bump them whenever the
extraction or normalization logic changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Tuple

# Bumped when the on-disk SQLite layout changes; an older database is rejected
# instead of being silently migrated (see store.connect).
# Version 2 adds the shingle-hash blob (exact scoring) and the MinHash estimate
# column on ``pairs``.
DB_SCHEMA_VERSION = 2

# Bumped when normalize() changes, so cached normalized text is invalidated.
NORMALIZE_VERSION = 1

# Bumped when the extraction logic changes, so cached extracted text is invalidated.
# Version 2 strips Markdown syntax markers, so a Markdown twin normalizes to the
# same text as its DOCX/PDF twin (data/README.md 4.3).
EXTRACTOR_VERSION = 2

# Bumped when the fingerprint algorithm changes, so cached fingerprints are invalidated.
# Version 2 is the release that first *writes* fingerprints: an index produced by
# M1 has document rows without normalized text, so its cache keys must not match
# or those rows would stay un-fingerprinted forever.
FINGERPRINT_VERSION = 2


@dataclass(frozen=True)
class Settings:
    """All tunable numbers for one run.

    Defaults are intentionally conservative; the acceptance numbers themselves
    (recall floor, cache speed-up floor) live in ``data/README.md`` and in the
    benchmark manifests, never here.
    """

    # --- fingerprinting (data/README.md 4.1-4.6) ---
    shingle_size: int = 3
    simhash_bits: int = 64
    minhash_num_perm: int = 128
    lsh_bands: int = 32
    lsh_rows: int = 4  # num_perm must equal lsh_bands * lsh_rows

    # --- candidate pre-filter (only reduces candidates, never the truth set) ---
    # Applied to *normalized text length*, not raw file size: the same body is a
    # 37 KB DOCX and a 1.3 KB TXT, so a raw-size window would reject every
    # cross-format pair before scoring. The window still does its documented job
    # (data/README.md 4.6) — it keeps "a note and a report" from being compared.
    size_ratio_window: float = 0.5
    min_text_chars: int = 80
    # SimHash Hamming bound. Unrelated documents in the corpus sit around 24-30
    # bits apart; 20 is the point where the auxiliary filter starts contributing
    # rather than merely agreeing with the MinHash score.
    simhash_max_hamming: int = 20

    # --- running heads (data/README.md 4.3 step 4) ---
    # A header/footer is a *short* line that repeats. Both bounds matter: the
    # length cap keeps repeated body sentences, the repeat count keeps a title
    # that legitimately appears twice.
    header_footer_max_chars: int = 40
    header_footer_min_repeats: int = 2

    # --- clustering tiers (data/README.md 4.5) ---
    exact_threshold: float = 1.0
    near_duplicate_threshold: float = 0.80
    template_threshold: float = 0.90
    template_field_diff_max: int = 6

    # --- pair retention (data/README.md 4.6) ---
    # ``pairs`` keeps every scored candidate at or above this floor. The floor is
    # below the tier thresholds on purpose: template-family members sit around
    # 0.57-0.80, and module 3 has to see them to decide the template tier. It is
    # a *retention* floor, not a decision: no tier is assigned here.
    pair_store_threshold: float = 0.55

    # --- diff summary (data/README.md 4.10) ---
    signature_max_chars: int = 40

    # --- scanning ---
    ignore_dirs: Tuple[str, ...] = (
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".dedupe-quarantine",
        ".dedupe-cache",
        "$RECYCLE.BIN",
        "System Volume Information",
    )
    indexed_suffixes: Tuple[str, ...] = (
        ".pdf",
        ".docx",
        ".md",
        ".markdown",
        ".txt",
        ".html",
        ".htm",
    )
    # Files the scanner opens only to report them as unsupported. They are real
    # documents in a format we do not read, so "not compared" has to be visible
    # rather than the file silently disappearing from the scan.
    legacy_suffixes: Tuple[str, ...] = (
        ".doc",
        ".docm",
        ".rtf",
    )

    @property
    def scan_candidate_suffixes(self) -> Tuple[str, ...]:
        """Suffixes the walker is willing to open.

        Anything else (``.json``, ``.zip``, images, source files) is not a
        document candidate at all and must not inflate the skip counters.
        """
        return self.indexed_suffixes + self.legacy_suffixes

    # --- web ---
    web_host: str = "127.0.0.1"
    web_port: int = 8765

    # --- quota ---
    max_document_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.simhash_bits != 64:
            raise ValueError("simhash_bits is fixed at 64 by data/README.md 4.1")
        if self.shingle_size != 3:
            raise ValueError("shingle_size is fixed at 3 by data/README.md 4.2")
        if self.lsh_bands * self.lsh_rows != self.minhash_num_perm:
            raise ValueError(
                "minhash_num_perm must equal lsh_bands * lsh_rows "
                "(%d != %d * %d)"
                % (self.minhash_num_perm, self.lsh_bands, self.lsh_rows)
            )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "Settings":
        """Build settings from a mapping, rejecting unknown keys.

        Unknown keys are rejected on purpose: a typo in a config file must not
        silently fall back to a default and change clustering results.
        """
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError("unknown setting(s): %s" % ", ".join(unknown))
        kwargs: Dict[str, Any] = dict(values)
        for key in ("ignore_dirs", "indexed_suffixes"):
            if key in kwargs:
                kwargs[key] = tuple(kwargs[key])
        return cls(**kwargs)

    def with_overrides(self, **overrides: Any) -> "Settings":
        """Return a copy with the given fields replaced (validated again)."""
        return replace(self, **overrides)


DEFAULT_SETTINGS = Settings()


def cache_key_fields(settings: Settings) -> Tuple[int, int, int, int, int]:
    """The version/parameter tuple that participates in every cache key."""
    return (
        EXTRACTOR_VERSION,
        NORMALIZE_VERSION,
        FINGERPRINT_VERSION,
        settings.shingle_size,
        settings.minhash_num_perm,
    )
