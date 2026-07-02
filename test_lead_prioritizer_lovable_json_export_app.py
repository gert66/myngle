"""Tests for the Lovable JSON export Streamlit app's pure helper functions.

Covers two areas:
- Upload handling (the Windows PermissionError fix): uploaded workbooks are
  written to a stable local folder (not tempfile.TemporaryDirectory), so
  nothing ever calls shutil.rmtree on a directory that pandas/openpyxl may
  still have a file handle open in.
- Country list / output-directory / filename resolution: no Streamlit UI
  rendering, no live APIs, no real workbooks.

Requires Streamlit to be importable (the app imports it at module load,
unlike the batch app's lazy import), matching this app's existing design.
"""

from __future__ import annotations

from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import patch

from lead_prioritizer_lovable_json_export_app import (
    COUNTRY_PLACEHOLDER,
    EXPORT_COUNTRIES,
    DEFAULT_LOVABLE_OUTPUT_DIR,
    cleanup_uploaded_workbook,
    make_upload_path,
    save_uploaded_workbook,
    sanitize_filename_part,
    clean_user_path,
    resolve_lovable_output_dir,
    make_lovable_export_folder_name,
    make_lovable_zip_filename,
    parse_cold_callers,
)

_FIXED_NOW = _dt(2026, 7, 2, 23, 15, 0)


