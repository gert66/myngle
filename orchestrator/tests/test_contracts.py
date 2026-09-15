import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import contracts  # noqa: E402
from core.contracts import (  # noqa: E402
    BRAIN_DECISIONS, CONTRACT_NAMES, ContractError, FINDING_SEVERITIES, JOB_MODES, PHASES,
    REVIEWER_VERDICTS, WORKER_STATUSES, load_schema, validate, validate_brain, validate_job,
    validate_reviewer, validate_state, validate_worker,
)

BATCH = {
    "step_id": "step-01",
    "goal": "Add the empty AM workspace page",
    "instructions": "Create the route and a placeholder component. Do not touch auth.",
    "acceptance_criteria": ["route renders", "tests pass"],
}

VALID = {
    "brain": {
        "schema_version": 1,
        "decision": "NEXT",
        "reason": "Previous batch passed review.",
        "next_batch": BATCH,
        "human_question": None,
        "wait_seconds": None,
    },
    "worker": {
        "schema_version": 1,
        "step_id": "step-01",
        "status": "COMPLETED",
        "summary": "Added route and placeholder component.",
        "files_changed": ["app/routes/am.tsx"],
        "tests_run": ["npm test"],
        "tests_passed": True,
        "blockers": [],
    },
    "reviewer": {
        "schema_version": 1,
        "step_id": "step-01",
        "verdict": "PASS",
        "scope_ok": True,
        "tests_ok": True,
        "summary": "Matches the batch goal; tests green.",
        "findings": [{"severity": "MINOR", "message": "Missing docstring", "file": "app/routes/am.tsx"}],
    },
    "job": {
        "schema_version": 1,
        "job_id": "am-workspace-001",
        "repo": "gert66/myngle",
        "branch": "work",
        "mode": "write",
        "goal": "Build the safe BUILD NOW items of AM Workspace.",
        "created_at": "2026-09-13T14:07:00Z",
        "input_files": ["input/am-workspace-20260913/AM_Workspace_Build_Brief_2026-09-13.txt"],
        "max_steps": 20,
        "max_repairs_per_step": 2,
    },
    "state": {
        "schema_version": 1,
        "job_id": "am-workspace-001",
        "phase": "REVIEWING",
        "step_id": "step-01",
        "attempt": 1,
        "created_at": "2026-09-13T14:07:00Z",
        "updated_at": "2026-09-13T14:30:00Z",
        "last_error": None,
    },
}


def brain(**overrides):
    obj = copy.deepcopy(VALID["brain"])
    obj.update(overrides)
    return obj


def reviewer(**overrides):
    obj = copy.deepcopy(VALID["reviewer"])
    obj.update(overrides)
    return obj


class CommonContractTests(unittest.TestCase):
    def test_valid_examples_pass_and_return_copies(self):
        for name in CONTRACT_NAMES:
            with self.subTest(contract=name):
                original = copy.deepcopy(VALID[name])
                result = validate(name, VALID[name])
                self.assertIsNot(result, VALID[name])
                self.assertEqual(VALID[name], original)  # input untouched
                for key, value in original.items():
                    self.assertEqual(result[key], value)

    def test_unknown_top_level_key_rejected(self):
        for name in CONTRACT_NAMES:
            with self.subTest(contract=name):
                obj = dict(VALID[name], surprise=1)
                with self.assertRaises(ContractError) as ctx:
                    validate(name, obj)
                self.assertIn("unknown keys ['surprise']", str(ctx.exception))

    def test_missing_required_key_rejected(self):
        for name in CONTRACT_NAMES:
            with self.subTest(contract=name):
                obj = dict(VALID[name])
                del obj["schema_version"]
                with self.assertRaises(ContractError) as ctx:
                    validate(name, obj)
                self.assertIn("schema_version", str(ctx.exception))

    def test_wrong_schema_version_rejected(self):
        for name in CONTRACT_NAMES:
            with self.subTest(contract=name):
                with self.assertRaises(ContractError):
                    validate(name, dict(VALID[name], schema_version=2))

    def test_non_object_rejected(self):
        for name in CONTRACT_NAMES:
            with self.subTest(contract=name):
                with self.assertRaises(ContractError):
                    validate(name, ["not", "a", "dict"])

    def test_bool_is_not_accepted_as_int(self):
        with self.assertRaises(ContractError):
            validate_state(dict(VALID["state"], attempt=True))

    def test_unknown_contract_name_rejected(self):
        with self.assertRaises(ContractError):
            validate("planner", {})


