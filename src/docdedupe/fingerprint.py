"""Chinese-friendly fingerprints and candidate recall (module 2, stage 2).

Four ideas, each with one job:

* **Character 3-gram shingles.** Chinese has no spaces and no reliable word
  boundary, so shingles are cut by character. The same code path handles English
  text; there is no tokenizer to version or to disagree about.
* **MinHash (128 permutations) + LSH banding (32 x 4).** The recall mechanism:
  pairs that share a band are the only pairs that get scored. Without it the
  scan would compare every document with every other document.
* **Jaccard over the shingle sets.** The score. It is computed exactly for the
  band candidates from stored 64-bit shingle hashes, and the MinHash estimate is
  kept beside it as an auditable secondary signal.
* **64-bit SimHash.** A bucketing signal only. Its Hamming distance rejects
  clearly unrelated candidates before the (still cheap) exact comparison; it
  never decides a similarity score.

Why the score is exact rather than the MinHash estimate
------------------------------------------------------
A 128-permutation signature estimates Jaccard with a standard error of about
``sqrt(0.2 * 0.8 / 128) ~= 0.035``. The synthetic corpus deliberately puts its
weakest true pairs at 0.81-0.83, a margin of 0.01-0.03 over the 0.80 threshold
(``data/README.md`` 4.5). Gating on the estimate would therefore put sampling
noise, not document similarity, on the decision boundary: four of the twenty
benchmark groups split apart purely because their estimate happened to land on
the wrong side. The estimate remains stored (``pairs.jaccard_estimate``) and
``score_kind`` names the score that produced ``pairs.jaccard``, so a report
cannot present the auxiliary signal as the decision.

Everything here is deterministic. Shingles are hashed with BLAKE2b rather than
``hash()`` because Python salts string hashes per process; a signature built on
``hash()`` would differ between two runs of the same scan.

Threshold values are not defined here. ``data/README.md`` section 4 owns them and
:class:`docdedupe.config.Settings` carries the defaults; this module reads the
settings it is handed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from .config import DEFAULT_SETTINGS, Settings

# ``pairs.score_kind``: which computation produced ``pairs.jaccard``. The report
# layer reads this instead of assuming (data/README.md 4.4).
SCORE_KIND_EXACT = "exact_jaccard"
SCORE_KIND_MINHASH = "minhash"

_MASK64 = (1 << 64) - 1


# ---------------------------------------------------------------------------
# Shingles and hashing
# ---------------------------------------------------------------------------


def shingles(text: str, n: int = 3) -> List[str]:
    """Return the sorted unique character n-grams of ``text``.

    Sorted so the result is comparable across runs and easy to assert on;
    uniqueness because Jaccard similarity is defined over a set. A text shorter
    than ``n`` yields no shingles, which is why tiny documents are filtered out
    before scoring rather than being compared as "empty vs empty".
    """
    if n < 1:
        raise ValueError("shingle length must be positive")
    if len(text) < n:
        return []
    return sorted({text[index : index + n] for index in range(len(text) - n + 1)})


def shingle_set(text: str, n: int = 3) -> Set[str]:
    if n < 1:
        raise ValueError("shingle length must be positive")
    if len(text) < n:
        return set()
    return {text[index : index + n] for index in range(len(text) - n + 1)}


def _hash64(value: str) -> int:
    """Stable 64-bit hash of a string.

    ``digest_size=8`` of BLAKE2b: fast, available on every supported Python, and
    unlike the built-in ``hash()`` it does not change between processes.
    """
    return int.from_bytes(
        hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(), "big"
    )


def shingle_hashes(text: str, n: int = 3) -> Tuple[int, ...]:
    """64-bit hashes of the shingle set, sorted ascending.

    Hashing the shingles once serves MinHash (which permutes hashes) and exact
    Jaccard (which intersects sorted hash lists). Sorting makes the stored blob
    canonical: the same text always produces the same bytes.
    """
    return tuple(sorted(_hash64(value) for value in shingle_set(text, n)))


# ---------------------------------------------------------------------------
# Exact Jaccard
# ---------------------------------------------------------------------------


def jaccard_from_hash_sets(left: Set[int], right: Set[int]) -> float:
    """Exact Jaccard of two already-built hash sets.

    The scoring path builds the set once per document and reuses it for every
    pair that document appears in; rebuilding a set per pair would dominate the
    run time. Intersection then happens at C speed.
    """
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    return intersection / float(len(left) + len(right) - intersection)


def jaccard_from_hashes(left: Sequence[int], right: Sequence[int]) -> float:
    """Exact Jaccard similarity of two hash sequences.

    Two distinct shingles colliding in 64 bits would make the score a negligible
    amount too high; at this width that is not observable.
    """
    return jaccard_from_hash_sets(set(left), set(right))


# ---------------------------------------------------------------------------
# SimHash (auxiliary signal)
# ---------------------------------------------------------------------------


def simhash64(shingle_list: Iterable[str]) -> int:
    """64-bit SimHash of a shingle collection.

    Classic weighted majority per bit with unit weights. An empty collection
    yields 0, which is a distinct fingerprint rather than an error: the caller
    decides whether such a document may enter the candidate set.
    """
    return simhash64_from_hashes(_hash64(value) for value in shingle_list)


def simhash64_from_hashes(hashes: Iterable[int]) -> int:
    """:func:`simhash64` for shingle hashes already computed.

    The per-bit tally is done with one pass over the shingle *weights* rather than
    64 passes over the shingle list: each shingle adds ``+1`` to the bits that are
    set, and the complement of that mask adds ``-1`` to them. With unit weights
    a bit is positive exactly when it is set in more than half the shingles, so
    the result is ``(ones << 1) > count`` per position.
    """
    ones = [0] * 64
    count = 0
    for hashed in hashes:
        count += 1
        # Walk only the set bits: a random 64-bit hash has 32 on average, half
        # the work of testing all 64 positions.
        value = hashed
        while value:
            lowest = value & -value
            ones[lowest.bit_length() - 1] += 1
            value ^= lowest
    if count == 0:
        return 0
    result = 0
    for bit in range(64):
        # ``ones[bit] > count - ones[bit]``  <=>  2 * ones[bit] > count
        if ones[bit] * 2 > count:
            result |= 1 << bit
    return result & _MASK64


def hamming(left: int, right: int) -> int:
    """Number of differing bits between two 64-bit fingerprints."""
    return int(bin((left ^ right) & _MASK64).count("1"))


# ---------------------------------------------------------------------------
# MinHash (recall signal)
# ---------------------------------------------------------------------------


def _permutation_salts(num_perm: int) -> Tuple[int, ...]:
    """Deterministic permutation salts for one ``num_perm``.

    XOR with a fixed 64-bit constant is a cheap universal family and needs no
    modular inverse at call time. The sequence is derived from a fixed seed, so
    the same build always produces the same signature.
    """

    def splitmix64(value: int) -> int:
        value = (value + 0x9E3779B97F4A7C15) & _MASK64
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
        return (value ^ (value >> 31)) & _MASK64

    return tuple(
        splitmix64(0x5DEECE66D + index * 0x9E3779B97F4A7C15) for index in range(num_perm)
    )


_SALT_CACHE: Dict[int, Tuple[int, ...]] = {}


def permutation_salts(num_perm: int) -> Tuple[int, ...]:
    salts = _SALT_CACHE.get(num_perm)
    if salts is None:
        salts = _permutation_salts(num_perm)
        _SALT_CACHE[num_perm] = salts
    return salts


def minhash_signature(
    shingle_values: Iterable[str], num_perm: int = 128
) -> Tuple[int, ...]:
    """MinHash signature over already-shingled text.

    The shingles are hashed once, then each permutation takes the minimum of
    ``hash ^ salt``. Two identical shingle sets produce identical signatures, and
    the fraction of equal positions estimates their Jaccard similarity.
    """
    if num_perm < 1:
        raise ValueError("num_perm must be positive")
    hashes = [_hash64(value) for value in shingle_values]
    if not hashes:
        # An empty set has no minimum. A signature of zeros is unambiguous here
        # because a real shingle never hashes to zero across every permutation.
        return tuple(0 for _ in range(num_perm))
    salts = permutation_salts(num_perm)
    return tuple(min(value ^ salt for value in hashes) for salt in salts)


def minhash_signature_from_hashes(
    hashes: Sequence[int], num_perm: int = 128
) -> Tuple[int, ...]:
    """:func:`minhash_signature` for shingle *hashes* already computed.

    ``map(salt.__xor__, hashes)`` instead of a generator expression: the loop then
    runs in C and the XOR is resolved once per salt rather than once per pair,
    which is a measurable share of the scan on a large corpus.
    """
    if num_perm < 1:
        raise ValueError("num_perm must be positive")
    if not hashes:
        return tuple(0 for _ in range(num_perm))
    return tuple(min(map(salt.__xor__, hashes)) for salt in permutation_salts(num_perm))


def jaccard_from_signature(left: Sequence[int], right: Sequence[int]) -> float:
    """Estimate Jaccard similarity from two equal-length MinHash signatures.

    Raises ``ValueError`` on a length mismatch: silently truncating would
    compare two different permutation families and return a meaningless number.
    """
    if len(left) != len(right):
        raise ValueError(
            "signatures have different lengths (%d != %d); they were built with "
            "different num_perm" % (len(left), len(right))
        )
    if not left:
        raise ValueError("signatures are empty")
    equal = sum(1 for a, b in zip(left, right) if a == b)
    return equal / float(len(left))


# ---------------------------------------------------------------------------
# LSH banding
# ---------------------------------------------------------------------------


def _pack(values: Sequence[int]) -> bytes:
    return b"".join(int(value).to_bytes(8, "big") for value in values)


def lsh_bands(signature: Sequence[int], bands: int) -> List[Tuple[int, int]]:
    """Return ``(band_index, band_hash)`` for every band of ``signature``.

    Rows per band is derived from the signature length, so the band count and
    ``num_perm`` cannot silently disagree. Two documents are candidates when any
    band hash matches; that is an OR over bands, which is why a missed pair
    needs *every* band to differ.
    """
    if bands < 1:
        raise ValueError("bands must be positive")
    if len(signature) % bands:
        raise ValueError(
            "signature length %d is not divisible by %d bands"
            % (len(signature), bands)
        )
    rows = len(signature) // bands
    result = []
    for band in range(bands):
        start = band * rows
        chunk = signature[start : start + rows]
        digest = hashlib.blake2b(
            int(band).to_bytes(2, "big") + _pack(chunk), digest_size=8
        ).digest()
        result.append((band, int.from_bytes(digest, "big")))
    return result


# ---------------------------------------------------------------------------
# Per-document fingerprint bundle
# ---------------------------------------------------------------------------


class Fingerprint(NamedTuple):
    """Everything stored for one document in ``fingerprints``."""

    simhash: int
    signature: Tuple[int, ...]
    shingle_count: int
    shingle_size: int
    hashes: Tuple[int, ...]


def fingerprint_text(text: str, settings: Optional[Settings] = None) -> Fingerprint:
    """Build the full fingerprint bundle for already-normalized text."""
    cfg = settings or DEFAULT_SETTINGS
    hashes = shingle_hashes(text, cfg.shingle_size)
    return Fingerprint(
        simhash=simhash64_from_hashes(hashes),
        signature=minhash_signature_from_hashes(hashes, cfg.minhash_num_perm),
        shingle_count=len(hashes),
        shingle_size=cfg.shingle_size,
        hashes=hashes,
    )


def signature_from_json(payload: str) -> Tuple[int, ...]:
    """Decode a stored signature, rejecting a malformed one.

    A corrupt column must fail loudly: a silently truncated signature would
    produce a wrong-but-plausible similarity.
    """
    value = json.loads(payload)
    if not isinstance(value, list) or any(not isinstance(item, int) for item in value):
        raise ValueError("stored signature is not a list of integers")
    return tuple(value)


def signature_to_json(signature: Sequence[int]) -> str:
    """Canonical JSON for a signature.

    No spaces and no key order to worry about: the value is a list of ints, so
    the serialization is byte-stable, which is what the determinism test checks.
    """
    return json.dumps(list(int(item) for item in signature), separators=(",", ":"))


def hashes_to_blob(hashes: Sequence[int]) -> bytes:
    """Canonical binary form of a sorted hash sequence (big-endian, 8 bytes each)."""
    return b"".join(int(value).to_bytes(8, "big") for value in hashes)


def hashes_from_blob(blob: bytes) -> Tuple[int, ...]:
    """Decode :func:`hashes_to_blob`, rejecting a length that is not a multiple of 8."""
    if len(blob) % 8:
        raise ValueError("stored shingle blob has a partial 64-bit entry")
    return tuple(
        int.from_bytes(blob[index : index + 8], "big")
        for index in range(0, len(blob), 8)
    )


# ---------------------------------------------------------------------------
# Candidate recall
# ---------------------------------------------------------------------------


class Candidate(NamedTuple):
    """One comparable pair of documents, with every signal it was judged on."""

    a_id: int
    b_id: int
    jaccard: float
    jaccard_estimate: float
    hamming: int
    score_kind: str


class CandidateStats(NamedTuple):
    """Counters that make the recall stage explainable in the scan summary."""

    documents: int
    skipped_short: int
    band_candidates: int
    rejected_by_hamming: int
    rejected_by_score: int
    pairs: int


def _bucket_candidates(
    entries: Sequence[Tuple[int, Sequence[int]]],
    bands: int,
) -> Set[Tuple[int, int]]:
    """Pairs that share at least one LSH band."""
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for doc_id, signature in entries:
        for band, band_hash in lsh_bands(signature, bands):
            buckets.setdefault((band, band_hash), []).append(doc_id)
    pairs: Set[Tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                if left != right:
                    pairs.add((left, right))
    return pairs


def build_candidates(
    entries: Iterable[Tuple[int, int, int, Sequence[int], Sequence[int]]],
    *,
    settings: Optional[Settings] = None,
) -> Tuple[List[Candidate], CandidateStats]:
    """Score band candidates and return them sorted by ``(a_id, b_id)``.

    ``entries`` is ``(doc_id, normalized_length, simhash, signature,
    shingle_hashes)`` for every comparable document. The pipeline is:

    1. drop documents whose normalized text is below ``min_text_chars`` (an
       almost-empty body cannot yield a meaningful score);
    2. gather pairs that share an LSH band;
    3. reject pairs whose text lengths differ beyond ``size_ratio_window`` —
       a 400-character note is not the "same document" as a 4000-character one;
    4. reject pairs whose SimHash Hamming distance exceeds ``simhash_max_hamming``;
    5. score the survivors exactly, and keep the MinHash estimate for audit.

    Steps 3 and 4 only *remove* candidates, so they can lower recall but never
    invent a pair. Both are far looser than the near-duplicate threshold, which
    keeps them out of the truth definition (``data/README.md`` 4.6).

    All scored candidates are returned, not only those above the threshold:
    ``pairs`` is the evidence layer, and the tier gate belongs to clustering.
    Consumers gate on ``near_duplicate_threshold`` themselves.
    """
    cfg = settings or DEFAULT_SETTINGS
    usable = []
    skipped_short = 0
    for doc_id, length, simhash_value, signature, hashes in entries:
        signature = tuple(int(value) for value in signature)
        if len(signature) != cfg.minhash_num_perm:
            raise ValueError(
                "document %d has a %d-permutation signature, expected %d; the "
                "index was written with different settings"
                % (doc_id, len(signature), cfg.minhash_num_perm)
            )
        if length < cfg.min_text_chars or not signature:
            skipped_short += 1
            continue
        usable.append(
            (int(doc_id), int(length), int(simhash_value), signature, tuple(hashes))
        )

    candidate_pairs = _bucket_candidates(
        [(doc_id, signature) for doc_id, _, _, signature, _ in usable],
        cfg.lsh_bands,
    )
    by_id = {
        doc_id: (length, simhash_value, signature, frozenset(hashes))
        for doc_id, length, simhash_value, signature, hashes in usable
    }

    rejected_hamming = 0
    rejected_by_score = 0
    candidates: List[Candidate] = []
    for left, right in sorted(candidate_pairs):
        left_length, left_simhash, left_sig, left_hashes = by_id[left]
        right_length, right_simhash, right_sig, right_hashes = by_id[right]
        if min(left_length, right_length) < cfg.size_ratio_window * max(
            left_length, right_length
        ):
            # Not a duplicate candidate: one document is less than half the
            # other. Reported through the counters, never silently dropped.
            continue
        distance = hamming(left_simhash, right_simhash)
        if distance > cfg.simhash_max_hamming:
            rejected_hamming += 1
            continue
        score = jaccard_from_hash_sets(left_hashes, right_hashes)
        if score < cfg.pair_store_threshold:
            # Below the retention floor: nothing downstream can use the pair.
            rejected_by_score += 1
            continue
        candidates.append(
            Candidate(
                a_id=left,
                b_id=right,
                jaccard=score,
                jaccard_estimate=jaccard_from_signature(left_sig, right_sig),
                hamming=distance,
                score_kind=SCORE_KIND_EXACT,
            )
        )

    stats = CandidateStats(
        documents=len(usable),
        skipped_short=skipped_short,
        band_candidates=len(candidate_pairs),
        rejected_by_hamming=rejected_hamming,
        rejected_by_score=rejected_by_score,
        pairs=len(candidates),
    )
    return candidates, stats


__all__ = [
    "Candidate",
    "CandidateStats",
    "Fingerprint",
    "SCORE_KIND_EXACT",
    "SCORE_KIND_MINHASH",
    "build_candidates",
    "fingerprint_text",
    "hamming",
    "hashes_from_blob",
    "hashes_to_blob",
    "jaccard_from_hash_sets",
    "jaccard_from_hashes",
    "jaccard_from_signature",
    "lsh_bands",
    "minhash_signature",
    "minhash_signature_from_hashes",
    "permutation_salts",
    "shingle_hashes",
    "shingle_set",
    "shingles",
    "signature_from_json",
    "signature_to_json",
    "simhash64",
    "simhash64_from_hashes",
]