class _FakeUploadedFile:
    """Minimal stand-in for streamlit's UploadedFile."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


# ---------------------------------------------------------------------------
# Upload handling (Windows PermissionError fix)
# ---------------------------------------------------------------------------

def test_save_uploaded_workbook_writes_to_stable_folder(tmp_path):
    upload_dir = tmp_path / "batch_temp_uploads"
    uploaded = _FakeUploadedFile("Brazil_20260702.xlsx", b"fake-xlsx-bytes")

    upload_path = save_uploaded_workbook(uploaded, upload_dir=upload_dir)

    assert upload_path.parent == upload_dir
    assert upload_dir.exists()
    assert upload_path.exists()
    assert upload_path.read_bytes() == b"fake-xlsx-bytes"


def test_generated_upload_path_has_xlsx_suffix(tmp_path):
    upload_dir = tmp_path / "batch_temp_uploads"
    uploaded = _FakeUploadedFile("Brazil_20260702.xlsx", b"data")

    upload_path = save_uploaded_workbook(uploaded, upload_dir=upload_dir)

    assert upload_path.suffix == ".xlsx"
    assert upload_path.name.startswith("uploaded_")


def test_make_upload_path_falls_back_to_xlsx_suffix(tmp_path):
    upload_dir = tmp_path / "batch_temp_uploads"

    upload_path = make_upload_path("no_extension_name", upload_dir=upload_dir)

    assert upload_path.suffix == ".xlsx"


def test_make_upload_path_is_unique_per_call(tmp_path):
    upload_dir = tmp_path / "batch_temp_uploads"

    first = make_upload_path("workbook.xlsx", upload_dir=upload_dir)
    second = make_upload_path("workbook.xlsx", upload_dir=upload_dir)

    assert first != second


def test_no_temporary_directory_cleanup_required(tmp_path):
    """The upload file must still exist after save; nothing auto-deletes it."""
    upload_dir = tmp_path / "batch_temp_uploads"
    uploaded = _FakeUploadedFile("workbook.xlsx", b"data")

    upload_path = save_uploaded_workbook(uploaded, upload_dir=upload_dir)

    # Unlike tempfile.TemporaryDirectory, nothing removes this on its own.
    assert upload_path.exists()


def test_cleanup_uploaded_workbook_removes_file(tmp_path):
    upload_dir = tmp_path / "batch_temp_uploads"
    uploaded = _FakeUploadedFile("workbook.xlsx", b"data")
    upload_path = save_uploaded_workbook(uploaded, upload_dir=upload_dir)

    warning = cleanup_uploaded_workbook(upload_path)

    assert warning is None
    assert not upload_path.exists()


def test_cleanup_uploaded_workbook_missing_file_is_noop(tmp_path):
    missing_path = tmp_path / "does_not_exist.xlsx"

    warning = cleanup_uploaded_workbook(missing_path)

    assert warning is None


def test_cleanup_permission_error_does_not_raise(tmp_path):
    """Simulates Windows holding the file handle open: unlink raises."""
    upload_dir = tmp_path / "batch_temp_uploads"
    uploaded = _FakeUploadedFile("workbook.xlsx", b"data")
    upload_path = save_uploaded_workbook(uploaded, upload_dir=upload_dir)

    with patch.object(Path, "unlink", side_effect=PermissionError(
            "[WinError 32] The process cannot access the file")):
        warning = cleanup_uploaded_workbook(upload_path)

    assert warning is not None
    assert str(upload_path) in warning
    assert upload_path.exists()  # unlink was mocked away, file still there


# ---------------------------------------------------------------------------
# Country list
# ---------------------------------------------------------------------------

class TestExportCountries:
    def test_alphabetically_sorted(self):
        assert EXPORT_COUNTRIES == sorted(EXPORT_COUNTRIES)

    def test_includes_required_countries(self):
        for country in ("Australia", "Brazil", "Italy", "New Zealand", "Uruguay"):
            assert country in EXPORT_COUNTRIES

    def test_exact_list(self):
        assert EXPORT_COUNTRIES == ["Australia", "Brazil", "Italy", "New Zealand", "Uruguay"]

    def test_placeholder_first_in_dropdown_options(self):
        options = [COUNTRY_PLACEHOLDER] + EXPORT_COUNTRIES
        assert options[0] == COUNTRY_PLACEHOLDER
        assert options[1:] == sorted(options[1:])


# ---------------------------------------------------------------------------
# sanitize_filename_part / clean_user_path
# ---------------------------------------------------------------------------

class TestSanitizeFilenamePart:
    def test_spaces_become_underscores(self):
        assert sanitize_filename_part("New Zealand") == "New_Zealand"

    def test_blank_uses_fallback(self):
        assert sanitize_filename_part("", fallback="Country") == "Country"
        assert sanitize_filename_part(None, fallback="Country") == "Country"

    def test_unsafe_characters_removed(self):
        assert sanitize_filename_part('Bra<>zil:"/\\|?*') == "Bra_zil"


class TestCleanUserPath:
    def test_strips_quotes(self):
        assert str(clean_user_path('"C:\\Data\\Brazil"')) == "C:\\Data\\Brazil"
        assert str(clean_user_path("'/home/user/Brazil'")) == "/home/user/Brazil"

    def test_blank_returns_none(self):
        assert clean_user_path("") is None
        assert clean_user_path("   ") is None
        assert clean_user_path(None) is None

    def test_plain_path_unchanged(self):
        assert str(clean_user_path("/data/brazil")) == "/data/brazil"


# ---------------------------------------------------------------------------
# resolve_lovable_output_dir
# ---------------------------------------------------------------------------

class TestResolveLovableOutputDir:
    def test_resolves_from_source_folder(self):
        result = resolve_lovable_output_dir("C:/Data/Brazil")
        assert str(result).replace("\\", "/") == "C:/Data/Brazil/lovable_export"

    def test_falls_back_to_lovable_export_when_blank(self):
        assert str(resolve_lovable_output_dir("")) == DEFAULT_LOVABLE_OUTPUT_DIR
        assert str(resolve_lovable_output_dir(None)) == "lovable_export"

    def test_strips_quotes_around_windows_path(self):
        result = resolve_lovable_output_dir('"C:\\Data\\Brazil"')
        assert str(result).replace("\\", "/").endswith("Data/Brazil/lovable_export")

    def test_never_downloads_as_fallback(self):
        assert "Downloads" not in str(resolve_lovable_output_dir(""))
        assert "Downloads" not in str(resolve_lovable_output_dir("C:/Data/Brazil"))


# ---------------------------------------------------------------------------
# make_lovable_export_folder_name / make_lovable_zip_filename
# ---------------------------------------------------------------------------

class TestMakeLovableExportFolderName:
    def test_brazil_example(self):
        assert make_lovable_export_folder_name("Brazil", _FIXED_NOW) == \
            "Brazil_lovable_json_enriched_20260702_231500"

    def test_new_zealand_sanitized(self):
        name = make_lovable_export_folder_name("New Zealand", _FIXED_NOW)
        assert " " not in name
        assert name == "New_Zealand_lovable_json_enriched_20260702_231500"

    def test_includes_all_required_components(self):
        name = make_lovable_export_folder_name("Australia", _FIXED_NOW)
        assert "Australia" in name
        assert "lovable_json" in name
        assert "enriched" in name
        assert "20260702_231500" in name


class TestMakeLovableZipFilename:
    def test_brazil_example(self):
        assert make_lovable_zip_filename("Brazil", _FIXED_NOW) == \
            "Brazil_lovable_json_enriched_20260702_231500.zip"

    def test_matches_folder_name_plus_extension(self):
        folder = make_lovable_export_folder_name("Uruguay", _FIXED_NOW)
        assert make_lovable_zip_filename("Uruguay", _FIXED_NOW) == f"{folder}.zip"


# ---------------------------------------------------------------------------
# parse_cold_callers (existing behavior — regression guard)
# ---------------------------------------------------------------------------

class TestParseColdCallers:
    def test_one_per_line_blank_lines_ignored(self):
        assert parse_cold_callers("Alice\n\nBob\n  \nCarol") == ["Alice", "Bob", "Carol"]

    def test_blank_input(self):
        assert parse_cold_callers("") == []
        assert parse_cold_callers(None) == []
