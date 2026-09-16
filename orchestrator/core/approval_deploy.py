"""Exact-commit production promotion for approved Sales Cockpit fixes."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SUPPORTED_REPO = "gert66/myngle-company-hub"
SUPPORTED_SOURCE_BRANCH = "work"
SUPPORTED_LOVABLE_PROJECT = "a4691ca7-4294-496a-af73-cdba24a5ac0f"
DEFAULT_REPO_PATH = Path("/home/myngle/myngle-company-hub")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class DeploymentError(RuntimeError):
    pass


def _run(args, *, cwd=None, timeout=300, check=True):
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if check and proc.returncode:
        raise DeploymentError(f"command failed ({proc.returncode}): {' '.join(args)}\n{proc.stderr[-1200:]}")
    return proc


def _shell(command: str, *, cwd: Path, timeout=900):
    return _run(["bash", "-lc", command], cwd=cwd, timeout=timeout)


def validate_spec(record: dict) -> dict:
    if record.get("status") != "approved":
        raise DeploymentError("proposal is not approved")
    proposal = record.get("proposal") or {}
    if proposal.get("risk") != "GREEN":
        raise DeploymentError("only GREEN proposals may deploy")
    spec = proposal.get("deployment")
    if not isinstance(spec, dict):
        return {}
    required = ("repo", "source_branch", "commit_sha", "expected_main_sha", "lovable_project_id")
    missing = [k for k in required if not spec.get(k)]
    if missing:
        raise DeploymentError(f"deployment metadata missing: {', '.join(missing)}")
    if spec["repo"] != SUPPORTED_REPO or spec["source_branch"] != SUPPORTED_SOURCE_BRANCH:
        raise DeploymentError("deployment target is outside the approved Sales Cockpit route")
    if spec["lovable_project_id"] != SUPPORTED_LOVABLE_PROJECT:
        raise DeploymentError("unexpected Lovable project")
    if not _SHA.match(str(spec["commit_sha"])) or not _SHA.match(str(spec["expected_main_sha"])):
        raise DeploymentError("deployment commit SHAs must be full 40-character lowercase SHAs")
    if not spec.get("test_commands") or not spec.get("verification_commands"):
        raise DeploymentError("tests and production verification commands are required")
    return spec


def _lovable_deploy(project_id: str) -> str:
    prompt = (
        "Use the Lovable MCP only. Call deploy_project exactly once for project " + project_id +
        ". Do not edit files, send messages, alter databases, or use any other tool. "
        "If deployment succeeds, return exactly DEPLOY_OK followed by the live URL."
    )
    proc = _run([
        "claude", "-p", prompt,
        "--allowedTools", "mcp__claude_ai_Lovable__deploy_project",
        "--output-format", "text",
    ], cwd=Path("/home/myngle/orchestrator"), timeout=600)
    text = proc.stdout.strip()
    if "DEPLOY_OK" not in text:
        raise DeploymentError(f"Lovable publish did not confirm success: {text[-800:]}")
    return text


def _rollback(repo: Path, worktree: Path, promoted_sha: str, project_id: str) -> str:
    _run(["git", "fetch", "origin", "main"], cwd=repo)
    current = _run(["git", "rev-parse", "origin/main"], cwd=repo).stdout.strip()
    if current != promoted_sha:
        raise DeploymentError("cannot auto-rollback because main advanced after promotion")
    _run(["git", "revert", "--no-edit", promoted_sha], cwd=worktree)
    rollback_sha = _run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    _run(["git", "push", "origin", f"{rollback_sha}:refs/heads/main"], cwd=worktree)
    _lovable_deploy(project_id)
    return rollback_sha


def deploy_approved(record: dict, *, repo_path: Path = DEFAULT_REPO_PATH) -> dict:
    spec = validate_spec(record)
    if not spec:
        return {"status": "not_configured"}
    repo = Path(repo_path)
    if _run(["git", "remote", "get-url", "origin"], cwd=repo).stdout.strip() != "https://github.com/gert66/myngle-company-hub.git":
        raise DeploymentError("unexpected Git origin")
    _run(["git", "fetch", "origin", "main", "work"], cwd=repo)
    current_main = _run(["git", "rev-parse", "origin/main"], cwd=repo).stdout.strip()
    if current_main != spec["expected_main_sha"]:
        raise DeploymentError("main advanced since this proposal was prepared; prepare a fresh proposal")
    ancestor = _run(["git", "merge-base", "--is-ancestor", spec["commit_sha"], "origin/work"], cwd=repo, check=False)
    if ancestor.returncode != 0:
        raise DeploymentError("approved commit is not on origin/work")
    parents = _run(["git", "show", "-s", "--format=%P", spec["commit_sha"]], cwd=repo).stdout.split()
    if len(parents) != 1:
        raise DeploymentError("approved deployment must be exactly one non-merge commit")
    changed = set(filter(None, _run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", spec["commit_sha"]], cwd=repo).stdout.splitlines()))
    approved_files = set((record.get("proposal") or {}).get("changed_files") or [])
    if not changed or not changed.issubset(approved_files):
        raise DeploymentError(f"approved commit changes files outside the proposal: {sorted(changed - approved_files)}")
    tmp = Path(tempfile.mkdtemp(prefix="sales-cockpit-deploy-"))
    worktree = tmp / "repo"
    promoted_sha = None
    try:
        _run(["git", "worktree", "add", "--detach", str(worktree), current_main], cwd=repo)
        _run(["git", "cherry-pick", spec["commit_sha"]], cwd=worktree)
        for command in spec["test_commands"]:
            _shell(str(command), cwd=worktree)
        promoted_sha = _run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        _run(["git", "push", "origin", f"{promoted_sha}:refs/heads/main", f"--force-with-lease=refs/heads/main:{current_main}"], cwd=worktree)
        publish = _lovable_deploy(spec["lovable_project_id"])
        for command in spec["verification_commands"]:
            _shell(str(command), cwd=worktree)
        return {"status": "verified", "predeploy_main_sha": current_main, "deployment_commit_sha": promoted_sha, "publish": publish}
    except Exception as exc:
        if promoted_sha:
            try:
                rollback_sha = _rollback(repo, worktree, promoted_sha, spec["lovable_project_id"])
            except Exception as rollback_exc:
                raise DeploymentError(f"deployment failed: {exc}; rollback also failed: {rollback_exc}") from exc
            raise DeploymentError(f"deployment failed and was rolled back as {rollback_sha}: {exc}") from exc
        raise
    finally:
        if worktree.exists():
            _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
