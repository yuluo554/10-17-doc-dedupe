"""Module 4's review page: the read model, the HTTP surface, and the promises.

The tests are grouped by the three promises the module makes.

* **It displays what the index says.** The cluster list is compared against the
  ``clusters`` table read with plain ``sqlite3``, *and* against the JSON report
  ``report`` wrote in the same run. Three independent readings of the same
  truth; if the page re-derived a tier or a score, one of them would differ.
* **It cannot write.** The connection is opened read-only by
  :func:`docdedupe.store.connect_readonly`, and a static check keeps the writable
  opener out of the web module. A scanned-but-not-yet-reported index is the case
  that would tempt an implementation to "helpfully" cluster on demand; the tests
  assert that it instead explains itself and leaves the tables empty.
* **It never goes remote.** The page is one file, the render path asserts it on
  every request, and the check is shown to be able to fail.

The socket-level audit lives in ``tests/test_no_network.py``; this file is about
what the page says and what it refuses to do.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from docdedupe import cli, diffing, quarantine, report, scanner, store
from docdedupe.web import server

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
WEB_DIR = REPO_ROOT / "src" / "docdedupe" / "web"

pytestmark = pytest.mark.skipif(
    not (SAMPLES / "truth" / "manifest.json").is_file(),
    reason="sample corpus is not generated",
)


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


@pytest.fixture(scope="module")
def reported(tmp_path_factory):
    """A scanned *and reported* index, produced by the real CLI commands.

    Built through ``cli.main`` rather than by calling the internals so the page is
    tested against exactly what the two user-facing commands leave behind.
    """
    db_path = tmp_path_factory.mktemp("m4-web") / "index.db"
    conn = store.connect(str(db_path))
    try:
        scanner.scan(conn, [SAMPLES], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
    finally:
        conn.close()

    code, out, err = run_cli(["report", "--db", str(db_path), "--format", "json"])
    assert code == 1, err
    return db_path, json.loads(out)


@pytest.fixture(scope="module")
def scanned_only(tmp_path_factory):
    """An index that was scanned but never reported: ``clusters`` is empty."""
    samples = tmp_path_factory.mktemp("m4-two-docs")
    body = "示例项目甲 正文。\n" + "本段用于构成足够长的正文内容。" * 12 + "\n"
    (samples / "方案_a.txt").write_text(body, encoding="utf-8")
    (samples / "方案_b.txt").write_text(body, encoding="utf-8")
    (samples / "无关.md").write_text("完全不同的另一份文档。" * 20, encoding="utf-8")

    db_path = tmp_path_factory.mktemp("m4-scanned") / "index.db"
    conn = store.connect(str(db_path))
    try:
        scanner.scan(conn, [str(samples)], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture(scope="module")
def client(reported):
    pytest.importorskip("fastapi", reason="the review page needs the 'web' extra")
    from fastapi.testclient import TestClient

    return TestClient(server.create_app(str(reported[0])))


@pytest.fixture(scope="module")
def api_clusters(client):
    response = client.get("/api/clusters")
    assert response.status_code == 200
    return response.json()


def _table_rows(db_path, sql):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The page: one file, no build step, no remote reference
# ---------------------------------------------------------------------------


def test_the_page_is_a_single_file_with_no_build_step():
    assert sorted(path.name for path in server.STATIC_DIR.iterdir()) == ["index.html"]
    stray = [
        path.relative_to(REPO_ROOT).as_posix()
        for pattern in ("*.js", "*.css", "*.ts", "*.map")
        for path in WEB_DIR.rglob(pattern)
    ]
    assert stray == [], "the page is served prebuilt from one file: %s" % stray


def test_the_page_loads_nothing_from_anywhere_else():
    """A static scan of the served page, in the same shape as the runtime check."""
    html = server.render_page("index.html")

    assert server.find_external_resources(html) == []
    assert "<script src=" not in html
    # Every request the page makes is same-origin and relative. All of them go
    # through one helper, so both the helper and its callers are checked: a
    # request that left the origin would have to spell out a URL somewhere.
    assert "fetch(url, options)" in html, "the single request helper disappeared"
    targets = re.findall(r'fetchJson\(\s*("(?:[^"\\]|\\.)*")', html)
    assert targets, "the page no longer fetches anything; the guard would be vacuous"
    for target in targets:
        assert target.startswith('"/'), "non-relative request target: %r" % target


@pytest.mark.parametrize(
    "snippet",
    [
        '<link rel="stylesheet" href="style.css">',
        "<style>@import url(theme.css);</style>",
        '<script src="app.js"></script>',
        '<script src="//cdn.example.com/app.js"></script>',
        '<img src="http://example.com/logo.png">',
        '<iframe src="page.html"></iframe>',
        "<style>body{background:url(https://example.com/a.png)}</style>",
    ],
)
def test_the_external_reference_check_can_fail(snippet):
    """A guard that cannot fail is not a guard."""
    with pytest.raises(server.ExternalResourceError):
        server.assert_no_external_resources("<html><body>%s</body></html>" % snippet)


def test_the_index_path_is_injected_into_the_page(reported):
    html = server.render_page(str(reported[0]))

    assert server.INDEX_PLACEHOLDER not in html
    assert json.dumps(str(reported[0])) in html


def test_the_page_carries_the_standing_disclaimer(client, api_clusters):
    response = client.get("/")

    assert response.status_code == 200
    assert server.DISCLAIMER in response.text
    assert api_clusters["disclaimer"] == server.DISCLAIMER


def test_swagger_and_redoc_are_disabled(client):
    """They load their assets from a CDN, so the routes must not exist."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---------------------------------------------------------------------------
