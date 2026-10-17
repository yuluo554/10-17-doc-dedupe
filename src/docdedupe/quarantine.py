"""Module 5: the quarantine execution layer — move, resume, undo.

This is the only module in the package that writes to the user's documents, and
it does exactly one thing to them: **move**. There is no deletion path anywhere
in the package (``tests/test_repo_invariants.py`` asserts that statically), and
the two mutating calls this module makes are ``shutil.move`` — into a batch
directory — and ``os.replace``, which rewrites the manifest atomically.

Four promises shape the design, each enforced rather than documented:

1. **The manifest is written before anything moves.** Every item is planned,
   then recorded as ``pending``, and only then is a file moved; a successful move
   flips that item to ``moved``. A process that dies half way through therefore
   leaves a record of *which file was going where*, and re-running the same
   ``batch_id`` finishes the job (``pending`` items are redone, items already in
   the quarantine get their status completed) instead of guessing.
2. **Undo verifies before it moves anything.** The whole batch is pre-flighted:
   every quarantined file's ``sha256`` and size must match what the manifest
   recorded, and every original path must be free (or already hold the identical
   bytes). One mismatch aborts the batch with *nothing* touched — a partial
   restore would be worse than none, because the user could no longer tell which
   copies are the good ones. After each restore the destination is hashed again,
   so "byte-identical" is measured, not assumed.
3. **Nothing outside the batch is touched.** The paths to move are checked
   against the ``clusters`` table first: only members of the given cluster may be
   listed, the kept member may not be listed, and each file must still have the
   size and mtime the scan recorded. A file the user never reviewed is a refusal,
   not a warning.
4. **Reads are read-only; the module writes only its own registry.** Membership,
   size and mtime come from a ``mode=ro`` connection (the same structural
   guarantee module 4 relies on). The one table this module writes is
   ``quarantine_batches``: a lookup index that maps ``batch_id`` to the manifest
   file, so ``undo --batch`` and the review page can find a batch without being
   told where the corpus lives. The manifest file stays the single state machine.

The quarantine layout is::

    <common parent of the cluster's members>/
        .dedupe-quarantine/
            <batch-id>/
                manifest.json
                <member name>            (plain file name)
                <member name>__2.<ext>   (only when two members share a name)

Nothing is ever overwritten: when two members of one cluster share a base name
they are disambiguated with a deterministic ``__<n>`` suffix, chosen in
``path_norm`` order so a resumed run computes the same names again.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import store

# ---------------------------------------------------------------------------
# Layout and vocabulary
# ---------------------------------------------------------------------------

QUARANTINE_DIR_NAME = ".dedupe-quarantine"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "doc-dedupe-quarantine/1"

STATUS_PENDING = "pending"
STATUS_MOVED = "moved"
STATUS_RESTORED = "restored"

# The state machine, spelled out so a status written by a future version is
# visible as unknown instead of being silently treated as pending.
ITEM_STATUSES = (STATUS_PENDING, STATUS_MOVED, STATUS_RESTORED)

# Columns of the CSV/JSON report (``plan/03`` module 5). One row per item, the
# batch-level fields repeated, in this order.
REPORT_COLUMNS = (
    "batch_id",
    "cluster_id",
    "tier",
    "keep",
    "moved_path",
    "original_path",
    "sha256",
    "status",
)

# A file being moved can be held open by an indexer, a virus scanner or the
# Explorer preview pane for a moment. Retrying is the documented Windows
# handling; the budget is small so a real permission error still fails fast.
MOVE_ATTEMPTS = 6
MOVE_RETRY_SECONDS = 0.15

_BS = "\\"
_LONG_PREFIX = _BS + _BS + "?" + _BS
_LONG_UNC_PREFIX = _BS + _BS + "?" + _BS + "UNC" + _BS


class QuarantineError(RuntimeError):
    """A request that must not touch the filesystem.

    Raised for every refusal the user can act on: a path that is not a member of
    the cluster, a file that changed since it was scanned, an occupied original
    path, an unknown batch. The CLI turns it into exit code 2 and the HTTP layer
    into status 400/404, and in every case the file tree is left as it was.
    """


class UnknownBatch(QuarantineError):
    """The requested ``batch_id`` is not registered in the index."""


# ---------------------------------------------------------------------------
# The OS boundary
# ---------------------------------------------------------------------------


def windows_extended_path(path: str) -> str:
    """Return the ``\\\\?\\`` form of an absolute path on Windows.

    The prefix removes the historical ``MAX_PATH`` limit, which matters here
    because a document that is already deep inside a folder gets one path segment
    deeper once it is moved into the quarantine. On POSIX (and for a path that
    already carries a prefix) the input is returned unchanged. UNC paths get the
    ``\\\\?\\UNC\\`` form, which is the only spelling Windows accepts for them.
    """
    if os.name != "nt":
        return path
    if path.startswith(_LONG_PREFIX):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith(_BS + _BS):
        return _LONG_UNC_PREFIX + absolute[2:]
    return _LONG_PREFIX + absolute


def move_file(
    src: str,
    dst: str,
    *,
    attempts: int = MOVE_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Move one file, retrying while another process still holds it.

    This is deliberately the *single* place in the package where a file changes
    place: fault injection (the B6 benchmark and the tests) has exactly one seam,
    and "the tool only moves files" is one call to audit.

    Returns the number of attempts used. Raises the last :class:`OSError` when
    every attempt failed; the caller records the item as still ``pending``
    instead of pretending it moved.
    """
    last: Optional[OSError] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            shutil.move(windows_extended_path(src), windows_extended_path(dst))
            return attempt
        except OSError as exc:  # locked file, transient sharing violation
            last = exc
            if attempt < attempts:
                sleep(MOVE_RETRY_SECONDS * attempt)
    assert last is not None  # attempts >= 1 always sets it
    raise last


