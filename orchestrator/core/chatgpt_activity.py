"""Bridge ChatGPT work into Orchestrator Control activity_records."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error

ORCH_ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ORCH_ROOT / "secrets" / "orchestrator_ingest_token"
OPS_PUSH_URL_FILE = ORCH_ROOT / "config" / "ops_push_url.txt"
STATE_DIR = ORCH_ROOT / "activity"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def activity_url(path=OPS_PUSH_URL_FILE):
    base = Path(path).read_text(encoding="utf-8").strip()
    suffix = "/api/public/orchestrator-snapshot"
    if not base.endswith(suffix):
        raise ValueError("ops_push_url.txt has unexpected format")
    return base[:-len(suffix)] + "/api/public/activity-upsert"


def _safe_key(value):
    return "".join(c if c.isalnum() or c in "-_.:" else "-" for c in value)[:200]


def _state_path(key, state_dir=STATE_DIR):
    return Path(state_dir) / f"{_safe_key(key)}.json"


def load_state(key, state_dir=STATE_DIR):
    return json.loads(_state_path(key, state_dir).read_text(encoding="utf-8"))


def save_state(payload, state_dir=STATE_DIR):
    path = _state_path(payload["external_key"], state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def post_activity(payload, *, url=None, token=None, timeout=15):
    url = url or activity_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "x-orchestrator-token": token,
    })
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (OSError, error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"activity push failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"activity push rejected: {result}")
    return result


def start(key, title, *, project=None, thread=None, summary=None, worker="ChatGPT", stale_minutes=10,
          url=None, token=None, state_dir=STATE_DIR):
    now = utc_now()
    payload = {
        "external_key": key,
        "title": title,
        "source_type": "chatgpt",
        "source_project": project,
        "source_thread": thread,
        "status": "active",
        "detail_status": "ChatGPT is working",
        "started_at": now,
        "last_activity_at": now,
        "worker": worker,
        "summary": summary,
        "stale_after_minutes": stale_minutes,
    }
    post_activity(payload, url=url, token=token)
    save_state(payload, state_dir=state_dir)
    return payload


def update(key, status, *, detail=None, summary=None, action_required=None, action_where=None,
           url=None, token=None, state_dir=STATE_DIR):
    payload = load_state(key, state_dir=state_dir)
    now = utc_now()
    payload["status"] = status
    payload["last_activity_at"] = now
    if detail is not None:
        payload["detail_status"] = detail
    if summary is not None:
        payload["summary"] = summary
    if action_required is not None:
        payload["action_required"] = action_required
    if action_where is not None:
        payload["action_where"] = action_where
    if status == "closed":
        payload["closed_at"] = now
    else:
        payload.pop("closed_at", None)
    post_activity(payload, url=url, token=token)
    save_state(payload, state_dir=state_dir)
    return payload


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m core.chatgpt_activity")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--key", required=True); s.add_argument("--title", required=True)
    s.add_argument("--project"); s.add_argument("--thread"); s.add_argument("--summary")
    s.add_argument("--worker", default="ChatGPT"); s.add_argument("--stale-minutes", type=int, default=10)
    for name in ("heartbeat", "waiting", "needs-action", "close"):
        q = sub.add_parser(name); q.add_argument("--key", required=True)
        q.add_argument("--detail"); q.add_argument("--summary")
        q.add_argument("--action-required"); q.add_argument("--action-where")
    args = p.parse_args(argv)
    if args.cmd == "start":
        result = start(args.key, args.title, project=args.project, thread=args.thread,
                       summary=args.summary, worker=args.worker, stale_minutes=args.stale_minutes)
    else:
        status = {"heartbeat":"active", "waiting":"waiting", "needs-action":"needs_action", "close":"closed"}[args.cmd]
        detail = args.detail or ({"heartbeat":"ChatGPT is working", "waiting":"Waiting", "needs-action":"Needs action", "close":"Completed"}[args.cmd])
        result = update(args.key, status, detail=detail, summary=args.summary,
                        action_required=args.action_required, action_where=args.action_where)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
