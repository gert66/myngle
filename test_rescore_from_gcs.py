"""Tests for rescore_from_gcs — pure re-scoring logic + GCS CLI plumbing.

No real network calls, no real Google Cloud SDK required (subprocess is
always mocked, same style as test_lovable_gcs_upload.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commercial_fit_scoring import LEAN_COEFFICIENTS, score_company
from rescore_from_gcs import (
    C5_UPGRADE_HQ_SCORE,
    CURRENT_MANIFEST_FILENAME,
    LIST_FILENAME,
    apply_c5_foreign_hq_upgrade,
    build_rescore_manifest,
    build_rescored_run,
    default_rescore_run_folder,
    download_current_run,
    download_file,
    download_files_batch,
    gcs_current_dir,
    gcs_run_dir,
    list_country_folders,
    list_current_files,
    list_run_files,
    promote_run_to_current,
    rehydrate_scoring_row,
    rescore_all_countries,
    rescore_country,
    rescore_detail_record,
    rescore_details_bucket,
    resolve_detail_employee_range,
    rescore_list_items,
    resolve_gcs_tool,
    skipped_company_ids,
    tier_distribution,
    tier_movers,
    unexpected_rescore_warning,
    upload_file,
    upload_rescored_run,
    write_rescored_run,
)


# ---------------------------------------------------------------------------
# Fixtures mimicking the scoring_inputs / company-details schema exported by
# export_lead_prioritizer_to_lovable_json._build_scoring_inputs /
# _build_detail_record.
# ---------------------------------------------------------------------------

def fixture_scoring_inputs(**signal_overrides) -> dict:
    signals = {field: 0.0 for field in LEAN_COEFFICIENTS}
    signals.update(signal_overrides)
    return {
        "schema_version": 1,
        "signals": signals,
        "employee_range": "1001-5000",
    }


def fixture_detail(company_id="c1", **signal_overrides) -> dict:
    scoring_inputs = fixture_scoring_inputs(**signal_overrides)
    row = rehydrate_scoring_row(scoring_inputs)
    scored = score_company(row)
    return {
        "company_id": company_id,
        "company_name": f"Company {company_id}",
        "commercial_fit_score": scored["final_commercial_fit_score"],
        "commercial_tier": scored["commercial_tier"],
        "scoring_inputs": scoring_inputs,
    }


def fixture_c5_detail(company_id="c1", adjudication="foreign_parent_confirmed",
                       confidence="Medium", sig_foreign_hq_score=None, **overrides) -> dict:
    detail = fixture_detail(company_id, sig_foreign_hq_score=sig_foreign_hq_score, **overrides)
    detail["c5_adjudication"] = adjudication
    detail["c5_confidence"] = confidence
    detail["sig_foreign_hq_score_for_next_scoring"] = None
    return detail


# ---------------------------------------------------------------------------
# apply_c5_foreign_hq_upgrade
# ---------------------------------------------------------------------------

class TestApplyC5ForeignHqUpgrade:
    def test_confirmed_medium_confidence_gets_upgraded(self):
        detail = fixture_c5_detail(confidence="Medium")
        upgraded = apply_c5_foreign_hq_upgrade(detail)
        assert upgraded["scoring_inputs"]["signals"]["sig_foreign_hq_score"] == C5_UPGRADE_HQ_SCORE
        assert upgraded["sig_foreign_hq_score_for_next_scoring"] == C5_UPGRADE_HQ_SCORE
        assert upgraded["c5_upgrade_applied"] is True

    def test_confirmed_high_confidence_gets_upgraded(self):
        detail = fixture_c5_detail(confidence="High")
        upgraded = apply_c5_foreign_hq_upgrade(detail)
        assert upgraded["scoring_inputs"]["signals"]["sig_foreign_hq_score"] == C5_UPGRADE_HQ_SCORE

    def test_low_confidence_is_not_upgraded(self):
        detail = fixture_c5_detail(confidence="Low")
        upgraded = apply_c5_foreign_hq_upgrade(detail)
        assert upgraded is detail
        assert upgraded["scoring_inputs"]["signals"]["sig_foreign_hq_score"] is None

    def test_unclear_adjudication_is_not_upgraded(self):
        detail = fixture_c5_detail(adjudication="unclear", confidence="High")
        upgraded = apply_c5_foreign_hq_upgrade(detail)
        assert upgraded is detail

    def test_domestic_confirmed_is_not_upgraded(self):
        detail = fixture_c5_detail(adjudication="domestic_confirmed", confidence="High")
        upgraded = apply_c5_foreign_hq_upgrade(detail)
        assert upgraded is detail

    def test_no_c5_fields_is_not_upgraded(self):
        detail = fixture_detail(sig_foreign_hq_score=None)
        upgraded = apply_c5_foreign_hq_upgrade(detail)
        assert upgraded is detail

    def test_existing_signal_is_never_overridden(self):
        # The pipeline already produced a real (even if low/zero) signal --
        # a C5 recommendation must never override actual scoring output.
        detail = fixture_c5_detail(sig_foreign_hq_score=1.0)
        upgraded = apply_c5_foreign_hq_upgrade(detail)
        assert upgraded is detail
        assert upgraded["scoring_inputs"]["signals"]["sig_foreign_hq_score"] == 1.0

    def test_does_not_mutate_input(self):
        detail = fixture_c5_detail()
        original = json.loads(json.dumps(detail))
        apply_c5_foreign_hq_upgrade(detail)
        assert detail == original

    def test_wired_through_rescore_detail_record_when_flag_set(self):
        detail = fixture_c5_detail(confidence="High")
        rescored = rescore_detail_record(
            detail, params={}, now_iso="2026-07-12T00:00:00Z", apply_c5_upgrade=True)
        assert rescored["c5_upgrade_applied"] is True
        assert rescored["rescore_audit"]["c5_upgrade_applied"] is True
        # The upgraded signal must actually have moved the score.
        not_upgraded = rescore_detail_record(
            fixture_c5_detail(confidence="High"), params={}, now_iso="2026-07-12T00:00:00Z",
            apply_c5_upgrade=False)
        assert rescored["commercial_fit_score"] > not_upgraded["commercial_fit_score"]

    def test_not_applied_when_flag_is_false(self):
        detail = fixture_c5_detail(confidence="High")
        rescored = rescore_detail_record(
            detail, params={}, now_iso="2026-07-12T00:00:00Z", apply_c5_upgrade=False)
        assert not rescored.get("c5_upgrade_applied")
        assert rescored["scoring_inputs"]["signals"]["sig_foreign_hq_score"] is None


# ---------------------------------------------------------------------------
# Pure re-scoring logic
# ---------------------------------------------------------------------------

class TestRehydrateScoringRow:
    def test_signals_and_employee_range_carried_over(self):
        scoring_inputs = fixture_scoring_inputs(sig_foreign_hq_score=3.0)
        row = rehydrate_scoring_row(scoring_inputs)
        assert row["sig_foreign_hq_score"] == 3.0
        assert row["employee_range"] == "1001-5000"

    def test_missing_signal_stays_none_not_coerced_to_zero(self):
        scoring_inputs = fixture_scoring_inputs()
        scoring_inputs["signals"]["sig_rapid_growth_score"] = None
        row = rehydrate_scoring_row(scoring_inputs)
        assert row["sig_rapid_growth_score"] is None


class TestRescoreDetailRecord:
    def test_round_trip_reproduces_pipeline_score_with_same_params(self):
        """The whole point of scoring_inputs: read it back out, feed it into
        score_company() with the SAME params as the original run, and get
        the identical final_commercial_fit_score/commercial_tier."""
        detail = fixture_detail(
            sig_foreign_hq_score=3, sig_explicit_lnd_score=2,
            sig_intl_footprint_score=3, sig_employer_branding_score=1,
            sig_lnd_onboarding_score=2, ti_onboarding_score=1,
            sig_rapid_growth_score=0,
        )
        expected = score_company(rehydrate_scoring_row(detail["scoring_inputs"]))

        rescored = rescore_detail_record(detail, params={}, now_iso="2026-07-08T00:00:00Z")

        assert rescored["commercial_fit_score"] == expected["final_commercial_fit_score"]
        assert rescored["commercial_tier"] == expected["commercial_tier"]
        assert rescored["commercial_fit_score"] == detail["commercial_fit_score"]
        assert rescored["commercial_tier"] == detail["commercial_tier"]

    def test_different_params_change_the_score(self):
        detail = fixture_detail(sig_foreign_hq_score=3, sig_explicit_lnd_score=3)
        rescored = rescore_detail_record(
            detail, params={"intercept": -99.0}, now_iso="2026-07-08T00:00:00Z")
        assert rescored["commercial_fit_score"] != detail["commercial_fit_score"]
        assert rescored["commercial_tier"] == "❄️ Pass"

    def test_app_facing_score_and_tier_are_updated_too(self):
        # commercial_fit_score_app / commercial_tier_app are the fields the
        # Company Hub frontend actually reads (it prioritizes them over the
        # canonical commercial_fit_score/commercial_tier) -- a re-score must
        # update them too, or the app keeps showing the pre-rescore number.
        detail = fixture_detail(sig_foreign_hq_score=3, sig_explicit_lnd_score=3)
        detail["commercial_fit_score_app"] = detail["commercial_fit_score"]
        detail["commercial_tier_app"] = detail["commercial_tier"]

        rescored = rescore_detail_record(
            detail, params={"intercept": -99.0}, now_iso="2026-07-08T00:00:00Z")

        assert rescored["commercial_fit_score_app"] == rescored["commercial_fit_score"]
        assert rescored["commercial_tier_app"] == rescored["commercial_tier"]
        assert rescored["commercial_fit_score_app"] != detail["commercial_fit_score_app"]

    def test_missing_app_facing_fields_are_not_invented(self):
        # An older export that never had the _app fields stays that way --
        # a re-score never introduces new keys the original export lacked.
        detail = fixture_detail()
        assert "commercial_fit_score_app" not in detail
        rescored = rescore_detail_record(detail, params={}, now_iso="2026-07-08T00:00:00Z")
        assert "commercial_fit_score_app" not in rescored
        assert "commercial_tier_app" not in rescored

    def test_does_not_mutate_input_detail(self):
        detail = fixture_detail()
        original = json.loads(json.dumps(detail))
        rescore_detail_record(detail, params={}, now_iso="2026-07-08T00:00:00Z")
        assert detail == original

    def test_missing_scoring_inputs_raises(self):
        detail = {"company_id": "c1"}
        with pytest.raises(ValueError, match="scoring_inputs"):
            rescore_detail_record(detail, params={}, now_iso="2026-07-08T00:00:00Z")

    def test_rescore_audit_records_before_after_and_missing_signals(self):
        detail = fixture_detail(sig_foreign_hq_score=3)
        detail["scoring_inputs"]["signals"]["sig_rapid_growth_score"] = None
        rescored = rescore_detail_record(
            detail, params={"intercept": -1.0}, now_iso="2026-07-08T00:00:00Z")

        audit = rescored["rescore_audit"]
        assert audit["schema_version"] == 1
        assert audit["rescored_at"] == "2026-07-08T00:00:00Z"
        assert audit["params"] == {"intercept": -1.0}
        assert audit["previous_commercial_fit_score"] == detail["commercial_fit_score"]
        assert audit["previous_commercial_tier"] == detail["commercial_tier"]
        assert audit["final_commercial_fit_score"] == rescored["commercial_fit_score"]
        assert audit["commercial_tier"] == rescored["commercial_tier"]
        assert "sig_rapid_growth_score" in audit["missing_scoring_signals"]
        assert isinstance(audit["scoring_notes"], str) and audit["scoring_notes"]


class TestScoreOffsetInRescoreDetailRecord:
    """score_offset flows through rescore_detail_record into both the
    top-level commercial_fit_score and the rescore_audit trail — see
    commercial_fit_scoring.score_company's score_offset step and
    rescore_streamlit_app's "Final score offset" control."""

    def test_offset_shifts_score_and_preserves_before_offset_audit_fields(self):
        detail = fixture_detail(**{f: 0 for f in LEAN_COEFFICIENTS})
        baseline = rescore_detail_record(detail, params={}, now_iso="2026-07-13T00:00:00Z")
        offset_result = rescore_detail_record(
            detail, params={"score_offset": 0.5}, now_iso="2026-07-13T00:00:00Z")

        assert offset_result["final_commercial_fit_score_before_offset"] == \
            baseline["commercial_fit_score"]
        assert offset_result["score_offset_applied"] == 0.5
        assert offset_result["commercial_fit_score"] == \
            round(min(10.0, baseline["commercial_fit_score"] + 0.5), 2)

    def test_offset_audit_fields_mirrored_into_rescore_audit(self):
        detail = fixture_detail(**{f: 0 for f in LEAN_COEFFICIENTS})
        rescored = rescore_detail_record(
            detail, params={"score_offset": -0.5}, now_iso="2026-07-13T00:00:00Z")
        audit = rescored["rescore_audit"]
        assert audit["score_offset_applied"] == -0.5
        assert audit["final_commercial_fit_score_before_offset"] == \
            rescored["final_commercial_fit_score_before_offset"]

    def test_no_offset_in_params_defaults_to_zero_backward_compatible(self):
        # Every params dict that existed before this feature (no
        # "score_offset" key) must behave exactly as before.
        detail = fixture_detail(sig_foreign_hq_score=2)
        rescored = rescore_detail_record(detail, params={}, now_iso="2026-07-13T00:00:00Z")
        assert rescored["score_offset_applied"] == 0.0
        assert rescored["final_commercial_fit_score_before_offset"] == \
            rescored["commercial_fit_score"]

    def test_tier_assignment_uses_the_offset_adjusted_score(self):
        detail = fixture_detail(**{f: 0 for f in LEAN_COEFFICIENTS})
        params = {"model_weight": 0.0, "size_weight": 1.0}  # size_score 6.63 -> Cool
        baseline = rescore_detail_record(detail, params=params, now_iso="2026-07-13T00:00:00Z")
        with_offset = rescore_detail_record(
            detail, params={**params, "score_offset": 1.0}, now_iso="2026-07-13T00:00:00Z")
        assert baseline["commercial_tier"] == "🥉 Cool"
        assert with_offset["commercial_tier"] == "🥈 Warm"

    def test_params_persisted_in_manifest_and_rescore_audit_include_score_offset(self):
        # "saved parameter metadata" / "uploaded rescore run configuration":
        # both build_rescore_manifest and rescore_audit persist the FULL
        # params dict verbatim, so score_offset travels with the run without
        # any extra plumbing.
        detail = fixture_detail(sig_foreign_hq_score=1)
        rescored = rescore_detail_record(
            detail, params={"score_offset": 0.3}, now_iso="2026-07-13T00:00:00Z")
        assert rescored["rescore_audit"]["params"]["score_offset"] == 0.3

        manifest = build_rescore_manifest(
            country_folder="brazil", source_current_manifest=None,
            params={"score_offset": 0.3}, run_folder="run1",
            original_details_by_id={"c1": detail},
            rescored_details_by_id={"c1": rescored},
            generated_at="2026-07-13T00:00:00Z",
        )
        assert manifest["params"]["score_offset"] == 0.3


