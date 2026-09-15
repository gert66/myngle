import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import machine as machine_mod  # noqa: E402
from core.claude_runner import ClaudeResult  # noqa: E402
from core.contracts import load_schema  # noqa: E402
from core.machine import (  # noqa: E402
    DryRunRunner, JobLock, LockHeld, RepoLock, RepoLockHeld, Machine, MachineError, create_job, fake_result,
)
from core.providers import build_runner  # noqa: E402
from core.state import load_json, load_state, read_events, save_state  # noqa: E402

PY = sys.executable
WORK = "work"
ORIGIN = "https://github.com/gert66/myngle.git"
STEP = "step-01"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout


def make_repo(root, branch=WORK):
    repo = Path(root) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "remote", "add", "origin", ORIGIN)
    (repo / "README.md").write_text("hello\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n")
    git(repo, "add", "--", ".")
    git(repo, "commit", "-q", "-m", "init")
    # Local tracking ref lets deterministic freshness tests avoid network access.
    git(repo, "update-ref", "refs/remotes/origin/work", "HEAD")
    return repo


def snapshot(repo):
    """Everything about the repo a dry run must not change."""
    files = {}
    for path in sorted(repo.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        files[str(path.relative_to(repo))] = path.read_bytes()
    return {"head": git(repo, "rev-parse", "HEAD"), "status": git(repo, "status", "--porcelain"), "files": files}


def combined_prompt(call):
    """Logical prompt seen by Claude: cached system prefix plus dynamic user prompt."""
    prefix = call.get("append_system_prompt_file")
    stable = Path(prefix).read_text(encoding="utf-8") if prefix else ""
    return stable + "\n" + call["prompt"]


def brain_next(step_id=STEP, goal="Add a feature", instructions="Create src/feature.py"):
    return {"schema_version": 1, "decision": "NEXT", "reason": f"start {step_id}",
            "next_batch": {"step_id": step_id, "goal": goal, "instructions": instructions,
                           "acceptance_criteria": ["file exists"]}}


def brain_repair(step_id=STEP):
    return {"schema_version": 1, "decision": "REPAIR", "reason": "reviewer found problems",
            "next_batch": {"step_id": step_id, "goal": "Fix findings", "instructions": "Address the findings"}}


def brain(decision, **extra):
    obj = {"schema_version": 1, "decision": decision, "reason": f"brain says {decision}"}
    obj.update(extra)
    return obj


def worker_ok(step_id=STEP, status="COMPLETED", files=()):
    return {"schema_version": 1, "step_id": step_id, "status": status, "summary": "did the work",
            "files_changed": list(files), "tests_run": [], "tests_passed": None, "blockers": []}


def reviewer(verdict="PASS", step_id=STEP, **extra):
    obj = {"schema_version": 1, "step_id": step_id, "verdict": verdict, "scope_ok": True, "tests_ok": True,
           "summary": f"review {verdict}", "findings": []}
    obj.update(extra)
    return obj


def reviewer_fail(step_id=STEP, message="Missing docstring in feature"):
    return reviewer("FAIL", step_id, findings=[{"severity": "MAJOR", "message": message, "file": "src/feature.py"}])


def failed_result(stderr="", stdout="", exit_code=1, timed_out=False):
    return ClaudeResult(argv=["fake"], cwd=".", exit_code=exit_code, stdout=stdout, stderr=stderr,
                        duration_seconds=0.0, timed_out=timed_out, structured=True, schema_requested=True,
                        parsed_json=None)


def writes(repo, relpath, text, payload):
    """Script item: a Worker that changes a file, then reports payload."""
    def side_effect(role, prompt, kwargs):
        target = Path(repo) / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return payload
    return side_effect


class FakeRunner:
    """Scripted stand-in for Claude. Items: payload dict, ClaudeResult, or callable(role, prompt, kwargs)."""

    dry_run_safe = True

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def __call__(self, role, prompt, **kwargs):
        self.calls.append({"role": role, "prompt": prompt, **kwargs})
        if not self.script:
            raise AssertionError(f"unexpected {role} call #{len(self.calls)}")
        item = self.script.pop(0)
        if callable(item):
            item = item(role, prompt, kwargs)
        if isinstance(item, ClaudeResult):
            return item
        return fake_result(item, prompt, kwargs.get("cwd", "."))

    @property
    def roles(self):
        return [c["role"] for c in self.calls]


class FakeClock:
    def __init__(self, start="2026-09-13T10:00:00Z"):
        self.now = start

    def __call__(self):
        return self.now


class MachineTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = make_repo(self.root)
        self.job_dir = self.root / "jobs" / "job-1"
        self.clock = FakeClock()

    def job(self, **overrides):
        job = {"schema_version": 1, "job_id": "job-1", "repo": "gert66/myngle", "branch": WORK,
               "mode": "write", "goal": "Build the feature.", "created_at": "2026-09-13T09:00:00Z"}
        job.update(overrides)
        return job

    def create(self, config=None, **job_overrides):
        cfg = {"repo_path": str(self.repo), "freshness_fetch_remote": False}
        cfg.update(config or {})
        return create_job(self.job_dir, self.job(**job_overrides), cfg)

    def machine(self, script, **kw):
        runner = FakeRunner(script)
        kw.setdefault("clock", self.clock)
        return Machine(self.job_dir, runner, **kw), runner

    def phases(self):
        return [(e["from"], e["to"]) for e in read_events(self.job_dir / "events.jsonl")]

    def state(self):
        return load_state(self.job_dir / "state.json")


# ---------------------------------------------------------------------------
# Creation, persistence, contracts
# ---------------------------------------------------------------------------

class CreateJobTests(MachineTestCase):
    def test_create_job_writes_queued_state_with_runtime(self):
        state = self.create(config={"test_commands": [[PY, "-c", "pass"]], "allowed_paths": ["src/", "tests"]})
        self.assertEqual(state["phase"], "QUEUED")
        rt = state["runtime"]
        self.assertEqual(rt["counters"], {"brain": 0, "batches": 0, "claude_calls": 0})
        self.assertEqual(rt["config"]["origin"], ORIGIN)
        self.assertEqual(rt["config"]["allowed_paths"], ["src", "tests"])
        self.assertEqual(rt["config"]["caps"]["max_batches"], 20)
        self.assertEqual(self.state(), state)  # round-trips through validate_state
        self.assertEqual(self.phases(), [(None, "QUEUED")])
        self.assertTrue((self.job_dir / "job.json").exists())
        for sub in ("steps", "evidence", "logs"):
            self.assertTrue((self.job_dir / sub).is_dir())

    def test_role_policy_is_strict_and_defaults_preserve_cli_behavior(self):
        state = self.create(config={"role_policy": {
            "brain": {"model": " sonnet ", "effort": "low", "fallback_model": "opus", "max_budget_usd": 1.5}
        }})
        policy = state["runtime"]["config"]["role_policy"]
        self.assertEqual(policy["brain"], {
            "model": "sonnet", "effort": "low", "fallback_model": "opus", "max_budget_usd": 1.5
        })
        self.assertEqual(policy["worker"]["model"], None)
        with self.assertRaises(MachineError):
            create_job(self.root / "jobs" / "bad-policy", self.job(job_id="bad-policy"), {
                "repo_path": str(self.repo), "role_policy": {"brain": {"effort": "ultra"}}
            })

    def test_job_caps_override_defaults(self):
        state = self.create(max_steps=3, max_repairs_per_step=0)
        self.assertEqual(state["runtime"]["config"]["caps"]["max_batches"], 3)
        self.assertEqual(state["runtime"]["config"]["caps"]["max_repairs_per_batch"], 0)

    def test_state_schema_documents_runtime(self):
        self.assertIn("runtime", load_schema("state")["properties"])

    def test_bad_config_rejected(self):
        with self.assertRaises(MachineError):
            create_job(self.job_dir, self.job(), {})
        with self.assertRaises(MachineError):
            create_job(self.job_dir, self.job(), {"repo_path": str(self.repo), "test_commands": ["echo hi"]})
        with self.assertRaises(MachineError):
            create_job(self.job_dir, self.job(), {"repo_path": str(self.repo), "caps": {"max_batches": -1}})
        with self.assertRaises(MachineError):
            create_job(self.job_dir, self.job(), {"repo_path": str(self.repo), "bogus": 1})

    def test_duplicate_job_refused(self):
        self.create()
        with self.assertRaises(MachineError):
            self.create()

    def test_machine_requires_runtime_section(self):
        self.create()
        state = self.state()
        del state["runtime"]
        save_state(self.job_dir / "state.json", state)
        with self.assertRaises(MachineError):
            Machine(self.job_dir, FakeRunner())


# ---------------------------------------------------------------------------
# Full flows
# ---------------------------------------------------------------------------

class FullFlowTests(MachineTestCase):
    def test_next_worker_evidence_review_pass_commit_done(self):
        self.create(config={"test_commands": [[PY, "-c", "print('tests ok')"]]})
        m, runner = self.machine([
            brain_next(),
            writes(self.repo, "src/feature.py", "def feature():\n    return 1\n", worker_ok(files=["src/feature.py"])),
            reviewer("PASS"),
            brain("DONE"),
        ])
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(runner.roles, ["brain", "worker", "reviewer", "brain"])
        self.assertEqual(self.phases(), [
            (None, "QUEUED"), ("QUEUED", "PLANNING"), ("PLANNING", "WORKING"), ("WORKING", "EVIDENCE"),
            ("EVIDENCE", "REVIEWING"), ("REVIEWING", "COMMITTING"), ("COMMITTING", "PLANNING"), ("PLANNING", "DONE"),
        ])
        # Committed on the work branch, tree clean afterwards.
        log = git(self.repo, "log", "--format=%s", WORK).splitlines()
        self.assertEqual(len(log), 2)
        self.assertTrue(log[0].startswith("step-01: Add a feature"))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "show", "--stat", "--format=", "HEAD").count("src/feature.py"), 1)
        # State persisted and consistent.
        state = self.state()
        self.assertEqual(state["phase"], "DONE")
        self.assertIsNone(state["step_id"])
        rt = state["runtime"]
        self.assertIsNone(rt["batch"])
        self.assertIsNone(rt["inflight"])
        self.assertEqual(rt["counters"], {"brain": 2, "batches": 1, "claude_calls": 4})
        self.assertEqual(len(rt["history"]), 1)
        self.assertEqual(rt["history"][0]["outcome"], "committed")
        self.assertEqual(rt["history"][0]["commit"], git(self.repo, "rev-parse", "HEAD").strip())
        self.assertEqual(rt["history"][0]["paths"], ["src/feature.py"])
        # Step outputs and evidence are on disk.
        steps = sorted(p.name for p in (self.job_dir / "steps").iterdir())
        self.assertEqual(steps, ["brain-001.json", "brain-002.json", "step-01-reviewer-1.json", "step-01-worker-1.json"])
        evidence = load_json(self.job_dir / "evidence" / "step-01-evidence-1.json")
        self.assertEqual(evidence["tests_summary"], "PASS")
        self.assertIn("src/feature.py", evidence["diff"])
        self.assertEqual(len(list((self.job_dir / "logs").iterdir())), 4)

    def test_prompts_carry_goal_evidence_and_feedback(self):
        self.create(config={"test_commands": [[PY, "-c", "print('tests ok')"]]})
        m, runner = self.machine([
            brain_next(),
            writes(self.repo, "src/feature.py", "x = 1\n", worker_ok(files=["src/feature.py"])),
            reviewer("PASS"),
            brain("DONE"),
        ])
        m.run()
        first_brain, worker, review, last_brain = runner.calls
        self.assertIn("Build the feature.", combined_prompt(first_brain))
        self.assertNotIn("Build the feature.", first_brain["prompt"])  # stable prefix, not rebilled user text
        self.assertTrue(Path(first_brain["append_system_prompt_file"]).is_file())
        self.assertEqual(first_brain["append_system_prompt_file"], last_brain["append_system_prompt_file"])
        self.assertEqual(first_brain["permission_mode"], "plan")
        self.assertIn("Edit", first_brain["disallowed_tools"])
        self.assertEqual(first_brain["json_schema"], load_schema("brain"))
        self.assertIn("Create src/feature.py", combined_prompt(worker))
        self.assertIn("step-01", combined_prompt(worker))
        self.assertEqual(worker["permission_mode"], "acceptEdits")
        self.assertEqual(worker["cwd"], str(self.repo))
        self.assertIn("Bash(git push:*)", worker["disallowed_tools"])
        for blocked in ("Bash(git stash:*)", "Bash(git clean:*)", "Bash(git add:*)",
                        "Bash(git branch:*)", "Bash(git tag:*)"):
            self.assertIn(blocked, worker["disallowed_tools"])
        self.assertIn("+x = 1", review["prompt"])          # actual diff
        self.assertIn('"tests_summary": "PASS"', review["prompt"])
        self.assertIn("did the work", review["prompt"])    # worker claim
        self.assertEqual(review["permission_mode"], "plan")
        self.assertIn('"outcome": "committed"', last_brain["prompt"])
        self.assertIn('"step-01"', last_brain["prompt"])

    def test_fail_repair_pass(self):
        self.create()
        m, runner = self.machine([
            brain_next(),
            writes(self.repo, "src/feature.py", "def feature(): pass\n", worker_ok(files=["src/feature.py"])),
            reviewer_fail(),
            brain_repair(),
            writes(self.repo, "src/feature.py", '"""doc"""\ndef feature(): pass\n', worker_ok(files=["src/feature.py"])),
            reviewer("PASS"),
            brain("DONE"),
        ])
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(runner.roles, ["brain", "worker", "reviewer", "brain", "worker", "reviewer", "brain"])
        self.assertIn("Missing docstring in feature", runner.calls[4]["prompt"])  # repair worker sees findings
        self.assertIn("Address the findings", runner.calls[4]["prompt"])
        self.assertIn('"verdict": "FAIL"', runner.calls[3]["prompt"])           # brain sees reviewer FAIL
        self.assertEqual(self.phases()[4:9], [
            ("EVIDENCE", "REVIEWING"), ("REVIEWING", "PLANNING"), ("PLANNING", "WORKING"),
            ("WORKING", "EVIDENCE"), ("EVIDENCE", "REVIEWING"),
        ])
        history = self.state()["runtime"]["history"]
        self.assertEqual(history[0]["repairs"], 1)
        self.assertEqual(history[0]["reviews"], 2)
        self.assertEqual(len(git(self.repo, "log", "--format=%h").splitlines()), 2)
        steps = sorted(p.name for p in (self.job_dir / "steps").iterdir())
        self.assertIn("step-01-worker-2.json", steps)
        self.assertIn("step-01-reviewer-2.json", steps)

    def test_reviewer_pass_degraded_by_contract_guard_is_not_committed(self):
        self.create()
        m, runner = self.machine([
            brain_next(),
            writes(self.repo, "src/feature.py", "x\n", worker_ok()),
            reviewer("PASS", scope_ok=False),
            brain("ERROR"),
        ])
        self.assertEqual(m.run(), "ERROR")
        self.assertIn(("REVIEWING", "PLANNING"), self.phases())
        self.assertNotIn(("REVIEWING", "COMMITTING"), self.phases())
        self.assertEqual(len(git(self.repo, "log", "--format=%h").splitlines()), 1)
        self.assertIn("scope_ok is false", runner.calls[3]["prompt"])

    def test_reviewer_pass_overruled_by_failing_test_evidence(self):
        self.create(config={"test_commands": [[PY, "-c", "raise SystemExit(1)"]]})
        m, runner = self.machine([
            brain_next(), writes(self.repo, "src/feature.py", "x\n", worker_ok()), reviewer("PASS"), brain("ERROR"),
        ])
        self.assertEqual(m.run(), "ERROR")
        self.assertNotIn(("REVIEWING", "COMMITTING"), self.phases())
        self.assertIn("orchestrator test evidence is FAIL", runner.calls[3]["prompt"])

    def test_harness_error_pauses_before_review_then_set_config_recovers(self):
        self.create(config={"test_commands": [["/nonexistent/orchestrator-test-binary"]]})
        m, runner = self.machine([
            brain_next(),
            writes(self.repo, "src/feature.py", "x\n", worker_ok(files=["src/feature.py"])),
        ])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertEqual(runner.roles, ["brain", "worker"])
        state = self.state()
        self.assertEqual(state["runtime"]["batch"]["evidence"]["tests_summary"], "HARNESS_ERROR")
        self.assertEqual(state["runtime"]["retry_phase"], "EVIDENCE")
        self.assertIn("Evidence harness error", state["human_question"])
        self.assertEqual(
            sum(1 for before, after in self.phases() if after == "NEEDS_HUMAN" and before != after), 1
        )

        control = Machine(self.job_dir, runner=None, clock=self.clock)
        cfg = control.set_config(test_commands=[[PY, "-c", "pass"]], answer="use local interpreter")
        self.assertEqual(cfg["test_commands"], [[PY, "-c", "pass"]])
        self.assertEqual(control.approve("rerun corrected evidence"), "EVIDENCE")

        m2, runner2 = self.machine([reviewer("PASS"), brain("DONE")])
        self.assertEqual(m2.run(), "DONE")
        self.assertEqual(runner2.roles, ["reviewer", "brain"])
        self.assertEqual(self.state()["runtime"]["history"][0]["verdict"], "PASS")

    def test_review_again_reviews_without_new_worker_run(self):
        self.create()
        m, runner = self.machine([
            brain_next(), writes(self.repo, "src/feature.py", "x\n", worker_ok()), reviewer_fail(),
            brain("REVIEW_AGAIN"), reviewer("PASS"), brain("DONE"),
        ])
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(runner.roles, ["brain", "worker", "reviewer", "brain", "reviewer", "brain"])
        self.assertEqual(len(git(self.repo, "log", "--format=%h").splitlines()), 2)

    def test_read_mode_never_commits_and_worker_cannot_edit(self):
        self.create(mode="read")
        m, runner = self.machine([brain_next(), worker_ok(), reviewer("PASS"), brain("DONE")])
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(runner.calls[1]["permission_mode"], "plan")
        self.assertEqual(len(git(self.repo, "log", "--format=%h").splitlines()), 1)
        self.assertEqual(self.state()["runtime"]["history"][0]["outcome"], "reviewed, nothing to commit")

    def test_read_mode_job_with_changes_needs_human(self):
        self.create(mode="read")
        m, runner = self.machine([brain_next(), writes(self.repo, "src/x.py", "x\n", worker_ok()), reviewer("PASS")])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("Read-mode job", self.state()["human_question"])
        self.assertEqual(self.state()["runtime"]["retry_phase"], "COMMITTING")
        self.assertEqual(len(git(self.repo, "log", "--format=%h").splitlines()), 1)

    def test_out_of_scope_change_blocks_commit(self):
        self.create(config={"allowed_paths": ["src"]})
        m, runner = self.machine([brain_next(), writes(self.repo, "README.md", "changed\n", worker_ok()), reviewer("PASS")])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("out of scope: README.md", self.state()["human_question"])
        self.assertEqual(len(git(self.repo, "log", "--format=%h").splitlines()), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "M README.md")  # nothing staged

    def test_committing_requires_reviewer_pass(self):
        self.create()
        m, _ = self.machine([brain_next(), writes(self.repo, "src/x.py", "x\n", worker_ok()), reviewer_fail()])
        m.run(max_transitions=5)  # stops in REVIEWING -> PLANNING
        state = self.state()
        state["phase"] = "COMMITTING"
        save_state(self.job_dir / "state.json", state)
        m, _ = self.machine([])
        self.assertEqual(m.run(), "ERROR")
        self.assertIn("without a reviewer PASS", self.state()["last_error"])
        self.assertEqual(len(git(self.repo, "log", "--format=%h").splitlines()), 1)


