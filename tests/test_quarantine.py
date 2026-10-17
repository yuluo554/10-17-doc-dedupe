"""Module 5: quarantine and rollback, against the filesystem.

The claims this file exists to falsify:

* **only moves, never deletes** — every document is compared byte for byte before
  and after, and a batch directory that is left behind is asserted explicitly;
* **the manifest is written before the first move** — a crash injected at the move
  seam still leaves a manifest that names every file, and a re-run finishes the
  job from it;
* **undo verifies before it moves anything** — a tampered quarantine copy or an
  occupied original path aborts the *whole* batch, and the test proves the other
  item did not move either;
* **the destructive switch means "which ones"** — a `--move` list is explicit, and
  members and non-members that were not listed are asserted unchanged.

Everything runs on a tmp corpus built here. ``data/samples`` is frozen, and
quarantining a sample would mutate the corpus the freeze gate checks.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from docdedupe import cli, cluster as cluster_mod, quarantine, store

REPO_ROOT = Path(__file__).resolve().parents[1]

BATCH = "20261017T101500Z-7f3a"
CREATED_AT = "2026-10-17T10:15:00Z"


class _Capture:
    """Capture stdout/stderr around a ``cli.main`` call."""

    def __init__(self) -> None:
        self.out = io.StringIO()
        self.err = io.StringIO()

    def __enter__(self) -> "_Capture":
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout, sys.stderr = self._saved


def run_cli(argv):
    with _Capture() as capture:
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, capture.out.getvalue(), capture.err.getvalue()


def sha256_of(path: Path) -> str:
    """Content hash computed here, not asked of the module under test."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


BODY = "示例项目甲 正文。\n" + "本段用于构成足够长的正文内容。" * 12 + "\n"


@pytest.fixture
def corpus(tmp_path):
    """A scanned+reported corpus with one three-member cluster and an outsider.

    The three duplicates are named ``方案.txt`` in three different directories, so
    they share a base name: the quarantine has to disambiguate them without ever
    overwriting one with another. ``无关.md`` is the file the tests expect to stay
    untouched — it is indexed, it is simply not in the cluster.

    Absolute paths come from the index, not from the report: the report speaks in
    the shortened spellings a human reads, and a test that guessed at them would
    not be testing the same strings the tool acts on.
    """
    root = tmp_path / "docs"
    # Distinct, fixed modification times: the recommendation ranks members by
    # recency, and three files written in one loop can land on the same clock tick.
    # Pinning them keeps "which member is the recommended keep" the same on every
    # machine and removes a real source of test flakiness.
    base_ns = 1_700_000_000_000_000_000
    for index, name in enumerate(("A", "B", "C")):
        (root / name).mkdir(parents=True)
        target = root / name / "方案.txt"
        target.write_text(BODY, encoding="utf-8")
        stamp = base_ns + index * 1_000_000_000
        os.utime(str(target), ns=(stamp, stamp))
    outsider = root / "无关.md"
    outsider.write_text("完全不同的另一份文档。" * 20, encoding="utf-8")
    os.utime(str(outsider), ns=(base_ns, base_ns))

    db_path = tmp_path / "index.db"
    code, out, err = run_cli(["scan", str(root), "--db", str(db_path)])
    assert code == 0, err
    code, out, err = run_cli(["report", "--db", str(db_path), "--format", "json"])
    assert code == 1, err
    payload = json.loads(out)
    assert len(payload["clusters"]) == 1, payload
    reported = payload["clusters"][0]

    conn = store.connect_readonly(str(db_path))
    try:
        rows = [
            row
            for row in store.cluster_rows(conn)
            if str(row["cluster_id"]) == str(reported["cluster_id"])
        ]
    finally:
        conn.close()
    displays = cluster_mod.display_paths([str(row["path_norm"]) for row in rows])
    abs_by_display = {
        display: str(row["path_abs"]) for display, row in zip(displays, rows)
    }
    members = [
        {
            "doc_id": member["doc_id"],
            "path": member["path"],
            "path_abs": abs_by_display[member["path"]],
            "kept": bool(member["kept"]),
        }
        for member in reported["members"]
    ]
    assert len(members) == 3, members
    assert len({member["path_abs"] for member in members}) == 3
    keep = [member for member in members if member["kept"]]
    assert len(keep) == 1, keep
    return {
        "root": root,
        "db": db_path,
        "cluster_id": str(reported["cluster_id"]),
        "keep": keep[0],
        "members": members,
        "outsider": root / "无关.md",
    }


