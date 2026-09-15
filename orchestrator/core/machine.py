"""Job state machine: Brain -> Worker -> Evidence -> Reviewer -> Brain, stdlib only.

One job lives in one directory:

  job.json        Job contract (validate_job), written once by create_job
  state.json      canonical State (core.state); phase + runtime bookkeeping,
                  saved atomically after EVERY transition
  events.jsonl    transition log (core.state.append_event)
  steps/<id>.json completed Claude outputs keyed by call id; existence means
                  "done", so a restart reuses them instead of calling again
  evidence/*.json BatchEvidence collected by the orchestrator itself
  logs/*.json     classification of every Claude invocation (success or not)
  lock            flock target: two runners can never execute the same job

Phases (core.contracts.PHASES) and how they are left:

  QUEUED     -> PLANNING
  PLANNING   Brain decides: NEXT/REPAIR -> WORKING, REVIEW_AGAIN -> REVIEWING,
             WAIT -> WAITING, NEEDS_HUMAN -> NEEDS_HUMAN, DONE, ERROR
  WORKING    git preflight, then the Worker runs one batch -> EVIDENCE
  EVIDENCE   git diff/status + orchestrator-run tests -> REVIEWING
             (identical diff after a repair -> NEEDS_HUMAN)
  REVIEWING  Reviewer: PASS -> COMMITTING, FAIL -> PLANNING, NEEDS_HUMAN
  COMMITTING scope check + git add/commit of the reviewed batch -> PLANNING
  WAITING    resume() returns to runtime.retry_phase once wait_until has passed
  NEEDS_HUMAN approve() returns to runtime.retry_phase (default PLANNING)
  DONE/ERROR terminal

Every Claude call goes through _invoke: a call id is allocated and persisted
in runtime.inflight before the call, the Claude call cap is checked, the
result is classified (core.classify) and validated (core.contracts), and the
payload is written to steps/<call id>.json. Rate limits and transient errors
become WAITING with a backoff; invalid output is retried up to max_attempts;
auth/budget/task errors and every exhausted cap become NEEDS_HUMAN.

Dry-run: the Machine refuses to run with dry_run=True unless the injected
runner declares dry_run_safe = True, never runs test commands, and never
stages or commits. The CLI additionally runs dry-runs on a copy of the job.
"""

import fcntl
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template

from core import classify as classify_mod
from core import evidence as evidence_mod
from core import gitsafe
from core.hermes_advisor import request_advice
from core.repo_policy import RepoPolicyError, load_repo_policy
from core.claude_runner import DEFAULT_CLAUDE_BIN, DEFAULT_TIMEOUT_SECONDS, ClaudeResult, run_claude
from core.providers import (DEFAULT_WORKER_COMPLEXITY, DEFAULT_WORKER_PROVIDER, WORKER_COMPLEXITIES, WORKER_PROVIDERS)
from core.contracts import (
    ContractError, load_schema, validate_brain, validate_job, validate_reviewer, validate_worker,
)
from core.state import (
    StateError, append_event, load_json, load_state, new_state, save_json, save_state, utc_now,
)

__all__ = [
    "MachineError", "LockHeld", "JobLock", "RepoLockHeld", "RepoLock",
    "TERMINAL_PHASES", "PAUSED_PHASES", "RUNNABLE_PHASES", "DEFAULT_CAPS",
    "fake_result", "claude_runner", "DryRunRunner",
    "new_runtime", "create_job", "Machine",
]

ORCH_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = ORCH_ROOT / "prompts"

JOB_FILE = "job.json"
STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
LOCK_FILE = "lock"
REPO_LOCKS_DIR = ORCH_ROOT / "locks" / "repos"
STEPS_DIR = "steps"
EVIDENCE_DIR = "evidence"
LOGS_DIR = "logs"
CALLS_FILE = "calls.jsonl"
SYSTEM_PROMPTS_DIR = "system_prompts"
HERMES_ADVISORY_FLAG = ORCH_ROOT / "config" / "hermes_advisory.enabled"

TERMINAL_PHASES = ("DONE", "ERROR")
PAUSED_PHASES = ("WAITING", "NEEDS_HUMAN")
RUNNABLE_PHASES = ("QUEUED", "PLANNING", "WORKING", "EVIDENCE", "REVIEWING", "COMMITTING")

DEFAULT_CAPS = {
    "max_batches": 20,            # overridden by job.max_steps
    "max_repairs_per_batch": 2,   # overridden by job.max_repairs_per_step
    "max_reviews_per_batch": 3,
    "max_claude_calls": 100,
    "max_attempts": 3,            # retries of one Claude call (invalid output, rate limit, ...)
}
DEFAULT_ROLE_POLICY = {
    role: {"model": None, "effort": None, "fallback_model": None, "max_budget_usd": None}
    for role in ("brain", "worker", "reviewer")
}

DEFAULT_CONFIG = {
    "repo_path": None,            # local checkout; required
    "origin": None,               # expected origin URL; default https://github.com/<job.repo>.git
    "allowed_paths": ["*"],
    "forbidden_paths": [],
    "test_commands": [],          # argv lists run by the orchestrator after the Worker
    "test_timeout": evidence_mod.DEFAULT_TEST_TIMEOUT_SECONDS,
    "prompt_diff_chars": 60_000,
    "prompt_output_chars": 4_000,
    "input_file_chars": 30_000,
    "role_policy": None,           # strict per-role Claude policy; None preserves CLI defaults
    "worker_provider": DEFAULT_WORKER_PROVIDER,  # Brain/Reviewer always Claude; only Worker is selectable
    "worker_complexity": DEFAULT_WORKER_COMPLEXITY,  # low|normal|high; auto provider routing only
    "claude_bin": DEFAULT_CLAUDE_BIN,  # persisted account/profile; survives supervisor restarts
    "quota_routing": True,           # permanent shared account/model-bucket router
    "quota_state": "/home/myngle/orchestrator/config/claude_quota.json",
    "not_before": None,              # optional UTC/offset ISO-8601 earliest start
    "freshness_fetch_remote": True,   # fetch canonical base before every write freshness decision
}
DEFAULT_MAX_TRANSITIONS = 200

ROLE_VALIDATORS = {"brain": validate_brain, "worker": validate_worker, "reviewer": validate_reviewer}
# Brain and Reviewer must never edit; no role may push, merge or move branches.
_GIT_TOOLS = [
    "Bash(git push:*)", "Bash(git merge:*)", "Bash(git rebase:*)", "Bash(git reset:*)",
    "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git commit:*)",
    "Bash(git stash:*)", "Bash(git clean:*)", "Bash(git add:*)", "Bash(git branch:*)", "Bash(git tag:*)",
]
_EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
ROLE_DISALLOWED_TOOLS = {
    "brain": _EDIT_TOOLS + _GIT_TOOLS,
    "reviewer": _EDIT_TOOLS + _GIT_TOOLS,
    "worker": _GIT_TOOLS,
}
WRITE_PERMISSION = "acceptEdits"
READ_PERMISSION = "plan"


class MachineError(Exception):
    """Job directory, configuration or invariant problem (never a Claude failure)."""


class LockHeld(MachineError):
    """Another runner holds this job's lock."""


class RepoLockHeld(MachineError):
    """Another job currently owns this exact repository/worktree checkout."""


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

