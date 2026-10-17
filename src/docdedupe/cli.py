"""Command line interface.

Exit codes are a public contract and are locked by tests:

====  ==================================================================
0     completed, nothing needed attention
1     completed, but results were found that need review (a non-empty
      report, a benchmark that missed its threshold, or a quarantine or
      undo that left an item half-way)
2     usage error, missing input, or unusable input
3     a dependency is missing (the ``web`` extra, a PDF/DOCX extractor)
====  ==================================================================

Code 1 is a *successful* run with findings, not a failure: ``report`` exits 1
when it wrote a non-empty report, because "there are duplicates" is exactly the
answer the user asked for, and ``quarantine`` exits 1 when an item could not be
moved, because "there is still something to do" is likewise an answer. Code 3
now means only "a dependency is missing"; every declared command is implemented
and is dispatched through :data:`HANDLERS`, which ``selftest`` cross-checks
against the parser so a command can never be added without a handler.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from . import __version__
from .config import DEFAULT_SETTINGS
from .store import SchemaVersionError

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_NOT_READY = 3


def _ensure_utf8_stdio() -> None:
    """Make stdout/stderr able to print non-ASCII on a cp1252/cp936 console.

    Only reconfigures when the stream reports a non-UTF-8 encoding, so a working
    terminal is left untouched. Tests substitute a text stream, which has no
    ``reconfigure`` attribute, and that case is skipped.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", None) or "").lower()
        if encoding and "utf" not in encoding:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc-dedupe",
        description=(
            "Find near-duplicate documents by content, review the differences, "
            "and move the redundant copies into a reversible quarantine."
        ),
        epilog=(
            "Exit codes: 0 completed, 1 results need review, 2 usage or input "
            "error, 3 a dependency is missing."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="doc-dedupe %s" % __version__,
        help="print the version and exit",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="index database path (default: <root>/.dedupe-cache/index.db)",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_scan = sub.add_parser(
        "scan",
        help="scan directories and build/refresh the local index",
        description="Scan one or more roots, extract text, and store fingerprints.",
    )
    p_scan.add_argument("roots", nargs="+", metavar="DIR", help="directories to scan")
    p_scan.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="PATTERN",
        help="additional directory name to ignore (repeatable)",
    )
    # SUPPRESS keeps the top-level --db value when the subcommand does not set
    # one; giving a plain default here would clobber it with None.
    p_scan.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="index database path (default: <first root>/.dedupe-cache/index.db)",
    )

    p_report = sub.add_parser(
        "report",
        help="build clusters and write a report",
        description="Cluster similar documents and export a md/csv/json report.",
    )
    p_report.add_argument(
        "--format",
        default="md",
        choices=("md", "csv", "json"),
        help="report format (default: md)",
    )
    p_report.add_argument("--out", default=None, metavar="PATH", help="output file")
    p_report.add_argument(
        "--tier",
        action="append",
        default=[],
        choices=("exact", "near", "template"),
        help="only include the given tier (repeatable)",
    )
    # Same SUPPRESS trick as ``scan``: without a subcommand-level --db the
    # top-level value has to survive parsing.
    p_report.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="index database path (default: ./.dedupe-cache/index.db)",
    )

    p_serve = sub.add_parser(
        "serve",
        help="start the local review page",
        description="Serve the single-page review UI bound to 127.0.0.1.",
    )
    p_serve.add_argument("--port", type=int, default=DEFAULT_SETTINGS.web_port)
    p_serve.add_argument(
        "--host",
        default=DEFAULT_SETTINGS.web_host,
        help="bind address (only loopback addresses are accepted)",
    )
    p_serve.add_argument(
        "--open",
        action="store_true",
        help="open the page in the default browser",
    )
    # Same SUPPRESS trick as ``scan``/``report``, so ``--db`` before the
    # subcommand keeps working and ``serve --db x.db`` is also accepted.
    p_serve.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="index database path (default: ./.dedupe-cache/index.db)",
    )

    p_quarantine = sub.add_parser(
        "quarantine",
        help="move non-kept cluster members into the quarantine",
        description=(
            "Move explicitly listed members of one cluster into "
            ".dedupe-quarantine/<batch-id>/. Never deletes anything."
        ),
    )
    p_quarantine.add_argument("--cluster", required=True, metavar="ID")
    p_quarantine.add_argument(
        "--keep",
        required=True,
        metavar="PATH",
        help="the member to keep, reported in the manifest",
    )
    p_quarantine.add_argument(
        "--move",
        action="append",
        required=True,
        metavar="PATH",
        help="explicitly listed member to move (repeatable); nothing else is touched",
    )
    p_quarantine.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="required; without it the command exits 2 and touches no file",
    )
    p_quarantine.add_argument(
        "--batch-id",
        default=None,
        metavar="ID",
        help="continue an interrupted batch instead of starting a new one",
    )
    p_quarantine.add_argument(
        "--format",
        default="text",
        choices=("text", "json"),
        help="output format (default: text); json is the same payload the review page returns",
    )
    p_quarantine.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="index database path (default: ./.dedupe-cache/index.db)",
    )

    p_undo = sub.add_parser(
        "undo",
        help="restore one quarantine batch",
        description="Restore every moved file of a batch, verifying content hashes.",
    )
    p_undo.add_argument("--batch", required=True, metavar="ID")
    p_undo.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="required; without it the command exits 2 and touches no file",
    )
    p_undo.add_argument(
        "--format",
        default="text",
        choices=("text", "json"),
        help="output format (default: text); json is the same payload the review page returns",
    )
    p_undo.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="index database path (default: ./.dedupe-cache/index.db)",
    )

    p_reports = sub.add_parser(
        "reports",
        help="export the quarantine record as CSV or JSON",
        description=(
            "Write one row per quarantined file (batch_id, cluster_id, tier, keep, "
            "moved_path, original_path, sha256, status)."
        ),
    )
    p_reports.add_argument(
        "--format",
        default="csv",
        choices=("csv", "json"),
        help="report format (default: csv)",
    )
    p_reports.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "output file, or a directory (existing, or written with a trailing "
            "separator) to write quarantine_report.<format> into (default: stdout)"
        ),
    )
    p_reports.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="index database path (default: ./.dedupe-cache/index.db)",
    )

    sub.add_parser("version", help="print the version and exit")
    sub.add_parser(
        "selftest",
        help="run offline consistency checks and exit non-zero on failure",
    )
    return parser


