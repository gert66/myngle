"""Tests for the upload/tempfile handling in the Lovable JSON export app.

Covers the Windows PermissionError fix: uploaded workbooks are written to a
stable local folder (not tempfile.TemporaryDirectory), so nothing ever calls
shutil.rmtree on a directory that pandas/openpyxl may still have a file handle
open in.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lead_prioritizer_lovable_json_export_app import (
    cleanup_uploaded_workbook,
    make_upload_path,
    save_uploaded_workbook,
)


class _FakeUploadedFile:
    """Minimal stand-in for streamlit's UploadedFile."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


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