class BrainTests(unittest.TestCase):
    def test_all_decisions_have_valid_form(self):
        forms = {
            "NEXT": brain(),
            "REPAIR": brain(decision="REPAIR"),
            "REVIEW_AGAIN": brain(decision="REVIEW_AGAIN", next_batch=None),
            "WAIT": brain(decision="WAIT", next_batch=None, wait_seconds=300),
            "NEEDS_HUMAN": brain(decision="NEEDS_HUMAN", next_batch=None, human_question="Which tenant?"),
            "DONE": brain(decision="DONE", next_batch=None),
            "ERROR": brain(decision="ERROR", next_batch=None),
        }
        self.assertEqual(set(forms), set(BRAIN_DECISIONS))
        for decision, obj in forms.items():
            with self.subTest(decision=decision):
                self.assertEqual(validate_brain(obj)["decision"], decision)

    def test_optional_null_keys_may_be_omitted(self):
        obj = {"schema_version": 1, "decision": "DONE", "reason": "All batches done."}
        self.assertEqual(validate_brain(obj)["decision"], "DONE")

    def test_invalid_decision_rejected(self):
        with self.assertRaises(ContractError):
            validate_brain(brain(decision="CONTINUE"))

    def test_next_missing_next_batch_rejected(self):
        for decision in ("NEXT", "REPAIR"):
            with self.subTest(decision=decision):
                with self.assertRaises(ContractError) as ctx:
                    validate_brain(brain(decision=decision, next_batch=None))
                self.assertIn("next_batch is required", str(ctx.exception))
                obj = brain(decision=decision)
                del obj["next_batch"]
                with self.assertRaises(ContractError):
                    validate_brain(obj)

    def test_non_next_with_next_batch_rejected(self):
        for decision in ("REVIEW_AGAIN", "WAIT", "NEEDS_HUMAN", "DONE", "ERROR"):
            with self.subTest(decision=decision):
                obj = brain(decision=decision, next_batch=BATCH)
                if decision == "WAIT":
                    obj["wait_seconds"] = 120
                if decision == "NEEDS_HUMAN":
                    obj["human_question"] = "?"
                with self.assertRaises(ContractError) as ctx:
                    validate_brain(obj)
                self.assertIn("next_batch must be null", str(ctx.exception))

    def test_next_batch_is_validated_strictly(self):
        with self.assertRaises(ContractError):
            validate_brain(brain(next_batch=dict(BATCH, extra="x")))
        with self.assertRaises(ContractError):
            validate_brain(brain(next_batch={"step_id": "s", "goal": "g"}))
        with self.assertRaises(ContractError):
            validate_brain(brain(next_batch=dict(BATCH, step_id="bad id/with slash")))
        with self.assertRaises(ContractError):
            validate_brain(brain(next_batch=dict(BATCH, acceptance_criteria=[1])))

    def test_needs_human_missing_question_rejected(self):
        for question in (None, "", "   "):
            with self.subTest(question=question):
                with self.assertRaises(ContractError) as ctx:
                    validate_brain(brain(decision="NEEDS_HUMAN", next_batch=None, human_question=question))
                self.assertIn("human_question is required", str(ctx.exception))

    def test_question_on_other_decision_rejected(self):
        with self.assertRaises(ContractError):
            validate_brain(brain(human_question="Why?"))

    def test_wait_seconds_range(self):
        for seconds in (60, 3600, 1234):
            with self.subTest(seconds=seconds):
                obj = validate_brain(brain(decision="WAIT", next_batch=None, wait_seconds=seconds))
                self.assertEqual(obj["wait_seconds"], seconds)
        for seconds in (None, 0, 59, 3601, -5):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ContractError):
                    validate_brain(brain(decision="WAIT", next_batch=None, wait_seconds=seconds))
        with self.assertRaises(ContractError):
            validate_brain(brain(decision="WAIT", next_batch=None, wait_seconds="120"))

    def test_wait_seconds_on_other_decision_rejected(self):
        with self.assertRaises(ContractError):
            validate_brain(brain(wait_seconds=120))

    def test_empty_reason_rejected(self):
        with self.assertRaises(ContractError):
            validate_brain(brain(reason=""))


