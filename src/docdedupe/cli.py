"""Command line interface.

Exit codes are a public contract and are locked by tests:

====  ==================================================================
0     completed, nothing needed attention
1     completed, but results were found that need review (or a benchmark
      threshold was not met)
2     usage error, missing input, or unusable input
3     a dependency is missing, or this milestone has not implemented the
      requested command yet
====  ==================================================================

Code 3 is temporary scaffolding: each command is retired from
``STAGE_NOT_READY`` as its module lands, and the constant is deleted once the
last command is implemented. Retiring a command means editing this table, the
dispatch below, the documentation, and the tests in the same commit.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from . import __version__
from .config import DEFAULT_SETTINGS
from .store import SchemaVersionError

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_NOT_READY = 3

# Commands that are declared in the interface but not implemented yet. Each entry
# maps the command name to the milestone that will implement it.
STAGE_NOT_READY = {
    "report": "M3",
    "serve": "M4",
    "quarantine": "M5",
    "undo": "M5",
}


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
            "error, 3 dependency missing or command not implemented."
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

    # 5. The declared staging table must reference commands that exist.
    known = set(_command_names(build_parser()))
    for name in STAGE_NOT_READY:
        if name not in known:
            failures.append("STAGE_NOT_READY names unknown command %r" % name)

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


def _format_scan_summary(summary, db_path: str) -> List[str]:
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

    lines.append("index: %s" % db_path)
    return lines


def cmd_scan(args: argparse.Namespace, out, err) -> int:
    """Scan the given roots and update the local index."""
    from . import scanner, store
    from . import extract as extract_mod

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
    for line in _format_scan_summary(summary, db_path):
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


def _not_ready(command: str, milestone: str, err) -> int:
    _emit(
        err,
        "error: '%s' is not implemented in this build (planned for %s). "
        "Run 'doc-dedupe --help' for the commands that do work."
        % (command, milestone),
    )
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

    if args.command == "version":
        return cmd_version(args, out, err)

    if args.command == "selftest":
        return cmd_selftest(args, out, err)

    if args.command == "scan":
        return cmd_scan(args, out, err)

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

    milestone = STAGE_NOT_READY.get(args.command)
    if milestone is not None:
        return _not_ready(args.command, milestone, err)

    # Unreachable while every declared command is staged; kept so that adding a
    # command without a handler fails loudly instead of returning success.
    _emit(err, "error: command %r has no handler" % args.command)
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
