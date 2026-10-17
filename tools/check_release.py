"""Release gate for this repository: `python tools/check_release.py`.

The M6 DoD is prose; prose gates rot. Measured during M5: "read the checklist
once" is not a gate — a tag pointing at the wrong commit looks exactly like a
correct one until someone diffs it. This script turns the mechanically decidable
part of the M6 DoD into checks that print PASS/WARN/FAIL and set the exit code.

    exit 0 = no FAIL (warnings allowed)
    exit 1 = at least one FAIL
    exit 2 = usage error (not a git repository)

It reads the release facts from ``plan/RELEASE-M6.md``. A small machine-readable
block records what the tag is and which artifacts the release carries::

    # release-facts
    tag: v0.1.0
    artifact: doc-dedupe-0.1.0.tar.gz, doc-dedupe-0.1.0-py3-none-any.whl

The digests are read from the block or, failing that, from later ``sha256:``
lines written in ``sha256sum`` form — one per artifact, so the archive can be
re-verified with a command the reader already knows::

    sha256: <64-hex>  doc-dedupe-0.1.0.tar.gz

They have to be allowed to come later for an honest reason: the artifact is built
*from* the tagged tree, so its digest cannot be written inside that tree —
recording it changes the tree and therefore the digest. The pre-tag block fixes
the tag and the artifact names; the digests are a post-tag addition to the same
document, which is the division the M6 DoD asks for.

``--no-gh`` skips the checks that need the network; those then report WARN or
SKIP instead of PASS, so an offline run is never mistaken for a verified one.

The tools in this repository never delete anything (K71): the temporary download
directory is left in place, like B6's ``--work`` directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RESULTS: List[Tuple[str, str, str]] = []
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

RELEASE_DOC = "plan/RELEASE-M6.md"
FACTS_MARKER = "# release-facts"
FACT_KEYS = ("tag", "commit", "artifact", "sha256")

NOREPLY_RE = re.compile(r"^\d+\+[^@]+@users\.noreply\.github\.com$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")

# Machine-specific evidence, same shapes the invariant test guards (see
# tests/test_repo_invariants.py). Placeholders such as <user> are not leaks.
_LEAK_PATTERNS = (
    (re.compile(r"(?:[A-Za-z]:[\\/]|/c/|/)(?:Users|users|home)[\\/]([A-Za-z0-9_.-]+)[\\/]"),
     "developer home directory"),
    (re.compile(r"[A-Za-z]:[\\/]ProgramData[\\/]([A-Za-z0-9_.-]+)[\\/]"),
     "Windows per-machine data root"),
    (re.compile(r"\d{5,12}@qq[.]com", re.I), "QQ mailbox"),
)
_PLACEHOLDERS = {"...", "..", ".", "<user>", "<you>", "<name>", "user", "username", "you"}


def add(level: str, code: str, message: str) -> None:
    RESULTS.append((level, code, message))


def run(args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def git(repo, *args):
    return run(["git", "-C", repo] + list(args))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_facts(repo: str) -> Dict[str, str]:
    """Parse the release-facts block, plus any ``sha256:`` lines in the document.

    ``tag`` and ``artifact`` must be inside the block (they are written before the
    tag); digests may be in the block or in ``sha256sum``-style lines added after
    it. ``digests`` is a name -> hex map; ``sha256_source`` says where they came
    from so a run can report whether the digest was inside the tag tree.
    """
    path = Path(repo) / RELEASE_DOC
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    facts: Dict[str, str] = {}
    digests: Dict[str, str] = {}
    prefixed: Optional[str] = None
    inside = False
    inside_was_digest = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(FACTS_MARKER):
            inside = True
            continue
        if inside:
            if stripped.startswith("```"):
                inside = False
                continue
            match = re.match(r"^([a-z0-9_]+)\s*:\s*(.+?)\s*$", stripped)
            if match and match.group(1) in FACT_KEYS:
                key, value = match.group(1), match.group(2)
                if key == "sha256":
                    named_inside = re.match(r"^([0-9a-fA-F]{64})\s+(\S+)$", value)
                    if named_inside:
                        digests[named_inside.group(2)] = named_inside.group(1).lower()
                        inside_was_digest = True
                    elif re.match(r"^[0-9a-fA-F]{64}$", value):
                        prefixed = value.lower()
                        inside_was_digest = True
                    continue
                facts[key] = value
            continue
        named = re.match(r"^sha-?256\s*[:：]\s*([0-9a-fA-F]{64})\s+(\S+)\s*$", stripped)
        if named:
            digests[named.group(2)] = named.group(1).lower()
            continue
        bare = re.match(r"^sha-?256\s*[:：]\s*([0-9a-fA-F]{64})\s*$", stripped)
        if bare and prefixed is None:
            prefixed = bare.group(1).lower()

    names = artifact_names(facts)
    if not digests and prefixed and len(names) == 1:
        digests[names[0]] = prefixed
    if digests:
        facts["digests"] = digests
        facts["sha256_source"] = (
            "in the release-facts block" if inside_was_digest else "recorded after the tag"
        )
    return facts


def artifact_names(facts: Dict[str, str]) -> List[str]:
    raw = facts.get("artifact") or ""
    return [name.strip() for name in raw.split(",") if name.strip()]


# ---------------------------------------------------------------------------
# The three deliverables
# ---------------------------------------------------------------------------


def check_tag(repo: str, facts: Dict[str, str]) -> Optional[str]:
    tag = facts.get("tag")
    if not tag:
        add(FAIL, "TAG_RECORDED", "%s does not record a tag in its release-facts block" % RELEASE_DOC)
        return None
    code, _ = git(repo, "rev-parse", "--verify", "refs/tags/%s" % tag)
    if code != 0:
        add(FAIL, "TAG_EXISTS", "tag %s is recorded but does not exist" % tag)
        return None
    _, kind = git(repo, "cat-file", "-t", tag)
    if kind.strip() != "tag":
        add(FAIL, "TAG_ANNOTATED", "tag %s is lightweight; the DoD asks for an annotated tag" % tag)
    else:
        add(PASS, "TAG_ANNOTATED", "tag %s is annotated" % tag)

    _, target = git(repo, "rev-parse", "%s^{commit}" % tag)
    target = target.strip()
    # Recording the commit inside the commit is circular, so the field is
    # optional: when present it must agree with the tag, and when absent the
    # CI check below is what places the tag on the verifying commit.
    recorded = (facts.get("commit") or "").strip()
    if not recorded:
        add(WARN, "TAG_COMMIT", "no commit recorded in the facts block; the tag target is %s"
            % target[:12])
    elif not HEX40_RE.match(recorded):
        add(FAIL, "TAG_COMMIT_RECORDED", "commit in the facts block is not a full sha: %r" % recorded)
    elif recorded != target:
        add(FAIL, "TAG_COMMIT", "tag %s points at %s but the document records %s"
            % (tag, target[:12], recorded[:12]))
    else:
        add(PASS, "TAG_COMMIT", "tag %s points at the recorded commit %s" % (tag, target[:12]))

    _, tagger = git(repo, "for-each-ref", "refs/tags/%s" % tag, "--format=%(taggeremail)")
    tagger = tagger.strip().strip("<>")
    if not tagger:
        add(WARN, "TAGGER_EMAIL", "tag has no tagger email")
    elif not NOREPLY_RE.match(tagger):
        add(FAIL, "TAGGER_EMAIL", "tagger email is not a GitHub noreply address: %s" % tagger)
    else:
        add(PASS, "TAGGER_EMAIL", "tagger email is noreply")
    return tag


def check_release_doc_in_tag(repo: str, tag: str) -> None:
    code, out = git(repo, "cat-file", "-e", "%s:%s" % (tag, RELEASE_DOC))
    if code != 0:
        add(FAIL, "DOC_IN_TAG",
            "%s is not in the %s tree; the release document must be written before the tag, "
            "not after it" % (RELEASE_DOC, tag))
        return
    add(PASS, "DOC_IN_TAG", "%s is inside the %s tree" % (RELEASE_DOC, tag))

    _, tagged = git(repo, "show", "%s:%s" % (tag, RELEASE_DOC))
    on_disk = (Path(repo) / RELEASE_DOC).read_text(encoding="utf-8", errors="replace")
    # The checkout may have CRLF while the blob has LF (core.autocrlf), so the
    # comparison normalises line endings; anything else is a real difference.
    if tagged.replace("\r\n", "\n") == on_disk.replace("\r\n", "\n"):
        add(PASS, "DOC_UNCHANGED", "the release document matches the tagged revision")
    else:
        add(WARN, "DOC_UNCHANGED",
            "the release document changed after the tag; the post-tag additions must be a "
            "separate, clearly marked section")


def check_artifact(repo: str, facts: Dict[str, str], use_gh: bool, tag: Optional[str]) -> None:
    names = artifact_names(facts)
    digests = facts.get("digests") or {}
    if not names:
        add(FAIL, "ARTIFACT_RECORDED", "the facts block records no artifact name")
        return
    unrecorded = [name for name in names if name not in digests]
    if unrecorded:
        add(FAIL, "ARTIFACT_RECORDED",
            "no recorded sha256 for: %s (add a 'sha256: <hex>  <name>' line to %s)"
            % (", ".join(unrecorded), RELEASE_DOC))
        return
    add(PASS, "ARTIFACT_DIGEST",
        "%d artifact digest(s) %s" % (len(digests), facts.get("sha256_source", "recorded")))

    for name in names:
        recorded = digests[name]
        local = Path(repo) / "release" / name
        if not local.is_file():
            add(WARN, "ARTIFACT_LOCAL", "no local copy at release/%s to compare" % name)
            continue
        actual = sha256_of(local)
        if actual == recorded:
            add(PASS, "ARTIFACT_LOCAL", "release/%s matches the recorded sha256" % name)
        else:
            add(FAIL, "ARTIFACT_LOCAL",
                "release/%s hashes to %s, the document records %s" % (name, actual[:16], recorded[:16]))

    if not use_gh or not tag:
        add(SKIP, "ARTIFACT_REMOTE", "gh checks disabled")
        return
    code, slug = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=repo)
    if code != 0:
        add(SKIP, "ARTIFACT_REMOTE", "gh is unavailable, cannot verify the Release")
        return
    slug = slug.strip()
    import tempfile

    target_dir = Path(tempfile.mkdtemp(prefix="check-release-"))
    for name in names:
        recorded = digests[name]
        code, out = run(["gh", "release", "download", tag, "--pattern", name,
                         "--dir", str(target_dir), "--clobber"], cwd=repo)
        downloaded = target_dir / name
        if code != 0 or not downloaded.is_file():
            add(FAIL, "ARTIFACT_REMOTE", "could not download %s from the %s Release: %s"
                % (name, tag, out[:120]))
            continue
        actual = sha256_of(downloaded)
        if actual == recorded:
            add(PASS, "ARTIFACT_REMOTE",
                "downloaded %s from %s; sha256 matches (%s)" % (name, slug, actual[:16]))
        else:
            add(FAIL, "ARTIFACT_REMOTE",
                "downloaded %s hashes to %s but the document records %s"
                % (name, actual[:16], recorded[:16]))


def check_release_has_assets(repo: str, tag: Optional[str], use_gh: bool) -> None:
    if not use_gh or not tag:
        add(SKIP, "RELEASE_ASSETS", "gh checks disabled")
        return
    code, slug = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=repo)
    if code != 0:
        add(SKIP, "RELEASE_ASSETS", "gh is unavailable")
        return
    slug = slug.strip()
    code, body = run(["gh", "api", "repos/%s/releases" % slug], cwd=repo)
    if code != 0:
        add(WARN, "RELEASE_ASSETS", "could not read Releases: %s" % body[:100])
        return
    try:
        releases = json.loads(body)
    except ValueError:
        add(WARN, "RELEASE_ASSETS", "the Releases endpoint did not return JSON")
        return
    matching = [r for r in releases if r.get("tag_name") == tag]
    if not matching:
        add(FAIL, "RELEASE_ASSETS", "no GitHub Release for tag %s" % tag)
        return
    assets = matching[0].get("assets") or []
    if not assets:
        add(FAIL, "RELEASE_ASSETS", "the Release for %s has no attached artifact" % tag)
    else:
        add(PASS, "RELEASE_ASSETS", "%d asset(s): %s"
            % (len(assets), ", ".join(a.get("name", "?") for a in assets[:4])))


# ---------------------------------------------------------------------------
# Repository state
# ---------------------------------------------------------------------------


def check_ci_green(repo: str, tag: Optional[str], use_gh: bool) -> None:
    """The tag must sit on a commit the four-job matrix actually verified.

    The M6 DoD asks for two runs on that commit — one from the branch push and
    one from the tag push — so "CI is green" cannot be satisfied by an older run
    that happened to pass. Requires gh; reports SKIP otherwise rather than
    pretending the claim was checked.
    """
    if not use_gh or not tag:
        add(SKIP, "CI_GREEN", "gh checks disabled")
        return
    code, target = git(repo, "rev-parse", "%s^{commit}" % tag)
    if code != 0:
        add(SKIP, "CI_GREEN", "cannot resolve the tag")
        return
    target = target.strip()
    code, body = run(
        ["gh", "run", "list", "--commit", target, "--limit", "50",
         "--json", "databaseId,event,headBranch,headSha,conclusion,status,workflowName"],
        cwd=repo,
    )
    if code != 0:
        add(SKIP, "CI_GREEN", "gh run list failed: %s" % body[:100])
        return
    try:
        runs = json.loads(body)
    except ValueError:
        add(WARN, "CI_GREEN", "gh run list did not return JSON")
        return
    if not runs:
        add(FAIL, "CI_GREEN", "no workflow run for commit %s" % target[:12])
        return
    failed = [r for r in runs if r.get("conclusion") not in (None, "success")]
    unfinished = [r for r in runs if r.get("status") != "completed"]
    if unfinished:
        add(WARN, "CI_GREEN", "%d run(s) still %s" % (len(unfinished), unfinished[0].get("status")))
    if failed:
        add(FAIL, "CI_GREEN", "%d run(s) did not succeed on %s: %s"
            % (len(failed), target[:12],
               ", ".join("%s/%s" % (r.get("workflowName"), r.get("conclusion")) for r in failed[:3])))
        return
    branches = {r.get("headBranch") for r in runs}
    if tag not in branches:
        add(FAIL, "CI_GREEN",
            "no run was triggered by the tag push (runs on %s cover only %s); push the tag and "
            "let CI run before calling the release verified"
            % (target[:12], ", ".join(sorted(b for b in branches if b))))
    elif len(branches) < 2:
        add(WARN, "CI_GREEN",
            "only the tag push ran on %s; the DoD asks for a branch push too" % target[:12])
    else:
        add(PASS, "CI_GREEN", "%d successful run(s) on %s, covering %s"
            % (len(runs), target[:12], ", ".join(sorted(b for b in branches if b))))


def check_plan_ledger(repo: str) -> None:
    """Three rows in plan/00 carry M6 status, and all three must be flipped.

    They are the document index row, the promise ledger row and the milestone
    row. Checking only the milestone row would pass on a repository whose ledger
    still says "待 M6", which is exactly the state a release reviewer must catch.
    """
    path = Path(repo) / "plan" / "00-README总览.md"
    if not path.is_file():
        add(WARN, "PLAN00", "plan/00 overview not found")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = [line for line in text.splitlines()
            if line.lstrip().startswith("|")
            and re.search(r"M6|发布三件套|RELEASE-M6", line)
            and not line.startswith("| 里程碑") and "HANDOFF-M6" not in line
            and "M0–M6" not in line]
    if not rows:
        add(WARN, "PLAN00", "no M6 status row found in the plan overview")
        return
    pending = [line for line in rows if "⬜" in line or "待 M6" in line]
    if pending:
        add(FAIL, "PLAN00", "%d M6 row(s) still marked pending — %s"
            % (len(pending), pending[0].strip()[:100]))
        return
    add(PASS, "PLAN00", "all %d M6 status row(s) are flipped" % len(rows))


def check_readme_exit_codes(repo: str) -> None:
    """The README must state the real exit codes, not a comfortable "all zero".

    ``report``/``quarantine``/``undo``/``reports`` exit 1 when they have work
    left, and copying "everything exits 0" from a template is the cheapest way to
    ship a false promise. The check anchors on the command lines themselves, so a
    stray ``1：`` in prose cannot satisfy it.
    """
    path = Path(repo) / "README.md"
    if not path.is_file():
        add(FAIL, "README", "README.md is missing")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if "LICENSE" not in text or not (Path(repo) / "LICENSE").is_file():
        add(FAIL, "README_LICENSE", "README does not point at a LICENSE that exists")
    else:
        add(PASS, "README_LICENSE", "LICENSE exists and the README names it")
    missing = []
    for command in ("report", "quarantine", "undo", "reports"):
        lines = [line for line in text.splitlines()
                 if line.strip().startswith("doc-dedupe %s" % command)]
        if not lines:
            missing.append("%s (no invocation line)" % command)
        elif not any(re.search(r"1\s*[：:]", line) for line in lines):
            missing.append("%s (no exit 1)" % command)
    if missing:
        add(FAIL, "README_EXIT_CODES", "the README does not document exit 1 for: %s"
            % ", ".join(missing))
    else:
        add(PASS, "README_EXIT_CODES",
            "report/quarantine/undo/reports each document exit code 1 on their command line")


def check_leaks(repo: str) -> None:
    code, out = git(repo, "-c", "core.quotepath=false", "ls-files", "-z")
    if code != 0:
        add(WARN, "LEAKS", "could not list tracked files")
        return
    offenders = []
    scanned = 0
    for name in out.split("\0"):
        if not name:
            continue
        path = Path(repo) / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:
            continue
        scanned += 1
        text = raw.decode("utf-8", "replace")
        for pattern, label in _LEAK_PATTERNS:
            for match in pattern.finditer(text):
                groups = [g for g in match.groups() if g]
                if groups and groups[0].lower() in _PLACEHOLDERS:
                    continue
                offenders.append("%s: %s (%s)" % (name, match.group(0), label))
    if offenders:
        add(FAIL, "LEAKS", "machine-specific path in tracked files: %s" % "; ".join(offenders[:5]))
    else:
        add(PASS, "LEAKS", "no machine-specific path or QQ mailbox in %d scanned file(s)" % scanned)

    identities = set()
    _, log = git(repo, "log", "--all", "--format=%ae%n%ce")
    for line in log.splitlines():
        line = line.strip()
        if line:
            identities.add(line)
    bad = sorted(addr for addr in identities if not NOREPLY_RE.match(addr))
    if bad:
        add(FAIL, "IDENTITIES", "non-noreply commit identities: %s" % ", ".join(bad))
    else:
        add(PASS, "IDENTITIES", "%d unique commit identity/identities, all noreply" % len(identities))


def check_clean_tree(repo: str) -> None:
    code, out = git(repo, "status", "--porcelain")
    if code != 0:
        add(WARN, "CLEAN_TREE", "could not read the working tree state")
        return
    if out.strip():
        add(FAIL, "CLEAN_TREE", "the working tree is not clean (%d entr(y|ies))" % len(out.splitlines()))
    else:
        add(PASS, "CLEAN_TREE", "the working tree is clean")


def check_remote(repo: str, tag: Optional[str], use_gh: bool = True) -> None:
    code, remotes = git(repo, "remote")
    if code != 0 or not remotes.strip():
        add(FAIL, "REMOTE", "no git remote: the public repository is part of the M6 DoD")
        return
    remote = remotes.splitlines()[0].strip()
    add(PASS, "REMOTE", "remote configured: %s" % remote)
    if not tag:
        return
    _, local = git(repo, "rev-parse", "%s^{commit}" % tag)
    local = local.strip()

    code, out = git(repo, "ls-remote", "--tags", remote)
    if code == 0:
        peeled = {}
        for line in out.splitlines():
            sha, ref = line.split("\t", 1)
            peeled[ref] = sha
        remote_sha = peeled.get("refs/tags/%s^{}" % tag) or peeled.get("refs/tags/%s" % tag)
        if remote_sha == local:
            add(PASS, "REMOTE_TAG", "the remote tag matches the local one (%s)" % local[:12])
        elif remote_sha:
            add(FAIL, "REMOTE_TAG", "remote tag %s != local %s" % (remote_sha[:12], local[:12]))
        else:
            add(FAIL, "REMOTE_TAG", "tag %s was never pushed" % tag)
        return

    # A network that blocks github.com but allows api.github.com is not unusual
    # (measured on the machine that cut this release), so fall back to the API
    # instead of reporting an unverifiable WARN.
    if use_gh:
        code, slug = run(["gh", "repo", "view", "--json", "nameWithOwner",
                          "-q", ".nameWithOwner"], cwd=repo)
        if code == 0 and slug.strip():
            slug = slug.strip()
            code, body = run(["gh", "api", "repos/%s/git/ref/tags/%s" % (slug, tag)], cwd=repo)
            if code == 0:
                try:
                    obj = json.loads(body)["object"]
                    sha = obj["sha"]
                    if obj.get("type") == "tag":  # annotated: peel to the commit
                        code, tag_body = run(
                            ["gh", "api", "repos/%s/git/tags/%s" % (slug, sha)], cwd=repo)
                        if code == 0:
                            sha = json.loads(tag_body)["object"]["sha"]
                    if sha == local:
                        add(PASS, "REMOTE_TAG",
                            "the remote tag matches the local one via the API (%s)" % local[:12])
                    else:
                        add(FAIL, "REMOTE_TAG",
                            "remote tag %s != local %s" % (sha[:12], local[:12]))
                    return
                except (ValueError, KeyError):
                    pass
    add(WARN, "REMOTE_TAG", "could not read remote tags (offline or blocked): %s"
        % out[:80].replace("\n", " "))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--no-gh", action="store_true", help="skip the checks that need the network")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # One run, one report: the list is module-level so callers can import and
    # inspect it, which means it has to be cleared here rather than at import.
    RESULTS.clear()

    repo = os.path.abspath(args.repo)
    code, _ = git(repo, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        print("not a git repository: %s" % repo)
        return 2

    facts = read_facts(repo)
    tag = check_tag(repo, facts)
    if tag:
        check_release_doc_in_tag(repo, tag)
    check_artifact(repo, facts, not args.no_gh, tag)
    check_release_has_assets(repo, tag, not args.no_gh)
    check_ci_green(repo, tag, not args.no_gh)
    check_plan_ledger(repo)
    check_readme_exit_codes(repo)
    check_leaks(repo)
    check_clean_tree(repo)
    check_remote(repo, tag, not args.no_gh)

    print("== release gate: %s (tag=%s) ==" % (os.path.basename(repo), tag))
    for level, name, message in RESULTS:
        print("[%s] %-20s %s" % (level, name, message))
    fails = sum(1 for level, _, _ in RESULTS if level == FAIL)
    warns = sum(1 for level, _, _ in RESULTS if level == WARN)
    passes = sum(1 for level, _, _ in RESULTS if level == PASS)
    print("\n%d FAIL / %d WARN / %d PASS" % (fails, warns, passes))
    return 1 if fails else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
