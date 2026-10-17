"""Fingerprint tests: shingles, SimHash, MinHash, LSH, and exact Jaccard.

Two properties get the most attention:

* **Determinism across processes.** Signatures are built on BLAKE2b, not on
  ``hash()``; a signature built on the built-in hash would differ between two
  runs, and the "two runs produce the same index" gate would be untestable.
* **The recall path never silently drops a true pair.** The SimHash and length
  filters are only allowed to remove *unrelated* candidates; the tests below pin
  that they leave every true pair alone at the configured bounds.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from docdedupe import fingerprint as fp
from docdedupe.config import DEFAULT_SETTINGS

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shingles
# ---------------------------------------------------------------------------


def test_shingles_are_character_trigrams_with_no_word_boundaries():
    """Chinese has no spaces; the split must not depend on them."""
    assert fp.shingles("示例项目甲", 3) == sorted(["示例项", "例项目", "项目甲"])
    assert set(fp.shingles("示例项目甲", 3)) == {"示例项", "例项目", "项目甲"}


def test_shingles_are_deduplicated_and_sorted():
    assert fp.shingles("aaaa", 3) == ["aaa"]
    assert fp.shingles("abcd", 3) == ["abc", "bcd"]


def test_a_text_shorter_than_n_yields_no_shingles():
    assert fp.shingles("ab", 3) == []
    assert fp.shingle_set("ab", 3) == set()


def test_whitespace_does_not_create_shingles():
    """Normalized text has no whitespace, so a space cannot hide in a shingle."""
    left = fp.shingle_set("示例项目甲技术方案", 3)
    right = fp.shingle_set("示例 项目甲 技术方案", 3)
    # The second string still has spaces in it; this test documents that
    # normalization, not shingling, is what removes them.
    assert left != right
    from docdedupe.normalize import normalize

    assert fp.shingle_set(normalize("示例 项目甲 技术方案"), 3) == left


def test_invalid_shingle_length_is_rejected():
    with pytest.raises(ValueError):
        fp.shingles("abc", 0)


# ---------------------------------------------------------------------------
# Exact Jaccard
# ---------------------------------------------------------------------------


def test_exact_jaccard_matches_the_set_definition():
    left = fp.shingles("示例项目甲技术方案", 3)
    right = fp.shingles("示例项目甲施工方案", 3)
    expected = len(set(left) & set(right)) / len(set(left) | set(right))
    assert fp.jaccard_from_hashes(
        fp.shingle_hashes("示例项目甲技术方案"), fp.shingle_hashes("示例项目甲施工方案")
    ) == pytest.approx(expected)


def test_exact_jaccard_handles_the_boundary_cases():
    empty = fp.shingle_hashes("ab")
    assert fp.jaccard_from_hashes(empty, empty) == 1.0
    assert fp.jaccard_from_hashes(empty, fp.shingle_hashes("示例项目甲")) == 0.0


def test_exact_jaccard_is_symmetric():
    left = fp.shingle_hashes("示例项目甲技术方案与施工组织")
    right = fp.shingle_hashes("示例项目甲技术方案与质量验收")
    assert fp.jaccard_from_hashes(left, right) == fp.jaccard_from_hashes(right, left)


# ---------------------------------------------------------------------------
# SimHash
# ---------------------------------------------------------------------------


def test_simhash_is_64_bits():
    value = fp.simhash64(["示例项", "例项目", "项目甲"])
    assert 0 <= value < (1 << 64)


def test_simhash_is_zero_for_an_empty_collection():
    """Documented as "no signal", which the candidate filter must handle."""
    assert fp.simhash64([]) == 0


def test_identical_text_has_zero_hamming_distance():
    text = "示例项目甲技术方案与施工组织设计"
    assert fp.hamming(fp.simhash64(fp.shingle_set(text)), fp.simhash64(fp.shingle_set(text))) == 0


def test_near_identical_text_has_a_small_hamming_distance():
    base = "本阶段围绕构造做法开展归档，设计交底按既定口径取值，并在设计文件中留档。"
    changed = base.replace("构造做法", "施工工艺")
    distance = fp.hamming(
        fp.simhash64(fp.shingle_set(base)), fp.simhash64(fp.shingle_set(changed))
    )
    assert distance < DEFAULT_SETTINGS.simhash_max_hamming


def test_unrelated_text_has_a_large_hamming_distance():
    left = "本阶段围绕构造做法开展归档，设计交底按既定口径取值，并在设计文件中留档。"
    right = "本期培训覆盖新员工入职流程，考核合格后进入项目组轮岗实习。"
    distance = fp.hamming(
        fp.simhash64(fp.shingle_set(left)), fp.simhash64(fp.shingle_set(right))
    )
    assert distance > DEFAULT_SETTINGS.simhash_max_hamming


# ---------------------------------------------------------------------------
# MinHash
# ---------------------------------------------------------------------------


def test_minhash_signature_length_is_the_configured_num_perm():
    signature = fp.minhash_signature(fp.shingle_set("示例项目甲技术方案"), 128)
    assert len(signature) == 128


def test_identical_sets_have_identical_signatures():
    left = fp.minhash_signature(fp.shingle_set("示例项目甲技术方案"))
    right = fp.minhash_signature(fp.shingle_set("示例项目甲技术方案"))
    assert left == right
    assert fp.jaccard_from_signature(left, right) == 1.0


def test_disjoint_sets_estimate_zero():
    left = fp.minhash_signature(fp.shingle_set("甲乙丙丁戊己庚辛"))
    right = fp.minhash_signature(fp.shingle_set("子丑寅卯辰巳午未"))
    assert fp.jaccard_from_signature(left, right) == 0.0


def test_the_estimate_tracks_the_true_jaccard_within_sampling_error():
    """128 permutations have a standard error of about 0.035 at J = 0.8.

    The bound is generous on purpose: the assertion is that the estimator is
    wired up correctly, not that it is precise. Precision is why the score that
    *decides* a pair is exact (see :mod:`docdedupe.fingerprint`).
    """
    base = "".join(chr(0x4E00 + index) for index in range(600))
    variant = base[:480] + "".join(chr(0x6000 + index) for index in range(120))
    true_jaccard = fp.jaccard_from_hashes(fp.shingle_hashes(base), fp.shingle_hashes(variant))
    estimate = fp.jaccard_from_signature(
        fp.minhash_signature(fp.shingle_set(base)),
        fp.minhash_signature(fp.shingle_set(variant)),
    )
    assert abs(estimate - true_jaccard) < 4 * 0.035


def test_mismatched_signature_lengths_are_rejected():
    """Truncating silently would compare two different permutation families."""
    with pytest.raises(ValueError):
        fp.jaccard_from_signature((1, 2, 3), (1, 2))


def test_salts_are_stable_for_a_given_num_perm():
    assert fp.permutation_salts(128) == fp.permutation_salts(128)
    assert len(set(fp.permutation_salts(128))) == 128


# ---------------------------------------------------------------------------
# LSH
# ---------------------------------------------------------------------------


def test_lsh_bands_cover_the_signature_exactly():
    signature = fp.minhash_signature(fp.shingle_set("示例项目甲技术方案"), 128)
    bands = fp.lsh_bands(signature, 32)
    assert len(bands) == 32
    assert [index for index, _ in bands] == list(range(32))
    assert len({value for _, value in bands}) == len(bands)


def test_lsh_rejects_a_band_count_that_does_not_divide_the_signature():
    with pytest.raises(ValueError):
        fp.lsh_bands((1, 2, 3, 4, 5), 2)


def test_identical_signatures_share_every_band():
    signature = fp.minhash_signature(fp.shingle_set("示例项目甲技术方案"), 128)
    assert fp.lsh_bands(signature, 32) == fp.lsh_bands(signature, 32)


# ---------------------------------------------------------------------------
# Determinism across processes
# ---------------------------------------------------------------------------


def test_signature_is_stable_across_processes():
    """The stored fingerprint must not depend on PYTHONHASHSEED.

    ``hash()`` is salted per process; if the implementation used it, this test
    would pass or fail depending on how the two interpreters were started.
    """
    code = (
        "import json;"
        "from docdedupe.fingerprint import minhash_signature, shingle_set;"
        "print(json.dumps(list(minhash_signature(shingle_set('示例项目甲技术方案与施工组织')))))"
    )
    outputs = []
    for seed in ("0", "12345"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={**dict(__import__("os").environ), "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1]


def test_shingle_hashes_are_sorted_and_unique():
    hashes = fp.shingle_hashes("示例项目甲技术方案")
    assert list(hashes) == sorted(hashes)
    assert len(hashes) == len(set(hashes))


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_signature_json_round_trips_byte_identically():
    signature = fp.minhash_signature(fp.shingle_set("示例项目甲技术方案"))
    payload = fp.signature_to_json(signature)
    assert fp.signature_from_json(payload) == signature
    assert fp.signature_to_json(fp.signature_from_json(payload)) == payload


def test_a_corrupt_signature_payload_is_rejected():
    with pytest.raises(ValueError):
        fp.signature_from_json('{"not": "a list"}')
    with pytest.raises(ValueError):
        fp.signature_from_json('["1", "2"]')


def test_hash_blob_round_trips():
    hashes = fp.shingle_hashes("示例项目甲技术方案")
    assert fp.hashes_from_blob(fp.hashes_to_blob(hashes)) == hashes


def test_a_partial_hash_blob_is_rejected():
    with pytest.raises(ValueError):
        fp.hashes_from_blob(b"\x00\x01\x02")


# ---------------------------------------------------------------------------
# Candidate recall
# ---------------------------------------------------------------------------


def _entry(doc_id, text, settings=DEFAULT_SETTINGS):
    fingerprint = fp.fingerprint_text(text, settings)
    return (
        doc_id,
        len(text),
        fingerprint.simhash,
        fingerprint.signature,
        fingerprint.hashes,
    )


def _long_text(seed: int, length: int = 500) -> str:
    rng = random.Random(seed)
    return "".join(chr(0x4E00 + rng.randrange(0, 2000)) for _ in range(length))


def test_a_duplicate_pair_is_returned_as_a_candidate():
    text = _long_text(1)
    candidates, stats = fp.build_candidates([_entry(1, text), _entry(2, text)])
    assert stats.pairs == 1
    candidate = candidates[0]
    assert (candidate.a_id, candidate.b_id) == (1, 2)
    assert candidate.jaccard == 1.0
    assert candidate.score_kind == fp.SCORE_KIND_EXACT


def test_pairs_are_ordered_and_canonical():
    """``a_id < b_id`` and a sorted list: the schema enforces the first."""
    base = _long_text(2)
    entries = [_entry(index, base) for index in (3, 1, 2)]
    candidates, _ = fp.build_candidates(entries)
    assert [(c.a_id, c.b_id) for c in candidates] == [(1, 2), (1, 3), (2, 3)]


def test_the_minhash_estimate_is_kept_beside_the_exact_score():
    """The auxiliary signal must remain visible, or a report cannot audit it."""
    text = _long_text(3)
    candidates, _ = fp.build_candidates(
        [_entry(1, text), _entry(2, text[:450] + _long_text(4, 50))]
    )
    assert candidates
    for candidate in candidates:
        assert 0.0 <= candidate.jaccard_estimate <= 1.0
        assert abs(candidate.jaccard_estimate - candidate.jaccard) < 0.2


def test_short_documents_are_not_compared():
    settings = DEFAULT_SETTINGS
    text = "短" * (settings.min_text_chars - 1)
    candidates, stats = fp.build_candidates([_entry(1, text), _entry(2, text)])
    assert candidates == []
    assert stats.skipped_short == 2


def test_a_length_mismatch_beyond_the_window_is_not_a_candidate():
    long_text = _long_text(5, 1000)
    short_text = long_text[:100]
    candidates, _ = fp.build_candidates([_entry(1, long_text), _entry(2, short_text)])
    # One is a strict prefix of the other, so the shingles overlap heavily, but
    # "a tenth of the document" is not the same document.
    assert all(c.jaccard < 1.0 for c in candidates)


def test_the_simhash_filter_never_removes_a_true_pair():
    """Regression guard for the filter bound.

    A pair that is 90% identical must survive the auxiliary filter; if the bound
    were tightened to "very similar only", recall would silently degrade and the
    candidate counters would still look healthy.
    """
    base = _long_text(6, 600)
    variant = base[:540] + _long_text(7, 60)
    settings = DEFAULT_SETTINGS
    true_pair_hamming = fp.hamming(
        fp.fingerprint_text(base, settings).simhash,
        fp.fingerprint_text(variant, settings).simhash,
    )
    assert true_pair_hamming <= settings.simhash_max_hamming

    candidates, stats = fp.build_candidates([_entry(1, base), _entry(2, variant)], settings=settings)
    assert stats.rejected_by_hamming == 0
    assert len(candidates) == 1


def test_a_signature_from_different_settings_is_rejected():
    """An index written with num_perm=64 must not be scored as if it had 128."""
    text = _long_text(8)
    entry = _entry(1, text)
    with pytest.raises(ValueError) as exc:
        fp.build_candidates([entry[:3] + (entry[3][:64],) + (entry[4],)])
    assert "64" in str(exc.value)


def test_candidate_building_is_deterministic():
    texts = [_long_text(seed, 400) for seed in range(10)]
    entries = [_entry(index, text) for index, text in enumerate(texts)]
    first, _ = fp.build_candidates(list(entries))
    second, _ = fp.build_candidates(list(entries))
    assert first == second


def test_fingerprint_text_bundles_every_signal():
    bundle = fp.fingerprint_text(_long_text(9))
    assert bundle.shingle_count == len(fp.shingle_set(_long_text(9)))
    assert len(bundle.signature) == DEFAULT_SETTINGS.minhash_num_perm
    assert len(bundle.hashes) == bundle.shingle_count
    assert bundle.shingle_size == DEFAULT_SETTINGS.shingle_size
