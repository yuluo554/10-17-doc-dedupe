"""Dynamic audit: no module opens an outbound network connection.

``plan/06`` section 4 promises "pure local, zero upload, zero telemetry", and the
static import check in ``tests/test_repo_invariants.py`` only proves the package
does not import an HTTP client. That is necessary but not sufficient: a socket
could be opened without an HTTP client, and a dependency could open one on the
project's behalf.

So this file *intercepts the socket layer* and drives the real pipeline —
``scan`` → ``report`` → the review page's read models → its HTML render → real
requests through the ASGI app — while every attempt to connect out, resolve a
name, or create a connection is recorded and refused. The run must complete, and
the recorded attempt list must be empty.

The one legitimate socket in the project is the *listening* socket uvicorn opens
for the review page, which is why it is not exercised here. Its address is
asserted separately, in ``test_web.py`` and ``test_cli_contract.py`` (loopback
only, rejected at the CLI entry point), and
:func:`test_the_server_is_started_on_the_loopback_address` below pins the actual
arguments uvicorn receives.
"""

from __future__ import annotations

import io
import re
import socket
import sys
from pathlib import Path

import pytest

from docdedupe import cli, diffing, store
from docdedupe.web import server

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"


class NetworkGuard:
    """Refuse and record outbound connection attempts.

    Patches the three places an outbound connection can start: ``socket.socket``
    (its ``connect`` / ``connect_ex`` methods), ``socket.create_connection``, and
    ``socket.getaddrinfo`` (a name lookup is already an act of reaching out).
    ``socket.socketpair`` is left alone: it is a purely local, already-connected
    pair, and refusing it would police something that cannot leave the machine.

    Two modes, because "does not reach the network" and "never uses a socket" are
    different promises and only the first one is made:

    * ``allow_loopback=False`` (strict) — every attempt is refused and recorded.
      Used for the pipeline that should touch nothing but files and the index.
    * ``allow_loopback=True`` — traffic to ``127.0.0.0/8`` and ``::1`` passes
      through (counted in :attr:`loopback`), everything else is still refused.
      The HTTP test client runs a real loopback transport, and loopback traffic
      cannot leave the machine, so refusing it would fail the audit for a
      harness detail rather than for a privacy leak.
    """

    def __init__(self, allow_loopback: bool = False) -> None:
        self.allow_loopback = allow_loopback
        self.attempts = []
        self.loopback = []

    def __enter__(self) -> "NetworkGuard":
        guard = self
        self._socket_class = socket.socket
        self._create_connection = socket.create_connection
        self._getaddrinfo = socket.getaddrinfo

        real_socket = self._socket_class

        def classify(kind, address):
            """Record the attempt; return True when it may proceed."""
            guard.attempts.append((kind, address))
            if guard.allow_loopback and _is_loopback_address(address):
                guard.loopback.append((kind, address))
                return True
            return False

        class GuardedSocket(real_socket):  # type: ignore[misc, valid-type]
            def connect(self, address):  # noqa: D102
                if classify("connect", address):
                    return real_socket.connect(self, address)
                raise AssertionError("outbound connect(%r)" % (address,))

            def connect_ex(self, address):  # noqa: D102
                if classify("connect_ex", address):
                    return real_socket.connect_ex(self, address)
                raise AssertionError("outbound connect_ex(%r)" % (address,))

        def guarded_create_connection(address, *args, **kwargs):
            if classify("create_connection", address):
                return guard._create_connection(address, *args, **kwargs)
            raise AssertionError("outbound create_connection(%r)" % (address,))

        def guarded_getaddrinfo(host, *args, **kwargs):
            if classify("getaddrinfo", host):
                return guard._getaddrinfo(host, *args, **kwargs)
            raise AssertionError("name resolution for %r" % (host,))

        socket.socket = GuardedSocket  # type: ignore[misc]
        socket.create_connection = guarded_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        socket.socket = self._socket_class  # type: ignore[misc]
        socket.create_connection = self._create_connection  # type: ignore[assignment]
        socket.getaddrinfo = self._getaddrinfo  # type: ignore[assignment]

    @property
    def remote(self):
        """Attempts that were not loopback: these are the ones that matter."""
        return [item for item in self.attempts if item not in self.loopback]


def _is_loopback_address(address) -> bool:
    """True for a tuple address whose host is a loopback literal or localhost."""
    if isinstance(address, tuple):
        host = address[0] if address else None
    else:
        host = address
    if host is None:
        return True  # a bind with no host, or a wildcard resolution
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    text = str(host).strip("[]").lower()
    if text in ("", "localhost", "::1"):
        return True
    return re.match(r"^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$", text) is not None


class _Capture:
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


@pytest.fixture()
def corpus(tmp_path):
    """A small corpus: one duplicate pair across two formats, plus an unrelated file.

    The paragraphs are all distinct on purpose. A body made of one repeated line
    is stripped by the running-head rule in ``data/README.md`` 4.3 — which is the
    documented behaviour, but it would leave this fixture testing "two empty
    documents look alike" instead of "two documents look alike".
    """
    root = tmp_path / "docs"
    root.mkdir()
    body = (
        "示例项目甲 · 投资可行性研究\n"
        "编制单位：示例科技有限公司A\n"
        + "".join(
            "第%d段正文内容，用于构成足够长的比较语料。\n" % index for index in range(1, 13)
        )
    )
    (root / "方案_a.txt").write_text(body, encoding="utf-8")
    (root / "方案_b.md").write_text(body, encoding="utf-8")
    (root / "无关.md").write_text("完全不同的另一份文档。" * 20, encoding="utf-8")
    return root


