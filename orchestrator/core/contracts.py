"""Strict, stdlib-only validation of orchestrator contracts.

Contracts: Brain, Worker, Reviewer, Job, State.

Each validator takes a plain dict (as parsed from JSON), checks required keys,
basic types, enums and cross-field rules, rejects unknown top-level keys, and
returns a validated deep copy. Nothing is coerced or silently dropped.

The JSON schema files in schemas/ document the same shapes for humans and
external tooling. They are loaded for reference only (see load_schema); this
module does not implement JSON Schema.
"""

import copy
import json
import re
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
CONTRACT_NAMES = ("brain", "worker", "reviewer", "job", "state")

PHASES = (
    "QUEUED", "PLANNING", "WORKING", "EVIDENCE", "REVIEWING",
    "COMMITTING", "WAITING", "NEEDS_HUMAN", "DONE", "ERROR",
)
BRAIN_DECISIONS = ("NEXT", "REPAIR", "REVIEW_AGAIN", "WAIT", "NEEDS_HUMAN", "DONE", "ERROR")
WORKER_STATUSES = ("COMPLETED", "PARTIAL", "BLOCKED", "FAILED")
REVIEWER_VERDICTS = ("PASS", "FAIL", "NEEDS_HUMAN")
FINDING_SEVERITIES = ("BLOCKER", "MAJOR", "MINOR", "INFO")
JOB_MODES = ("read", "write")  # lowercase to match bin/run_claude_job.sh

WAIT_SECONDS_MIN = 60
WAIT_SECONDS_MAX = 3600

# Safe for use in file names and log lines.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# ISO 8601 UTC timestamp, e.g. 2026-09-13T14:07:00Z (an explicit offset is also accepted).
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class ContractError(ValueError):
    """Raised when a dictionary does not satisfy its contract."""


# ---------------------------------------------------------------------------
# Field tables: key -> (allowed python type or tuple of types, required)
# Keep these in sync with schemas/*.schema.json (tests check that).
# ---------------------------------------------------------------------------

NULL = type(None)

BRAIN_FIELDS = {
    "schema_version": (int, True),
    "decision": (str, True),
    "reason": (str, True),
    "next_batch": ((dict, NULL), False),
    "human_question": ((str, NULL), False),
    "wait_seconds": ((int, NULL), False),
}

BATCH_FIELDS = {
    "step_id": (str, True),
    "goal": (str, True),
    "instructions": (str, True),
    "acceptance_criteria": (list, False),
}

WORKER_FIELDS = {
    "schema_version": (int, True),
    "step_id": (str, True),
    "status": (str, True),
    "summary": (str, True),
    "files_changed": (list, False),
    "tests_run": (list, False),
    "tests_passed": ((bool, NULL), False),
    "blockers": (list, False),
}

REVIEWER_FIELDS = {
    "schema_version": (int, True),
    "step_id": (str, True),
    "verdict": (str, True),
    "scope_ok": (bool, True),
    "tests_ok": (bool, True),
    "summary": (str, True),
    "findings": (list, True),
    "human_question": ((str, NULL), False),
    # Set by validate_reviewer, never trusted from input.
    "verdict_degraded": (bool, False),
    "degrade_reasons": (list, False),
}

FINDING_FIELDS = {
    "severity": (str, True),
    "message": (str, True),
    "file": ((str, NULL), False),
}

JOB_FIELDS = {
    "schema_version": (int, True),
    "job_id": (str, True),
    "repo": (str, True),
    "branch": (str, True),
    "mode": (str, True),
    "goal": (str, True),
    "created_at": (str, True),
    "input_files": (list, False),
    "max_steps": (int, False),
    "max_repairs_per_step": (int, False),
}

STATE_FIELDS = {
    "schema_version": (int, True),
    "job_id": (str, True),
    "phase": (str, True),
    "step_id": ((str, NULL), True),
    "attempt": (int, True),
    "created_at": (str, True),
    "updated_at": (str, True),
    "last_error": ((str, NULL), True),
    "wait_until": ((str, NULL), False),
    "human_question": ((str, NULL), False),
    # Machine bookkeeping (core/machine.py): kept inside the canonical state so
    # that counters, inflight step identity and the phase change atomically.
    "runtime": ((dict, NULL), False),
}

CONTRACT_FIELDS = {
    "brain": BRAIN_FIELDS,
    "worker": WORKER_FIELDS,
    "reviewer": REVIEWER_FIELDS,
    "job": JOB_FIELDS,
    "state": STATE_FIELDS,
}


