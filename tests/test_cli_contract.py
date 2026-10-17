"""CLI contract tests: exit codes, help output, and the not-ready staging table.

These tests exist so that the exit-code contract in ``cli.py`` cannot drift. Each
code claimed by the table gets at least one assertion that really exercises it.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

import pdfutil

from docdedupe import __version__
from docdedupe import cli
from docdedupe.cli import (
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
    if command == "report":
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


def test_scan_is_retired_from_the_staging_table():
    """M1 landed scan; leaving it staged would make the command look unimplemented."""
    assert "scan" not in STAGE_NOT_READY


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


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _sample_tree(root):
    (root / "a").mkdir(parents=True)
    (root / "a" / "one.txt").write_text("示例项目甲 正文第一段。\n", encoding="utf-8")
    (root / "a" / "two.md").write_text("# 示例项目乙\n\n正文第二段。\n", encoding="utf-8")
    return root


def test_scan_reports_a_summary_and_exits_zero(tmp_path):
    root = _sample_tree(tmp_path / "docs")
    db = tmp_path / "index.db"

    code, out, err = run(["scan", str(root), "--db", str(db)])
    assert code == EXIT_OK
    assert err == ""
    assert "2 file(s)" in out
    assert "ok" in out
    assert "from cache" in out
    assert str(db) in out
    assert db.is_file()


def test_scan_second_run_reports_cache_hits(tmp_path):
    root = _sample_tree(tmp_path / "docs")
    db = tmp_path / "index.db"

    code, _, _ = run(["scan", str(root), "--db", str(db)])
    assert code == EXIT_OK
    code, out, err = run(["scan", str(root), "--db", str(db)])
    assert code == EXIT_OK
    assert "2 indexed (2 from cache, 0 extracted)" in out


def test_scan_lists_skipped_files_by_name(tmp_path):
    from docdedupe import store

    root = tmp_path / "docs"
    root.mkdir()
    (root / "good.txt").write_text("正常正文。\n", encoding="utf-8")
    (root / "scan.pdf").write_bytes(pdfutil.build_no_text_pdf(1))

    code, out, err = run(["scan", str(root), "--db", str(tmp_path / "i.db")])
    assert code == EXIT_OK
    assert store.STATUS_NO_TEXT_LAYER in out
    assert "scan.pdf" in out


def test_scan_exits_two_when_the_root_does_not_exist(tmp_path):
    code, out, err = run(["scan", str(tmp_path / "nope"), "--db", str(tmp_path / "i.db")])
    assert code == EXIT_USAGE
    assert "no such file or directory" in err
    assert out == ""


def test_scan_exits_two_when_every_file_is_unusable(tmp_path):
    """An index with nothing usable must not look like a successful scan."""
    from docdedupe import store

    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.pdf").write_bytes(pdfutil.build_no_text_pdf(1))
    (root / "b.doc").write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(32))

    code, out, err = run(["scan", str(root), "--db", str(tmp_path / "i.db")])
    assert code == EXIT_USAGE
    assert "no document yielded text" in err
    assert store.STATUS_NO_TEXT_LAYER in out


def test_scan_exits_two_when_no_documents_are_found(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "readme.json").write_text("{}", encoding="utf-8")

    code, out, err = run(["scan", str(root), "--db", str(tmp_path / "i.db")])
    assert code == EXIT_USAGE
    assert "no documents were found" in err


def test_scan_accepts_a_single_file_root(tmp_path):
    target = tmp_path / "one.txt"
    target.write_text("示例项目甲 正文。\n", encoding="utf-8")

    code, out, err = run(["scan", str(target), "--db", str(tmp_path / "i.db")])
    assert code == EXIT_OK
    assert "1 file(s)" in out


def test_scan_default_db_path_lives_under_the_first_root(tmp_path):
    root = _sample_tree(tmp_path / "docs")
    code, out, err = run(["scan", str(root)])
    assert code == EXIT_OK
    assert (root / ".dedupe-cache" / "index.db").is_file()
    assert ".dedupe-cache" in out


def test_default_db_path_ignores_a_foreign_top_level_value_for_other_commands():
    """The top-level --db must still be honoured by commands that read it."""
    parser = build_parser()
    args = parser.parse_args(["--db", "top.db", "scan", "."])
    assert args.db == "top.db"
    args = parser.parse_args(["scan", ".", "--db", "sub.db"])
    assert args.db == "sub.db"
    args = parser.parse_args(["--db", "top.db", "scan", "."])
    assert args.db == "top.db"
