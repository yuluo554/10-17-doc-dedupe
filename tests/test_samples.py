"""Guards on the synthetic corpus and its truth manifest.

These tests protect the *evidence*, not the code. A benchmark is worthless if the
truth file can drift, if the samples are not reproducible, or if the "noise"
corpus quietly becomes a set of near-duplicates. Each assertion below fails on a
specific, plausible mistake rather than restating the generator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import gen_samples

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
MANIFEST = SAMPLES / "truth" / "manifest.json"

pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(), reason="sample corpus is not generated"
)

# plan/04 section 2.2: the ten forms the corpus must cover.
REQUIRED_FORMS = {
    "rewrite": "改写版",
    "truncate": "截断版",
    "reformat": "换格式版",
    "exact": "完全重复",
    "template": "模板族",
    "no_text_layer": "无文本层 PDF",
    "encrypted": "加密 PDF",
    "fullwidth": "全半角混排",
    "header_footer": "页眉页脚与页码",
    "negative": "无关文档（负样本）",
}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Truth discipline (K11)
# ---------------------------------------------------------------------------


def test_manifest_has_no_observed_fields():
    """Truth records the input and the expectation, never a measured value."""
    text = MANIFEST.read_text(encoding="utf-8")
    assert "observed_" not in text, "manifest contains an observed_* field"
    assert "similarity" not in text.lower()
    assert "jaccard" not in text.lower()
    assert "score" not in text.lower()


def test_manifest_never_records_a_number_that_looks_like_a_score():
    """A float in the truth file would mean a score leaked in under another name."""
    manifest = _manifest()

    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from walk(value, "%s.%s" % (path, key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, "%s[%d]" % (path, index))
        else:
            yield path, node

    floats = [(path, value) for path, value in walk(manifest) if isinstance(value, float)]
    assert floats == [], "manifest contains float values: %s" % floats


def test_tiers_are_inside_the_documented_enum():
    manifest = _manifest()
    assert manifest["groups"]
    for group in manifest["groups"]:
        assert group["tier"] in gen_samples.TIER_VALUES, group
    # All four tiers must actually occur; a corpus with no negatives would let a
    # "zero false positives" claim pass without ever testing one.
    assert {group["tier"] for group in manifest["groups"]} == set(gen_samples.TIER_VALUES)


def test_variants_are_inside_the_documented_enum():
    manifest = _manifest()
    seen = set()
    for group in manifest["groups"]:
        seen.add(group["variant"])
        for member in group["members"]:
            seen.add(member["variant"])
    for item in manifest["skipped"]:
        seen.add(item["variant"])
    assert seen <= set(gen_samples.VARIANT_VALUES), seen - set(gen_samples.VARIANT_VALUES)


def test_skipped_records_carry_an_extract_status():
    manifest = _manifest()
    statuses = {item["extract_status"] for item in manifest["skipped"]}
    assert statuses == {"skipped:no_text_layer", "skipped:encrypted"}
    for item in manifest["unsupported"]:
        assert item["extract_status"] == "skipped:unsupported"


def test_every_group_base_is_one_of_its_members():
    manifest = _manifest()
    for group in manifest["groups"]:
        assert group["base"] in group["files"], group["group_id"]
        assert group["files"], group["group_id"]


def test_every_negative_group_has_exactly_one_member():
    """A negative group with two members is a duplicate the engine must find.

    That is not a negative any more, and it would make the false-positive check
    unsatisfiable by design.
    """
    manifest = _manifest()
    for group in manifest["groups"]:
        if group["tier"] == "negative":
            assert len(group["files"]) == 1, group["group_id"]


# ---------------------------------------------------------------------------
# Coverage (plan/04 section 2.2)
# ---------------------------------------------------------------------------


def test_corpus_covers_every_required_form():
    manifest = _manifest()
    present = set()
    for group in manifest["groups"]:
        present.add(group["tier"])
        for member in group["members"]:
            present.add(member["variant"])
    for item in manifest["skipped"]:
        present.add(item["variant"])

    missing = {name for name in REQUIRED_FORMS if name not in present}
    assert missing == set(), "corpus is missing required forms: %s" % sorted(missing)


def test_cross_format_group_contains_all_five_formats():
    manifest = _manifest()
    formats = set()
    for group in manifest["groups"]:
        if group["group_id"] == "G005":
            formats = {Path(name).suffix.lower() for name in group["files"]}
    assert formats == {".docx", ".md", ".txt", ".html", ".pdf"}, formats


def test_twenty_near_groups_exist_for_the_recall_benchmark():
    """data/README.md 4.7 defines recall over 20 constructed groups."""
    manifest = _manifest()
    near = [group for group in manifest["groups"] if group["tier"] == "near"]
    assert len(near) >= 20, "recall benchmark needs at least 20 near groups, found %d" % len(near)


def test_every_near_group_has_at_least_two_members():
    manifest = _manifest()
    for group in manifest["groups"]:
        if group["tier"] == "near":
            assert len(group["files"]) >= 2, group["group_id"]


# ---------------------------------------------------------------------------
# Files on disk
# ---------------------------------------------------------------------------


def test_every_manifest_reference_exists_on_disk():
    manifest = _manifest()
    referenced = []
    for group in manifest["groups"]:
        referenced.extend(group["files"])
    referenced.extend(item["file"] for item in manifest["skipped"])
    referenced.extend(item["file"] for item in manifest["unsupported"])

    missing = [name for name in referenced if not (SAMPLES / name).is_file()]
    assert missing == [], "manifest references missing files: %s" % missing


def test_no_document_file_on_disk_is_missing_from_the_manifest():
    """The complement of the previous test: an unreferenced sample is a blind spot."""
    manifest = _manifest()
    referenced = set()
    for group in manifest["groups"]:
        referenced.update(group["files"])
    referenced.update(item["file"] for item in manifest["skipped"])
    referenced.update(item["file"] for item in manifest["unsupported"])

    on_disk = {
        path.relative_to(SAMPLES).as_posix()
        for path in SAMPLES.rglob("*")
        if path.is_file() and path.relative_to(SAMPLES).as_posix() != "truth/manifest.json"
    }
    assert on_disk == referenced, "unreferenced: %s; missing: %s" % (
        sorted(on_disk - referenced),
        sorted(referenced - on_disk),
    )


def test_manifest_counts_match_reality():
    manifest = _manifest()
    counts = manifest["counts"]
    assert counts["groups"] == len(manifest["groups"])

    files = set()
    for group in manifest["groups"]:
        files.update(group["files"])
    files.update(item["file"] for item in manifest["skipped"])
    files.update(item["file"] for item in manifest["unsupported"])
    assert counts["files"] == len(files)

    tier_totals = {name: 0 for name in gen_samples.TIER_VALUES}
    for group in manifest["groups"]:
        tier_totals[group["tier"]] += 1
    assert counts["by_tier"] == tier_totals


def test_no_two_truth_groups_sit_near_the_similarity_threshold():
    """Different truth groups must be separable, not just "below the line".

    The corpus leans on a shared business register, which creates a background
    similarity floor between unrelated documents. That floor is fine as long as
    it stays well clear of the threshold the engine will use, because otherwise
    the M2 recall benchmark would be measuring noise. The margin is expressed
    against the configured threshold rather than a duplicated literal.
    """
    from docdedupe.config import DEFAULT_SETTINGS
    from docdedupe.extract import extract

    def trigrams(text: str) -> set:
        compact = "".join(text.split())
        return {compact[i : i + 3] for i in range(max(0, len(compact) - 2))}

    manifest = _manifest()
    representatives = {}
    for group in manifest["groups"]:
        for member in group["members"]:
            if member["variant"] != "original":
                continue
            text = extract(SAMPLES / member["file"]).text
            if text:
                representatives[group["group_id"]] = trigrams(text)
            break

    assert len(representatives) >= 20

    threshold = DEFAULT_SETTINGS.near_duplicate_threshold
    margin = 0.15
    names = sorted(representatives)
    offenders = []
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            a, b = representatives[left], representatives[right]
            if not a or not b:
                continue
            score = len(a & b) / len(a | b)
            if score >= threshold - margin:
                offenders.append((left, right, round(score, 3)))
    assert offenders == [], (
        "unrelated truth groups approach the %.2f threshold: %s" % (threshold, offenders)
    )


def test_negatives_have_distinct_shapes_not_one_shared_pool():
    """Each negative is written in its own voice; a shared pool would be a blind spot."""
    from docdedupe.extract import extract

    manifest = _manifest()
    texts = [
        extract(SAMPLES / group["files"][0]).text
        for group in manifest["groups"]
        if group["tier"] == "negative"
    ]
    assert len(texts) >= 5
    openers = {text.splitlines()[0] for text in texts}
    assert len(openers) >= 3, "negatives share openers: %s" % sorted(openers)


def test_sample_files_are_not_empty():
    manifest = _manifest()
    for path in SAMPLES.rglob("*"):
        if path.is_file():
            assert path.stat().st_size > 0, "%s is empty" % path


def test_no_real_entity_appears_in_the_corpus():
    """Whitelist enforcement belongs on the artefacts, not only in the generator."""
    from gen_samples import _assert_entities_whitelisted

    for path in sorted(SAMPLES.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".docx", ".pdf", ".doc", ".rtf"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        _assert_entities_whitelisted(text)


# ---------------------------------------------------------------------------
# Reproducibility (the --check gate)
# ---------------------------------------------------------------------------


def test_regeneration_is_byte_identical_even_across_a_clock_tick(tmp_path):
    """The clock must not reach the artefacts.

    ``--check`` compares against the committed tree; this test isolates the
    generator itself so a future timestamp regression fails with a clear name
    instead of "the samples are dirty".
    """
    first = gen_samples.full_corpus()
    second = gen_samples.full_corpus()
    assert first == second

    gen_samples.write_corpus(tmp_path, first)
    on_disk = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert on_disk == first


def test_docx_writer_is_deterministic(tmp_path):
    import io
    import time

    import docxutil
    from docx import Document

    def build() -> bytes:
        document = Document()
        docxutil.pin_core_properties(document, title="示例项目甲")
        document.add_paragraph("示例项目甲 技术方案")
        buffer = io.BytesIO()
        document.save(buffer)
        return docxutil.normalise_zip(buffer.getvalue())

    first = build()
    time.sleep(0.05)
    second = build()
    assert first == second


def test_pdf_writers_are_deterministic():
    import pdfutil

    pages = [["示例项目甲", "正文第一段。"]]
    assert pdfutil.build_text_pdf(pages) == pdfutil.build_text_pdf(pages)
    assert pdfutil.build_no_text_pdf(1) == pdfutil.build_no_text_pdf(1)
    assert pdfutil.build_encrypted_pdf(pages) == pdfutil.build_encrypted_pdf(pages)


def test_check_detects_a_tampered_sample(tmp_path):
    """The frozen gate must fail on a one-byte change, not just run."""
    gen_samples.write_corpus(tmp_path, gen_samples.full_corpus())
    assert gen_samples.check(tmp_path) == 0

    victim = next(
        path for path in sorted(tmp_path.rglob("*.md")) if path.name.startswith("G00")
    )
    victim.write_bytes(victim.read_bytes() + b"\n")
    assert gen_samples.check(tmp_path) == 1


def test_check_detects_a_deleted_sample(tmp_path):
    gen_samples.write_corpus(tmp_path, gen_samples.full_corpus())
    victim = sorted(tmp_path.rglob("*.txt"))[0]
    victim.unlink()
    assert gen_samples.check(tmp_path) == 1


def test_check_detects_an_unexpected_file(tmp_path):
    gen_samples.write_corpus(tmp_path, gen_samples.full_corpus())
    (tmp_path / "G001_项目方案" / "stray.md").write_text("多余文件", encoding="utf-8")
    assert gen_samples.check(tmp_path) == 1


def test_committed_corpus_is_frozen():
    """The committed tree must equal a fresh generation, byte for byte."""
    assert gen_samples.check(SAMPLES) == 0


def test_mutation_injector_refuses_a_no_op():
    """``_require_change`` is the guard against silent str.replace misses."""
    with pytest.raises(AssertionError):
        gen_samples._require_change("原文", "原文", "noop")
    gen_samples._require_change("原文", "新文", "changed")


def test_rewrite_variant_actually_differs_from_its_base():
    corpus = gen_samples.build_corpus()
    for group in corpus.groups:
        if group["variant"] != "original":
            continue
        base = corpus.files[group["base"]]
        for member in group["members"]:
            if member["variant"] != "rewrite":
                continue
            assert corpus.files[member["file"]] != base, member["file"]


def test_fullwidth_variant_is_nfkc_equivalent_to_its_base():
    """The pair exists to prove normalization, so it must survive NFKC."""
    import unicodedata

    corpus = gen_samples.build_corpus()
    group = next(item for item in corpus.groups if item["group_id"] == "G010")
    plain = next(m for m in group["members"] if m["variant"] == "original")
    wide = next(m for m in group["members"] if m["variant"] == "fullwidth" and m["file"].endswith(".txt"))

    def text_of(rel: str) -> str:
        return corpus.files[rel].decode("utf-8")

    assert text_of(wide["file"]) != text_of(plain["file"])
    assert (
        unicodedata.normalize("NFKC", text_of(wide["file"]))
        == unicodedata.normalize("NFKC", text_of(plain["file"]))
    )


def test_bulk_corpus_is_generated_on_demand_and_not_committed():
    bulk = gen_samples.build_bulk_corpus(gen_samples.DEFAULT_SEED, 20)
    assert len(bulk) == 20
    assert all(name.startswith("bulk/") for name in bulk)
    committed = {path.relative_to(SAMPLES).as_posix() for path in SAMPLES.rglob("*")}
    assert not any(name.startswith("bulk/") for name in committed)