# ---------------------------------------------------------------------------
# Human, wait, resume
# ---------------------------------------------------------------------------

class HumanAndWaitTests(MachineTestCase):
    def test_brain_needs_human_then_approve_resumes(self):
        self.create()
        m, runner = self.machine([brain("NEEDS_HUMAN", human_question="Ship to EU only?")])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        state = self.state()
        self.assertEqual(state["human_question"], "Ship to EU only?")
        self.assertEqual(state["runtime"]["retry_phase"], "PLANNING")
        # Running again does nothing and calls nobody.
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertEqual(runner.roles, ["brain"])
        with self.assertRaises(MachineError):
            m.resume()

        m2, runner2 = self.machine([brain("DONE")])
        self.assertEqual(m2.approve("Yes, EU only."), "PLANNING")
        self.assertIsNone(self.state()["human_question"])
        self.assertEqual(m2.run(), "DONE")
        self.assertIn("Yes, EU only.", runner2.calls[0]["prompt"])
        self.assertIn("Ship to EU only?", runner2.calls[0]["prompt"])
        self.assertEqual(self.phases()[-3:], [("PLANNING", "NEEDS_HUMAN"), ("NEEDS_HUMAN", "PLANNING"), ("PLANNING", "DONE")])

    def test_reviewer_needs_human(self):
        self.create()
        m, _ = self.machine([
            brain_next(), writes(self.repo, "src/x.py", "x\n", worker_ok()),
            reviewer("NEEDS_HUMAN", human_question="Is the VAT rule 21%?"),
        ])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertEqual(self.state()["human_question"], "Is the VAT rule 21%?")
        self.assertEqual(self.state()["runtime"]["retry_phase"], "PLANNING")

    def test_not_before_uses_waiting_then_starts_without_human(self):
        self.create(config={"not_before": "2026-09-13T12:00:00+02:00"})
        m, runner = self.machine([brain("DONE")])
        # +02:00 normalises to the fake clock's 10:00Z, so use a later target.
        state = self.state()
        self.assertEqual(state["runtime"]["config"]["not_before"], "2026-09-13T10:00:00Z")
        self.assertEqual(m.run(), "DONE")

        # A genuinely future schedule pauses deterministically.
        self.root.joinpath("jobs2").mkdir(exist_ok=True)
        second = self.root / "jobs2" / "job-2"
        create_job(second, self.job(job_id="job-2"), {"repo_path": str(self.repo),
                   "not_before": "2026-09-13T11:00:00Z", "freshness_fetch_remote": False})
        runner2 = FakeRunner([brain("DONE")])
        m2 = Machine(second, runner2, clock=self.clock)
        self.assertEqual(m2.run(), "WAITING")
        self.assertEqual(load_state(second / "state.json")["wait_until"], "2026-09-13T11:00:00Z")
        self.clock.now = "2026-09-13T11:00:00Z"
        self.assertEqual(m2.resume(), "DONE")
        self.assertEqual(runner2.roles, ["brain"])

    def test_brain_wait_then_resume_continues(self):
        self.create()
        m, runner = self.machine([brain("WAIT", wait_seconds=120)])
        self.assertEqual(m.run(), "WAITING")
        state = self.state()
        self.assertEqual(state["wait_until"], "2026-09-13T10:02:00Z")
        self.assertEqual(state["runtime"]["retry_phase"], "PLANNING")
        self.assertEqual(state["runtime"]["counters"]["brain"], 1)

        m2, runner2 = self.machine([brain("DONE")])
        self.assertEqual(m2.resume(), "WAITING")        # not due yet: no call, no transition
        self.assertEqual(runner2.roles, [])
        self.clock.now = "2026-09-13T10:02:00Z"
        self.assertEqual(m2.resume(), "DONE")
        self.assertEqual(runner2.roles, ["brain"])
        self.assertIsNone(self.state()["wait_until"])
        self.assertEqual(self.phases()[-3:], [("PLANNING", "WAITING"), ("WAITING", "PLANNING"), ("PLANNING", "DONE")])

    def test_rate_limit_waits_and_resume_retries_same_step(self):
        self.create()
        m, runner = self.machine([
            brain_next(),
            failed_result(stderr='API Error: 429 {"type":"rate_limit_error"}'),
        ])
        self.assertEqual(m.run(), "WAITING")
        state = self.state()
        self.assertEqual(state["phase"], "WAITING")
        self.assertEqual(state["attempt"], 1)
        self.assertEqual(state["runtime"]["retry_phase"], "WORKING")
        self.assertEqual(state["runtime"]["inflight"]["call_id"], "step-01-worker-1")
        self.assertEqual(state["wait_until"], "2026-09-13T10:05:00Z")  # 300 s backoff
        self.assertIn("RATE_LIMIT", state["last_error"])

        m2, runner2 = self.machine([
            writes(self.repo, "src/x.py", "x\n", worker_ok()), reviewer("PASS"), brain("DONE"),
        ])
        self.assertEqual(m2.resume(force=True), "DONE")
        self.assertEqual(runner2.roles, ["worker", "reviewer", "brain"])
        self.assertTrue((self.job_dir / "steps" / "step-01-worker-1.json").exists())
        self.assertEqual(self.state()["attempt"], 0)

    def test_quota_routing_rate_limit_reroutes_without_needs_human(self):
        self.create(config={"quota_routing": True})

        class Router:
            def __init__(self):
                self.marked = []
            def mark_rate_limited(self, route, now=None):
                self.marked.append((route, now))
                return "2026-09-13T11:00:00Z"
            def select(self, role, requested_model=None):
                return {"account": "gert66", "bucket": "all"}
            def next_available_at(self, role, requested_model=None, now=None):
                return "2026-09-13T11:00:00Z"

        limited = failed_result(stderr='API Error: 429 {"type":"rate_limit_error"}')
        limited.quota_route = {"account": "marina", "bucket": "all", "model": None}
        runner = FakeRunner([limited, brain("DONE")])
        router = Router()
        runner.quota_router = router
        m = Machine(self.job_dir, runner, clock=self.clock)
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(runner.roles, ["brain", "brain"] )
        self.assertEqual(router.marked[0][0]["account"], "marina")
        self.assertNotIn("NEEDS_HUMAN", [b for _, b in self.phases()])

    def test_auto_worker_no_longer_falls_through_to_codex_when_claude_is_exhausted(self):
        self.create(config={"quota_routing": True, "worker_provider": "auto"})

        class Router:
            def mark_rate_limited(self, route, now=None):
                return "2026-09-13T11:00:00Z"
            def select(self, role, requested_model=None):
                return None
            def next_available_at(self, role, requested_model=None, now=None):
                return "2026-09-13T11:00:00Z"

        limited = failed_result(stderr="usage limit reached")
        limited.provider = "claude"
        limited.quota_route = {"account": "marina", "bucket": "all", "model": None}
        runner = FakeRunner([brain_next(), limited])
        runner.quota_router = Router()
        m = Machine(self.job_dir, runner, clock=self.clock)
        self.assertEqual(m.run(), "WAITING")
        self.assertEqual(runner.roles, ["brain", "worker"])
        self.assertIn("WAITING", [b for _, b in self.phases()])
        self.assertNotIn("NEEDS_HUMAN", [b for _, b in self.phases()])

    def test_quota_routing_rate_limit_waits_until_known_reset_when_no_alternative(self):
        self.create(config={"quota_routing": True})

        class Router:
            def mark_rate_limited(self, route, now=None):
                return "2026-09-13T11:00:00Z"
            def select(self, role, requested_model=None):
                return None
            def next_available_at(self, role, requested_model=None, now=None):
                return "2026-09-13T11:00:00Z"

        limited = failed_result(stderr="usage limit reached")
        limited.quota_route = {"account": "marina", "bucket": "all", "model": None}
        runner = FakeRunner([limited])
        runner.quota_router = Router()
        m = Machine(self.job_dir, runner, clock=self.clock)
        self.assertEqual(m.run(), "WAITING")
        state = self.state()
        self.assertEqual(state["wait_until"], "2026-09-13T11:00:00Z")
        self.assertEqual(state["runtime"]["retry_phase"], "PLANNING")
        self.assertIsNone(state.get("human_question"))

    def test_transient_error_backoff_scales_with_attempt(self):
        self.create()
        m, _ = self.machine([failed_result(stderr="ECONNRESET")])
        self.assertEqual(m.run(), "WAITING")
        self.assertEqual(self.state()["wait_until"], "2026-09-13T10:00:30Z")
        m2, _ = self.machine([failed_result(stderr="ECONNRESET")])
        self.assertEqual(m2.resume(force=True), "WAITING")
        self.assertEqual(self.state()["attempt"], 2)
        self.assertEqual(self.state()["wait_until"], "2026-09-13T10:01:00Z")

    def test_auth_failure_needs_human(self):
        self.create()
        m, _ = self.machine([failed_result(stderr="Not logged in. Please run /login")])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("AUTH", self.state()["human_question"])
        self.assertEqual(self.state()["runtime"]["retry_phase"], "PLANNING")

    def test_preflight_failure_needs_human_and_approve_retries_preflight(self):
        self.create()
        (self.repo / "dirty.txt").write_text("x\n")
        m, runner = self.machine([brain_next()])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("clean_tree", self.state()["human_question"])
        self.assertEqual(self.state()["runtime"]["retry_phase"], "QUEUED")
        self.assertEqual(runner.roles, [])  # hard gate blocks before Brain/Worker
        (self.repo / "dirty.txt").unlink()
        m2, runner2 = self.machine([brain_next(), worker_ok(), reviewer("PASS"), brain("DONE")])
        self.assertEqual(m2.approve(), "QUEUED")
        self.assertEqual(m2.run(), "DONE")
        self.assertEqual(runner2.roles, ["brain", "worker", "reviewer", "brain"])

    def test_base_advances_after_planning_blocks_before_first_worker(self):
        self.create()
        m, runner = self.machine([brain_next(), worker_ok()])
        self.assertEqual(m.run(max_transitions=2), "WORKING")
        self.assertEqual(runner.roles, ["brain"])
        old = git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "README.md").write_text("new base\n")
        git(self.repo, "commit", "-q", "-am", "base advanced")
        advanced = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "reset", "-q", "--hard", old)
        git(self.repo, "update-ref", "refs/remotes/origin/work", advanced)
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        state = self.state()
        self.assertIn("1 commit(s) behind", state["human_question"])
        self.assertEqual(state["runtime"]["branch_freshness"]["behind_by"], 1)
        self.assertEqual(state["runtime"]["branch_freshness"]["stage"], "first Worker write")
        self.assertEqual(runner.roles, ["brain"])  # Worker never ran

    def test_protected_branch_refused(self):
        self.create(branch="main")
        git(self.repo, "branch", "-m", "main")
        m, _ = self.machine([brain_next()])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("protected", self.state()["human_question"])

    def test_abort_from_any_non_terminal_phase(self):
        self.create()
        m, _ = self.machine([brain("WAIT", wait_seconds=60)])
        m.run()
        self.assertEqual(m.abort("changed my mind"), "ERROR")
        state = self.state()
        self.assertEqual(state["phase"], "ERROR")
        self.assertEqual(state["last_error"], "changed my mind")
        self.assertIsNone(state["wait_until"])
        with self.assertRaises(MachineError):
            m.abort()


