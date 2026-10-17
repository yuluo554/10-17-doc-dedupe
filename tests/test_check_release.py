"""The release gate must fail on an unreleased repository and pass on a released one.

A gate that only ever prints PASS is decoration. These tests build a throwaway
repository twice — once as it looks before the tag exists, once as it looks after
the release — and pin both verdicts. The happy path runs with ``--no-gh`` so the
suite stays offline; the network-backed checks report SKIP there and are verified
by the real M6 run.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_release  # noqa: E402

README_BODY = """# demo

MIT, see LICENSE.

```bash
doc-dedupe scan data/samples --db index.db   # 0
doc-dedupe report --db index.db              # 1：clusters found
doc-dedupe quarantine --db index.db --cluster C --keep K --move M --confirm  # 0；1：items left
doc-dedupe undo --db index.db --batch B --confirm  # 0；1：batch not restored
doc-dedupe reports --db index.db --out out/  # 0；1：pending rows
```
"""

LEDGER_BODY = """# ledger

| doc | content | status |
|---|---|---|
| plan/RELEASE-M6.md | release record | ✅ M6 |

| promise | evidence | status |
|---|---|---|
| release trilogy | tag + Release + record | ✅ M6 |

| milestone | scope | DoD |
|---|---|---|
| M6 release | tag + Release + record | ✅ done |
"""

FACTS_BODY = """# release record

```yaml
# release-facts
tag: v0.1.0
artifact: doc_dedupe-0.1.0.tar.gz
```

## after the tag

sha256: {digest}  doc_dedupe-0.1.0.tar.gz
"""

FACTS_WITH_DIGEST_INSIDE = """# release record

```yaml
# release-facts
tag: v0.1.0
artifact: doc_dedupe-0.1.0.tar.gz
sha256: {digest}
```
"""


def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _run_gate(repo: Path, capsys):
    code = check_release.main([str(repo), "--no-gh"])
    return code, capsys.readouterr().out


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "1+demo@users.noreply.github.com")
    _git(root, "config", "user.name", "demo")
    # The DoD includes publishing, so the gate expects a remote to exist.
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _git(root, "remote", "add", "origin", str(bare))
    return root


def _write_release(root: Path, digest: str, template: str = FACTS_BODY):
    (root / "LICENSE").write_text("MIT License\n\ndemo\n", encoding="utf-8")
    (root / "README.md").write_text(README_BODY, encoding="utf-8")
    (root / "plan").mkdir(exist_ok=True)
    (root / "plan" / "00-README总览.md").write_text(LEDGER_BODY, encoding="utf-8")
    (root / "release").mkdir(exist_ok=True)
    artifact = root / "release" / "doc_dedupe-0.1.0.tar.gz"
    artifact.write_bytes(b"pretend tarball\n")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (root / "plan" / "RELEASE-M6.md").write_text(template.format(digest=digest), encoding="utf-8")
    return digest


# ---------------------------------------------------------------------------
# Before the release
# ---------------------------------------------------------------------------


def test_an_untagged_repository_fails_the_gate(repo, capsys):
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] TAG_EXISTS" in out
    assert "does not record a tag" in out or "does not exist" in out


def test_a_lightweight_tag_fails_the_annotated_requirement(repo, capsys):
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v0.1.0")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] TAG_ANNOTATED" in out


def test_a_pending_ledger_row_fails_even_when_the_tag_exists(repo, capsys):
    digest = _write_release(repo, "")
    ledger = repo / "plan" / "00-README总览.md"
    ledger.write_text(LEDGER_BODY.replace("| ✅ M6 |", "| ⬜ 待 M6 |"), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] PLAN00" in out


def test_the_release_document_must_be_inside_the_tag(repo, capsys):
    """A record written after the tag is a record nobody can trust."""
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    (repo / "plan" / "RELEASE-M6.md").write_text("no facts here\n", encoding="utf-8")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] ARTIFACT_RECORDED" in out


# ---------------------------------------------------------------------------
# After the release
# ---------------------------------------------------------------------------


def test_a_complete_local_release_passes(repo, capsys):
    digest = _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    _git(repo, "push", "-q", "origin", "HEAD", "--tags")
    code, out = _run_gate(repo, capsys)
    assert "[FAIL]" not in out, out
    assert code == 0
    assert "[PASS] ARTIFACT_LOCAL" in out
    assert "[PASS] DOC_IN_TAG" in out
    assert "[PASS] PLAN00" in out
    assert "[PASS] REMOTE_TAG" in out
    assert "[SKIP] CI_GREEN" in out and "[SKIP] ARTIFACT_REMOTE" in out


def test_a_tampered_local_artifact_fails(repo, capsys):
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    (repo / "release" / "doc_dedupe-0.1.0.tar.gz").write_bytes(b"different bytes\n")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] ARTIFACT_LOCAL" in out


def test_a_machine_path_in_a_tracked_file_fails(repo, capsys):
    # Assembled at runtime on purpose: writing the literal here would put a
    # machine-looking path in a tracked file, and the desensitization guard
    # (tests/test_repo_invariants.py) would — correctly — fail on this file.
    leak = "C:" + "/" + "Users" + "/" + "someone/notes.md"
    _write_release(repo, "")
    (repo / "notes.md").write_text("see %s\n" % leak, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] LEAKS" in out


def test_a_non_noreply_identity_fails(repo, capsys):
    # Assembled at runtime: a literal personal mailbox in a tracked file is
    # exactly what this release is supposed to contain zero of, and the
    # desensitization guard checks for it (tests/test_repo_invariants.py).
    mailbox = "someone" + "@" + "qq" + ".com"
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=%s" % mailbox, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] IDENTITIES" in out


def test_a_missing_remote_fails(tmp_path, capsys):
    """The public repository is part of the M6 DoD, so its absence is a FAIL."""
    root = tmp_path / "noremote"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "1+demo@users.noreply.github.com")
    _git(root, "config", "user.name", "demo")
    _write_release(root, "")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    _git(root, "tag", "-a", "v0.1.0", "-m", "release")
    code, out = _run_gate(root, capsys)
    assert code == 1
    assert "[FAIL] REMOTE" in out


def test_a_dirty_tree_fails(repo, capsys):
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    (repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] CLEAN_TREE" in out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_facts_block_is_parsed_from_the_document(repo):
    _write_release(repo, "")
    facts = check_release.read_facts(str(repo))
    assert facts["tag"] == "v0.1.0"
    assert facts["artifact"] == "doc_dedupe-0.1.0.tar.gz"
    assert list(facts["digests"]) == ["doc_dedupe-0.1.0.tar.gz"]
    assert check_release.SHA256_RE.match(facts["digests"]["doc_dedupe-0.1.0.tar.gz"])


def test_the_digest_may_be_recorded_after_the_tag(repo, capsys):
    """The artifact is built from the tagged tree, so its digest cannot be in it."""
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    _git(repo, "push", "-q", "origin", "HEAD", "--tags")
    code, out = _run_gate(repo, capsys)
    assert code == 0, out
    assert "recorded after the tag" in out


def test_a_digest_inside_the_tag_tree_is_also_accepted(repo, capsys):
    digest = _write_release(repo, "", template=FACTS_WITH_DIGEST_INSIDE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    _git(repo, "push", "-q", "origin", "HEAD", "--tags")
    code, out = _run_gate(repo, capsys)
    assert code == 0, out
    assert "in the release-facts block" in out


def test_a_missing_digest_anywhere_fails(repo, capsys):
    _write_release(repo, "", template="""# release record

