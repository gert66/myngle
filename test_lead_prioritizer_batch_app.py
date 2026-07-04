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
    build_parallel_progress_status_text,
    build_chunk_detail_line,
    format_local_time,
    PARALLEL_PROGRESS_NOTE_TEXT,
    RUN_BUTTON_NOTE_TEXT,
    autosave_output_workbook,
    sanitize_run_mode_for_filename,
    sanitize_filename_part,
    clean_user_path,
    resolve_batch_output_dir,
    make_batch_output_filename,
    make_parallel_run_folder_name,
    MODE_LABELS,
    SUPPORTED_DEFAULT_INPUT_COUNTRIES,
    DEFAULT_COUNTRY_PLACEHOLDER,
    NON_ENGLISH_FOREIGN_HQ_ONLY_HELP_TEXT,
)
from lead_prioritizer_batch_core import (
    NON_ENGLISH_FOREIGN_HQ_ONLY_MODE as _NON_ENGLISH_MODE_CONST,
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
            b"workbook-bytes", str(out_dir), "full_foreign_hq_only",
            country="Brazil", now=_FIXED_NOW)
        assert out_dir.is_dir()
        assert path.name == \
            "lead_prioritizer_v2_Brazil_full_foreign_hq_only_enriched_20260702_021530.xlsx"
        assert path.read_bytes() == b"workbook-bytes"

    def test_does_not_overwrite_existing_files(self, tmp_path):
        p1 = autosave_output_workbook(b"first", str(tmp_path), "full",
                                      country="Italy", now=_FIXED_NOW)
        p2 = autosave_output_workbook(b"second", str(tmp_path), "full",
                                      country="Italy", now=_FIXED_NOW)
        p3 = autosave_output_workbook(b"third", str(tmp_path), "full",
                                      country="Italy", now=_FIXED_NOW)
        assert p1 != p2 != p3
        assert p1.read_bytes() == b"first"   # original untouched
        assert p2.read_bytes() == b"second"
        assert p2.name.endswith("_2.xlsx")
        assert p3.name.endswith("_3.xlsx")

    def test_relative_path_resolves_under_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = autosave_output_workbook(b"x", "batch_outputs", "hq_only",
                                        country="Uruguay", now=_FIXED_NOW)
        assert path.parent == (tmp_path / "batch_outputs").resolve()
        assert path.exists()

    def test_run_mode_is_sanitized_in_filename(self, tmp_path):
        path = autosave_output_workbook(
            b"x", str(tmp_path), "weird/../mode name!", country="Italy", now=_FIXED_NOW)
        assert "/" not in path.name and ".." not in path.name and " " not in path.name
        assert path.name.startswith("lead_prioritizer_v2_Italy_weird_mode_name_enriched_")
        assert path.suffix == ".xlsx"

    def test_country_is_sanitized_in_filename(self, tmp_path):
        path = autosave_output_workbook(
            b"x", str(tmp_path), "hq_only", country="New Zealand", now=_FIXED_NOW)
        assert path.name == "lead_prioritizer_v2_New_Zealand_hq_only_enriched_20260702_021530.xlsx"

    def test_blank_country_falls_back_to_placeholder_token(self, tmp_path):
        path = autosave_output_workbook(b"x", str(tmp_path), "full", now=_FIXED_NOW)
        assert path.name == "lead_prioritizer_v2_Country_full_enriched_20260702_021530.xlsx"

    def test_unwritable_path_raises(self, tmp_path):
        blocker = tmp_path / "not_a_dir"
        blocker.write_bytes(b"i am a file")  # mkdir on a path through a file fails
        with pytest.raises(Exception):
            autosave_output_workbook(b"x", str(blocker / "sub"), "full",
                                     country="Italy", now=_FIXED_NOW)


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


# ---------------------------------------------------------------------------
# Australia + non-English foreign-HQ mode (app-level wiring)
# ---------------------------------------------------------------------------