# ---------------------------------------------------------------------------
# Caps and loop prevention
# ---------------------------------------------------------------------------

class CapTests(MachineTestCase):
    def test_repair_cap_stops(self):
        self.create(max_repairs_per_step=1)
        m, runner = self.machine([
            brain_next(), writes(self.repo, "src/x.py", "1\n", worker_ok()), reviewer_fail(),
            brain_repair(), writes(self.repo, "src/x.py", "2\n", worker_ok()), reviewer_fail(),
            brain_repair(),
        ])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        state = self.state()
        self.assertIn("Repair cap reached", state["human_question"])
        self.assertEqual(state["runtime"]["cap_hit"], "max_repairs_per_batch")
        self.assertEqual(runner.script, [])
        # approve lifts the cap and the brain is asked again
        m2, runner2 = self.machine([brain("DONE")])
        m2.approve("one more try")
        state = self.state()
        self.assertEqual(state["runtime"]["batch"]["repairs"], 1)
        self.assertEqual(state["runtime"]["config"]["caps"]["max_repairs_per_batch"], 2)
        self.assertEqual(m2.run(), "DONE")

    def test_review_cap_stops(self):
        self.create(config={"caps": {"max_reviews_per_batch": 1}})
        m, _ = self.machine([
            brain_next(), writes(self.repo, "src/x.py", "1\n", worker_ok()), reviewer_fail(), brain("REVIEW_AGAIN"),
        ])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("Review cap reached", self.state()["human_question"])

    def test_batch_cap_stops(self):
        self.create(max_steps=1)
        m, _ = self.machine([
            brain_next(), writes(self.repo, "src/x.py", "1\n", worker_ok()), reviewer("PASS"), brain_next("step-02"),
        ])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("Batch cap reached", self.state()["human_question"])
        self.assertEqual(self.state()["runtime"]["cap_hit"], "max_batches")

    def test_claude_call_cap_stops(self):
        self.create(config={"caps": {"max_claude_calls": 2}})
        m, runner = self.machine([brain_next(), writes(self.repo, "src/x.py", "1\n", worker_ok()), reviewer("PASS")])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertEqual(runner.roles, ["brain", "worker"])
        self.assertIn("Claude call cap reached", self.state()["human_question"])
        self.assertEqual(self.state()["runtime"]["retry_phase"], "REVIEWING")
        before = self.state()["runtime"]["counters"]["claude_calls"]
        m2, _ = self.machine([])
        self.assertEqual(m2.approve("allow another call chunk"), "REVIEWING")
        state = self.state()
        self.assertEqual(state["runtime"]["counters"]["claude_calls"], before)
        self.assertEqual(state["runtime"]["config"]["caps"]["max_claude_calls"], 4)

    def test_identical_diff_after_repair_stops(self):
        self.create()
        m, runner = self.machine([
            brain_next(), writes(self.repo, "src/x.py", "1\n", worker_ok()), reviewer_fail(),
            brain_repair(), worker_ok(),  # repair changes nothing
        ])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("identical diff", self.state()["human_question"])
        self.assertEqual(self.phases()[-1], ("EVIDENCE", "NEEDS_HUMAN"))
        self.assertEqual(runner.script, [])

    def test_repeated_invalid_output_stops(self):
        self.create()
        bad = [failed_result(stdout="{not json", exit_code=0) for _ in range(5)]
        m, runner = self.machine(bad)
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertEqual(len(runner.calls), 3)  # max_attempts
        self.assertEqual(self.state()["attempt"], 3)
        self.assertEqual(self.state()["runtime"]["cap_hit"], "max_attempts")
        self.assertIn("INVALID_OUTPUT", self.state()["human_question"])
        self.assertEqual(self.phases()[2:], [
            ("PLANNING", "PLANNING"), ("PLANNING", "PLANNING"), ("PLANNING", "NEEDS_HUMAN"),
        ])

    def test_contract_violation_counts_as_invalid_output(self):
        self.create()
        m, runner = self.machine([
            {"schema_version": 1, "decision": "NEXT", "reason": "no batch given"},  # NEXT without next_batch
            brain("REPAIR", next_batch={"step_id": "s", "goal": "g", "instructions": "i"}),  # no active batch
            brain_next(), worker_ok("wrong-step"),  # step_id mismatch
            worker_ok(), reviewer("PASS"), brain("DONE"),
        ])
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(len(runner.calls), 7)
        self.assertEqual(self.state()["runtime"]["counters"]["brain"], 2)

    def test_reused_step_id_rejected(self):
        self.create()
        m, runner = self.machine([
            brain_next(), worker_ok(), reviewer("PASS"),
            brain_next("step-01"), brain_next("step-02"), worker_ok("step-02"), reviewer("PASS", "step-02"), brain("DONE"),
        ])
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(self.state()["runtime"]["used_step_ids"], ["step-01", "step-02"])


