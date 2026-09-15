"""Objective evidence for a Worker batch, collected by the orchestrator itself.

Nothing in here reads the Worker's report: the branch, HEAD, git status,
changed paths and diff come straight from git, and every test result comes
from a command the orchestrator ran with subprocess (argv list, shell=False).
A Worker saying "tests pass" is a claim; a TestCommandResult with status
PASS is evidence.

Test command statuses:
  PASS     exit code 0
  FAIL     exit code != 0
  TIMEOUT  still running after timeout_seconds; its process group was stopped
  ERROR    the command could not be started (missing binary, bad cwd, ...)

The diff is `git diff HEAD` plus a `git diff --no-index` against os.devnull
for every untracked file, so new files are visible too. It is truncated to
diff_max_bytes deterministically (cut at a UTF-8 character boundary, no
marker inserted; see diff_truncated / diff_total_bytes).
"""

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import gitsafe
from core.state import utc_now

__all__ = [
    "PASS", "FAIL", "TIMEOUT", "ERROR", "HARNESS_ERROR", "TEST_STATUSES",
    "DEFAULT_DIFF_MAX_BYTES", "DEFAULT_OUTPUT_MAX_BYTES", "DEFAULT_TEST_TIMEOUT_SECONDS",
    "EvidenceError", "truncate_text",
    "TestCommandResult", "run_test_command",
    "BatchEvidence", "collect_evidence",
]

PASS = "PASS"
FAIL = "FAIL"
TIMEOUT = "TIMEOUT"
ERROR = "ERROR"
HARNESS_ERROR = "HARNESS_ERROR"
TEST_STATUSES = (PASS, FAIL, TIMEOUT, ERROR)

DEFAULT_DIFF_MAX_BYTES = 200_000
DEFAULT_OUTPUT_MAX_BYTES = 200_000
DEFAULT_TEST_TIMEOUT_SECONDS = 900
DEFAULT_KILL_GRACE_SECONDS = 5
EXIT_TIMEOUT = -1   # exit_code of a command we had to stop
EXIT_NOT_RUN = -2   # exit_code of a command that never started


class EvidenceError(ValueError):
    """Raised for invalid arguments (never for a failing test command)."""


def truncate_text(text, max_bytes):
    """Return (text cut to at most max_bytes of UTF-8, truncated flag, total_bytes).

    Deterministic: the same input always yields the same output. A multibyte
    character straddling the limit is dropped rather than split.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise EvidenceError("max_bytes: must be a non-negative integer")
    data = text.encode("utf-8", errors="replace")
    total = len(data)
    if total <= max_bytes:
        return text, False, total
    return data[:max_bytes].decode("utf-8", errors="ignore"), True, total


# ---------------------------------------------------------------------------
# Test commands
# ---------------------------------------------------------------------------

@dataclass
class TestCommandResult:
    """Outcome of one test command run by the orchestrator."""

    argv: list
    cwd: str
    status: str               # one of TEST_STATUSES
    exit_code: int            # process exit status, or EXIT_* sentinel
    stdout: str
    stderr: str
    duration_seconds: float
    timeout_seconds: float
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str = None         # why the command could not run / was stopped

    @property
    def passed(self):
        return self.status == PASS

    def to_dict(self):
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timeout_seconds": self.timeout_seconds,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "error": self.error,
        }


def _check_argv(argv):
    if isinstance(argv, str):
        raise EvidenceError("test command: expected an argv list, not a shell string")
    argv = list(argv)
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise EvidenceError("test command: argv must be a non-empty list of non-empty strings")
    return argv


def _signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_process(proc, grace):
    """SIGTERM the command's process group, then SIGKILL it if it does not exit."""
    _signal_group(proc, signal.SIGTERM)
    try:
        return proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, signal.SIGKILL)
    try:
        return proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        return "", ""


def run_test_command(
    argv,
    cwd,
    timeout=DEFAULT_TEST_TIMEOUT_SECONDS,
    output_max_bytes=DEFAULT_OUTPUT_MAX_BYTES,
    kill_grace_seconds=DEFAULT_KILL_GRACE_SECONDS,
):
    """Run one argv list with shell=False and classify it. Never raises for a failing command."""
    argv = _check_argv(argv)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise EvidenceError("timeout: must be a positive number of seconds")
    cwd = str(cwd)

    def result(status, exit_code, stdout, stderr, started, error=None):
        out, out_cut, _ = truncate_text(stdout or "", output_max_bytes)
        err, err_cut, _ = truncate_text(stderr or "", output_max_bytes)
        return TestCommandResult(
            argv=argv,
            cwd=cwd,
            status=status,
            exit_code=exit_code,
            stdout=out,
            stderr=err,
            duration_seconds=round(time.monotonic() - started, 3),
            timeout_seconds=timeout,
            stdout_truncated=out_cut,
            stderr_truncated=err_cut,
            error=error,
        )

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,  # own process group so a timeout reaches child processes
        )
    except (FileNotFoundError, NotADirectoryError):
        msg = f"cannot start {argv[0]!r} in {cwd!r}"
        return result(ERROR, EXIT_NOT_RUN, "", "", started, error=msg)
    except PermissionError:
        msg = f"not executable: {argv[0]!r}"
        return result(ERROR, EXIT_NOT_RUN, "", "", started, error=msg)
    except OSError as exc:
        msg = f"failed to start command: {exc.__class__.__name__}"
        return result(ERROR, EXIT_NOT_RUN, "", "", started, error=msg)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _stop_process(proc, kill_grace_seconds)
        return result(
            TIMEOUT, EXIT_TIMEOUT, stdout, stderr, started,
            error=f"timed out after {timeout} seconds",
        )
    status = PASS if proc.returncode == 0 else FAIL
    return result(status, proc.returncode, stdout, stderr, started)