def paths_of(corpus) -> dict:
    """The paths the tests act on: the kept member, the movable ones, all of them.

    others is the natural --move list (the members the index does not
    recommend keeping), and all includes the kept one, which is what an
    "everything else stayed put" assertion needs.
    """
    members = sorted(member["path_abs"] for member in corpus["members"])
    return {
        "keep": corpus["keep"]["path_abs"],
        "all": members,
        "others": [
            member["path_abs"] for member in corpus["members"] if not member["kept"]
        ],
    }


def quarantine_argv(corpus, move, keep=None, batch_id=BATCH, extra=()):
    argv = [
        "quarantine",
        "--db",
        str(corpus["db"]),
        "--cluster",
        corpus["cluster_id"],
        "--keep",
        keep or corpus["keep"]["path_abs"],
        "--batch-id",
        batch_id,
        "--confirm",
    ]
    for path in move:
        argv += ["--move", str(path)]
    return argv + list(extra)


def manifest_path(corpus, batch_id=BATCH) -> Path:
    return corpus["root"] / quarantine.QUARANTINE_DIR_NAME / batch_id / "manifest.json"


def read_manifest(corpus, batch_id=BATCH) -> dict:
    return json.loads(manifest_path(corpus, batch_id).read_text(encoding="utf-8"))


def statuses(manifest) -> dict:
    return {item["original_path"]: item["status"] for item in manifest["items"]}


# ---------------------------------------------------------------------------
# quarantine: the move and the manifest
# ---------------------------------------------------------------------------


def test_the_fixture_has_the_shape_the_tests_below_assume(corpus):
    """The corpus shape is asserted, not assumed.

    ``others`` has to be the two *non-kept* members, because several tests use it
    as "a member that is not the recommendation". That was an implicit assumption
    until a run recommended ``B`` instead of ``C`` (three files can share a clock
    tick, and the recency tie-break then decides whom to keep) and one test failed
    for a reason that had nothing to do with the code under test.
    """
    paths = paths_of(corpus)
    assert Path(corpus["keep"]["path_abs"]).parent.name == "C", "the newest file is the recommendation"
    assert len(paths["all"]) == 3
    assert len(paths["others"]) == 2
    assert paths["keep"] not in paths["others"]
    assert sorted(paths["others"] + [paths["keep"]]) == paths["all"]