def test_the_guard_actually_intercepts_a_socket(corpus):
    """Prove the audit is not inert before trusting the runs below."""
    with NetworkGuard() as guard:
        with pytest.raises(AssertionError, match="outbound connect"):
            socket.socket().connect(("example.invalid", 80))
        with pytest.raises(AssertionError, match="name resolution"):
            socket.getaddrinfo("example.invalid", 80)
        with pytest.raises(AssertionError, match="outbound create_connection"):
            socket.create_connection(("example.invalid", 80))
        # Strict mode refuses loopback too: the pipeline under it needs no socket.
        with pytest.raises(AssertionError, match="outbound connect"):
            socket.socket().connect(("127.0.0.1", 9))

    assert len(guard.attempts) == 4
    assert len(guard.remote) == 4
    # The guard is gone afterwards, or the rest of the suite could not run.
    socket.socket()


def test_loopback_traffic_is_carried_through_when_it_is_allowed(corpus):
    """The tolerant mode lets the local test transport work, and only that.

    Proving this matters: if the tolerant mode silently swallowed everything, the
    HTTP step below would pass without checking anything.
    """
    with NetworkGuard(allow_loopback=True) as guard:
        try:
            socket.socket().connect(("127.0.0.1", 9))
        except AssertionError:  # pragma: no cover - a regression in the guard
            pytest.fail("loopback traffic must be allowed through in tolerant mode")
        except OSError:
            pass  # nothing listening on port 9: the connection was attempted for real

        with pytest.raises(AssertionError, match="outbound connect"):
            socket.socket().connect(("example.invalid", 80))

    assert len(guard.loopback) == 1
    assert guard.remote == [("connect", ("example.invalid", 80))]


def test_the_whole_pipeline_opens_nothing(corpus, tmp_path):
    """scan → report → the page's read models → HTML render, with no socket at all.

    Strict mode: the CLI and the render path must not touch a socket of any kind,
    loopback included. Driven through ``cli.main`` so the audit covers the code
    paths a user actually takes.
    """
    db_path = tmp_path / "index.db"

    with NetworkGuard() as guard:
        code, out, err = run_cli(["scan", str(corpus), "--db", str(db_path)])
        assert code == 0, err

        code, out, err = run_cli(["report", "--db", str(db_path), "--format", "json"])
        assert code == 1, err

        assert server.render_page(str(db_path))

        conn = store.connect_readonly(str(db_path))
        try:
            view = server.clusters_view(conn, None)
            payload = server.diff_view(conn, view["clusters"][0]["cluster_id"])
        finally:
            conn.close()
        assert payload["summary"]

    assert guard.attempts == [], "the pipeline reached out: %s" % guard.attempts


def test_the_http_surface_sends_nothing_anywhere(corpus, tmp_path):
    """The requests a browser would make, with remote traffic still refused.

    The test client runs a real loopback transport, so it is allowed through and
    counted; the assertion that matters is that nothing went anywhere else.
    """
    from fastapi.testclient import TestClient

    db_path = tmp_path / "index.db"
    assert run_cli(["scan", str(corpus), "--db", str(db_path)])[0] == 0
    assert run_cli(["report", "--db", str(db_path), "--format", "json"])[0] == 1

    with NetworkGuard(allow_loopback=True) as guard:
        client = TestClient(server.create_app(str(db_path)))
        listing = client.get("/api/clusters")
        assert listing.status_code == 200
        cluster_id = listing.json()["clusters"][0]["cluster_id"]
        assert client.get("/api/clusters/%s/diff" % cluster_id).status_code == 200
        assert client.get("/").status_code == 200
        assert client.post("/api/quarantine", json={"confirm": True}).status_code == 501

    assert guard.remote == [], "a request left the machine: %s" % guard.remote
    # Loopback traffic did happen, which is what makes the assertion above real
    # rather than a consequence of the client never having opened anything.
    assert guard.loopback, "the test client did not use its loopback transport"


def test_the_diff_pipeline_alone_opens_nothing():
    """The summary path touches text and nothing else."""
    with NetworkGuard() as guard:
        result = diffing.diff_documents("签署人：张三", "签署人：王五")
        assert result.summary == diffing.SUMMARY_SIGNATURE

    assert guard.attempts == []


def test_the_read_only_index_helper_opens_nothing(corpus, tmp_path):
    db_path = tmp_path / "index.db"
    assert run_cli(["scan", str(corpus), "--db", str(db_path)])[0] == 0

    with NetworkGuard() as guard:
        conn = store.connect_readonly(str(db_path))
        try:
            assert store.count_documents(conn) == 3
        finally:
            conn.close()

    assert guard.attempts == []


def test_the_server_is_started_on_the_loopback_address(corpus, tmp_path, monkeypatch):
    """The one socket the project owns is a listener on 127.0.0.1.

    Not exercised above (a real listener would be a real socket); its address is
    pinned here, and the CLI refuses anything that is not loopback before this
    point is reached.
    """
    db_path = tmp_path / "index.db"
    assert run_cli(["scan", str(corpus), "--db", str(db_path)])[0] == 0

    import uvicorn

    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    code, out, err = run_cli(["serve", "--db", str(db_path), "--port", "8791"])

    assert code == 0, err
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8791
    assert "http://127.0.0.1:8791/" in out


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_a_non_loopback_bind_address_is_refused_before_any_socket(host, corpus, tmp_path):
    db_path = tmp_path / "index.db"
    assert run_cli(["scan", str(corpus), "--db", str(db_path)])[0] == 0

    with NetworkGuard() as guard:
        code, out, err = run_cli(["serve", "--host", host, "--db", str(db_path)])

    assert code == 2
    assert "loopback" in err
    assert guard.attempts == []
    assert out == ""
