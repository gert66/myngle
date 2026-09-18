import tempfile
import unittest
from pathlib import Path

from lead_list_safety import (
    annotate_import_provenance,
    build_enrichment_plan,
    build_change_ledger,
    build_import_plan,
    prematch_companies,
    prematch_export_records,
    reconcile_export_ids,
    reconcile_export_with_import_plan,
    selective_rollback,
    validate_import_plan,
    write_before_snapshot,
)


def _existing():
    items = [
        {"company_id": "samsung-com", "company_name": "Samsung Electronics Co., Ltd.", "domain": "samsung.com"},
        {"company_id": "hanwha-group", "company_name": "Hanwha Group", "domain": ""},
    ]
    details = {"hanwha-group": {"phone": "+82 2 1234 5678"}}
    return items, details


def test_exact_domain_reuses_existing_id():
    existing, details = _existing()
    report = prematch_companies([
        {"company_key": "k1", "company_name": "Samsung Electronics", "domain": "samsung.com"},
    ], existing, existing_details=details)
    assert report["summary"]["matched_existing"] == 1
    assert report["entries"][0]["existing_company_id"] == "samsung-com"


def test_name_only_is_ambiguous_not_automatic():
    existing, details = _existing()
    report = prematch_companies([
        {"company_key": "k1", "company_name": "Hanwha Group", "domain": ""},
    ], existing, existing_details=details)
    assert report["summary"]["ambiguous"] == 1
    assert report["entries"][0]["match_basis"] == "exact_name_only"


def test_name_and_phone_can_match_without_domain():
    existing, details = _existing()
    report = prematch_companies([
        {"company_key": "k1", "company_name": "Hanwha Group", "domain": ""},
    ], existing, normalized_rows=[{"company_key": "k1", "direct_phone": "+82 2 1234 5678"}], existing_details=details)
    assert report["summary"]["matched_existing"] == 1
    assert report["entries"][0]["match_basis"] == "exact_name_and_phone"


def test_reconcile_provenance_ledger_and_selective_rollback():
    existing, details = _existing()
    new_items = [
        {"company_id": "incoming-samsung", "company_name": "Samsung Electronics", "domain": "samsung.com"},
        {"company_id": "newco-1", "company_name": "NewCo", "domain": "newco.kr"},
    ]
    new_details = {"incoming-samsung": {"company_id": "incoming-samsung"}, "newco-1": {"company_id": "newco-1"}}
    pm = prematch_export_records(new_items, existing, new_details=new_details, existing_details=details)
    reconciled, rdetails = reconcile_export_ids(new_items, new_details, pm)
    annotated, adetails = annotate_import_provenance(reconciled, rdetails, batch_id="batch-1", source_list_id="list-1")
    ledger = build_change_ledger(existing, annotated, pm, batch_id="batch-1")
    assert ledger["summary"] == {"created": 1, "updated_existing": 1, "total": 2}

    current = [x for x in existing if x["company_id"] != "samsung-com"] + annotated
    current_details = {**details, **adetails}
    rolled = selective_rollback(current, current_details, existing, details, ledger)
    assert rolled["safe_to_apply"] is True
    ids = {x["company_id"] for x in rolled["items"]}
    assert ids == {"samsung-com", "hanwha-group"}
    samsung = next(x for x in rolled["items"] if x["company_id"] == "samsung-com")
    assert samsung["company_name"] == "Samsung Electronics Co., Ltd."


def test_rollback_refuses_later_touched_record_and_snapshot_is_written():
    before = [{"company_id": "a", "company_name": "A", "domain": "a.com"}]
    ledger = {"batch_id": "batch-1", "entries": [{"company_id": "a", "action": "updated_existing"}]}
    current = [{"company_id": "a", "company_name": "A2", "domain": "a.com", "last_import_batch": "batch-2"}]
    rolled = selective_rollback(current, {}, before, {}, ledger)
    assert rolled["safe_to_apply"] is False
    assert rolled["conflict_company_ids"] == ["a"]
    with tempfile.TemporaryDirectory() as td:
        path = write_before_snapshot(Path(td) / "before", before, {}, batch_id="batch-1", country_key="south-korea")
        assert (path / "companies.list.json").is_file()
        assert (path / "details.by-id.json").is_file()
        assert (path / "snapshot_manifest.json").is_file()


def test_enrichment_plan_holds_ambiguous_before_zyte():
    plan = build_enrichment_plan({"entries": [
        {"source_company_key": "n", "action": "new"},
        {"source_company_key": "m", "action": "matched_existing"},
        {"source_company_key": "a", "action": "ambiguous"},
    ]})
    assert plan["summary"] == {"full_enrichment": 1, "gap_fill_existing": 1, "held_for_review": 1}
    assert plan["safe_to_enrich"] is False




