"""Tests for the pure helpers behind rescore_streamlit_app.py.

Only the pure helpers are tested — no Streamlit/Plotly rendering. Streamlit
and Plotly are imported lazily inside ``main()`` (see the module docstring),
so this module is importable without them.
"""

from __future__ import annotations

import pandas as pd
import pytest

from commercial_fit_scoring import (
    COMPANY_SIZE_WEIGHT,
    ICP_SIMILARITY_WEIGHT,
    INTERCEPT,
    LEAN_COEFFICIENTS,
    SIGMOID_K,
    SIZE_BAND_LOOKUP,
    TIER_THRESHOLDS,
    score_company,
)
from rescore_streamlit_app import (
    TIER_LABELS,
    biggest_movers_dataframe,
    build_multi_country_preview,
    build_multi_country_rescored_runs,
    default_params,
    employee_range_options,
    multi_country_summary_dataframe,
    score_component_breakdown,
    score_distribution_dataframe,
    signal_has_presence,
    signal_split_score_dataframe,
    signal_split_summary,
    sigmoid_curve_dataframe,
    tier_distribution_dataframe,
    validate_tier_thresholds,
)


class TestDefaultParams:
    def test_matches_commercial_fit_scoring_module_defaults(self):
        params = default_params()
        assert params["intercept"] == INTERCEPT
        assert params["coefficients"] == LEAN_COEFFICIENTS
        assert params["model_weight"] == ICP_SIMILARITY_WEIGHT
        assert params["size_weight"] == COMPANY_SIZE_WEIGHT
        assert params["sigmoid_k"] == SIGMOID_K
        assert [tuple(t) for t in params["tier_thresholds"]] == TIER_THRESHOLDS

    def test_returns_independent_copies(self):
        a = default_params()
        b = default_params()
        a["coefficients"]["sig_foreign_hq_score"] = 999.0
        assert b["coefficients"]["sig_foreign_hq_score"] != 999.0
        assert LEAN_COEFFICIENTS["sig_foreign_hq_score"] != 999.0


class TestSigmoidCurveDataframe:
    def test_shape_and_columns(self):
        df = sigmoid_curve_dataframe(k=10.0, n=50)
        assert len(df) == 51
        assert set(df.columns) == {"probability", "sigmoid_raw_s", "icp_similarity_score"}
        assert df["probability"].min() == 0.0
        assert df["probability"].max() == 1.0

    def test_icp_similarity_score_stays_within_1_to_10(self):
        df = sigmoid_curve_dataframe(k=10.0)
        assert df["icp_similarity_score"].min() >= 1.0
        assert df["icp_similarity_score"].max() <= 10.0

    def test_higher_k_produces_steeper_curve_around_midpoint(self):
        gentle = sigmoid_curve_dataframe(k=1.0, n=100)
        steep = sigmoid_curve_dataframe(k=20.0, n=100)
        # Slope of icp_similarity_score right around p=0.5 should be bigger for higher k.
        mid = 50
        gentle_slope = gentle["icp_similarity_score"][mid + 5] - gentle["icp_similarity_score"][mid - 5]
        steep_slope = steep["icp_similarity_score"][mid + 5] - steep["icp_similarity_score"][mid - 5]
        assert steep_slope > gentle_slope


class TestValidateTierThresholds:
    def test_valid_descending_thresholds_pass(self):
        assert validate_tier_thresholds(9.34, 8.28, 4.26) is None

    def test_non_descending_thresholds_error(self):
        assert validate_tier_thresholds(5.0, 8.0, 4.26) is not None

    def test_negative_cool_threshold_errors(self):
        assert validate_tier_thresholds(9.0, 5.0, -1.0) is not None

    def test_equal_thresholds_error(self):
        assert validate_tier_thresholds(9.0, 9.0, 4.0) is not None


class TestScoreDistributionDataframe:
    def test_produces_long_form_before_after_rows(self):
        original = {"c1": {"commercial_fit_score": 3.0}}
        rescored = {"c1": {"commercial_fit_score": 7.0}}
        df = score_distribution_dataframe(original, rescored)
        assert len(df) == 2
        assert set(df["when"]) == {"Huidig", "Nieuw"}
        assert sorted(df["commercial_fit_score"]) == [3.0, 7.0]