class TestAustraliaNonEnglishModeWiring:
    def test_australia_in_supported_default_countries(self):
        assert "Australia" in SUPPORTED_DEFAULT_INPUT_COUNTRIES

    def test_resolve_default_input_country_australia(self):
        country, error = resolve_default_input_country("Australia")
        assert country == "Australia"
        assert error is None

    def test_mode_label_maps_to_core_constant(self):
        label = "Full enrichment, confirmed non-English foreign-HQ only"
        assert label in MODE_LABELS
        assert mode_label_to_core_mode(label) == _NON_ENGLISH_MODE_CONST
        assert _NON_ENGLISH_MODE_CONST == "full_non_english_foreign_hq_only"

    def test_existing_foreign_hq_only_label_unchanged(self):
        label = "Full enrichment, confirmed foreign-HQ only"
        assert label in MODE_LABELS
        assert mode_label_to_core_mode(label) == "full_foreign_hq_only"

    def test_help_text_present(self):
        assert NON_ENGLISH_FOREIGN_HQ_ONLY_HELP_TEXT
        assert "Australia" in NON_ENGLISH_FOREIGN_HQ_ONLY_HELP_TEXT


# ---------------------------------------------------------------------------
# Country list, output-directory resolution, and filename/foldername builders
# (country-organized outputs; see: Improve country output naming and folders)
# ---------------------------------------------------------------------------

class TestCountryListAlphabeticalAndComplete:
    def test_alphabetically_sorted(self):
        assert SUPPORTED_DEFAULT_INPUT_COUNTRIES == sorted(SUPPORTED_DEFAULT_INPUT_COUNTRIES)

    def test_includes_required_countries(self):
        for country in ("Australia", "Brazil", "Italy", "New Zealand", "Uruguay"):
            assert country in SUPPORTED_DEFAULT_INPUT_COUNTRIES

    def test_exact_list(self):
        assert SUPPORTED_DEFAULT_INPUT_COUNTRIES == \
            ["Australia", "Brazil", "Italy", "Netherlands", "New Zealand", "Uruguay"]

    def test_placeholder_is_first_in_dropdown_options(self):
        options = [DEFAULT_COUNTRY_PLACEHOLDER] + SUPPORTED_DEFAULT_INPUT_COUNTRIES
        assert options[0] == DEFAULT_COUNTRY_PLACEHOLDER
        assert options[1:] == sorted(options[1:])

    def test_new_zealand_is_valid_default_country(self):
        country, error = resolve_default_input_country("New Zealand")
        assert country == "New Zealand"
        assert error is None

    def test_netherlands_is_valid_default_country(self):
        country, error = resolve_default_input_country("Netherlands")
        assert country == "Netherlands"
        assert error is None


class TestSanitizeFilenamePart:
    def test_spaces_become_underscores(self):
        assert sanitize_filename_part("New Zealand") == "New_Zealand"

    def test_unsafe_characters_removed(self):
        assert sanitize_filename_part('Bra<>zil:"/\\|?*') == "Bra_zil"

    def test_blank_uses_fallback(self):
        assert sanitize_filename_part("", fallback="Country") == "Country"
        assert sanitize_filename_part(None, fallback="Country") == "Country"


class TestCleanUserPath:
    def test_strips_surrounding_double_quotes(self):
        p = clean_user_path('"C:\\Data\\Brazil"')
        assert str(p) == "C:\\Data\\Brazil"

    def test_strips_surrounding_single_quotes(self):
        p = clean_user_path("'/home/user/Brazil'")
        assert str(p) == "/home/user/Brazil"

    def test_blank_returns_none(self):
        assert clean_user_path("") is None
        assert clean_user_path("   ") is None
        assert clean_user_path(None) is None

    def test_expands_home(self):
        p = clean_user_path("~/Brazil")
        assert not str(p).startswith("~")

    def test_plain_path_unchanged(self):
        assert str(clean_user_path("/data/brazil")) == "/data/brazil"


class TestResolveBatchOutputDir:
    def test_resolves_from_source_folder(self):
        result = resolve_batch_output_dir("C:/Data/Brazil")
        assert str(result).replace("\\", "/") == "C:/Data/Brazil/lead_prioritizer_outputs"

    def test_falls_back_to_batch_outputs_when_blank(self):
        assert str(resolve_batch_output_dir("")) == "batch_outputs"
        assert str(resolve_batch_output_dir(None)) == "batch_outputs"

    def test_strips_quotes_around_windows_path(self):
        result = resolve_batch_output_dir('"C:\\Data\\Brazil"')
        assert str(result).replace("\\", "/").endswith("Data/Brazil/lead_prioritizer_outputs")

    def test_never_downloads_as_fallback(self):
        assert "Downloads" not in str(resolve_batch_output_dir(""))
        assert "Downloads" not in str(resolve_batch_output_dir("C:/Data/Brazil"))