class TestEmployeeRangeRecovery:
    """v2-era exports (Spain, ...) persisted scoring_inputs.employee_range as
    None — the exporter read only the employee_range column while the v2
    pipeline stores the Lusha range under lusha_employee_range. The raw
    Lusha columns survive under debug.lead_prioritizer_row, so a re-score
    must recover the real size from there instead of silently scoring
    everyone with the neutral 5.5 default, and must backfill the app-facing
    size fields the original export left blank."""

    def _v2_style_detail(self, company_id="c1", lusha_range="1001 - 5000") -> dict:
        signals = {field: 2.0 for field in LEAN_COEFFICIENTS}
        return {
            "company_id": company_id,
            "company_name": f"Company {company_id}",
            "commercial_fit_score": 5.0,
            "commercial_tier": "🥉 Cool",
            "employee_range": "",
            "size_category_app": None,
            "display_size_category_app": None,
            "scoring_inputs": {
                "schema_version": 1, "signals": signals, "employee_range": None,
            },
            "debug": {"lead_prioritizer_row": {
                "employee_range": "", "lusha_employee_range": lusha_range,
            }},
        }

    def test_resolve_priority_scoring_inputs_then_detail_then_debug(self):
        detail = self._v2_style_detail()
        assert resolve_detail_employee_range(detail) == ("1001 - 5000", "debug_row")

        detail_with_field = dict(detail, employee_range="201-500")
        assert resolve_detail_employee_range(detail_with_field) == \
            ("201-500", "detail_record")

        detail_with_inputs = json.loads(json.dumps(detail))
        detail_with_inputs["scoring_inputs"]["employee_range"] = "51 - 200"
        assert resolve_detail_employee_range(detail_with_inputs) == \
            ("51 - 200", "scoring_inputs")

    def test_nothing_usable_anywhere_is_missing(self):
        detail = self._v2_style_detail(lusha_range="")
        assert resolve_detail_employee_range(detail) == (None, "missing")

    def test_rescore_uses_recovered_size_not_neutral_default(self):
        detail = self._v2_style_detail()
        rescored = rescore_detail_record(detail, params={}, now_iso="2026-07-10T00:00:00Z")

        signals = {field: 2.0 for field in LEAN_COEFFICIENTS}
        with_size = score_company({**signals, "employee_range": "1001 - 5000"})
        neutral = score_company(signals)
        assert rescored["commercial_fit_score"] == with_size["final_commercial_fit_score"]
        assert rescored["commercial_fit_score"] != neutral["final_commercial_fit_score"]

    def test_rescore_backfills_app_facing_size_fields(self):
        rescored = rescore_detail_record(
            self._v2_style_detail(), params={}, now_iso="2026-07-10T00:00:00Z")
        assert rescored["employee_range"] == "1001 - 5000"
        assert rescored["size_category_app"] == "large"
        assert rescored["display_size_category_app"] == "Large (1,001–5,000 employees)"
        assert rescored["rescore_audit"]["employee_range_used"] == "1001 - 5000"
        assert rescored["rescore_audit"]["employee_range_source"] == "debug_row"

    def test_explicit_original_size_fields_never_overwritten(self):
        detail = self._v2_style_detail()
        detail["employee_range"] = "201-500"
        detail["size_category_app"] = "custom_slug"
        detail["display_size_category_app"] = "Custom Label"
        rescored = rescore_detail_record(detail, params={}, now_iso="2026-07-10T00:00:00Z")
        assert rescored["employee_range"] == "201-500"
        assert rescored["size_category_app"] == "custom_slug"
        assert rescored["display_size_category_app"] == "Custom Label"

    def test_list_items_mirror_backfilled_size_fields_without_overwriting(self):
        rescored = rescore_detail_record(
            self._v2_style_detail("c1"), params={}, now_iso="2026-07-10T00:00:00Z")
        list_items = [
            {"company_id": "c1", "commercial_fit_score": 5.0,
             "commercial_tier": "🥉 Cool", "employee_range": "",
             "size_category_app": None, "display_size_category_app": None},
            {"company_id": "c1", "commercial_fit_score": 5.0,
             "commercial_tier": "🥉 Cool", "employee_range": "51 - 200",
             "size_category_app": "explicit", "display_size_category_app": "Explicit"},
        ]
        updated = rescore_list_items(list_items, {"c1": rescored})
        assert updated[0]["employee_range"] == "1001 - 5000"
        assert updated[0]["size_category_app"] == "large"
        assert updated[0]["display_size_category_app"] == "Large (1,001–5,000 employees)"
        # An item with explicit original values keeps them.
        assert updated[1]["employee_range"] == "51 - 200"
        assert updated[1]["size_category_app"] == "explicit"
        assert updated[1]["display_size_category_app"] == "Explicit"


