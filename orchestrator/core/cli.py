"""Command line for the job state machine (core/machine.py), stdlib only.

    python3 -m core.cli submit  --job-id ID --repo OWNER/NAME --repo-path DIR --branch B \
                                --mode read|write (--goal TEXT | --goal-file F) [options]
    python3 -m core.cli run     --job-id ID [--dry-run] [--claude-bin P] [--timeout S]
    python3 -m core.cli resume  --job-id ID [--force] [run options]
    python3 -m core.cli status  --job-id ID [--json]
    python3 -m core.cli approve      --job-id ID [--answer TEXT]
    python3 -m core.cli extend-scope --job-id ID --allow PATH [--allow PATH ...] [--answer TEXT]
    python3 -m core.cli complete     --job-id ID [--reason TEXT]
    python3 -m core.cli set-config   --job-id ID [--test CMD ...] [--test-timeout S] [caps]
    python3 -m core.cli abort        --job-id ID [--reason TEXT]

Jobs live under --jobs-dir (default <orchestrator>/jobs/<job-id>).

--dry-run copies the job directory to a temporary location and drives the
copy with DryRunRunner (canned answers): real Claude is never invoked, no
test command runs, nothing is staged or committed, and the real job
directory is untouched. This batch has no push, merge or deploy command.

Exit codes: 0 DONE, 2 ERROR, 3 WAITING, 4 NEEDS_HUMAN, 5 lock held,
6 stopped mid-way (max transitions), 1 usage/configuration error.
"""

import argparse
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from core.claude_runner import DEFAULT_CLAUDE_BIN, DEFAULT_TIMEOUT_SECONDS
from core.contracts import SCHEMA_VERSION, ContractError
from core.providers import DEFAULT_CODEX_BIN, WORKER_COMPLEXITIES, WORKER_PROVIDERS, build_runner
from core.recover import RecoverError, start_job_service
from core.machine import (
    DEFAULT_MAX_TRANSITIONS, ORCH_ROOT, DryRunRunner, JobLock, LockHeld, Machine, MachineError,
    create_job,
)
from core.state import StateError, load_state, read_events, utc_now
from core.scheduler import write_job_policy