def test_import_plan_freezes_identity_and_reconciles_by_source_lineage():
    prematch = {
        "summary": {"matched_existing": 1, "new": 1, "ambiguous": 0, "total": 2},
        "entries": [
            {
                "source_company_key": "source-existing",
                "company_name": "Existing Co",
                "action": "matched_existing",
                "existing_company_id": "existing-1",
                "match_basis": "reviewed_identity_match",
                "confidence": "high",
            },
            {
                "source_company_key": "source-new",
                "company_name": "New Co",
                "action": "new",
                "existing_company_id": None,
                "match_basis": "none",
                "confidence": "none",
            },
        ],
    }
    plan = build_import_plan(
        prematch, batch_id="batch-1", country="south-korea", caller="Carla"
    )
    assert plan["locked"] is True
    assert plan["summary"] == {"matched_existing": 1, "new": 1, "total": 2}
    assert plan["plan_sha256"]

    refreshed = validate_import_plan(
        plan, prematch, batch_id="batch-1", country="south-korea", caller="Carla"
    )
    assert refreshed["valid"] is True

    exported = [
        {"company_id": "generated-existing", "company_name": "Existing Co"},
        {"company_id": "generated-new", "company_name": "New Co"},
    ]
    details = {
        "generated-existing": {
            "debug": {"lead_prioritizer_row": {"source_company_key": "source-existing"}}
        },
        "generated-new": {
            "debug": {"lead_prioritizer_row": {"source_company_key": "source-new"}}
        },
    }
    items, out_details, lineage = reconcile_export_with_import_plan(exported, details, plan)
    planned_new_id = next(
        e["target_company_id"] for e in plan["entries"] if e["source_company_key"] == "source-new"
    )
    ids = {x["company_id"] for x in items}
    assert ids == {"existing-1", planned_new_id}
    assert planned_new_id != "generated-new"
    assert out_details["existing-1"]["company_id"] == "existing-1"
    assert lineage["summary"]["ambiguous"] == 0


def test_import_plan_detects_real_identity_drift():
    prematch = {
        "summary": {"matched_existing": 0, "new": 1, "ambiguous": 0, "total": 1},
        "entries": [{
            "source_company_key": "source-1",
            "company_name": "Example",
            "action": "new",
            "existing_company_id": None,
            "match_basis": "none",
            "confidence": "none",
        }],
    }
    plan = build_import_plan(
        prematch, batch_id="batch-1", country="south-korea", caller="Carla"
    )
    changed = {
        "summary": {"matched_existing": 1, "new": 0, "ambiguous": 0, "total": 1},
        "entries": [{
            "source_company_key": "source-1",
            "company_name": "Example",
            "action": "matched_existing",
            "existing_company_id": "now-existing",
            "match_basis": "exact_domain",
            "confidence": "high",
        }],
    }
    validation = validate_import_plan(
        plan, changed, batch_id="batch-1", country="south-korea", caller="Carla"
    )
    assert validation["valid"] is False
    assert validation["drift_count"] == 1
    assert validation["drift"][0]["type"] == "identity_decision_changed"



def test_email_domain_hint_cannot_override_conflicting_exact_name():
    existing = [
        {"company_id": "group", "company_name": "Hanwha Group", "domain": "hanwha.com"},
        {"company_id": "systems", "company_name": "Hanwha Systems", "domain": "hanwhasystems.com"},
    ]
    report = prematch_companies([{
        "company_key": "k1",
        "company_name": "Hanwha Systems",
        "domain": "",
        "email_domain_hint": "hanwha.com",
    }], existing)
    entry = report["entries"][0]
    assert entry["action"] == "ambiguous"
    assert entry["match_basis"] == "name_hint_conflict"
    assert set(entry["candidate_company_ids"]) == {"group", "systems"}


def test_shared_hint_matching_same_exact_name_is_safe():
    existing = [
        {"company_id": "samsung", "company_name": "Samsung Electronics", "domain": "samsung.com"},
    ]
    report = prematch_companies([{
        "company_key": "k1",
        "company_name": "Samsung Electronics",
        "domain": "",
        "email_domain_hint": "samsung.com",
    }], existing)
    entry = report["entries"][0]
    assert entry["action"] == "matched_existing"
    assert entry["existing_company_id"] == "samsung"
    assert entry["match_basis"] == "exact_name_and_email_domain_hint"


def test_review_can_explicitly_create_new():
    from lead_list_safety import apply_reviewed_match_overrides
    prematch = {
        "summary": {"matched_existing": 0, "new": 0, "ambiguous": 1, "total": 1},
        "entries": [{
            "source_company_key": "k1",
            "company_name": "Distinct Subsidiary",
            "action": "ambiguous",
            "existing_company_id": None,
            "match_basis": "name_hint_conflict",
            "confidence": "review",
            "candidate_company_ids": ["group"],
        }],
    }
    out = apply_reviewed_match_overrides(prematch, [{
        "source_company_key": "k1",
        "action": "new",
        "evidence": ["official-source: distinct legal entity"],
    }])
    assert out["summary"]["new"] == 1
    assert out["summary"]["reviewed_create_new"] == 1
    assert out["entries"][0]["match_basis"] == "reviewed_create_new"


def test_multiple_sources_cannot_claim_same_existing_target():
    existing = [
        {"company_id": "shared", "company_name": "Shared", "domain": "shared.com"},
    ]
    report = prematch_companies([
        {"company_key": "a", "company_name": "A", "domain": "shared.com"},
        {"company_key": "b", "company_name": "B", "domain": "shared.com"},
    ], existing)
    assert report["summary"]["matched_existing"] == 0
    assert report["summary"]["ambiguous"] == 2
    assert report["summary"]["target_collision_groups"] == 1

if __name__ == "__main__":
    for fn in [test_exact_domain_reuses_existing_id, test_name_only_is_ambiguous_not_automatic, test_name_and_phone_can_match_without_domain, test_reconcile_provenance_ledger_and_selective_rollback, test_rollback_refuses_later_touched_record_and_snapshot_is_written, test_enrichment_plan_holds_ambiguous_before_zyte, test_import_plan_freezes_identity_and_reconciles_by_source_lineage, test_import_plan_detects_real_identity_drift, test_email_domain_hint_cannot_override_conflicting_exact_name, test_shared_hint_matching_same_exact_name_is_safe, test_review_can_explicitly_create_new, test_multiple_sources_cannot_claim_same_existing_target]:
        fn()
        print(fn.__name__, "OK")
