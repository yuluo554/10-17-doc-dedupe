"""CLI contract tests: exit codes, help output, and the not-ready staging table.

These tests exist so that the exit-code contract in ``cli.py`` cannot drift. Each
code claimed by the table gets at least one assertion that really exercises it.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from docdedupe import __version__
from docdedupe import cli
from docdedupe.cli import (
    EXIT_FINDINGS,
    EXIT_NOT_READY,
    EXIT_OK,
    EXIT_USAGE,
    STAGE_NOT_READY,
    build_parser,
    main,
)


class _Capture:
    """Capture stdout/stderr without touching the real streams.

    ``main`` reads ``sys.stdout``/``sys.stderr`` directly, so the streams are
    replaced for the duration of a call.
    """

    def __init__(self) -> None:
        self.out = io.StringIO()
        self.err = io.StringIO()

    def __enter__(self) -> "_Capture":
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout, sys.stderr = self._saved


def run(argv):
    """Invoke ``main`` and normalize ``SystemExit`` into an exit code.

    ``argparse`` raises ``SystemExit`` for ``--help``/``--version`` and for usage
    errors; the contract under test is the exit code, so both shapes are folded
    into one return value.
    """
    with _Capture() as cap:
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
    return code, cap.out.getvalue(), cap.err.getvalue()


def test_help_exits_zero_and_lists_commands():
    code, out, err = run(["--help"])
    assert code == EXIT_OK
    assert "scan" in out
    assert "report" in out
    assert "serve" in out
    assert "quarantine" in out
    assert "undo" in out
    assert err == ""


def test_no_subcommand_prints_help_and_exits_zero():
    code, out, err = run([])
    assert code == EXIT_OK
    assert "usage:" in out
    assert err == ""


def test_version_flag_matches_package_version():
    code, out, err = run(["--version"])
    assert code == EXIT_OK
    assert out.strip() == "doc-dedupe %s" % __version__
    assert err == ""


def test_version_subcommand_matches_package_version():
    code, out, err = run(["version"])
    assert code == EXIT_OK
    assert out.strip() == "doc-dedupe %s" % __version__


def test_unknown_command_exits_usage():
    code, out, err = run(["definitely-not-a-command"])
    assert code == EXIT_USAGE
    assert "invalid choice" in err


def test_unknown_flag_exits_usage():
    code, out, err = run(["--definitely-not-a-flag"])
    assert code == EXIT_USAGE
    assert "unrecognized arguments" in err


@pytest.mark.parametrize("command", sorted(STAGE_NOT_READY))
def test_staged_commands_exit_three_with_milestone_hint(command):
    """A not-yet-implemented command must not pretend to succeed."""
    argv = [command]
    if command == "scan":
        argv.append(".")
    elif command == "report":
        argv += ["--format", "json"]
    elif command == "quarantine":
        argv += ["--cluster", "c1", "--keep", "a", "--move", "b", "--confirm"]
    elif command == "undo":
        argv += ["--batch", "b1", "--confirm"]

    code, out, err = run(argv)
    assert code == EXIT_NOT_READY
    assert STAGE_NOT_READY[command] in err
    assert "not implemented" in err
    # A staged command must never claim success nor emit a result payload.
    assert out == ""


def test_staged_commands_all_declared_in_parser():
    known = set(cli._command_names(build_parser()))
    assert set(STAGE_NOT_READY) <= known


def test_parser_rejects_serve_on_non_loopback_host():
    """``serve`` must refuse to bind a routable address (privacy promise)."""
    from docdedupe.cli import _validate_serve_host

    assert _validate_serve_host("127.0.0.1") == "127.0.0.1"
    assert _validate_serve_host("::1") == "::1"
    assert _validate_serve_host("localhost") == "localhost"
    with pytest.raises(ValueError):
        _validate_serve_host("0.0.0.0")
    with pytest.raises(ValueError):
        _validate_serve_host("192.168.1.10")


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_serve_command_refuses_non_loopback_host_at_the_entry_point(host):
    """The refusal must happen on the real dispatch path, not only in the helper.

    Otherwise the guard is dead code: a test can call the helper and pass while
    the actual command would happily bind a routable address.
    """
    code, out, err = run(["serve", "--host", host])
    assert code == EXIT_USAGE
    assert "loopback" in err
    assert "not implemented" not in err


@pytest.mark.parametrize("port", ["0", "70000", "-1"])
def test_serve_command_rejects_out_of_range_port(port):
    code, out, err = run(["serve", "--port", port])
    assert code == EXIT_USAGE
    assert "port" in err


def test_serve_command_accepts_loopback_and_reports_the_stage():
    code, out, err = run(["serve", "--host", "127.0.0.1", "--port", "8765"])
    assert code == EXIT_NOT_READY
    assert "M4" in err


def test_quarantine_requires_confirm_flag_in_parser():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["quarantine", "--cluster", "c", "--keep", "a", "--move", "b"])
    assert exc.value.code == EXIT_USAGE


def test_undo_requires_confirm_flag_in_parser():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["undo", "--batch", "b"])
    assert exc.value.code == EXIT_USAGE

def test_utf8_stdio_guard_tolerates_streams_without_reconfigure():
    """The guard must not raise on streams that have no ``reconfigure``."""
    saved = (sys.stdout, sys.stderr)
    try:
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        cli._ensure_utf8_stdio()
    finally:
        sys.stdout, sys.stderr = saved