def _validate_serve_host(host: str) -> str:
    """Reject any bind address that is not loopback.

    The privacy promise is "the review page never leaves this machine". Binding
    0.0.0.0 or a LAN address would silently publish the document index to the
    network, so the refusal happens before the server starts.
    """
    normalized = host.strip().lower()
    if normalized in ("localhost", "::1"):
        return host
    if normalized.startswith("127.") and normalized.count(".") == 3:
        return host
    raise ValueError(
        "refusing to bind %r: only loopback addresses (127.0.0.1, ::1, localhost) "
        "are accepted" % host
    )


def _emit(stream, text: str) -> None:
    stream.write(text + "\n")


def _write_payload(stream, payload: str) -> None:
    """Write generated output without platform newline translation.

    A report is compared byte-for-byte across runs and machines
    (``tests/test_report.py``), so ``\\n`` must stay ``\\n`` even when stdout is
    a pipe on Windows, where text mode would turn every line ending into
    ``\\r\\n``. When the stream exposes a binary buffer (a real stdout/stderr) the
    payload is encoded and written there; a test that substitutes a text stream
    has no buffer and gets the plain ``write``, which is what it reads back.
    """
    buffer = getattr(stream, "buffer", None)
    encoding = (getattr(stream, "encoding", None) or "utf-8").lower()
    if buffer is not None and "utf" in encoding:
        stream.flush()
        buffer.write(payload.encode("utf-8"))
        buffer.flush()
        return
    stream.write(payload)