# It displays what the index says
# ---------------------------------------------------------------------------


def test_the_cluster_list_matches_the_clusters_table(reported, api_clusters):
    db_path, _ = reported
    rows = _table_rows(
        db_path,
        "SELECT cluster_id, tier, keep_id, reason FROM clusters ORDER BY cluster_id",
    )
    by_id = {item["cluster_id"]: item for item in api_clusters["clusters"]}

    assert len(rows) == len(by_id) == 27
    for row in rows:
        shown = by_id[row["cluster_id"]]
        assert shown["tier"] == row["tier"]
        assert shown["keep_id"] == row["keep_id"]
        assert shown["reason"] == row["reason"]


def test_the_cluster_members_match_the_cluster_members_table(reported, api_clusters):
    db_path, _ = reported
    rows = _table_rows(
        db_path,
        "SELECT cluster_id, doc_id FROM cluster_members ORDER BY cluster_id, doc_id",
    )
    expected = {}
    for row in rows:
        expected.setdefault(row["cluster_id"], []).append(row["doc_id"])

    for item in api_clusters["clusters"]:
        shown = sorted(member["doc_id"] for member in item["members"])
        assert shown == expected[item["cluster_id"]]
        assert item["member_count"] == len(shown)


def test_the_keep_recommendation_matches_the_report_for_every_cluster(reported, api_clusters):
    """Three readings of one truth: the table, the report file, and the page."""
    _, report_payload = reported
    from_report = {item["cluster_id"]: item for item in report_payload["clusters"]}

    assert set(from_report) == {item["cluster_id"] for item in api_clusters["clusters"]}
    for item in api_clusters["clusters"]:
        expected = from_report[item["cluster_id"]]
        assert item["tier"] == expected["tier"]
        assert item["keep"] == expected["keep"]
        assert item["reason"] == expected["reason"]
        assert [member["path"] for member in item["members"]] == [
            member["path"] for member in expected["members"]
        ]


def test_tier_labels_come_from_the_report_labels(api_clusters):
    for item in api_clusters["clusters"]:
        assert item["tier_label"] == report.TIER_LABELS[item["tier"]]


def test_scores_are_read_from_the_stored_score_table(reported, api_clusters):
    """``scores_json`` is or is not the source; a recompute would show up here."""
    db_path, _ = reported
    rows = _table_rows(db_path, "SELECT cluster_id, scores_json FROM clusters")
    stored = {row["cluster_id"]: json.loads(row["scores_json"]) for row in rows}

    checked = 0
    for item in api_clusters["clusters"]:
        plain = _table_rows(
            db_path,
            "SELECT d.path_norm FROM cluster_members m JOIN documents d ON d.id = m.doc_id "
            "WHERE m.cluster_id = '%s'" % item["cluster_id"],
        )
        for member, row in zip(item["members"], plain):
            assert member["scores"] == stored[item["cluster_id"]][row["path_norm"]]
            checked += 1
    assert checked >= 27