class TestTierDistributionDataframe:
    def test_counts_and_ordering(self):
        original = {"c1": {"commercial_tier": "🥉 Cool"}, "c2": {"commercial_tier": "🥉 Cool"}}
        rescored = {"c1": {"commercial_tier": "🥇 Hot"}, "c2": {"commercial_tier": "🥉 Cool"}}
        df = tier_distribution_dataframe(original, rescored)
        hot_rows = df[(df["tier"] == "🥇 Hot") & (df["when"] == "Nieuw")]
        assert hot_rows["count"].iloc[0] == 1
        cool_before = df[(df["tier"] == "🥉 Cool") & (df["when"] == "Huidig")]
        assert cool_before["count"].iloc[0] == 2
        # Known tiers should appear in Hot -> Warm -> Cool -> Pass order.
        seen_tiers = list(dict.fromkeys(df["tier"]))
        assert seen_tiers == [t for t in TIER_LABELS if t in seen_tiers]


class TestBiggestMoversDataframe:
    def test_sorted_by_absolute_delta_descending(self):
        original = {
            "c1": {"commercial_fit_score": 5.0, "commercial_tier": "Cool"},
            "c2": {"commercial_fit_score": 5.0, "commercial_tier": "Cool"},
        }
        rescored = {
            "c1": {"commercial_fit_score": 5.5, "commercial_tier": "Cool", "company_name": "A"},
            "c2": {"commercial_fit_score": 9.0, "commercial_tier": "Hot", "company_name": "B"},
        }
        df = biggest_movers_dataframe(original, rescored)
        assert list(df["company_id"]) == ["c2", "c1"]
        assert df.iloc[0]["delta"] == 4.0

    def test_skips_companies_missing_from_either_side(self):
        original = {"c1": {"commercial_fit_score": 5.0}}
        rescored = {"c1": {"commercial_fit_score": 6.0}, "c2": {"commercial_fit_score": 3.0}}
        df = biggest_movers_dataframe(original, rescored)
        assert list(df["company_id"]) == ["c1"]

    def test_empty_input_returns_empty_dataframe(self):
        df = biggest_movers_dataframe({}, {})
        assert df.empty


class TestSignalHasPresence:
    def test_positive_signal_is_present(self):
        detail = {"scoring_inputs": {"signals": {"sig_foreign_hq_score": 3}}}
        assert signal_has_presence(detail, "sig_foreign_hq_score") is True

    def test_missing_signal_is_not_present(self):
        detail = {"scoring_inputs": {"signals": {"sig_foreign_hq_score": None}}}
        assert signal_has_presence(detail, "sig_foreign_hq_score") is False

    def test_explicit_zero_is_not_present(self):
        # score_company folds missing and explicit-0 into the same 0.0
        # contribution, so "present" means strictly > 0.
        detail = {"scoring_inputs": {"signals": {"sig_foreign_hq_score": 0}}}
        assert signal_has_presence(detail, "sig_foreign_hq_score") is False

    def test_no_scoring_inputs_block_is_not_present(self):
        assert signal_has_presence({}, "sig_foreign_hq_score") is False


class TestSignalSplitScoreDataframe:
    def test_splits_into_met_and_zonder_groups(self):
        original = {
            "c1": {"commercial_fit_score": 5.0,
                   "scoring_inputs": {"signals": {"sig_foreign_hq_score": 3}}},
            "c2": {"commercial_fit_score": 4.0,
                   "scoring_inputs": {"signals": {"sig_foreign_hq_score": None}}},
        }
        rescored = {
            "c1": {"commercial_fit_score": 8.0},
            "c2": {"commercial_fit_score": 4.2},
        }
        df = signal_split_score_dataframe(original, rescored, "sig_foreign_hq_score")
        assert len(df) == 4
        c1_groups = set(df[df["company_id"] == "c1"]["group"])
        c2_groups = set(df[df["company_id"] == "c2"]["group"])
        assert c1_groups == {"Met sig_foreign_hq_score"}
        assert c2_groups == {"Zonder sig_foreign_hq_score"}

    def test_rescored_company_keeps_original_group(self):
        # Rescoring never changes which companies had the signal, only the
        # score — a company's "Nieuw" row must land in the same group as its
        # "Huidig" row.
        original = {"c1": {"commercial_fit_score": 5.0,
                            "scoring_inputs": {"signals": {"sig_foreign_hq_score": 2}}}}
        rescored = {"c1": {"commercial_fit_score": 9.0}}
        df = signal_split_score_dataframe(original, rescored, "sig_foreign_hq_score")
        assert set(df["group"]) == {"Met sig_foreign_hq_score"}

    def test_empty_input_returns_empty_dataframe(self):
        df = signal_split_score_dataframe({}, {}, "sig_foreign_hq_score")
        assert df.empty