class TestRescoreDetailsBucket:
    def test_rescores_every_company_and_returns_new_dict(self):
        bucket = {
            "c1": fixture_detail("c1", sig_foreign_hq_score=3),
            "c2": fixture_detail("c2", sig_foreign_hq_score=0),
        }
        rescored = rescore_details_bucket(bucket, params={}, now_iso="2026-07-08T00:00:00Z")
        assert set(rescored) == {"c1", "c2"}
        assert rescored is not bucket
        assert all("rescore_audit" in d for d in rescored.values())

    def test_company_without_scoring_inputs_is_skipped_not_fatal(self):
        # A current run that predates the scoring_inputs export (or a single
        # company that was never re-exported) must not abort the whole
        # country's re-score — it should be left unchanged and reported.
        legacy_detail = {
            "company_id": "legacy-co",
            "commercial_fit_score": 4.2,
            "commercial_tier": "🥉 Cool",
        }
        bucket = {
            "legacy-co": legacy_detail,
            "c2": fixture_detail("c2", sig_foreign_hq_score=3),
        }
        rescored = rescore_details_bucket(bucket, params={}, now_iso="2026-07-08T00:00:00Z")

        assert rescored["legacy-co"]["commercial_fit_score"] == 4.2
        assert rescored["legacy-co"]["commercial_tier"] == "🥉 Cool"
        assert rescored["legacy-co"]["rescore_audit"]["skipped"] is True
        assert "scoring_inputs" in rescored["legacy-co"]["rescore_audit"]["skip_reason"]
        assert rescored["c2"]["rescore_audit"].get("skipped") is not True
        assert skipped_company_ids(rescored) == ["legacy-co"]


