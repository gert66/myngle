"""Run the Claude CLI as a subprocess and capture the outcome, stdlib only.

The runner never uses a shell: argv is built explicitly (see build_argv) and
the prompt is delivered on stdin, so prompt size is not limited by the
kernel's per-argument limit and prompt text never appears in process lists.

Structured calls use `--print --output-format json` (plus `--json-schema` when
a schema is supplied) and the resulting top-level JSON object is parsed into
ClaudeResult.parsed_json. Timeouts terminate the whole process group, then
kill it if it does not exit within the grace period; a ClaudeResult with
timed_out=True is returned instead of raising.

Nothing here logs. ClaudeResult contains no environment values and no prompt
text (only its length), so it is safe to persist or forward to Slack.
"""

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CLAUDE_BIN = "/home/myngle/.npm-global/bin/claude"  # matches bin/run_claude_job.sh
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_KILL_GRACE_SECONDS = 10

EXIT_TIMEOUT = -1       # exit_code when the process was terminated/killed by us
EXIT_NOT_FOUND = 127    # binary missing (shell convention)
EXIT_NOT_EXEC = 126     # binary present but not executable

# Same as bin/run_claude_job.sh: the CLI itself decides what a mode means,
# but only these are accepted so a typo cannot silently become interactive.
PERMISSION_MODES = ("auto", "manual", "acceptEdits", "plan", "dontAsk", "bypassPermissions")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ClaudeRunnerError(ValueError):
    """Raised for invalid runner arguments (never for a failed Claude run)."""


@dataclass
class ClaudeResult:
    """Outcome of one Claude invocation. Safe to serialise (see to_dict)."""

    argv: list
    cwd: str
    exit_code: int            # process exit status, or EXIT_* sentinel
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    structured: bool          # called with --output-format json
    schema_requested: bool    # called with --json-schema
    parsed_json: object = None  # top-level JSON from stdout when stdout is valid JSON
    prompt_chars: int = 0
    error: str = field(default=None)  # runner-level problem (binary missing, ...)

    def to_dict(self):
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "structured": self.structured,
            "schema_requested": self.schema_requested,
            "parsed_json": self.parsed_json,
            "prompt_chars": self.prompt_chars,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def _format_usd(amount):
    text = format(float(amount), "f").rstrip("0").rstrip(".")
    return text or "0"


def _check_tools(tools, name):
    if tools is None:
        return []
    if isinstance(tools, str) or not all(isinstance(t, str) and t.strip() for t in tools):
        raise ClaudeRunnerError(f"{name}: expected a list of non-empty strings")
    return [t.strip() for t in tools]


def build_argv(
    claude_bin=DEFAULT_CLAUDE_BIN,
    *,
    structured=True,
    json_schema=None,
    session_id=None,
    append_system_prompt_file=None,
    permission_mode=None,
    allowed_tools=None,
    disallowed_tools=None,
    model=None,
    effort=None,
    fallback_model=None,
    max_budget_usd=None,
):
    """Build the explicit argv for a non-interactive Claude call.

    claude_bin may be a path string or a list (e.g. [python, fake_script]).
    json_schema may be a dict (serialised compactly) or a pre-serialised string.
    The prompt is NOT part of argv; run_claude sends it on stdin.
    """
    if isinstance(claude_bin, str):
        if not claude_bin.strip():
            raise ClaudeRunnerError("claude_bin: must be a non-empty path")
        argv = [claude_bin]
    else:
        argv = [str(part) for part in claude_bin]
        if not argv:
            raise ClaudeRunnerError("claude_bin: must not be empty")

    argv += ["--print", "--output-format", "json" if structured else "text"]

    if json_schema is not None:
        if not structured:
            raise ClaudeRunnerError("json_schema requires a structured call")
        if isinstance(json_schema, dict):
            wire_schema = dict(json_schema)
        elif isinstance(json_schema, str):
            try:
                wire_schema = json.loads(json_schema)
            except ValueError as exc:
                raise ClaudeRunnerError(f"json_schema: not valid JSON ({exc})") from None
            if not isinstance(wire_schema, dict):
                raise ClaudeRunnerError("json_schema: top level must be an object")
        else:
            raise ClaudeRunnerError("json_schema: expected dict or JSON string")
        # Claude Code validates --json-schema with its own validator. Current CLI
        # builds reject draft metadata and top-level combinators. Our canonical
        # schema stays untouched; local contracts.py enforces the omitted rules.
        wire_schema.pop("$schema", None)
        wire_schema.pop("allOf", None)
        wire_schema.pop("oneOf", None)
        wire_schema.pop("anyOf", None)
        schema_text = json.dumps(wire_schema, separators=(",", ":"), sort_keys=True)
        argv += ["--json-schema", schema_text]

    if append_system_prompt_file is not None:
        if not isinstance(append_system_prompt_file, (str, os.PathLike)) or not str(append_system_prompt_file).strip():
            raise ClaudeRunnerError("append_system_prompt_file: must be a non-empty path")
        argv += ["--append-system-prompt-file", str(append_system_prompt_file)]

    if session_id is not None:
        try:
            uuid.UUID(str(session_id))
        except (ValueError, AttributeError, TypeError):
            raise ClaudeRunnerError("session_id: must be a UUID") from None
        argv += ["--session-id", str(session_id)]

    if permission_mode is not None:
        if permission_mode not in PERMISSION_MODES:
            raise ClaudeRunnerError(
                f"permission_mode: {permission_mode!r} not in {list(PERMISSION_MODES)}"
            )
        argv += ["--permission-mode", permission_mode]

    # One flag per tool: the CLI's variadic options accumulate, and this
    # avoids any ambiguity about comma/space separated lists.
    for tool in _check_tools(allowed_tools, "allowed_tools"):
        argv += ["--allowedTools", tool]
    for tool in _check_tools(disallowed_tools, "disallowed_tools"):
        argv += ["--disallowedTools", tool]

    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ClaudeRunnerError("model: must be a non-empty string")
        argv += ["--model", model.strip()]

    if effort is not None:
        if effort not in EFFORT_LEVELS:
            raise ClaudeRunnerError(f"effort: {effort!r} not in {list(EFFORT_LEVELS)}")
        argv += ["--effort", effort]

    if fallback_model is not None:
        if not isinstance(fallback_model, str) or not fallback_model.strip():
            raise ClaudeRunnerError("fallback_model: must be a non-empty string")
        argv += ["--fallback-model", fallback_model.strip()]

    if max_budget_usd is not None:
        if isinstance(max_budget_usd, bool) or not isinstance(max_budget_usd, (int, float)):
            raise ClaudeRunnerError("max_budget_usd: must be a number")
        if max_budget_usd <= 0:
            raise ClaudeRunnerError("max_budget_usd: must be > 0")
        argv += ["--max-budget-usd", _format_usd(max_budget_usd)]

    return argv


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def parse_top_level_json(text):
    """Return the parsed JSON value when the whole text is one JSON document, else None."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _signal_group(proc, sig):
    """Send sig to the child's process group (falls back to the child alone)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        pass


