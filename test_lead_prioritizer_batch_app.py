"""Tests for the Lead Prioritizer v2 Streamlit batch app helper functions.

Only the pure helpers are tested — no Streamlit UI rendering, no live APIs, and
Streamlit itself is not required (the app imports it lazily inside ``main``).
"""

from __future__ import annotations

import pytest

import lead_prioritizer_batch_app as app
from lead_prioritizer_batch_app import (
    get_secret_or_env,
    resolve_default_column,
    resolve_default_input_country,
    count_selected_rows,
    mode_label_to_core_mode,
    build_download_filename,
    format_duration,
    build_progress_status_text,
    MODE_LABELS,
    SUPPORTED_DEFAULT_INPUT_COUNTRIES,
    DEFAULT_COUNTRY_PLACEHOLDER,
)


# ---------------------------------------------------------------------------
# get_secret_or_env
# ---------------------------------------------------------------------------

class TestSecretOrEnv:
    def test_prefers_secrets_over_env(self):
        val = get_secret_or_env("SERPER_API_KEY",
                                secrets={"SERPER_API_KEY": "from_secrets"},
                                env={"SERPER_API_KEY": "from_env"})
        assert val == "from_secrets"

    def test_falls_back_to_env(self):
        val = get_secret_or_env("SERPER_API_KEY",
                                secrets={},  # present but empty mapping
                                env={"SERPER_API_KEY": "from_env"})
        assert val == "from_env"

    def test_missing_returns_empty(self):
        assert get_secret_or_env("SERPER_API_KEY", secrets={}, env={}) == ""

    def test_empty_secret_value_falls_back_to_env(self):
        val = get_secret_or_env("K", secrets={"K": ""}, env={"K": "env_val"})
        assert val == "env_val"

    def test_secrets_access_error_falls_back(self):
        class _Boom:
            def __contains__(self, k):
                raise RuntimeError("no secrets.toml")

        assert get_secret_or_env("K", secrets=_Boom(), env={"K": "env_val"}) == "env_val"


# ---------------------------------------------------------------------------
# resolve_default_column
# ---------------------------------------------------------------------------

class TestResolveDefaultColumn:
    def test_exact_match_first(self):
        cols = ["id", "company_name", "domain"]
        assert resolve_default_column(cols, ["company_name", "name"]) == "company_name"

    def test_case_insensitive_fallback(self):
        # Pure case difference (not space/underscore) resolves via lowercasing.
        cols = ["ID", "COMPANY_NAME", "Domain"]
        assert resolve_default_column(cols, ["company_name"]) == "COMPANY_NAME"

    def test_exact_space_variant_candidate(self):
        # Real usage passes both variants; the "Company Name" candidate matches.
        cols = ["ID", "Company Name", "Domain"]
        assert resolve_default_column(cols, ["company_name", "Company Name"]) == "Company Name"

    def test_none_when_absent(self):
        assert resolve_default_column(["a", "b"], ["company_name"]) is None

    def test_priority_order(self):
        cols = ["name", "company_name"]
        # company_name is first candidate → preferred even though name exists
        assert resolve_default_column(cols, ["company_name", "name"]) == "company_name"


# ---------------------------------------------------------------------------
# count_selected_rows
# ---------------------------------------------------------------------------

class TestCountSelectedRows:
    def test_limit_zero_is_all_remaining(self):
        assert count_selected_rows(100, 0, 0) == 100
        assert count_selected_rows(100, 30, 0) == 70

    def test_nonzero_limit(self):
        assert count_selected_rows(100, 0, 10) == 10
        assert count_selected_rows(100, 95, 10) == 5  # only 5 remain

    def test_start_beyond_end(self):
        assert count_selected_rows(10, 20, 0) == 0
        assert count_selected_rows(10, 20, 5) == 0

    def test_limit_larger_than_remaining(self):
        assert count_selected_rows(3, 0, 100) == 3


# ---------------------------------------------------------------------------
# mode_label_to_core_mode
# ---------------------------------------------------------------------------

