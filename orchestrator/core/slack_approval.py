"""Slack approval gate for Sales Cockpit feedback fixes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APPROVAL_DIR = Path(os.getenv("SALES_COCKPIT_APPROVAL_DIR", "/home/myngle/orchestrator/approvals"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def proposal_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _path(proposal_id: str) -> Path:
    safe = "".join(c for c in proposal_id if c.isalnum() or c in "-_" )
    if not safe or safe != proposal_id:
        raise ValueError("invalid proposal id")
    return APPROVAL_DIR / f"{safe}.json"

@dataclass
class ApprovalProposal:
    proposal_id: str
    reporter: str
    company: str
    reported: str
    diagnosis: str
    prepared_fix: str
    checks: list[str]
    resolution_note: str
    reporter_reply: str
    changed_files: list[str]
    risk: str = "GREEN"
    version: int = 1
    deployment: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return proposal_hash(self.payload())


def save_proposal(proposal: ApprovalProposal, status: str = "pending") -> Path:
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "proposal": proposal.payload(),
        "fingerprint": proposal.fingerprint,
        "status": status,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    target = _path(proposal.proposal_id)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name, dir=str(APPROVAL_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


def load_record(proposal_id: str) -> dict[str, Any]:
    with _path(proposal_id).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def update_status(proposal_id: str, fingerprint: str, status: str, actor: str, note: str = "") -> dict[str, Any]:
    record = load_record(proposal_id)
    if record.get("fingerprint") != fingerprint:
        raise ValueError("proposal changed; approval is stale")
    current = str(record.get("status") or "pending")
    if current in {"approved", "rejected"} and status != current:
        raise ValueError(f"proposal already finalized as {current}")
    if status not in {"pending", "discussion_requested", "approved", "rejected"}:
        raise ValueError("invalid approval status")
    record["status"] = status
    record["updated_at"] = utc_now()
    record["actor"] = actor
    if note:
        record["note"] = note
    target = _path(proposal_id)
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record



def record_deployment(proposal_id: str, fingerprint: str, **fields: Any) -> dict[str, Any]:
    record = load_record(proposal_id)
    if record.get("fingerprint") != fingerprint:
        raise ValueError("proposal changed; deployment result is stale")
    deployment = dict(record.get("deployment_result") or {})
    deployment.update(fields)
    deployment["updated_at"] = utc_now()
    record["deployment_result"] = deployment
    record["updated_at"] = utc_now()
    target = _path(proposal_id)
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record

def build_blocks(proposal: ApprovalProposal) -> list[dict[str, Any]]:
    action_value = json.dumps({
        "proposal_id": proposal.proposal_id,
        "fingerprint": proposal.fingerprint,
    }, separators=(",", ":"))
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Fix ready for approval"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{proposal.reporter}* · {proposal.company}\n{proposal.reported}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*My conclusion*\n{proposal.diagnosis}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Prepared fix*\n{proposal.prepared_fix}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Resolution note*\n{proposal.resolution_note}\n\n*Reply to {proposal.reporter}*\n{proposal.reporter_reply}"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary", "action_id": "feedback_approve", "value": action_value},
                {"type": "button", "text": {"type": "plain_text", "text": "Discuss"}, "action_id": "feedback_discuss", "value": action_value},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"}, "style": "danger", "action_id": "feedback_reject", "value": action_value},
            ],
        },
    ]


def parse_action_value(raw: str) -> tuple[str, str]:
    value = json.loads(raw)
    proposal_id = str(value["proposal_id"])
    fingerprint = str(value["fingerprint"])
    return proposal_id, fingerprint