# ---------------------------------------------------------------------------
# Idempotency, locking, dry-run
# ---------------------------------------------------------------------------

class RestartTests(MachineTestCase):
    def test_restart_reuses_completed_step_output(self):
        self.create()
        m, runner = self.machine([brain_next(), writes(self.repo, "src/x.py", "1\n", worker_ok())])
        original = machine_mod.Machine._transition

        def crash_after_worker(self_, to, reason, **fields):
            if to == "EVIDENCE":
                raise RuntimeError("simulated crash before the transition was persisted")
            return original(self_, to, reason, **fields)

        with mock.patch.object(machine_mod.Machine, "_transition", crash_after_worker):
            with self.assertRaises(RuntimeError):
                m.run()
        state = self.state()
        self.assertEqual(state["phase"], "WORKING")
        self.assertEqual(state["runtime"]["inflight"]["call_id"], "step-01-worker-1")
        self.assertTrue((self.job_dir / "steps" / "step-01-worker-1.json").exists())
        self.assertEqual(runner.roles, ["brain", "worker"])

        m2, runner2 = self.machine([reviewer("PASS"), brain("DONE")])  # no worker payload available
        self.assertEqual(m2.run(), "DONE")
        self.assertEqual(runner2.roles, ["reviewer", "brain"])
        self.assertEqual(self.state()["runtime"]["counters"]["claude_calls"], 4)
        self.assertIn("src/x.py", git(self.repo, "show", "--stat", "--format=", "HEAD"))

    def test_restart_mid_brain_call_uses_same_call_id(self):
        self.create()
        m, runner = self.machine([failed_result(stderr="ECONNRESET")])
        m.run()
        self.assertEqual(self.state()["runtime"]["inflight"]["call_id"], "brain-001")
        m2, runner2 = self.machine([brain("DONE")])
        m2.resume(force=True)
        self.assertTrue((self.job_dir / "steps" / "brain-001.json").exists())

    def test_state_persisted_after_every_transition(self):
        self.create()
        seen = []
        original = machine_mod.Machine._transition

        def record(self_, to, reason, **fields):
            original(self_, to, reason, **fields)
            seen.append((to, load_state(self.job_dir / "state.json")["phase"]))

        with mock.patch.object(machine_mod.Machine, "_transition", record):
            m, _ = self.machine([brain_next(), worker_ok(), reviewer("PASS"), brain("DONE")])
            m.run()
        self.assertEqual(len(seen), 7)
        for to, on_disk in seen:
            self.assertEqual(to, on_disk)