class WorkerTests(unittest.TestCase):
    def test_all_statuses_accepted(self):
        for status in WORKER_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(validate_worker(dict(VALID["worker"], status=status))["status"], status)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ContractError):
            validate_worker(dict(VALID["worker"], status="DONE"))

    def test_minimal_worker_passes(self):
        obj = {"schema_version": 1, "step_id": "step-01", "status": "FAILED", "summary": "Build broke."}
        self.assertEqual(validate_worker(obj), obj)

    def test_bad_types_rejected(self):
        with self.assertRaises(ContractError):
            validate_worker(dict(VALID["worker"], files_changed="app.tsx"))
        with self.assertRaises(ContractError):
            validate_worker(dict(VALID["worker"], files_changed=[1]))
        with self.assertRaises(ContractError):
            validate_worker(dict(VALID["worker"], tests_passed="yes"))


class ReviewerTests(unittest.TestCase):
    def test_clean_pass_is_not_degraded(self):
        result = validate_reviewer(VALID["reviewer"])
        self.assertEqual(result["verdict"], "PASS")
        self.assertFalse(result["verdict_degraded"])
        self.assertEqual(result["degrade_reasons"], [])
        # Output is itself a valid reviewer object.
        self.assertEqual(validate_reviewer(result), result)

    def test_pass_with_blocker_becomes_fail(self):
        obj = reviewer(findings=[{"severity": "BLOCKER", "message": "Writes to main branch"}])
        result = validate_reviewer(obj)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(result["verdict_degraded"])
        self.assertEqual(result["degrade_reasons"], ["1 BLOCKER finding(s)"])
        self.assertEqual(obj["verdict"], "PASS")  # input untouched

    def test_pass_with_scope_not_ok_becomes_fail(self):
        result = validate_reviewer(reviewer(scope_ok=False))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(result["verdict_degraded"])
        self.assertEqual(result["degrade_reasons"], ["scope_ok is false"])

    def test_pass_with_tests_not_ok_becomes_fail(self):
        result = validate_reviewer(reviewer(tests_ok=False))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["degrade_reasons"], ["tests_ok is false"])

    def test_all_reasons_are_reported(self):
        obj = reviewer(scope_ok=False, tests_ok=False,
                       findings=[{"severity": "BLOCKER", "message": "a"}, {"severity": "BLOCKER", "message": "b"}])
        result = validate_reviewer(obj)
        self.assertEqual(result["degrade_reasons"],
                         ["scope_ok is false", "tests_ok is false", "2 BLOCKER finding(s)"])

    def test_input_degrade_flags_are_overwritten(self):
        result = validate_reviewer(reviewer(verdict_degraded=True, degrade_reasons=["made up"]))
        self.assertFalse(result["verdict_degraded"])
        self.assertEqual(result["degrade_reasons"], [])

    def test_fail_is_not_degraded(self):
        result = validate_reviewer(reviewer(verdict="FAIL", scope_ok=False))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertFalse(result["verdict_degraded"])

    def test_needs_human_requires_question(self):
        with self.assertRaises(ContractError):
            validate_reviewer(reviewer(verdict="NEEDS_HUMAN"))
        result = validate_reviewer(reviewer(verdict="NEEDS_HUMAN", human_question="Is X in scope?"))
        self.assertEqual(result["verdict"], "NEEDS_HUMAN")

    def test_invalid_verdict_and_findings_rejected(self):
        with self.assertRaises(ContractError):
            validate_reviewer(reviewer(verdict="OK"))
        with self.assertRaises(ContractError):
            validate_reviewer(reviewer(findings=[{"severity": "CRITICAL", "message": "x"}]))
        with self.assertRaises(ContractError):
            validate_reviewer(reviewer(findings=[{"severity": "MINOR", "message": "x", "line": 3}]))
        with self.assertRaises(ContractError):
            validate_reviewer(reviewer(findings=[{"severity": "MINOR"}]))
        with self.assertRaises(ContractError):
            validate_reviewer(reviewer(scope_ok="yes"))


