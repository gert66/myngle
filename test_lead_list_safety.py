import tempfile
import unittest
from pathlib import Path

from lead_list_safety import (
    annotate_import_provenance,
    build_enrichment_plan,
    build_change_ledger,
    prematch_companies,
    prematch_export_records,
    reconcile_export_ids,
    selective_rollback,
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


if __name__ == "__main__":
    for fn in [test_exact_domain_reuses_existing_id, test_name_only_is_ambiguous_not_automatic, test_name_and_phone_can_match_without_domain, test_reconcile_provenance_ledger_and_selective_rollback, test_rollback_refuses_later_touched_record_and_snapshot_is_written, test_enrichment_plan_holds_ambiguous_before_zyte]:
        fn()
        print(fn.__name__, "OK")