class LockTests(MachineTestCase):
    def test_repo_lock_busy_waits_without_running_worker_then_resumes(self):
        self.create()
        m, runner = self.machine([brain_next(), worker_ok(), reviewer("PASS"), brain("DONE")])
        lock_dir = self.root / "repo-locks"
        original = machine_mod.REPO_LOCKS_DIR
        machine_mod.REPO_LOCKS_DIR = lock_dir
        try:
            with RepoLock(self.repo, holder="other-job", lock_dir=lock_dir):
                self.assertEqual(m.run(), "WAITING")
                self.assertEqual(runner.roles, ["brain"])
                self.assertEqual(self.state()["runtime"]["retry_phase"], "WORKING")
            self.assertEqual(m.resume(force=True), "DONE")
            self.assertEqual(runner.roles, ["brain", "worker", "reviewer", "brain"])
        finally:
            machine_mod.REPO_LOCKS_DIR = original

    def test_repo_reservation_survives_lock_release_while_holder_batch_is_paused(self):
        self.create()
        lock_dir = self.root / "repo-locks"
        # Simulate an active batch in this job, then release the OS flock.
        st = self.state()
        st["phase"] = "NEEDS_HUMAN"
        st["runtime"]["batch"] = {"step_id": "held"}
        save_state(self.job_dir / "state.json", st)
        with RepoLock(self.repo, holder="job-1", job_dir=self.job_dir, lock_dir=lock_dir):
            pass
        with self.assertRaises(RepoLockHeld):
            with RepoLock(self.repo, holder="job-2", job_dir=self.root / "jobs" / "job-2", lock_dir=lock_dir):
                pass
        # Once the holder is terminal, the reservation is stale and may be replaced.
        st = self.state()
        st["phase"] = "ERROR"
        save_state(self.job_dir / "state.json", st)
        with RepoLock(self.repo, holder="job-2", job_dir=self.root / "jobs" / "job-2", lock_dir=lock_dir):
            pass

    def test_lock_prevents_concurrent_run_in_process(self):
        self.create()
        m, runner = self.machine([brain("DONE")])
        with JobLock(self.job_dir):
            with self.assertRaises(LockHeld):
                m.run()
            with self.assertRaises(LockHeld):
                m.approve()
        self.assertEqual(runner.roles, [])
        self.assertEqual(m.run(), "DONE")  # released

    def test_lock_prevents_concurrent_run_across_processes(self):
        self.create()
        holder = subprocess.Popen(
            [PY, "-c",
             "import fcntl, sys; fh = open(sys.argv[1], 'a+'); fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB);"
             "print('locked', flush=True); sys.stdin.readline()",
             str(self.job_dir / "lock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            m, runner = self.machine([brain("DONE")])
            with self.assertRaises(LockHeld):
                m.run()
            self.assertEqual(runner.roles, [])
        finally:
            holder.stdin.write("\n")
            holder.stdin.close()
            holder.wait(timeout=10)
        self.assertEqual(m.run(), "DONE")



    def test_refresh_evidence_allows_identical_diff_once(self):
        self.create(config={"test_commands": [[PY, "-c", "pass"]]})
        m, _ = self.machine([brain_next(), worker_ok(), reviewer("FAIL")])
        self.assertEqual(m.run(max_transitions=5), "PLANNING")
        st = self.state()
        batch = st["runtime"]["batch"]
        self.assertTrue(batch["diff_hashes"])
        m2 = Machine(self.job_dir, FakeRunner([]), clock=self.clock)
        self.assertEqual(m2.refresh_evidence("harness changed"), "EVIDENCE")
        self.assertEqual(m2.run(max_transitions=1), "REVIEWING")
        self.assertIsNone(self.state()["human_question"])

class DryRunTests(MachineTestCase):
    def test_dry_run_never_mutates_repo_or_runs_tests(self):
        marker = self.root / "tests-ran"
        self.create(config={"test_commands": [[PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]]})
        before = snapshot(self.repo)
        runner = DryRunRunner()
        m = Machine(self.job_dir, runner, dry_run=True, clock=self.clock)
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(snapshot(self.repo), before)
        self.assertFalse(marker.exists())
        self.assertEqual(runner.calls, ["brain", "worker", "reviewer", "brain"])
        self.assertEqual(m.status()["history"][0]["outcome"], "dry-run (not committed)")
        self.assertIn(("REVIEWING", "COMMITTING"), self.phases())
        self.assertEqual(self.state()["runtime"]["batch"], None)

    def test_dry_run_worker_never_gets_write_permission(self):
        self.create()
        m, runner = self.machine([brain_next(), worker_ok(), reviewer("PASS"), brain("DONE")], dry_run=True)
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(runner.calls[1]["permission_mode"], "plan")

    def test_dry_run_refuses_unsafe_runner(self):
        self.create()
        with self.assertRaises(MachineError):
            Machine(self.job_dir, lambda *a, **k: None, dry_run=True)


class RolePolicyTests(MachineTestCase):
    def test_machine_passes_role_policy_to_runner(self):
        self.create(config={"role_policy": {
            "brain": {"model": "sonnet", "effort": "low", "fallback_model": "opus", "max_budget_usd": 2.0}
        }})
        m, runner = self.machine([brain("NEEDS_HUMAN", human_question="stop")])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        call = runner.calls[0]
        self.assertEqual(call["model"], "sonnet")
        self.assertEqual(call["effort"], "low")
        self.assertEqual(call["fallback_model"], "opus")
        self.assertEqual(call["max_budget_usd"], 2.0)

    def test_old_runtime_without_role_policy_uses_defaults(self):
        self.create()
        state = self.state()
        del state["runtime"]["config"]["role_policy"]
        save_state(self.job_dir / "state.json", state)
        m, runner = self.machine([brain("NEEDS_HUMAN", human_question="stop")])
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIsNone(runner.calls[0]["model"])
        self.assertIsNone(runner.calls[0]["effort"])


class WorkerProviderTests(MachineTestCase):
    """config.worker_provider selects Claude vs. Codex for the Worker role only."""

    def _full_flow(self):
        return [
            brain_next(), writes(self.repo, "src/x.py", "1\n", worker_ok(files=["src/x.py"])),
            reviewer("PASS"), brain("DONE"),
        ]

    def test_default_worker_provider_is_auto_and_never_reaches_brain_or_reviewer(self):
        self.create()
        m, runner = self.machine(self._full_flow())
        self.assertEqual(m.run(), "DONE")
        brain1, worker, review, brain2 = runner.calls
        self.assertEqual(worker["worker_provider"], "auto")
        self.assertNotIn("worker_provider", brain1)
        self.assertNotIn("worker_provider", review)
        self.assertNotIn("worker_provider", brain2)

    def test_explicit_codex_selection_is_sent_only_to_the_worker_call(self):
        self.create(config={"worker_provider": "codex"})
        m, runner = self.machine(self._full_flow())
        self.assertEqual(m.run(), "DONE")
        brain1, worker, review, brain2 = runner.calls
        self.assertEqual(worker["worker_provider"], "codex")
        self.assertNotIn("worker_provider", brain1)
        self.assertNotIn("worker_provider", review)
        self.assertNotIn("worker_provider", brain2)

    def test_bad_worker_provider_rejected_at_job_creation(self):
        with self.assertRaises(MachineError):
            create_job(self.job_dir, self.job(), {"repo_path": str(self.repo), "worker_provider": "gpt5"})

    def test_old_runtime_without_worker_provider_defaults_to_claude(self):
        self.create()
        state = self.state()
        del state["runtime"]["config"]["worker_provider"]
        save_state(self.job_dir / "state.json", state)
        m, runner = self.machine(self._full_flow())
        self.assertEqual(m.run(), "DONE")
        self.assertEqual(runner.calls[1]["worker_provider"], "claude")

    def test_missing_codex_binary_fails_closed_to_needs_human(self):
        """The real codex adapter (not FakeRunner), wired through Machine via config."""
        self.create(config={"worker_provider": "codex", "caps": {"max_attempts": 1}})
        codex_run = build_runner(codex_bin="definitely-not-a-real-binary-xyz")

        def runner(role, prompt, **kwargs):
            if role == "brain":
                return fake_result(brain_next(), prompt, kwargs.get("cwd", "."))
            return codex_run(role, prompt, **kwargs)

        m = Machine(self.job_dir, runner, clock=self.clock)
        self.assertEqual(m.run(), "NEEDS_HUMAN")
        self.assertIn("codex binary not found", self.state()["human_question"])


class TelemetryTests(MachineTestCase):
    def test_success_call_persists_usage_and_non_resetting_totals(self):
        self.create()
        payload = brain("NEEDS_HUMAN", human_question="stop after telemetry")
        envelope = {
            "type": "result", "subtype": "success", "is_error": False,
            "structured_output": payload, "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 30,
                "cache_creation": {"ephemeral_1h_input_tokens": 25, "ephemeral_5m_input_tokens": 5},
            },
            "modelUsage": {"claude-sonnet-test": {"costUSD": 0.0123}},
            "num_turns": 3, "duration_ms": 1200, "duration_api_ms": 900,
            "session_id": "session-123", "stop_reason": "end_turn",
        }
        result = ClaudeResult(
            argv=["claude", "--print", "--model", "sonnet", "--effort", "low"],
            cwd=str(self.repo), exit_code=0, stdout=json.dumps(envelope), stderr="",
            duration_seconds=1.25, timed_out=False, structured=True, schema_requested=True,
            parsed_json=envelope, prompt_chars=321,
        )
        m, _ = self.machine([result])
        self.assertEqual(m.run(), "NEEDS_HUMAN")

        calls = [json.loads(line) for line in (self.job_dir / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["model_requested"], "sonnet")
        self.assertEqual(call["effort_requested"], "low")
        self.assertEqual(call["models_used"], ["claude-sonnet-test"])
        self.assertEqual(call["input_tokens"], 100)
        self.assertEqual(call["output_tokens"], 20)
        self.assertEqual(call["cache_read_input_tokens"], 70)
        self.assertEqual(call["cache_creation_1h_input_tokens"], 25)
        self.assertEqual(call["session_id"], "session-123")
        self.assertAlmostEqual(call["total_cost_usd"], 0.0123)

        state = self.state()
        totals = state["runtime"]["totals"]
        self.assertEqual(totals["claude_calls"], 1)
        self.assertEqual(totals["input_tokens"], 100)
        self.assertEqual(totals["cache_read_input_tokens"], 70)
        self.assertAlmostEqual(totals["cost_usd"], 0.0123)
        self.assertEqual(totals["by_role"]["brain"]["claude_calls"], 1)
        log = json.loads(next((self.job_dir / "logs").glob("*.json")).read_text())
        self.assertEqual(log["telemetry"]["num_turns"], 3)

    def test_old_runtime_without_totals_is_backward_compatible(self):
        self.create()
        state = self.state()
        del state["runtime"]["totals"]
        save_state(self.job_dir / "state.json", state)
        status = Machine(self.job_dir, runner=None, clock=self.clock).status()
        self.assertEqual(status["totals"]["claude_calls"], 0)


class HumanControlHardeningTests(MachineTestCase):
    def _pause(self, committed=False):
        self.create(config={"allowed_paths": ["src/lib/a.ts"]})
        state = self.state()
        state["phase"] = "NEEDS_HUMAN"
        state["human_question"] = "approve?"
        state["runtime"]["retry_phase"] = "COMMITTING"
        state["runtime"]["cap_hit"] = "max_claude_calls"
        state["runtime"]["human"] = {"question": "approve?", "asked_at": self.clock(), "answer": None, "answered_at": None}
        if committed:
            state["runtime"]["history"] = [{
                "step_id": "s1", "goal": "g", "outcome": "committed", "detail": "ok",
                "repairs": 0, "reviews": 1, "verdict": "PASS", "commit": "abc123",
                "paths": ["src/lib/a.ts"], "closed_at": self.clock(),
            }]
        save_state(self.job_dir / "state.json", state)

    def test_extend_scope_requires_pause_and_persists_unique_paths(self):
        self._pause()
        m = Machine(self.job_dir, runner=None, clock=self.clock)
        allowed = m.extend_scope(["src/routes/admin.ceo.tsx", "src/routes/admin.ceo.tsx"], "approved label")
        self.assertEqual(allowed, ["src/lib/a.ts", "src/routes/admin.ceo.tsx"])
        self.assertEqual(self.state()["phase"], "NEEDS_HUMAN")
        self.assertIn("src/routes/admin.ceo.tsx", self.state()["runtime"]["config"]["allowed_paths"])
        self.assertIn("scope extended by human", read_events(self.job_dir / "events.jsonl")[-1]["reason"])

    def test_set_config_requires_pause_and_updates_only_supported_fields(self):
        self._pause()
        m = Machine(self.job_dir, runner=None, clock=self.clock)
        cfg = m.set_config(
            test_commands=[[PY, "-c", "pass"]], test_timeout=45,
            caps={"max_claude_calls": 42}, answer="repair harness",
        )
        self.assertEqual(cfg["test_commands"], [[PY, "-c", "pass"]])
        self.assertEqual(cfg["test_timeout"], 45)
        self.assertEqual(cfg["caps"]["max_claude_calls"], 42)
        self.assertEqual(self.state()["phase"], "NEEDS_HUMAN")
        self.assertIn("config updated by human", read_events(self.job_dir / "events.jsonl")[-1]["reason"])
        with self.assertRaises(MachineError):
            m.set_config(test_timeout=0)

    def test_complete_requires_verified_committed_pass_and_clears_pause(self):
        self._pause(committed=True)
        m = Machine(self.job_dir, runner=None, clock=self.clock)
        self.assertEqual(m.complete("accepted bounded result"), "DONE")
        state = self.state()
        self.assertEqual(state["phase"], "DONE")
        self.assertIsNone(state["human_question"])
        self.assertIsNone(state["last_error"])
        self.assertIsNone(state["runtime"]["inflight"])
        self.assertIsNone(state["runtime"]["retry_phase"])
        self.assertIsNone(state["runtime"]["cap_hit"])

    def test_complete_refuses_without_committed_pass(self):
        self._pause(committed=False)
        with self.assertRaises(MachineError):
            Machine(self.job_dir, runner=None, clock=self.clock).complete()



if __name__ == "__main__":
    unittest.main()
