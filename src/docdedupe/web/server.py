"""The local review page: a read-only view over the index (module 4).

Three promises shape this module, and each one is enforced rather than
documented.

1. **It never decides anything.** Tier, keep recommendation and reason are read
   from the ``clusters`` table, which module 3 wrote; scores are read from the
   stored ``scores_json``. Nothing here scores a member, classifies an edge or
   re-runs the clustering. If the page re-derived the tier, a user could read
   "keep A" in a report and see "keep B" on the page, and there would be no way
   to tell which one was wrong (``plan/03`` module 4 invariant).
2. **It cannot write.** The connection is opened read-only
   (:func:`docdedupe.store.connect_readonly`), so SQLite itself rejects any
   accidental write. The two POST routes hand the user's decision to the
   execution layer of module 5 (:mod:`docdedupe.quarantine`), which opens its own
   writable connection for its own registry; this module never moves a file
   itself and never grows a second copy of the manifest state machine, so the
   command line and the page cannot report two different accounts of one action.
3. **It never reaches the network.** The page is one file with inline CSS and JS,
   and :func:`render_page` re-asserts that on every render — not once in a test —
   so a future edit that adds a CDN link fails at the point where the page is
   produced. The server binds a loopback address only (validated in
   :mod:`docdedupe.cli`), and uvicorn is started with ``access_log`` left on a
   local socket.

An index that has been scanned but never reported has an empty ``clusters``
table. The page then says so and names the command to run. It deliberately does
*not* cluster on demand: writing the result would make the page a writer, and
clustering without writing the result would put the page's recommendation at odds
with the table (the same inconsistency as point 1).

Payload shapes
--------------
``GET /api/clusters``::

    {"schema": "doc-dedupe-review/1", "status": "ok"|"empty", "hint": str|null,
     "built_at": str|null, "counts": {"exact": n, "near": n, "template": n},
     "clusters": [{"cluster_id", "tier", "tier_label", "keep", "keep_id",
                   "reason", "member_count", "members": [...]}],
     "batches": [{"batch_id", "cluster_id", "keep", "created_at", "manifest",
                  "items", "moved", "restored", "pending", "manifest_missing"}]}

``POST /api/quarantine`` (body ``{"cluster_id", "keep", "move": [...], "confirm": true}``)
and ``POST /api/undo`` (body ``{"batch_id", "confirm": true}``) return
``docdedupe.quarantine.BatchResult.as_dict()`` on success — the same payload
``doc-dedupe quarantine --format json`` prints — 400 for a refused request and
404 for an unknown batch.

``GET /api/clusters/{id}/diff?a=&b=``::

    {"schema": ..., "cluster_id", "tier", "tier_label", "summary", "stats",
     "left":  {"doc_id", "path", "segments": [{"kind", "text", "spans"}]},
     "right": {...}}

``segments`` on the two sides are in row order. A client walks them with two
cursors: a pair of ``same``/``replace`` segments is one row, a ``del`` advances
the left side alone and an ``add`` the right side. That rule is a reading of the
kinds produced by :mod:`docdedupe.diffing`, not a second implementation of the
alignment, so the page cannot disagree with the summary about what changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .. import cluster as cluster_mod
from .. import diffing
from .. import quarantine as quarantine_mod
from .. import store
from ..config import DEFAULT_SETTINGS, Settings
from ..report import TIER_LABELS
from . import MissingWebDependency

# The optional web stack. Imported at module level but tolerated missing, so
# that this module's read models (``clusters_view``, ``diff_view``,
# ``render_page``) stay importable and testable on an install without the
# ``web`` extra. ``create_app`` refuses to build an app when these are ``None``,
# and the CLI turns that into the documented exit code 3.
# ``uvicorn`` is imported inside :func:`serve` instead: it is only needed to
# actually run a server, and importing it here would make the missing-extra path
# depend on two packages rather than one.
try:  # pragma: no cover - the except branch is exercised by patching sys.modules
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:  # pragma: no cover
    FastAPI = HTTPException = Request = None  # type: ignore[assignment]
    HTMLResponse = JSONResponse = None  # type: ignore[assignment]

REVIEW_SCHEMA = "doc-dedupe-review/1"

STATIC_DIR = Path(__file__).resolve().parent / "static"
PAGE_FILE = STATIC_DIR / "index.html"

# Placeholders the static page carries so that the values with a single source
# (the index path, the standing disclaimer) are not duplicated in the HTML. A
# missing placeholder is a broken page, not a cosmetic issue: the disclaimer
# would silently disappear from what the user reads.
INDEX_PLACEHOLDER = "__DOCDEDUPE_INDEX__"
DISCLAIMER_PLACEHOLDER = "__DOCDEDUPE_DISCLAIMER__"

EMPTY_HINT = (
    "索引里还没有聚类结果：请先在命令行运行 "
    "doc-dedupe report --db <索引库>（或直接 report）后再刷新本页。"
)

# Sentences the page shows verbatim. The first is the standing disclaimer
# (``plan/06`` section 4); the second describes what the button does, in the words
# of the execution layer's own promise: the members move into the quarantine
# directory beside the corpus and can be put back at any time.
DISCLAIMER = (
    "相似度是机械测量值，不构成抄袭或侵权判断；所有处置都只是建议，且永远可逆。"
)
QUARANTINE_HINT = (
    "隔离会把选中的成员移动到语料目录下的 .dedupe-quarantine/<batch-id>/，"
    "不删除任何文件，随时可用 undo 复原（每次隔离都会写下 manifest，记录哪一份去了哪里）。"
)


class ExternalResourceError(RuntimeError):
    """The page tried to reference something outside this machine."""


class PageContractError(RuntimeError):
    """The page no longer honours a structural promise (a missing placeholder)."""


# Attribute positions that would make the browser fetch a remote resource, plus
# the tags that would make it load a *second* file at all. The check runs on the
# rendered page before it is served (``render_page``), so a CDN link added in a
# later edit fails immediately and loudly instead of quietly leaking a request
# from the user's browser. Inline resources (``data:`` URIs) would be allowed by
# these patterns, but adding one should be a deliberate edit of this list rather
# than something that slips through.
_FORBIDDEN_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("<link", "an external stylesheet or icon (<link>)"),
    ("@import", "a CSS @import"),
    ("<script src", "an external script file"),
    ("<img", "an embedded image file"),
    ("<iframe", "an embedded frame"),
    ("<object", "an embedded object"),
    ("<embed", "an embedded object"),
    ("<source", "an embedded media source"),
    ("src=\"http", "a remote script/image source"),
    ("src='http", "a remote script/image source"),
    ("src=\"//", "a protocol-relative script/image source"),
    ("href=\"http", "a remote hyperlink"),
    ("href='http", "a remote hyperlink"),
    ("url(http", "a remote CSS url()"),
    ("url(//", "a protocol-relative CSS url()"),
)


def find_external_resources(html: str) -> List[str]:
    """Reasons the page would fetch something remote; empty when it is clean."""
    lowered = html.lower()
    return [
        "%s: %s" % (reason, pattern)
        for pattern, reason in _FORBIDDEN_PATTERNS
        if pattern in lowered
    ]


def assert_no_external_resources(html: str) -> None:
    problems = find_external_resources(html)
    if problems:
        raise ExternalResourceError(
            "the review page must not reference external resources, found: %s"
            % "; ".join(problems)
        )


def render_page(db_path: str) -> str:
    """Read the single page file and validate it before it leaves the process.

    Three things happen here, in this order, and each of them is a promise:

    1. the two placeholders are filled in — the index path (so one static file
       serves every index) and the standing disclaimer (so its wording has one
       source instead of a copy in the HTML);
    2. a placeholder that is missing means the page was edited into a state that
       silently drops one of those values, which is an error rather than a
       cosmetic difference;
    3. the result is scanned for references to anything outside this machine.

    Steps 2 and 3 run on every render, not once in a test: a test proves the
    *current* file is clean, while this covers the next edit too.
    """
    html = PAGE_FILE.read_text(encoding="utf-8")
    for placeholder, value in (
        (DISCLAIMER_PLACEHOLDER, _escape_text(DISCLAIMER)),
        (INDEX_PLACEHOLDER, json.dumps(str(db_path))),
    ):
        if placeholder not in html:
            raise PageContractError(
                "the review page lost its %s placeholder" % placeholder.strip("_")
            )
        html = html.replace(placeholder, value)
    assert_no_external_resources(html)
    return html


def _escape_text(value: str) -> str:
    """Escape the characters that would end an HTML text node."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def _connect(db_path: str):
    return store.connect_readonly(db_path)


