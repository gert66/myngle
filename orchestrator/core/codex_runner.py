"""Safe non-interactive Codex Worker adapter.

Codex is used only for the Worker role. It runs with approval=never and the
workspace-write sandbox; the orchestrator still owns Git scope, evidence,
review and commits. Prompts are sent on stdin and the final response is
validated by Codex against the same JSON schema used for Claude Workers.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from core.claude_runner import ClaudeResult, EXIT_NOT_FOUND, EXIT_TIMEOUT

DEFAULT_CODEX_BIN = "/home/myngle/.npm-global/bin/codex"


def _codex_result(**kwargs):
    result = ClaudeResult(**kwargs)
    result.provider = "codex"
    return result


def run_codex(prompt, *, cwd, json_schema, timeout=1800, codex_bin=DEFAULT_CODEX_BIN,
              append_system_prompt_file=None):
    cwd = Path(cwd)
    resolved = shutil.which(str(codex_bin)) if os.path.sep not in str(codex_bin) else str(codex_bin)
    if not resolved or not Path(resolved).is_file():
        msg = f"codex binary not found: {codex_bin}"
        return _codex_result(argv=[str(codex_bin)], cwd=str(cwd), exit_code=EXIT_NOT_FOUND,
                            stdout="", stderr=msg, duration_seconds=0, timed_out=False,
                            structured=True, schema_requested=True, parsed_json=None,
                            prompt_chars=len(prompt), error=msg)
    combined = prompt
    if append_system_prompt_file:
        stable = Path(append_system_prompt_file).read_text(encoding="utf-8")
        combined = stable + "\n\n" + prompt
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="orchestrator-codex-") as td:
        schema_path = Path(td) / "schema.json"
        output_path = Path(td) / "last.json"
        schema_path.write_text(json.dumps(json_schema), encoding="utf-8")
        argv = [resolved, "-a", "never", "exec", "--sandbox", "workspace-write",
                "--ephemeral", "--color", "never", "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-"]
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
        except OSError as exc:
            msg = f"cannot execute codex: {exc}"
            return _codex_result(argv=argv, cwd=str(cwd), exit_code=126, stdout="", stderr=msg,
                                duration_seconds=0, timed_out=False, structured=True,
                                schema_requested=True, parsed_json=None,
                                prompt_chars=len(combined), error=msg)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(input=combined, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
            exit_code = EXIT_TIMEOUT
        parsed = None
        if not timed_out and exit_code == 0 and output_path.is_file():
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
                parsed = {"type": "result", "subtype": "success", "is_error": False,
                          "structured_output": payload, "provider": "codex"}
            except (OSError, json.JSONDecodeError):
                parsed = None
        return _codex_result(argv=argv, cwd=str(cwd), exit_code=exit_code,
                            stdout=stdout or "", stderr=stderr or "",
                            duration_seconds=time.monotonic() - started, timed_out=timed_out,
                            structured=True, schema_requested=True, parsed_json=parsed,
                            prompt_chars=len(combined), error=None)