def test_the_cluster_counts_match_the_table(api_clusters):
    counts = {tier: 0 for tier in ("exact", "near", "template")}
    for item in api_clusters["clusters"]:
        counts[item["tier"]] += 1

    assert api_clusters["counts"] == counts
    assert api_clusters["status"] == "ok"


# ---------------------------------------------------------------------------
# The diff endpoint
# ---------------------------------------------------------------------------


def test_the_diff_endpoint_uses_the_single_summary_implementation(client, api_clusters):
    """The page's sentence must be the module's sentence, not a copy of it."""
    cluster = api_clusters["clusters"][0]
    response = client.get("/api/clusters/%s/diff" % cluster["cluster_id"])
    assert response.status_code == 200
    payload = response.json()

    ids = [payload["left"]["doc_id"], payload["right"]["doc_id"]]
    conn = store.connect_readonly(str(_reported_db(client)))
    try:
        texts = store.document_texts(conn, ids)
    finally:
        conn.close()

    expected = diffing.diff_documents(texts[ids[0]], texts[ids[1]])
    assert payload["summary"] == expected.summary
    assert payload["stats"] == expected.stats.as_dict()
    assert [segment["text"] for segment in payload["left"]["segments"]] == [
        segment.text for segment in expected.left_segments
    ]
    assert [segment["text"] for segment in payload["right"]["segments"]] == [
        segment.text for segment in expected.right_segments
    ]
    assert [segment["spans"] for segment in payload["left"]["segments"]] == [
        [[start, end] for start, end in segment.spans] for segment in expected.left_segments
    ]


def test_the_diff_endpoint_defaults_to_the_recommended_keep(client, api_clusters):
    exact = [item for item in api_clusters["clusters"] if item["keep_id"] is not None][0]
    payload = client.get("/api/clusters/%s/diff" % exact["cluster_id"]).json()

    assert payload["left"]["doc_id"] == exact["keep_id"]
    assert payload["right"]["doc_id"] != exact["keep_id"]
    assert payload["tier"] == exact["tier"]


def test_the_diff_endpoint_returns_both_sides_in_row_order(client, api_clusters):
    """Every non-``same`` left segment is mirrored by a right one, or is a ``del``."""
    cluster = [item for item in api_clusters["clusters"] if item["tier"] == "near"][0]
    payload = client.get("/api/clusters/%s/diff" % cluster["cluster_id"]).json()

    left = payload["left"]["segments"]
    right = payload["right"]["segments"]
    assert left and right
    for segment in left + right:
        assert segment["kind"] in diffing.SEGMENT_KINDS
    # The two cursors meet: paired rows consume one segment per side, one-sided
    # rows consume one segment on that side only.
    i = j = 0
    paired = 0
    while i < len(left) or j < len(right):
        if i < len(left) and j < len(right) and left[i]["kind"] != "add" and right[j]["kind"] != "del":
            assert left[i]["kind"] == right[j]["kind"]
            i += 1
            j += 1
            paired += 1
        elif i < len(left) and left[i]["kind"] == "del":
            i += 1
        else:
            assert right[j]["kind"] == "add"
            j += 1
    assert paired > 0


def test_the_diff_endpoint_reports_summaries_the_definitions_allow(client, api_clusters):
    allowed_prefix = "仅差 "
    allowed = {
        diffing.SUMMARY_IDENTICAL,
        diffing.SUMMARY_CONTAINER,
        diffing.SUMMARY_SIGNATURE,
    }
    for cluster in api_clusters["clusters"]:
        payload = client.get("/api/clusters/%s/diff" % cluster["cluster_id"]).json()
        summary = payload["summary"]
        assert summary in allowed or summary.startswith(allowed_prefix), summary
        assert "页" not in summary


