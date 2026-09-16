import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import slack_approval

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "process_approval_commands.py"
spec = importlib.util.spec_from_file_location("process_approval_commands", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ApprovalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = slack_approval.APPROVAL_DIR
        slack_approval.APPROVAL_DIR = Path(self.tmp.name)
        mod.update_status = slack_approval.update_status
        mod.record_deployment = slack_approval.record_deployment
        proposal = slack_approval.ApprovalProposal(
            proposal_id="FB-1", reporter="Carla", company="Acme", reported="Problem",
            diagnosis="Cause", prepared_fix="Fix", checks=["tests"], resolution_note="Done",
            reporter_reply="Thanks", changed_files=["x.py"], version=2,
        )
        slack_approval.save_proposal(proposal)
        self.fingerprint = proposal.fingerprint

    def tearDown(self):
        slack_approval.APPROVAL_DIR = self.old
        self.tmp.cleanup()

    def test_apply_approve_is_fingerprint_bound(self):
        row = mod.apply_command({"proposal_id":"FB-1", "fingerprint":self.fingerprint,
                                 "action":"approve", "created_by":"owner"})
        self.assertEqual(row["status"], "approved")
        with self.assertRaises(ValueError):
            mod.apply_command({"proposal_id":"FB-1", "fingerprint":"wrong",
                               "action":"approve", "created_by":"owner"})

    def test_finalized_decision_cannot_be_reversed(self):
        mod.apply_command({"proposal_id":"FB-1", "fingerprint":self.fingerprint,
                           "action":"approve", "created_by":"owner"})
        with self.assertRaises(ValueError):
            mod.apply_command({"proposal_id":"FB-1", "fingerprint":self.fingerprint,
                               "action":"reject", "created_by":"owner"})


    @mock.patch.object(mod, "deploy_approved", return_value={"status": "verified", "deployment_commit_sha": "c" * 40})
    @mock.patch.object(mod, "ack")
    @mock.patch.object(mod, "fetch_commands")
    def test_approve_runs_configured_deployment(self, fetch, ack, deploy):
        proposal = slack_approval.ApprovalProposal(
            proposal_id="FB-DEPLOY", reporter="Carla", company="Acme", reported="Problem",
            diagnosis="Cause", prepared_fix="Fix", checks=["tests"], resolution_note="Done",
            reporter_reply="Thanks", changed_files=["x.py"], version=1,
            deployment={"repo": "gert66/myngle-company-hub"},
        )
        slack_approval.save_proposal(proposal)
        fetch.return_value = [{"command_id":"c2", "proposal_id":"FB-DEPLOY", "fingerprint":proposal.fingerprint,
                               "action":"approve", "created_by":"owner"}]
        result = mod.process_once(url="https://example.test", token="t")
        self.assertEqual(result, [("c2", "executed")])
        deploy.assert_called_once()
        saved = slack_approval.load_record("FB-DEPLOY")
        self.assertEqual(saved["deployment_result"]["status"], "verified")

    @mock.patch.object(mod, "ack")
    @mock.patch.object(mod, "fetch_commands")
    def test_process_marks_stale_command(self, fetch, ack):
        fetch.return_value = [{"command_id":"c1", "proposal_id":"FB-1", "fingerprint":"wrong",
                               "action":"approve", "created_by":"owner"}]
        result = mod.process_once(url="https://example.test", token="t")
        self.assertEqual(result, [("c1", "stale")])
        ack.assert_called_once()
        self.assertEqual(ack.call_args.args[1], "stale")


if __name__ == "__main__":
    unittest.main()