def _cluster_displays(member_records: Sequence[dict]) -> Tuple[str, ...]:
    """Common-parent-relative spellings, computed the same way the report does."""
    return cluster_mod.display_paths([record["path_norm"] for record in member_records])


def _member_records(rows: Sequence, cluster_id: str) -> List[dict]:
    records = []
    for row in rows:
        if str(row["cluster_id"]) != cluster_id:
            continue
        records.append(
            {
                "doc_id": int(row["doc_id"]),
                "path_norm": str(row["path_norm"]),
                "path_abs": str(row["path_abs"]),
            }
        )
    records.sort(key=lambda record: record["path_norm"])
    return records


def _group_rows(rows: Sequence) -> Dict[str, List]:
    grouped: Dict[str, List] = {}
    for row in rows:
        grouped.setdefault(str(row["cluster_id"]), []).append(row)
    return grouped


def clusters_view(conn, settings: Optional[Settings] = None) -> dict:
    """The cluster list, read from the tables module 3 wrote."""
    rows = store.cluster_rows(conn)
    grouped = _group_rows(rows)
    built_at = None
    clusters: List[dict] = []

    for cluster_id, cluster_rows in grouped.items():
        head = cluster_rows[0]
        scores = json.loads(str(head["scores_json"] or "{}"))
        records = _member_records(cluster_rows, cluster_id)
        displays = _cluster_displays(records)
        keep_id = head["keep_id"]
        keep_id = None if keep_id is None else int(keep_id)
        keep = None
        members = []
        for record, display in zip(records, displays):
            kept = keep_id is not None and record["doc_id"] == keep_id
            if kept:
                keep = display
            members.append(
                {
                    "doc_id": record["doc_id"],
                    "path": display,
                    "path_abs": record["path_abs"],
                    "kept": kept,
                    "scores": scores.get(record["path_norm"], {}),
                }
            )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "tier": str(head["tier"]),
                "tier_label": TIER_LABELS.get(str(head["tier"]), str(head["tier"])),
                "keep": keep,
                "keep_id": keep_id,
                "reason": str(head["reason"]),
                "member_count": len(members),
                "members": members,
                "built_at": str(head["built_at"]) if head["built_at"] else None,
            }
        )
        if built_at is None and head["built_at"]:
            built_at = str(head["built_at"])

    # Same order the report prints: actionable tiers first.
    order = {tier: index for index, tier in enumerate(cluster_mod.TIER_ORDER)}
    clusters.sort(
        key=lambda item: (order.get(item["tier"], len(order)), item["members"][0]["path"])
    )

    counts = {tier: 0 for tier in cluster_mod.TIER_ORDER}
    for item in clusters:
        counts[item["tier"]] = counts.get(item["tier"], 0) + 1

    empty = not clusters
    return {
        "schema": REVIEW_SCHEMA,
        "status": "empty" if empty else "ok",
        "hint": EMPTY_HINT if empty else None,
        "built_at": built_at,
        "counts": counts,
        "clusters": clusters,
        "disclaimer": DISCLAIMER,
        # The page offers the action; the execution layer decides whether a
        # request is allowed. This flag only tells the page that the button is
        # not a placeholder any more.
        "quarantine_available": True,
        "quarantine_message": QUARANTINE_HINT,
        # Batches already registered in this index, so a reviewer can undo one
        # without remembering its id. Counted from the manifests (see
        # ``quarantine.batch_summaries``).
        "batches": quarantine_mod.batch_summaries(conn),
    }