def test_the_diff_endpoint_rejects_bad_requests(client, api_clusters):
    cluster = api_clusters["clusters"][0]
    first, second = cluster["members"][0]["doc_id"], cluster["members"][1]["doc_id"]

    assert client.get("/api/clusters/no-such-cluster/diff").status_code == 404
    assert client.get("/api/clusters/%s/diff?a=1&b=2" % cluster["cluster_id"]).status_code == 404
    assert (
        client.get(
            "/api/clusters/%s/diff?a=%d&b=%d" % (cluster["cluster_id"], first, first)
        ).status_code
        == 400
    )


def _reported_db(client) -> Path:
    """Recover the index path the app was built for (from its own render)."""
    html = client.get("/").text
    match = re.search(r'var INDEX_PATH = (".*?");', html)
    assert match is not None, "the page no longer carries the index path"
    return Path(json.loads(match.group(1)))


# ---------------------------------------------------------------------------
# It cannot write
# ---------------------------------------------------------------------------


def test_the_read_only_connection_refuses_writes(reported):
    db_path, _ = reported
    conn = store.connect_readonly(str(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM clusters")
    finally:
        conn.close()


def test_the_web_module_opens_the_index_read_only():
    """A static check: the writable opener must not appear in the web module."""
    tree = ast.parse((WEB_DIR / "server.py").read_text(encoding="utf-8"), filename="server.py")
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)

    assert "connect_readonly" in called
    assert "connect" not in called
    assert "replace_clusters" not in called


def test_the_web_module_does_not_recompute_tiers_or_scores():
    """``clusters`` holds the decision; the page only reads it."""
    tree = ast.parse((WEB_DIR / "server.py").read_text(encoding="utf-8"), filename="server.py")
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            called.add(func.attr)
        elif isinstance(func, ast.Name):
            called.add(func.id)

    for forbidden in (
        "build_clusters",
        "clusters_from_index",
        "template_evidence",
        "scores_json",
        "shingle_set",
        "jaccard_from_hashes",
        "load_inputs",
    ):
        assert forbidden not in called, "the web layer calls %r" % forbidden


def test_the_review_request_does_not_close_the_writable_opener(reported, monkeypatch):
    """Adjacent to the static check: an actual request must not open for writing."""
    from fastapi.testclient import TestClient

    def refuse(*args, **kwargs):  # pragma: no cover - only fires on a regression
        raise AssertionError("the review page opened the index for writing")

    monkeypatch.setattr(store, "connect", refuse)
    app = server.create_app(str(reported[0]))
    client = TestClient(app)

    assert client.get("/api/clusters").status_code == 200
    cluster_id = client.get("/api/clusters").json()["clusters"][0]["cluster_id"]
    assert client.get("/api/clusters/%s/diff" % cluster_id).status_code == 200


def test_a_scanned_but_unreported_index_explains_itself_without_writing(scanned_only):
    """The page must work on a fresh scan — and must not cluster behind the user's back.

    Clustering on demand would have to write the result, which would make the
    page a writer *and* make it disagree with the table whenever the write did not
    happen. The chosen behaviour is to say which command to run.
    """
    from fastapi.testclient import TestClient

    client = TestClient(server.create_app(str(scanned_only)))

    page = client.get("/")
    assert page.status_code == 200

    payload = client.get("/api/clusters").json()
    assert payload["status"] == "empty"
    assert payload["clusters"] == []
    assert "report" in payload["hint"]

    conn = store.connect(str(scanned_only))
    try:
        assert store.count_clusters(conn) == 0, "the review page wrote a clustering result"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Isolation goes through the execution layer of module 5
# ---------------------------------------------------------------------------
#
# These tests need a corpus that may actually be moved, so they scan a copy of
# two duplicates in ``tmp_path``. The module-scoped ``reported`` fixture above
# reads ``data/samples``, which is frozen: quarantining a sample would mutate the
# corpus and break the sample-freeze gate.


@pytest.fixture
def movable(tmp_path_factory):
    """A tmp corpus, scanned and reported, whose files may safely be moved.

    Function-scoped on purpose: each test starts from an untouched tree, so
    "nothing else moved" assertions cannot be confused by a previous test's
    quarantine directory.
    """
    root = tmp_path_factory.mktemp("m5-web") / "docs"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir(parents=True)
    body = "示例项目甲 正文。\n" + "本段用于构成足够长的正文内容。" * 12 + "\n"
    (root / "A" / "方案.txt").write_text(body, encoding="utf-8")
    (root / "B" / "方案.txt").write_text(body, encoding="utf-8")
    (root / "无关.md").write_text("完全不同的另一份文档。" * 20, encoding="utf-8")

    db_path = tmp_path_factory.mktemp("m5-web-db") / "index.db"
    conn = store.connect(str(db_path))
    try:
        scanner.scan(conn, [str(root)], scanned_at="2026-10-17T00:00:00Z", clock=lambda: 0.0)
        conn.commit()
    finally:
        conn.close()
    code, out, err = run_cli(["report", "--db", str(db_path), "--format", "json"])
    assert code == 1, err
    return db_path, root, json.loads(out)


@pytest.fixture
def movable_client(movable):
    pytest.importorskip("fastapi", reason="the review page needs the 'web' extra")
    from fastapi.testclient import TestClient

    return TestClient(server.create_app(str(movable[0])))


def _documents(root: Path):
    """The corpus files only: the quarantine record is not part of the corpus.

    A batch directory survives an undo on purpose — the manifest is the record of
    what was moved where, and this tool has no deletion path at all. So "the
    corpus is unchanged" is asserted on everything *outside*
    ``.dedupe-quarantine``, and the batch directory gets its own assertion.
    """
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if quarantine.QUARANTINE_DIR_NAME not in path.relative_to(root).parts
    )


