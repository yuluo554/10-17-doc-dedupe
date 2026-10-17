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

    The generator in ``tools/`` is included: it runs inside the user's checkout,
    and a helper that "helpfully" clears the samples directory would be just as
    destructive as one in the package.
    """
    forbidden_attrs = {"remove", "unlink", "rmtree", "removedirs"}
    offenders = []
    for root in (SRC, REPO_ROOT / "tools"):
        if not root.is_dir():
            continue
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                    offenders.append(
                        "%s:%d %s" % (path.relative_to(REPO_ROOT), node.lineno, func.attr)
                    )
                elif isinstance(func, ast.Name) and func.id == "remove":
                    offenders.append(
                        "%s:%d remove()" % (path.relative_to(REPO_ROOT), node.lineno)
                    )
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


def test_gitignore_admits_the_synthetic_samples():
    """The sample allow-list must survive the blanket ``*.pdf``/``*.docx`` rules.

    Without these negations every binary sample would be silently untracked, the
    corpus would ship as metadata only, and ``--check`` would pass on a machine
    that has the files while a fresh clone has none.
    """
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("!data/samples/**/*.pdf", "!data/samples/**/*.docx"):
        assert pattern in text, "sample allow-list entry missing: %s" % pattern


def test_sample_bytes_are_not_line_ending_converted():
    """``--check`` compares bytes, so git must not normalise the corpus."""
    text = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "data/samples/** -text" in text


# A path segment that stands in for somebody's real one is not a leak: an
# elided `C:\Users\...` or an explicit `C:/Users/<user>/` is exactly what a
# published repository is supposed to contain.
_PLACEHOLDER_SEGMENTS = {"...", "..", ".", "<user>", "<you>", "<name>", "user", "username", "you"}

# Machine-specific evidence. The captured group is the path segment that must
# not name a real account, machine or work directory. Each pattern is written so
# that it cannot match its own source text: the literal in this file always has
# a character-class bracket where the pattern expects a path separator.
_MACHINE_PATH_PATTERNS = (
    (re.compile(r"(?:[A-Za-z]:[\\/]|/c/|/)(?:Users|users|home)[\\/]([A-Za-z0-9_.-]+)[\\/]"),
     "developer home directory"),
    (re.compile(r"[A-Za-z]:[\\/]ProgramData[\\/]([A-Za-z0-9_.-]+)[\\/]"),
     "Windows per-machine data root"),
    (re.compile(r"AppData[\\/]Local[\\/]Temp[\\/]([A-Za-z0-9_.-]+)", re.I),
     "local temp directory"),
    # A personal mailbox. The M6 DoD asks for zero hits; the bracket around the
    # dot keeps the pattern from matching its own source text.
    (re.compile(r"@qq[.]com", re.I), "QQ mailbox"),
)


def _machine_path_offenders(text):
    """Return the machine-specific paths in ``text`` (placeholders excluded)."""
    found = []
    for pattern, label in _MACHINE_PATH_PATTERNS:
        for match in pattern.finditer(text):
            groups = [g for g in match.groups() if g]
            if groups and groups[0].lower() in _PLACEHOLDER_SEGMENTS:
                continue
            found.append("%s (%s)" % (match.group(0), label))
    return found


def _git(*args, text=True):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout


def _tracked_files():
    out = _git("-c", "core.quotepath=false", "ls-files", "-z")
    return [name for name in out.split("\0") if name]


def _history_text_blobs():
    """Every blob reachable from every ref, as (sha, first-path, bytes).

    History is scanned in addition to the working tree: a path that was committed
    once stays published in the packed objects even after a later commit removes
    it, so "the current tree is clean" is not the claim the repository makes.
    """
    import subprocess

    paths_by_sha = {}
    for rev in _git("rev-list", "--all").split():
        for entry in _git("-c", "core.quotepath=false", "ls-tree", "-r", "-z", rev).split("\0"):
            if not entry or "\t" not in entry:
                continue
            meta, path = entry.split("\t", 1)
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                paths_by_sha.setdefault(parts[2], path)

    if not paths_by_sha:
        return []

    stdin = "".join("%s\n" % sha for sha in paths_by_sha)
    proc = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=REPO_ROOT, input=stdin.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    data = proc.stdout
    blobs = []
    pos = 0
    while pos < len(data):
        newline = data.index(b"\n", pos)
        header = data[pos:newline].decode("utf-8", "replace").split()
        pos = newline + 1
        if len(header) != 3:
            break
        sha, kind, size = header
        size = int(size)
        body = data[pos:pos + size]
        pos += size + 1  # trailing newline after each object
        if kind == "blob":
            blobs.append((sha, paths_by_sha.get(sha, "?"), body))
    return blobs


def test_no_machine_absolute_paths_in_tracked_sources():
    """The repository is published; a developer's checkout path must not leak.

    Scan results and index paths are runtime output (``.gitignore``d). This test
    covers *every* tracked file, not only the ones that happen to carry a
    source-code suffix, and it reads bytes so a path hidden in a binary-looking
    artefact (an HTML sample, a screenshot sidecar) counts too.
    """
    offenders = []
    scanned = 0
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:  # binary (PDF/DOCX/PNG): text patterns do not apply
            continue
        scanned += 1
        for hit in _machine_path_offenders(raw.decode("utf-8", "replace")):
            offenders.append("%s: %s" % (rel, hit))
    assert scanned > 100, "tracked-file scan looked at only %d files; guard is vacuous" % scanned
    assert offenders == [], "machine-specific path in a tracked file: %s" % offenders


def test_no_machine_absolute_paths_in_git_history():
    """A scrubbed working tree is not enough: blobs already packed stay shipped.

    The check is byte-level over every reachable blob, so it also covers commits
    that were later corrected in place.
    """
    offenders = []
    for sha, rel, body in _history_text_blobs():
        if b"\0" in body[:8192]:
            continue
        for hit in _machine_path_offenders(body.decode("utf-8", "replace")):
            offenders.append("%s (%s): %s" % (rel, sha[:12], hit))
    assert offenders == [], "machine-specific path in git history: %s" % offenders


def test_the_history_scan_covers_more_than_the_tip_commit():
    """A shallow checkout would silently shrink the gate to one commit.

    Found while cutting M6: ``actions/checkout`` defaults to ``fetch-depth: 1``,
    so in CI this gate scanned the tip's ~172 blobs instead of the ~1100 the
    full history holds — present, named, green, and covering a twelfth of what
    it claims. The workflow now asks for full history; this test makes a
    regression fail loudly instead of quietly reducing coverage.
    """
    revisions = _git("rev-list", "--all").split()
    assert len(revisions) > 1, \
        "only %d revision(s) reachable; the history gate cannot see past the tip" % len(revisions)
    blobs = _history_text_blobs()
    unique = {sha for sha, _, _ in blobs}
    assert len(unique) > len(_tracked_files()), (
        "the history scan saw %d unique blob(s) while the tree has %d file(s): this looks "
        "like a shallow checkout, so the gate is only covering the tip"
        % (len(unique), len(_tracked_files()))
    )


def test_commit_identities_are_not_personal_mailboxes():
    """Commits are published; the author/committer address must be the noreply one.

    Requiring the noreply suffix already excludes every personal mailbox, so no
    second check against a particular provider is needed here — and writing a
    provider address into this file would trip the scan above.
    """
    identities = set()
    for line in _git("log", "--all", "--format=%an|%ae|%cn|%ce").splitlines():
        identities.add(line)
    assert identities, "no commits found; the identity guard would be vacuous"
    for line in sorted(identities):
        for addr in line.split("|")[1::2]:
            addr = addr.strip().lower()
            assert addr.endswith("@users.noreply.github.com"), \
                "commit identity is not a GitHub noreply address: %r" % line


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