def file_sha256(path: str) -> str:
    """Content hash of one file, read in chunks (documents can be large)."""
    digest = hashlib.sha256()
    with open(windows_extended_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanItem:
    """One member that will be (or was) moved, resolved against the index."""

    doc_id: int
    path_norm: str
    original_path: str
    quarantine_path: str
    sha256: str
    size: int
    index_size: int
    index_mtime_ns: int

    def as_dict(self, *, status: str, moved_at: Optional[str]) -> dict:
        return {
            "status": status,
            "original_path": self.original_path,
            "quarantine_path": self.quarantine_path,
            "sha256": self.sha256,
            "size": self.size,
            "moved_at": moved_at,
        }


@dataclass(frozen=True)
class BatchPlan:
    cluster_id: str
    keep: str
    items: Tuple[PlanItem, ...]
    quarantine_dir: str
    manifest_path: str
    tier: str


def _cluster_members(conn, cluster_id: str) -> Tuple[str, Dict[str, dict]]:
    """``(tier, members)`` of one cluster, read from the ``clusters`` table.

    Read-only and table-only: the tier and the member list are module 3's
    decision, and re-deriving them here would let the execution layer and the
    review page disagree about which files form a cluster.
    """
    tier: Optional[str] = None
    members: Dict[str, dict] = {}
    for row in store.cluster_rows(conn):
        if str(row["cluster_id"]) != cluster_id:
            continue
        tier = str(row["tier"])
        members[str(row["path_norm"])] = {
            "doc_id": int(row["doc_id"]),
            "path_abs": str(row["path_abs"]),
            "path_norm": str(row["path_norm"]),
        }
    if tier is None:
        raise QuarantineError(
            "no cluster %s in this index; run 'doc-dedupe report' first or check the id"
            % cluster_id
        )
    return tier, members


def _common_root(paths: Sequence[str]) -> str:
    """The deepest directory that contains every given file."""
    if len(paths) == 1:
        return os.path.dirname(os.path.abspath(paths[0]))
    try:
        common = os.path.commonpath([os.path.abspath(path) for path in paths])
    except ValueError as exc:  # different drives on Windows
        raise QuarantineError(
            "these members live on different volumes (%s), so they cannot share one "
            "quarantine directory; quarantine them in separate batches with --move"
            % exc
        )
    if not os.path.isdir(common):
        common = os.path.dirname(common)
    return common


def _free_name(directory: str, name: str, taken: Iterable[str]) -> str:
    """``name`` if it is unused, else ``stem__2.ext``, ``stem__3.ext``, ...

    Used both for two members of the same batch that share a base name and for a
    name already present on disk from an earlier batch. The quarantine never
    overwrites an existing file, so a collision changes the destination name
    instead of destroying whatever was there.
    """
    taken_set = set(taken)
    candidate = name
    stem, suffix = os.path.splitext(name)
    counter = 2
    while candidate in taken_set or os.path.exists(
        os.path.join(windows_extended_path(directory), candidate)
    ):
        candidate = "%s__%d%s" % (stem, counter, suffix)
        counter += 1
    return candidate


def plan_batch(
    conn,
    *,
    cluster_id: str,
    keep: str,
    move: Sequence[str],
    batch_id: str,
) -> BatchPlan:
    """Resolve a request into concrete moves, or refuse before touching anything.

    Every refusal here happens *before* the manifest is written, which is why the
    order matters: the manifest promises "these files are going to these places",
    so it must not be written for a request that is already known to be bad.
    """
    tier, members = _cluster_members(conn, cluster_id)

    keep_norm = store.normalize_path(keep)
    if keep_norm not in members:
        raise QuarantineError(
            "%s is not a member of cluster %s; nothing was moved" % (keep, cluster_id)
        )
    keep_path = members[keep_norm]["path_abs"]

    if not move:
        raise QuarantineError(
            "no member was listed to move; --move names the files to take away "
            "(an empty list never means \"everything\")"
        )

    wanted: List[str] = []
    seen: set = set()
    spelled: Dict[str, str] = {}
    for value in move:
        norm = store.normalize_path(value)
        if norm in seen:
            raise QuarantineError("%s is listed twice; nothing was moved" % value)
        seen.add(norm)
        wanted.append(norm)
        spelled[norm] = str(value)

    if keep_norm in seen:
        raise QuarantineError(
            "the kept member %s is also listed in --move; nothing was moved" % keep_path
        )

    outsiders = [norm for norm in wanted if norm not in members]
    if outsiders:
        raise QuarantineError(
            "%s is not a member of cluster %s; only that cluster's own members may be "
            "quarantined (nothing was moved)"
            % (spelled[outsiders[0]], cluster_id)
        )

    # Deterministic order: the same request against the same index plans the same
    # names again, which is what makes a resumed batch reproducible.
    wanted.sort()

    quarantine_dir = os.path.join(
        _common_root([members[norm]["path_abs"] for norm in members]),
        QUARANTINE_DIR_NAME,
        batch_id,
    )

    items: List[PlanItem] = []
    taken: List[str] = []
    for norm in wanted:
        member = members[norm]
        path_abs = member["path_abs"]
        row = store.find_document(conn, norm)
        if row is None:
            raise QuarantineError(
                "%s is not in the index; re-run 'doc-dedupe scan' (nothing was moved)"
                % path_abs
            )
        if not os.path.isfile(path_abs):
            raise QuarantineError(
                "%s is missing on disk; re-run 'doc-dedupe scan' (nothing was moved)"
                % path_abs
            )
        stat = os.stat(windows_extended_path(path_abs))
        if stat.st_size != int(row["size"]) or stat.st_mtime_ns != int(row["mtime_ns"]):
            raise QuarantineError(
                "%s changed after it was scanned (size or mtime differs from the index), "
                "so what you reviewed is not what is on disk; re-run 'doc-dedupe scan' "
                "(nothing was moved)" % path_abs
            )
        name = _free_name(quarantine_dir, os.path.basename(path_abs), taken)
        taken.append(name)
        items.append(
            PlanItem(
                doc_id=member["doc_id"],
                path_norm=norm,
                original_path=path_abs,
                quarantine_path=os.path.join(quarantine_dir, name),
                sha256=file_sha256(path_abs),
                size=int(stat.st_size),
                index_size=int(row["size"]),
                index_mtime_ns=int(row["mtime_ns"]),
            )
        )

    return BatchPlan(
        cluster_id=cluster_id,
        keep=keep_path,
        items=tuple(items),
        quarantine_dir=quarantine_dir,
        manifest_path=os.path.join(quarantine_dir, MANIFEST_NAME),
        tier=tier,
    )


# ---------------------------------------------------------------------------
# Manifest: the single state machine
# ---------------------------------------------------------------------------


@dataclass
class ManifestItem:
    status: str
    original_path: str
    quarantine_path: str
    sha256: str
    size: int
    moved_at: Optional[str]

    @classmethod
    def from_dict(cls, payload: dict) -> "ManifestItem":
        return cls(
            status=str(payload["status"]),
            original_path=str(payload["original_path"]),
            quarantine_path=str(payload["quarantine_path"]),
            sha256=str(payload["sha256"]),
            size=int(payload["size"]),
            moved_at=None if payload.get("moved_at") is None else str(payload["moved_at"]),
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "original_path": self.original_path,
            "quarantine_path": self.quarantine_path,
            "sha256": self.sha256,
            "size": self.size,
            "moved_at": self.moved_at,
        }


def render_manifest(
    *,
    batch_id: str,
    created_at: str,
    cluster_id: str,
    keep: str,
    items: Sequence[ManifestItem],
) -> str:
    """The manifest text.

    One implementation, used for the first write and for every status update, so
    there is no way for a resumed batch to be written in a different shape. Keys
    are in the order ``plan/03`` module 5 spells out; ``ensure_ascii=False`` keeps
    the paths readable and ``\\n`` is fixed so the bytes are the same everywhere.
    """
    payload = {
        "schema": MANIFEST_SCHEMA,
        "batch_id": batch_id,
        "created_at": created_at,
        "cluster_id": cluster_id,
        "keep": keep,
        "items": [item.as_dict() for item in items],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_manifest(path: str, text: str) -> None:
    """Write the manifest atomically: a reader never sees a half-written file.

    A temporary file beside the target plus :func:`os.replace` (atomic on the
    same volume) is the whole trick. ``os.replace`` overwrites the previous
    manifest, which is the one exception to "never overwrite" a *document*: the
    manifest is this module's own bookkeeping, not the user's data.
    """
    directory = os.path.dirname(path)
    os.makedirs(windows_extended_path(directory), exist_ok=True)
    temporary = path + ".tmp"
    with open(windows_extended_path(temporary), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(windows_extended_path(temporary), windows_extended_path(path))


def read_manifest(path: str) -> dict:
    with open(windows_extended_path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _manifest_items(manifest: dict) -> List[ManifestItem]:
    return [ManifestItem.from_dict(item) for item in manifest.get("items", [])]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchResult:
    """What one quarantine or undo run did, in one shape for every entry point.

    The CLI prints :meth:`describe` and the HTTP layer returns :meth:`as_dict`;
    both read the same object, which is what keeps the two entry points from
    drifting into two accounts of the same action.
    """

    action: str  # "quarantine" | "undo"
    batch_id: str
    cluster_id: str
    keep: str
    manifest_path: str
    quarantine_dir: str
    items: Tuple[dict, ...]
    problems: Tuple[str, ...] = ()
    pending: Tuple[str, ...] = ()

    @property
    def moved(self) -> int:
        return sum(1 for item in self.items if item["status"] == STATUS_MOVED)

    @property
    def restored(self) -> int:
        return sum(1 for item in self.items if item["status"] == STATUS_RESTORED)

    @property
    def complete(self) -> bool:
        """True when no item was left half-way, i.e. the run needs no review."""
        return not self.pending and not self.problems

    def as_dict(self) -> dict:
        return {
            "ok": self.complete,
            "action": self.action,
            "batch_id": self.batch_id,
            "cluster_id": self.cluster_id,
            "keep": self.keep,
            "manifest": self.manifest_path,
            "quarantine_dir": self.quarantine_dir,
            "counts": {
                "items": len(self.items),
                "moved": self.moved,
                "restored": self.restored,
                "pending": len(self.pending),
            },
            "items": [dict(item) for item in self.items],
            "pending": list(self.pending),
            "problems": list(self.problems),
        }

    def describe(self) -> List[str]:
        verb = "moved" if self.action == "quarantine" else "restored"
        lines = [
            "%s batch %s: %d of %d item(s) %s"
            % (self.action, self.batch_id, len(self.items) - len(self.pending), len(self.items), verb)
        ]
        for item in self.items:
            lines.append(
                "  %-9s %s -> %s"
                % (item["status"], item["original_path"], item["quarantine_path"])
            )
        lines.append("manifest: %s" % self.manifest_path)
        return lines

    def describe_problems(self) -> List[str]:
        """Why the run is incomplete. Printed on stderr by the CLI.

        Separate from :meth:`describe` because "which files did not move and why"
        is the part a script or a person acts on, and it must not be mixed into a
        payload that a caller may be parsing.
        """
        return ["problem: %s" % problem for problem in self.problems] + [
            "pending: %s" % path for path in self.pending
        ]


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_batch_id(now: Optional[str] = None) -> str:
    """``20261017T101500Z-7f3a``: sortable timestamp plus a random suffix.

    The timestamp makes batches sort chronologically in a report; the suffix keeps
    two batches in the same second apart, which matters because a batch directory
    may never be reused: its manifest is the record of what happened.
    """
    stamp = (now or _now()).replace("-", "").replace(":", "")
    return "%s-%s" % (stamp, secrets.token_hex(2))


def execute_quarantine(
    db_path: str,
    *,
    cluster_id: str,
    keep: str,
    move: Sequence[str],
    confirm: bool = False,
    batch_id: Optional[str] = None,
    created_at: Optional[str] = None,
    now: Optional[Callable[[], str]] = None,
) -> BatchResult:
    """Move the listed members into ``.dedupe-quarantine/<batch-id>/``.

    ``confirm`` must be true: refusing here as well as in the argument parser and
    the HTTP body means no caller can reach the moving code by forgetting a check.

    ``batch_id`` names an existing (possibly interrupted) batch to continue. On a
    resume the manifest *is* the plan: the request is checked against what the
    manifest already records, and then the state machine finishes the job — items
    still ``pending`` are moved, items already in the quarantine get their status
    completed. Re-planning from the filesystem instead would refuse the very case
    resume exists for, because a file that moved is no longer at its original
    path.
    """
    if not confirm:
        raise QuarantineError(
            "refusing to move anything without confirm: this action is reversible, "
            "but it must be asked for explicitly"
        )

    clock = now or _now
    created_at = created_at or clock()
    batch_id = batch_id or new_batch_id(created_at)
    requested_move = [store.normalize_path(path) for path in move]

    read_conn = store.connect_readonly(db_path)
    try:
        existing, manifest_path = _existing_batch(read_conn, batch_id)
        if existing is not None:
            _assert_same_request(existing, cluster_id, keep, requested_move, batch_id)
            items = _manifest_items(existing)
            cluster_id = str(existing["cluster_id"])
            keep = str(existing["keep"])
            quarantine_dir = os.path.dirname(manifest_path)
            manifest_created_at = str(existing["created_at"])
        else:
            plan = plan_batch(
                read_conn, cluster_id=cluster_id, keep=keep, move=move, batch_id=batch_id
            )
            cluster_id, keep = plan.cluster_id, plan.keep
            manifest_path, quarantine_dir = plan.manifest_path, plan.quarantine_dir
            manifest_created_at = created_at
            items = [
                ManifestItem(
                    status=STATUS_PENDING,
                    original_path=item.original_path,
                    quarantine_path=item.quarantine_path,
                    sha256=item.sha256,
                    size=item.size,
                    moved_at=None,
                )
                for item in plan.items
            ]
    finally:
        read_conn.close()

    def write_state() -> None:
        write_manifest(
            manifest_path,
            render_manifest(
                batch_id=batch_id,
                created_at=manifest_created_at,
                cluster_id=cluster_id,
                keep=keep,
                items=items,
            ),
        )

    # -- 1. the manifest is on disk before a single file moves ----------------
    write_state()

    conn = store.connect(db_path)
    try:
        store.upsert_quarantine_batch(
            conn,
            batch_id=batch_id,
            cluster_id=cluster_id,
            keep_path=keep,
            manifest_path=manifest_path,
            created_at=manifest_created_at,
        )
        conn.commit()
    finally:
        conn.close()

    # -- 2. now move, flipping each item to ``moved`` as it lands -------------
    problems: List[str] = []
    pending: List[str] = []
    for index, item in enumerate(items):
        if item.status == STATUS_MOVED:
            if _verify_quarantined(item) is None:
                problems.append(
                    "%s is recorded as moved but the quarantine copy is missing or "
                    "changed; undo will refuse this batch until it is repaired"
                    % item.original_path
                )
            continue
        if item.status == STATUS_RESTORED:
            pending.append(item.original_path)
            problems.append(
                "%s was already restored by an undo; it is not moved again (use a new "
                "--batch-id to quarantine it again)" % item.original_path
            )
            continue

        landed = _settle_pending(item, problems)
        if landed is None:
            pending.append(item.original_path)
            continue
        items[index] = replace(item, status=STATUS_MOVED, sha256=landed, moved_at=clock())
        write_state()

    return BatchResult(
        action="quarantine",
        batch_id=batch_id,
        cluster_id=cluster_id,
        keep=keep,
        manifest_path=manifest_path,
        quarantine_dir=quarantine_dir,
        items=tuple(
            {
                "status": item.status,
                "original_path": item.original_path,
                "quarantine_path": item.quarantine_path,
                "sha256": item.sha256,
                "size": item.size,
                "moved_at": item.moved_at,
            }
            for item in items
        ),
        problems=tuple(problems),
        pending=tuple(pending),
    )


def _existing_batch(conn, batch_id: str) -> Tuple[Optional[dict], str]:
    """``(manifest, manifest_path)`` of a registered batch, or ``(None, "")``.

    The registry is what lets a resume find a batch the way ``undo`` does: by id,
    without being told where the corpus lives.
    """
    row = store.find_quarantine_batch(conn, batch_id)
    if row is None:
        return None, ""
    manifest_path = str(row["manifest"])
    if not os.path.isfile(manifest_path):
        raise QuarantineError(
            "batch %s is registered but its manifest %s is gone; the quarantine "
            "cannot be continued without it" % (batch_id, manifest_path)
        )
    return read_manifest(manifest_path), manifest_path


def _assert_same_request(
    existing: dict,
    cluster_id: str,
    keep: str,
    requested_move: Sequence[str],
    batch_id: str,
) -> None:
    """Guard a resumed batch against being repurposed for different files."""
    if str(existing.get("cluster_id")) != cluster_id:
        raise QuarantineError(
            "batch %s already exists for cluster %s; nothing was moved"
            % (batch_id, existing.get("cluster_id"))
        )
    if store.normalize_path(str(existing.get("keep", ""))) != store.normalize_path(keep):
        raise QuarantineError(
            "batch %s already exists with a different kept member (%s); nothing was "
            "moved" % (batch_id, existing.get("keep"))
        )
    recorded = sorted(
        store.normalize_path(str(item["original_path"]))
        for item in existing.get("items", [])
    )
    if recorded != sorted(requested_move):
        raise QuarantineError(
            "batch %s already lists different files than this request; nothing was "
            "moved (use a new batch id to quarantine a different set)" % batch_id
        )


def _verify_quarantined(item: ManifestItem) -> Optional[str]:
    """The quarantine copy's hash when it is present and unchanged, else None."""
    if not os.path.isfile(item.quarantine_path):
        return None
    size = os.stat(windows_extended_path(item.quarantine_path)).st_size
    if size != item.size:
        return None
    digest = file_sha256(item.quarantine_path)
    return digest if digest == item.sha256 else None


def _settle_pending(item: ManifestItem, problems: List[str]) -> Optional[str]:
    """Move one ``pending`` item, or complete it if it is already in place.

    Returns the sha256 to store (the verified quarantine copy) or ``None`` when
    the item stays pending. The "already in place" branch is what makes an
    interrupted run resumable: a file that was moved before the crash must not be
    moved twice, and one that is still in its original place must be moved now.
    """
    original_exists = os.path.isfile(item.original_path)
    quarantined = _verify_quarantined(item)
    if quarantined is not None and not original_exists:
        return quarantined  # moved before the interruption: complete the status
    if not original_exists:
        problems.append(
            "%s is neither at its original path nor in the quarantine; leaving the "
            "batch untouched and reporting it" % item.original_path
        )
        return None
    if quarantined is not None:
        problems.append(
            "%s is in the quarantine and still at its original path; refusing to "
            "guess which one is current" % item.original_path
        )
        return None
    if os.path.exists(item.quarantine_path):
        problems.append(
            "%s already exists (a different file); refusing to overwrite it"
            % item.quarantine_path
        )
        return None
    current = file_sha256(item.original_path)
    if current != item.sha256:
        problems.append(
            "%s changed since it was planned (sha256 differs); nothing was moved for it"
            % item.original_path
        )
        return None
    try:
        move_file(item.original_path, item.quarantine_path)
    except OSError as exc:
        problems.append("could not move %s: %s" % (item.original_path, exc))
        return None
    landed = _verify_quarantined(item)
    if landed is None:
        problems.append(
            "%s did not arrive in the quarantine byte-identical; inspect %s and the "
            "manifest before continuing" % (item.original_path, item.quarantine_path)
        )
        return None
    return landed


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def load_batch(conn, batch_id: str) -> Tuple[dict, str]:
    """``(manifest, manifest_path)`` for one registered batch."""
    row = store.find_quarantine_batch(conn, batch_id)
    if row is None:
        raise UnknownBatch(
            "no batch %s in this index; run 'doc-dedupe reports' to list the batches "
            "this index knows about" % batch_id
        )
    manifest_path = str(row["manifest"])
    if not os.path.isfile(manifest_path):
        raise UnknownBatch(
            "batch %s is registered but its manifest %s is gone; the quarantine "
            "cannot be restored without it" % (batch_id, manifest_path)
        )
    return read_manifest(manifest_path), manifest_path


def execute_undo(
    db_path: str,
    *,
    batch_id: str,
    confirm: bool = False,
    now: Optional[Callable[[], str]] = None,
) -> BatchResult:
    """Put every file of one batch back where it came from.

    Two passes, deliberately:

    1. **Verify the whole batch** — every quarantine copy must hash to what the
       manifest recorded, and every original path must be free (or already hold
       the identical bytes, which makes undo idempotent). Any problem aborts the
       run before a single file moves.
    2. **Restore in reverse order** — the discipline ``plan/03`` module 5 asks
       for, so the last file moved is the first one put back.
    """
    if not confirm:
        raise QuarantineError(
            "refusing to restore anything without confirm: run the command with "
            "--confirm (or \"confirm\": true over HTTP)"
        )
    clock = now or _now

    read_conn = store.connect_readonly(db_path)
    try:
        manifest, manifest_path = load_batch(read_conn, batch_id)
    finally:
        read_conn.close()

    batch_id = str(manifest["batch_id"])
    cluster_id = str(manifest.get("cluster_id", ""))
    keep = str(manifest.get("keep", ""))
    items = _manifest_items(manifest)
    quarantine_dir = os.path.dirname(manifest_path)

    problems: List[str] = []
    plan: List[Tuple[int, ManifestItem]] = []

    for index, item in enumerate(items):
        if item.status == STATUS_RESTORED:
            continue
        if item.status != STATUS_MOVED:
            problems.append(
                "%s is %r in the manifest; only a moved item can be restored"
                % (item.original_path, item.status)
            )
            continue
        if not os.path.isfile(item.quarantine_path):
            problems.append(
                "%s is missing from the quarantine; refusing to restore the batch"
                % item.quarantine_path
            )
            continue
        size = os.stat(windows_extended_path(item.quarantine_path)).st_size
        if size != item.size:
            problems.append(
                "%s has size %d, the manifest recorded %d; refusing to restore it"
                % (item.quarantine_path, size, item.size)
            )
            continue
        digest = file_sha256(item.quarantine_path)
        if digest != item.sha256:
            problems.append(
                "%s no longer matches the recorded sha256 (contents changed); refusing "
                "to restore it" % item.quarantine_path
            )
            continue
        if os.path.exists(item.original_path):
            if file_sha256(item.original_path) != item.sha256:
                problems.append(
                    "%s is occupied by a different file; refusing to overwrite it"
                    % item.original_path
                )
            continue
        plan.append((index, item))

    if problems:
        # Nothing was touched: a half-restored batch would leave the user unable
        # to tell which of two copies is the reviewed one.
        return BatchResult(
            action="undo",
            batch_id=batch_id,
            cluster_id=cluster_id,
            keep=keep,
            manifest_path=manifest_path,
            quarantine_dir=quarantine_dir,
            items=tuple(item.as_dict() for item in items),
            problems=tuple(problems),
            pending=tuple(item.original_path for item in items if item.status == STATUS_MOVED),
        )

    for index, item in reversed(plan):
        try:
            move_file(item.quarantine_path, item.original_path)
        except OSError as exc:
            problems.append("could not restore %s: %s" % (item.original_path, exc))
            continue
        restored_ok = os.path.isfile(item.original_path) and (
            file_sha256(item.original_path) == item.sha256
        )
        if not restored_ok:
            problems.append(
                "%s was put back but does not hash to the recorded sha256; the "
                "quarantine copy is still at %s" % (item.original_path, item.quarantine_path)
            )
            continue
        items[index] = replace(item, status=STATUS_RESTORED, moved_at=clock())
        write_manifest(manifest_path, render_manifest(
            batch_id=batch_id,
            created_at=str(manifest.get("created_at", "")),
            cluster_id=cluster_id,
            keep=keep,
            items=items,
        ))

    result = BatchResult(
        action="undo",
        batch_id=batch_id,
        cluster_id=cluster_id,
        keep=keep,
        manifest_path=manifest_path,
        quarantine_dir=quarantine_dir,
        items=tuple(item.as_dict() for item in items),
        problems=tuple(problems),
        pending=tuple(
            item.original_path for item in items if item.status == STATUS_MOVED
        ),
    )
    return result


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def report_rows(conn) -> List[dict]:
    """One row per quarantined item, across every batch this index knows.

    The tier comes from the ``clusters`` table (a read-only join, not a
    recomputation): the report is a record of what was decided, and it must show
    the tier the review page showed.
    """
    tiers = {
        str(row["cluster_id"]): str(row["tier"]) for row in store.cluster_rows(conn)
    }
    rows: List[dict] = []
    for batch in store.quarantine_batch_rows(conn):
        batch_id = str(batch["batch_id"])
        manifest_path = str(batch["manifest"])
        if not os.path.isfile(manifest_path):
            rows.append(
                {
                    "batch_id": batch_id,
                    "cluster_id": str(batch["cluster_id"]),
                    "tier": tiers.get(str(batch["cluster_id"]), ""),
                    "keep": str(batch["keep_path"]),
                    "moved_path": "",
                    "original_path": "",
                    "sha256": "",
                    "status": "manifest_missing",
                }
            )
            continue
        manifest = read_manifest(manifest_path)
        for item in _manifest_items(manifest):
            rows.append(
                {
                    "batch_id": batch_id,
                    "cluster_id": str(batch["cluster_id"]),
                    "tier": tiers.get(str(batch["cluster_id"]), ""),
                    "keep": str(manifest.get("keep", batch["keep_path"])),
                    "moved_path": item.quarantine_path,
                    "original_path": item.original_path,
                    "sha256": item.sha256,
                    "status": item.status,
                }
            )
    rows.sort(key=lambda row: (row["batch_id"], row["original_path"]))
    return rows


def render_report(rows: Sequence[dict], fmt: str) -> str:
    """CSV or JSON, both with the column order ``plan/03`` module 5 lists."""
    if fmt == "csv":
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(REPORT_COLUMNS)
        for row in rows:
            writer.writerow([row[column] for column in REPORT_COLUMNS])
        return buffer.getvalue()
    if fmt == "json":
        return (
            json.dumps(
                {
                    "schema": "doc-dedupe-quarantine-report/1",
                    "columns": list(REPORT_COLUMNS),
                    "count": len(rows),
                    "rows": [{column: row[column] for column in REPORT_COLUMNS} for row in rows],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    raise ValueError("unknown report format %r" % fmt)


def manifest_of(db_path: str, batch_id: str) -> dict:
    """The manifest of one batch (used by tests and by the review page)."""
    conn = store.connect_readonly(db_path)
    try:
        manifest, _ = load_batch(conn, batch_id)
    finally:
        conn.close()
    return manifest


def batch_summaries(conn) -> List[dict]:
    """One summary per registered batch, for the review page's undo list.

    Counted from the manifest files, which stay the state machine: a summary that
    was derived anywhere else could tell the page a batch is finished while its
    manifest still holds a ``pending`` item.
    """
    summaries: List[dict] = []
    for batch in store.quarantine_batch_rows(conn):
        manifest_path = str(batch["manifest"])
        counts = {STATUS_PENDING: 0, STATUS_MOVED: 0, STATUS_RESTORED: 0}
        if os.path.isfile(manifest_path):
            for item in _manifest_items(read_manifest(manifest_path)):
                counts[item.status] = counts.get(item.status, 0) + 1
        else:
            counts["manifest_missing"] = 1
        summaries.append(
            {
                "batch_id": str(batch["batch_id"]),
                "cluster_id": str(batch["cluster_id"]),
                "keep": str(batch["keep_path"]),
                "created_at": str(batch["created_at"]),
                "manifest": manifest_path,
                "items": sum(
                    count for status, count in counts.items() if status in ITEM_STATUSES
                ),
                "moved": counts.get(STATUS_MOVED, 0),
                "restored": counts.get(STATUS_RESTORED, 0),
                "pending": counts.get(STATUS_PENDING, 0),
                "manifest_missing": counts.get("manifest_missing", 0),
            }
        )
    return summaries


__all__ = [
    "BatchPlan",
    "BatchResult",
    "ITEM_STATUSES",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "ManifestItem",
    "PlanItem",
    "QUARANTINE_DIR_NAME",
    "QuarantineError",
    "REPORT_COLUMNS",
    "STATUS_MOVED",
    "STATUS_PENDING",
    "STATUS_RESTORED",
    "UnknownBatch",
    "batch_summaries",
    "execute_quarantine",
    "execute_undo",
    "file_sha256",
    "load_batch",
    "manifest_of",
    "move_file",
    "new_batch_id",
    "plan_batch",
    "read_manifest",
    "render_manifest",
    "render_report",
    "report_rows",
    "windows_extended_path",
    "write_manifest",
]