class TestModeMapping:
    def test_all_labels_map(self):
        expected = {
            "Full v2 enrichment": "full",
            "HQ only": "hq_only",
            "Evidence only": "evidence_only",
            "Signals, no score": "signals_no_score",
            "Full, no score": "full_no_score",
        }
        for label, mode in expected.items():
            assert mode_label_to_core_mode(label) == mode

    def test_default_label_is_full(self):
        assert MODE_LABELS[0] == "Full v2 enrichment"
        assert mode_label_to_core_mode(MODE_LABELS[0]) == "full"

    def test_unknown_label_raises(self):
        with pytest.raises(ValueError):
            mode_label_to_core_mode("Nope")


# ---------------------------------------------------------------------------
# build_download_filename
# ---------------------------------------------------------------------------

class TestDownloadFilename:
    def test_contains_mode_and_extension(self):
        assert build_download_filename("full") == "lead_prioritizer_v2_full_enriched.xlsx"
        assert build_download_filename("hq_only").endswith(".xlsx")
        assert "hq_only" in build_download_filename("hq_only")


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    def test_basic(self):
        assert format_duration(0) == "00:00:00"
        assert format_duration(65) == "00:01:05"
        assert format_duration(3672) == "01:01:12"

    def test_negative_and_bad_input(self):
        assert format_duration(-5) == "00:00:00"
        assert format_duration(None) == "00:00:00"
        assert format_duration("x") == "00:00:00"


# ---------------------------------------------------------------------------
# build_progress_status_text
# ---------------------------------------------------------------------------

class TestProgressStatusText:
    def test_includes_counts_and_eta(self):
        payload = {
            "processed_rows": 17, "selected_rows": 100,
            "success_count": 16, "error_count": 1,
            "current_company_name": "BMW ITALIA SPA",
        }
        # started 192s ago; avg = 192/17 ≈ 11.29s/row; remaining ≈ 937s
        text = build_progress_status_text(payload, started_at=1000.0, now=1192.0)
        assert "Processed 17/100" in text
        assert "Success 16" in text
        assert "Errors 1" in text
        assert "Current: BMW ITALIA SPA" in text
        assert "Elapsed 00:03:12" in text
        assert "ETA " in text and "Finish around " in text

    def test_zero_processed_is_unknown_eta(self):
        payload = {"processed_rows": 0, "selected_rows": 10,
                   "success_count": 0, "error_count": 0, "current_company_name": "X"}
        text = build_progress_status_text(payload, started_at=1000.0, now=1000.0)
        assert "ETA unknown" in text
        assert "Finish around" not in text

    def test_no_secrets_in_text(self):
        payload = {"processed_rows": 1, "selected_rows": 2, "success_count": 1,
                   "error_count": 0, "current_company_name": "Acme"}
        text = build_progress_status_text(payload, started_at=0.0, now=10.0)
        assert "api_key" not in text.lower() and "sk-ant" not in text.lower()


# ---------------------------------------------------------------------------
# resolve_default_input_country
# ---------------------------------------------------------------------------

class TestResolveDefaultInputCountry:
    def test_placeholder_is_rejected(self):
        country, error = resolve_default_input_country(DEFAULT_COUNTRY_PLACEHOLDER)
        assert country is None
        assert error == "Please select a default input country before running."

    def test_blank_is_rejected(self):
        country, error = resolve_default_input_country("")
        assert country is None
        assert error

    @pytest.mark.parametrize("selected", ["Italy", "Brazil", "Uruguay"])
    def test_supported_countries_are_valid(self, selected):
        country, error = resolve_default_input_country(selected)
        assert country == selected
        assert error is None

    def test_unknown_country_is_rejected(self):
        country, error = resolve_default_input_country("Atlantis")
        assert country is None
        assert error

    def test_supported_list_contains_brazil_and_uruguay(self):
        # Guards against regressing to an Italy-only default.
        assert "Italy" in SUPPORTED_DEFAULT_INPUT_COUNTRIES
        assert "Brazil" in SUPPORTED_DEFAULT_INPUT_COUNTRIES
        assert "Uruguay" in SUPPORTED_DEFAULT_INPUT_COUNTRIES


# ---------------------------------------------------------------------------
# Module import guard
# ---------------------------------------------------------------------------

def test_module_imports_without_streamlit():
    # Importing the app module must not require Streamlit (lazy import in main).
    assert hasattr(app, "main")
    assert app.CONFIRM_THRESHOLD == 50