DEFAULT_JOBS_DIR = ORCH_ROOT / "jobs"
EXIT_BY_PHASE = {"DONE": 0, "ERROR": 2, "WAITING": 3, "NEEDS_HUMAN": 4}
EXIT_USAGE = 1
EXIT_LOCKED = 5
EXIT_STOPPED = 6


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m core.cli", description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR), help="directory holding one subdir per job")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("submit", help="create a new QUEUED job")
    s.add_argument("--job-id", required=True)
    s.add_argument("--repo", required=True, help="repository slug, e.g. gert66/myngle")
    s.add_argument("--repo-path", required=True, help="local checkout directory")
    s.add_argument("--branch", required=True, help="work branch (main/master are refused at run time)")
    s.add_argument("--mode", required=True, choices=("read", "write"))
    goal = s.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal")
    goal.add_argument("--goal-file")
    s.add_argument("--input", action="append", default=[], help="input file for the Brain (repeatable)")
    s.add_argument("--test", action="append", default=[], help="test command run by the orchestrator (repeatable)")
    s.add_argument("--allow", action="append", default=None, help="allowed repo-relative path pattern (repeatable; default *)")
    s.add_argument("--forbid", action="append", default=[], help="forbidden path pattern (repeatable)")
    s.add_argument("--origin", help="expected origin URL (default https://github.com/<repo>.git)")
    s.add_argument("--max-batches", type=int, help="job.max_steps")
    s.add_argument("--max-repairs", type=int, help="job.max_repairs_per_step")
    s.add_argument("--max-reviews", type=int)
    s.add_argument("--max-claude-calls", type=int)
    s.add_argument("--max-attempts", type=int)
    s.add_argument("--test-timeout", type=int)
    s.add_argument("--role-policy-json", help="strict JSON object overriding Brain/Worker/Reviewer Claude policy")
    s.add_argument("--worker-provider", choices=WORKER_PROVIDERS,
                   help="Worker role provider (default auto: LOW/NORMAL Gemini then Sonnet fallback; HIGH Sonnet)")
    s.add_argument("--worker-complexity", choices=WORKER_COMPLEXITIES,
                   help="Worker routing complexity for auto provider: low|normal|high (default normal)")
    s.add_argument("--claude-bin", default=DEFAULT_CLAUDE_BIN,
                   help="Claude executable/profile pinned to this job across restarts")
    quota = s.add_mutually_exclusive_group()
    quota.add_argument("--quota-routing", dest="quota_routing", action="store_true", default=True,
                       help="route Claude calls using shared quota state (default)")
    quota.add_argument("--no-quota-routing", dest="quota_routing", action="store_false",
                       help="disable shared Claude quota routing for this job")
    s.add_argument("--quota-state", default="/home/myngle/orchestrator/config/claude_quota.json")
    s.add_argument("--not-before", help="earliest start as offset-aware ISO-8601, e.g. 2026-09-15T03:00:00+02:00")
    s.add_argument("--no-start", action="store_true", help="create QUEUED job without starting its user-systemd supervisor")
    s.add_argument("--resource-class", choices=("read", "dev", "heavy"),
                   help="scheduler resource class; default inferred from mode")
    s.add_argument("--priority", type=int, default=0, help="scheduler priority; higher runs first")
    s.add_argument("--depends-on", action="append", default=[], help="job id that must be DONE first; repeatable")

    runner_opts = argparse.ArgumentParser(add_help=False)
    runner_opts.add_argument("--dry-run", action="store_true", help="drive a temporary copy with canned answers")
    runner_opts.add_argument("--claude-bin", default=None, help="override the Claude executable pinned in job config")
    runner_opts.add_argument("--codex-bin", default=DEFAULT_CODEX_BIN,
                             help="Codex executable used only when a job's worker_provider is codex")
    runner_opts.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="seconds per Claude call")
    runner_opts.add_argument("--max-transitions", type=int, default=DEFAULT_MAX_TRANSITIONS)

    for name, help_text in (("run", "run a QUEUED or in-progress job until it pauses or finishes"),
                            ("resume", "continue a WAITING job at its retry phase")):
        p = sub.add_parser(name, parents=[runner_opts], help=help_text)
        p.add_argument("--job-id", required=True)
        if name == "resume":
            p.add_argument("--force", action="store_true", help="ignore wait_until")

    p = sub.add_parser("status", help="show phase, counters and recent events")
    p.add_argument("--job-id", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("approve", help="answer a NEEDS_HUMAN question and unblock the job")
    p.add_argument("--job-id", required=True)
    p.add_argument("--answer", default="approved")

    p = sub.add_parser("extend-scope", help="add explicitly human-approved allowed paths while paused")
    p.add_argument("--job-id", required=True)
    p.add_argument("--allow", action="append", required=True, help="repo-relative path/pattern; repeatable")
    p.add_argument("--answer", default="scope extended by human")

    p = sub.add_parser("complete", help="mark a paused job DONE after a verified committed PASS batch")
    p.add_argument("--job-id", required=True)
    p.add_argument("--reason", default="completed by human approval")

    p = sub.add_parser("set-config", help="update test commands/timeouts/caps while paused")
    p.add_argument("--job-id", required=True)
    tests = p.add_mutually_exclusive_group()
    tests.add_argument("--test", action="append", help="replacement test command; repeatable")
    tests.add_argument("--clear-tests", action="store_true", help="replace test command list with empty list")
    p.add_argument("--test-timeout", type=int)
    p.add_argument("--max-batches", type=int)
    p.add_argument("--max-repairs", type=int)
    p.add_argument("--max-reviews", type=int)
    p.add_argument("--max-claude-calls", type=int)
    p.add_argument("--max-attempts", type=int)
    p.add_argument("--role-policy-json", help="strict JSON object overriding Brain/Worker/Reviewer Claude policy")
    p.add_argument("--answer", default="config updated by human")

    p = sub.add_parser("refresh-evidence", help="re-run evidence for the active batch without requiring a changed diff")
    p.add_argument("--job-id", required=True)
    p.add_argument("--reason", default="evidence refresh requested")

    p = sub.add_parser("abort", help="stop a job permanently (phase ERROR)")
    p.add_argument("--job-id", required=True)
    p.add_argument("--reason", default="aborted by human")
    return parser


def _job_dir(args):
    return Path(args.jobs_dir) / args.job_id


def _caps(args):
    caps = {}
    for cap, attr in (("max_reviews_per_batch", "max_reviews"), ("max_claude_calls", "max_claude_calls"),
                      ("max_attempts", "max_attempts")):
        if getattr(args, attr) is not None:
            caps[cap] = getattr(args, attr)
    return caps


def cmd_submit(args, out):
    goal = args.goal if args.goal is not None else Path(args.goal_file).read_text(encoding="utf-8")
    job = {
        "schema_version": SCHEMA_VERSION, "job_id": args.job_id, "repo": args.repo, "branch": args.branch,
        "mode": args.mode, "goal": goal, "created_at": utc_now(), "input_files": list(args.input),
    }
    if args.max_batches is not None:
        job["max_steps"] = args.max_batches
    if args.max_repairs is not None:
        job["max_repairs_per_step"] = args.max_repairs
    role_policy = None
    if args.role_policy_json is not None:
        try:
            role_policy = json.loads(args.role_policy_json)
        except ValueError as exc:
            raise MachineError(f"role-policy-json: invalid JSON ({exc})") from None
    config = {
        "repo_path": str(Path(args.repo_path).resolve()), "origin": args.origin,
        "allowed_paths": args.allow if args.allow is not None else ["*"], "forbidden_paths": args.forbid,
        "test_commands": [shlex.split(cmd) for cmd in args.test], "caps": _caps(args),
        "role_policy": role_policy,
        "claude_bin": args.claude_bin,
        "quota_routing": bool(args.quota_routing),
        "quota_state": args.quota_state,
        "not_before": args.not_before,
    }
    if args.test_timeout is not None:
        config["test_timeout"] = args.test_timeout
    if args.worker_provider is not None:
        config["worker_provider"] = args.worker_provider
    if args.worker_complexity is not None:
        config["worker_complexity"] = args.worker_complexity
    state = create_job(_job_dir(args), job, config)
    write_job_policy(_job_dir(args), resource_class=args.resource_class,
                     priority=args.priority, dependencies=args.depends_on)
    out.write(f"submitted {args.job_id}: {state['phase']} in {_job_dir(args)}\n")
    if not args.no_start:
        try:
            unit = start_job_service(args.job_id)
        except RecoverError as exc:
            raise MachineError(f"job was created but its supervisor could not start: {exc}") from exc
        out.write(f"started {unit}\n")
    return 0


def _make_machine(args, runner, out):
    job_dir = _job_dir(args)
    if args.dry_run:
        copy = Path(tempfile.mkdtemp(prefix=f"orchestrator-dryrun-{args.job_id}-")) / args.job_id
        shutil.copytree(job_dir, copy)
        out.write(f"dry-run copy: {copy}\n")
        return Machine(copy, DryRunRunner(), dry_run=True, log=lambda m: out.write(f"  {m}\n"))
    if runner is None:
        state = load_state(job_dir / "state.json")
        cfg = state.get("runtime", {}).get("config", {})
        selected_bin = args.claude_bin or cfg.get("claude_bin") or DEFAULT_CLAUDE_BIN
        account_router = None
        if cfg.get("quota_routing"):
            from core.quota_router import QuotaRouter
            account_router = QuotaRouter(cfg.get("quota_state"))
        runner = build_runner(claude_bin=selected_bin, timeout=args.timeout, codex_bin=args.codex_bin, account_router=account_router)
    return Machine(job_dir, runner, log=lambda m: out.write(f"  {m}\n"))


def _finish(machine, phase, out):
    st = machine.status()
    out.write(f"{st['job_id']}: {phase}")
    if phase == "WAITING":
        out.write(f" until {st['wait_until']} (then {st['retry_phase']})")
    if phase == "NEEDS_HUMAN":
        out.write(f"\n  question: {st['human_question']}")
    if phase == "ERROR" and st["last_error"]:
        out.write(f"\n  error: {st['last_error']}")
    out.write("\n")
    return EXIT_BY_PHASE.get(phase, EXIT_STOPPED)


def cmd_run(args, out, runner=None):
    machine = _make_machine(args, runner, out)
    return _finish(machine, machine.run(max_transitions=args.max_transitions), out)


def cmd_resume(args, out, runner=None):
    machine = _make_machine(args, runner, out)
    return _finish(machine, machine.resume(force=args.force, max_transitions=args.max_transitions), out)


def cmd_status(args, out):
    job_dir = _job_dir(args)
    machine = Machine(job_dir, runner=None)
    st = machine.status()
    events = read_events(job_dir / "events.jsonl")
    if args.json:
        out.write(json.dumps(dict(st, events=events[-10:]), indent=2, sort_keys=True) + "\n")
        return EXIT_BY_PHASE.get(st["phase"], EXIT_STOPPED)
    out.write(f"job:        {st['job_id']}\nphase:      {st['phase']}\nstep:       {st['step_id']}\n"
              f"attempt:    {st['attempt']}\nupdated:    {st['updated_at']}\n")
    if st["wait_until"]:
        out.write(f"wait_until: {st['wait_until']} -> {st['retry_phase']}\n")
    if st["human_question"]:
        out.write(f"question:   {st['human_question']}\n")
    if st["last_error"]:
        out.write(f"last_error: {st['last_error']}\n")
    out.write(f"counters:   {json.dumps(st['counters'])}\ncaps:       {json.dumps(st['caps'])}\n")
    totals = st.get("totals") or {}
    out.write(f"usage:      calls={totals.get('claude_calls', 0)} cost_usd={totals.get('cost_usd', 0):.6f} "
              f"in={totals.get('input_tokens', 0)} out={totals.get('output_tokens', 0)} "
              f"cache_read={totals.get('cache_read_input_tokens', 0)}\n")
    if st["batch"]:
        out.write(f"batch:      {json.dumps(st['batch'])}\n")
    out.write(f"history:    {len(st['history'])} batch(es) closed\n")
    for ev in events[-10:]:
        out.write(f"  {ev['ts']} {ev['from'] or '-'} -> {ev['to']}: {ev['reason']}\n")
    return EXIT_BY_PHASE.get(st["phase"], EXIT_STOPPED)


def cmd_approve(args, out):
    machine = Machine(_job_dir(args), runner=None)
    phase = machine.approve(args.answer)
    try:
        unit = start_job_service(args.job_id)
    except RecoverError as exc:
        raise MachineError(f"approval recorded but supervisor could not restart: {exc}") from exc
    out.write(f"{args.job_id}: approved -> {phase}; started {unit}\n")
    return 0


def cmd_extend_scope(args, out):
    machine = Machine(_job_dir(args), runner=None)
    allowed = machine.extend_scope(args.allow, args.answer)
    out.write(f"{args.job_id}: allowed paths -> {json.dumps(allowed)}\n")
    return 0


def cmd_complete(args, out):
    machine = Machine(_job_dir(args), runner=None)
    machine.complete(args.reason)
    out.write(f"{args.job_id}: DONE ({args.reason})\n")
    return 0

def cmd_set_config(args, out):
    machine = Machine(_job_dir(args), runner=None)
    commands = [] if args.clear_tests else ([shlex.split(x) for x in args.test] if args.test is not None else None)
    caps = {}
    for key, attr in (("max_batches", "max_batches"), ("max_repairs_per_batch", "max_repairs"),
                      ("max_reviews_per_batch", "max_reviews"), ("max_claude_calls", "max_claude_calls"),
                      ("max_attempts", "max_attempts")):
        value = getattr(args, attr)
        if value is not None:
            caps[key] = value
    role_policy = None
    if args.role_policy_json is not None:
        try:
            role_policy = json.loads(args.role_policy_json)
        except ValueError as exc:
            raise MachineError(f"role-policy-json: invalid JSON ({exc})") from None
    cfg = machine.set_config(test_commands=commands, test_timeout=args.test_timeout, caps=caps,
                             role_policy=role_policy, answer=args.answer)
    out.write(f"{args.job_id}: config updated -> {json.dumps(cfg, sort_keys=True)}\n")
    return 0


def cmd_refresh_evidence(args, out):
    machine = Machine(_job_dir(args), runner=None)
    phase = machine.refresh_evidence(args.reason)
    try:
        unit = start_job_service(args.job_id)
    except RecoverError as exc:
        raise MachineError(f"evidence refresh recorded but supervisor could not restart: {exc}") from exc
    out.write(f"{args.job_id}: {phase} ({args.reason}); started {unit}\n")
    return 0

def cmd_abort(args, out):
    machine = Machine(_job_dir(args), runner=None)
    machine.abort(args.reason)
    out.write(f"{args.job_id}: ERROR ({args.reason})\n")
    return 0


def main(argv=None, runner=None, out=None, err=None):
    """Entry point. `runner` overrides the real Claude runner for run/resume (tests)."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        if args.command == "submit":
            return cmd_submit(args, out)
        if args.command == "run":
            return cmd_run(args, out, runner)
        if args.command == "resume":
            return cmd_resume(args, out, runner)
        if args.command == "status":
            return cmd_status(args, out)
        if args.command == "approve":
            return cmd_approve(args, out)
        if args.command == "extend-scope":
            return cmd_extend_scope(args, out)
        if args.command == "complete":
            return cmd_complete(args, out)
        if args.command == "set-config":
            return cmd_set_config(args, out)
        if args.command == "refresh-evidence":
            return cmd_refresh_evidence(args, out)
        return cmd_abort(args, out)
    except LockHeld as exc:
        err.write(f"error: {exc}\n")
        return EXIT_LOCKED
    except (MachineError, StateError, ContractError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