class TestRescoreListItems:
    def test_mirrors_new_score_and_tier_onto_matching_list_item(self):
        list_items = [{"company_id": "c1", "commercial_fit_score": 1.0, "commercial_tier": "X"}]
        rescored_by_id = {"c1": {"commercial_fit_score": 9.9, "commercial_tier": "\U0001f947 Hot"}}
        updated = rescore_list_items(list_items, rescored_by_id)
        assert updated[0]["commercial_fit_score"] == 9.9
        assert updated[0]["commercial_tier"] == "\U0001f947 Hot"
        assert updated is not list_items

    def test_item_without_rescored_counterpart_passes_through_unchanged(self):
        list_items = [{"company_id": "c-missing", "commercial_fit_score": 1.0, "commercial_tier": "X"}]
        updated = rescore_list_items(list_items, rescored_by_id={})
        assert updated[0] == list_items[0]
        assert updated[0] is not list_items[0]

    def test_mirrors_app_facing_score_and_tier_when_present(self):
        list_items = [{
            "company_id": "c1", "commercial_fit_score": 1.0, "commercial_tier": "X",
            "commercial_fit_score_app": 1.0, "commercial_tier_app": "X",
        }]
        rescored_by_id = {"c1": {
            "commercial_fit_score": 9.9, "commercial_tier": "\U0001f947 Hot",
            "commercial_fit_score_app": 9.9, "commercial_tier_app": "\U0001f947 Hot",
        }}
        updated = rescore_list_items(list_items, rescored_by_id)
        assert updated[0]["commercial_fit_score_app"] == 9.9
        assert updated[0]["commercial_tier_app"] == "\U0001f947 Hot"


class TestTierDistribution:
    def test_counts_by_tier(self):
        details = {
            "c1": {"commercial_tier": "Hot"},
            "c2": {"commercial_tier": "Hot"},
            "c3": {"commercial_tier": "Cool"},
        }
        assert tier_distribution(details) == {"Hot": 2, "Cool": 1}

    def test_empty_details_yields_empty_distribution(self):
        assert tier_distribution({}) == {}


class TestBuildRescoreManifest:
    def test_shape(self):
        original = {"c1": {"commercial_tier": "Cool"}}
        rescored = {"c1": {"commercial_tier": "Hot"}}
        manifest = build_rescore_manifest(
            country_folder="brazil",
            source_current_manifest={"generated_at": "2026-01-01T00:00:00Z"},
            params={"intercept": -1.0},
            run_folder="2026-07-08_rescore",
            original_details_by_id=original,
            rescored_details_by_id=rescored,
            generated_at="2026-07-08T00:00:00Z",
        )
        assert manifest["schema_version"] == 1
        assert manifest["country_folder"] == "brazil"
        assert manifest["run_folder"] == "2026-07-08_rescore"
        assert manifest["companies_rescored"] == 1
        assert manifest["companies_skipped"] == 0
        assert manifest["skipped_company_ids"] == []
        assert manifest["companies_tier_changed"] == 1
        assert manifest["tier_distribution_before"] == {"Cool": 1}
        assert manifest["tier_distribution_after"] == {"Hot": 1}
        assert manifest["promoted_to_current"] is False
        assert manifest["source_current_manifest"]["generated_at"] == "2026-01-01T00:00:00Z"

    def test_skipped_companies_counted_separately_from_rescored(self):
        original = {"c1": {"commercial_tier": "Cool"}, "c2": {"commercial_tier": "Cool"}}
        rescored = {
            "c1": {"commercial_tier": "Hot", "rescore_audit": {}},
            "c2": {"commercial_tier": "Cool", "rescore_audit": {"skipped": True}},
        }
        manifest = build_rescore_manifest(
            country_folder="brazil", source_current_manifest=None, params={},
            run_folder="run1", original_details_by_id=original,
            rescored_details_by_id=rescored, generated_at="2026-07-08T00:00:00Z",
        )
        assert manifest["companies_rescored"] == 1
        assert manifest["companies_skipped"] == 1
        assert manifest["skipped_company_ids"] == ["c2"]