class TestMakeBatchOutputFilename:
    def test_brazil_foreign_hq_only(self):
        stamp = _dt(2026, 7, 2, 23, 15, 0)
        assert make_batch_output_filename("Brazil", "full_foreign_hq_only", stamp) == \
            "lead_prioritizer_v2_Brazil_full_foreign_hq_only_enriched_20260702_231500.xlsx"

    def test_australia_hq_only(self):
        stamp = _dt(2026, 7, 2, 23, 15, 0)
        assert make_batch_output_filename("Australia", "hq_only", stamp) == \
            "lead_prioritizer_v2_Australia_hq_only_enriched_20260702_231500.xlsx"

    def test_new_zealand_sanitizes_space(self):
        stamp = _dt(2026, 7, 2, 23, 15, 0)
        name = make_batch_output_filename("New Zealand", "full", stamp)
        assert "New_Zealand" in name
        assert " " not in name
        assert name.startswith("lead_prioritizer_v2_New_Zealand_full_enriched_")
        assert name.endswith(".xlsx")

    def test_includes_all_required_components(self):
        stamp = _dt(2026, 7, 2, 23, 15, 0)
        name = make_batch_output_filename("Italy", "signals_no_score", stamp)
        assert "Italy" in name
        assert "signals_no_score" in name
        assert "enriched" in name
        assert "20260702_231500" in name


class TestMakeParallelRunFolderName:
    def test_shape(self):
        stamp = _dt(2026, 7, 2, 23, 15, 0)
        assert make_parallel_run_folder_name("Brazil", "full_foreign_hq_only", stamp) == \
            "run_Brazil_full_foreign_hq_only_20260702_231500"

    def test_new_zealand_sanitized(self):
        stamp = _dt(2026, 7, 2, 23, 15, 0)
        name = make_parallel_run_folder_name("New Zealand", "hq_only", stamp)
        assert " " not in name
        assert "New_Zealand" in name


# ---------------------------------------------------------------------------
# format_local_time / build_parallel_progress_status_text
# ---------------------------------------------------------------------------

class TestFormatLocalTime:
    def test_returns_hh_mm_ss_shape(self):
        text = format_local_time(1000.0)
        assert len(text) == 8
        assert text.count(":") == 2

    def test_defaults_to_now_without_error(self):
        assert len(format_local_time()) == 8


class TestParallelProgressStatusText:
    def test_heartbeat_headline(self):
        payload = {
            "heartbeat": True, "parallel_chunks_total": 4, "parallel_chunks_completed": 1,
            "parallel_workers": 4, "processed_rows": 120, "selected_rows": 671,
            "success_count": 115, "error_count": 5, "current_company_name": "Acme Brasil",
            "active_chunks": [
                {"chunk_index": 2, "processed": 40, "selected": 168,
                 "phase_label": "HQ screening", "current_company_name": "Acme Brasil"},
            ],
        }
        text = build_parallel_progress_status_text(payload, started_at=1000.0, now=1090.0)
        assert "Still running; waiting for worker results..." in text
        assert "Chunks 1/4" in text
        assert "Workers 4" in text
        assert "Processed 120/671" in text
        assert "Success 115" in text
        assert "Errors 5" in text
        assert "Phase: HQ screening" in text
        assert "Current: Acme Brasil" in text
        assert "Elapsed 00:01:30" in text
        assert "Last update" in text
        assert "0/0" not in text  # never a misleadingly empty progress line

    def test_chunk_completion_headline(self):
        payload = {
            "heartbeat": False, "chunk_index": 2, "chunk_row_count": 168,
            "chunk_success": True, "parallel_chunks_total": 4, "parallel_chunks_completed": 2,
            "parallel_workers": 4, "processed_rows": 336, "selected_rows": 671,
            "success_count": 330, "error_count": 6, "current_company_name": "Beta Corp",
        }
        text = build_parallel_progress_status_text(payload, started_at=0.0, now=60.0)
        assert "Chunk 2 (168 rows) ok" in text
        assert "Chunks 2/4" in text

    def test_chunk_failure_headline(self):
        payload = {
            "heartbeat": False, "chunk_index": 3, "chunk_row_count": 168,
            "chunk_success": False, "parallel_chunks_total": 4, "parallel_chunks_completed": 3,
            "parallel_workers": 4, "processed_rows": 504, "selected_rows": 671,
            "success_count": 400, "error_count": 104,
        }
        text = build_parallel_progress_status_text(payload, started_at=0.0, now=10.0)
        assert "Chunk 3 (168 rows) FAILED" in text

    def test_no_secrets_in_text(self):
        payload = {"heartbeat": True, "parallel_chunks_total": 1, "parallel_chunks_completed": 0,
                   "parallel_workers": 1, "processed_rows": 1, "selected_rows": 2,
                   "success_count": 1, "error_count": 0, "current_company_name": "Acme"}
        text = build_parallel_progress_status_text(payload, started_at=0.0, now=5.0)
        assert "api_key" not in text.lower() and "sk-ant" not in text.lower()


