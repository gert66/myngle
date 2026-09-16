import json

from core.slack_approval import ApprovalProposal, build_blocks, parse_action_value, proposal_hash


def sample():
    return ApprovalProposal(
        proposal_id="FB-1", reporter="Carla", company="Acme",
        reported="Broken", diagnosis="Mismatch", prepared_fix="Align mapping",
        checks=["test passed"], resolution_note="Fixed mapping",
        reporter_reply="Thanks, fixed.", changed_files=["app.py"],
    )


def test_fingerprint_changes_with_fix():
    a = sample()
    b = sample()
    b.prepared_fix = "Different fix"
    assert a.fingerprint != b.fingerprint


def test_action_value_roundtrip():
    p = sample()
    blocks = build_blocks(p)
    actions = next(block for block in blocks if block["type"] == "actions")
    value = actions["elements"][0]["value"]
    assert parse_action_value(value) == (p.proposal_id, p.fingerprint)


def test_proposal_hash_stable():
    assert proposal_hash({"b": 2, "a": 1}) == proposal_hash({"a": 1, "b": 2})
