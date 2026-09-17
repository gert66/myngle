from lead_list_live import build_membership_payload, publish_prospect_membership


def test_membership_payload_deduplicates_ids_and_traces_batch():
    preflight = {
        "batch_id": "carla-korea-20260917-2250429a",
        "country": "south-korea",
        "caller": "Carla",
        "safety_preflight": "GREEN",
    }
    payload = build_membership_payload(["a", "a", "b"], preflight=preflight)
    assert payload["import_batch"] == preflight["batch_id"]
    assert payload["list_key"] == "carla-korea"
    assert payload["members"] == [
        {"company_id": "a", "assigned_caller": "Carla"},
        {"company_id": "b", "assigned_caller": "Carla"},
    ]


def test_membership_write_blocks_before_network_when_preflight_not_green():
    preflight = {"batch_id": "batch-1", "safety_preflight": "BLOCKED"}
    try:
        publish_prospect_membership(["a"], preflight=preflight, confirm_batch_id="batch-1")
    except RuntimeError as exc:
        assert "GREEN" in str(exc) or "blocked" in str(exc).lower()
    else:
        raise AssertionError("blocked preflight unexpectedly reached membership write")
