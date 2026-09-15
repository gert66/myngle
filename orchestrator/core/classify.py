"""Classify a ClaudeResult into a small set of outcome classes, stdlib only.

Classes: SUCCESS, RATE_LIMIT, AUTH, TRANSIENT, TIMEOUT, INVALID_OUTPUT,
BUDGET, CONFIG, TASK_ERROR. Each class carries recommended retry metadata
(retryable flag + default backoff seconds) so the orchestrator loop does not
have to know the details of Claude's failure modes.

Evidence used, in order of trust:
  1. the runner's timed_out flag,
  2. explicit fields of Claude's top-level JSON (is_error, subtype),
  3. exit code,
  4. case-insensitive text patterns over stderr/stdout (failed runs only, so
     a successful answer that merely mentions "429" is never misclassified).

Rules of thumb:
  * A nonzero exit that matches nothing is TRANSIENT (retryable), never SUCCESS.
  * Claude reporting its own failure in well-formed JSON (is_error/subtype)
    that is not an infrastructure problem is TASK_ERROR: retrying the same
    prompt is unlikely to help.
  * A structured call (--output-format json) that "succeeds" without a
    parseable JSON object, or with --json-schema but without a
    structured_output object, is INVALID_OUTPUT.

The Classification never copies raw stdout/stderr: `reason` is a short fixed
description plus the pattern that matched, so it is safe to log or forward.
"""

import re
from dataclasses import dataclass

SUCCESS = "SUCCESS"
RATE_LIMIT = "RATE_LIMIT"
AUTH = "AUTH"
TRANSIENT = "TRANSIENT"
TIMEOUT = "TIMEOUT"
INVALID_OUTPUT = "INVALID_OUTPUT"
BUDGET = "BUDGET"
CONFIG = "CONFIG"
TASK_ERROR = "TASK_ERROR"

CLASSES = (SUCCESS, RATE_LIMIT, AUTH, TRANSIENT, TIMEOUT, INVALID_OUTPUT, BUDGET, CONFIG, TASK_ERROR)

# Recommended retry policy per class. Backoff is a default; the orchestrator
# may scale it per attempt. Non-retryable classes have backoff 0.
BACKOFF_SECONDS = {
    SUCCESS: 0,
    RATE_LIMIT: 300,
    AUTH: 0,
    TRANSIENT: 30,
    TIMEOUT: 30,
    INVALID_OUTPUT: 0,
    BUDGET: 0,
    CONFIG: 0,
    TASK_ERROR: 0,
}
RETRYABLE = frozenset((RATE_LIMIT, TRANSIENT, TIMEOUT, INVALID_OUTPUT))

# Key under which the Claude CLI places the --json-schema-validated payload
# inside its --output-format json result object.
STRUCTURED_OUTPUT_KEY = "structured_output"

# Text patterns, all matched case-insensitively. Checked in this order; the
# first class with any match wins.
RATE_LIMIT_PATTERNS = (
    r"rate[\s_-]*limit",
    r"usage[\s_-]*limit",
    r"hit your limit",
    r"\b429\b",
    r"overloaded",
    r"\b529\b",
    r"resets at",
    r"too many requests",
)
AUTH_PATTERNS = (
    r"\b401\b",
    r"unauthori[sz]ed",
    r"not logged in",
    r"authentication[\s_-]*(required|error|failed)",
    r"invalid api key",
    r"invalid x-api-key",
    r"not authenticated",
    r"please (run /login|log ?in)",
)
BUDGET_PATTERNS = (
    r"max[\s_-]*budget",
    r"\bbudget\b",
    r"cost[\s_-]*limit",
    r"spend(ing)?[\s_-]*limit",
)
CONFIG_PATTERNS = (
    r"--json-schema[^\n]*not a valid json schema",
    r"unknown (option|argument)",
    r"invalid (value|argument)[^\n]*--(model|effort|permission-mode|json-schema|fallback-model|max-budget-usd)",
    r"(^|\n)usage:\s*claude\b",
)

_TEXT_CLASSES = (
    (CONFIG, CONFIG_PATTERNS),
    (RATE_LIMIT, RATE_LIMIT_PATTERNS),
    (AUTH, AUTH_PATTERNS),
    (BUDGET, BUDGET_PATTERNS),
)
_COMPILED = [
    (cls, [re.compile(p, re.IGNORECASE) for p in patterns]) for cls, patterns in _TEXT_CLASSES
]

# Claude CLI result subtypes that name the failure explicitly.
_SUBTYPE_CLASSES = {
    "error_max_budget_usd": BUDGET,
    "error_max_turns": TASK_ERROR,
    "error_during_execution": TASK_ERROR,
}