class TestTierMovers:
    def test_only_changed_tiers_included(self):
        original = {
            "c1": {"commercial_tier": "Cool"},
            "c2": {"commercial_tier": "Hot"},
        }
        rescored = {
            "c1": {"company_name": "One", "commercial_tier": "Hot",
                   "commercial_fit_score": 8.0},
            "c2": {"company_name": "Two", "commercial_tier": "Hot",
                   "commercial_fit_score": 9.0},
        }
        movers = tier_movers(original, rescored)
        assert len(movers) == 1
        assert movers[0]["company_id"] == "c1"
        assert movers[0]["tier_before"] == "Cool"
        assert movers[0]["tier_after"] == "Hot"

    def test_company_missing_from_original_is_excluded(self):
        original = {}
        rescored = {"c1": {"commercial_tier": "Hot"}}
        assert tier_movers(original, rescored) == []

    def test_no_changes_yields_empty_list(self):
        original = {"c1": {"commercial_tier": "Cool"}}
        rescored = {"c1": {"commercial_tier": "Cool"}}
        assert tier_movers(original, rescored) == []


class TestUnexpectedRescoreWarning:
    def _manifest(self, *, n_changed, source_params, new_params, n_rescored=5):
        return {
            "companies_tier_changed": n_changed,
            "companies_rescored": n_rescored,
            "params": new_params,
            "source_current_manifest": (
                {"params": source_params} if source_params is not None else {}
            ),
        }

    def test_no_warning_when_nothing_changed_tier(self):
        manifest = self._manifest(
            n_changed=0, source_params={"intercept": -1.0}, new_params={"intercept": -1.0})
        assert unexpected_rescore_warning(manifest) is None

    def test_no_warning_when_source_has_no_recorded_params(self):
        # Source run was a fresh export, never itself re-scored -- nothing to
        # compare against, so tier movement here isn't necessarily unexpected.
        manifest = self._manifest(
            n_changed=3, source_params=None, new_params={"intercept": -1.0})
        assert unexpected_rescore_warning(manifest) is None

    def test_no_warning_when_params_actually_differ(self):
        manifest = self._manifest(
            n_changed=3, source_params={"intercept": -1.0},
            new_params={"intercept": -2.0})
        assert unexpected_rescore_warning(manifest) is None

    def test_warning_when_identical_params_still_move_tiers(self):
        manifest = self._manifest(
            n_changed=3, source_params={"intercept": -1.0},
            new_params={"intercept": -1.0})
        warning = unexpected_rescore_warning(manifest)
        assert warning is not None
        assert "3 of 5 companies" in warning
        assert "IDENTICAL" in warning


class TestSkippedCompanyIds:
    def test_finds_only_skipped_entries(self):
        details = {
            "c1": {"rescore_audit": {"skipped": True}},
            "c2": {"rescore_audit": {"skipped": False}},
            "c3": {},
        }
        assert skipped_company_ids(details) == ["c1"]


class TestDefaultRescoreRunFolder:
    def test_format(self):
        now = datetime(2026, 7, 8, 10, 30, tzinfo=timezone.utc)
        assert default_rescore_run_folder(now) == "2026-07-08_rescore"


# ---------------------------------------------------------------------------
# GCS CLI plumbing — subprocess always mocked
# ---------------------------------------------------------------------------

class TestResolveGcsTool:
    def test_prefers_gcloud_storage(self):
        with patch("rescore_from_gcs.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            assert resolve_gcs_tool() == ["/usr/bin/gcloud", "storage"]

    def test_falls_back_to_gsutil(self):
        def _which(name):
            return "/usr/bin/gsutil" if name == "gsutil" else None
        with patch("rescore_from_gcs.shutil.which", side_effect=_which):
            assert resolve_gcs_tool() == ["/usr/bin/gsutil"]

    def test_none_when_neither_present(self):
        with patch("rescore_from_gcs.shutil.which", return_value=None):
            assert resolve_gcs_tool() is None


class TestPathBuilders:
    def test_current_dir(self):
        assert gcs_current_dir("bucket-a", "brazil") == "gs://bucket-a/brazil/current"

    def test_run_dir(self):
        assert gcs_run_dir("bucket-a", "brazil", "2026-07-08_rescore") == \
            "gs://bucket-a/brazil/runs/2026-07-08_rescore"


class TestListCountryFolders:
    def test_parses_folder_names(self):
        stdout = "gs://bucket-a/brazil/\ngs://bucket-a/italy/\ngs://bucket-a/countries.index.json\n"
        mock_proc = MagicMock(returncode=0, stdout=stdout, stderr="")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            assert list_country_folders("bucket-a") == ["brazil", "italy"]

    def test_no_tool_and_no_python_backend_returns_empty_without_subprocess(self):
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=None), \
             patch("gcs_python_backend.get_client", return_value=None), \
             patch("rescore_from_gcs.subprocess.run") as mock_run:
            assert list_country_folders("bucket-a") == []
        mock_run.assert_not_called()

    def test_failed_listing_returns_empty(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="AccessDenied")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            assert list_country_folders("bucket-a") == []


class TestListCurrentFiles:
    def test_parses_filenames(self):
        stdout = (
            "gs://bucket-a/brazil/current/companies.list.json\n"
            "gs://bucket-a/brazil/current/company-details-000.json\n"
            "gs://bucket-a/brazil/current/export_manifest.json\n"
        )
        mock_proc = MagicMock(returncode=0, stdout=stdout, stderr="")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            files = list_current_files("bucket-a", "brazil")
        assert files == [
            "companies.list.json", "company-details-000.json", "export_manifest.json",
        ]

    def test_empty_current_folder_returns_empty(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="not found")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            assert list_current_files("bucket-a", "brazil") == []


class TestDownloadFile:
    def test_successful_download_no_shell(self):
        mock_proc = MagicMock(returncode=0, stdout="Copying...", stderr="")
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc) as mock_run:
            result = download_file(["gcloud", "storage"], "gs://b/f.json", "/tmp/f.json")
        assert result["success"] is True
        args, kwargs = mock_run.call_args
        assert args[0] == ["gcloud", "storage", "cp", "gs://b/f.json", "/tmp/f.json"]
        assert "shell" not in kwargs or kwargs["shell"] is False

    def test_failed_download_captures_stderr(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="NotFound")
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            result = download_file(["gsutil"], "gs://b/f.json", "/tmp/f.json")
        assert result["success"] is False
        assert result["stderr"] == "NotFound"

    def test_subprocess_exception_does_not_raise(self):
        with patch("rescore_from_gcs.subprocess.run", side_effect=OSError("boom")):
            result = download_file(["gcloud", "storage"], "gs://b/f.json", "/tmp/f.json")
        assert result["success"] is False
        assert "boom" in result["error"]