```yaml
# release-facts
tag: v0.1.0
artifact: doc_dedupe-0.1.0.tar.gz
```
""")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] ARTIFACT_RECORDED" in out


def test_every_named_artifact_needs_its_own_digest(repo, capsys):
    """One digest cannot stand for two files; each name needs its own line."""
    root = repo
    (root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (root / "README.md").write_text(README_BODY, encoding="utf-8")
    (root / "plan").mkdir(exist_ok=True)
    (root / "plan" / "00-README总览.md").write_text(LEDGER_BODY, encoding="utf-8")
    (root / "release").mkdir(exist_ok=True)
    first = root / "release" / "a.tar.gz"
    second = root / "release" / "b.whl"
    first.write_bytes(b"first\n")
    second.write_bytes(b"second\n")
    (root / "plan" / "RELEASE-M6.md").write_text(
        "# record\n\n```yaml\n# release-facts\ntag: v0.1.0\nartifact: a.tar.gz, b.whl\n```\n\n"
        "## after the tag\n\nsha256: %s  a.tar.gz\n" % hashlib.sha256(b"first\n").hexdigest(),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    _git(root, "tag", "-a", "v0.1.0", "-m", "release")
    code, out = _run_gate(root, capsys)
    assert code == 1
    assert "[FAIL] ARTIFACT_RECORDED" in out
    assert "b.whl" in out


def test_a_repository_that_is_not_a_repo_is_a_usage_error(tmp_path, capsys):
    assert check_release.main([str(tmp_path)]) == 2


def test_two_runs_do_not_accumulate_results(repo, capsys):
    """The results list is module-level, so a second call must start from empty.

    Found during M6: without the reset the second run of the gate reported the
    first run's verdicts as well, which silently inflates the PASS count.
    """
    _write_release(repo, "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    _git(repo, "push", "-q", "origin", "HEAD", "--tags")
    _run_gate(repo, capsys)
    first = len(check_release.RESULTS)
    _run_gate(repo, capsys)
    assert len(check_release.RESULTS) == first


def test_a_readme_naming_an_unshipped_artifact_fails(repo, capsys):
    """The README is where a reader copies a filename from.

    Setuptools writes ``doc_dedupe-0.1.0.tar.gz`` (underscore) while the project
    is ``doc-dedupe``; the README shipped the hyphenated spelling until this gate
    existed.
    """
    _write_release(repo, "")
    (repo / "README.md").write_text(
        README_BODY + "\n`doc-dedupe-0.1.0.tar.gz`\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    code, out = _run_gate(repo, capsys)
    assert code == 1
    assert "[FAIL] README_ARTIFACTS" in out
    assert "doc-dedupe-0.1.0.tar.gz" in out


def test_a_readme_naming_the_shipped_artifact_passes(repo, capsys):
    _write_release(repo, "")
    (repo / "README.md").write_text(
        README_BODY + "\n`doc_dedupe-0.1.0.tar.gz`\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "-a", "v0.1.0", "-m", "release")
    _git(repo, "push", "-q", "origin", "HEAD", "--tags")
    code, out = _run_gate(repo, capsys)
    assert code == 0, out
    assert "[PASS] README_ARTIFACTS" in out