@dataclass
class Classification:
    """Outcome class plus retry advice. Safe to serialise (see to_dict)."""

    category: str
    retryable: bool
    backoff_seconds: int
    reason: str
    matched_pattern: str = None  # regex that triggered a text-based class
    payload: object = None       # structured_output (or top-level JSON) on SUCCESS

    def to_dict(self):
        return {
            "category": self.category,
            "retryable": self.retryable,
            "backoff_seconds": self.backoff_seconds,
            "reason": self.reason,
            "matched_pattern": self.matched_pattern,
            "payload": self.payload,
        }


def retry_metadata(category):
    """Return (retryable, backoff_seconds) for a class name."""
    if category not in BACKOFF_SECONDS:
        raise ValueError(f"unknown class: {category!r}")
    return category in RETRYABLE, BACKOFF_SECONDS[category]


def _make(category, reason, matched_pattern=None, payload=None):
    retryable, backoff = retry_metadata(category)
    return Classification(
        category=category,
        retryable=retryable,
        backoff_seconds=backoff,
        reason=reason,
        matched_pattern=matched_pattern,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------

def _get(result, key, default=None):
    """Read a field from a ClaudeResult or an equivalent dict (to_dict output)."""
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def match_text(text):
    """Return (class, pattern) for the first infrastructure pattern found in text, else None."""
    if not isinstance(text, str) or not text:
        return None
    for cls, regexes in _COMPILED:
        for regex in regexes:
            if regex.search(text):
                return cls, regex.pattern
    return None


def _json_reports_error(parsed):
    """True when Claude's top-level JSON says the run failed."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("is_error") is True:
        return True
    subtype = parsed.get("subtype")
    return isinstance(subtype, str) and subtype.startswith("error")


def _structured_payload(result, parsed):
    """Return (ok, payload) for a structured call that otherwise succeeded."""
    if not isinstance(parsed, dict):
        return False, None
    if not _get(result, "schema_requested", False):
        return True, parsed
    payload = parsed.get(STRUCTURED_OUTPUT_KEY)
    if not isinstance(payload, dict):
        return False, None
    return True, payload


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(result):
    """Classify a ClaudeResult (or its to_dict() form). Never raises for a bad run."""
    timed_out = bool(_get(result, "timed_out", False))
    exit_code = _get(result, "exit_code", 0)
    stdout = _get(result, "stdout", "") or ""
    stderr = _get(result, "stderr", "") or ""
    parsed = _get(result, "parsed_json")
    structured = bool(_get(result, "structured", False))
    runner_error = _get(result, "error")

    if timed_out:
        return _make(TIMEOUT, "runner timed out and stopped the process")

    failed = exit_code != 0 or _json_reports_error(parsed)

    if not failed:
        if not structured:
            return _make(SUCCESS, "exit 0 (unstructured call)", payload=None)
        ok, payload = _structured_payload(result, parsed)
        if not ok:
            if parsed is None:
                return _make(INVALID_OUTPUT, "exit 0 but stdout is not a JSON document")
            if not isinstance(parsed, dict):
                return _make(INVALID_OUTPUT, "exit 0 but top-level JSON is not an object")
            return _make(
                INVALID_OUTPUT,
                f"exit 0 but {STRUCTURED_OUTPUT_KEY} is missing or not an object",
            )
        return _make(SUCCESS, "exit 0 with valid structured output", payload=payload)

    # Failed run: explicit JSON subtype first, then text patterns, then defaults.
    subtype = parsed.get("subtype") if isinstance(parsed, dict) else None
    if isinstance(subtype, str) and subtype in _SUBTYPE_CLASSES:
        cls = _SUBTYPE_CLASSES[subtype]
        if cls == BUDGET:
            return _make(BUDGET, f"claude reported subtype {subtype}")
        # Task-level subtypes still lose to infrastructure text (e.g. an
        # execution error whose message is a 429).
        hit = match_text(stderr) or match_text(stdout)
        if hit:
            return _make(hit[0], "pattern matched in failed run output", matched_pattern=hit[1])
        return _make(TASK_ERROR, f"claude reported subtype {subtype}")

    hit = match_text(stderr) or match_text(stdout)
    if hit:
        return _make(hit[0], "pattern matched in failed run output", matched_pattern=hit[1])

    if _json_reports_error(parsed):
        return _make(TASK_ERROR, "claude reported is_error in its JSON result")

    if runner_error:
        return _make(TRANSIENT, f"runner error: {runner_error}")
    return _make(TRANSIENT, f"exit {exit_code} with no recognised pattern")
