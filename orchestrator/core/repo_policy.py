"""Deterministic repository safety policy for orchestrator write jobs."""
from __future__ import annotations

import json
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ORCH_ROOT / "config" / "repo_policy.json"


class RepoPolicyError(ValueError):
    pass


def load_repo_policy(repo, path=DEFAULT_POLICY_PATH):
    if not isinstance(repo, str) or not repo.strip():
        raise RepoPolicyError("repo must be a non-empty string")
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepoPolicyError(f"cannot load repo policy {path}: {exc}") from exc
    repos = state.get("repos") if isinstance(state, dict) else None
    item = (repos or {}).get(repo)
    if not isinstance(item, dict):
        raise RepoPolicyError(f"no canonical branch policy configured for {repo}")
    origin = item.get("expected_origin")
    base = item.get("canonical_base_branch")
    if not isinstance(origin, str) or not origin.strip():
        raise RepoPolicyError(f"{repo}: expected_origin is missing")
    if not isinstance(base, str) or not base.strip():
        raise RepoPolicyError(f"{repo}: canonical_base_branch is missing")
    return {
        "repo": repo,
        "expected_origin": origin.strip(),
        "canonical_base_branch": base.strip(),
        "allow_write_on_canonical_base": bool(item.get("allow_write_on_canonical_base", False)),
    }
