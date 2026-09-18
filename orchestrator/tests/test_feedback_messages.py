import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "process_feedback_messages.py"
spec = importlib.util.spec_from_file_location("feedback_messages", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


class Result:
    returncode = 0
    stdout = "ok"
    stderr = ""


def test_messages_url_uses_control_center_base(tmp_path):
    path = tmp_path / "push-url.txt"
    path.write_text("https://control.whofirst.nl/api/public/orchestrator-snapshot\n")
    assert mod.messages_url(path) == "https://control.whofirst.nl/api/public/feedback-messages"


def test_immediate_reply_stays_conversational(monkeypatch):
    case = {
        "feedback_id": "abcdef12-3456-4abc-9def-123456789abc",
        "status": "needs_review",
        "comment": "Can you make this shorter?",
    }
    saved = []
    updates = []
    monkeypatch.setattr(mod, "load_case", lambda _fid: case)
    monkeypatch.setattr(mod, "save_case", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(mod, "decide_message", lambda *_a, **_k: {
        "mode": "reply",
        "assistant_reply": "Ja. Ik heb hem korter gemaakt.",
        "investigation_instruction": "",
        "proposed_reply": "Thanks, this is now fixed.",
    })
    monkeypatch.setattr(mod, "update_message", lambda *a, **k: updates.append((a, k)))

    result = mod.handle_pending({
        "message_id": "12345678-1234-1234-1234-123456789abc",
        "feedback_id": case["feedback_id"],
        "body": "Maak hem korter.",
        "thread": [],
    })
    assert result == "done"
    assert saved[-1]["proposed_reply"] == "Thanks, this is now fixed."
    assert updates[-1][0][1] == "done"
    assert updates[-1][1]["assistant_body"].startswith("Ja.")


def test_investigation_claims_message_and_starts_job(monkeypatch, tmp_path):
    case = {
        "feedback_id": "abcdef12-3456-4abc-9def-123456789abc",
        "status": "needs_review",
        "reporter_email": "nicole@example.com",
        "company": "Acme",
        "comment": "Please check the dialer",
        "iteration": 0,
    }
    saved = []
    updates = []
    monkeypatch.setattr(mod, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(mod, "load_case", lambda _fid: dict(saved[-1]) if saved else case)
    monkeypatch.setattr(mod, "save_case", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(mod, "decide_message", lambda *_a, **_k: {
        "mode": "investigate",
        "assistant_reply": "Ik zoek dit uit.",
        "investigation_instruction": "Trace the dialer issue without deploying.",
        "proposed_reply": "",
    })
    monkeypatch.setattr(mod, "update_message", lambda *a, **k: updates.append((a, k)))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: Result())

    result = mod.handle_pending({
        "message_id": "12345678-1234-1234-1234-123456789abc",
        "feedback_id": case["feedback_id"],
        "body": "Zoek dit uit.",
        "thread": [],
    })
    assert result == "processing"
    assert saved[-1]["conversation_message_id"] == "12345678-1234-1234-1234-123456789abc"
    assert saved[-1]["job_id"].startswith("feedback-abcdef12-c1")
    assert updates[-1][0][1] == "processing"


def test_processing_message_returns_plain_result(monkeypatch):
    case = {
        "feedback_id": "abcdef12-3456-4abc-9def-123456789abc",
        "status": "investigating",
        "job_id": "feedback-abcdef12-c1",
        "conversation_job_id": "feedback-abcdef12-c1",
        "conversation_message_id": "12345678-1234-1234-1234-123456789abc",
    }
    state = {"phase": "DONE", "runtime": {"history": [{"verdict": "PASS", "detail": "technical stuff"}]}}
    saved = []
    updates = []
    monkeypatch.setattr(mod, "load_case", lambda _fid: case)
    monkeypatch.setattr(mod, "_job_state", lambda _jid: state)
    monkeypatch.setattr(mod, "reconcile_case", lambda value: value)
    monkeypatch.setattr(mod, "summarize_result", lambda *_a, **_k: {
        "assistant_reply": "Ik heb het uitgezocht. Er hoeft niets in productie te veranderen.",
        "proposed_reply": "Hi Nicole, I checked this and no product change is needed.",
    })
    monkeypatch.setattr(mod, "save_case", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(mod, "update_message", lambda *a, **k: updates.append((a, k)))

    result = mod.handle_processing({
        "message_id": case["conversation_message_id"],
        "feedback_id": case["feedback_id"],
        "status": "processing",
        "thread": [],
    })
    assert result == "done"
    assert saved[-1]["proposed_reply"].startswith("Hi Nicole")
    assert saved[-1]["conversation_message_id"] is None
    assert updates[-1][1]["assistant_body"].startswith("Ik heb het uitgezocht")


def test_internal_blocker_is_retried_once_without_asking_owner(monkeypatch):
    mid="12345678-1234-1234-1234-123456789abc"
    fid="abcdef12-3456-4abc-9def-123456789abc"
    case={
        "feedback_id":fid, "status":"investigating", "job_mode":"write",
        "job_id":"feedback-abcdef12-c1", "conversation_job_id":"feedback-abcdef12-c1",
        "conversation_message_id":mid, "conversation_infra_retry_count":0,
    }
    state={"phase":"NEEDS_HUMAN","human_question":"Branch freshness gate blocked write job: clean_tree"}
    saved=[]; updates=[]
    monkeypatch.setattr(mod, "load_case", lambda _fid: dict(saved[-1]) if saved else dict(case))
    monkeypatch.setattr(mod, "save_case", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(mod, "_job_state", lambda _jid: state)
    monkeypatch.setattr(mod, "_submit_follow_up", lambda *_a, **_k: "feedback-abcdef12-c2")
    monkeypatch.setattr(mod, "update_message", lambda *a, **k: updates.append((a,k)))
    result=mod.handle_processing({"message_id":mid,"feedback_id":fid,"status":"processing","body":"Ga verder","thread":[]})
    assert result == "retrying"
    assert saved[-1]["conversation_job_id"] == "feedback-abcdef12-c2"
    assert saved[-1]["conversation_infra_retry_count"] == 1
    assert updates[-1][0][1] == "processing"
    assert "assistant_body" not in updates[-1][1]


def test_internal_blocker_retry_cap_never_becomes_owner_question(monkeypatch):
    mid="12345678-1234-1234-1234-123456789abc"
    fid="abcdef12-3456-4abc-9def-123456789abc"
    case={
        "feedback_id":fid, "status":"investigating", "job_mode":"write",
        "job_id":"feedback-abcdef12-c2", "conversation_job_id":"feedback-abcdef12-c2",
        "conversation_message_id":mid, "conversation_infra_retry_count":1,
    }
    state={"phase":"ERROR","last_error":"Git preflight failed: repository configuration"}
    saved=[]; updates=[]
    monkeypatch.setattr(mod, "load_case", lambda _fid: case)
    monkeypatch.setattr(mod, "save_case", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(mod, "_job_state", lambda _jid: state)
    monkeypatch.setattr(mod, "update_message", lambda *a, **k: updates.append((a,k)))
    result=mod.handle_processing({"message_id":mid,"feedback_id":fid,"status":"processing","thread":[]})
    assert result == "done"
    assert saved[-1]["question"] is None
    assert "hoeft" in updates[-1][1]["assistant_body"].lower()
