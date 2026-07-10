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
from commercial_fit_scoring import _SIGMOID_P_HI, _SIGMOID_P_LO
from rescore_streamlit_app import (
    CALIBRATION_TARGET_HI,
    CALIBRATION_TARGET_LO,
    FOREIGN_HQ_REBALANCED_COEFFICIENTS,
    TIER_LABELS,
    auto_calibrate_sigmoid_anchors,
    biggest_movers_dataframe,
    build_multi_country_preview,
    build_multi_country_rescored_runs,
    calibrate_intercept_and_k,
    collect_calibration_features,
    compute_model_probabilities,
    default_params,
    employee_range_options,
    multi_country_summary_dataframe,
    percentile_sample_ids,
    sample_current_bundle,
    score_component_breakdown,
    score_distribution_dataframe,
    score_percentile_summary_dataframe,
    signal_has_presence,
    signal_split_score_dataframe,
    signal_split_summary,
    sigmoid_curve_dataframe,
    size_coverage_summary,
    suggest_tier_thresholds,
    tier_distribution_dataframe,
    top_companies_dataframe,
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
        assert params["sigmoid_p_lo"] == _SIGMOID_P_LO
        assert params["sigmoid_p_hi"] == _SIGMOID_P_HI
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


def _details_with_scores(scores: "list[float]") -> dict:
    return {
        f"c{i}": {"commercial_fit_score": s, "commercial_tier": "🥉 Cool"}
        for i, s in enumerate(scores)
    }


class TestPercentileSampleIds:
    def test_small_population_returns_everything_ranked_high_to_low(self):
        details = _details_with_scores([3.0, 9.0, 6.0])
        ids = percentile_sample_ids(details, sample_size=10)
        assert ids == ["c1", "c2", "c0"]  # 9.0, 6.0, 3.0

    def test_always_includes_the_full_top(self):
        details = _details_with_scores([i / 10.0 for i in range(100)])
        ids = percentile_sample_ids(details, sample_size=30, always_top=10)
        scores = sorted(
            (details[cid]["commercial_fit_score"] for cid in details), reverse=True)
        top_scores = set(scores[:10])
        sampled_scores = {details[cid]["commercial_fit_score"] for cid in ids}
        assert top_scores <= sampled_scores

    def test_spans_the_full_range_including_bottom(self):
        details = _details_with_scores([i / 10.0 for i in range(200)])
        ids = percentile_sample_ids(details, sample_size=50)
        sampled = [details[cid]["commercial_fit_score"] for cid in ids]
        assert max(sampled) == max(d["commercial_fit_score"] for d in details.values())
        assert min(sampled) == min(d["commercial_fit_score"] for d in details.values())

    def test_deterministic(self):
        details = _details_with_scores([i * 0.037 % 10 for i in range(500)])
        assert percentile_sample_ids(details, 100) == percentile_sample_ids(details, 100)

    def test_never_exceeds_sample_size(self):
        details = _details_with_scores([i / 10.0 for i in range(1000)])
        assert len(percentile_sample_ids(details, sample_size=100)) <= 100

    def test_companies_without_score_are_excluded(self):
        details = {"a": {"commercial_fit_score": 5.0}, "b": {}}
        assert percentile_sample_ids(details, 10) == ["a"]


class TestSampleCurrentBundle:
    def test_keeps_detail_file_shape_and_empty_list_items(self):
        details = _details_with_scores([i / 10.0 for i in range(100)])
        current = {
            "detail_files": {"d1.json": details},
            "list_items": [{"company_id": "c0"}],
            "manifest": {"country_folder": "nl"},
        }
        sampled = sample_current_bundle(current, sample_size=20)
        assert set(sampled) == {"detail_files", "list_items", "manifest"}
        assert sampled["list_items"] == []
        assert sampled["manifest"] == {"country_folder": "nl"}
        n = sum(len(b) for b in sampled["detail_files"].values())
        assert 0 < n <= 20

    def test_small_bundle_passes_through_all_companies(self):
        details = _details_with_scores([1.0, 2.0])
        current = {"detail_files": {"d1.json": details}, "list_items": [], "manifest": None}
        sampled = sample_current_bundle(current, sample_size=50)
        assert set(sampled["detail_files"]["d1.json"]) == {"c0", "c1"}


def _detail_with_signals(value: float, employee_range: "str | None" = None) -> dict:
    return {
        "commercial_fit_score": 5.0,
        "commercial_tier": "🥉 Cool",
        "scoring_inputs": {
            "employee_range": employee_range,
            "signals": {f: value for f in LEAN_COEFFICIENTS},
        },
    }


class TestAutoCalibrateSigmoidAnchors:
    def _population(self) -> dict:
        # Mixed signal strengths -> a real spread of model probabilities.
        return {
            f"c{i}": _detail_with_signals(v)
            for i, v in enumerate([0, 0, 1, 1, 2, 2, 3, 3, 0.5, 2.5])
        }

    def test_returns_ordered_anchors_within_0_1(self):
        anchors = auto_calibrate_sigmoid_anchors(self._population(), default_params())
        assert anchors is not None
        p_lo, p_hi = anchors
        assert 0.0 < p_lo < p_hi < 1.0

    def test_calibrated_anchors_push_top_company_to_10(self):
        population = self._population()
        params = default_params()
        anchors = auto_calibrate_sigmoid_anchors(population, params, lo_pct=0.0, hi_pct=100.0)
        params["sigmoid_p_lo"], params["sigmoid_p_hi"] = anchors
        best = score_company(
            {f: 3 for f in LEAN_COEFFICIENTS}, params=params)
        assert best["icp_similarity_score"] == 10.0

    def test_too_few_companies_returns_none(self):
        assert auto_calibrate_sigmoid_anchors({}, default_params()) is None
        assert auto_calibrate_sigmoid_anchors(
            {"c1": _detail_with_signals(2)}, default_params()) is None

    def test_no_spread_returns_none(self):
        population = {f"c{i}": _detail_with_signals(2) for i in range(5)}
        assert auto_calibrate_sigmoid_anchors(population, default_params()) is None

    def test_companies_without_scoring_inputs_are_skipped(self):
        population = {"a": {}, "b": {"scoring_inputs": {}}}
        assert compute_model_probabilities(population, default_params()) == []


class TestScorePercentileSummaryDataframe:
    def test_contains_expected_statistics_for_both_sides(self):
        original = _details_with_scores([2.0, 4.0, 6.0, 8.0])
        rescored = _details_with_scores([3.0, 5.0, 7.0, 9.0])
        df = score_percentile_summary_dataframe(original, rescored)
        stats = dict(zip(df["statistiek"], zip(df["Huidig"], df["Nieuw"])))
        assert stats["min"] == (2.0, 3.0)
        assert stats["max"] == (8.0, 9.0)
        assert stats["mediaan"] == (5.0, 6.0)
        assert "scheefheid" in stats

    def test_empty_input_returns_empty_dataframe(self):
        assert score_percentile_summary_dataframe({}, {}).empty


class TestTopCompaniesDataframe:
    def test_ranked_high_to_low_by_new_score(self):
        original = _details_with_scores([5.0, 5.0, 5.0])
        rescored = {
            "c0": {"commercial_fit_score": 7.0, "company_name": "A", "commercial_tier": "🥈 Warm"},
            "c1": {"commercial_fit_score": 9.5, "company_name": "B", "commercial_tier": "🥇 Hot"},
            "c2": {"commercial_fit_score": 3.0, "company_name": "C", "commercial_tier": "❄️ Pass"},
        }
        df = top_companies_dataframe(original, rescored, top_n=2)
        assert list(df["company_id"]) == ["c1", "c0"]
        assert list(df["rang"]) == [1, 2]
        assert df.iloc[0]["delta"] == 4.5

    def test_empty_input_returns_empty_dataframe(self):
        assert top_companies_dataframe({}, {}).empty


class TestSizeCoverageSummary:
    def test_counts_range_presence_only_for_scorable_companies(self):
        details = {
            "a": _detail_with_signals(2, "1001 - 5000"),
            "b": _detail_with_signals(2, None),
            "c": _detail_with_signals(2, ""),
            "d": {"commercial_fit_score": 5.0},  # no scoring_inputs -> ignored
        }
        cov = size_coverage_summary(details)
        assert cov["n_total"] == 3
        assert cov["n_with_range"] == 1
        assert cov["n_missing"] == 2
        assert cov["pct_with_range"] == pytest.approx(33.3)

    def test_empty_input(self):
        cov = size_coverage_summary({})
        assert cov == {"n_total": 0, "n_with_range": 0, "n_missing": 0,
                       "pct_with_range": 0.0, "sources": {}}

    def test_v2_export_range_recovered_from_debug_row(self):
        # v2-era exports (Spain, ...) have scoring_inputs.employee_range=None
        # but keep the raw Lusha columns under debug.lead_prioritizer_row —
        # coverage must count those, matching what the re-score itself uses.
        details = {
            "a": {
                "scoring_inputs": {"employee_range": None,
                                   "signals": {f: 2 for f in LEAN_COEFFICIENTS}},
                "debug": {"lead_prioritizer_row": {
                    "lusha_employee_range": "1001 - 5000"}},
            },
        }
        cov = size_coverage_summary(details)
        assert cov["n_with_range"] == 1
        assert cov["sources"] == {"debug_row": 1}


class TestSuggestTierThresholds:
    def test_thresholds_follow_10_20_30_rule_and_descend(self):
        scores = [i / 10.0 for i in range(1, 101)]  # 0.1 .. 10.0
        details = _details_with_scores(scores)
        suggestion = suggest_tier_thresholds(details)
        assert suggestion is not None
        hot, warm, cool = suggestion[0][0], suggestion[1][0], suggestion[2][0]
        assert hot > warm > cool >= 0
        assert suggestion[3] == [0.0, "❄️ Pass"]
        # Top 10% of 0.1..10.0 starts at the 90th percentile ≈ 9.1.
        assert hot == pytest.approx(9.1, abs=0.2)

    def test_no_spread_returns_none(self):
        details = _details_with_scores([5.0] * 10)
        assert suggest_tier_thresholds(details) is None

    def test_too_few_companies_returns_none(self):
        assert suggest_tier_thresholds(_details_with_scores([1.0, 2.0])) is None


class TestForeignHqRebalancedCoefficients:
    def test_same_signal_set_as_lean_coefficients(self):
        assert set(FOREIGN_HQ_REBALANCED_COEFFICIENTS) == set(LEAN_COEFFICIENTS)

    def test_foreign_hq_lowered_others_raised_rapid_growth_untouched(self):
        assert FOREIGN_HQ_REBALANCED_COEFFICIENTS["sig_foreign_hq_score"] < \
            LEAN_COEFFICIENTS["sig_foreign_hq_score"]
        for field in ("sig_explicit_lnd_score", "sig_intl_footprint_score",
                      "sig_employer_branding_score", "sig_lnd_onboarding_score",
                      "ti_onboarding_score"):
            assert FOREIGN_HQ_REBALANCED_COEFFICIENTS[field] > LEAN_COEFFICIENTS[field]
        assert FOREIGN_HQ_REBALANCED_COEFFICIENTS["sig_rapid_growth_score"] == \
            LEAN_COEFFICIENTS["sig_rapid_growth_score"]

    def test_total_positive_weight_essentially_unchanged(self):
        # A modest REdistribution, not a re-weighting: the model's overall
        # dynamic range must stay put.
        default_sum = sum(v for v in LEAN_COEFFICIENTS.values() if v > 0)
        rebalanced_sum = sum(v for v in FOREIGN_HQ_REBALANCED_COEFFICIENTS.values() if v > 0)
        assert abs(rebalanced_sum - default_sum) < 0.01

    def test_tweak_is_modest(self):
        # "Don't tweak too much" — the foreign-HQ cut stays under 15%.
        default = LEAN_COEFFICIENTS["sig_foreign_hq_score"]
        rebalanced = FOREIGN_HQ_REBALANCED_COEFFICIENTS["sig_foreign_hq_score"]
        assert (default - rebalanced) / default < 0.15


def _calibration_population(n: int = 80) -> dict:
    """Synthetic population with a realistic spread: signal levels vary per
    company AND per field, employee ranges cycle over the size bands."""
    ranges = [None, "11 - 50", "201 - 500", "1001 - 5000", "10001 - 100000"]
    details = {}
    for i in range(n):
        signals = {
            field: (i * 3 + j) % 4
            for j, field in enumerate(LEAN_COEFFICIENTS)
        }
        details[f"c{i}"] = {
            "commercial_fit_score": 5.0,
            "commercial_tier": "🥉 Cool",
            "scoring_inputs": {
                "schema_version": 1, "signals": signals,
                "employee_range": ranges[i % len(ranges)],
            },
        }
    return details


class TestCalibrateInterceptAndK:
    def test_hits_targets_on_spread_population(self):
        result = calibrate_intercept_and_k(_calibration_population(), default_params())
        assert result is not None
        assert abs(result["achieved_hi"] - CALIBRATION_TARGET_HI) <= 0.5
        assert abs(result["achieved_lo"] - CALIBRATION_TARGET_LO) <= 0.8
        assert result["achieved_hi"] > result["achieved_lo"] + 3.0

    def test_result_stays_on_the_ui_sliders_grids_and_bounds(self):
        result = calibrate_intercept_and_k(_calibration_population(), default_params())
        assert -3.0 <= result["intercept"] <= 1.0
        assert 1.0 <= result["sigmoid_k"] <= 25.0
        # K stays on the slider's 0.5 grid; intercept on its 0.01 grid.
        assert (result["sigmoid_k"] * 2) == int(result["sigmoid_k"] * 2)
        assert round(result["intercept"], 2) == result["intercept"]

    def test_does_not_mutate_caller_params(self):
        import copy
        params = default_params()
        before = copy.deepcopy(params)
        calibrate_intercept_and_k(_calibration_population(), params)
        assert params == before

    def test_deterministic(self):
        population = _calibration_population()
        a = calibrate_intercept_and_k(population, default_params())
        b = calibrate_intercept_and_k(population, default_params())
        assert a == b

    def test_custom_targets_shift_the_result(self):
        population = _calibration_population()
        wide = calibrate_intercept_and_k(
            population, default_params(), target_hi=9.5, target_lo=2.0)
        narrow = calibrate_intercept_and_k(
            population, default_params(), target_hi=8.5, target_lo=6.0)
        assert wide["achieved_hi"] - wide["achieved_lo"] > \
            narrow["achieved_hi"] - narrow["achieved_lo"]

    def test_too_few_companies_returns_none(self):
        assert calibrate_intercept_and_k({}, default_params()) is None
        single = dict(list(_calibration_population().items())[:1])
        assert calibrate_intercept_and_k(single, default_params()) is None

    def test_large_population_is_thinned_deterministically(self):
        population = _calibration_population(n=800)
        result = calibrate_intercept_and_k(
            population, default_params(), max_companies=300)
        assert result["n_companies_used"] == 300

    def test_collect_features_recovers_v2_debug_row_size(self):
        # Same recovery chain as the actual re-score: a v2-era record with
        # scoring_inputs.employee_range=None but a Lusha range in the debug
        # row must contribute its REAL size score, not the neutral 5.5.
        detail = {
            "scoring_inputs": {"employee_range": None,
                               "signals": {f: 2 for f in LEAN_COEFFICIENTS}},
            "debug": {"lead_prioritizer_row": {
                "lusha_employee_range": "1001 - 5000"}},
        }
        features = collect_calibration_features({"a": detail}, default_params())
        assert len(features) == 1
        assert features[0][1] == 6.63  # the "1001 - 5000" band, not 5.5


class TestSigmoidAnchorParamsFlowThroughScoring:
    def test_default_anchors_leave_reference_score_unchanged(self):
        row = {f: 2 for f in LEAN_COEFFICIENTS}
        base = score_company(row)
        with_default_params = score_company(row, params=default_params())
        assert with_default_params["icp_similarity_score"] == base["icp_similarity_score"]

    def test_sigmoid_curve_dataframe_accepts_custom_anchors(self):
        wide = sigmoid_curve_dataframe(k=10.0, n=100)
        narrow = sigmoid_curve_dataframe(k=10.0, n=100, p_lo=0.45, p_hi=0.55)
        # Narrower anchors -> a probability just above the high anchor
        # already maps to (clamped) 10.
        p_idx = 60  # p = 0.60
        assert narrow["icp_similarity_score"][p_idx] == 10.0
        assert wide["icp_similarity_score"][p_idx] < 10.0
