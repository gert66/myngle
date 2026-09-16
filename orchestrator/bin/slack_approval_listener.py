#!/usr/bin/env python3
"""Socket Mode listener for Sales Cockpit feedback approvals."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from core.slack_approval import load_record, parse_action_value, update_status  # noqa: E402

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError as exc:
    raise SystemExit("Install orchestrator/requirements-slack.txt first") from exc

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
ALLOWED_USERS = {x.strip() for x in os.environ.get("SLACK_APPROVER_USER_IDS", "").split(",") if x.strip()}

if not BOT_TOKEN.startswith("xoxb-"):
    raise SystemExit("SLACK_BOT_TOKEN must be an xoxb token")
if not APP_TOKEN.startswith("xapp-"):
    raise SystemExit("SLACK_APP_TOKEN must be an xapp token")

app = App(token=BOT_TOKEN, request_verification_enabled=False)

def _actor(body: dict) -> str:
    return str(body.get("user", {}).get("id", "unknown"))


def _allowed(body: dict) -> bool:
    return not ALLOWED_USERS or _actor(body) in ALLOWED_USERS


def _status_blocks(record: dict, label: str) -> list[dict]:
    proposal = record["proposal"]
    return [
        {"type": "header", "text": {"type": "plain_text", "text": label}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{proposal['reporter']}* · {proposal['company']}"}},
    ]


def _update_message(client, body: dict, record: dict, label: str) -> None:
    container = body.get("container", {})
    client.chat_update(
        channel=container["channel_id"],
        ts=container["message_ts"],
        text=label,
        blocks=_status_blocks(record, label),
    )

@app.action("feedback_approve")
def handle_approve(ack, body, client):
    ack()
    if not _allowed(body):
        return
    proposal_id, fingerprint = parse_action_value(body["actions"][0]["value"])
    record = update_status(proposal_id, fingerprint, "approved", _actor(body))
    _update_message(client, body, record, "Approved")


@app.action("feedback_reject")
def handle_reject(ack, body, client):
    ack()
    if not _allowed(body):
        return
    proposal_id, fingerprint = parse_action_value(body["actions"][0]["value"])
    record = update_status(proposal_id, fingerprint, "rejected", _actor(body))
    _update_message(client, body, record, "Rejected")


@app.action("feedback_discuss")
def handle_discuss(ack, body, client):
    ack()
    if not _allowed(body):
        return
    proposal_id, fingerprint = parse_action_value(body["actions"][0]["value"])
    private_metadata = json.dumps({
        "proposal_id": proposal_id,
        "fingerprint": fingerprint,
        "channel_id": body["container"]["channel_id"],
        "message_ts": body["container"]["message_ts"],
    })
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "feedback_discuss_submit",
            "private_metadata": private_metadata,
            "title": {"type": "plain_text", "text": "Discuss fix"},
            "submit": {"type": "plain_text", "text": "Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{
                "type": "input",
                "block_id": "discussion",
                "label": {"type": "plain_text", "text": "What should I check or change?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "note",
                    "multiline": True,
                },
            }],
        },
    )


@app.view("feedback_discuss_submit")
def handle_discuss_submit(ack, body, client, view):
    ack()
    if not _allowed(body):
        return
    meta = json.loads(view["private_metadata"])
    note = view["state"]["values"]["discussion"]["note"]["value"].strip()
    record = update_status(
        meta["proposal_id"],
        meta["fingerprint"],
        "discussion_requested",
        _actor(body),
        note=note,
    )
    client.chat_update(
        channel=meta["channel_id"],
        ts=meta["message_ts"],
        text="Discussion requested",
        blocks=_status_blocks(record, "Discussion requested"),
    )
    client.chat_postMessage(
        channel=meta["channel_id"],
        thread_ts=meta["message_ts"],
        text=f"Discussion note from <@{_actor(body)}>: {note}",
    )


def main() -> None:
    print("Sales Cockpit Slack approval listener starting", flush=True)
    SocketModeHandler(app, APP_TOKEN).start()


if __name__ == "__main__":
    main()