class TestChunkDetailLine:
    def test_c5_phase_uses_phase_progress_not_chunk_rows(self):
        # Regression: a chunk in C5 adjudication used to render its phase-1
        # end state ("168/168 rows"), looking finished while C5 was running.
        chunk = {
            "chunk_index": 2, "processed": 168, "selected": 168,
            "phase": 2, "phase_label": "C5 adjudication",
            "phase_processed": 43, "phase_total": 168,
            "current_company_name": "SKG Radiology",
        }
        line = build_chunk_detail_line(chunk)
        assert line == "Chunk 2: C5 adjudication 43/168 — current: SKG Radiology"
        assert "168/168" not in line

    def test_hq_screening_phase_shows_phase_progress(self):
        chunk = {
            "chunk_index": 1, "processed": 42, "selected": 168,
            "phase": 1, "phase_label": "HQ screening",
            "phase_processed": 42, "phase_total": 168,
            "current_company_name": "Acme Brasil",
        }
        assert build_chunk_detail_line(chunk) == \
            "Chunk 1: HQ screening 42/168 — current: Acme Brasil"

    def test_full_enrichment_phase_labels_render(self):
        for label in (
            "Full enrichment for confirmed foreign-HQ leads",
            "Full enrichment for confirmed non-English foreign-HQ leads",
        ):
            chunk = {
                "chunk_index": 3, "processed": 168, "selected": 168,
                "phase": 3, "phase_label": label,
                "phase_processed": 7, "phase_total": 31,
                "current_company_name": "Beta Corp",
            }
            assert build_chunk_detail_line(chunk) == \
                f"Chunk 3: {label} 7/31 — current: Beta Corp"

    def test_non_phased_chunk_keeps_rows_format(self):
        chunk = {
            "chunk_index": 4, "processed": 12, "selected": 168,
            "phase": None, "phase_label": None,
            "phase_processed": 0, "phase_total": 0,
            "current_company_name": "Gamma Ltd",
        }
        assert build_chunk_detail_line(chunk) == \
            "Chunk 4: 12/168 rows — current: Gamma Ltd"

    def test_phase_label_without_total_falls_back_to_rows(self):
        chunk = {
            "chunk_index": 5, "processed": 10, "selected": 20,
            "phase": 2, "phase_label": "C5 adjudication",
            "phase_processed": 0, "phase_total": 0,
            "current_company_name": "",
        }
        assert build_chunk_detail_line(chunk) == \
            "Chunk 5: 10/20 rows — C5 adjudication"

    def test_no_company_suffix_when_blank(self):
        chunk = {"chunk_index": 1, "processed": 1, "selected": 2,
                 "phase_label": None, "current_company_name": ""}
        assert build_chunk_detail_line(chunk) == "Chunk 1: 1/2 rows"


