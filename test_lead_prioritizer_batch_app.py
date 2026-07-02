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
    build_phase_progress_status_text,
    autosave_output_workbook,
    sanitize_run_mode_for_filename,
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
# build_phase_progress_status_text (foreign-HQ-only mode)
# ---------------------------------------------------------------------------

class TestPhaseProgressStatusText:
    def test_phase1_renders_label_counts_and_company(self):
        payload = {
            "phase": 1, "phase_count": 3, "phase_label": "HQ screening",
            "phase_processed": 3, "phase_total": 30,
            "success_count": 3, "error_count": 0,
            "current_company_name": "Macromercado",
        }
        text = build_phase_progress_status_text(payload, started_at=1000.0, now=1065.0)
        assert "Phase 1/3: HQ screening" in text
        assert "Processed 3/30" in text
        assert "Success 3" in text and "Errors 0" in text
        assert "Current: Macromercado" in text
        assert "Elapsed 00:01:05" in text
        assert "0/0" not in text

    def test_phase2_without_success_counts(self):
        payload = {
            "phase": 2, "phase_count": 3, "phase_label": "C5 adjudication",
            "phase_processed": 1, "phase_total": 4,
            "current_company_name": "Toto Calzados",
        }
        text = build_phase_progress_status_text(payload, started_at=0.0, now=10.0)
        assert "Phase 2/3: C5 adjudication" in text
        assert "Processed 1/4" in text
        assert "Success" not in text and "Errors" not in text
        assert "Current: Toto Calzados" in text

    def test_phase3_label(self):
        payload = {
            "phase": 3, "phase_count": 3,
            "phase_label": "Full enrichment for confirmed foreign-HQ leads",
            "phase_processed": 2, "phase_total": 5,
            "success_count": 2, "error_count": 0,
            "current_company_name": "Gestam",
        }
        text = build_phase_progress_status_text(payload, started_at=0.0, now=1.0)
        assert "Phase 3/3: Full enrichment for confirmed foreign-HQ leads" in text
        assert "Processed 2/5" in text

    def test_falls_back_to_plain_status_without_phase(self):
        payload = {
            "processed_rows": 2, "selected_rows": 5, "success_count": 2,
            "error_count": 0, "current_company_name": "Acme",
        }
        assert build_phase_progress_status_text(payload, 0.0, now=10.0) == \
            build_progress_status_text(payload, 0.0, now=10.0)

    def test_no_secrets_in_text(self):
        payload = {"phase": 1, "phase_label": "HQ screening",
                   "phase_processed": 1, "phase_total": 2,
                   "current_company_name": "Acme"}
        text = build_phase_progress_status_text(payload, 0.0, now=5.0)
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


# ---------------------------------------------------------------------------
# autosave_output_workbook
# ---------------------------------------------------------------------------

from datetime import datetime as _dt  # noqa: E402

_FIXED_NOW = _dt(2026, 7, 2, 2, 15, 30)


class TestAutosaveOutputWorkbook:
    def test_creates_missing_directory_and_writes_timestamped_xlsx(self, tmp_path):
        out_dir = tmp_path / "nested" / "batch_outputs"
        assert not out_dir.exists()
        path = autosave_output_workbook(
            b"workbook-bytes", str(out_dir), "full_foreign_hq_only", now=_FIXED_NOW)
        assert out_dir.is_dir()
        assert path.name == "lead_prioritizer_v2_full_foreign_hq_only_20260702_021530.xlsx"
        assert path.read_bytes() == b"workbook-bytes"

    def test_does_not_overwrite_existing_files(self, tmp_path):
        p1 = autosave_output_workbook(b"first", str(tmp_path), "full", now=_FIXED_NOW)
        p2 = autosave_output_workbook(b"second", str(tmp_path), "full", now=_FIXED_NOW)
        p3 = autosave_output_workbook(b"third", str(tmp_path), "full", now=_FIXED_NOW)
        assert p1 != p2 != p3
        assert p1.read_bytes() == b"first"   # original untouched
        assert p2.read_bytes() == b"second"
        assert p2.name.endswith("_2.xlsx")
        assert p3.name.endswith("_3.xlsx")

    def test_relative_path_resolves_under_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = autosave_output_workbook(b"x", "batch_outputs", "hq_only", now=_FIXED_NOW)
        assert path.parent == (tmp_path / "batch_outputs").resolve()
        assert path.exists()

    def test_run_mode_is_sanitized_in_filename(self, tmp_path):
        path = autosave_output_workbook(
            b"x", str(tmp_path), "weird/../mode name!", now=_FIXED_NOW)
        assert "/" not in path.name and ".." not in path.name and " " not in path.name
        assert path.name.startswith("lead_prioritizer_v2_weird_mode_name_")
        assert path.suffix == ".xlsx"

    def test_unwritable_path_raises(self, tmp_path):
        blocker = tmp_path / "not_a_dir"
        blocker.write_bytes(b"i am a file")  # mkdir on a path through a file fails
        with pytest.raises(Exception):
            autosave_output_workbook(b"x", str(blocker / "sub"), "full", now=_FIXED_NOW)


class TestSanitizeRunMode:
    def test_known_modes_unchanged(self):
        assert sanitize_run_mode_for_filename("full_foreign_hq_only") == "full_foreign_hq_only"
        assert sanitize_run_mode_for_filename("hq_only") == "hq_only"

    def test_blank_falls_back(self):
        assert sanitize_run_mode_for_filename("") == "run"
        assert sanitize_run_mode_for_filename(None) == "run"


# ---------------------------------------------------------------------------
# Parallel-run helpers (manifest / directory resolution / constants)
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402

from lead_prioritizer_batch_app import (  # noqa: E402
    resolve_autosave_directory,
    write_parallel_run_manifest,
    PARALLEL_WORKER_CHOICES,
    PARALLEL_HELP_TEXT,
    PARALLEL_WARNING_TEXT,
)


class TestParallelHelpers:
    def test_worker_choices_capped_at_4(self):
        assert PARALLEL_WORKER_CHOICES == [1, 2, 3, 4]
        assert PARALLEL_HELP_TEXT and PARALLEL_WARNING_TEXT

    def test_resolve_autosave_directory_relative(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_autosave_directory("batch_outputs") == tmp_path / "batch_outputs"

    def test_resolve_autosave_directory_absolute(self, tmp_path):
        assert resolve_autosave_directory(str(tmp_path / "abs")) == tmp_path / "abs"

    def test_write_parallel_run_manifest(self, tmp_path):
        manifest = {
            "run_mode": "full_foreign_hq_only",
            "selected_rows": 30,
            "workers": 3,
            "chunk_count": 3,
            "chunks": [
                {"chunk_index": 1, "row_count": 10, "source_index_first": 0,
                 "source_index_last": 9, "success": True, "error": "",
                 "output_file": "chunk_001_output.xlsx"},
                {"chunk_index": 2, "row_count": 10, "source_index_first": 10,
                 "source_index_last": 19, "success": False,
                 "error": "RuntimeError: boom", "output_file": ""},
            ],
            "combined_output_file": "lead_prioritizer_v2_full_foreign_hq_only_combined_20260702_021530.xlsx",
        }
        path = write_parallel_run_manifest(tmp_path, manifest)
        assert path.name == "run_manifest.json"
        loaded = _json.loads(path.read_text(encoding="utf-8"))
        assert loaded["workers"] == 3
        assert loaded["chunks"][1]["success"] is False
        assert loaded["combined_output_file"].endswith(".xlsx")
        # manifest never contains keys/secrets
        text = path.read_text(encoding="utf-8").lower()
        assert "api_key" not in text and "sk-ant" not in text
