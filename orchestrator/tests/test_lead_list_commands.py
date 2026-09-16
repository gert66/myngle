import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "process_lead_list_commands.py"
spec = importlib.util.spec_from_file_location("process_lead_list_commands", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class LeadListCommandTests(unittest.TestCase):
    def test_commands_url_reuses_control_center_base(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "url.txt"
            path.write_text(
                "https://example.test/api/public/orchestrator-snapshot\n",
                encoding="utf-8",
            )
            self.assertEqual(
                mod.commands_url(path),
                "https://example.test/api/public/lead-list-commands",
            )

    @mock.patch.object(mod, "_download")
    def test_process_command_normalizes_company_first(self, download):
        def fake_download(_url, target):
            target.write_text(
                "Contact name;Work email;Company name;LinkedIn profile\n"
                "A;a@viseca.ch;Viseca;https://linkedin.com/in/a\n"
                "B;b@viseca.ch;Viseca;https://linkedin.com/in/b\n",
                encoding="utf-8",
            )
        download.side_effect = fake_download
        outcome = mod.process_command({
            "list_id": "11111111-1111-4111-8111-111111111111",
            "file_url": "https://signed.example/list.csv",
            "original_filename": "luana.csv",
            "country": "Switzerland",
            "cold_caller": "Luana",
            "name": "Luana Switzerland",
        })
        self.assertEqual(outcome["status"], "checking")
        self.assertEqual(outcome["intake_report"]["quality_status"], "GREEN")
        self.assertEqual(outcome["intake_report"]["decision"], "READY")
        self.assertEqual(outcome["intake_report"]["source_rows"], 2)
        self.assertEqual(outcome["intake_report"]["unique_companies"], 1)
        self.assertEqual(outcome["intake_report"]["companies_with_email_domain_hint"], 1)
        self.assertEqual(outcome["intake_report"]["companies_needing_domain"], 1)

    @mock.patch.object(mod, "ack")
    @mock.patch.object(mod, "process_command")
    @mock.patch.object(mod, "fetch_commands")
    def test_process_once_acks_success(self, fetch, process, ack):
        fetch.return_value = [{"list_id": "c1"}]
        process.return_value = {
            "list_id": "c1",
            "status": "checking",
            "status_note": "Intake ok",
            "intake_report": {"unique_companies": 3},
        }
        result = mod.process_once(url="https://example.test", token="t")
        self.assertEqual(result, [("c1", "executed")])
        self.assertEqual(ack.call_args.args[:2], ("c1", "checking"))
        self.assertEqual(ack.call_args.kwargs["status_note"], "Intake ok")
        self.assertEqual(ack.call_args.kwargs["intake_report"]["unique_companies"], 3)

    @mock.patch.object(mod, "ack")
    @mock.patch.object(mod, "fetch_commands")
    def test_unsupported_upload_is_stale_and_needs_review(self, fetch, ack):
        fetch.return_value = [{
            "list_id": "c1",
            "file_url": "https://example.test/x",
            "original_filename": "bad.pdf",
        }]
        result = mod.process_once(url="https://example.test", token="t")
        self.assertEqual(result, [("c1", "stale")])
        self.assertEqual(ack.call_args.args[:2], ("c1", "review_required"))


if __name__ == "__main__":
    unittest.main()