class TestParallelUINoteText:
    def test_notes_present_and_match_spec(self):
        assert PARALLEL_PROGRESS_NOTE_TEXT == (
            "Parallel progress updates when a row/chunk completes. Long rows may "
            "take several minutes."
        )
        assert RUN_BUTTON_NOTE_TEXT == (
            "For large full enrichment runs, progress may update slowly while "
            "external API calls are in flight."
        )


# ---------------------------------------------------------------------------
# Lovable JSON export section — pure helpers
# ---------------------------------------------------------------------------

from datetime import datetime as _dt2  # noqa: E402

from lead_prioritizer_batch_app import (  # noqa: E402
    parse_cold_callers,
    default_foreign_hq_only_export,
    default_lovable_output_folder,
    zip_directory_bytes,
    DEFAULT_COLD_CALLERS_TEXT,
)
from lead_prioritizer_batch_core import (  # noqa: E402
    FOREIGN_HQ_ONLY_MODE,
    NON_ENGLISH_FOREIGN_HQ_ONLY_MODE,
)


class TestParseColdCallers:
    def test_default_caller_text(self):
        assert parse_cold_callers(DEFAULT_COLD_CALLERS_TEXT) == \
            ["Vanessa", "Francesca", "Lorenzo", "Matteo"]

    def test_strips_whitespace_and_drops_blanks(self):
        assert parse_cold_callers(" Jantje ,, Pietje ,  ") == ["Jantje", "Pietje"]

    def test_blank_input_yields_empty_list(self):
        assert parse_cold_callers("") == []
        assert parse_cold_callers(None) == []

    def test_single_caller(self):
        assert parse_cold_callers("Marietje") == ["Marietje"]


class TestDefaultForeignHqOnlyExport:
    def test_true_for_foreign_hq_only_modes(self):
        assert default_foreign_hq_only_export(FOREIGN_HQ_ONLY_MODE) is True
        assert default_foreign_hq_only_export(NON_ENGLISH_FOREIGN_HQ_ONLY_MODE) is True

    def test_false_for_other_modes(self):
        for mode in ("full", "hq_only", "evidence_only", "signals_no_score", "full_no_score"):
            assert default_foreign_hq_only_export(mode) is False, mode

    def test_false_for_blank_or_unknown(self):
        assert default_foreign_hq_only_export("") is False
        assert default_foreign_hq_only_export("something_else") is False


class TestDefaultLovableOutputFolder:
    def test_folder_shape(self):
        stamp = _dt2(2026, 7, 3, 10, 30, 0)
        folder = default_lovable_output_folder("Brazil", stamp)
        assert folder == str(
            __import__("pathlib").Path("lovable_json_exports") / "Brazil" / "20260703_103000")

    def test_sanitizes_country_with_spaces(self):
        stamp = _dt2(2026, 7, 3, 10, 30, 0)
        folder = default_lovable_output_folder("New Zealand", stamp)
        assert "New_Zealand" in folder
        assert " " not in folder

    def test_blank_country_falls_back(self):
        stamp = _dt2(2026, 7, 3, 10, 30, 0)
        folder = default_lovable_output_folder("", stamp)
        assert "export" in folder


class TestZipDirectoryBytes:
    def test_zips_existing_files(self, tmp_path):
        (tmp_path / "companies.list.json").write_text("[]")
        (tmp_path / "export_manifest.json").write_text("{}")
        data = zip_directory_bytes(
            tmp_path, ["companies.list.json", "export_manifest.json"])

        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = sorted(zf.namelist())
            assert names == ["companies.list.json", "export_manifest.json"]
            assert zf.read("companies.list.json") == b"[]"

    def test_skips_missing_files_without_raising(self, tmp_path):
        (tmp_path / "companies.list.json").write_text("[]")
        data = zip_directory_bytes(
            tmp_path, ["companies.list.json", "does-not-exist.json"])

        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == ["companies.list.json"]

    def test_empty_filename_list_yields_empty_zip(self, tmp_path):
        data = zip_directory_bytes(tmp_path, [])
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == []


# ---------------------------------------------------------------------------
# Experimental AI-provider selection helpers
# ---------------------------------------------------------------------------