def _stop_process(proc, kill_grace_seconds):
    """Terminate the process group; kill it if still alive after the grace period."""
    _signal_group(proc, signal.SIGTERM)
    try:
        return proc.communicate(timeout=kill_grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, signal.SIGKILL)
    try:
        return proc.communicate(timeout=kill_grace_seconds)
    except subprocess.TimeoutExpired:
        # Unkillable (e.g. stuck in D state); give up on output rather than hang.
        return "", ""


def run_claude(
    prompt,
    *,
    cwd,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    claude_bin=DEFAULT_CLAUDE_BIN,
    structured=True,
    json_schema=None,
    session_id=None,
    append_system_prompt_file=None,
    permission_mode=None,
    allowed_tools=None,
    disallowed_tools=None,
    model=None,
    effort=None,
    fallback_model=None,
    max_budget_usd=None,
    kill_grace_seconds=DEFAULT_KILL_GRACE_SECONDS,
):
    """Run Claude once and return a ClaudeResult. Never raises for a failed run.

    Raises ClaudeRunnerError only for invalid arguments (bad cwd, bad flags).
    The child inherits the current environment (Claude needs its own config)
    but nothing from the environment is copied into the result.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ClaudeRunnerError("prompt: must be a non-empty string")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ClaudeRunnerError("timeout: must be a positive number of seconds")
    if isinstance(kill_grace_seconds, bool) or not isinstance(kill_grace_seconds, (int, float)) \
            or kill_grace_seconds < 0:
        raise ClaudeRunnerError("kill_grace_seconds: must be >= 0")
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        raise ClaudeRunnerError(f"cwd: {str(cwd)!r} is not a directory")
    if append_system_prompt_file is not None and not Path(append_system_prompt_file).is_file():
        raise ClaudeRunnerError(f"append_system_prompt_file: not a file: {str(append_system_prompt_file)!r}")

    argv = build_argv(
        claude_bin,
        structured=structured,
        json_schema=json_schema,
        session_id=session_id,
        append_system_prompt_file=append_system_prompt_file,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=model,
        effort=effort,
        fallback_model=fallback_model,
        max_budget_usd=max_budget_usd,
    )

    def result(exit_code, stdout, stderr, started, timed_out=False, error=None):
        return ClaudeResult(
            argv=argv,
            cwd=str(cwd_path),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(time.monotonic() - started, 3),
            timed_out=timed_out,
            structured=structured,
            schema_requested=json_schema is not None,
            parsed_json=parse_top_level_json(stdout),
            prompt_chars=len(prompt),
            error=error,
        )

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,  # own process group so timeouts reach tool subprocesses
        )
    except FileNotFoundError:
        msg = f"claude binary not found: {argv[0]}"
        return result(EXIT_NOT_FOUND, "", msg, started, error=msg)
    except PermissionError:
        msg = f"claude binary not executable: {argv[0]}"
        return result(EXIT_NOT_EXEC, "", msg, started, error=msg)
    except OSError as exc:
        msg = f"failed to start claude: {exc.__class__.__name__}"
        return result(EXIT_NOT_EXEC, "", msg, started, error=msg)

    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _stop_process(proc, kill_grace_seconds)
        return result(
            EXIT_TIMEOUT, stdout or "", stderr or "", started,
            timed_out=True, error=f"timed out after {timeout} seconds",
        )
    return result(proc.returncode, stdout or "", stderr or "", started)
