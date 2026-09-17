import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "process_chat_commands.py"
spec = importlib.util.spec_from_file_location("chat_commands", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


class Result:
    returncode = 0
    stdout = "ok"
    stderr = ""


def test_submit_is_idempotent_for_existing_job(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "JOBS_DIR", tmp_path / "jobs")
    cid = "12345678-1234-1234-1234-123456789abc"
    jid = mod.job_id_for(cid)
    (mod.JOBS_DIR / jid).mkdir(parents=True)
    seen = []

    def no_run(*args, **kwargs):
        raise AssertionError("existing job must not be submitted twice")

    receipt = mod.submit_command(
        {"command_id": cid, "message_id": "m1", "body": "What is running?"},
        run=no_run,
        ack_fn=lambda *args, **kwargs: seen.append((args, kwargs)),
    )
    assert receipt["job_id"] == jid
    assert seen[0][0] == (cid, "working")


def test_submit_creates_read_only_job(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "JOBS_DIR", tmp_path / "jobs")
    cid = "abcdef12-1234-1234-1234-123456789abc"
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return Result()

    mod.submit_command(
        {"command_id": cid, "message_id": "m2", "body": "Check the VM health"},
        run=runner,
        ack_fn=lambda *a, **k: None,
    )
    args = calls[0]
    assert args[args.index("--mode") + 1] == "read"
    assert "--goal" in args
    assert "Do not edit files" in args[args.index("--goal") + 1]


def test_terminal_done_uses_latest_history_detail():
    state = {"phase": "DONE", "runtime": {"history": [{"detail": "Everything is healthy."}]}}
    assert mod._terminal_update(state) == ("done", "Completed", "Everything is healthy.")


def test_terminal_needs_human_maps_to_needs_input():
    state = {"phase": "NEEDS_HUMAN", "runtime": {"human": {"question": "Which country?"}}}
    status, note, message = mod._terminal_update(state)
    assert status == "needs_input"
    assert note == "Needs your input"
    assert "Which country?" in message


def test_reconcile_terminal_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "receipts")
    monkeypatch.setattr(mod, "JOBS_DIR", tmp_path / "jobs")
    cid = "feedface-1234-1234-1234-123456789abc"
    jid = mod.job_id_for(cid)
    job = mod.JOBS_DIR / jid
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"phase":"ERROR","last_error":"boom"}))
    receipt = {"command_id":cid,"job_id":jid,"terminal_sent":False}
    seen=[]
    assert mod.reconcile_receipt(receipt, ack_fn=lambda *a, **k: seen.append((a,k))) is True
    assert seen[0][0][1] == "error"
    loaded = json.loads((mod.STATE_DIR / f"{cid}.json").read_text())
    assert loaded["terminal_sent"] is True
    assert mod.reconcile_receipt(loaded, ack_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError())) is False