class RepoLock:
    """Cross-job exclusive lock keyed by canonical repo/worktree path."""

    def __init__(self, repo_path, holder=None, job_dir=None, lock_dir=None):
        canonical = str(Path(repo_path).resolve())
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        self.path = Path(lock_dir or REPO_LOCKS_DIR) / f"{digest}.lock"
        self.repo_path = canonical
        self.holder = holder or "unknown"
        self.job_dir = str(Path(job_dir).resolve()) if job_dir else None
        self._fh = None

    @staticmethod
    def _reservation_active(meta):
        job_dir = meta.get("job_dir") if isinstance(meta, dict) else None
        if not job_dir:
            return False
        state_path = Path(job_dir) / STATE_FILE
        if not state_path.is_file():
            return False
        try:
            state = load_state(state_path)
        except Exception as exc:
            raise RepoLockHeld(f"cannot verify prior repo reservation {state_path}: {exc}") from exc
        return state["phase"] not in TERMINAL_PHASES and bool((state.get("runtime") or {}).get("batch"))

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            owner = fh.read().strip() or "another job"
            fh.close()
            raise RepoLockHeld(f"repo checkout is locked: {self.repo_path} ({owner})") from None
        fh.seek(0)
        raw = fh.read().strip()
        if raw:
            try:
                previous = json.loads(raw)
            except ValueError:
                previous = None
            if previous and previous.get("holder") != self.holder and self._reservation_active(previous):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
                raise RepoLockHeld(
                    f"repo checkout is reserved by paused/active job {previous.get('holder')}: {self.repo_path}"
                )
        fh.seek(0)
        fh.truncate()
        json.dump({"holder": self.holder, "job_dir": self.job_dir, "repo": self.repo_path}, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
        self._fh = fh
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


class JobLock:
    """Exclusive, non-blocking flock on <job_dir>/lock for the duration of a with-block."""

    def __init__(self, job_dir):
        self.path = Path(job_dir) / LOCK_FILE
        self._fh = None

    def __enter__(self):
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockHeld(f"job is locked by another runner: {self.path}") from None
        self._fh = fh
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


# ---------------------------------------------------------------------------
# Runners: callable(role, prompt, *, cwd, json_schema, permission_mode, disallowed_tools)
# ---------------------------------------------------------------------------

def fake_result(payload, prompt="", cwd="."):
    """A ClaudeResult equivalent to a successful --json-schema call returning payload."""
    top = {"type": "result", "subtype": "success", "is_error": False, "structured_output": payload}
    return ClaudeResult(
        argv=["fake-claude"], cwd=str(cwd), exit_code=0, stdout=json.dumps(top), stderr="",
        duration_seconds=0.0, timed_out=False, structured=True, schema_requested=True,
        parsed_json=top, prompt_chars=len(prompt),
    )


def claude_runner(claude_bin=DEFAULT_CLAUDE_BIN, timeout=DEFAULT_TIMEOUT_SECONDS):
    """The plain, Claude-only runner: one core.claude_runner.run_claude call per invocation.

    Accepts (and ignores) worker_provider so it stays a valid Machine runner
    even though it never routes to anything but Claude; use
    core.providers.build_runner() when worker_provider="codex" should work.
    """
    def run(role, prompt, *, cwd, json_schema, permission_mode, disallowed_tools,
            model=None, effort=None, fallback_model=None, max_budget_usd=None,
            append_system_prompt_file=None, worker_provider=None, worker_complexity=None,
            worker_escalated=False, worker_mode="read", allowed_paths=None, forbidden_paths=None):
        return run_claude(
            prompt, cwd=cwd, timeout=timeout, claude_bin=claude_bin, json_schema=json_schema,
            permission_mode=permission_mode, disallowed_tools=disallowed_tools, model=model,
            effort=effort, fallback_model=fallback_model, max_budget_usd=max_budget_usd,
            append_system_prompt_file=append_system_prompt_file,
        )
    return run


class DryRunRunner:
    """Canned answers for --dry-run: one no-op batch, a PASS review, then DONE. Touches nothing."""

    dry_run_safe = True
    STEP_ID = "dry-run-01"

    def __init__(self):
        self.calls = []

    def __call__(self, role, prompt, **kwargs):
        self.calls.append(role)
        if role == "brain":
            if sum(1 for r in self.calls if r == "brain") == 1:
                payload = {"schema_version": 1, "decision": "NEXT", "reason": "dry run: one no-op batch",
                           "next_batch": {"step_id": self.STEP_ID, "goal": "Dry run: change nothing.",
                                          "instructions": "Do nothing.", "acceptance_criteria": []}}
            else:
                payload = {"schema_version": 1, "decision": "DONE", "reason": "dry run complete"}
        elif role == "worker":
            payload = {"schema_version": 1, "step_id": self.STEP_ID, "status": "COMPLETED",
                       "summary": "dry run: no files changed", "files_changed": []}
        else:
            payload = {"schema_version": 1, "step_id": self.STEP_ID, "verdict": "PASS",
                       "scope_ok": True, "tests_ok": True, "summary": "dry run", "findings": []}
        return fake_result(payload, prompt, kwargs.get("cwd", "."))


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------

def new_runtime(job, config=None):
    """Validated runtime section for a fresh job: config + caps + zeroed counters."""
    cfg = dict(DEFAULT_CONFIG)
    caps = dict(DEFAULT_CAPS)
    for key, value in dict(config or {}).items():
        if key == "caps":
            caps.update(value)
        elif key in cfg:
            cfg[key] = value
        else:
            raise MachineError(f"config: unknown key {key!r}")
    if "max_steps" in job:
        caps["max_batches"] = job["max_steps"]
    if "max_repairs_per_step" in job:
        caps["max_repairs_per_batch"] = job["max_repairs_per_step"]
    if not isinstance(cfg["repo_path"], str) or not cfg["repo_path"].strip():
        raise MachineError("config.repo_path: local checkout path is required")
    if cfg.get("not_before") is not None:
        value = cfg["not_before"]
        if not isinstance(value, str) or not value.strip():
            raise MachineError("config.not_before: must be an offset-aware ISO-8601 timestamp or null")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise MachineError("config.not_before: invalid ISO-8601 timestamp") from None
        if parsed.tzinfo is None:
            raise MachineError("config.not_before: timezone/offset is required")
        cfg["not_before"] = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if cfg["origin"] is None:
        cfg["origin"] = f"https://github.com/{job['repo']}.git"
    cfg["allowed_paths"] = [gitsafe.normalize_pattern(p) for p in cfg["allowed_paths"]]
    cfg["forbidden_paths"] = [gitsafe.normalize_pattern(p) for p in cfg["forbidden_paths"]]
    for argv in cfg["test_commands"]:
        if isinstance(argv, str) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise MachineError("config.test_commands: each command must be a non-empty argv list")
    if cfg["worker_provider"] not in WORKER_PROVIDERS:
        raise MachineError(f"config.worker_provider: {cfg['worker_provider']!r} not in {list(WORKER_PROVIDERS)}")
    if cfg["worker_complexity"] not in WORKER_COMPLEXITIES:
        raise MachineError(f"config.worker_complexity: {cfg['worker_complexity']!r} not in {list(WORKER_COMPLEXITIES)}")
    for key, value in caps.items():
        if key not in DEFAULT_CAPS or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MachineError(f"caps.{key}: must be a non-negative integer")
    raw_policy = cfg.get("role_policy")
    policy = {role: dict(values) for role, values in DEFAULT_ROLE_POLICY.items()}
    if raw_policy is not None:
        if not isinstance(raw_policy, dict):
            raise MachineError("config.role_policy: must be an object")
        for role, values in raw_policy.items():
            if role not in policy or not isinstance(values, dict):
                raise MachineError(f"config.role_policy.{role}: unknown role or non-object policy")
            unknown = set(values) - set(policy[role])
            if unknown:
                raise MachineError(f"config.role_policy.{role}: unknown keys {sorted(unknown)}")
            policy[role].update(values)
    from core.claude_runner import EFFORT_LEVELS
    for role, values in policy.items():
        for key in ("model", "fallback_model"):
            value = values[key]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise MachineError(f"config.role_policy.{role}.{key}: must be a non-empty string or null")
            if isinstance(value, str):
                values[key] = value.strip()
        if values["effort"] is not None and values["effort"] not in EFFORT_LEVELS:
            raise MachineError(f"config.role_policy.{role}.effort: invalid level {values['effort']!r}")
        budget = values["max_budget_usd"]
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0):
            raise MachineError(f"config.role_policy.{role}.max_budget_usd: must be > 0 or null")
    cfg["role_policy"] = policy
    cfg["caps"] = caps
    return {
        "config": cfg,
        "counters": {"brain": 0, "batches": 0, "claude_calls": 0},
        "totals": _empty_totals(),
        "batch": None,
        "inflight": None,
        "retry_phase": None,
        "cap_hit": None,
        "human": None,
        "history": [],
        "used_step_ids": [],
    }