from lead_prioritizer_batch_app import (  # noqa: E402
    AI_PROVIDER_LABELS,
    COMPARE_MODE_WARNING_TEXT,
    ai_provider_label_to_mode,
    build_comparison_download_filename,
    build_provider_comparison_workbook_bytes,
    validate_ai_provider_run,
)
from lead_prioritizer_batch_core import FOREIGN_HQ_ONLY_MODE  # noqa: E402


class TestAiProviderSelection:
    def test_default_label_first_and_anthropic(self):
        assert AI_PROVIDER_LABELS[0] == "Anthropic only (default)"
        assert ai_provider_label_to_mode(AI_PROVIDER_LABELS[0]) == "anthropic"

    def test_all_labels_map(self):
        assert [ai_provider_label_to_mode(lbl) for lbl in AI_PROVIDER_LABELS] == \
            ["anthropic", "openai", "compare", "compare_triple"]

    def test_unknown_label_raises(self):
        with pytest.raises(ValueError):
            ai_provider_label_to_mode("Gemini only")

    def test_compare_warning_mentions_double_cost_and_row_limit(self):
        assert "TWICE" in COMPARE_MODE_WARNING_TEXT
        assert "5-10" in COMPARE_MODE_WARNING_TEXT
        assert "EXPERIMENTAL" in COMPARE_MODE_WARNING_TEXT


class TestValidateAiProviderRun:
    def test_anthropic_always_allowed_without_openai_key(self):
        assert validate_ai_provider_run("anthropic", "full", True, "") is None
        assert validate_ai_provider_run(
            "anthropic", FOREIGN_HQ_ONLY_MODE, True, "") is None

    def test_openai_missing_key_blocked(self):
        err = validate_ai_provider_run("openai", "full", False, "")
        assert err is not None and "OPENAI_API_KEY" in err

    def test_openai_with_key_and_normal_mode_allowed(self):
        assert validate_ai_provider_run("openai", "full", False, "k") is None
        assert validate_ai_provider_run("compare", "hq_only", False, "k") is None

    def test_compare_blocked_with_c5(self):
        err = validate_ai_provider_run("compare", "full", True, "k")
        assert err is not None and "C5" in err

    def test_compare_triple_allowed_and_blocked_like_compare(self):
        assert validate_ai_provider_run("compare_triple", "hq_only", False, "k") is None
        err = validate_ai_provider_run("compare_triple", "full", True, "k")
        assert err is not None and "C5" in err
        err2 = validate_ai_provider_run("compare_triple", "full", False, "")
        assert err2 is not None and "OPENAI_API_KEY" in err2

    def test_blocked_for_foreign_hq_only_modes(self):
        err = validate_ai_provider_run("compare", FOREIGN_HQ_ONLY_MODE, False, "k")
        assert err is not None and "standard batch modes" in err
        err2 = validate_ai_provider_run("openai", _NON_ENGLISH_MODE_CONST, False, "k")
        assert err2 is not None and "standard batch modes" in err2

    def test_unknown_mode_blocked(self):
        assert validate_ai_provider_run("gemini", "full", False, "k") is not None

    def test_no_key_material_in_messages(self):
        err = validate_ai_provider_run("openai", "full", False, "")
        assert "sk-" not in (err or "")


class TestProviderComparisonWorkbook:
    def test_workbook_bytes_round_trip(self, tmp_path):
        import io

        import pandas as pd

        from compare_ai_providers_lead_prioritizer import COMPARISON_COLUMNS

        df = pd.DataFrame([{col: "" for col in COMPARISON_COLUMNS}])
        df.loc[0, "company_name"] = "Acme"

        data = build_provider_comparison_workbook_bytes(df)
        loaded = pd.read_excel(io.BytesIO(data), sheet_name="Provider Comparison")
        assert list(loaded.columns) == COMPARISON_COLUMNS
        assert loaded.loc[0, "company_name"] == "Acme"

    def test_comparison_download_filename(self):
        from datetime import datetime

        name = build_comparison_download_filename(datetime(2026, 7, 4, 9, 30, 0))
        assert name == "lead_prioritizer_provider_comparison_20260704_093000.xlsx"

    def test_workbook_with_cost_summary_sheet(self, tmp_path):
        import io

        import pandas as pd

        from compare_ai_providers_lead_prioritizer import COMPARISON_COLUMNS

        df = pd.DataFrame([{col: "" for col in COMPARISON_COLUMNS}])
        df.loc[0, "company_name"] = "Acme"
        cost_df = pd.DataFrame([{"provider": "anthropic", "estimated_cost_usd": 0.0015}])

        data = build_provider_comparison_workbook_bytes(df, cost_df)
        xls = pd.ExcelFile(io.BytesIO(data))
        assert "Provider Comparison" in xls.sheet_names
        assert "Cost Summary" in xls.sheet_names
        loaded_cost = pd.read_excel(io.BytesIO(data), sheet_name="Cost Summary")
        assert loaded_cost.loc[0, "provider"] == "anthropic"