# ---------------------------------------------------------------------------
# Batch evidence
# ---------------------------------------------------------------------------

@dataclass
class BatchEvidence:
    """Everything the orchestrator observed about the repository and the tests."""

    repo: str
    collected_at: str
    head_sha: str
    branch: str
    status_porcelain: str
    changed_paths: list              # ChangedPath entries
    diff: str
    diff_truncated: bool
    diff_total_bytes: int
    diff_max_bytes: int
    tests: list = field(default_factory=list)       # TestCommandResult entries
    git_errors: list = field(default_factory=list)  # git commands that failed while collecting

    @property
    def tests_passed(self):
        """True only when at least one test command ran and every one is PASS."""
        return bool(self.tests) and all(t.status == PASS for t in self.tests)

    @property
    def tests_summary(self):
        """NONE, PASS, FAIL, or HARNESS_ERROR when a command could not run reliably."""
        if not self.tests:
            return "NONE"
        if any(t.status in (ERROR, TIMEOUT) for t in self.tests):
            return HARNESS_ERROR
        return PASS if self.tests_passed else FAIL

    def to_dict(self):
        return {
            "repo": self.repo,
            "collected_at": self.collected_at,
            "head_sha": self.head_sha,
            "branch": self.branch,
            "status_porcelain": self.status_porcelain,
            "changed_paths": [c.to_dict() for c in self.changed_paths],
            "diff": self.diff,
            "diff_truncated": self.diff_truncated,
            "diff_total_bytes": self.diff_total_bytes,
            "diff_max_bytes": self.diff_max_bytes,
            "tests": [t.to_dict() for t in self.tests],
            "tests_passed": self.tests_passed,
            "tests_summary": self.tests_summary,
            "git_errors": list(self.git_errors),
        }


def _full_diff(repo, head, changed, errors):
    """Tracked changes vs HEAD followed by each untracked file diffed against os.devnull."""
    parts = []
    res = gitsafe.run_git(repo, ["diff", "HEAD", "--"] if head else ["diff", "--"])
    if res.ok:
        parts.append(res.stdout)
    else:
        errors.append(f"git diff: {res.error or res.stderr.strip()}")
    for entry in changed:
        if entry.status != "??":
            continue
        res = gitsafe.run_git(repo, ["diff", "--no-index", "--", os.devnull, entry.path])
        if res.exit_code in (0, 1) and res.error is None:  # 1 = differences found
            parts.append(res.stdout)
        else:
            errors.append(f"git diff --no-index {entry.path}: {res.error or res.stderr.strip()}")
    return "".join(parts)


def collect_evidence(
    repo,
    test_commands=(),
    diff_max_bytes=DEFAULT_DIFF_MAX_BYTES,
    test_timeout=DEFAULT_TEST_TIMEOUT_SECONDS,
    test_cwd=None,
    output_max_bytes=DEFAULT_OUTPUT_MAX_BYTES,
):
    """Collect git facts for repo, then run each test command (argv list) in test_cwd or repo.

    Git problems are recorded in git_errors rather than raised, so a broken
    repository still yields evidence of being broken. Invalid arguments
    raise EvidenceError before anything runs.
    """
    repo_path = Path(repo)
    if not repo_path.is_dir():
        raise EvidenceError(f"repo: {str(repo)!r} is not a directory")
    if isinstance(diff_max_bytes, bool) or not isinstance(diff_max_bytes, int) or diff_max_bytes < 0:
        raise EvidenceError("diff_max_bytes: must be a non-negative integer")
    commands = [_check_argv(c) for c in test_commands]
    cwd = str(test_cwd) if test_cwd is not None else str(repo_path)

    errors = []
    head = gitsafe.head_sha(repo_path)
    status = gitsafe.status_porcelain(repo_path)
    if status is None:
        errors.append("git status failed")
        status = ""
    try:
        changed = gitsafe.changed_paths(repo_path)
    except gitsafe.GitSafeError as exc:
        errors.append(str(exc))
        changed = []
    diff, truncated, total = truncate_text(_full_diff(repo_path, head, changed, errors), diff_max_bytes)

    evidence = BatchEvidence(
        repo=str(repo_path),
        collected_at=utc_now(),
        head_sha=head,
        branch=gitsafe.current_branch(repo_path),
        status_porcelain=status,
        changed_paths=changed,
        diff=diff,
        diff_truncated=truncated,
        diff_total_bytes=total,
        diff_max_bytes=diff_max_bytes,
        git_errors=errors,
    )
    for argv in commands:
        evidence.tests.append(
            run_test_command(argv, cwd, timeout=test_timeout, output_max_bytes=output_max_bytes)
        )
    return evidence