def create_job(job_dir, job, config=None):
    """Create <job_dir> with job.json, a QUEUED state.json and the first event. Returns the state."""
    job_dir = Path(job_dir)
    if (job_dir / STATE_FILE).exists():
        raise MachineError(f"job already exists: {job_dir}")
    job = validate_job(job)
    runtime = new_runtime(job, config)
    job_dir.mkdir(parents=True, exist_ok=True)
    for sub in (STEPS_DIR, EVIDENCE_DIR, LOGS_DIR):
        (job_dir / sub).mkdir(exist_ok=True)
    save_json(job_dir / JOB_FILE, job)
    state = new_state(job["job_id"])
    state.update({"wait_until": None, "human_question": None, "runtime": runtime})
    saved = save_state(job_dir / STATE_FILE, state)
    append_event(job_dir / EVENTS_FILE, None, "QUEUED", "job submitted")
    return saved


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _format_ts(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cut(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated, {len(text)} chars total ...]"


def _tail(text, limit):
    text = text or ""
    return text if len(text) <= limit else "[...]" + text[-limit:]


def _diff_hash(status, diff):
    return hashlib.sha256((status or "").encode("utf-8") + b"\n" + (diff or "").encode("utf-8")).hexdigest()


def _field(result, key, default=None):
    """Read a ClaudeResult attribute or the same key of its to_dict() form."""
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _json_value(mapping, *keys, default=None):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _numeric(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _argv_option(argv, flag):
    argv = list(argv or [])
    try:
        idx = argv.index(flag)
    except ValueError:
        return None
    return argv[idx + 1] if idx + 1 < len(argv) else None


def _telemetry_from_result(result):
    """Extract stable provider/model/token telemetry from any Worker/Claude result."""
    parsed = _field(result, "parsed_json")
    parsed = parsed if isinstance(parsed, dict) else {}
    usage = _json_value(parsed, "usage", default={})
    usage = usage if isinstance(usage, dict) else {}
    model_usage = _json_value(parsed, "modelUsage", "model_usage", default={})
    model_usage = model_usage if isinstance(model_usage, dict) else {}
    cache_creation = _json_value(usage, "cache_creation", "cacheCreation", default={})
    cache_creation = cache_creation if isinstance(cache_creation, dict) else {}

    def metric(*keys):
        value = _json_value(usage, *keys)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        total = 0
        found = False
        for data in model_usage.values():
            value = _json_value(data, *keys)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += value
                found = True
        return total if found else 0

    input_tokens = metric("input_tokens", "inputTokens", "total_input_tokens")
    output_tokens = metric("output_tokens", "outputTokens", "total_output_tokens")
    thinking_tokens = metric("thinking_tokens", "thinkingTokens", "total_thought_tokens")
    total_tokens = metric("total_tokens", "totalTokens") or (input_tokens + output_tokens + thinking_tokens)

    # If an auto route tried Gemini successfully enough to consume tokens but then
    # fell back to Claude, account for those upstream tokens too.
    upstream = _field(result, "gemini_usage_before_fallback", {})
    upstream = upstream if isinstance(upstream, dict) else {}
    upstream_input = _numeric(upstream.get("input_tokens"), 0)
    upstream_output = _numeric(upstream.get("output_tokens"), 0)
    upstream_thinking = _numeric(upstream.get("thinking_tokens"), 0)
    upstream_total = _numeric(upstream.get("total_tokens"), 0)
    input_tokens += upstream_input
    output_tokens += upstream_output
    thinking_tokens += upstream_thinking
    total_tokens += upstream_total or (upstream_input + upstream_output + upstream_thinking)

    cost_by_model = {}
    for model, data in model_usage.items():
        cost = _json_value(data, "costUSD", "cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost_by_model[str(model)] = cost
    argv = _field(result, "argv", [])
    prefix_path = _argv_option(argv, "--append-system-prompt-file")
    prefix_hash, prefix_chars = None, 0
    if prefix_path:
        try:
            prefix_text = Path(prefix_path).read_text(encoding="utf-8")
            prefix_hash = hashlib.sha256(prefix_text.encode("utf-8")).hexdigest()
            prefix_chars = len(prefix_text)
        except OSError:
            pass
    denials = _json_value(parsed, "permission_denials", "permissionDenials", default=[])
    provider = _field(result, "provider", "claude")
    model_value = _field(result, "model") or _json_value(parsed, "model", "canonical_model")
    if model_value is None:
        model_value = _argv_option(argv, "--model")
    return {
        "provider": provider,
        "provider_route": _field(result, "provider_route"),
        "model": model_value,
        "service_tier": _field(result, "service_tier") or _json_value(parsed, "service_tier"),
        "retries": int(_numeric(_field(result, "retries", 0), 0)),
        "fallback_events": list(_field(result, "fallback_events", []) or []),
        "provider_attempts": list(_field(result, "provider_attempts", []) or []),
        "model_requested": _argv_option(argv, "--model"),
        "effort_requested": _argv_option(argv, "--effort"),
        "fallback_requested": _argv_option(argv, "--fallback-model"),
        "max_budget_usd_requested": _argv_option(argv, "--max-budget-usd"),
        "stable_prefix_sha256": prefix_hash,
        "stable_prefix_chars": prefix_chars,
        "models_used": sorted(str(k) for k in model_usage),
        "canonical_model": _json_value(parsed, "model", "canonical_model"),
        "total_cost_usd": _numeric(_json_value(parsed, "total_cost_usd", "totalCostUsd"), 0.0),
        "cost_usd_per_model": cost_by_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "total_tokens": total_tokens,
        "upstream_gemini_usage": upstream,
        "cache_creation_input_tokens": metric("cache_creation_input_tokens", "cacheCreationInputTokens"),
        "cache_read_input_tokens": metric("cache_read_input_tokens", "cacheReadInputTokens"),
        "cache_creation_1h_input_tokens": _numeric(_json_value(cache_creation, "ephemeral_1h_input_tokens", "ephemeral1hInputTokens")),
        "cache_creation_5m_input_tokens": _numeric(_json_value(cache_creation, "ephemeral_5m_input_tokens", "ephemeral5mInputTokens")),
        "num_turns": _numeric(_json_value(parsed, "num_turns", "numTurns")),
        "duration_ms": _numeric(_json_value(parsed, "duration_ms", "durationMs")),
        "duration_api_ms": _numeric(_json_value(parsed, "duration_api_ms", "durationApiMs")),
        "session_id": _json_value(parsed, "session_id", "sessionId"),
        "stop_reason": _json_value(parsed, "stop_reason", "stopReason"),
        "terminal_reason": _json_value(parsed, "terminal_reason", "terminalReason"),
        "subtype": _json_value(parsed, "subtype"),
        "is_error": bool(_json_value(parsed, "is_error", "isError", default=False)),
        "permission_denials_count": len(denials) if isinstance(denials, list) else 0,
        "prompt_chars": _numeric(_field(result, "prompt_chars", 0)),
        "duration_seconds": _numeric(_field(result, "duration_seconds", 0), 0.0),
    }

def _append_jsonl(path, record):
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def _empty_totals():
    return {
        "claude_calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
        "thinking_tokens": 0, "total_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "duration_seconds": 0.0, "by_role": {},
    }


def _render(name, **values):
    template = Template((PROMPTS_DIR / name).read_text(encoding="utf-8"))
    return template.safe_substitute(values)


def _render_split(name, dynamic_start, dynamic_end, **values):
    """Render one existing prompt as stable system prefix + dynamic user suffix.

    The source markdown remains the single wording source; only section placement changes.
    """
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    if dynamic_start not in text or dynamic_end not in text:
        raise MachineError(f"prompt split markers missing in {name}")
    before, rest = text.split(dynamic_start, 1)
    dynamic_body, after = rest.split(dynamic_end, 1)
    stable = Template(before + dynamic_end + after).safe_substitute(values)
    dynamic = Template(dynamic_start + dynamic_body).safe_substitute(values)
    return stable, dynamic


# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------

class Machine:
    """Drive one job. Construct, then call run() / resume() / approve() / abort()."""

    def __init__(self, job_dir, runner, *, dry_run=False, clock=utc_now, log=None):
        self.job_dir = Path(job_dir)
        self.runner = runner
        self.dry_run = bool(dry_run)
        self.clock = clock
        self.log = log or (lambda message: None)
        if self.dry_run and not getattr(runner, "dry_run_safe", False):
            raise MachineError("dry-run requires a runner that declares dry_run_safe = True")
        self.job = validate_job(load_json(self.job_dir / JOB_FILE))
        self.schemas = {role: load_schema(role) for role in ROLE_VALIDATORS}
        self._reload()

    # -- persistence -------------------------------------------------------

    def _reload(self):
        self.state = load_state(self.job_dir / STATE_FILE)
        if not isinstance(self.state.get("runtime"), dict):
            raise MachineError(f"{self.job_dir / STATE_FILE} has no runtime section (not created by create_job)")
        self.rt = self.state["runtime"]
        self.rt.setdefault("totals", _empty_totals())
        if not self.rt.get("config", {}).get("role_policy"):
            self.rt["config"]["role_policy"] = {role: dict(values) for role, values in DEFAULT_ROLE_POLICY.items()}
        self.rt["config"].setdefault("worker_provider", "claude")
        self.rt["config"].setdefault("worker_complexity", DEFAULT_WORKER_COMPLEXITY)
        self.rt.setdefault("branch_freshness", None)
        self.rt.setdefault("branch_freshness_history", [])
        for key in ("wait_until", "human_question"):
            self.state.setdefault(key, None)

    def _persist(self):
        self.state["updated_at"] = self.clock()
        save_state(self.job_dir / STATE_FILE, self.state)

    def _transition(self, to, reason, **fields):
        """Persist state (atomically) and then log the event. Every phase change goes through here."""
        frm = self.state["phase"]
        self.state.update(fields)
        self.state["phase"] = to
        self._persist()
        append_event(self.job_dir / EVENTS_FILE, frm, to, reason, step_id=self.state["step_id"])
        self.log(f"{frm} -> {to}: {reason}")

    def _needs_human(self, question, retry_phase, cap=None, **fields):
        self.rt["retry_phase"] = retry_phase
        self.rt["cap_hit"] = cap
        self.rt["human"] = {"question": question, "asked_at": self.clock(), "answer": None, "answered_at": None}
        self._transition("NEEDS_HUMAN", question, human_question=question, **fields)
        batch = self.rt.get("batch") or {}
        repeated = bool(cap) or self.state.get("attempt", 0) > 0 or batch.get("repairs", 0) > 0 or batch.get("reviews", 0) > 0
        if repeated:
            self._hermes_advice("needs_human", question)

    def _wait(self, seconds, retry_phase, reason, **fields):
        self.rt["retry_phase"] = retry_phase
        until = _format_ts(_parse_ts(self.clock()) + timedelta(seconds=int(seconds)))
        self._transition("WAITING", f"{reason}; resume after {until}", wait_until=until, **fields)

    def _hermes_advice(self, trigger, reason):
        """Record read-only Hermes advice without changing machine decisions."""
        if self.dry_run or not HERMES_ADVISORY_FLAG.is_file():
            return None
        record = request_advice(self.job_dir, trigger, reason)
        if record.get("ok"):
            self.log(f"Hermes advisory recorded for {trigger}")
        else:
            self.log(f"Hermes advisory unavailable for {trigger}: {record.get('error')}")
        return record

    def _branch_freshness_check(self, stage):
        """Deterministic, fail-closed write safety gate. AI roles cannot override it."""
        if self.job["mode"] != "write" or self.dry_run:
            return True
        cfg = self.rt["config"]
        try:
            policy = load_repo_policy(self.job["repo"])
        except RepoPolicyError as exc:
            record = {
                "repo": self.job["repo"], "canonical_base_branch": None, "base_commit": None,
                "current_branch": gitsafe.current_branch(cfg["repo_path"]),
                "ahead_by": None, "behind_by": None, "checked_at": self.clock(),
                "ok": False, "stage": stage, "error": str(exc),
            }
            self.rt["branch_freshness"] = record
            self.rt["branch_freshness_history"].append(dict(record))
            self._persist()
            self._needs_human(
                f"Branch freshness gate blocked write job: {exc}. Configure an explicit canonical base; "
                "Brain/Worker/Reviewer/Hermes cannot override this safety rule.",
                self.state["phase"],
            )
            return False
        if gitsafe.normalize_remote_url(cfg["origin"]) != gitsafe.normalize_remote_url(policy["expected_origin"]):
            record = {
                "repo": self.job["repo"], "canonical_base_branch": policy["canonical_base_branch"],
                "base_commit": None, "current_branch": gitsafe.current_branch(cfg["repo_path"]),
                "ahead_by": None, "behind_by": None, "checked_at": self.clock(),
                "ok": False, "stage": stage,
                "error": f"job origin {cfg['origin']!r} does not match policy origin {policy['expected_origin']!r}",
            }
            self.rt["branch_freshness"] = record
            self.rt["branch_freshness_history"].append(dict(record))
            self._persist()
            self._needs_human(
                f"Branch freshness gate blocked write job: {record['error']}. Fix the repository configuration; no build started.",
                self.state["phase"],
            )
            return False
        gate = gitsafe.branch_freshness_gate(
            cfg["repo_path"], self.job["repo"], policy["expected_origin"], self.job["branch"],
            policy["canonical_base_branch"], mode="write", fetch_remote=bool(cfg.get("freshness_fetch_remote", True)),
            checked_at=self.clock(),
        )
        record = gate.to_dict()
        record["stage"] = stage
        self.rt["branch_freshness"] = record
        self.rt["branch_freshness_history"].append(dict(record))
        self._persist()
        if gate.ok:
            return True
        detail = "; ".join(f"{c.name}: {c.detail}" for c in gate.failures) or "unknown freshness failure"
        behind = gate.behind_by
        lag = f" It is {behind} commit(s) behind the canonical base." if isinstance(behind, int) and behind > 0 else ""
        self._needs_human(
            f"Branch freshness gate blocked write job before {stage}: {detail}.{lag} No Worker was allowed to write; "
            f"no automatic merge/rebase was attempted. Create a fresh branch/worktree from current "
            f"origin/{policy['canonical_base_branch']} (or make an explicit safe sync decision), then retry.",
            self.state["phase"],
        )
        return False

    # -- public entry points ----------------------------------------------

    def run(self, max_transitions=DEFAULT_MAX_TRANSITIONS):
        """Step until the job is DONE/ERROR, paused (WAITING/NEEDS_HUMAN) or max_transitions is hit."""
        with JobLock(self.job_dir):
            self._reload()
            return self._loop(max_transitions)

    def resume(self, force=False, max_transitions=DEFAULT_MAX_TRANSITIONS):
        """Continue a WAITING job at its retry_phase once wait_until has passed (or force=True)."""
        with JobLock(self.job_dir):
            self._reload()
            if self.state["phase"] != "WAITING":
                raise MachineError(f"resume: job is {self.state['phase']}, not WAITING")
            until = self.state.get("wait_until")
            if not force and until and self.clock() < until:
                self.log(f"not due until {until}")
                return "WAITING"
            target = self.rt.get("retry_phase") or "PLANNING"
            self.rt["retry_phase"] = None
            self._transition(target, "resumed" + (" (forced)" if force else ""), wait_until=None)
            return self._loop(max_transitions)

    def approve(self, answer="approved"):
        """Record the human answer, lift the cap that was hit (if any) and return to retry_phase."""
        with JobLock(self.job_dir):
            self._reload()
            if self.state["phase"] != "NEEDS_HUMAN":
                raise MachineError(f"approve: job is {self.state['phase']}, not NEEDS_HUMAN")
            human = self.rt.get("human") or {"question": self.state.get("human_question")}
            human.update({"answer": answer, "answered_at": self.clock()})
            self.rt["human"] = human
            self._lift_cap(self.rt.get("cap_hit"))
            self.rt["cap_hit"] = None
            target = self.rt.get("retry_phase") or "PLANNING"
            self.rt["retry_phase"] = None
            self._transition(target, f"approved: {answer}", human_question=None, attempt=0)
            return target

    def set_config(self, *, test_commands=None, test_timeout=None, caps=None, role_policy=None, answer="config updated by human"):
        """Safely update retry-relevant config while paused; never edits arbitrary state."""
        with JobLock(self.job_dir):
            self._reload()
            if self.state["phase"] != "NEEDS_HUMAN":
                raise MachineError(f"set_config: job is {self.state['phase']}, not NEEDS_HUMAN")
            cfg = self.rt["config"]
            changed = []
            if test_commands is not None:
                commands = []
                for argv in test_commands:
                    if isinstance(argv, str) or not argv or not all(isinstance(a, str) and a for a in argv):
                        raise MachineError("set_config.test_commands: each command must be a non-empty argv list")
                    commands.append(list(argv))
                cfg["test_commands"] = commands
                changed.append("test_commands")
            if test_timeout is not None:
                if isinstance(test_timeout, bool) or not isinstance(test_timeout, int) or test_timeout <= 0:
                    raise MachineError("set_config.test_timeout: must be a positive integer")
                cfg["test_timeout"] = test_timeout
                changed.append("test_timeout")
            if caps:
                for key, value in caps.items():
                    if key not in DEFAULT_CAPS or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise MachineError(f"set_config.caps.{key}: must be a non-negative integer")
                    cfg["caps"][key] = value
                    changed.append(f"caps.{key}")
            if role_policy is not None:
                if not isinstance(role_policy, dict):
                    raise MachineError("set_config.role_policy: must be an object")
                policy = {role: dict(values) for role, values in cfg["role_policy"].items()}
                from core.claude_runner import EFFORT_LEVELS
                for role, values in role_policy.items():
                    if role not in policy or not isinstance(values, dict):
                        raise MachineError(f"set_config.role_policy.{role}: unknown role or non-object policy")
                    unknown = set(values) - set(policy[role])
                    if unknown:
                        raise MachineError(f"set_config.role_policy.{role}: unknown keys {sorted(unknown)}")
                    candidate = dict(policy[role])
                    candidate.update(values)
                    for key in ("model", "fallback_model"):
                        value = candidate[key]
                        if value is not None and (not isinstance(value, str) or not value.strip()):
                            raise MachineError(f"set_config.role_policy.{role}.{key}: must be a non-empty string or null")
                        if isinstance(value, str):
                            candidate[key] = value.strip()
                    if candidate["effort"] is not None and candidate["effort"] not in EFFORT_LEVELS:
                        raise MachineError(f"set_config.role_policy.{role}.effort: invalid level {candidate['effort']!r}")
                    budget = candidate["max_budget_usd"]
                    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0):
                        raise MachineError(f"set_config.role_policy.{role}.max_budget_usd: must be > 0 or null")
                    policy[role] = candidate
                cfg["role_policy"] = policy
                changed.append("role_policy")
            if not changed:
                raise MachineError("set_config: no supported setting supplied")
            self._persist()
            append_event(self.job_dir / EVENTS_FILE, "NEEDS_HUMAN", "NEEDS_HUMAN",
                         f"config updated by human: {', '.join(changed)}; {answer}",
                         step_id=self.state["step_id"])
            return {"test_commands": cfg["test_commands"], "test_timeout": cfg["test_timeout"],
                    "caps": dict(cfg["caps"]), "role_policy": cfg["role_policy"]}

    def extend_scope(self, paths, answer="scope extended by human"):
        """Add explicitly human-approved allowed paths while paused for human input."""
        with JobLock(self.job_dir):
            self._reload()
            if self.state["phase"] != "NEEDS_HUMAN":
                raise MachineError(f"extend_scope: job is {self.state['phase']}, not NEEDS_HUMAN")
            if isinstance(paths, str):
                paths = [paths]
            normalized = [gitsafe.normalize_pattern(p) for p in paths]
            if not normalized:
                raise MachineError("extend_scope: at least one path is required")
            allowed = self.rt["config"]["allowed_paths"]
            for path in normalized:
                if path not in allowed:
                    allowed.append(path)
            self._persist()
            append_event(self.job_dir / EVENTS_FILE, "NEEDS_HUMAN", "NEEDS_HUMAN",
                         f"scope extended by human: {', '.join(normalized)}; {answer}",
                         step_id=self.state["step_id"])
            return list(allowed)

    def complete(self, reason="completed by human approval"):
        """Finish a paused job without another Claude call after verified committed work."""
        with JobLock(self.job_dir):
            self._reload()
            if self.state["phase"] != "NEEDS_HUMAN":
                raise MachineError(f"complete: job is {self.state['phase']}, not NEEDS_HUMAN")
            if self.rt.get("batch") is not None:
                raise MachineError("complete: active batch still open")
            history = self.rt.get("history") or []
            if not history or history[-1].get("outcome") != "committed" or history[-1].get("verdict") != "PASS":
                raise MachineError("complete: no verified committed PASS batch in history")
            self.rt["inflight"] = None
            self.rt["retry_phase"] = None
            self.rt["cap_hit"] = None
            self.rt["human"] = None
            self._transition("DONE", reason, human_question=None, last_error=None, wait_until=None, attempt=0)
            return "DONE"

    def refresh_evidence(self, reason="evidence refresh requested"):
        """Re-run evidence without treating an unchanged diff as a failed repair."""
        with JobLock(self.job_dir):
            self._reload()
            if self.state["phase"] in TERMINAL_PHASES:
                raise MachineError(f"refresh_evidence: job is already {self.state['phase']}")
            batch = self.rt.get("batch")
            if not batch:
                raise MachineError("refresh_evidence: no active batch")
            batch["evidence_refresh"] = True
            batch["evidence"] = None
            batch["reviewer"] = None
            self.rt["inflight"] = None
            self.rt["retry_phase"] = None
            self.rt["cap_hit"] = None
            self._transition("EVIDENCE", reason, human_question=None, wait_until=None)
            return "EVIDENCE"

    def abort(self, reason="aborted by human"):
        with JobLock(self.job_dir):
            self._reload()
            if self.state["phase"] in TERMINAL_PHASES:
                raise MachineError(f"abort: job is already {self.state['phase']}")
            self.rt["inflight"] = None
            self._transition("ERROR", reason, last_error=reason, wait_until=None, human_question=None)
            return "ERROR"

    def _lift_cap(self, cap):
        """Increase policy limits without erasing historical counters."""
        if cap not in DEFAULT_CAPS:
            return None
        caps = self.rt["config"]["caps"]
        current = caps[cap]
        # Claude calls are approved in chunks; structural caps grow one unit at a time.
        delta = max(1, current) if cap == "max_claude_calls" else 1
        caps[cap] = current + delta
        return caps[cap]

    def _repo_lock_needed(self):
        """Keep one checkout stable for the entire lifetime of an open batch."""
        return bool(self.rt.get("batch")) and self.state["phase"] in {
            "PLANNING", "WORKING", "EVIDENCE", "REVIEWING", "COMMITTING"
        }

    def _run_one(self):
        phase = self.state["phase"]
        getattr(self, f"_do_{phase.lower()}")()

    def _loop(self, max_transitions):
        transitions = 0
        while transitions < max_transitions:
            phase = self.state["phase"]
            if phase in TERMINAL_PHASES or phase in PAUSED_PHASES:
                break
            if not self._repo_lock_needed():
                self._run_one()
                transitions += 1
                continue
            try:
                with RepoLock(self.rt["config"]["repo_path"], holder=self.job["job_id"], job_dir=self.job_dir):
                    while transitions < max_transitions and self._repo_lock_needed():
                        self._run_one()
                        transitions += 1
                        if self.state["phase"] in TERMINAL_PHASES or self.state["phase"] in PAUSED_PHASES:
                            break
            except RepoLockHeld as exc:
                self._wait(60, phase, str(exc))
                break
        return self.state["phase"]

    # -- Claude invocation ------------------------------------------------

    def _allocate_call_id(self, role):
        batch = self.rt.get("batch") or {}
        if role == "brain":
            return f"brain-{self.rt['counters']['brain'] + 1:03d}"
        if role == "worker":
            return f"{batch['step_id']}-worker-{batch['worker_calls'] + 1}"
        return f"{batch['step_id']}-reviewer-{batch['review_calls'] + 1}"

    def _invoke(self, role, prompt, *, cwd, permission_mode, check=None, system_prompt_file=None):
        """Return the validated payload for role, or None after the machine paused/retried itself.

        Idempotent across restarts: the call id is persisted before the call
        and a completed steps/<call id>.json is reused instead of calling again.
        """
        phase = self.state["phase"]
        inflight = self.rt.get("inflight")
        if inflight and inflight["role"] == role and inflight["phase"] == phase:
            call_id = inflight["call_id"]
        else:
            call_id = self._allocate_call_id(role)
            self.rt["inflight"] = {"role": role, "call_id": call_id, "phase": phase}
        path = self.job_dir / STEPS_DIR / f"{call_id}.json"
        if path.exists():
            self.log(f"reusing completed output {call_id}")
            try:
                return ROLE_VALIDATORS[role](load_json(path)["payload"])
            except (ContractError, KeyError, TypeError) as exc:
                raise MachineError(f"corrupt step output {path}: {exc}") from exc

        caps = self.rt["config"]["caps"]
        counters = self.rt["counters"]
        if counters["claude_calls"] >= caps["max_claude_calls"]:
            self._needs_human(
                f"Claude call cap reached ({caps['max_claude_calls']}). Approve to allow another "
                f"{caps['max_claude_calls']} calls, or abort.", phase, cap="max_claude_calls",
            )
            return None
        counters["claude_calls"] += 1
        self._persist()  # call id + count are durable before Claude runs

        policy = self.rt["config"]["role_policy"][role]
        # Only Worker is provider-selectable. Brain and Reviewer never receive
        # Worker routing fields and therefore remain Claude-only.
        provider_kwargs = {}
        if role == "worker":
            batch = self.rt.get("batch") or {}
            provider_kwargs = {
                "worker_provider": self.rt["config"]["worker_provider"],
                "worker_complexity": self.rt["config"].get("worker_complexity", DEFAULT_WORKER_COMPLEXITY),
                "worker_escalated": bool(batch.get("repair")) or int(batch.get("reviews", 0) or 0) > 0,
                "worker_mode": self.job["mode"],
                "allowed_paths": list(self.rt["config"].get("allowed_paths") or []),
                "forbidden_paths": list(self.rt["config"].get("forbidden_paths") or []),
            }
        result = self.runner(
            role, prompt, cwd=str(cwd), json_schema=self.schemas[role],
            permission_mode=permission_mode, disallowed_tools=ROLE_DISALLOWED_TOOLS[role],
            model=policy["model"], effort=policy["effort"],
            fallback_model=policy["fallback_model"], max_budget_usd=policy["max_budget_usd"],
            append_system_prompt_file=system_prompt_file, **provider_kwargs,
        )
        cls = classify_mod.classify(result)
        payload, problem = None, None
        if cls.category == classify_mod.SUCCESS:
            try:
                payload = ROLE_VALIDATORS[role](cls.payload)
                if check:
                    message = check(payload)
                    if message:
                        raise ContractError(message)
            except ContractError as exc:
                payload, problem = None, f"invalid {role} output: {exc}"
        self._write_call_log(call_id, role, cls, result, problem)
        if payload is not None:
            save_json(path, {
                "role": role, "call_id": call_id, "phase": phase, "step_id": self.state["step_id"],
                "completed_at": self.clock(), "payload": payload,
                "exit_code": _field(result, "exit_code"),
                "duration_seconds": _field(result, "duration_seconds"),
            })
            self.state["attempt"] = 0
            return payload
        self._handle_call_failure(role, cls, problem, phase, result=result)
        return None

    def _write_call_log(self, call_id, role, cls, result, problem):
        n = self.rt["counters"]["claude_calls"]
        telemetry = _telemetry_from_result(result)
        record = dict(cls.to_dict(), payload=None)
        record.update({
            "call_id": call_id, "role": role, "provider": _field(result, "provider", "claude"),
            "problem": problem, "ts": self.clock(),
            "exit_code": _field(result, "exit_code"),
            "timed_out": _field(result, "timed_out"),
            "duration_seconds": _field(result, "duration_seconds"),
            "stderr": _tail(_field(result, "stderr", ""), 2000),
            "stdout": None if cls.category == classify_mod.SUCCESS and not problem
            else _tail(_field(result, "stdout", ""), 2000),
            "telemetry": telemetry,
        })
        save_json(self.job_dir / LOGS_DIR / f"{call_id}.call-{n}.json", record)
        _append_jsonl(self.job_dir / CALLS_FILE, {
            "job_id": self.job["job_id"], "call_id": call_id, "role": role,
            "provider": _field(result, "provider", "claude"),
            "phase": self.state["phase"], "step_id": self.state.get("step_id"),
            "attempt": self.state["attempt"], "ts": self.clock(),
            "classification": cls.category, "validated": cls.category == classify_mod.SUCCESS and not problem,
            "problem": problem, **telemetry,
        })
        totals = self.rt.setdefault("totals", _empty_totals())
        role_totals = totals["by_role"].setdefault(role, _empty_totals() | {"by_role": {}})
        for target in (totals, role_totals):
            target["claude_calls"] += 1
            target["cost_usd"] = round(target["cost_usd"] + telemetry["total_cost_usd"], 8)
            target["duration_seconds"] = round(target["duration_seconds"] + telemetry["duration_seconds"], 3)
            for key in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens"):
                target[key] = int(target.get(key, 0) or 0) + int(telemetry[key] or 0)
        self.rt["last_call_telemetry"] = dict(telemetry)
        self._persist()

    def _handle_call_failure(self, role, cls, problem, phase, result=None):
        category = classify_mod.INVALID_OUTPUT if problem else cls.category
        reason = problem or f"{role} call {category}: {cls.reason}"
        attempt = self.state["attempt"] + 1
        max_attempts = self.rt["config"]["caps"]["max_attempts"]

        # A normal Claude usage limit is capacity scheduling, not a human decision.
        # When quota routing is enabled, retire the account that actually hit the
        # limit, immediately retry on another eligible account if one exists, or
        # wait until the earliest known reset. This path intentionally does not
        # consume max_attempts: a reset can take hours and is not a failing job.
        if (category == classify_mod.RATE_LIMIT and self.rt["config"].get("quota_routing")
                and _field(result, "provider", "claude") == "claude"):
            router = getattr(self.runner, "quota_router", None)
            if router is not None:
                policy = self.rt["config"]["role_policy"][role]
                requested_model = policy.get("model")
                now = _parse_ts(self.clock())
                failed_route = _field(result, "quota_route")
                if failed_route:
                    router.mark_rate_limited(failed_route, now=now)
                alternative = router.select(role, requested_model=requested_model)
                if alternative is not None:
                    self._transition(
                        phase,
                        f"quota reroute after {reason}: next account {alternative['account']}",
                        last_error=reason, attempt=0,
                    )
                    return
                retry_at = router.next_available_at(role, requested_model=requested_model, now=now)
                if retry_at:
                    self.rt["retry_phase"] = phase
                    self._transition(
                        "WAITING", f"{reason}; quota resume after {retry_at}",
                        wait_until=retry_at, last_error=reason, attempt=attempt,
                    )
                    return

        if attempt >= 2 and attempt < max_attempts:
            self._hermes_advice(f"{role}_retry_{attempt}", reason)
        if category in (classify_mod.AUTH, classify_mod.BUDGET, classify_mod.CONFIG, classify_mod.TASK_ERROR):
            self._needs_human(f"{reason}. Fix the cause, then approve to retry or abort.", phase,
                              last_error=reason, attempt=attempt)
        elif attempt >= max_attempts:
            self._needs_human(f"{reason} ({attempt} attempts, cap {max_attempts}). Approve to retry or abort.",
                              phase, cap="max_attempts", last_error=reason, attempt=attempt)
        elif category == classify_mod.INVALID_OUTPUT:
            self._transition(phase, f"retry {attempt}/{max_attempts}: {reason}", last_error=reason, attempt=attempt)
        else:
            self._wait(cls.backoff_seconds * attempt, phase, f"retry {attempt}/{max_attempts}: {reason}",
                       last_error=reason, attempt=attempt)

    def _consume(self, role):
        """The inflight call's payload has been acted on; the next call gets a fresh id."""
        self.rt["inflight"] = None
        self.state["attempt"] = 0
        if role == "brain":
            self.rt["counters"]["brain"] += 1
        elif role == "worker":
            self.rt["batch"]["worker_calls"] += 1
        else:
            self.rt["batch"]["review_calls"] += 1

    # -- phase handlers ---------------------------------------------------

    def _do_queued(self):
        not_before = self.rt["config"].get("not_before")
        if not_before:
            now = _parse_ts(self.clock())
            target = _parse_ts(not_before)
            if now < target:
                seconds = max(1, int((target - now).total_seconds()))
                self._wait(seconds, "QUEUED", f"scheduled start not before {not_before}")
                return
        if not self._branch_freshness_check("job start"):
            return
        self._transition("PLANNING", "job picked up")

    def _do_planning(self):
        batch = self.rt.get("batch")

        def check(payload):
            decision = payload["decision"]
            if decision == "NEXT" and payload["next_batch"]["step_id"] in self.rt["used_step_ids"]:
                return f"NEXT reuses step_id {payload['next_batch']['step_id']!r}; step ids must be new"
            if decision in ("REPAIR", "REVIEW_AGAIN") and not batch:
                return f"{decision} without an active batch"
            if decision == "REVIEW_AGAIN" and not batch.get("evidence"):
                return "REVIEW_AGAIN before any evidence was collected"
            return None

        system_prompt, prompt = self._brain_prompt_parts()
        system_file = self._stable_prompt_file("brain", "job", system_prompt)
        payload = self._invoke("brain", prompt, cwd=self._cwd(), permission_mode=READ_PERMISSION, check=check,
                               system_prompt_file=system_file)
        if payload is None:
            return
        self._consume("brain")
        decision, reason = payload["decision"], payload["reason"]
        caps = self.rt["config"]["caps"]
        if decision == "NEXT":
            if self.rt["counters"]["batches"] >= caps["max_batches"]:
                self._needs_human(f"Batch cap reached ({caps['max_batches']}). Brain wanted: {reason}. "
                                  "Approve to allow more batches, or abort.", "PLANNING", cap="max_batches")
                return
            if batch:
                self._close_batch("abandoned", reason)
            self._start_batch(payload["next_batch"], reason)
        elif decision == "REPAIR":
            if batch["repairs"] >= 1:
                self._hermes_advice("repeat_repair", reason)
            if batch["repairs"] >= caps["max_repairs_per_batch"]:
                self._needs_human(f"Repair cap reached for {batch['step_id']} ({caps['max_repairs_per_batch']}). "
                                  f"Brain wanted: {reason}. Approve to allow more repairs, or abort.",
                                  "PLANNING", cap="max_repairs_per_batch")
                return
            batch["repairs"] += 1
            batch["repair"] = payload["next_batch"]
            self._transition("WORKING", f"brain REPAIR #{batch['repairs']}: {reason}")
        elif decision == "REVIEW_AGAIN":
            self._transition("REVIEWING", f"brain REVIEW_AGAIN: {reason}")
        elif decision == "WAIT":
            self._wait(payload["wait_seconds"], "PLANNING", f"brain WAIT: {reason}")
        elif decision == "NEEDS_HUMAN":
            self._needs_human(payload["human_question"], "PLANNING")
        elif decision == "DONE":
            if batch:
                self._close_batch("open at DONE", reason)
            self._transition("DONE", f"brain DONE: {reason}", step_id=None)
        else:
            self._transition("ERROR", f"brain ERROR: {reason}", last_error=reason)

    def _start_batch(self, next_batch, reason):
        self.rt["counters"]["batches"] += 1
        self.rt["batch"] = {
            "step_id": next_batch["step_id"],
            "goal": next_batch["goal"],
            "instructions": next_batch["instructions"],
            "acceptance_criteria": list(next_batch.get("acceptance_criteria") or []),
            "number": self.rt["counters"]["batches"],
            "started_at": self.clock(),
            "repairs": 0, "reviews": 0,            # cap counters (reset by approve)
            "worker_calls": 0, "review_calls": 0,  # call-id counters (never reset)
            "head_sha": None, "diff_hashes": [], "evidence_refresh": False,
            "repair": None, "worker": None, "evidence": None, "reviewer": None,
        }
        self.rt["used_step_ids"].append(next_batch["step_id"])
        self._transition("WORKING", f"brain NEXT {next_batch['step_id']}: {reason}", step_id=next_batch["step_id"])

    def _close_batch(self, outcome, detail, commit=None, paths=None):
        batch = self.rt["batch"]
        reviewer = batch.get("reviewer") or {}
        self.rt["history"].append({
            "step_id": batch["step_id"], "goal": batch["goal"], "outcome": outcome, "detail": detail,
            "repairs": batch["repairs"], "reviews": batch["reviews"],
            "verdict": reviewer.get("verdict"), "commit": commit, "paths": paths or [],
            "worker_run": batch.get("worker_run"),
            "closed_at": self.clock(),
        })
        self.rt["batch"] = None

    def _do_working(self):
        batch = self.rt.get("batch")
        if not batch:
            self._transition("ERROR", "WORKING without an active batch", last_error="no batch")
            return
        cfg = self.rt["config"]
        # A clean tree is required only before the very first Worker run of the
        # batch; a restart mid-run or a repair legitimately finds it dirty.
        fresh = batch["worker_calls"] == 0 and not self.rt.get("inflight")
        if fresh and self.job["mode"] == "write" and not self._branch_freshness_check("first Worker write"):
            return
        pre = gitsafe.preflight(cfg["repo_path"], cfg["origin"], self.job["branch"], require_clean=fresh)
        if not pre.ok:
            detail = "; ".join(f"{c.name}: {c.detail}" for c in pre.failures)
            self._needs_human(f"Git preflight failed: {detail}. Fix the checkout, then approve or abort.", "WORKING")
            return
        if batch["head_sha"] is None:
            batch["head_sha"] = pre.head_sha
        else:
            guard = gitsafe.check_head(cfg["repo_path"], batch["head_sha"])
            if not guard.ok:
                self._needs_human(f"{guard.detail}. Approve to continue on the new HEAD, or abort.", "WORKING")
                return
        writable = self.job["mode"] == "write" and not self.dry_run
        system_prompt, prompt = self._worker_prompt_parts(batch)
        system_file = self._stable_prompt_file("worker", batch["step_id"], system_prompt)
        payload = self._invoke(
            "worker", prompt, cwd=cfg["repo_path"],
            permission_mode=WRITE_PERMISSION if writable else READ_PERMISSION,
            system_prompt_file=system_file,
            check=lambda p: None if p["step_id"] == batch["step_id"]
            else f"worker reported step_id {p['step_id']!r}, expected {batch['step_id']!r}",
        )
        if payload is None:
            return
        batch["worker_run"] = dict(self.rt.get("last_call_telemetry") or {})
        self._consume("worker")
        batch["worker"] = payload
        self._transition("EVIDENCE", f"worker {payload['status']}: {payload['summary'][:200]}")

    def _do_evidence(self):
        batch = self.rt["batch"]
        cfg = self.rt["config"]
        commands = [] if self.dry_run else cfg["test_commands"]
        ev = evidence_mod.collect_evidence(cfg["repo_path"], test_commands=commands, test_timeout=cfg["test_timeout"])
        name = f"{batch['step_id']}-evidence-{batch['worker_calls']}.json"
        save_json(self.job_dir / EVIDENCE_DIR / name, ev.to_dict())
        digest = _diff_hash(ev.status_porcelain, ev.diff)
        batch["evidence"] = {
            "file": name, "head_sha": ev.head_sha, "branch": ev.branch,
            "changed_paths": [c.path for c in ev.changed_paths],
            "tests_summary": ev.tests_summary, "tests_passed": ev.tests_passed,
            "tests": [{"argv": t.argv, "status": t.status, "exit_code": t.exit_code} for t in ev.tests],
            "diff_hash": digest, "diff_truncated": ev.diff_truncated, "git_errors": ev.git_errors,
            "dry_run_skipped_tests": self.dry_run and bool(cfg["test_commands"]),
        }
        if ev.tests_summary == evidence_mod.HARNESS_ERROR:
            batch["evidence_refresh"] = True
            self._needs_human(
                f"Evidence harness error for {batch['step_id']}: at least one test command could not run or timed out. "
                "Fix the test command/timeout with set-config, then approve to rerun evidence, or abort.",
                "EVIDENCE", last_error="evidence harness error",
            )
            return
        repeated = bool(batch["diff_hashes"]) and digest == batch["diff_hashes"][-1]
        refresh = bool(batch.get("evidence_refresh"))
        batch["diff_hashes"].append(digest)
        batch["evidence_refresh"] = False
        if repeated and not refresh:
            self._needs_human(f"Repair of {batch['step_id']} produced an identical diff (hash {digest[:12]}); "
                              "the Worker is not converging. Approve to let the Brain decide, or abort.", "PLANNING")
            return
        self._transition("REVIEWING", f"evidence collected: {len(ev.changed_paths)} changed path(s), tests {ev.tests_summary}")

    def _do_reviewing(self):
        batch = self.rt["batch"]
        if not batch or not batch.get("evidence"):
            self._transition("ERROR", "REVIEWING without evidence", last_error="no evidence")
            return
        if batch["evidence"].get("tests_summary") == evidence_mod.HARNESS_ERROR:
            self._needs_human(
                f"Reviewer blocked for {batch['step_id']}: evidence harness is not trustworthy. "
                "Fix it with set-config and rerun evidence first.",
                "EVIDENCE", last_error="evidence harness error",
            )
            return
        caps = self.rt["config"]["caps"]
        if batch["reviews"] >= caps["max_reviews_per_batch"]:
            self._needs_human(f"Review cap reached for {batch['step_id']} ({caps['max_reviews_per_batch']}). "
                              "Approve to allow more reviews, or abort.", "PLANNING", cap="max_reviews_per_batch")
            return
        system_prompt, prompt = self._reviewer_prompt_parts(batch)
        system_file = self._stable_prompt_file("reviewer", batch["step_id"], system_prompt)
        payload = self._invoke(
            "reviewer", prompt, cwd=self.rt["config"]["repo_path"],
            permission_mode=READ_PERMISSION, system_prompt_file=system_file,
            check=lambda p: None if p["step_id"] == batch["step_id"]
            else f"reviewer reported step_id {p['step_id']!r}, expected {batch['step_id']!r}",
        )
        if payload is None:
            return
        self._consume("reviewer")
        batch["reviews"] += 1
        # validate_reviewer already degraded PASS on scope/tests/BLOCKER claims;
        # the orchestrator's own test evidence is the last word.
        if payload["verdict"] == "PASS" and batch["evidence"]["tests_summary"] == evidence_mod.FAIL:
            payload["verdict"] = "FAIL"
            payload["verdict_degraded"] = True
            payload["degrade_reasons"].append("orchestrator test evidence is FAIL")
        batch["reviewer"] = payload
        verdict = payload["verdict"]
        if verdict == "PASS":
            self._transition("COMMITTING", f"reviewer PASS: {payload['summary'][:200]}")
        elif verdict == "FAIL":
            why = "; ".join(payload["degrade_reasons"]) or payload["summary"][:200]
            if batch["reviews"] >= 2:
                self._hermes_advice("repeat_review_fail", why)
            self._transition("PLANNING", f"reviewer FAIL: {why}")
        else:
            self._needs_human(payload["human_question"], "PLANNING")

    def _do_committing(self):
        batch = self.rt["batch"]
        reviewer = (batch or {}).get("reviewer") or {}
        if reviewer.get("verdict") != "PASS":
            self._transition("ERROR", "COMMITTING without a reviewer PASS", last_error="without a reviewer PASS")
            return
        cfg = self.rt["config"]
        repo = cfg["repo_path"]
        if self.dry_run:
            self._close_batch("dry-run (not committed)", "dry run never stages or commits")
            self._transition("PLANNING", f"dry-run: commit of {batch['step_id']} skipped", step_id=None)
            return
        plan = gitsafe.staging_plan(repo, cfg["allowed_paths"], cfg["forbidden_paths"])
        if not plan.ok:
            self._needs_human(f"Cannot commit {batch['step_id']}: {plan.detail}. Fix the tree, then approve or abort.",
                              "COMMITTING")
            return
        if not plan.paths:
            self._close_batch("reviewed, nothing to commit", reviewer["summary"])
            self._transition("PLANNING", f"{batch['step_id']} passed review; no changes to commit", step_id=None)
            return
        if self.job["mode"] != "write":
            self._needs_human(f"Read-mode job left {len(plan.paths)} changed path(s): {', '.join(plan.paths[:10])}. "
                              "Clean the tree, then approve or abort.", "COMMITTING")
            return
        guard = gitsafe.check_head(repo, batch["head_sha"])
        branch = gitsafe.current_branch(repo)
        if not guard.ok or branch != self.job["branch"] or branch in gitsafe.PROTECTED_BRANCHES:
            self._needs_human(f"Refusing to commit {batch['step_id']}: {guard.detail}; branch is {branch!r}. "
                              "Restore the checkout, then approve or abort.", "COMMITTING")
            return
        message = (f"{batch['step_id']}: {batch['goal'].strip().splitlines()[0][:72]}\n\n"
                   f"{batch['worker']['summary'].strip()}\n\nReviewed: {reviewer['summary'].strip()}\n\n"
                   f"Orchestrator job {self.job['job_id']}")
        add = gitsafe.run_git(repo, ["add", "--", *plan.paths])
        commit = gitsafe.run_git(repo, ["commit", "-q", "-m", message]) if add.ok else add
        if not (add.ok and commit.ok):
            failed = add if not add.ok else commit
            self._needs_human(f"git {failed.argv[1]} failed: {failed.error or failed.stderr.strip()[:500]}. "
                              "Fix the repository, then approve or abort.", "COMMITTING")
            return
        sha = gitsafe.head_sha(repo)
        self._close_batch("committed", reviewer["summary"], commit=sha, paths=plan.paths)
        self._transition("PLANNING", f"committed {batch['step_id']} as {(sha or '')[:12]} ({len(plan.paths)} path(s))",
                         step_id=None)

    # -- prompts ----------------------------------------------------------

    def _cwd(self):
        repo = Path(self.rt["config"]["repo_path"])
        return repo if repo.is_dir() else self.job_dir

    def _stable_prompt_file(self, role, key, text):
        directory = self.job_dir / SYSTEM_PROMPTS_DIR
        directory.mkdir(exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        path = directory / f"{role}-{key}-{digest}.md"
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        return str(path.resolve())

    def _load_evidence(self, batch):
        info = batch.get("evidence")
        if not info:
            return None
        try:
            return load_json(self.job_dir / EVIDENCE_DIR / info["file"])
        except StateError:
            return None

    def _evidence_for_prompt(self, batch):
        raw = self._load_evidence(batch)
        limit = self.rt["config"]["prompt_output_chars"]
        if raw is None:
            return {"summary": batch.get("evidence"), "tests": []}, ""
        summary = {
            "branch": raw["branch"], "head_sha": raw["head_sha"], "changed_paths": raw["changed_paths"],
            "status_porcelain": raw["status_porcelain"], "tests_summary": raw["tests_summary"],
            "diff_truncated": raw["diff_truncated"], "git_errors": raw["git_errors"],
            "tests": [{"argv": t["argv"], "status": t["status"], "exit_code": t["exit_code"],
                       "stdout_tail": _tail(t["stdout"], limit), "stderr_tail": _tail(t["stderr"], limit)}
                      for t in raw["tests"]],
        }
        return summary, _cut(raw["diff"], self.rt["config"]["prompt_diff_chars"])

    def _input_files_text(self):
        limit = self.rt["config"]["input_file_chars"]
        parts = []
        for name in self.job.get("input_files") or []:
            path = Path(name) if Path(name).is_absolute() else ORCH_ROOT / name
            try:
                body = _cut(path.read_text(encoding="utf-8", errors="replace"), limit)
            except OSError as exc:
                body = f"(cannot read: {exc.__class__.__name__})"
            parts.append(f"--- {name} ---\n{body}\n")
        return "\n".join(parts) or "(no input files)"

    def _brain_prompt_parts(self):
        batch = self.rt.get("batch")
        evidence, diff = self._evidence_for_prompt(batch) if batch else ({}, "")
        context = {
            "job": {k: self.job[k] for k in ("job_id", "repo", "branch", "mode")},
            "phase": self.state["phase"], "attempt": self.state["attempt"], "last_error": self.state["last_error"],
            "counters": self.rt["counters"], "caps": self.rt["config"]["caps"], "cap_hit": self.rt.get("cap_hit"),
            "history": self.rt["history"][-20:], "used_step_ids": self.rt["used_step_ids"],
            "human": self.rt.get("human"),
            "current_batch": None if not batch else {
                k: batch[k] for k in ("step_id", "goal", "instructions", "acceptance_criteria", "number",
                                       "repairs", "reviews", "repair", "worker", "reviewer")
            },
            "evidence": evidence,
        }
        values = dict(
            goal=self.job["goal"], input_files=self._input_files_text(),
            context_json=json.dumps(context, indent=2, sort_keys=True), diff=diff or "(no diff)",
        )
        return _render_split("brain.md", "## Current state (machine-generated, authoritative)",
                             "## Decision rules", **values)

    def _worker_prompt_parts(self, batch):
        cfg = self.rt["config"]
        repair = batch.get("repair")
        reviewer = batch.get("reviewer")
        feedback = "(first run of this batch)"
        if repair:
            feedback = json.dumps({"repair_goal": repair["goal"], "repair_instructions": repair["instructions"],
                                   "repair_acceptance_criteria": repair.get("acceptance_criteria") or [],
                                   "previous_reviewer": reviewer}, indent=2)
        values = dict(
            step_id=batch["step_id"], job_goal=self.job["goal"], goal=batch["goal"],
            instructions=batch["instructions"],
            acceptance_criteria="\n".join(f"- {c}" for c in batch["acceptance_criteria"]) or "- (none given)",
            repo_path=cfg["repo_path"], branch=self.job["branch"], mode=self.job["mode"],
            allowed_paths=json.dumps(cfg["allowed_paths"]), forbidden_paths=json.dumps(cfg["forbidden_paths"]),
            test_commands=json.dumps(cfg["test_commands"]), repair_feedback=feedback,
        )
        return _render_split("worker.md", "## Repair feedback (present only when this batch is being repaired)",
                             "## Environment and hard limits", **values)

    def _reviewer_prompt_parts(self, batch):
        cfg = self.rt["config"]
        evidence, diff = self._evidence_for_prompt(batch)
        values = dict(
            step_id=batch["step_id"], job_goal=self.job["goal"], goal=batch["goal"],
            instructions=batch["instructions"],
            acceptance_criteria="\n".join(f"- {c}" for c in batch["acceptance_criteria"]) or "- (none given)",
            allowed_paths=json.dumps(cfg["allowed_paths"]), forbidden_paths=json.dumps(cfg["forbidden_paths"]),
            worker_json=json.dumps(batch["worker"], indent=2), evidence_json=json.dumps(evidence, indent=2),
            diff=diff or "(empty diff: no changes in the working tree)", mode=self.job["mode"],
        )
        return _render_split("reviewer.md", "## What the Worker claims", "## How to review", **values)

    # -- status -----------------------------------------------------------

    def status(self):
        """Compact, JSON-serialisable view of the job for the CLI."""
        batch = self.rt.get("batch") or {}
        return {
            "job_id": self.job["job_id"], "phase": self.state["phase"], "step_id": self.state["step_id"],
            "attempt": self.state["attempt"], "updated_at": self.state["updated_at"],
            "wait_until": self.state.get("wait_until"), "retry_phase": self.rt.get("retry_phase"),
            "human_question": self.state.get("human_question"), "last_error": self.state["last_error"],
            "counters": self.rt["counters"], "caps": self.rt["config"]["caps"], "cap_hit": self.rt.get("cap_hit"),
            "totals": self.rt.get("totals", _empty_totals()), "inflight": self.rt.get("inflight"),
            "batch": None if not batch else {
                "step_id": batch["step_id"], "goal": batch["goal"], "repairs": batch["repairs"],
                "reviews": batch["reviews"], "verdict": (batch.get("reviewer") or {}).get("verdict"),
            },
            "history": self.rt["history"],
        }
