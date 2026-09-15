"""Read-only git safety checks for a Worker batch, stdlib only.

Every git call is an explicit argv list run with subprocess (never a shell)
inside the repository directory. This module performs no write operations:
it inspects the repository and returns structured results that a later
batch can act on (git add / commit / push are deliberately not here).

Provided checks:
  * preflight()      repo is a git top-level, origin URL matches (optional
                     trailing .git ignored), branch is exactly the expected
                     work branch, main/master are refused unconditionally,
                     working tree is clean, HEAD SHA is captured.
  * check_head()     HEAD is still the SHA captured earlier, so a commit made
                     by someone else in the meantime is detected before any
                     write.
  * changed_paths()  every path git status reports (staged, unstaged and
                     untracked), with its XY status code.
  * validate_scope() pathlib/fnmatch matching of changed paths against
                     allowed_paths and forbidden_paths; forbidden wins, an
                     empty allow list allows nothing, absolute or ".." paths
                     are always out of scope.
  * staging_plan()   the exact in-scope paths eligible for `git add --`.
                     If any changed path is out of scope or forbidden the
                     plan is not ok and its path list is empty (fail closed).

Pattern semantics (validate_scope): a pattern matches a path when
fnmatch.fnmatchcase(path, pattern) is true (note that `*` also spans `/`),
or when the pattern has no wildcard and names the path itself or one of its
parent directories. Patterns and paths are repo-relative POSIX paths; a
trailing slash on a path denotes the directory itself ("tests/" == "tests").
"""

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "PROTECTED_BRANCHES", "GitSafeError",
    "GitCommandResult", "run_git", "normalize_remote_url",
    "CheckResult", "PreflightResult", "preflight",
    "head_sha", "current_branch", "origin_url", "status_porcelain", "is_clean",
    "HeadGuardResult", "check_head", "BranchFreshnessResult", "branch_freshness_gate",
    "ChangedPath", "changed_paths",
    "ScopeResult", "validate_scope", "path_matches", "normalize_pattern",
    "StagingPlan", "staging_plan",
]

# Refused as a working branch no matter what the job configuration says.
PROTECTED_BRANCHES = frozenset(("main", "master"))
GIT_BIN = "git"
DEFAULT_GIT_TIMEOUT_SECONDS = 60
_WILDCARDS = "*?["


class GitSafeError(ValueError):
    """Raised for invalid arguments (never for a failing check)."""


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------

@dataclass
class GitCommandResult:
    """Outcome of one git invocation."""

    argv: list
    exit_code: int    # process exit status, or -1 when git did not run/finish
    stdout: str
    stderr: str
    error: str = None  # set when git could not be started or timed out

    @property
    def ok(self):
        return self.exit_code == 0 and self.error is None

    def to_dict(self):
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }


def run_git(repo, args, timeout=DEFAULT_GIT_TIMEOUT_SECONDS, git_bin=GIT_BIN):
    """Run `git <args>` in repo with an explicit argv list. Never raises for a failed command."""
    if isinstance(args, str) or not all(isinstance(a, str) for a in args):
        raise GitSafeError("args: expected a list of strings (argv), not a shell string")
    argv = [git_bin, *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GitCommandResult(argv, -1, "", "", error=f"git timed out after {timeout} seconds")
    except (FileNotFoundError, NotADirectoryError):
        return GitCommandResult(argv, -1, "", "", error=f"cannot run {git_bin} in {repo}")
    except OSError as exc:
        return GitCommandResult(argv, -1, "", "", error=f"cannot run git: {exc.__class__.__name__}")
    return GitCommandResult(argv, proc.returncode, proc.stdout, proc.stderr)


def normalize_remote_url(url):
    """Strip surrounding whitespace and one optional trailing `.git`; nothing else."""
    if not isinstance(url, str):
        raise GitSafeError("remote url: expected a string")
    text = url.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text


# ---------------------------------------------------------------------------
# Single-fact readers (each returns None when git cannot answer)
# ---------------------------------------------------------------------------

def head_sha(repo):
    """Full SHA of HEAD, or None (no commits yet / not a repository)."""
    res = run_git(repo, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def current_branch(repo):
    """Short branch name, or None when HEAD is detached or there is no repository."""
    res = run_git(repo, ["symbolic-ref", "--short", "--quiet", "HEAD"])
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def origin_url(repo):
    """Configured URL of remote `origin`, or None."""
    res = run_git(repo, ["remote", "get-url", "origin"])
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def status_porcelain(repo):
    """`git status --porcelain=v1 --untracked-files=all` text, or None on failure."""
    res = run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"])
    return res.stdout if res.ok else None


def is_clean(repo):
    """True only when git status reports nothing at all (untracked files count as dirty)."""
    text = status_porcelain(repo)
    return text is not None and text.strip() == ""


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str

    def to_dict(self):
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class PreflightResult:
    """All preflight checks plus the facts they were based on."""

    ok: bool
    repo: str
    checks: list = field(default_factory=list)
    head_sha: str = None
    branch: str = None
    origin_url: str = None

    @property
    def failures(self):
        return [c for c in self.checks if not c.ok]

    def to_dict(self):
        return {
            "ok": self.ok,
            "repo": self.repo,
            "checks": [c.to_dict() for c in self.checks],
            "head_sha": self.head_sha,
            "branch": self.branch,
            "origin_url": self.origin_url,
        }


def _require_str(value, name):
    if not isinstance(value, str) or not value.strip():
        raise GitSafeError(f"{name}: must be a non-empty string")
    return value.strip()


def preflight(repo, expected_origin, expected_branch, require_clean=True):
    """Run every batch-start safety check. Returns a PreflightResult; ok only if all pass.

    Checks stop early only when later ones cannot be answered (missing
    directory, not a git top-level); otherwise all are recorded so a
    refusal explains everything that is wrong at once.
    """
    expected_origin = _require_str(expected_origin, "expected_origin")
    expected_branch = _require_str(expected_branch, "expected_branch")
    repo_path = Path(repo)
    result = PreflightResult(ok=False, repo=str(repo_path))
    checks = result.checks

    def fail(name, detail):
        checks.append(CheckResult(name, False, detail))

    def passed(name, detail):
        checks.append(CheckResult(name, True, detail))

    if not repo_path.is_dir():
        fail("repo_exists", f"{repo_path} is not a directory")
        return result
    passed("repo_exists", str(repo_path))

    top = run_git(repo_path, ["rev-parse", "--show-toplevel"])
    if not top.ok:
        fail("is_git_repo", top.error or top.stderr.strip() or "git rev-parse failed")
        return result
    try:
        top_ok = Path(top.stdout.strip()).resolve() == repo_path.resolve()
    except OSError:
        top_ok = False
    if not top_ok:
        fail("is_git_repo", f"{repo_path} is not the top level of a git work tree")
        return result
    passed("is_git_repo", "repository top level")

    actual_origin = origin_url(repo_path)
    result.origin_url = actual_origin
    if actual_origin is None:
        fail("origin_url", "remote origin is not configured")
    elif normalize_remote_url(actual_origin) != normalize_remote_url(expected_origin):
        fail("origin_url", f"origin is {actual_origin!r}, expected {expected_origin!r}")
    else:
        passed("origin_url", actual_origin)

    branch = current_branch(repo_path)
    result.branch = branch
    if branch is None:
        fail("branch", "HEAD is detached or unborn")
    elif branch != expected_branch:
        fail("branch", f"on {branch!r}, expected {expected_branch!r}")
    else:
        passed("branch", branch)

    if expected_branch in PROTECTED_BRANCHES:
        fail("protected_branch", f"expected_branch {expected_branch!r} is protected")
    elif branch in PROTECTED_BRANCHES:
        fail("protected_branch", f"refusing to work on protected branch {branch!r}")
    else:
        passed("protected_branch", f"{branch!r} is not in {sorted(PROTECTED_BRANCHES)}")

    if require_clean:
        status = status_porcelain(repo_path)
        if status is None:
            fail("clean_tree", "git status failed")
        elif status.strip():
            lines = [ln for ln in status.splitlines() if ln.strip()]
            fail("clean_tree", f"{len(lines)} path(s) modified or untracked: " + "; ".join(lines[:10]))
        else:
            passed("clean_tree", "working tree clean")

    sha = head_sha(repo_path)
    result.head_sha = sha
    if sha is None:
        fail("head_sha", "HEAD does not point at a commit")
    else:
        passed("head_sha", sha)

    result.ok = all(c.ok for c in checks)
    return result


# ---------------------------------------------------------------------------
# Branch freshness gate
# ---------------------------------------------------------------------------

@dataclass
class BranchFreshnessResult:
    ok: bool
    repo: str
    canonical_base_branch: str
    base_commit: str = None
    current_branch: str = None
    ahead_by: int = None
    behind_by: int = None
    checked_at: str = None
    checks: list = field(default_factory=list)

    @property
    def failures(self):
        return [c for c in self.checks if not c.ok]

    def to_dict(self):
        return {
            "ok": self.ok, "repo": self.repo,
            "canonical_base_branch": self.canonical_base_branch,
            "base_commit": self.base_commit, "current_branch": self.current_branch,
            "ahead_by": self.ahead_by, "behind_by": self.behind_by,
            "checked_at": self.checked_at,
            "checks": [c.to_dict() for c in self.checks],
        }


def branch_freshness_gate(repo_path, repo_name, expected_origin, expected_branch, canonical_base_branch,
                          *, mode="write", fetch_remote=True, checked_at=None):
    """Fail-closed freshness check for write jobs; never merges, rebases or changes branches.

    For write mode this verifies repository identity, remote, branch, clean tree, protected-branch
    policy, fetches the configured canonical base, then records HEAD/base divergence. Any
    behind_by > 0 blocks the write. Read jobs use the ordinary preflight only and do not require
    freshness against the canonical base.
    """
    from datetime import datetime, timezone
    repo_name = _require_str(repo_name, "repo_name")
    expected_origin = _require_str(expected_origin, "expected_origin")
    expected_branch = _require_str(expected_branch, "expected_branch")
    canonical_base_branch = _require_str(canonical_base_branch, "canonical_base_branch")
    if mode not in ("read", "write"):
        raise GitSafeError("mode must be 'read' or 'write'")
    result = BranchFreshnessResult(False, repo_name, canonical_base_branch)
    result.checked_at = checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    pre = preflight(repo_path, expected_origin, expected_branch, require_clean=True)
    result.current_branch = pre.branch
    result.checks.extend(pre.checks)
    # Repo identity and origin must be trustworthy before any remote contact.
    critical = {"repo_exists", "is_git_repo", "origin_url"}
    if any((not c.ok) and c.name in critical for c in pre.checks):
        return result
    if mode == "read":
        result.base_commit = pre.head_sha
        result.ahead_by = 0
        result.behind_by = 0
        result.checks.append(CheckResult("freshness", True, "read-only job: write freshness gate not required"))
        result.ok = all(c.ok for c in result.checks)
        return result

    # Never contact a remote until preflight proved that origin is exactly the configured remote.
    if fetch_remote:
        fetch = run_git(repo_path, ["fetch", "--quiet", "origin",
                                    f"+refs/heads/{canonical_base_branch}:refs/remotes/origin/{canonical_base_branch}"])
        if not fetch.ok:
            result.checks.append(CheckResult("fetch_base", False,
                fetch.error or fetch.stderr.strip() or f"cannot fetch origin/{canonical_base_branch}"))
            return result
        result.checks.append(CheckResult("fetch_base", True, f"fetched origin/{canonical_base_branch}"))

    base_ref = f"refs/remotes/origin/{canonical_base_branch}"
    base = run_git(repo_path, ["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"])
    if not base.ok or not base.stdout.strip():
        result.checks.append(CheckResult("canonical_base", False, f"missing canonical base origin/{canonical_base_branch}"))
        return result
    result.base_commit = base.stdout.strip()
    result.checks.append(CheckResult("canonical_base", True, f"origin/{canonical_base_branch} at {result.base_commit}"))

    div = run_git(repo_path, ["rev-list", "--left-right", "--count", f"{base_ref}...HEAD"])
    if not div.ok:
        result.checks.append(CheckResult("divergence", False, div.error or div.stderr.strip() or "git rev-list failed"))
        return result
    try:
        behind, ahead = (int(x) for x in div.stdout.strip().split())
    except (ValueError, TypeError):
        result.checks.append(CheckResult("divergence", False, f"invalid divergence output: {div.stdout!r}"))
        return result
    result.behind_by, result.ahead_by = behind, ahead
    if behind > 0:
        result.checks.append(CheckResult("freshness", False,
            f"branch {expected_branch!r} is {behind} commit(s) behind canonical base origin/{canonical_base_branch} "
            f"and {ahead} commit(s) ahead; building here risks losing or conflicting with newer base changes"))
    else:
        result.checks.append(CheckResult("freshness", True,
            f"branch {expected_branch!r} is current with origin/{canonical_base_branch} (ahead {ahead}, behind 0)"))
    result.ok = all(c.ok for c in result.checks)
    return result


# ---------------------------------------------------------------------------
# HEAD guard
# ---------------------------------------------------------------------------

@dataclass
class HeadGuardResult:
    ok: bool
    expected_sha: str
    actual_sha: str
    detail: str

    def to_dict(self):
        return {
            "ok": self.ok,
            "expected_sha": self.expected_sha,
            "actual_sha": self.actual_sha,
            "detail": self.detail,
        }


def check_head(repo, expected_sha):
    """Confirm HEAD is still expected_sha. Call immediately before any write or commit."""
    expected_sha = _require_str(expected_sha, "expected_sha")
    actual = head_sha(repo)
    if actual is None:
        return HeadGuardResult(False, expected_sha, None, "HEAD does not point at a commit")
    if actual != expected_sha:
        return HeadGuardResult(
            False, expected_sha, actual,
            f"HEAD drifted from {expected_sha[:12]} to {actual[:12]}: external change detected",
        )
    return HeadGuardResult(True, expected_sha, actual, "HEAD unchanged")


# ---------------------------------------------------------------------------
# Changed paths
# ---------------------------------------------------------------------------

@dataclass
class ChangedPath:
    """One entry of `git status --porcelain=v1 -z`. status is the two-letter XY code."""

    path: str
    status: str
    orig_path: str = None  # for renames/copies: the path the entry came from

    def to_dict(self):
        return {"path": self.path, "status": self.status, "orig_path": self.orig_path}


def changed_paths(repo):
    """Every path git status reports (staged, unstaged, untracked), sorted by path.

    Raises GitSafeError when git status itself fails, because "no changes"
    must never be reported by accident.
    """
    res = run_git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if not res.ok:
        raise GitSafeError(f"git status failed in {repo}: {res.error or res.stderr.strip()}")
    entries = []
    tokens = res.stdout.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue  # trailing empty token
        xy, path = token[:2], token[3:]
        orig = None
        if ("R" in xy or "C" in xy) and i < len(tokens) and tokens[i]:
            orig = tokens[i]
            i += 1
        entries.append(ChangedPath(path=path, status=xy, orig_path=orig))
    entries.sort(key=lambda e: e.path)
    return entries


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------

def normalize_pattern(pattern):
    """Repo-relative POSIX pattern: strip whitespace, a leading './' and trailing '/'."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise GitSafeError("scope pattern: must be a non-empty string")
    text = pattern.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    if not text or text == ".":
        raise GitSafeError(f"scope pattern {pattern!r} would match the whole repository")
    if PurePosixPath(text).is_absolute() or ".." in PurePosixPath(text).parts:
        raise GitSafeError(f"scope pattern {pattern!r} must be relative without '..'")
    return text


def _unsafe_path_reason(path):
    """Why a changed path can never be in scope, or None when it is well-formed."""
    if not isinstance(path, str) or not path.strip():
        return "empty path"
    pure = PurePosixPath(path)
    if pure.is_absolute():
        return "absolute path"
    if ".." in pure.parts:
        return "path escapes the repository ('..')"
    return None


def path_matches(path, pattern):
    """True when pattern covers path (see module docstring for semantics)."""
    pattern = normalize_pattern(pattern)
    if _unsafe_path_reason(path):
        return False
    # A trailing slash names the directory itself, not an entry inside it, so
    # "tests/" must not satisfy "tests/*" just because fnmatch's `*` matches "".
    path = path.rstrip("/")
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if any(ch in pattern for ch in _WILDCARDS):
        return False
    return path == pattern or path.startswith(pattern + "/")


@dataclass
class ScopeResult:
    """Outcome of checking changed paths against a scope. ok only when nothing is out of scope."""

    ok: bool
    in_scope: list = field(default_factory=list)     # paths allowed for staging
    out_of_scope: list = field(default_factory=list)  # {"path", "reason"} not covered by allowed
    forbidden: list = field(default_factory=list)     # {"path", "pattern"} hit a forbidden pattern
    allowed_paths: list = field(default_factory=list)
    forbidden_paths: list = field(default_factory=list)

    @property
    def detail(self):
        if self.ok:
            return f"{len(self.in_scope)} path(s) in scope"
        parts = []
        if self.forbidden:
            parts.append("forbidden: " + ", ".join(f"{f['path']} ({f['pattern']})" for f in self.forbidden))
        if self.out_of_scope:
            parts.append("out of scope: " + ", ".join(o["path"] for o in self.out_of_scope))
        return "; ".join(parts)

    def to_dict(self):
        return {
            "ok": self.ok,
            "detail": self.detail,
            "in_scope": list(self.in_scope),
            "out_of_scope": [dict(o) for o in self.out_of_scope],
            "forbidden": [dict(f) for f in self.forbidden],
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
        }


def _pattern_list(patterns, name):
    if patterns is None:
        return []
    if isinstance(patterns, str):
        raise GitSafeError(f"{name}: expected a list of patterns, not a string")
    return [normalize_pattern(p) for p in patterns]


def validate_scope(paths, allowed_paths, forbidden_paths=None):
    """Check repo-relative paths (str or ChangedPath) against allowed/forbidden patterns.

    Forbidden patterns win over allowed ones. With no allowed patterns every
    path is out of scope. A rename is checked on both its old and new path.
    """
    allowed = _pattern_list(allowed_paths, "allowed_paths")
    forbidden = _pattern_list(forbidden_paths, "forbidden_paths")
    result = ScopeResult(ok=True, allowed_paths=allowed, forbidden_paths=forbidden)

    candidates = []
    for item in paths:
        if isinstance(item, ChangedPath):
            candidates.append(item.path)
            if item.orig_path:
                candidates.append(item.orig_path)
        else:
            candidates.append(item)

    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        reason = _unsafe_path_reason(path)
        if reason:
            result.out_of_scope.append({"path": path, "reason": reason})
            continue
        hit = next((p for p in forbidden if path_matches(path, p)), None)
        if hit is not None:
            result.forbidden.append({"path": path, "pattern": hit})
            continue
        if not any(path_matches(path, p) for p in allowed):
            result.out_of_scope.append({"path": path, "reason": "not covered by allowed_paths"})
            continue
        result.in_scope.append(path)

    result.in_scope.sort()
    result.ok = not result.out_of_scope and not result.forbidden
    return result


# ---------------------------------------------------------------------------
# Staging plan (no git add is executed here)
# ---------------------------------------------------------------------------

@dataclass
class StagingPlan:
    """Exact paths a later batch may pass to `git add --`. Empty unless ok."""

    ok: bool
    paths: list
    changed: list          # ChangedPath entries the plan was built from
    scope: ScopeResult
    detail: str

    def git_add_argv(self):
        """argv for the eventual staging call, or None when the plan is not ok / empty."""
        if not self.ok or not self.paths:
            return None
        return [GIT_BIN, "add", "--", *self.paths]

    def to_dict(self):
        return {
            "ok": self.ok,
            "detail": self.detail,
            "paths": list(self.paths),
            "changed": [c.to_dict() for c in self.changed],
            "scope": self.scope.to_dict(),
        }


def staging_plan(repo, allowed_paths, forbidden_paths=None):
    """Build the list of changed paths eligible for staging; fail closed on any violation.

    Deleted and renamed paths are included (git add -- <path> records a
    deletion). Nothing is staged, committed or pushed.
    """
    changed = changed_paths(repo)
    scope = validate_scope(changed, allowed_paths, forbidden_paths)
    if not scope.ok:
        return StagingPlan(False, [], changed, scope, "refused: " + scope.detail)
    if not changed:
        return StagingPlan(True, [], changed, scope, "no changes to stage")
    return StagingPlan(True, list(scope.in_scope), changed, scope, f"{len(scope.in_scope)} path(s) eligible")
