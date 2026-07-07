"""Tests for input_cleaner_lusha_edition.py (Layer 0, standalone app).

No network / live keys: the Anthropic prescreen call is mocked. Covers
column mapping, deduplication, industry exclusion rules (incl. the
Education/Higher-Education exception and the blank-Main-Industry
never-excluded rule), the intent hot-list override, and defensive JSON
parsing of the Haiku prescreen response.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd

import input_cleaner_lusha_edition as m


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

class TestColumnMapping:
    def test_detects_exact_lusha_headers(self):
        df = pd.DataFrame(columns=[
            "Company Name", "Company Domain", "Company Description",
            "Company Number of Employees", "Company Revenue",
            "Company Main Industry", "Company Sub Industry",
            "Company Country", "Company Intent Topics",
            "Company linkedin URL",
        ])
        mapping = m.detect_lusha_columns(df)
        assert mapping["name"] == "Company Name"
        assert mapping["domain"] == "Company Domain"
        assert mapping["description"] == "Company Description"
        assert mapping["employees"] == "Company Number of Employees"
        assert mapping["revenue"] == "Company Revenue"
        assert mapping["main_industry"] == "Company Main Industry"
        assert mapping["sub_industry"] == "Company Sub Industry"
        assert mapping["country"] == "Company Country"
        assert mapping["intent_topics"] == "Company Intent Topics"
        assert mapping["linkedin_url"] == "Company linkedin URL"
        assert m.missing_required_lusha_columns(mapping) == []

    def test_detects_differing_case_and_spacing(self):
        df = pd.DataFrame(columns=[
            "company_name", "COMPANY DOMAIN", "company   description",
            "Company-Main-Industry", "company sub_industry",
        ])
        mapping = m.detect_lusha_columns(df)
        assert mapping["name"] == "company_name"
        assert mapping["domain"] == "COMPANY DOMAIN"
        assert mapping["description"] == "company   description"
        assert mapping["main_industry"] == "Company-Main-Industry"
        assert mapping["sub_industry"] == "company sub_industry"

    def test_missing_required_columns_reported(self):
        df = pd.DataFrame(columns=["Company Description", "Company Main Industry"])
        mapping = m.detect_lusha_columns(df)
        missing = m.missing_required_lusha_columns(mapping)
        assert set(missing) == {"name", "domain"}

    def test_optional_columns_missing_are_none_not_error(self):
        df = pd.DataFrame(columns=["Company Name", "Company Domain"])
        mapping = m.detect_lusha_columns(df)
        assert m.missing_required_lusha_columns(mapping) == []
        assert mapping["intent_topics"] is None
        assert mapping["linkedin_url"] is None


# ---------------------------------------------------------------------------
# Step 1 — deduplication
# ---------------------------------------------------------------------------

class TestDedupeByDomain:
    def test_duplicate_domain_least_filled_row_loses(self):
        df = pd.DataFrame({
            "Company Name": ["Acme AG", "Acme"],
            "Company Domain": ["acme.ch", "acme.ch"],
            "Company Description": ["Full description here.", ""],
            "Company Main Industry": ["Manufacturing", ""],
        })
        deduped, removed = m.dedupe_by_domain(df, "Company Domain")
        assert removed == 1
        assert len(deduped) == 1
        assert deduped.iloc[0]["Company Name"] == "Acme AG"

    def test_normalizes_domain_before_comparing(self):
        df = pd.DataFrame({
            "Company Name": ["Acme AG", "Acme Holding"],
            "Company Domain": ["https://www.acme.ch/", "acme.ch"],
        })
        deduped, removed = m.dedupe_by_domain(df, "Company Domain")
        assert removed == 1
        assert len(deduped) == 1

    def test_blank_domain_rows_never_deduplicated_against_each_other(self):
        df = pd.DataFrame({
            "Company Name": ["A", "B", "C"],
            "Company Domain": ["", "", "acme.ch"],
        })
        deduped, removed = m.dedupe_by_domain(df, "Company Domain")
        assert removed == 0
        assert len(deduped) == 3

    def test_tie_keeps_first_encountered_row(self):
        df = pd.DataFrame({
            "Company Name": ["First Co", "Second Co"],
            "Company Domain": ["tie.ch", "tie.ch"],
            "Company Description": ["Same length here.", "Same length here."],
        })
        deduped, removed = m.dedupe_by_domain(df, "Company Domain")
        assert removed == 1
        assert deduped.iloc[0]["Company Name"] == "First Co"

    def test_no_duplicates_removes_nothing(self):
        df = pd.DataFrame({
            "Company Name": ["A", "B"],
            "Company Domain": ["a.ch", "b.ch"],
        })
        deduped, removed = m.dedupe_by_domain(df, "Company Domain")
        assert removed == 0
        assert len(deduped) == 2


# ---------------------------------------------------------------------------
# Step 2 — industry exclusion rules
# ---------------------------------------------------------------------------

class TestIndustryExclusion:
    def _config(self, **overrides):
        return m.IndustryExclusionConfig(**overrides)

    def test_government_excluded(self):
        reason = m.classify_industry_exclusion("Government", "", self._config())
        assert reason == "Main Industry = Government"

    def test_government_rule_can_be_toggled_off(self):
        reason = m.classify_industry_exclusion(
            "Government", "", self._config(exclude_government=False))
        assert reason == ""

    def test_nonprofit_excluded(self):
        reason = m.classify_industry_exclusion(
            "Community & Nonprofit Organizations", "", self._config())
        assert "Nonprofit" in reason

    def test_education_excluded_by_default(self):
        reason = m.classify_industry_exclusion("Education", "Primary Education", self._config())
        assert reason.startswith("Main Industry = Education")

    def test_education_higher_education_exception_kept(self):
        reason = m.classify_industry_exclusion("Education", "Higher Education", self._config())
        assert reason == ""

    def test_education_training_exception_kept(self):
        reason = m.classify_industry_exclusion("Education", "Training", self._config())
        assert reason == ""

    def test_education_elearning_exception_kept(self):
        reason = m.classify_industry_exclusion(
            "Education", "E-Learning Providers", self._config())
        assert reason == ""

    def test_education_exception_is_case_insensitive(self):
        reason = m.classify_industry_exclusion("education", "higher education", self._config())
        assert reason == ""

    def test_care_delivery_sub_industries_excluded(self):
        for sub in (
            "Nursing Homes & Residential Care Facilities",
            "Community & Home Healthcare Services",
            "Medical Practices",
            "Mental Health Care",
            "Alternative Medicine",
            "Veterinary Services",
        ):
            reason = m.classify_industry_exclusion("Healthcare", sub, self._config())
            assert reason != "", f"expected exclusion for sub_industry={sub!r}"

    def test_hospitals_and_biotech_stay_in(self):
        # Hospitals / biotech are explicitly NOT in the care-delivery
        # exclusion set (only care-DELIVERY sub-industries are excluded).
        assert m.classify_industry_exclusion("Hospital & Health Care", "Hospitals", self._config()) == ""
        assert m.classify_industry_exclusion("Biotechnology", "Biotechnology Research", self._config()) == ""

    def test_care_delivery_rule_can_be_toggled_off(self):
        reason = m.classify_industry_exclusion(
            "Healthcare", "Medical Practices", self._config(exclude_care_delivery=False))
        assert reason == ""

    def test_blank_main_industry_never_excluded(self):
        assert m.classify_industry_exclusion("", "", self._config()) == ""
        assert m.classify_industry_exclusion(None, "Medical Practices", self._config()) == ""
        assert m.classify_industry_exclusion("   ", "", self._config()) == ""

    def test_unrelated_industry_not_excluded(self):
        reason = m.classify_industry_exclusion("Manufacturing", "Industrial Machinery", self._config())
        assert reason == ""


# ---------------------------------------------------------------------------
# Step 3 — hot list / intent override
# ---------------------------------------------------------------------------

class TestHotListAndIntentOverride:
    def _df(self):
        return pd.DataFrame({
            "Company Name": ["Gov Co", "Normal Co", "Gov Hot Co"],
            "Company Domain": ["gov.ch", "normal.ch", "govhot.ch"],
            "Company Main Industry": ["Government", "Manufacturing", "Government"],
            "Company Sub Industry": ["", "Machinery", ""],
            "Company Intent Topics": ["", "", "Cultural Training"],
        })

    def test_has_intent_topics(self):
        assert m.has_intent_topics("Cultural Training") is True
        assert m.has_intent_topics("") is False
        assert m.has_intent_topics(None) is False
        assert m.has_intent_topics("nan") is False

    def test_plain_excluded_row_has_no_warning(self):
        mapping = m.detect_lusha_columns(self._df())
        out = m.classify_rows(self._df(), mapping, m.IndustryExclusionConfig())
        gov_row = out[out["Company Name"] == "Gov Co"].iloc[0]
        assert gov_row["excluded"] == True  # noqa: E712
        assert gov_row["intent_override_warning"] == False  # noqa: E712
        assert gov_row["hot_list"] == False  # noqa: E712

    def test_intent_row_matching_exclusion_rule_is_kept_with_warning(self):
        mapping = m.detect_lusha_columns(self._df())
        out = m.classify_rows(self._df(), mapping, m.IndustryExclusionConfig())
        hot_gov_row = out[out["Company Name"] == "Gov Hot Co"].iloc[0]
        assert hot_gov_row["hot_list"] == True  # noqa: E712
        assert hot_gov_row["industry_exclusion_reason"] != ""
        assert hot_gov_row["intent_override_warning"] == True  # noqa: E712
        # The whole point: never actually excluded despite matching a rule.
        assert hot_gov_row["excluded"] == False  # noqa: E712

    def test_normal_row_not_excluded_no_warning(self):
        mapping = m.detect_lusha_columns(self._df())
        out = m.classify_rows(self._df(), mapping, m.IndustryExclusionConfig())
        normal_row = out[out["Company Name"] == "Normal Co"].iloc[0]
        assert normal_row["excluded"] == False  # noqa: E712
        assert normal_row["intent_override_warning"] == False  # noqa: E712


# ---------------------------------------------------------------------------
# Step 4 — Haiku prescreen (mocked, no network)
# ---------------------------------------------------------------------------

def _mock_anthropic(response_text: str):
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=response_text)]
    mock_msg.usage = MagicMock(input_tokens=250, output_tokens=30)
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    mock_lib = MagicMock()
    mock_lib.Anthropic.return_value = mock_client
    return patch("input_cleaner_lusha_edition._anthropic_lib", mock_lib)


class TestPrescreenResponseParsing:
    def test_parses_likely_fit(self):
        raw = json.dumps({"icp_prescreen": "likely_fit", "reason": "Multinational manufacturer."})
        parsed = m.parse_prescreen_response(raw)
        assert parsed == {"icp_prescreen": "likely_fit", "reason": "Multinational manufacturer."}

    def test_parses_unclear(self):
        raw = json.dumps({"icp_prescreen": "unclear", "reason": "Too thin a description."})
        parsed = m.parse_prescreen_response(raw)
        assert parsed["icp_prescreen"] == "unclear"

    def test_parses_unlikely_fit(self):
        raw = json.dumps({"icp_prescreen": "unlikely_fit", "reason": "Local nonprofit."})
        parsed = m.parse_prescreen_response(raw)
        assert parsed["icp_prescreen"] == "unlikely_fit"

    def test_strips_markdown_fences(self):
        raw = "```json\n" + json.dumps({"icp_prescreen": "likely_fit", "reason": "ok"}) + "\n```"
        parsed = m.parse_prescreen_response(raw)
        assert parsed["icp_prescreen"] == "likely_fit"

    def test_broken_json_never_raises(self):
        parsed = m.parse_prescreen_response("{not valid json at all")
        assert parsed == {"icp_prescreen": "", "reason": ""}

    def test_empty_response_never_raises(self):
        assert m.parse_prescreen_response("") == {"icp_prescreen": "", "reason": ""}

    def test_unknown_label_is_discarded(self):
        raw = json.dumps({"icp_prescreen": "definitely_maybe", "reason": "x"})
        parsed = m.parse_prescreen_response(raw)
        assert parsed["icp_prescreen"] == ""

    def test_non_dict_json_never_raises(self):
        assert m.parse_prescreen_response("[1, 2, 3]") == {"icp_prescreen": "", "reason": ""}


class TestEligibleForPrescreen:
    def test_blank_main_industry_with_description_is_eligible(self):
        df = pd.DataFrame({
            "Company Name": ["A", "B", "C"],
            "Company Domain": ["a.ch", "b.ch", "c.ch"],
            "Company Main Industry": ["", "Manufacturing", ""],
            "Company Description": ["Some description.", "Some description.", ""],
        })
        mapping = m.detect_lusha_columns(df)
        mask = m.eligible_for_prescreen(df, mapping)
        assert list(mask) == [True, False, False]


class TestPrescreenRowsWithAi:
    def _df(self):
        return pd.DataFrame({
            "Company Name": ["Alpha", "Beta"],
            "Company Domain": ["alpha.ch", "beta.ch"],
            "Company Main Industry": ["", ""],
            "Company Description": ["Global manufacturer of parts.", "Tiny local shop."],
            "Company Country": ["Switzerland", "Switzerland"],
        })

    def test_successful_prescreen_populates_columns(self):
        df = self._df()
        mapping = m.detect_lusha_columns(df)
        eligible = m.eligible_for_prescreen(df, mapping)
        raw = json.dumps({"icp_prescreen": "likely_fit", "reason": "Looks like a fit."})
        with _mock_anthropic(raw):
            out = m.prescreen_rows_with_ai(df, mapping, eligible, "fake-key")
        assert (out["icp_prescreen"] == "likely_fit").all()
        assert (out["icp_prescreen_error"] == "").all()

    def test_broken_json_response_does_not_crash_the_batch(self):
        df = self._df()
        mapping = m.detect_lusha_columns(df)
        eligible = m.eligible_for_prescreen(df, mapping)
        with _mock_anthropic("not valid json{{"):
            out = m.prescreen_rows_with_ai(df, mapping, eligible, "fake-key")
        # No crash; rows just get a blank prescreen label.
        assert (out["icp_prescreen"] == "").all()
        assert (out["icp_prescreen_error"] == "").all()

    def test_api_exception_is_isolated_per_row(self):
        df = self._df()
        mapping = m.detect_lusha_columns(df)
        eligible = m.eligible_for_prescreen(df, mapping)
        mock_lib = MagicMock()
        mock_lib.Anthropic.side_effect = RuntimeError("api down")
        with patch("input_cleaner_lusha_edition._anthropic_lib", mock_lib):
            out = m.prescreen_rows_with_ai(df, mapping, eligible, "fake-key")
        assert (out["icp_prescreen"] == "").all()
        assert out["icp_prescreen_error"].str.contains("api down").all()

    def test_non_eligible_rows_are_untouched(self):
        df = pd.DataFrame({
            "Company Name": ["Alpha", "Beta"],
            "Company Domain": ["alpha.ch", "beta.ch"],
            "Company Main Industry": ["Manufacturing", ""],
            "Company Description": ["desc", "desc"],
        })
        mapping = m.detect_lusha_columns(df)
        eligible = m.eligible_for_prescreen(df, mapping)
        raw = json.dumps({"icp_prescreen": "likely_fit", "reason": "ok"})
        with _mock_anthropic(raw):
            out = m.prescreen_rows_with_ai(df, mapping, eligible, "fake-key")
        assert out.iloc[0]["icp_prescreen"] == ""   # Manufacturing row -- not eligible
        assert out.iloc[1]["icp_prescreen"] == "likely_fit"


class TestEstimatePrescreenCost:
    def test_zero_eligible_rows_returns_zero_cost(self):
        df = pd.DataFrame({
            "Company Name": ["A"], "Company Domain": ["a.ch"],
            "Company Main Industry": ["Manufacturing"], "Company Description": ["d"],
        })
        mapping = m.detect_lusha_columns(df)
        eligible = m.eligible_for_prescreen(df, mapping)
        cost = m.estimate_prescreen_cost(df, mapping, eligible)
        assert cost == {"eligible_rows": 0, "estimated_input_tokens": 0,
                         "estimated_output_tokens": 0, "estimated_cost_usd": 0.0}

    def test_nonzero_eligible_rows_estimates_positive_cost(self):
        df = pd.DataFrame({
            "Company Name": ["A"], "Company Domain": ["a.ch"],
            "Company Main Industry": [""],
            "Company Description": ["A fairly long description of what this company does."],
        })
        mapping = m.detect_lusha_columns(df)
        eligible = m.eligible_for_prescreen(df, mapping)
        cost = m.estimate_prescreen_cost(df, mapping, eligible)
        assert cost["eligible_rows"] == 1
        assert cost["estimated_input_tokens"] > 0
        assert cost["estimated_output_tokens"] > 0
        assert cost["estimated_cost_usd"] > 0


# ---------------------------------------------------------------------------
# Step 5 — sorting and batch-app column compatibility
# ---------------------------------------------------------------------------

class TestSortAndBatchAppColumns:
    def test_hot_list_sorted_first(self):
        df = pd.DataFrame({
            "Company Name": ["Big Co", "Hot Small Co"],
            "Company Domain": ["big.ch", "hotsmall.ch"],
            "Company Number of Employees": ["10000", "10"],
            "hot_list": [False, True],
        })
        mapping = m.detect_lusha_columns(df)
        out = m.sort_selected_rows(df, mapping)
        assert out.iloc[0]["Company Name"] == "Hot Small Co"

    def test_within_same_hot_list_status_sorted_by_size_desc(self):
        df = pd.DataFrame({
            "Company Name": ["Small Co", "Big Co"],
            "Company Domain": ["small.ch", "big.ch"],
            "Company Number of Employees": ["10-50", "1001-5000"],
            "hot_list": [False, False],
        })
        mapping = m.detect_lusha_columns(df)
        out = m.sort_selected_rows(df, mapping)
        assert out.iloc[0]["Company Name"] == "Big Co"

    def test_parse_employee_count_handles_ranges_and_blanks(self):
        assert m.parse_employee_count("51-200") == 200.0
        assert m.parse_employee_count("10,001+") == 10001.0
        assert m.parse_employee_count("42") == 42.0
        assert m.parse_employee_count("") == -1.0
        assert m.parse_employee_count(None) == -1.0

    def test_batch_app_compatible_columns_added(self):
        df = pd.DataFrame({
            "Company Name": ["Acme AG"],
            "Company Domain": ["https://www.acme.ch/"],
            "Company Country": ["Switzerland"],
        })
        mapping = m.detect_lusha_columns(df)
        out = m.add_batch_app_compatible_columns(df, mapping)
        assert out.iloc[0]["domain"] == "acme.ch"
        assert out.iloc[0]["country"] == "Switzerland"
        # Original Lusha columns are preserved, not replaced.
        assert "Company Domain" in out.columns
        assert "Company Country" in out.columns