# ---------------------------------------------------------------------------
# Generic checks
# ---------------------------------------------------------------------------

def _type_names(types):
    types = types if isinstance(types, tuple) else (types,)
    return "|".join("null" if t is NULL else t.__name__ for t in types)


def _is_type(value, types):
    types = types if isinstance(types, tuple) else (types,)
    # bool is a subclass of int in Python; only accept it where bool is listed.
    if isinstance(value, bool) and bool not in types:
        return False
    return isinstance(value, types)


def _validate_object(obj, fields, name):
    """Check obj is a dict with only known keys, all required keys, and basic types."""
    if not isinstance(obj, dict):
        raise ContractError(f"{name}: expected object, got {type(obj).__name__}")
    unknown = sorted(set(obj) - set(fields))
    if unknown:
        raise ContractError(f"{name}: unknown keys {unknown}")
    missing = sorted(k for k, (_, required) in fields.items() if required and k not in obj)
    if missing:
        raise ContractError(f"{name}: missing required keys {missing}")
    for key, value in obj.items():
        types = fields[key][0]
        if not _is_type(value, types):
            raise ContractError(
                f"{name}.{key}: expected {_type_names(types)}, got {type(value).__name__}"
            )


def _check_schema_version(obj, name):
    if obj["schema_version"] != SCHEMA_VERSION:
        raise ContractError(
            f"{name}.schema_version: expected {SCHEMA_VERSION}, got {obj['schema_version']}"
        )


def _check_enum(obj, key, allowed, name):
    if obj[key] not in allowed:
        raise ContractError(f"{name}.{key}: {obj[key]!r} not in {list(allowed)}")


def _check_nonempty(obj, key, name):
    if not obj[key].strip():
        raise ContractError(f"{name}.{key}: must be a non-empty string")


def _check_str_list(obj, key, name):
    if key not in obj:
        return
    for i, item in enumerate(obj[key]):
        if not isinstance(item, str):
            raise ContractError(f"{name}.{key}[{i}]: expected str, got {type(item).__name__}")


def _check_id(obj, key, name):
    if not ID_RE.match(obj[key]):
        raise ContractError(f"{name}.{key}: {obj[key]!r} is not a safe identifier")


def _check_timestamp(obj, key, name):
    value = obj.get(key)
    if value is not None and not TIMESTAMP_RE.match(value):
        raise ContractError(f"{name}.{key}: {value!r} is not an ISO 8601 timestamp")


def _check_min(obj, key, minimum, name):
    if key in obj and obj[key] < minimum:
        raise ContractError(f"{name}.{key}: must be >= {minimum}, got {obj[key]}")


# ---------------------------------------------------------------------------
# Contract validators
# ---------------------------------------------------------------------------

def validate_brain(obj):
    """Validate a Brain decision. Returns a deep copy."""
    name = "brain"
    _validate_object(obj, BRAIN_FIELDS, name)
    _check_schema_version(obj, name)
    _check_enum(obj, "decision", BRAIN_DECISIONS, name)
    _check_nonempty(obj, "reason", name)
    decision = obj["decision"]

    batch = obj.get("next_batch")
    if decision in ("NEXT", "REPAIR"):
        if batch is None:
            raise ContractError(f"{name}: next_batch is required for decision {decision}")
        _validate_object(batch, BATCH_FIELDS, f"{name}.next_batch")
        _check_id(batch, "step_id", f"{name}.next_batch")
        _check_nonempty(batch, "goal", f"{name}.next_batch")
        _check_nonempty(batch, "instructions", f"{name}.next_batch")
        _check_str_list(batch, "acceptance_criteria", f"{name}.next_batch")
    elif batch is not None:
        raise ContractError(f"{name}: next_batch must be null for decision {decision}")

    question = obj.get("human_question")
    if decision == "NEEDS_HUMAN":
        if question is None or not question.strip():
            raise ContractError(f"{name}: human_question is required for decision NEEDS_HUMAN")
    elif question is not None:
        raise ContractError(f"{name}: human_question must be null for decision {decision}")

    wait = obj.get("wait_seconds")
    if decision == "WAIT":
        if wait is None or not (WAIT_SECONDS_MIN <= wait <= WAIT_SECONDS_MAX):
            raise ContractError(
                f"{name}: wait_seconds must be an integer in "
                f"{WAIT_SECONDS_MIN}..{WAIT_SECONDS_MAX} for decision WAIT, got {wait!r}"
            )
    elif wait is not None:
        raise ContractError(f"{name}: wait_seconds must be null for decision {decision}")

    return copy.deepcopy(obj)


