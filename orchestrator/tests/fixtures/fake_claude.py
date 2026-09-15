#!/usr/bin/env python3
"""Stand-in for the Claude CLI used by the runner/classifier tests.

Invoked as `python3 fake_claude.py [--fake-mode MODE] <claude flags...>`.
The mode comes from `--fake-mode` (a leading argument, so the runner can pass
it as part of claude_bin) or, failing that, the FAKE_CLAUDE_MODE environment
variable. Default: success.

Every JSON-emitting mode echoes the argv it received (minus --fake-mode) and
the number of prompt characters read from stdin, so tests can check what the
runner actually executed.

Modes:
  success            exit 0, valid result JSON with structured_output
  success_text       exit 0, plain text (for --output-format text calls)
  missing_structured exit 0, valid result JSON without structured_output
  invalid_json       exit 0, stdout is not JSON
  rate_limit         exit 1, JSON is_error result mentioning usage limit / resets at
  rate_limit_stderr  exit 1, non-JSON stdout, "429 rate limit" on stderr
  auth               exit 1, "Not logged in" on stderr
  transient          exit 1, unrecognised network error on stderr
  budget             exit 1, JSON subtype error_max_budget_usd
  task_error         exit 1, JSON subtype error_max_turns
  timeout            sleeps; exits on SIGTERM (graceful stop)
  timeout_stubborn   sleeps; ignores SIGTERM (needs SIGKILL)
"""

import json
import os
import signal
import sys
import time

SLEEP_SECONDS = 30


def _pick_mode(argv):
    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--fake-mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    return mode, rest


def _base(argv, prompt, subtype="success", is_error=False, result="ok"):
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "duration_ms": 12,
        "num_turns": 1,
        "result": result,
        "session_id": "00000000-0000-4000-8000-000000000000",
        "total_cost_usd": 0.0123,
        "argv": argv,
        "prompt_chars": len(prompt),
    }


def _emit(obj, code):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
    return code


def main():
    mode, argv = _pick_mode(sys.argv[1:])

    if mode in ("timeout", "timeout_stubborn"):
        if mode == "timeout_stubborn":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.stdout.write("partial output before hang\n")
        sys.stdout.flush()
        # Do not read stdin: the runner must cope with a child that never
        # consumes the prompt.
        time.sleep(SLEEP_SECONDS)
        return 0

    prompt = sys.stdin.read()

    if mode == "success":
        obj = _base(argv, prompt)
        obj["structured_output"] = {"status": "COMPLETED", "summary": "fake done"}
        return _emit(obj, 0)
    if mode == "success_text":
        sys.stdout.write("plain text answer\n")
        return 0
    if mode == "missing_structured":
        return _emit(_base(argv, prompt), 0)
    if mode == "invalid_json":
        sys.stdout.write("{not json at all\n")
        return 0
    if mode == "rate_limit":
        return _emit(
            _base(argv, prompt, subtype="error_during_execution", is_error=True,
                  result="You've hit your usage limit. Resets at 7pm (Europe/Amsterdam)."),
            1,
        )
    if mode == "rate_limit_stderr":
        sys.stdout.write("API Error\n")
        sys.stderr.write("API Error: 429 {\"type\":\"rate_limit_error\"}\n")
        return 1
    if mode == "auth":
        sys.stderr.write("Not logged in. Please run /login\n")
        return 1
    if mode == "transient":
        sys.stderr.write("Error: fetch failed (ECONNRESET: socket hang up)\n")
        return 1
    if mode == "budget":
        return _emit(
            _base(argv, prompt, subtype="error_max_budget_usd", is_error=True,
                  result="Reached max budget of $1.00 (spent $1.02)"),
            1,
        )
    if mode == "task_error":
        return _emit(
            _base(argv, prompt, subtype="error_max_turns", is_error=True,
                  result="Reached maximum number of turns without completing the task"),
            1,
        )

    sys.stderr.write(f"fake_claude: unknown mode {mode!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
