"""Guards for promises that the repository itself must keep.

These tests read the tree, not the runtime. They exist because "the README says
so" is not evidence: a missing LICENSE file, an accidental dependency on a
frontend build tool, or a leftover ``os.remove`` call would otherwise go
unnoticed until release.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "docdedupe"
PLAN = REPO_ROOT / "plan"


def _python_files(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_license_file_exists_and_matches_project_metadata():
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file(), "README declares MIT but LICENSE is missing"
    text = license_path.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "yuluo554" in text

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'license\s*=\s*\{\s*text\s*=\s*"MIT"\s*\}', pyproject)


def test_no_frontend_build_chain_in_the_repo():
    """The review UI is one static HTML file; a build chain is out of scope."""
    forbidden = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")
    for name in forbidden:
        matches = list(REPO_ROOT.rglob(name))
        matches = [m for m in matches if "node_modules" not in m.parts]
        assert matches == [], "unexpected frontend build artifact: %s" % matches


def test_no_delete_calls_in_the_package():
    """The tool quarantines files, never deletes them.

    This is a static assertion on purpose: a runtime test can only prove that the
    paths it exercised did not delete anything.
    """
    forbidden_attrs = {"remove", "unlink", "rmtree", "removedirs"}
    offenders = []
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                offenders.append("%s:%d %s" % (path.relative_to(REPO_ROOT), node.lineno, func.attr))
            elif isinstance(func, ast.Name) and func.id == "remove":
                offenders.append("%s:%d remove()" % (path.relative_to(REPO_ROOT), node.lineno))
    assert offenders == [], "deletion call found: %s" % offenders


def test_package_does_not_import_a_network_client():
    """Local-only promise: no HTTP client, no socket library in the package.

    ``web`` will bind a loopback socket in M4; the allowlist below names that
    single module so the assertion stays meaningful instead of being deleted
    when the server lands.
    """
    allowed = {"docdedupe/web/server.py"}
    forbidden = {"requests", "httpx", "urllib3", "aiohttp", "socket", "http.client", "ftplib", "smtplib"}
    offenders = []
    for path in _python_files(SRC):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in forbidden:
                    offenders.append("%s:%d %s" % (rel, node.lineno, name))
    assert offenders == [], "network client import found: %s" % offenders


def test_gitignore_keeps_runtime_state_out_of_the_repo():
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".dedupe-quarantine/", "*.db", "out/"):
        assert pattern in text, "%s missing from .gitignore" % pattern


def test_plan_documents_declare_the_same_module_count():
    """plan/01 and plan/03 must agree on how many modules exist."""
    pattern = re.compile(r"^###\s*(?:模块|Module)\s*[①②③④⑤⑥⑦⑧1-9]", re.M)
    p01 = (PLAN / "01-题目文档.md").read_text(encoding="utf-8")
    p03 = (PLAN / "03-模块详设.md").read_text(encoding="utf-8")
    assert len(pattern.findall(p01)) == len(pattern.findall(p03)) == 5


def test_readme_documents_every_command_the_parser_exposes():
    """A command the user cannot find in the README does not exist for them."""
    from docdedupe.cli import _command_names, build_parser

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for command in _command_names(build_parser()):
        assert command in readme, "README does not mention command %r" % command


def test_readme_install_extras_exist_in_pyproject():
    """Every extra the README tells the user to install must be declared."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    declared = set(re.findall(r"^([A-Za-z0-9_-]+)\s*=\s*\[", pyproject, re.M))
    used = set()
    for line in readme.splitlines():
        if "pip install" not in line:
            continue
        for group in re.findall(r"\.\[([^\]]+)\]", line):
            used.update(part.strip() for part in group.split(",") if part.strip())

    assert used, "README no longer documents an install command; the guard would be vacuous"
    missing = sorted(used - declared)
    assert missing == [], "README installs undeclared extras: %s" % missing
