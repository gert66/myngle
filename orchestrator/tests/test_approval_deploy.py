import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import approval_deploy
from core.approval_deploy import DeploymentError, validate_spec


class ApprovalDeployTests(unittest.TestCase):
    def base_record(self):
        return {
            "status": "approved",
            "proposal": {
                "risk": "GREEN",
                "changed_files": ["src/a.ts"],
                "deployment": {
                    "repo": "gert66/myngle-company-hub",
                    "source_branch": "work",
                    "commit_sha": "a" * 40,
                    "expected_main_sha": "b" * 40,
                    "lovable_project_id": "a4691ca7-4294-496a-af73-cdba24a5ac0f",
                    "test_commands": ["npm test"],
                    "verification_commands": ["curl -fsS https://myngle.whofirst.nl >/dev/null"],
                },
            },
        }

    def test_valid_exact_deployment_spec(self):
        spec = validate_spec(self.base_record())
        self.assertEqual(spec["source_branch"], "work")

    def test_legacy_proposal_has_no_deployment(self):
        row = self.base_record()
        row["proposal"].pop("deployment")
        self.assertEqual(validate_spec(row), {})

    def test_non_green_cannot_deploy(self):
        row = self.base_record()
        row["proposal"]["risk"] = "AMBER"
        with self.assertRaises(DeploymentError):
            validate_spec(row)

    def test_requires_tests_and_verification(self):
        row = self.base_record()
        row["proposal"]["deployment"]["verification_commands"] = []
        with self.assertRaises(DeploymentError):
            validate_spec(row)

    def test_prepare_dependencies_runs_npm_ci_for_package_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "package-lock.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(approval_deploy, "_run") as run:
                approval_deploy._prepare_dependencies(worktree)
            run.assert_called_once_with(
                ["npm", "ci", "--no-audit", "--no-fund"],
                cwd=worktree,
                timeout=900,
            )


if __name__ == "__main__":
    unittest.main()