def _cluster_action_body(clusters_payload):
    """``(POST body, cluster)`` moving every member but the recommended keep."""
    cluster = clusters_payload["clusters"][0]
    kept = [member for member in cluster["members"] if member["kept"]]
    keep = (kept[0] if kept else cluster["members"][0])
    return {
        "cluster_id": cluster["cluster_id"],
        "keep": keep["path_abs"],
        "move": [
            member["path_abs"]
            for member in cluster["members"]
            if member["doc_id"] != keep["doc_id"]
        ],
        "confirm": True,
    }, cluster


def test_the_quarantine_endpoint_requires_confirmation(client, api_clusters):
    cluster = api_clusters["clusters"][0]
    body = {"cluster_id": cluster["cluster_id"], "keep": None, "move": [], "confirm": False}

    assert client.post("/api/quarantine", json=body).status_code == 400
    assert client.post("/api/undo", json={"batch_id": "b1", "confirm": False}).status_code == 400
    assert client.post("/api/quarantine", json={"confirm": "yes"}).status_code == 400
    # The refusal happens before the execution layer, so the frozen corpus the
    # ``reported`` fixture reads is untouched by these three requests.
    assert not (SAMPLES / ".dedupe-quarantine").exists()


def test_the_quarantine_endpoint_moves_the_listed_members_and_undo_puts_them_back(
    movable, movable_client
):
    """The page's action is the command's action: move, record, restore."""
    db_path, root, _ = movable
    clusters = movable_client.get("/api/clusters").json()
    body, cluster = _cluster_action_body(clusters)
    before = _documents(root)

    response = movable_client.post("/api/quarantine", json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["action"] == "quarantine"
    assert payload["cluster_id"] == cluster["cluster_id"]
    assert [item["status"] for item in payload["items"]] == ["moved"]

    # The file is really in the quarantine, byte for byte, and no longer where it
    # was; the diff endpoint still reads it from the index.
    moved = payload["items"][0]
    assert not os.path.exists(moved["original_path"])
    assert os.path.isfile(moved["quarantine_path"])
    assert quarantine.file_sha256(moved["quarantine_path"]) == moved["sha256"]

    # The batch is visible on the page, so the user can undo it from there.
    after_payload = movable_client.get("/api/clusters").json()
    assert [batch["batch_id"] for batch in after_payload["batches"]] == [payload["batch_id"]]
    assert after_payload["batches"][0]["moved"] == 1

    undo = movable_client.post(
        "/api/undo", json={"batch_id": payload["batch_id"], "confirm": True}
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["counts"]["restored"] == 1
    assert os.path.isfile(moved["original_path"])
    assert quarantine.file_sha256(moved["original_path"]) == moved["sha256"]
    assert _documents(root) == before
    # The batch directory stays behind as the record of the action (nothing in
    # this tool deletes); the page reads its status from the manifest.
    assert (root / quarantine.QUARANTINE_DIR_NAME / payload["batch_id"]).is_dir()
    final = movable_client.get("/api/clusters").json()["batches"][0]
    assert (final["moved"], final["restored"], final["pending"]) == (0, 1, 0)


def test_the_quarantine_endpoint_refuses_a_path_that_is_not_a_member(movable, movable_client):
    """A POST is not a licence to move anything on the disk."""
    clusters = movable_client.get("/api/clusters").json()
    body, _ = _cluster_action_body(clusters)
    conn = store.connect_readonly(str(movable[0]))
    try:
        unrelated = [
            str(row["path_abs"])
            for row in store.cluster_document_rows(conn)
            if "无关" in str(row["path_abs"])
        ]
    finally:
        conn.close()
    assert unrelated, "the unrelated file should be indexed"
    before = _documents(movable[1])

    response = movable_client.post("/api/quarantine", json=dict(body, move=unrelated))

    assert response.status_code == 400
    assert "not a member" in response.json()["detail"]
    assert _documents(movable[1]) == before


def test_the_quarantine_response_is_the_execution_layers_own_payload(
    movable, movable_client, monkeypatch
):
    """The HTTP layer must not grow a second account of one action.

    The route is checked against a ``BatchResult`` produced by the same function
    the CLI calls: if the server assembled its own body (or moved files itself),
    the two would drift the moment either side changed.
    """
    db_path, root, _ = movable
    clusters = movable_client.get("/api/clusters").json()
    body, cluster = _cluster_action_body(clusters)
    before = _documents(root)

    recorded = {}

    def fake_execute(db, **kwargs):
        recorded["db"] = db
        recorded["kwargs"] = kwargs
        return quarantine.BatchResult(
            action="quarantine",
            batch_id="20261017T101500Z-7f3a",
            cluster_id=kwargs["cluster_id"],
            keep=kwargs["keep"],
            manifest_path=str(root / ".dedupe-quarantine" / "x" / "manifest.json"),
            quarantine_dir=str(root / ".dedupe-quarantine" / "x"),
            items=(
                {
                    "status": quarantine.STATUS_MOVED,
                    "original_path": "a",
                    "quarantine_path": "b",
                    "sha256": "c",
                    "size": 1,
                    "moved_at": "2026-10-17T10:15:01Z",
                },
            ),
        )

    monkeypatch.setattr(server.quarantine_mod, "execute_quarantine", fake_execute)
    response = movable_client.post("/api/quarantine", json=body)

    assert response.status_code == 200
    assert response.json()["batch_id"] == "20261017T101500Z-7f3a"
    assert response.json()["counts"] == {
        "items": 1,
        "moved": 1,
        "restored": 0,
        "pending": 0,
    }
    assert recorded["db"] == str(db_path)
    assert recorded["kwargs"]["confirm"] is True
    assert recorded["kwargs"]["cluster_id"] == cluster["cluster_id"]
    # Nothing moved: the stub replaced the execution layer entirely, so the tree
    # after the request is exactly the tree before it.
    assert _documents(root) == before


def test_the_undo_endpoint_reports_an_unknown_batch_as_404(movable_client):
    response = movable_client.post("/api/undo", json={"batch_id": "nope", "confirm": True})
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_the_undo_endpoint_requires_a_batch_id(movable_client):
    response = movable_client.post("/api/undo", json={"confirm": True})
    assert response.status_code == 400
    assert "batch_id" in response.json()["detail"]


def test_the_page_offers_the_action_and_describes_it_truthfully(movable_client):
    """M4 said "not available yet"; M5 connects it, and the wording has to follow."""
    payload = movable_client.get("/api/clusters").json()
    assert payload["quarantine_available"] is True
    assert "复原" in payload["quarantine_message"]
    assert ".dedupe-quarantine" in payload["quarantine_message"]

    html = movable_client.get("/").text
    # The page asks the server to act through the documented routes and passes
    # the confirmation through; it holds no logic of its own about what may move.
    assert 'fetchJson("/api/quarantine"' in html
    assert 'fetchJson("/api/undo"' in html
    assert "confirm: true" in html
    assert "复原" in html