class TestDownloadFilesBatch:
    def test_single_subprocess_call_for_many_sources(self, tmp_path):
        mock_proc = MagicMock(returncode=0, stdout="Copying...", stderr="")
        sources = [f"gs://b/company-details-{i:03d}.json" for i in range(11)]
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc) as mock_run:
            result = download_files_batch(["gcloud", "storage"], sources, str(tmp_path) + "/")
        assert result["success"] is True
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        cmd = args[0]
        assert cmd[:3] == ["gcloud", "storage", "cp"]
        assert cmd[3:-1] == sources
        assert cmd[-1] == str(tmp_path) + "/"

    def test_empty_sources_is_a_no_op(self):
        with patch("rescore_from_gcs.subprocess.run") as mock_run:
            result = download_files_batch(["gcloud", "storage"], [], "/tmp/out/")
        assert result["success"] is True
        mock_run.assert_not_called()

    def test_failed_batch_captures_stderr(self, tmp_path):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="PermissionDenied")
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            result = download_files_batch(
                ["gsutil"], ["gs://b/f1.json", "gs://b/f2.json"], str(tmp_path) + "/")
        assert result["success"] is False
        assert result["stderr"] == "PermissionDenied"

    def test_subprocess_exception_does_not_raise(self):
        with patch("rescore_from_gcs.subprocess.run", side_effect=OSError("boom")):
            result = download_files_batch(["gcloud", "storage"], ["gs://b/f.json"], "/tmp/out/")
        assert result["success"] is False
        assert "boom" in result["error"]


class TestUploadFile:
    def test_missing_local_file_fails_without_subprocess(self, tmp_path):
        missing = tmp_path / "nope.json"
        with patch("rescore_from_gcs.subprocess.run") as mock_run:
            result = upload_file(["gcloud", "storage"], str(missing), "gs://b/f.json")
        assert result["success"] is False
        assert "not found" in result["error"]
        mock_run.assert_not_called()

    def test_successful_upload(self, tmp_path):
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            result = upload_file(["gcloud", "storage"], str(local), "gs://b/f.json")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# download_current_run / rescore_country — end-to-end with mocked subprocess
# ---------------------------------------------------------------------------

def _write_fixture_current_run(country_dir: Path, *, include_legacy_company: bool = False) -> dict:
    """Write a small fixture 'current' run (manifest + list + one details
    bucket) to a local directory, mimicking what would live in
    gs://bucket/<country>/current/. Returns the parsed detail records by id."""
    country_dir.mkdir(parents=True, exist_ok=True)
    detail_a = fixture_detail("company-a", sig_foreign_hq_score=3, sig_explicit_lnd_score=3)
    detail_b = fixture_detail("company-b", sig_foreign_hq_score=0, sig_explicit_lnd_score=0)
    bucket = {"company-a": detail_a, "company-b": detail_b}
    if include_legacy_company:
        # Mimics a company exported before the scoring_inputs contract
        # existed — no scoring_inputs block at all.
        bucket["legacy-co"] = {
            "company_id": "legacy-co",
            "commercial_fit_score": 4.2,
            "commercial_tier": "🥉 Cool",
        }

    list_items = [
        {
            "company_id": cid,
            "commercial_fit_score": d["commercial_fit_score"],
            "commercial_tier": d["commercial_tier"],
        }
        for cid, d in bucket.items()
    ]
    manifest = {"generated_at": "2026-07-01T00:00:00Z", "export_country": "Brazil"}

    (country_dir / LIST_FILENAME).write_text(json.dumps(list_items), encoding="utf-8")
    (country_dir / "company-details-000.json").write_text(json.dumps(bucket), encoding="utf-8")
    (country_dir / CURRENT_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return bucket


def _fake_gcs_tool_for_local_dir(remote_root: Path):
    """Build a subprocess.run stand-in that serves 'ls'/'cp' from a local
    directory tree as if it were gs://bucket/... — keeps the download tests
    honest about the ls-then-cp flow without touching a real bucket.

    'cp' handles both the single-source form (upload_file / promote's
    download_file calls: ``cp source dest_file``) and the batch form
    (``download_files_batch``: ``cp source1 source2 ... dest_dir/``) so both
    code paths can be exercised against the same fake."""

    def _run(cmd, capture_output=True, text=True, timeout=None):
        if "ls" in cmd:
            target = cmd[-1]
            rel = target.replace("gs://bucket-a/", "").rstrip("/")
            local_dir = remote_root / rel
            if not local_dir.is_dir():
                return MagicMock(returncode=1, stdout="", stderr="not found")
            lines = [f"{target.rstrip('/')}/{p.name}" for p in sorted(local_dir.iterdir())]
            return MagicMock(returncode=0, stdout="\n".join(lines) + ("\n" if lines else ""), stderr="")
        if "cp" in cmd:
            cp_idx = cmd.index("cp")
            # Strip flags (e.g. --cache-control=...) and any preceding value
            # they take (e.g. gsutil's "-h" "Cache-Control:...") -- real
            # gcloud storage cp / gsutil cp accept flags in any position, so
            # this fake must too instead of only ever handling a bare
            # "cp source... dest" invocation.
            raw_args = cmd[cp_idx + 1:]
            args = []
            skip_next = False
            for tok in raw_args:
                if skip_next:
                    skip_next = False
                    continue
                if tok.startswith("--"):
                    continue
                if tok == "-h":
                    skip_next = True
                    continue
                args.append(tok)
            *sources, dest = args
            if len(sources) > 1 or dest.endswith("/"):
                # Batch form: many sources -> one destination directory.
                dest_dir = Path(dest)
                dest_dir.mkdir(parents=True, exist_ok=True)
                for source in sources:
                    rel = source.replace("gs://bucket-a/", "")
                    local_source = remote_root / rel
                    (dest_dir / local_source.name).write_bytes(local_source.read_bytes())
                return MagicMock(returncode=0, stdout="", stderr="")
            source = sources[0]
            if source.startswith("gs://"):
                rel = source.replace("gs://bucket-a/", "")
                local_source = remote_root / rel
                Path(dest).write_bytes(local_source.read_bytes())
            else:
                rel = dest.replace("gs://bucket-a/", "")
                remote_path = remote_root / rel
                remote_path.parent.mkdir(parents=True, exist_ok=True)
                remote_path.write_bytes(Path(source).read_bytes())
            return MagicMock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


class TestDownloadCurrentRun:
    def test_downloads_manifest_list_and_detail_files(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        work_dir = tmp_path / "work"

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            current = download_current_run("bucket-a", "brazil", work_dir)

        assert current["manifest"]["export_country"] == "Brazil"
        assert len(current["list_items"]) == 2
        assert "company-details-000.json" in current["detail_files"]
        assert set(current["detail_files"]["company-details-000.json"]) == {"company-a", "company-b"}

    def test_no_tool_and_no_python_backend_raises_clear_error(self, tmp_path):
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=None), \
             patch("gcs_python_backend.get_client", return_value=None):
            with pytest.raises(RuntimeError, match="gcloud"):
                download_current_run("bucket-a", "brazil", tmp_path / "work")

    def test_empty_current_folder_raises(self, tmp_path):
        remote_root = tmp_path / "remote"
        (remote_root / "brazil" / "current").mkdir(parents=True)
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            with pytest.raises(RuntimeError, match="No re-scorable files"):
                download_current_run("bucket-a", "brazil", tmp_path / "work")

    def test_many_detail_buckets_use_one_cp_call_not_one_per_file(self, tmp_path):
        """The slow part users hit ('Huidige run laden' taking minutes) was
        one subprocess (CLI startup + auth) per file. With 11 detail-bucket
        files (a ~5000-company country at bucket_size=500) plus manifest +
        list, downloading must still cost exactly one 'ls' and one 'cp'
        call — not 13."""
        remote_root = tmp_path / "remote"
        country_dir = remote_root / "brazil" / "current"
        country_dir.mkdir(parents=True)
        all_ids = []
        for bucket_no in range(11):
            bucket = {
                f"company-{bucket_no}-{i}": {
                    "company_id": f"company-{bucket_no}-{i}",
                    "commercial_fit_score": 5.0,
                    "commercial_tier": "🥉 Cool",
                }
                for i in range(5)
            }
            all_ids.extend(bucket)
            (country_dir / f"company-details-{bucket_no:03d}.json").write_text(
                json.dumps(bucket), encoding="utf-8")
        (country_dir / LIST_FILENAME).write_text(
            json.dumps([{"company_id": cid} for cid in all_ids]), encoding="utf-8")
        (country_dir / CURRENT_MANIFEST_FILENAME).write_text(
            json.dumps({"export_country": "Brazil"}), encoding="utf-8")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run",
                   side_effect=_fake_gcs_tool_for_local_dir(remote_root)) as mock_run:
            current = download_current_run("bucket-a", "brazil", tmp_path / "work")

        assert len(current["detail_files"]) == 11
        cp_calls = [c for c in mock_run.call_args_list if "cp" in c.args[0]]
        ls_calls = [c for c in mock_run.call_args_list if "ls" in c.args[0]]
        assert len(cp_calls) == 1
        assert len(ls_calls) == 1