class TestSignalSplitSummary:
    def test_groups_by_group_and_when_with_n_and_median(self):
        original = {
            "c1": {"commercial_fit_score": 4.0,
                   "scoring_inputs": {"signals": {"sig_foreign_hq_score": 3}}},
            "c2": {"commercial_fit_score": 6.0,
                   "scoring_inputs": {"signals": {"sig_foreign_hq_score": 3}}},
        }
        rescored = {"c1": {"commercial_fit_score": 8.0}, "c2": {"commercial_fit_score": 9.0}}
        split_df = signal_split_score_dataframe(original, rescored, "sig_foreign_hq_score")
        summary = signal_split_summary(split_df)
        row = summary[(summary["group"] == "Met sig_foreign_hq_score")
                       & (summary["when"] == "Huidig")].iloc[0]
        assert row["n"] == 2
        assert row["mediaan"] == 5.0

    def test_empty_input_returns_empty_dataframe(self):
        assert signal_split_summary(pd.DataFrame()).empty


class TestBuildMultiCountryPreview:
    def _detail(self, employee_range: str) -> dict:
        return {
            "commercial_fit_score": 5.0,
            "commercial_tier": "🥉 Cool",
            "scoring_inputs": {
                "employee_range": employee_range,
                "signals": {f: 3 for f in LEAN_COEFFICIENTS},
            },
        }

    def test_ids_are_prefixed_by_country_and_never_collide(self):
        current_by_country = {
            "nl": {"detail_files": {"d1.json": {"c1": self._detail("1 - 10")}}},
            "be": {"detail_files": {"d1.json": {"c1": self._detail("100001 - 10000000")}}},
        }
        preview = build_multi_country_preview(
            current_by_country, default_params(), now_iso="2026-01-01T00:00:00Z")
        assert set(preview["original_by_id"]) == {"nl:c1", "be:c1"}
        assert set(preview["rescored_by_id"]) == {"nl:c1", "be:c1"}
        # Different company size per country -> different rescored score.
        assert preview["rescored_by_id"]["nl:c1"]["commercial_fit_score"] != \
            preview["rescored_by_id"]["be:c1"]["commercial_fit_score"]

    def test_empty_input_returns_empty_dicts(self):
        preview = build_multi_country_preview({}, default_params(), now_iso="2026-01-01T00:00:00Z")
        assert preview == {"original_by_id": {}, "rescored_by_id": {}}