class TestCostSummaryDataframe:
    def test_build_cost_summary_dataframe_for_triple(self):
        from lead_prioritizer_batch_app import build_cost_summary_dataframe
        from compare_ai_providers_lead_prioritizer import TRIPLE_COST_PROVIDERS

        import pandas as pd

        df = pd.DataFrame([{
            "anthropic_model": "claude-haiku-4-5-20251001",
            "anthropic_estimated_cost_usd": 0.0015,
            "openai_nano_model": "gpt-5.4-nano",
            "openai_nano_estimated_cost_usd": 0.0003,
            "openai_mini_model": "gpt-5.4-mini",
            "openai_mini_estimated_cost_usd": 0.0011,
        }])
        out = build_cost_summary_dataframe(df, TRIPLE_COST_PROVIDERS)
        assert list(out["provider"]) == ["anthropic", "openai_nano", "openai_mini"]
        assert out.loc[0, "estimated_cost_usd"] == 0.0015


# ---------------------------------------------------------------------------
# After-run Lovable JSON / GCS auto-export ("one-click / overnight" workflow)
# ---------------------------------------------------------------------------

from lead_prioritizer_batch_app import (  # noqa: E402
    suggest_country_from_filename,
    resolve_auto_export_country_default,
    default_auto_lovable_base_folder,
    default_gcs_country_prefix,
    validate_auto_export_settings,
)