class TestBuildRescoredRunPureCore:
    """build_rescored_run/write_rescored_run/upload_rescored_run — the
    no-download/no-upload core reused by an interactive UI that recomputes
    on every parameter tweak without re-hitting GCS."""

    def _fixture_current(self) -> dict:
        bucket = {
            "company-a": fixture_detail("company-a", sig_foreign_hq_score=3),
            "company-b": fixture_detail("company-b", sig_foreign_hq_score=0),
        }
        list_items = [
            {"company_id": cid, "commercial_fit_score": d["commercial_fit_score"],
             "commercial_tier": d["commercial_tier"]}
            for cid, d in bucket.items()
        ]
        return {
            "manifest": {"generated_at": "2026-07-01T00:00:00Z"},
            "list_items": list_items,
            "detail_files": {"company-details-000.json": bucket},
        }

    def test_build_rescored_run_shape(self):
        current = self._fixture_current()
        rescored_run = build_rescored_run(
            current, params={}, country_folder="brazil",
            run_folder="2026-07-08_rescore", now_iso="2026-07-08T00:00:00Z")

        assert set(rescored_run) == {"list_items", "detail_files", "manifest"}
        assert len(rescored_run["list_items"]) == 2
        assert "company-details-000.json" in rescored_run["detail_files"]
        assert rescored_run["manifest"]["companies_rescored"] == 2
        assert rescored_run["manifest"]["run_folder"] == "2026-07-08_rescore"

    def test_recomputing_with_different_params_does_not_touch_original_current(self):
        current = self._fixture_current()
        original_snapshot = json.loads(json.dumps(current))
        build_rescored_run(
            current, params={"intercept": -99.0}, country_folder="brazil",
            run_folder="run1", now_iso="2026-07-08T00:00:00Z")
        assert current == original_snapshot

    def test_write_then_upload_round_trip(self, tmp_path):
        current = self._fixture_current()
        rescored_run = build_rescored_run(
            current, params={}, country_folder="brazil",
            run_folder="2026-07-08_rescore", now_iso="2026-07-08T00:00:00Z")

        out_dir = write_rescored_run(rescored_run, tmp_path / "out")
        assert (out_dir / LIST_FILENAME).exists()
        assert (out_dir / "company-details-000.json").exists()
        assert (out_dir / CURRENT_MANIFEST_FILENAME).exists()

        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            results = upload_rescored_run(out_dir, "bucket-a", "brazil", "2026-07-08_rescore")

        assert len(results) == 3
        assert all(r["success"] for r in results)
        assert all(
            r["destination"].startswith("gs://bucket-a/brazil/runs/2026-07-08_rescore/")
            for r in results)

    def test_upload_rescored_run_raises_without_gcs_tool_or_python_backend(self, tmp_path):
        (tmp_path / LIST_FILENAME).write_text("[]")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=None), \
             patch("gcs_python_backend.get_client", return_value=None):
            with pytest.raises(RuntimeError, match="gcloud"):
                upload_rescored_run(tmp_path, "bucket-a", "brazil", "run1")