def cmd_version(args: argparse.Namespace, out, err) -> int:
    _emit(out, "doc-dedupe %s" % __version__)
    return EXIT_OK


def cmd_selftest(args: argparse.Namespace, out, err) -> int:
    """Offline consistency checks that are deliberately falsifiable.

    Every check compares two independently derived facts. A check that cannot
    fail (``return True`` style) is a fake gate, so each one below has a real
    failure mode.
    """
    failures: List[str] = []

    # 1. Version advertised by the package must match the distribution metadata.
    try:
        from importlib import metadata

        installed = metadata.version("doc-dedupe")
    except Exception:  # pragma: no cover - only when running from a source tree
        installed = None
    if installed is not None and installed != __version__:
        failures.append(
            "package version %s != installed distribution version %s"
            % (__version__, installed)
        )

    # 2. The settings validators must actually reject a broken configuration.
    from .config import Settings

    try:
        Settings(shingle_size=4)
    except ValueError:
        pass
    else:
        failures.append("Settings accepted shingle_size=4 (validator is inert)")

    # 3. The cache key must change when a component changes.
    from .store import cache_key

    base = cache_key("C:/docs/a.txt", 10, 20, DEFAULT_SETTINGS)
    moved = cache_key("C:/docs/b.txt", 10, 20, DEFAULT_SETTINGS)
    touched = cache_key("C:/docs/a.txt", 10, 21, DEFAULT_SETTINGS)
    if base == moved:
        failures.append("cache key ignores the path")
    if base == touched:
        failures.append("cache key ignores mtime_ns")

    # 4. The SQLite schema must round-trip through a real database file.
    import tempfile

    from .store import connect

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "selftest.db")
        try:
            conn = connect(db_path)
        except (SchemaVersionError, OSError) as exc:
            failures.append("cannot open the index database: %s" % exc)
        else:
            try:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                for expected in ("documents", "fingerprints", "pairs", "clusters"):
                    if expected not in tables:
                        failures.append("schema is missing table %r" % expected)
            finally:
                conn.close()

    # 5. Every command the parser exposes must have a handler. This is the check
    #    that used to watch STAGE_NOT_READY: a command added to the interface
    #    without an implementation has to fail loudly instead of reaching the
    #    "no handler" branch in production.
    known = set(_command_names(build_parser()))
    missing = sorted(known - set(HANDLERS))
    if missing:
        failures.append("declared command(s) without a handler: %s" % ", ".join(missing))
    unknown = sorted(set(HANDLERS) - known)
    if unknown:
        failures.append("handler(s) for undeclared command(s): %s" % ", ".join(unknown))

    if failures:
        for item in failures:
            _emit(err, "FAIL %s" % item)
        _emit(err, "selftest failed: %d problem(s)" % len(failures))
        return EXIT_FINDINGS
    _emit(out, "selftest OK (doc-dedupe %s)" % __version__)
    return EXIT_OK