def _find_member(clusters: Sequence[dict], cluster_id: str) -> Optional[dict]:
    for item in clusters:
        if item["cluster_id"] == cluster_id:
            return item
    return None


def diff_view(
    conn,
    cluster_id: str,
    a: Optional[str] = None,
    b: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> dict:
    """The two-column diff for one pair of a cluster's members.

    ``a``/``b`` are document ids as strings. When they are omitted the pair is
    chosen for the reviewer: the recommended keep against the first other member
    (or, for a template cluster, which has no keep, the first two members).
    That is a *default selection*, not a decision — ``tier``, ``keep`` and
    ``reason`` in the reply are still the stored ones.

    Raises :class:`KeyError` for an unknown cluster or a document that is not a
    member, and :class:`ValueError` for a pair that is not two distinct members.
    The HTTP layer maps those onto 404 and 400.
    """
    view = clusters_view(conn, settings)
    item = _find_member(view["clusters"], cluster_id)
    if item is None:
        raise KeyError("no cluster %s in this index" % cluster_id)

    members = item["members"]
    by_id = {str(member["doc_id"]): member for member in members}

    left_id = a
    right_id = b
    if left_id is None:
        left_id = str(item["keep_id"]) if item["keep_id"] is not None else str(members[0]["doc_id"])
    if right_id is None:
        right_id = next(
            (str(member["doc_id"]) for member in members if str(member["doc_id"]) != left_id),
            None,
        )
    if right_id is None:
        raise ValueError("cluster %s has no second member to compare" % cluster_id)
    if left_id == right_id:
        raise ValueError("--a and --b must be two different members")

    for doc_id in (left_id, right_id):
        if doc_id not in by_id:
            raise KeyError("%s is not a member of cluster %s" % (doc_id, cluster_id))

    texts = store.document_texts(conn, [int(left_id), int(right_id)])
    left_text = texts.get(int(left_id), "")
    right_text = texts.get(int(right_id), "")
    result = diffing.diff_documents(left_text, right_text, settings)

    return {
        "schema": REVIEW_SCHEMA,
        "cluster_id": cluster_id,
        "tier": item["tier"],
        "tier_label": item["tier_label"],
        "keep": item["keep"],
        "reason": item["reason"],
        "summary": result.summary,
        "stats": result.stats.as_dict(),
        "left": {
            "doc_id": int(left_id),
            "path": by_id[left_id]["path"],
            "path_abs": by_id[left_id]["path_abs"],
            "segments": [segment.as_dict() for segment in result.left_segments],
        },
        "right": {
            "doc_id": int(right_id),
            "path": by_id[right_id]["path"],
            "path_abs": by_id[right_id]["path_abs"],
            "segments": [segment.as_dict() for segment in result.right_segments],
        },
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def web_stack_available() -> bool:
    """True when the optional ``web`` extra was importable.

    The CLI asks before it announces a URL, so a missing extra is reported as
    exit code 3 instead of a URL that is never served.
    """
    return FastAPI is not None


def create_app(db_path: str, settings: Optional[Settings] = None):
    """Build the FastAPI app that serves the review page for one index.

    ``docs_url``/``redoc_url``/``openapi_url`` are disabled because Swagger UI and
    ReDoc load their assets from a CDN: leaving them on would make the "no page
    loads anything remote" promise false for a URL the user could easily reach.
    """
    if FastAPI is None:
        raise MissingWebDependency("fastapi")
    cfg = settings or DEFAULT_SETTINGS

    app = FastAPI(
        title="DocDedupe review",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def with_index(view):
        conn = _connect(db_path)
        try:
            return view(conn)
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def page() -> object:
        return HTMLResponse(render_page(db_path))

    @app.get("/api/clusters")
    def api_clusters() -> object:
        return JSONResponse(with_index(lambda conn: clusters_view(conn, cfg)))

    @app.get("/api/clusters/{cluster_id}/diff")
    def api_diff(cluster_id: str, a: Optional[str] = None, b: Optional[str] = None) -> object:
        try:
            payload = with_index(lambda conn: diff_view(conn, cluster_id, a, b, cfg))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse(payload)

    def _confirm(request_body: Optional[dict]) -> dict:
        """The rule every destructive route must satisfy (``plan/03`` module 4)."""
        if not isinstance(request_body, dict) or request_body.get("confirm") is not True:
            raise HTTPException(
                status_code=400,
                detail="this action requires \"confirm\": true in the request body",
            )
        return request_body

    def _run_action(action, **kwargs) -> object:
        """Run one execution-layer call and translate its refusals into HTTP.

        The body returned on success is ``BatchResult.as_dict()`` — the very dict
        ``doc-dedupe quarantine --format json`` prints — so the page and the
        command line are two views of one result, not two implementations of one
        action. Turning a refusal into 400 here and a missing batch into 404 is
        transport mapping, not policy: membership, the "changed since the scan"
        check and the hash verification all live in the execution layer, which is
        what keeps a hand-written POST from moving a file the reviewer never saw.
        """
        try:
            result = action(db_path, **kwargs)
        except quarantine_mod.UnknownBatch as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except quarantine_mod.QuarantineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse(result.as_dict())

    @app.post("/api/quarantine")
    async def api_quarantine(request: Request) -> object:
        payload = _confirm(await _json_body(request))
        return _run_action(
            quarantine_mod.execute_quarantine,
            cluster_id=payload.get("cluster_id"),
            keep=payload.get("keep"),
            move=payload.get("move") or [],
            confirm=True,
        )

    @app.post("/api/undo")
    async def api_undo(request: Request) -> object:
        payload = _confirm(await _json_body(request))
        batch_id = payload.get("batch_id") or payload.get("batch")
        if not batch_id:
            raise HTTPException(
                status_code=400, detail="this action requires a \"batch_id\""
            )
        return _run_action(
            quarantine_mod.execute_undo, batch_id=str(batch_id), confirm=True
        )

    return app


async def _json_body(request) -> Optional[dict]:
    """Parse the request body, tolerating an empty one (then ``confirm`` fails)."""
    try:
        payload = await request.json()
    except Exception:  # malformed or empty body: treated as "no confirmation"
        return None
    return payload if isinstance(payload, dict) else None


def serve(
    db_path: str,
    host: str = DEFAULT_SETTINGS.web_host,
    port: int = DEFAULT_SETTINGS.web_port,
    *,
    open_browser: bool = False,
    settings: Optional[Settings] = None,
) -> int:
    """Run the local review server until interrupted.

    Blocking, like any server. ``host`` was already restricted to loopback by the
    CLI before this function is reached; it is repeated in the log line so the
    user can see the address that was actually bound.
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised by patching sys.modules
        raise MissingWebDependency("uvicorn") from exc

    app = create_app(db_path, settings)
    url = "http://%s:%d/" % (host, port)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    uvicorn.run(app, host=host, port=int(port), log_level="info")
    return 0


__all__ = [
    "DISCLAIMER",
    "DISCLAIMER_PLACEHOLDER",
    "EMPTY_HINT",
    "ExternalResourceError",
    "INDEX_PLACEHOLDER",
    "PAGE_FILE",
    "PageContractError",
    "QUARANTINE_HINT",
    "REVIEW_SCHEMA",
    "assert_no_external_resources",
    "clusters_view",
    "create_app",
    "diff_view",
    "find_external_resources",
    "render_page",
    "serve",
    "web_stack_available",
]
