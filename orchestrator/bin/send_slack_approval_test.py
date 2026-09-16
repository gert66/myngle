#!/usr/bin/env python3
"""Send a test approval card to the Sales Cockpit approvals channel."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from core.slack_approval import ApprovalProposal, build_blocks, save_proposal  # noqa: E402

try:
    from slack_sdk import WebClient
except ImportError as exc:
    raise SystemExit("Install orchestrator/requirements-slack.txt first") from exc

CHANNEL = os.environ.get("SLACK_APPROVAL_CHANNEL_ID", "C0C2ACZ1R0U")
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")


def main() -> None:
    proposal = ApprovalProposal(
        proposal_id="TEST-001",
        reporter="Carla",
        company="Demo company",
        reported="Test feedback for the approval flow.",
        diagnosis="This is a connectivity test. No production bug is being changed.",
        prepared_fix="A test approval card has been prepared. Approving it only changes the local test record.",
        checks=["Slack bot token loaded", "Socket Mode app token loaded", "No production change"],
        resolution_note="Approval workflow connectivity test completed successfully.",
        reporter_reply="Hi Carla, this is only a test message. No action is required.",
        changed_files=["orchestrator/core/slack_approval.py"],
        risk="GREEN",
    )
    save_proposal(proposal)
    client = WebClient(token=BOT_TOKEN)
    response = client.chat_postMessage(
        channel=CHANNEL,
        text="Feedback ready for approval",
        blocks=build_blocks(proposal),
    )
    print("sent", response.get("ok"), response.get("channel"), response.get("ts"))


if __name__ == "__main__":
    main()
