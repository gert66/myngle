"""Gemini Worker adapter for the deterministic orchestrator control plane.

Gemini is Worker-only. Brain and Reviewer remain Claude. LOW/NORMAL auto-routing
uses Gemini 3.8 Flash, Flex first and Standard on HTTP 429/500/503. The adapter
never commits, merges, pushes or changes branches. For write-mode jobs it may
apply a model-produced unified diff only after deterministic path/scope checks;
tests, evidence, review and commit ownership remain in core.machine.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from core.claude_runner import ClaudeResult, EXIT_TIMEOUT
from core import gitsafe

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_SECRETS_FILE = Path.home() / ".config" / "hermes" / "secrets.env"
FLEX_TIER = "flex"
STANDARD_TIER = "standard"
FALLBACK_HTTP_STATUSES = frozenset({429, 500, 503})
DEFAULT_CONTEXT_CHARS = 90_000


class GeminiRunnerError(ValueError):
    pass


class GeminiHTTPError(RuntimeError):
    def __init__(self, status, body=""):
        self.status = int(status)
        self.body = body
        super().__init__(f"HTTP {self.status}: {body[:500]}")


def _strip_shell_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_secrets(path=DEFAULT_SECRETS_FILE, environ=None):
    """Load simple KEY=VALUE entries without logging values. Existing env wins."""
    env = os.environ if environ is None else environ
    path = Path(path)
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in env:
            env[key] = _strip_shell_quotes(value)
    return env


def _http_call(prompt, service_tier, *, model=DEFAULT_MODEL, timeout=600, url=DEFAULT_URL,
               api_key=None, opener=None):
    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        load_secrets()
        key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise GeminiRunnerError("GEMINI_API_KEY is not configured")
    payload = {"model": model, "input": prompt, "service_tier": service_tier}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(exc.code, body) from exc


def extract_text(data):
    texts = []
    for step in (data or {}).get("steps", []):
        if step.get("type") != "model_output":
            continue
        for item in step.get("content", []):
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
    return "\n".join(texts).strip()


def extract_usage(data):
    raw = (data or {}).get("usage") or {}
    def n(name):
        value = raw.get(name, 0)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    input_tokens = n("total_input_tokens")
    output_tokens = n("total_output_tokens")
    thinking_tokens = n("total_thought_tokens")
    total_tokens = n("total_tokens") or (input_tokens + output_tokens + thinking_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "total_tokens": total_tokens,
    }


def _parse_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise GeminiRunnerError(f"Gemini output is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise GeminiRunnerError("Gemini output top level must be an object")
    return value


def _git_lines(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise GeminiRunnerError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def build_repo_context(repo, prompt, allowed_paths, forbidden_paths, max_chars=DEFAULT_CONTEXT_CHARS):
    """Build a bounded read-only source snapshot for an API model with no local file tools."""
    repo = Path(repo)
    raw = _git_lines(repo, "ls-files", "-z")
    paths = [p for p in raw.split("\0") if p]
    allowed_paths = list(allowed_paths or [])
    forbidden_paths = list(forbidden_paths or [])

    def eligible(path):
        scope = gitsafe.validate_scope([path], allowed_paths, forbidden_paths)
        return scope.ok

    candidates = [p for p in paths if eligible(p)]
    lower_prompt = prompt.lower()
    def score(path):
        name = Path(path).name.lower()
        return (2 if path.lower() in lower_prompt else 0) + (1 if name in lower_prompt else 0)
    candidates.sort(key=lambda p: (-score(p), p))

    tree = "\n".join(candidates)
    if len(tree) > 15_000:
        tree = tree[:15_000] + "\n[tree truncated]"
    parts = ["Repository file tree within allowed scope:\n" + tree]
    used = sum(len(x) for x in parts)
    included = 0
    skipped = 0
    for rel in candidates:
        path = repo / rel
        try:
            if not path.is_file() or path.stat().st_size > 150_000:
                skipped += 1
                continue
            data = path.read_bytes()
        except OSError:
            skipped += 1
            continue
        if b"\x00" in data:
            skipped += 1
            continue
        text = data.decode("utf-8", errors="replace")
        block = f"\n--- FILE {rel} ---\n{text}\n--- END FILE ---\n"
        if used + len(block) > max_chars:
            skipped += 1
            continue
        parts.append(block)
        used += len(block)
        included += 1
    parts.append(f"\nContext summary: {included} file(s) included; {skipped} skipped/truncated; max_chars={max_chars}.\n")
    return "".join(parts)


def _patch_paths(patch):
    paths = []
    for line in patch.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value == "/dev/null":
            continue
        if value.startswith("a/") or value.startswith("b/"):
            value = value[2:]
        if not value or value.startswith('"') or "\x00" in value:
            raise GeminiRunnerError("unsupported/ambiguous path in Gemini patch")
        paths.append(value)
    return sorted(set(paths))


def _apply_patch(repo, patch, allowed_paths, forbidden_paths):
    """Apply only an in-scope unified diff. Returns actual changed paths."""
    if not patch.strip():
        return []
    if not gitsafe.is_clean(repo):
        raise GeminiRunnerError("refusing Gemini patch: working tree is not clean before apply")
    paths = _patch_paths(patch)
    if not paths:
        raise GeminiRunnerError("Gemini patch contains no file paths")
    scope = gitsafe.validate_scope(paths, allowed_paths, forbidden_paths)
    if not scope.ok:
        raise GeminiRunnerError(f"Gemini patch is outside allowed scope: {scope.detail}")
    check = subprocess.run(["git", "apply", "--check", "--whitespace=error", "-"], cwd=str(repo),
                           input=patch, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check.returncode != 0:
        raise GeminiRunnerError(f"Gemini patch failed git apply --check: {check.stderr.strip()[:500]}")
    applied = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=str(repo),
                             input=patch, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if applied.returncode != 0:
        raise GeminiRunnerError(f"Gemini patch failed to apply: {applied.stderr.strip()[:500]}")
    changed = [c.path for c in gitsafe.changed_paths(repo)]
    post = gitsafe.validate_scope(changed, allowed_paths, forbidden_paths)
    if not post.ok:
        # Best-effort deterministic rollback of exactly the patch we just applied.
        subprocess.run(["git", "apply", "-R", "-"], cwd=str(repo), input=patch, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        raise GeminiRunnerError(f"post-apply scope check failed: {post.detail}")
    return changed


def _result(*, cwd, prompt, exit_code, stdout="", stderr="", parsed_json=None, duration=0.0,
            error=None, service_tier=None, fallback_events=None, retries=0, model=DEFAULT_MODEL,
            usage=None, provider_attempts=None):
    result = ClaudeResult(
        argv=["gemini-api", model, service_tier or "none"], cwd=str(cwd), exit_code=exit_code,
        stdout=stdout, stderr=stderr, duration_seconds=duration, timed_out=(exit_code == EXIT_TIMEOUT),
        structured=True, schema_requested=True, parsed_json=parsed_json,
        prompt_chars=len(prompt), error=error,
    )
    result.provider = "gemini"
    result.model = model
    result.service_tier = service_tier
    result.fallback_events = list(fallback_events or [])
    result.retries = int(retries)
    result.provider_attempts = list(provider_attempts or [])
    result.gemini_usage = dict(usage or {})
    return result


def run_gemini(prompt, *, cwd, json_schema, timeout=600, model=DEFAULT_MODEL,
               append_system_prompt_file=None, mode="read", allowed_paths=None,
               forbidden_paths=None, api_key=None, call_fn=None, context_chars=DEFAULT_CONTEXT_CHARS):
    """Run one Worker call. Flex -> Standard only on 429/500/503.

    The model returns an envelope {"worker": <Worker contract>, "patch": "..."}.
    In write mode the patch is applied only after deterministic scope validation.
    """
    if mode not in ("read", "write"):
        raise GeminiRunnerError("mode must be read or write")
    allowed_paths = list(allowed_paths or ["*"])
    forbidden_paths = list(forbidden_paths or [])
    stable = ""
    if append_system_prompt_file:
        stable = Path(append_system_prompt_file).read_text(encoding="utf-8")
    base_prompt = (stable + "\n\n" + prompt).strip()
    context = build_repo_context(cwd, base_prompt, allowed_paths, forbidden_paths, max_chars=context_chars)
    envelope_schema = {
        "worker": json_schema,
        "patch": "unified diff string; empty string for read mode/no file change",
    }
    request_prompt = (
        base_prompt
        + "\n\nYou are running through the Gemini API without direct local tools. "
          "Use only the repository snapshot below. Return exactly one JSON object, no markdown, with keys worker and patch. "
          "worker must satisfy the supplied Worker schema. In read mode patch MUST be empty. "
          "In write mode, if changes are required, patch MUST be a standard unified git diff and you must not claim tests were run. "
          "Set tests_run=[] and tests_passed=null; the deterministic orchestrator runs tests after you. "
          "If the provided repository context is insufficient, return worker.status=BLOCKED instead of guessing.\n"
        + "Required envelope description:\n" + json.dumps(envelope_schema, separators=(",", ":"), sort_keys=True)
        + "\n\n" + context
    )
    caller = call_fn or (lambda p, tier: _http_call(p, tier, model=model, timeout=timeout, api_key=api_key))
    started = time.monotonic()
    fallback_events = []
    attempts = []
    data = None
    tier = FLEX_TIER
    try:
        attempts.append({"provider": "gemini", "model": model, "service_tier": FLEX_TIER})
        data = caller(request_prompt, FLEX_TIER)
    except GeminiHTTPError as exc:
        if exc.status not in FALLBACK_HTTP_STATUSES:
            duration = time.monotonic() - started
            return _result(cwd=cwd, prompt=request_prompt, exit_code=1, stderr=str(exc), duration=duration,
                           error=str(exc), service_tier=FLEX_TIER, fallback_events=fallback_events,
                           retries=0, model=model, provider_attempts=attempts)
        fallback_events.append({"from": FLEX_TIER, "to": STANDARD_TIER, "reason": f"HTTP {exc.status}"})
        tier = STANDARD_TIER
        try:
            attempts.append({"provider": "gemini", "model": model, "service_tier": STANDARD_TIER})
            data = caller(request_prompt, STANDARD_TIER)
        except Exception as second:
            duration = time.monotonic() - started
            return _result(cwd=cwd, prompt=request_prompt, exit_code=1, stderr=str(second), duration=duration,
                           error=str(second), service_tier=STANDARD_TIER, fallback_events=fallback_events,
                           retries=1, model=model, provider_attempts=attempts)
    except Exception as exc:
        duration = time.monotonic() - started
        return _result(cwd=cwd, prompt=request_prompt, exit_code=1, stderr=str(exc), duration=duration,
                       error=str(exc), service_tier=FLEX_TIER, fallback_events=fallback_events,
                       retries=0, model=model, provider_attempts=attempts)

    duration = time.monotonic() - started
    usage = extract_usage(data)
    try:
        text = extract_text(data)
        envelope = _parse_json_object(text)
        worker = envelope.get("worker")
        patch = envelope.get("patch", "")
        if not isinstance(worker, dict):
            raise GeminiRunnerError("Gemini envelope.worker must be an object")
        if not isinstance(patch, str):
            raise GeminiRunnerError("Gemini envelope.patch must be a string")
        if mode == "read" and patch.strip():
            raise GeminiRunnerError("Gemini returned a patch in read mode")
        actual_changed = _apply_patch(cwd, patch, allowed_paths, forbidden_paths) if mode == "write" else []
        worker["files_changed"] = actual_changed
        worker["tests_run"] = []
        worker["tests_passed"] = None
        top = {
            "type": "result", "subtype": "success", "is_error": False,
            "structured_output": worker, "provider": "gemini", "model": model,
            "service_tier": tier,
            "usage": usage,
        }
        return _result(cwd=cwd, prompt=request_prompt, exit_code=0, stdout=text, parsed_json=top,
                       duration=duration, service_tier=tier, fallback_events=fallback_events,
                       retries=len(fallback_events), model=model, usage=usage, provider_attempts=attempts)
    except Exception as exc:
        return _result(cwd=cwd, prompt=request_prompt, exit_code=1, stdout=extract_text(data), stderr=str(exc),
                       duration=duration, error=str(exc), service_tier=tier, fallback_events=fallback_events,
                       retries=len(fallback_events), model=model, usage=usage, provider_attempts=attempts)