def _command_names(parser: argparse.ArgumentParser) -> Sequence[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return tuple(action.choices.keys())
    return ()


def _resolve_db_path(args: argparse.Namespace) -> str:
    """Pick the index path: explicit ``--db``, else a cache dir under the first root."""
    explicit = getattr(args, "db", None)
    if explicit:
        return explicit
    roots = getattr(args, "roots", None) or []
    base = os.path.abspath(roots[0]) if roots else os.getcwd()
    if os.path.isfile(base):
        base = os.path.dirname(base)
    return os.path.join(base, ".dedupe-cache", "index.db")


def _validate_roots(roots: Sequence[str], err) -> Optional[int]:
    missing = [root for root in roots if not os.path.exists(root)]
    if missing:
        for root in missing:
            _emit(err, "error: no such file or directory: %s" % root)
        return EXIT_USAGE
    return None


def _format_scan_summary(summary, db_path: str, cfg=None) -> List[str]:
    from .config import DEFAULT_SETTINGS

    settings = cfg or DEFAULT_SETTINGS
    lines = []
    lines.append(
        "scanned %d file(s): %d indexed (%d from cache, %d extracted) in %.2fs"
        % (
            summary.files_seen,
            summary.files_indexed,
            summary.cache_hits,
            summary.extracted,
            summary.elapsed_seconds,
        )
    )
    ordered = sorted(summary.by_status.items())
    for status, count in ordered:
        lines.append("  %-24s %d" % (status, count))

    if summary.skipped:
        lines.append("skipped files (not compared, listed for review):")
        for path, status in summary.skipped:
            lines.append("  %s  %s" % (status, path))

    # Recall is reported on its own line, with the counters that explain it. A
    # bare "0 similar pairs" would hide the difference between "nothing is
    # similar" and "nothing was compared".
    lines.append(
        "fingerprinted %d document(s) this pass (%d rebuilt from stored text); "
        "%d fingerprint row(s) in the index"
        % (
            summary.fingerprinted,
            summary.fingerprinted_from_cache,
            summary.fingerprints_stored,
        )
    )
    if summary.recall_reused:
        lines.append(
            "recall: not recomputed (no fingerprint changed and the stored pair "
            "table still matches its recorded digest); %d pair(s), %d at or "
            "above %.2f"
            % (
                summary.pairs,
                summary.pairs_above_threshold,
                settings.near_duplicate_threshold,
            )
        )
    else:
        lines.append(
            "recall: %d document(s) compared, %d band candidate(s), "
            "%d rejected by simhash, %d below the %.2f retention floor, "
            "%d stored pair(s), %d at or above %.2f"
            % (
                summary.recall_documents,
                summary.band_candidates,
                summary.rejected_by_hamming,
                summary.rejected_by_score,
                settings.pair_store_threshold,
                summary.pairs,
                summary.pairs_above_threshold,
                settings.near_duplicate_threshold,
            )
        )
    if summary.recall_skipped_short:
        lines.append(
            "  %d document(s) below %d normalized characters were not compared"
            % (summary.recall_skipped_short, settings.min_text_chars)
        )

    lines.append("index: %s" % db_path)
    return lines


def cmd_scan(args: argparse.Namespace, out, err) -> int:
    """Scan the given roots and update the local index."""
    from . import scanner, store
    from . import extract as extract_mod
    from .config import DEFAULT_SETTINGS

    failure = _validate_roots(args.roots, err)
    if failure is not None:
        return failure

    db_path = _resolve_db_path(args)
    started = time.perf_counter()
    scanned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        conn = store.connect(db_path)
    except (SchemaVersionError, OSError) as exc:
        _emit(err, "error: cannot open the index database: %s" % exc)
        return EXIT_USAGE

    try:
        try:
            summary = scanner.scan(
                conn,
                [os.path.abspath(root) for root in args.roots],
                ignore=args.ignore or None,
                scanned_at=scanned_at,
                clock=time.perf_counter,
            )
        except extract_mod.DependencyMissing as exc:
            _emit(err, "error: %s" % exc)
            return EXIT_NOT_READY
        conn.commit()
    finally:
        conn.close()

    summary.elapsed_seconds = time.perf_counter() - started
    for line in _format_scan_summary(summary, db_path, DEFAULT_SETTINGS):
        _emit(out, line)

    if summary.files_indexed == 0:
        _emit(err, "error: no documents were found under the given roots")
        return EXIT_USAGE
    if summary.usable == 0:
        # Everything was skipped: the index is not usable for any later step, and
        # returning 0 would let a script believe the scan succeeded.
        _emit(
            err,
            "error: no document yielded text; the index cannot be used "
            "(%d file(s) skipped)" % summary.skipped_total,
        )
        return EXIT_USAGE

    # Skipped files are reported but not a failure: a folder may legitimately
    # contain a scan-only PDF. They are counted and named above, never hidden.
    return EXIT_OK


def _open_index(db_path: str, err) -> Optional[object]:
    """Open the index read-only for the commands that only inspect it.

    Returns ``None`` after printing the reason, so each caller can return the
    documented exit code (2: no index, no documents) without repeating the check.
    """
    from . import store

    if not os.path.isfile(db_path):
        _emit(
            err,
            "error: no index at %s; run 'doc-dedupe scan <dir>' first or pass --db"
            % db_path,
        )
        return None
    try:
        conn = store.connect_readonly(db_path)
    except (SchemaVersionError, OSError) as exc:
        _emit(err, "error: cannot open the index database: %s" % exc)
        return None
    if store.count_documents(conn) == 0:
        conn.close()
        _emit(err, "error: the index contains no documents; run 'scan' first")
        return None
    return conn


def _emit_machine_output(args, out, err, result) -> None:
    """Print a result as text lines or as the shared JSON payload.

    ``--format json`` exists so the payload the review page returns and the
    payload the command prints are the *same object* rendered twice; a script can
    then compare them without the two entry points growing two accounts of the
    same action (``plan/03`` module 5).

    In text mode the result itself goes to stdout and the reasons it is
    incomplete go to stderr, because "which files did not move and why" is what a
    caller acts on.
    """
    if getattr(args, "format", "text") == "json":
        import json as json_mod

        _write_payload(
            out, json_mod.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n"
        )
        return
    for line in result.describe():
        _emit(out, line)
    for line in result.describe_problems():
        _emit(err, line)


def cmd_quarantine(args: argparse.Namespace, out, err) -> int:
    """Move the listed cluster members into a reversible quarantine.

    Exit codes: 0 when every listed item is in the quarantine, 1 when an item
    could not be moved (it stays ``pending`` in the manifest and a re-run with the
    same ``--batch-id`` continues the work), 2 when the request was refused.
    """
    from . import quarantine as quarantine_mod

    db_path = getattr(args, "db", None) or _default_report_db()
    conn = _open_index(db_path, err)
    if conn is None:
        return EXIT_USAGE
    conn.close()

    try:
        result = quarantine_mod.execute_quarantine(
            db_path,
            cluster_id=args.cluster,
            keep=args.keep,
            move=list(args.move),
            confirm=True,
            batch_id=args.batch_id,
        )
    except quarantine_mod.QuarantineError as exc:
        _emit(err, "error: %s" % exc)
        return EXIT_USAGE

    _emit_machine_output(args, out, err, result)
    if not result.complete:
        _emit(
            err,
            "error: %d item(s) are still pending; re-run with --batch-id %s to continue"
            % (len(result.pending), result.batch_id),
        )
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_undo(args: argparse.Namespace, out, err) -> int:
    """Restore one batch, verifying every hash before anything moves.

    Exit codes: 0 when every item is back, 1 when the batch could not be fully
    restored (the manifest names what is left), 2 when the batch is unknown or the
    request was refused.
    """
    from . import quarantine as quarantine_mod

    db_path = getattr(args, "db", None) or _default_report_db()
    conn = _open_index(db_path, err)
    if conn is None:
        return EXIT_USAGE
    conn.close()

    try:
        result = quarantine_mod.execute_undo(db_path, batch_id=args.batch, confirm=True)
    except quarantine_mod.QuarantineError as exc:
        _emit(err, "error: %s" % exc)
        return EXIT_USAGE

    _emit_machine_output(args, out, err, result)
    if not result.complete:
        return EXIT_FINDINGS
    return EXIT_OK


def _resolve_out_target(out: str, fmt: str) -> str:
    """Where ``reports --out`` writes: a file, or a directory that gets the default name.

    A path that already is a directory, or one written with a trailing separator,
    means "put the report in here" — the alternative, creating a *file* named
    ``out`` because ``out/`` did not exist yet, is the kind of surprise that ends
    with a stray file next to the corpus.
    """
    if os.path.isdir(out) or out.endswith((os.sep, "/", "\\")):
        return os.path.join(out, "quarantine_report.%s" % fmt)
    return out


def cmd_reports(args: argparse.Namespace, out, err) -> int:
    """Export the quarantine record: one row per moved file.

    Exit code 1 when some item is still ``pending`` — an interrupted quarantine is
    exactly the state a reviewer has to look at, and a report that returned 0
    would hide it from a script.
    """
    from . import quarantine as quarantine_mod

    db_path = getattr(args, "db", None) or _default_report_db()
    conn = _open_index(db_path, err)
    if conn is None:
        return EXIT_USAGE
    try:
        rows = quarantine_mod.report_rows(conn)
    finally:
        conn.close()

    payload = quarantine_mod.render_report(rows, args.format)

    if args.out:
        target = _resolve_out_target(args.out, args.format)
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
        _emit(out, "wrote %s report to %s" % (args.format, target))
    else:
        _write_payload(out, payload)

    if not rows:
        _emit(err, "no quarantine batches recorded in this index")
        return EXIT_OK
    pending = [row for row in rows if row["status"] == quarantine_mod.STATUS_PENDING]
    if pending:
        _emit(
            err,
            "%d item(s) are still pending; the quarantine was interrupted and can be "
            "continued with 'doc-dedupe quarantine --batch-id <id>'" % len(pending),
        )
        return EXIT_FINDINGS
    return EXIT_OK


def _default_report_db() -> str:
    """Where ``report`` looks for an index when ``--db`` is not given.

    ``scan`` puts its index under the first root; ``report`` has no root, so it
    falls back to the cache directory of the current working directory — the
    same place ``scan .`` would have written it.
    """
    return os.path.join(os.getcwd(), ".dedupe-cache", "index.db")


def cmd_report(args: argparse.Namespace, out, err) -> int:
    """Cluster the index and emit a md/csv/json report.

    Exit codes (the public contract): 1 when the report contains clusters, 0
    when it contains none, 2 when there is no usable index. Findings are the
    normal outcome of a successful run, so they get their own code rather than
    being folded into success — a script can then ask "are there duplicates?"
    without parsing the report body.
    """
    from . import cluster as cluster_mod
    from . import report as report_mod
    from . import store

    db_path = getattr(args, "db", None) or _default_report_db()
    if not os.path.isfile(db_path):
        _emit(
            err,
            "error: no index at %s; run 'doc-dedupe scan <dir>' first or pass --db"
            % db_path,
        )
        return EXIT_USAGE

    try:
        conn = store.connect(db_path)
    except (SchemaVersionError, OSError) as exc:
        _emit(err, "error: cannot open the index database: %s" % exc)
        return EXIT_USAGE

    try:
        if store.count_documents(conn) == 0:
            _emit(err, "error: the index contains no documents; run 'scan' first")
            return EXIT_USAGE
        clusters = cluster_mod.clusters_from_index(conn)

        wanted = tuple(args.tier or ())
        visible = (
            [item for item in clusters if item.tier in wanted] if wanted else clusters
        )

        # The table is written from the *unfiltered* clustering: --tier changes
        # what this report shows, not what the index says. Writing the filtered
        # set would make the next `serve` or `quarantine` run silently disagree
        # with the report the user just read.
        built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.replace_clusters(
            conn,
            [
                (
                    item.cluster_id,
                    item.tier,
                    item.keep_id,
                    item.reason,
                    cluster_mod.scores_json(item.scores),
                    item.member_ids,
                )
                for item in clusters
            ],
            built_at,
        )
        conn.commit()
    finally:
        conn.close()

    report_value = report_mod.build_report(visible)
    payload = report_mod.render(report_value, args.format)

    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
        _emit(out, "wrote %s report to %s" % (args.format, args.out))
    else:
        _write_payload(out, payload)

    if not report_value.has_findings:
        _emit(err, "no duplicate clusters found")
        return EXIT_OK
    return EXIT_FINDINGS


def cmd_serve(args: argparse.Namespace, out, err) -> int:
    """Start the local review page for one index.

    Exit codes here describe *starting the server*, not the review: 0 once it
    stops normally, 2 when there is no usable index, 3 when the ``web`` extra is
    missing. Findings do not set the exit code — the page is a viewer, and the
    user reads the clusters off the page rather than off ``$?``.

    An index that was scanned but never reported has an empty ``clusters`` table.
    The server starts anyway (that is the documented behaviour: the page then
    tells the user to run ``report``), and the same hint is printed here so the
    reason is visible before opening a browser. Clustering on demand was rejected
    on purpose: writing it would make the page a writer, and computing it without
    writing would make the page disagree with the table the report reads.
    """
    from . import store

    db_path = getattr(args, "db", None) or _default_report_db()
    if not os.path.isfile(db_path):
        _emit(
            err,
            "error: no index at %s; run 'doc-dedupe scan <dir>' first or pass --db"
            % db_path,
        )
        return EXIT_USAGE

    try:
        conn = store.connect_readonly(db_path)
    except (SchemaVersionError, OSError) as exc:
        _emit(err, "error: cannot open the index database: %s" % exc)
        return EXIT_USAGE

    try:
        documents = store.count_documents(conn)
        clusters = store.count_clusters(conn)
    finally:
        conn.close()

    if documents == 0:
        _emit(err, "error: the index contains no documents; run 'scan' first")
        return EXIT_USAGE

    # Importing the web module is cheap (its imports of the web stack are
    # tolerated missing), so the missing-extra case is reported before a URL is
    # announced.
    from .web import MissingWebDependency
    from .web import server as web_server

    if not web_server.web_stack_available():
        _emit(err, "error: %s" % MissingWebDependency("fastapi"))
        return EXIT_NOT_READY

    _emit(
        out,
        "serving the review page at http://%s:%d/ (index: %s, %d cluster(s))"
        % (args.host, args.port, db_path, clusters),
    )
    if clusters == 0:
        _emit(
            err,
            "note: the index has no clustering result yet; the page will ask you to "
            "run 'doc-dedupe report --db %s' first" % db_path,
        )
    try:
        return web_server.serve(
            db_path,
            args.host,
            args.port,
            open_browser=bool(getattr(args, "open", False)),
        )
    except MissingWebDependency as exc:
        _emit(err, "error: %s" % exc)
        return EXIT_NOT_READY


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    out = sys.stdout
    err = sys.stderr

    if args.command is None:
        parser.print_help(out)
        return EXIT_OK

    # Argument-level guarantees are enforced here, on the real dispatch path, and
    # not only inside the eventual command implementation: a bind address is a
    # privacy promise, so an invalid one is rejected before any other work.
    if args.command == "serve":
        try:
            args.host = _validate_serve_host(args.host)
        except ValueError as exc:
            _emit(err, "error: %s" % exc)
            return EXIT_USAGE
        if not (1 <= args.port <= 65535):
            _emit(err, "error: --port must be between 1 and 65535")
            return EXIT_USAGE

    handler = HANDLERS.get(args.command)
    if handler is None:
        # Unreachable while every declared command has a handler; ``selftest``
        # cross-checks the two sets, so adding a command without one fails there
        # instead of silently returning success here.
        _emit(err, "error: command %r has no handler" % args.command)
        return EXIT_USAGE
    return handler(args, out, err)


# The dispatch table. ``selftest`` asserts that its keys are exactly the commands
# the parser declares, so the two can never drift apart.
HANDLERS: Dict[str, Callable[[argparse.Namespace, object, object], int]] = {
    "version": cmd_version,
    "selftest": cmd_selftest,
    "scan": cmd_scan,
    "report": cmd_report,
    "serve": cmd_serve,
    "quarantine": cmd_quarantine,
    "undo": cmd_undo,
    "reports": cmd_reports,
}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