def validate_worker(obj):
    """Validate a Worker result. Returns a deep copy."""
    name = "worker"
    _validate_object(obj, WORKER_FIELDS, name)
    _check_schema_version(obj, name)
    _check_id(obj, "step_id", name)
    _check_enum(obj, "status", WORKER_STATUSES, name)
    _check_nonempty(obj, "summary", name)
    for key in ("files_changed", "tests_run", "blockers"):
        _check_str_list(obj, key, name)
    return copy.deepcopy(obj)


def validate_reviewer(obj):
    """Validate a Reviewer verdict and apply the consistency guard.

    A PASS verdict is degraded to FAIL when scope_ok is false, tests_ok is
    false, or any finding has severity BLOCKER. The returned copy always
    carries `verdict_degraded` (bool) and `degrade_reasons` (list of str);
    any values for those keys in the input are overwritten.
    """
    name = "reviewer"
    _validate_object(obj, REVIEWER_FIELDS, name)
    _check_schema_version(obj, name)
    _check_id(obj, "step_id", name)
    _check_enum(obj, "verdict", REVIEWER_VERDICTS, name)
    _check_nonempty(obj, "summary", name)
    _check_str_list(obj, "degrade_reasons", name)
    for i, finding in enumerate(obj["findings"]):
        fname = f"{name}.findings[{i}]"
        _validate_object(finding, FINDING_FIELDS, fname)
        _check_enum(finding, "severity", FINDING_SEVERITIES, fname)
        _check_nonempty(finding, "message", fname)

    result = copy.deepcopy(obj)
    reasons = []
    if result["verdict"] == "PASS":
        if not result["scope_ok"]:
            reasons.append("scope_ok is false")
        if not result["tests_ok"]:
            reasons.append("tests_ok is false")
        blockers = sum(1 for f in result["findings"] if f["severity"] == "BLOCKER")
        if blockers:
            reasons.append(f"{blockers} BLOCKER finding(s)")
    if reasons:
        result["verdict"] = "FAIL"
    result["verdict_degraded"] = bool(reasons)
    result["degrade_reasons"] = reasons

    question = result.get("human_question")
    if result["verdict"] == "NEEDS_HUMAN" and (question is None or not question.strip()):
        raise ContractError(f"{name}: human_question is required for verdict NEEDS_HUMAN")
    return result


def validate_job(obj):
    """Validate a Job definition. Returns a deep copy."""
    name = "job"
    _validate_object(obj, JOB_FIELDS, name)
    _check_schema_version(obj, name)
    _check_id(obj, "job_id", name)
    _check_nonempty(obj, "repo", name)
    _check_nonempty(obj, "branch", name)
    _check_enum(obj, "mode", JOB_MODES, name)
    _check_nonempty(obj, "goal", name)
    _check_timestamp(obj, "created_at", name)
    _check_str_list(obj, "input_files", name)
    _check_min(obj, "max_steps", 1, name)
    _check_min(obj, "max_repairs_per_step", 0, name)
    return copy.deepcopy(obj)


def validate_state(obj):
    """Validate persistent job State. Returns a deep copy."""
    name = "state"
    _validate_object(obj, STATE_FIELDS, name)
    _check_schema_version(obj, name)
    _check_id(obj, "job_id", name)
    _check_enum(obj, "phase", PHASES, name)
    if obj["step_id"] is not None:
        _check_id(obj, "step_id", name)
    _check_min(obj, "attempt", 0, name)
    for key in ("created_at", "updated_at", "wait_until"):
        _check_timestamp(obj, key, name)
    return copy.deepcopy(obj)


VALIDATORS = {
    "brain": validate_brain,
    "worker": validate_worker,
    "reviewer": validate_reviewer,
    "job": validate_job,
    "state": validate_state,
}


def validate(contract, obj):
    """Validate obj against the named contract ("brain", "worker", ...)."""
    if contract not in VALIDATORS:
        raise ContractError(f"unknown contract {contract!r}; expected one of {list(VALIDATORS)}")
    return VALIDATORS[contract](obj)


def load_schema(contract):
    """Load schemas/<contract>.schema.json for documentation/reference purposes."""
    if contract not in CONTRACT_NAMES:
        raise ContractError(f"unknown contract {contract!r}; expected one of {list(CONTRACT_NAMES)}")
    path = SCHEMAS_DIR / f"{contract}.schema.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