class TestRescoreCountry:
    def test_end_to_end_writes_to_new_run_folder_never_current(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={},
                run_folder="2026-07-08_rescore",
                now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            )

        assert manifest["companies_rescored"] == 2
        assert manifest["run_folder"] == "2026-07-08_rescore"
        destinations = {r["destination"] for r in manifest["upload_results"]}
        assert all(d.startswith("gs://bucket-a/brazil/runs/2026-07-08_rescore/") for d in destinations)
        assert all("/current/" not in d for d in destinations)
        assert all(r["success"] for r in manifest["upload_results"])

        # current/ on the fake remote must be untouched.
        current_dir = remote_root / "brazil" / "current"
        original_bucket = json.loads(
            (current_dir / "company-details-000.json").read_text(encoding="utf-8"))
        assert original_bucket["company-a"]["commercial_fit_score"] == \
            fixture_detail("company-a", sig_foreign_hq_score=3, sig_explicit_lnd_score=3)["commercial_fit_score"]

    def test_company_without_scoring_inputs_is_skipped_and_reported_not_fatal(self, tmp_path):
        # Real-world scenario: the current run predates the scoring_inputs
        # export for at least one company. The whole country must still
        # re-score successfully, with the legacy company carried over
        # unchanged and called out in the manifest.
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current", include_legacy_company=True)

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={},
                run_folder="2026-07-08_rescore",
                now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            )

        assert manifest["companies_rescored"] == 2
        assert manifest["companies_skipped"] == 1
        assert manifest["skipped_company_ids"] == ["legacy-co"]
        assert all(r["success"] for r in manifest["upload_results"])

        new_bucket = json.loads(
            (remote_root / "brazil" / "runs" / "2026-07-08_rescore" / "company-details-000.json")
            .read_text(encoding="utf-8"))
        assert new_bucket["legacy-co"]["commercial_fit_score"] == 4.2
        assert new_bucket["legacy-co"]["commercial_tier"] == "🥉 Cool"
        assert new_bucket["legacy-co"]["rescore_audit"]["skipped"] is True

    def test_same_params_reproduce_current_scores_in_new_run(self, tmp_path):
        remote_root = tmp_path / "remote"
        original_bucket = _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={},
                run_folder="2026-07-08_rescore",
                now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            )

        new_bucket = json.loads(
            (remote_root / "brazil" / "runs" / "2026-07-08_rescore" / "company-details-000.json")
            .read_text(encoding="utf-8"))
        for cid, original_detail in original_bucket.items():
            assert new_bucket[cid]["commercial_fit_score"] == original_detail["commercial_fit_score"]
            assert new_bucket[cid]["commercial_tier"] == original_detail["commercial_tier"]

    def test_dry_run_skips_upload(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={}, upload=False,
                run_folder="2026-07-08_rescore",
                now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            )

        assert manifest["upload_results"] == []
        assert not (remote_root / "brazil" / "runs").exists()

    def test_work_dir_output_kept_when_explicitly_provided(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        work_dir = tmp_path / "keep-me"

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={}, work_dir=work_dir, upload=False,
                run_folder="2026-07-08_rescore",
            )

        assert manifest["local_output_dir"] == str(work_dir / "out")
        assert (work_dir / "out" / LIST_FILENAME).exists()


class TestRescoreAllCountries:
    def test_one_country_failing_does_not_stop_the_others(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        # "italy" has no current/ folder at all -> should error, not raise.

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            results = rescore_all_countries(
                "bucket-a", params={}, countries=["brazil", "italy"],
                run_folder="2026-07-08_rescore",
            )

        assert results["brazil"]["companies_rescored"] == 2
        assert "error" in results["italy"]


class TestListRunFiles:
    def test_lists_filenames_under_run_folder(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "runs" / "2026-07-08_reallocate")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            names = list_run_files("bucket-a", "brazil", "2026-07-08_reallocate")

        assert set(names) == {
            LIST_FILENAME, CURRENT_MANIFEST_FILENAME, "company-details-000.json"}

    def test_no_tool_and_no_python_backend_returns_empty_list(self):
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=None), \
             patch("gcs_python_backend.get_client", return_value=None):
            assert list_run_files("bucket-a", "brazil", "run1") == []

    def test_missing_run_folder_returns_empty_list(self, tmp_path):
        remote_root = tmp_path / "remote"
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            assert list_run_files("bucket-a", "brazil", "does-not-exist") == []


class TestPromoteRunToCurrent:
    def test_copies_run_files_into_current_and_stamps_manifest(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "runs" / "2026-07-08_reallocate")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            result = promote_run_to_current(
                "bucket-a", "brazil", "2026-07-08_reallocate",
                now=datetime(2026, 7, 9, 13, 41, tzinfo=timezone.utc),
            )

        assert all(r["success"] for r in result["results"])
        destinations = {r["destination"] for r in result["results"]}
        assert destinations == {
            f"gs://bucket-a/brazil/current/{name}"
            for name in (LIST_FILENAME, CURRENT_MANIFEST_FILENAME, "company-details-000.json")
        }

        current_dir = remote_root / "brazil" / "current"
        promoted_manifest = json.loads(
            (current_dir / CURRENT_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert promoted_manifest["promoted_to_current"] is True
        assert promoted_manifest["promoted_from_run_folder"] == "2026-07-08_reallocate"
        assert promoted_manifest["promoted_at"] == "2026-07-09T13:41:00Z"

        promoted_list = json.loads((current_dir / LIST_FILENAME).read_text(encoding="utf-8"))
        assert len(promoted_list) == 2

        # The source run folder itself must be untouched.
        source_manifest = json.loads(
            (remote_root / "brazil" / "runs" / "2026-07-08_reallocate" /
             CURRENT_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert "promoted_to_current" not in source_manifest

    def test_no_tool_and_no_python_backend_raises_clear_error(self, tmp_path):
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=None), \
             patch("gcs_python_backend.get_client", return_value=None):
            with pytest.raises(RuntimeError, match="gcloud"):
                promote_run_to_current("bucket-a", "brazil", "run1")

    def test_missing_run_folder_raises(self, tmp_path):
        remote_root = tmp_path / "remote"
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            with pytest.raises(RuntimeError, match="No promotable files"):
                promote_run_to_current("bucket-a", "brazil", "does-not-exist")

    def test_existing_current_is_overwritten(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        _write_fixture_current_run(remote_root / "brazil" / "runs" / "2026-07-08_reallocate")
        # Mutate the run's list so it's distinguishable from the stale current/.
        run_list_path = (
            remote_root / "brazil" / "runs" / "2026-07-08_reallocate" / LIST_FILENAME)
        run_list = json.loads(run_list_path.read_text(encoding="utf-8"))
        run_list[0]["assigned_cold_caller"] = "Ernie"
        run_list_path.write_text(json.dumps(run_list), encoding="utf-8")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            promote_run_to_current("bucket-a", "brazil", "2026-07-08_reallocate")

        current_list = json.loads(
            (remote_root / "brazil" / "current" / LIST_FILENAME).read_text(encoding="utf-8"))
        assert current_list[0]["assigned_cold_caller"] == "Ernie"