class TestSuggestCountryFromFilename:
    def test_matches_plain_country_name(self):
        assert suggest_country_from_filename(
            "Netherlands_leads.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == "Netherlands"

    def test_matches_underscore_separated_multiword_country(self):
        assert suggest_country_from_filename(
            "leads_New_Zealand_2026.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == "New Zealand"

    def test_matches_hyphen_separated_multiword_country(self):
        assert suggest_country_from_filename(
            "leads-new-zealand.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == "New Zealand"

    def test_matches_space_separated_multiword_country_case_insensitive(self):
        assert suggest_country_from_filename(
            "New Zealand Leads.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == "New Zealand"

    def test_case_insensitive_single_word(self):
        assert suggest_country_from_filename(
            "BRAZIL_batch.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == "Brazil"

    def test_no_match_returns_blank_without_raising(self):
        assert suggest_country_from_filename(
            "generic_leads.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == ""

    def test_blank_filename_never_raises(self):
        assert suggest_country_from_filename("", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == ""
        assert suggest_country_from_filename(None, SUPPORTED_DEFAULT_INPUT_COUNTRIES) == ""


class TestResolveAutoExportCountryDefault:
    def test_default_input_country_has_priority_over_filename(self):
        assert resolve_auto_export_country_default(
            "Italy", "brazil_leads.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == "Italy"

    def test_falls_back_to_filename_guess_when_no_default_country(self):
        assert resolve_auto_export_country_default(
            "", "brazil_leads.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == "Brazil"

    def test_blank_when_neither_available(self):
        assert resolve_auto_export_country_default(
            "", "generic_leads.xlsx", SUPPORTED_DEFAULT_INPUT_COUNTRIES) == ""


class TestDefaultAutoLovableBaseFolder:
    def test_builds_country_subfolder_without_timestamp(self):
        import os

        result = default_auto_lovable_base_folder("Brazil")
        assert result == os.path.join("lovable_json_exports", "Brazil")

    def test_blank_country_falls_back(self):
        import os

        result = default_auto_lovable_base_folder("")
        assert result == os.path.join("lovable_json_exports", "export")


class TestDefaultGcsCountryPrefix:
    def test_without_base_prefix(self):
        assert default_gcs_country_prefix("Brazil", "") == "brazil"

    def test_with_base_prefix_from_env(self):
        # GCS_BASE_PREFIX-style base prefix prepended and normalized.
        assert default_gcs_country_prefix("Brazil", "myngle-data") == "myngle-data/brazil"

    def test_base_prefix_with_slashes_normalized(self):
        assert default_gcs_country_prefix("New Zealand", "/exports/") == "exports/new-zealand"


class TestValidateAutoExportSettings:
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            auto_export_enabled=True,
            export_country="Brazil",
            cold_callers=["Vanessa"],
            auto_gcs_upload_enabled=False,
            gcs_bucket="",
            gcs_prefix="",
            upload_current=True,
            upload_archive=True,
        )
        kwargs.update(overrides)
        return kwargs

    def test_disabled_is_always_none(self):
        assert validate_auto_export_settings(
            **self._base_kwargs(auto_export_enabled=False, export_country="",
                                cold_callers=[])) is None

    def test_valid_settings_without_gcs_pass(self):
        assert validate_auto_export_settings(**self._base_kwargs()) is None

    def test_empty_export_country_blocked(self):
        err = validate_auto_export_settings(**self._base_kwargs(export_country=""))
        assert err is not None and "export country" in err

    def test_empty_callers_blocked(self):
        err = validate_auto_export_settings(**self._base_kwargs(cold_callers=[]))
        assert err is not None and "cold caller" in err

    def test_gcs_enabled_requires_bucket(self):
        err = validate_auto_export_settings(**self._base_kwargs(
            auto_gcs_upload_enabled=True, gcs_bucket="", gcs_prefix="brazil"))
        assert err is not None and "bucket" in err

    def test_gcs_enabled_requires_prefix(self):
        err = validate_auto_export_settings(**self._base_kwargs(
            auto_gcs_upload_enabled=True, gcs_bucket="my-bucket", gcs_prefix=""))
        assert err is not None and "prefix" in err

    def test_gcs_enabled_requires_current_or_archive(self):
        err = validate_auto_export_settings(**self._base_kwargs(
            auto_gcs_upload_enabled=True, gcs_bucket="my-bucket", gcs_prefix="brazil",
            upload_current=False, upload_archive=False))
        assert err is not None and "current" in err

    def test_gcs_enabled_with_all_fields_passes(self):
        assert validate_auto_export_settings(**self._base_kwargs(
            auto_gcs_upload_enabled=True, gcs_bucket="my-bucket",
            gcs_prefix="brazil")) is None


class TestAutoGcsUploadJobsForNewCountryPrefix:
    def test_upload_jobs_target_new_country_prefix_without_directory_creation(self, tmp_path):
        # GCS has no real directories — build_upload_plan must produce jobs
        # for a brand-new country prefix without creating anything locally
        # beyond the already-exported JSON files.
        from lovable_gcs_upload import build_upload_plan

        (tmp_path / "companies.list.json").write_text("[]")
        jobs = build_upload_plan(
            tmp_path, ["companies.list.json"], "my-bucket",
            default_gcs_country_prefix("Newlandia", ""), "run1")
        assert len(jobs) == 2
        destinations = {job["destination"] for job in jobs}
        assert "gs://my-bucket/newlandia/current/companies.list.json" in destinations
        assert (
            "gs://my-bucket/newlandia/runs/run1/companies.list.json" in destinations)

    def test_no_upload_jobs_when_auto_gcs_upload_disabled(self):
        # When auto GCS upload is off, the app must not call build_upload_plan
        # at all — validate_auto_export_settings still passes (GCS fields are
        # only required once auto_gcs_upload_enabled is True).
        assert validate_auto_export_settings(
            auto_export_enabled=True, export_country="Brazil",
            cold_callers=["Vanessa"], auto_gcs_upload_enabled=False,
            gcs_bucket="", gcs_prefix="", upload_current=True,
            upload_archive=True,
        ) is None