class TestBuildMultiCountryRescoredRuns:
    def _detail(self, employee_range: str) -> dict:
        return {
            "commercial_fit_score": 5.0,
            "commercial_tier": "🥉 Cool",
            "scoring_inputs": {
                "employee_range": employee_range,
                "signals": {f: 3 for f in LEAN_COEFFICIENTS},
            },
        }

    def _current(self, company_id: str, employee_range: str) -> dict:
        detail = self._detail(employee_range)
        return {
            "detail_files": {"d1.json": {company_id: detail}},
            "list_items": [{"company_id": company_id, **detail}],
            "manifest": {"country_folder": "placeholder"},
        }

    def test_ids_stay_unprefixed_per_country(self):
        current_by_country = {
            "nl": self._current("c1", "1 - 10"),
            "be": self._current("c1", "100001 - 10000000"),
        }
        runs = build_multi_country_rescored_runs(
            current_by_country, default_params(),
            run_folder="2026-01-01_rescore", now_iso="2026-01-01T00:00:00Z",
        )
        assert set(runs) == {"nl", "be"}
        nl_ids = set(runs["nl"]["detail_files"]["d1.json"])
        be_ids = set(runs["be"]["detail_files"]["d1.json"])
        assert nl_ids == {"c1"}
        assert be_ids == {"c1"}
        # Different company size per country -> different rescored score,
        # same unprefixed id "c1" in both — the shape write_rescored_run/
        # upload_rescored_run expect for a single country.
        assert runs["nl"]["detail_files"]["d1.json"]["c1"]["commercial_fit_score"] != \
            runs["be"]["detail_files"]["d1.json"]["c1"]["commercial_fit_score"]

    def test_manifest_run_folder_matches_requested(self):
        current_by_country = {"nl": self._current("c1", "1 - 10")}
        runs = build_multi_country_rescored_runs(
            current_by_country, default_params(),
            run_folder="2026-02-02_rescore", now_iso="2026-01-01T00:00:00Z",
        )
        assert runs["nl"]["manifest"]["run_folder"] == "2026-02-02_rescore"
        assert runs["nl"]["manifest"]["country_folder"] == "nl"

    def test_empty_input_returns_empty_dict(self):
        runs = build_multi_country_rescored_runs(
            {}, default_params(), run_folder="r", now_iso="2026-01-01T00:00:00Z")
        assert runs == {}


class TestMultiCountrySummaryDataframe:
    def test_counts_companies_and_tier_changes_per_country(self):
        original = {
            "nl:c1": {"commercial_tier": "🥉 Cool"},
            "nl:c2": {"commercial_tier": "🥉 Cool"},
            "be:c1": {"commercial_tier": "🥉 Cool"},
        }
        rescored = {
            "nl:c1": {"commercial_tier": "🥇 Hot"},
            "nl:c2": {"commercial_tier": "🥉 Cool"},
            "be:c1": {"commercial_tier": "🥉 Cool"},
        }
        df = multi_country_summary_dataframe(original, rescored)
        nl_row = df[df["country_folder"] == "nl"].iloc[0]
        be_row = df[df["country_folder"] == "be"].iloc[0]
        assert nl_row["n_bedrijven"] == 2
        assert nl_row["n_tier_gewijzigd"] == 1
        assert be_row["n_bedrijven"] == 1
        assert be_row["n_tier_gewijzigd"] == 0

    def test_empty_input_returns_empty_dataframe(self):
        df = multi_country_summary_dataframe({}, {})
        assert df.empty


class TestScoreComponentBreakdown:
    def test_waterfall_steps_sum_to_lr_z_score(self):
        row = {f: 2 for f in LEAN_COEFFICIENTS}
        breakdown = score_component_breakdown(row, params={})
        total = sum(delta for _, delta in breakdown["waterfall_steps"])
        assert total == pytest.approx(breakdown["result"]["lr_z_score"], abs=1e-6)

    def test_matches_score_company_directly(self):
        row = {
            "sig_foreign_hq_score": 3, "sig_explicit_lnd_score": 2,
            "sig_intl_footprint_score": 3, "sig_employer_branding_score": 1,
            "sig_lnd_onboarding_score": 2, "ti_onboarding_score": 1,
            "sig_rapid_growth_score": 0, "employee_range": "1001-5000",
        }
        breakdown = score_component_breakdown(row, params={})
        expected = score_company(row)
        assert breakdown["result"]["final_commercial_fit_score"] == \
            expected["final_commercial_fit_score"]

    def test_custom_coefficients_change_the_breakdown(self):
        row = {f: 3 for f in LEAN_COEFFICIENTS}
        default_breakdown = score_component_breakdown(row, params={})
        custom_breakdown = score_component_breakdown(
            row, params={"coefficients": {"sig_foreign_hq_score": 0.0}})
        assert default_breakdown["result"]["lr_z_score"] != \
            custom_breakdown["result"]["lr_z_score"]


class TestEmployeeRangeOptions:
    def test_includes_all_size_bands_plus_missing_option(self):
        options = employee_range_options()
        for band in SIZE_BAND_LOOKUP:
            assert band in options
        assert "missing / unknown" in options
        assert options[-1] == "missing / unknown"