class JobTests(unittest.TestCase):
    def test_minimal_job_passes(self):
        obj = {
            "schema_version": 1, "job_id": "smoke", "repo": "gert66/myngle", "branch": "work",
            "mode": "read", "goal": "Smoke test.", "created_at": "2026-09-13T14:07:00Z",
        }
        self.assertEqual(validate_job(obj), obj)

    def test_invalid_values_rejected(self):
        for bad in (
            {"mode": "WRITE"},
            {"job_id": "../escape"},
            {"job_id": ""},
            {"created_at": "yesterday"},
            {"max_steps": 0},
            {"max_repairs_per_step": -1},
            {"branch": ""},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    validate_job(dict(VALID["job"], **bad))


class StateContractTests(unittest.TestCase):
    def test_all_phases_accepted(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                validate_state(dict(VALID["state"], phase=phase))

    def test_invalid_phase_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            validate_state(dict(VALID["state"], phase="RUNNING"))
        self.assertIn("phase", str(ctx.exception))

    def test_optional_fields_accepted(self):
        obj = dict(VALID["state"], phase="WAITING", wait_until="2026-09-13T15:00:00Z", human_question=None)
        self.assertEqual(validate_state(obj)["wait_until"], "2026-09-13T15:00:00Z")

    def test_invalid_values_rejected(self):
        for bad in (
            {"attempt": -1},
            {"step_id": "bad/step"},
            {"updated_at": "soon"},
            {"wait_until": "later"},
            {"last_error": 5},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    validate_state(dict(VALID["state"], **bad))


class SchemaFileTests(unittest.TestCase):
    """The JSON schema files are reference docs; keep them in lockstep with the code."""

    ENUMS = {
        "brain": ("decision", BRAIN_DECISIONS),
        "worker": ("status", WORKER_STATUSES),
        "reviewer": ("verdict", REVIEWER_VERDICTS),
        "job": ("mode", JOB_MODES),
        "state": ("phase", PHASES),
    }

    def test_schema_files_are_valid_json(self):
        for name in CONTRACT_NAMES:
            with self.subTest(contract=name):
                path = contracts.SCHEMAS_DIR / f"{name}.schema.json"
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(schema, load_schema(name))
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])

    def test_schema_keys_match_field_tables(self):
        for name in CONTRACT_NAMES:
            with self.subTest(contract=name):
                schema = load_schema(name)
                fields = contracts.CONTRACT_FIELDS[name]
                self.assertEqual(set(schema["properties"]), set(fields))
                required = sorted(k for k, (_, req) in fields.items() if req)
                self.assertEqual(sorted(schema["required"]), required)
                self.assertEqual(schema["properties"]["schema_version"], {"const": contracts.SCHEMA_VERSION})

    def test_schema_enums_match_code(self):
        for name, (key, values) in self.ENUMS.items():
            with self.subTest(contract=name):
                self.assertEqual(load_schema(name)["properties"][key]["enum"], list(values))
        self.assertEqual(load_schema("reviewer")["$defs"]["finding"]["properties"]["severity"]["enum"],
                         list(FINDING_SEVERITIES))
        batch = load_schema("brain")["$defs"]["batch"]
        self.assertEqual(set(batch["properties"]), set(contracts.BATCH_FIELDS))
        finding = load_schema("reviewer")["$defs"]["finding"]
        self.assertEqual(set(finding["properties"]), set(contracts.FINDING_FIELDS))

    def test_wait_seconds_bounds_match_code(self):
        prop = load_schema("brain")["properties"]["wait_seconds"]
        self.assertEqual(prop["minimum"], contracts.WAIT_SECONDS_MIN)
        self.assertEqual(prop["maximum"], contracts.WAIT_SECONDS_MAX)

    def test_unknown_schema_name_rejected(self):
        with self.assertRaises(ContractError):
            load_schema("planner")


if __name__ == "__main__":
    unittest.main()
