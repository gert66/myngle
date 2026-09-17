import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "process_feedback_commands.py"
spec = importlib.util.spec_from_file_location("feedback_commands", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


class Result:
    returncode = 0
    stdout = "ok"
    stderr = ""


def test_commands_url_uses_control_center_base(tmp_path):
    path = tmp_path / "push-url.txt"
    path.write_text("https://control.whofirst.nl/api/public/orchestrator-snapshot\n")
    assert mod.commands_url(path) == "https://control.whofirst.nl/api/public/feedback-commands"

def test_follow_up_queues_new_write_job(monkeypatch, tmp_path):
    case = {
        "feedback_id": "abcdef12-3456-4abc-9def-123456789abc",
        "reporter_email": "nicole@example.com",
        "company": "Acme",
        "comment": "Please add another outcome option",
        "status": "needs_review",
        "iteration": 0,
    }
    monkeypatch.setattr(mod, "JOBS_DIR", tmp_path / "jobs")
    seen = []
    saved = []

    def runner(args, **kwargs):
        seen.append(args)
        return Result()

    monkeypatch.setattr(mod, "save_case", lambda value: saved.append(dict(value)))
    jid = mod._submit_follow_up(case, "Make the comment optional", run=runner)
    args = seen[0]
    assert jid.startswith("feedback-abcdef12-r1")
    assert args[args.index("--mode") + 1] == "write"
    assert "Make the comment optional" in args[args.index("--goal") + 1]
    assert saved[-1]["status"] == "investigating"
    assert saved[-1]["human_follow_up"] == "Make the comment optional"

def test_send_close_resolves_case_with_human_reply(monkeypatch):
    case = {
        "feedback_id": "abcdef12-3456-4abc-9def-123456789abc",
        "status": "needs_review",
        "source_updated_at": "2026-09-17T10:00:00Z",
        "proposal_id": None,
    }
    saved = []
    monkeypatch.setattr(mod, "load_case", lambda _fid: case)
    monkeypatch.setattr(mod, "_ensure_verified_deployment", lambda *_a, **_k: "no deploy")
    monkeypatch.setattr(mod, "_post_resolution", lambda *_a, **_k: {
        "status": "resolved",
        "row": {"resolved_at": "2026-09-17T12:00:00Z"},
    })
    monkeypatch.setattr(mod, "save_case", lambda value: saved.append(dict(value)))

    note = mod.apply_command({
        "command_id": "12345678-1234-1234-1234-123456789abc",
        "feedback_id": case["feedback_id"],
        "action": "send_close",
        "body": "Thanks Nicole, this is fixed now.",
    })
    assert "case closed" in note
    assert saved[-1]["status"] == "resolved"
    assert saved[-1]["resolution_note"] == "Thanks Nicole, this is fixed now."
    assert saved[-1]["closed_at"] == "2026-09-17T12:00:00Z"