def test_quarantine_moves_the_listed_members_and_records_them(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    before = {path: sha256_of(Path(path)) for path in members}
    outsider_before = sha256_of(corpus["outsider"])
    moving = paths["others"]

    code, out, err = run_cli(quarantine_argv(corpus, moving))

    assert code == 0, err
    assert "moved" in out
    manifest = read_manifest(corpus)
    assert set(manifest) == {"schema", "batch_id", "created_at", "cluster_id", "keep", "items"}
    assert manifest["batch_id"] == BATCH
    assert manifest["cluster_id"] == corpus["cluster_id"]
    assert manifest["keep"] == corpus["keep"]["path_abs"]
    assert [item["status"] for item in manifest["items"]] == ["moved", "moved"]

    # Byte-for-byte, not "a file with that name exists": the quarantine copies
    # hash to what the originals hashed to, and the originals are gone.
    for item in manifest["items"]:
        assert not os.path.exists(item["original_path"])
        assert Path(item["quarantine_path"]).is_file()
        assert sha256_of(Path(item["quarantine_path"])) == before[item["original_path"]]
        assert item["sha256"] == before[item["original_path"]]
        assert item["moved_at"]
        assert Path(item["original_path"]).parent.is_dir(), "the empty folder must remain"

    # The kept member and the outsider are untouched.
    assert Path(paths["keep"]).is_file()
    assert sha256_of(Path(paths["keep"])) == before[paths["keep"]]
    assert sha256_of(corpus["outsider"]) == outsider_before

    # The batch directory holds exactly the manifest and the moved files: no
    # temporary manifest file survives the atomic write.
    batch_dir = Path(manifest["items"][0]["quarantine_path"]).parent
    assert sorted(path.name for path in batch_dir.iterdir()) == sorted(
        [quarantine.MANIFEST_NAME]
        + [Path(item["quarantine_path"]).name for item in manifest["items"]]
    )


def test_two_members_with_the_same_name_get_distinct_quarantine_paths(corpus):
    """Nothing is overwritten: a shared base name is disambiguated in the batch."""
    paths = paths_of(corpus)
    members = paths["all"]
    before = {path: sha256_of(Path(path)) for path in members}

    code, out, err = run_cli(quarantine_argv(corpus, paths["others"]))

    assert code == 0, err
    manifest = read_manifest(corpus)
    names = [Path(item["quarantine_path"]).name for item in manifest["items"]]
    assert len(set(names)) == len(names), names
    assert "方案.txt" in names
    assert sorted(names)[1] == "方案__2.txt", names
    for item in manifest["items"]:
        assert sha256_of(Path(item["quarantine_path"])) == before[item["original_path"]]


def test_the_manifest_is_registered_in_the_index_and_that_row_is_only_a_pointer(corpus):
    """The registry says where the manifest is; the manifest says what happened."""
    paths = paths_of(corpus)
    members = paths["all"]
    assert run_cli(quarantine_argv(corpus, paths["others"]))[0] == 0

    conn = store.connect(str(corpus["db"]))
    try:
        row = store.find_quarantine_batch(conn, BATCH)
        assert row is not None
        assert str(row["cluster_id"]) == corpus["cluster_id"]
        assert str(row["keep_path"]) == corpus["keep"]["path_abs"]
        assert Path(str(row["manifest"])) == manifest_path(corpus)
        assert store.count_quarantine_batches(conn) == 1
    finally:
        conn.close()
    # The row carries no second copy of the items: the file is the state machine.
    assert "items" not in dict(row).keys()


# ---------------------------------------------------------------------------
# The order: manifest first, files second
# ---------------------------------------------------------------------------


def test_the_manifest_exists_and_lists_pending_before_the_first_move(corpus, monkeypatch):
    """A crash at the move seam still leaves the plan on disk.

    This is the order the whole recovery story rests on: if the file moved first,
    an interrupted run would have no record of where it went.
    """
    paths = paths_of(corpus)
    members = paths["all"]
    seen = {}

    def crashing_move(src, dst, **kwargs):
        # Read the manifest at the exact moment the first move is attempted.
        seen["manifest_at_first_move"] = read_manifest(corpus)
        seen["moves_seen"] = seen.get("moves_seen", 0) + 1
        raise OSError("simulated crash (handle held by another process)")

    monkeypatch.setattr(quarantine, "move_file", crashing_move)
    code, out, err = run_cli(quarantine_argv(corpus, paths["others"]))

    assert code == 1, err  # items stay pending; the run reports it
    assert seen["moves_seen"] == 2, "every item was attempted"
    at_move = seen["manifest_at_first_move"]
    assert set(statuses(at_move).values()) == {quarantine.STATUS_PENDING}
    # Nothing moved: the originals are all still there, byte-identical.
    for path in members:
        assert Path(path).is_file()
    assert "pending" in err


def test_an_interrupted_batch_is_completed_by_rerunning_the_same_batch_id(corpus, monkeypatch):
    """``pending`` items are redone; nothing is moved twice."""
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    before = {path: sha256_of(Path(path)) for path in members}
    real_move = quarantine.move_file
    calls = []

    def crashing_move(src, dst, **kwargs):
        calls.append((src, dst))
        if len(calls) == 2:  # the first move succeeds, the second dies
            raise OSError("simulated crash")
        return real_move(src, dst, **kwargs)

    monkeypatch.setattr(quarantine, "move_file", crashing_move)
    code, out, err = run_cli(quarantine_argv(corpus, moving))
    assert code == 1, err

    partial = read_manifest(corpus)
    assert sorted(statuses(partial).values()) == [quarantine.STATUS_MOVED, quarantine.STATUS_PENDING]

    # Re-run without the fault: the finished item must not be touched again.
    monkeypatch.setattr(quarantine, "move_file", real_move)
    code, out, err = run_cli(quarantine_argv(corpus, moving))
    assert code == 0, err

    manifest = read_manifest(corpus)
    assert set(statuses(manifest).values()) == {quarantine.STATUS_MOVED}
    for item in manifest["items"]:
        assert sha256_of(Path(item["quarantine_path"])) == before[item["original_path"]]
        assert not os.path.exists(item["original_path"])
    # One quarantine copy per original, and the share of premature moves is zero.
    copies = [Path(item["quarantine_path"]).name for item in manifest["items"]]
    assert len(set(copies)) == len(copies) == len(moving)


def test_resuming_completes_the_status_of_a_file_already_in_the_quarantine(corpus, monkeypatch):
    """A file moved before the crash is recorded as moved, not moved again.

    The move that lands is the one whose manifest update never ran, so the item is
    still ``pending`` while the file is already in the quarantine. Re-running has
    to notice that instead of moving the (now missing) original.
    """
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    before = {path: sha256_of(Path(path)) for path in members}
    real_move = quarantine.move_file

    def move_then_crash(src, dst, **kwargs):
        real_move(src, dst, **kwargs)  # really moves, then the process "dies"
        raise OSError("simulated crash after the move")

    monkeypatch.setattr(quarantine, "move_file", move_then_crash)
    code, out, err = run_cli(quarantine_argv(corpus, moving))
    assert code == 1, err

    stuck = read_manifest(corpus)
    assert set(statuses(stuck).values()) == {quarantine.STATUS_PENDING}
    assert not os.path.exists(stuck["items"][0]["original_path"]), "the move really happened"

    monkeypatch.setattr(quarantine, "move_file", real_move)
    code, out, err = run_cli(quarantine_argv(corpus, moving))
    assert code == 0, err

    manifest = read_manifest(corpus)
    assert set(statuses(manifest).values()) == {quarantine.STATUS_MOVED}
    for item in manifest["items"]:
        assert sha256_of(Path(item["quarantine_path"])) == before[item["original_path"]]


def test_a_batch_id_is_not_reused_for_a_different_request(corpus):
    """A batch id names one decision; a resume may not repurpose it."""
    paths = paths_of(corpus)
    assert run_cli(quarantine_argv(corpus, paths["others"]))[0] == 0

    # Same batch id, same keep, but only one of the two files is listed now.
    code, out, err = run_cli(quarantine_argv(corpus, [paths["others"][0]]))
    assert code == 2
    assert "different files" in err

    # Same batch id, same files, but a different member to keep.
    code, out, err = run_cli(
        quarantine_argv(corpus, paths["others"], keep=paths["others"][1])
    )
    assert code == 2
    assert "different kept member" in err

    manifest = read_manifest(corpus)
    assert sorted(statuses(manifest)) == sorted(paths["others"])
    assert set(statuses(manifest).values()) == {quarantine.STATUS_MOVED}


def test_a_restored_batch_is_not_silently_reopened(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    assert run_cli(quarantine_argv(corpus, paths["others"]))[0] == 0
    assert run_cli(["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm"])[0] == 0

    code, out, err = run_cli(quarantine_argv(corpus, paths["others"]))

    assert code == 1
    assert "already restored" in err
    assert "pending" in err
    for path in paths["all"]:
        assert Path(path).is_file()


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------


def test_undo_restores_every_file_byte_for_byte(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    before = {path: Path(path).read_bytes() for path in members}

    assert run_cli(quarantine_argv(corpus, moving))[0] == 0
    code, out, err = run_cli(["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm"])

    assert code == 0, err
    assert "restored" in out
    manifest = read_manifest(corpus)
    assert set(statuses(manifest).values()) == {quarantine.STATUS_RESTORED}
    for item in manifest["items"]:
        restored = Path(item["original_path"])
        assert restored.is_file()
        assert restored.read_bytes() == before[item["original_path"]]
        assert sha256_of(restored) == item["sha256"]
        assert not os.path.exists(item["quarantine_path"])
    # The batch directory and its manifest stay: this tool has no delete path, and
    # the record is what makes "undo" auditable afterwards.
    assert manifest_path(corpus).is_file()


def test_undo_restores_in_reverse_move_order(corpus, monkeypatch):
    """``plan/03`` module 5 asks for a reverse-order restore.

    The order is asserted directly, because for a flat set of files it has no
    observable effect on the bytes — an injection that swapped it would be a fake
    gate, so the sequence is recorded instead of inferred.
    """
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    assert run_cli(quarantine_argv(corpus, moving))[0] == 0
    planned = [item["original_path"] for item in read_manifest(corpus)["items"]]

    real_move = quarantine.move_file
    order = []

    def recording_move(src, dst, **kwargs):
        order.append(dst)
        return real_move(src, dst, **kwargs)

    monkeypatch.setattr(quarantine, "move_file", recording_move)
    assert run_cli(["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm"])[0] == 0

    assert order == list(reversed(planned))


def test_undo_aborts_the_whole_batch_when_a_quarantine_copy_changed(corpus):
    """A tampered copy must not be restored, and must not take the others with it."""
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    assert run_cli(quarantine_argv(corpus, moving))[0] == 0

    manifest = read_manifest(corpus)
    victim, bystander = manifest["items"]
    # Same size, different bytes: the size check cannot catch this, so the sha256
    # verification is the only thing standing between the user and a wrong file.
    victim_path = Path(victim["quarantine_path"])
    victim_path.write_bytes(b"x" * victim_path.stat().st_size)
    assert victim_path.stat().st_size == victim["size"]

    code, out, err = run_cli(["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm"])

    assert code == 1
    assert "sha256" in err
    assert not os.path.exists(victim["original_path"])
    # The other item is still in the quarantine: "abort" means nothing moved.
    assert Path(bystander["quarantine_path"]).is_file()
    assert not os.path.exists(bystander["original_path"])
    assert set(statuses(read_manifest(corpus)).values()) == {quarantine.STATUS_MOVED}


def test_undo_refuses_to_overwrite_a_file_that_appeared_at_the_original_path(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    assert run_cli(quarantine_argv(corpus, moving))[0] == 0

    manifest = read_manifest(corpus)
    item = manifest["items"][0]
    intruder = "重新写过的正文，不应被覆盖。" * 10
    Path(item["original_path"]).write_text(intruder, encoding="utf-8")

    code, out, err = run_cli(["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm"])

    assert code == 1
    assert "occupied" in err
    assert Path(item["original_path"]).read_text(encoding="utf-8") == intruder
    assert Path(item["quarantine_path"]).is_file()


def test_undo_is_idempotent(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    assert run_cli(quarantine_argv(corpus, moving))[0] == 0
    undo = ["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm"]
    assert run_cli(undo)[0] == 0

    before = {path: sha256_of(Path(path)) for path in moving}
    code, out, err = run_cli(undo)

    assert code == 0, err
    assert {path: sha256_of(Path(path)) for path in moving} == before


def test_undoing_an_unknown_batch_is_a_usage_error(corpus):
    code, out, err = run_cli(["undo", "--db", str(corpus["db"]), "--batch", "nope", "--confirm"])
    assert code == 2
    assert "no batch" in err


# ---------------------------------------------------------------------------
# Refusals: nothing outside the request is touched
# ---------------------------------------------------------------------------


def test_a_path_outside_the_cluster_is_refused(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    outsider = str(corpus["outsider"])
    before = sha256_of(corpus["outsider"])

    code, out, err = run_cli(quarantine_argv(corpus, [outsider]))

    assert code == 2
    assert "not a member" in err
    assert sha256_of(corpus["outsider"]) == before
    assert not manifest_path(corpus).exists()
    for path in members:
        assert Path(path).is_file()


def test_the_kept_member_cannot_be_moved(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    keep = corpus["keep"]["path_abs"]

    code, out, err = run_cli(quarantine_argv(corpus, [keep], keep=keep))

    assert code == 2
    assert "--move" in err
    assert not manifest_path(corpus).exists()


def test_a_member_listed_twice_is_refused(corpus):
    paths = paths_of(corpus)
    twice = paths["others"][0]
    code, out, err = run_cli(quarantine_argv(corpus, [twice, twice]))
    assert code == 2
    assert "twice" in err


def test_an_unknown_cluster_is_refused(corpus):
    argv = quarantine_argv(corpus, paths_of(corpus)["others"])
    argv[argv.index("--cluster") + 1] = "0000000000000"
    code, out, err = run_cli(argv)
    assert code == 2
    assert "no cluster" in err


def test_a_file_edited_after_the_scan_is_refused(corpus):
    """What the user reviewed must be what is on disk."""
    victim = paths_of(corpus)["others"][0]
    target = Path(victim)
    target.write_text("扫描之后被改过的正文。" * 30, encoding="utf-8")
    edited = target.read_bytes()

    code, out, err = run_cli(quarantine_argv(corpus, [victim]))

    assert code == 2
    assert "changed after it was scanned" in err
    assert target.read_bytes() == edited
    assert not manifest_path(corpus).exists()


def test_a_file_missing_from_disk_is_refused(corpus):
    victim = paths_of(corpus)["others"][0]
    os.rename(victim, victim + ".gone")

    code, out, err = run_cli(quarantine_argv(corpus, [victim]))

    assert code == 2
    assert "missing on disk" in err
    assert not manifest_path(corpus).exists()


def test_an_empty_move_list_never_means_everything(corpus):
    """The switch means "which ones"; an empty list is an error, not a wildcard."""
    conn = store.connect_readonly(str(corpus["db"]))
    try:
        with pytest.raises(quarantine.QuarantineError) as exc:
            quarantine.plan_batch(
                conn,
                cluster_id=corpus["cluster_id"],
                keep=corpus["keep"]["path_abs"],
                move=[],
                batch_id=BATCH,
            )
    finally:
        conn.close()
    assert "no member was listed" in str(exc.value)
    assert not manifest_path(corpus).exists()


def test_the_execution_layer_refuses_without_confirm(corpus):
    """The parser, the execution layer and the HTTP body all require confirmation.

    The parser case is covered in ``test_cli_contract``; this is the layer itself,
    so no future caller can reach the moving code by forgetting a check.
    """
    paths = paths_of(corpus)
    members = paths["all"]

    with pytest.raises(quarantine.QuarantineError) as exc:
        quarantine.execute_quarantine(
            str(corpus["db"]),
            cluster_id=corpus["cluster_id"],
            keep=corpus["keep"]["path_abs"],
            move=paths["others"],
            confirm=False,
        )
    assert "without confirm" in str(exc.value)

    with pytest.raises(quarantine.QuarantineError):
        quarantine.execute_undo(str(corpus["db"]), batch_id=BATCH, confirm=False)

    assert not manifest_path(corpus).exists()
    for path in members:
        assert Path(path).is_file()


def test_a_whole_unlisted_cluster_stays_put(corpus):
    """``--move`` moves exactly what it names, no matter how big the cluster is."""
    paths = paths_of(corpus)
    moving = [paths["others"][0]]
    unlisted = [paths["keep"], paths["others"][1]]
    before = {path: sha256_of(Path(path)) for path in unlisted + moving}

    code, out, err = run_cli(quarantine_argv(corpus, moving))

    assert code == 0, err
    manifest = read_manifest(corpus)
    assert [item["original_path"] for item in manifest["items"]] == moving
    for path in unlisted:
        assert sha256_of(Path(path)) == before[path]


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


def test_reports_exports_the_documented_fields(corpus, tmp_path):
    paths = paths_of(corpus)
    members = paths["all"]
    moving = paths["others"]
    assert run_cli(quarantine_argv(corpus, moving))[0] == 0

    code, out, err = run_cli(["reports", "--db", str(corpus["db"]), "--format", "csv"])

    assert code == 0, err
    lines = out.strip().splitlines()
    assert lines[0] == ",".join(quarantine.REPORT_COLUMNS)
    assert len(lines) == len(moving) + 1
    manifest = read_manifest(corpus)
    by_original = {item["original_path"]: item for item in manifest["items"]}
    for line in lines[1:]:
        fields = line.split(",")
        assert fields[0] == BATCH
        assert fields[1] == corpus["cluster_id"]
        assert fields[2] == "exact"  # the tier, read from the clusters table
        assert fields[3] == corpus["keep"]["path_abs"]
        item = by_original[fields[5]]
        assert fields[4] == item["quarantine_path"]
        assert fields[6] == item["sha256"]
        assert fields[7] == quarantine.STATUS_MOVED

    # After an undo the same record says "restored" — the report is the history,
    # not a snapshot of the current filesystem.
    assert run_cli(["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm"])[0] == 0
    code, out, err = run_cli(["reports", "--db", str(corpus["db"]), "--format", "json"])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["columns"] == list(quarantine.REPORT_COLUMNS)
    assert {row["status"] for row in payload["rows"]} == {quarantine.STATUS_RESTORED}


def test_reports_writes_into_a_directory_or_a_file(corpus, tmp_path):
    paths = paths_of(corpus)
    members = paths["all"]
    assert run_cli(quarantine_argv(corpus, paths["others"]))[0] == 0
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    code, out, err = run_cli(
        ["reports", "--db", str(corpus["db"]), "--out", str(out_dir), "--format", "json"]
    )

    assert code == 0, err
    written = out_dir / "quarantine_report.json"
    assert written.is_file()
    assert json.loads(written.read_text(encoding="utf-8"))["count"] == 2

    # A trailing separator means "this directory", even when it does not exist
    # yet: the alternative is a stray *file* named ``new``.
    fresh = tmp_path / "new"
    code, out, err = run_cli(
        ["reports", "--db", str(corpus["db"]), "--out", str(fresh) + os.sep]
    )
    assert code == 0, err
    assert (fresh / "quarantine_report.csv").read_text(encoding="utf-8").startswith("batch_id,")

    code, out, err = run_cli(
        ["reports", "--db", str(corpus["db"]), "--out", str(tmp_path / "r.csv")]
    )
    assert (tmp_path / "r.csv").read_text(encoding="utf-8").startswith("batch_id,")
    assert members  # the corpus paths were used to build the batch above


def test_reports_exits_one_while_an_item_is_still_pending(corpus, monkeypatch):
    """An interrupted quarantine is exactly what a reviewer has to look at."""
    paths = paths_of(corpus)
    members = paths["all"]

    def crashing_move(src, dst, **kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(quarantine, "move_file", crashing_move)
    assert run_cli(quarantine_argv(corpus, paths["others"]))[0] == 1

    code, out, err = run_cli(["reports", "--db", str(corpus["db"])])

    assert code == 1
    assert "pending" in err
    assert quarantine.STATUS_PENDING in out


def test_reports_says_so_when_no_batch_exists(corpus):
    code, out, err = run_cli(["reports", "--db", str(corpus["db"])])
    assert code == 0
    assert "no quarantine batches" in err
    assert out.strip().splitlines() == [",".join(quarantine.REPORT_COLUMNS)]


def test_reports_needs_an_index(tmp_path):
    code, out, err = run_cli(["reports", "--db", str(tmp_path / "nope.db")])
    assert code == 2
    assert "no index" in err


# ---------------------------------------------------------------------------
# CLI/library plumbing
# ---------------------------------------------------------------------------


def test_the_json_output_is_the_execution_layers_payload(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    code, out, err = run_cli(quarantine_argv(corpus, paths["others"], extra=["--format", "json"]))
    assert code == 0, err

    payload = json.loads(out)
    assert payload["action"] == "quarantine"
    assert payload["batch_id"] == BATCH
    assert payload["counts"] == {"items": 2, "moved": 2, "restored": 0, "pending": 0}
    assert set(payload["items"][0]) == {
        "status",
        "original_path",
        "quarantine_path",
        "sha256",
        "size",
        "moved_at",
    }
    # Same object the HTTP layer returns (see tests/test_web.py).
    assert payload["ok"] is True


def test_the_undo_json_output_reports_restored_items(corpus):
    paths = paths_of(corpus)
    members = paths["all"]
    assert run_cli(quarantine_argv(corpus, paths["others"]))[0] == 0
    code, out, err = run_cli(
        ["undo", "--db", str(corpus["db"]), "--batch", BATCH, "--confirm", "--format", "json"]
    )
    assert code == 0, err
    assert json.loads(out)["counts"]["restored"] == 2


def test_the_batch_id_is_sortable_and_unique_enough():
    ids = {quarantine.new_batch_id("2026-10-17T10:15:00Z") for _ in range(50)}
    assert len(ids) == 50, "the random suffix must separate batches in the same second"
    assert all(item.startswith("20261017T101500Z-") for item in ids)
    assert max(ids) > min(ids)


def test_planning_the_same_request_twice_gives_the_same_plan(corpus):
    """A resumed batch recomputes the same names, which is why resume is safe.

    (Byte-identity of the manifest itself is not the claim: it carries absolute
    paths and a timestamp. The claim is that *planning* does not depend on chance.)
    """
    conn = store.connect_readonly(str(corpus["db"]))
    try:
        first = quarantine.plan_batch(
            conn,
            cluster_id=corpus["cluster_id"],
            keep=corpus["keep"]["path_abs"],
            move=paths_of(corpus)["others"],
            batch_id=BATCH,
        )
        second = quarantine.plan_batch(
            conn,
            cluster_id=corpus["cluster_id"],
            keep=corpus["keep"]["path_abs"],
            move=list(reversed(paths_of(corpus)["others"])),
            batch_id=BATCH,
        )
    finally:
        conn.close()
    assert first.items == second.items
    assert first.quarantine_dir == second.quarantine_dir


def test_the_manifest_rendering_is_stable():
    """One renderer for the first write and for every status update."""
    items = [
        quarantine.ManifestItem(
            status=quarantine.STATUS_PENDING,
            original_path="D:/docs/a.txt",
            quarantine_path="D:/docs/.dedupe-quarantine/b/a.txt",
            sha256="0" * 64,
            size=12,
            moved_at=None,
        )
    ]
    kwargs = dict(
        batch_id=BATCH,
        created_at=CREATED_AT,
        cluster_id="c1",
        keep="D:/docs/keep.txt",
        items=items,
    )
    text = quarantine.render_manifest(**kwargs)
    assert quarantine.render_manifest(**kwargs) == text
    assert "\r" not in text
    assert text.endswith("\n")
    payload = json.loads(text)
    assert list(payload) == ["schema", "batch_id", "created_at", "cluster_id", "keep", "items"]
    assert list(payload["items"][0]) == [
        "status",
        "original_path",
        "quarantine_path",
        "sha256",
        "size",
        "moved_at",
    ]


def test_move_file_retries_a_locked_file(corpus, monkeypatch, tmp_path):
    """A file held for a moment by another process is not a failed quarantine."""
    src = tmp_path / "src.txt"
    src.write_text("body", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    real_move = quarantine.shutil.move
    attempts = []

    def flaky(source, target):
        attempts.append(1)
        if len(attempts) < 3:  # Windows: sharing violation while a handle is open
            raise PermissionError(13, "the file is in use by another process")
        return real_move(source, target)

    monkeypatch.setattr(quarantine.shutil, "move", flaky)
    used = quarantine.move_file(str(src), str(dst), sleep=lambda _seconds: None)

    assert used == 3
    assert dst.read_text(encoding="utf-8") == "body"
    assert not src.exists()


def test_move_file_gives_up_and_reports_the_error(corpus, monkeypatch, tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("body", encoding="utf-8")

    def always_locked(source, target):
        raise PermissionError(13, "the file is in use by another process")

    monkeypatch.setattr(quarantine.shutil, "move", always_locked)
    with pytest.raises(PermissionError):
        quarantine.move_file(
            str(src), str(tmp_path / "dst.txt"), attempts=3, sleep=lambda _seconds: None
        )
    assert src.is_file()


def test_windows_long_paths_are_prefixed(tmp_path):
    import ntpath

    absolute = str(tmp_path / "a" / "b.txt")
    extended = quarantine.windows_extended_path(absolute)
    if os.name == "nt":
        assert extended.startswith("\\\\?\\")
        assert extended.endswith(ntpath.normpath(absolute).lstrip("\\").replace("/", "\\"))
        # Idempotent: applying it twice does not double the prefix.
        assert quarantine.windows_extended_path(extended) == extended
        unc = quarantine.windows_extended_path("\\\\server\\share\\a.txt")
        assert unc == "\\\\?\\UNC\\server\\share\\a.txt"
    else:
        assert extended == absolute


# ---------------------------------------------------------------------------
# The repository-level promises
# ---------------------------------------------------------------------------


def _git_check_ignore(*paths: Path) -> list:
    """Paths git would refuse to track; empty when git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--", *[str(path) for path in paths]],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        pytest.skip("git is not available")
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_the_quarantine_the_index_and_the_reports_stay_out_of_git():
    """The DoD item, checked against git itself rather than against .gitignore.

    ``git check-ignore`` is asked about paths that are never created: it reports
    what *would* be ignored, so this cannot pollute the working tree.
    """
    ignored = _git_check_ignore(
        REPO_ROOT / "docs" / ".dedupe-quarantine" / BATCH / "manifest.json",
        REPO_ROOT / "docs" / ".dedupe-cache" / "index.db",
        REPO_ROOT / "out" / "quarantine_report.csv",
    )
    assert len(ignored) == 3, ignored
