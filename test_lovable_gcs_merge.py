"""Tests for lovable_gcs_merge — merging a new Lovable export batch into an
already-published GCS export. No real network calls, no real Google Cloud
SDK required."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from lovable_gcs_merge import (
    build_merged_manifest,
    fetch_existing_flat_export,
    load_local_export,
    merge_company_lists,
    next_bucket_start,
    prepare_merge,
    renumber_new_export,
    write_merge_output,
)


def _item(company_id, bucket, **extra):
    return {"company_id": company_id, "detail_bucket": bucket, **extra}


class TestNextBucketStart:
    def test_no_existing_items_starts_at_zero(self):
        assert next_bucket_start([]) == 0
        assert next_bucket_start(None) == 0

    def test_continues_after_highest_bucket_number(self):
        items = [
            _item("a", "company-details-000.json"),
            _item("b", "company-details-001.json"),
            _item("c", "company-details-001.json"),
        ]
        assert next_bucket_start(items) == 2

    def test_ignores_items_without_a_bucket(self):
        items = [_item("a", None), _item("b", "company-details-003.json")]
        assert next_bucket_start(items) == 4


class TestRenumberNewExport:
    def test_renumbers_starting_at_given_offset(self):
        list_items = [
            _item("a", "company-details-000.json"),
            _item("b", "company-details-000.json"),
            _item("c", "company-details-001.json"),
        ]
        details_by_bucket = {
            "company-details-000.json": {
                "a": {"company_id": "a", "detail_bucket": "company-details-000.json"},
                "b": {"company_id": "b", "detail_bucket": "company-details-000.json"},
            },
            "company-details-001.json": {
                "c": {"company_id": "c", "detail_bucket": "company-details-001.json"},
            },
        }
        renumbered_items, renumbered_details = renumber_new_export(
            list_items, details_by_bucket, start_bucket_no=5)

        assert {i["company_id"]: i["detail_bucket"] for i in renumbered_items} == {
            "a": "company-details-005.json",
            "b": "company-details-005.json",
            "c": "company-details-006.json",
        }
        assert set(renumbered_details.keys()) == {
            "company-details-005.json", "company-details-006.json"}
        assert renumbered_details["company-details-005.json"]["a"]["detail_bucket"] == \
            "company-details-005.json"

    def test_no_collision_with_start_bucket_zero(self):
        # start_bucket_no=0 (nothing published yet) is a no-op renumbering.
        list_items = [_item("a", "company-details-000.json")]
        details_by_bucket = {
            "company-details-000.json": {
                "a": {"company_id": "a", "detail_bucket": "company-details-000.json"},
            },
        }
        renumbered_items, renumbered_details = renumber_new_export(
            list_items, details_by_bucket, start_bucket_no=0)
        assert renumbered_items[0]["detail_bucket"] == "company-details-000.json"
        assert "company-details-000.json" in renumbered_details


class TestMergeCompanyLists:
    def test_new_company_is_added(self):
        existing = [_item("a", "company-details-000.json")]
        new = [_item("b", "company-details-005.json")]
        merged, stats = merge_company_lists(existing, new)
        assert {i["company_id"] for i in merged} == {"a", "b"}
        assert stats == {"added": 1, "updated": 0, "kept_from_existing": 1}

    def test_duplicate_id_newest_wins(self):
        existing = [_item("a", "company-details-000.json", commercial_fit_score=1.0)]
        new = [_item("a", "company-details-005.json", commercial_fit_score=9.0)]
        merged, stats = merge_company_lists(existing, new)
        assert len(merged) == 1
        assert merged[0]["commercial_fit_score"] == 9.0
        assert merged[0]["detail_bucket"] == "company-details-005.json"
        assert stats == {"added": 0, "updated": 1, "kept_from_existing": 0}

    def test_existing_company_never_removed(self):
        existing = [_item("a", "b0"), _item("b", "b0"), _item("c", "b0")]
        new = [_item("d", "b1")]
        merged, stats = merge_company_lists(existing, new)
        ids = {i["company_id"] for i in merged}
        assert ids == {"a", "b", "c", "d"}
        assert stats["kept_from_existing"] == 3

    def test_empty_existing_is_a_plain_first_publish(self):
        new = [_item("a", "b0"), _item("b", "b0")]
        merged, stats = merge_company_lists([], new)
        assert len(merged) == 2
        assert stats == {"added": 2, "updated": 0, "kept_from_existing": 0}

    def test_empty_new_batch_keeps_everything(self):
        existing = [_item("a", "b0")]
        merged, stats = merge_company_lists(existing, [])
        assert merged == existing
        assert stats == {"added": 0, "updated": 0, "kept_from_existing": 1}


class TestBuildMergedManifest:
    def test_recomputes_totals_over_merged_list(self):
        new_manifest = {
            "generated_at": "2026-07-09T10:00:00", "rows_exported": 1,
            "bucket_count": 1, "foreign_hq_rows_exported": 1, "cold_callers": ["Jantje"],
        }
        merged_list = [
            _item("a", "b0", foreign_hq_detected_for_export=True),
            _item("b", "b1", foreign_hq_detected_for_export=False),
        ]
        existing_manifest = {"rows_exported": 1, "generated_at": "2026-07-01T10:00:00"}
        manifest = build_merged_manifest(
            new_manifest, merged_list, existing_manifest,
            {"added": 1, "updated": 0, "kept_from_existing": 1})

        assert manifest["rows_exported"] == 2
        assert manifest["bucket_count"] == 2
        assert manifest["foreign_hq_rows_exported"] == 1
        assert manifest["cold_callers"] == ["Jantje"]  # new run metadata kept
        assert manifest["merge"] == {
            "merged_into_existing": True,
            "previous_rows_exported": 1,
            "previous_generated_at": "2026-07-01T10:00:00",
            "added": 1, "updated": 0, "kept_from_existing": 1,
        }

    def test_no_existing_manifest_marks_first_publish(self):
        manifest = build_merged_manifest(
            {"generated_at": "now"}, [_item("a", "b0")], None,
            {"added": 1, "updated": 0, "kept_from_existing": 0})
        assert manifest["merge"]["merged_into_existing"] is False
        assert manifest["merge"]["previous_rows_exported"] is None


class TestLoadLocalExport:
    def test_loads_list_details_and_manifest(self, tmp_path):
        (tmp_path / "companies.list.json").write_text(json.dumps([
            _item("a", "company-details-000.json"),
        ]))
        (tmp_path / "company-details-000.json").write_text(json.dumps({
            "a": {"company_id": "a"},
        }))
        (tmp_path / "export_manifest.json").write_text(json.dumps({"rows_exported": 1}))

        list_items, details_by_bucket, manifest = load_local_export(tmp_path)

        assert list_items == [_item("a", "company-details-000.json")]
        assert details_by_bucket == {"company-details-000.json": {"a": {"company_id": "a"}}}
        assert manifest == {"rows_exported": 1}


class TestWriteMergeOutput:
    def test_writes_list_manifest_and_bucket_files(self, tmp_path):
        merged_list = [_item("a", "company-details-005.json")]
        details = {"company-details-005.json": {"a": {"company_id": "a"}}}
        manifest = {"rows_exported": 1}

        filenames = write_merge_output(tmp_path, merged_list, details, manifest)

        assert filenames == [
            "companies.list.json", "export_manifest.json", "company-details-005.json"]
        assert json.loads((tmp_path / "companies.list.json").read_text()) == merged_list
        assert json.loads((tmp_path / "export_manifest.json").read_text()) == manifest
        assert json.loads((tmp_path / "company-details-005.json").read_text()) == \
            details["company-details-005.json"]


class TestFetchExistingFlatExport:
    def test_nothing_published_yet(self):
        with patch("lovable_gcs_merge.fetch_gcs_text",
                   return_value={"success": False, "exists": False, "text": None, "error": None}):
            result = fetch_existing_flat_export("bucket-a", "brazil/current")
        assert result == {"exists": False, "list_items": None, "manifest": None, "error": None}

    def test_check_failure_surfaces_error(self):
        with patch("lovable_gcs_merge.fetch_gcs_text",
                   return_value={"success": False, "exists": None, "text": None, "error": "boom"}):
            result = fetch_existing_flat_export("bucket-a", "brazil/current")
        assert result["exists"] is None
        assert result["error"] == "boom"

    def test_existing_list_and_manifest_parsed(self):
        list_json = json.dumps([_item("a", "company-details-000.json")])
        manifest_json = json.dumps({"rows_exported": 1})

        def _fetch(destination):
            if destination.endswith("companies.list.json"):
                return {"success": True, "exists": True, "text": list_json, "error": None}
            return {"success": True, "exists": True, "text": manifest_json, "error": None}

        with patch("lovable_gcs_merge.fetch_gcs_text", side_effect=_fetch):
            result = fetch_existing_flat_export("bucket-a", "brazil/current")

        assert result["exists"] is True
        assert result["list_items"] == [_item("a", "company-details-000.json")]
        assert result["manifest"] == {"rows_exported": 1}
        assert result["error"] is None

    def test_invalid_existing_json_reports_error(self):
        def _fetch(destination):
            if destination.endswith("companies.list.json"):
                return {"success": True, "exists": True, "text": "not json", "error": None}
            return {"success": False, "exists": False, "text": None, "error": None}

        with patch("lovable_gcs_merge.fetch_gcs_text", side_effect=_fetch):
            result = fetch_existing_flat_export("bucket-a", "brazil/current")
        assert result["exists"] is True
        assert result["list_items"] is None
        assert "not valid JSON" in result["error"]

    def test_manifest_fetch_failure_does_not_block_list(self):
        def _fetch(destination):
            if destination.endswith("companies.list.json"):
                return {"success": True, "exists": True, "text": "[]", "error": None}
            return {"success": False, "exists": None, "text": None, "error": "boom"}

        with patch("lovable_gcs_merge.fetch_gcs_text", side_effect=_fetch):
            result = fetch_existing_flat_export("bucket-a", "brazil/current")
        assert result["exists"] is True
        assert result["list_items"] == []
        assert result["manifest"] is None
        assert result["error"] is None


class TestPrepareMerge:
    def _write_local_export(self, tmp_path, company_id="new-co"):
        (tmp_path / "companies.list.json").write_text(json.dumps([
            _item(company_id, "company-details-000.json", commercial_fit_score=5.0),
        ]))
        (tmp_path / "company-details-000.json").write_text(json.dumps({
            company_id: {"company_id": company_id},
        }))
        (tmp_path / "export_manifest.json").write_text(json.dumps({
            "generated_at": "2026-07-09T10:00:00", "rows_exported": 1,
            "bucket_count": 1, "foreign_hq_rows_exported": 0, "cold_callers": ["Jantje"],
        }))

    def test_fetch_failure_is_surfaced_without_falling_back_to_overwrite(self, tmp_path):
        self._write_local_export(tmp_path)
        with patch("lovable_gcs_merge.fetch_existing_flat_export",
                   return_value={"exists": None, "list_items": None, "manifest": None,
                                  "error": "auth failed"}):
            result = prepare_merge(tmp_path, "bucket-a", "brazil/current")
        assert result["fetch_error"] == "auth failed"
        assert result["jobs"] is None

    def test_first_publish_when_nothing_exists_yet(self, tmp_path):
        self._write_local_export(tmp_path)
        with patch("lovable_gcs_merge.fetch_existing_flat_export",
                   return_value={"exists": False, "list_items": None, "manifest": None,
                                  "error": None}):
            result = prepare_merge(tmp_path, "bucket-a", "brazil/current")

        assert result["fetch_error"] is None
        assert result["stats"] == {"added": 1, "updated": 0, "kept_from_existing": 0}
        merged_dir = tmp_path / "merged"
        merged_list = json.loads((merged_dir / "companies.list.json").read_text())
        assert merged_list[0]["company_id"] == "new-co"
        # No existing buckets published -> renumbering keeps bucket 000.
        assert merged_list[0]["detail_bucket"] == "company-details-000.json"
        destinations = {job["destination"] for job in result["jobs"]}
        assert "gs://bucket-a/brazil/current/companies.list.json" in destinations

    def test_merges_with_existing_and_renumbers_new_buckets(self, tmp_path):
        self._write_local_export(tmp_path, company_id="new-co")
        existing_list = [_item("old-co", "company-details-000.json")]
        with patch("lovable_gcs_merge.fetch_existing_flat_export",
                   return_value={"exists": True, "list_items": existing_list,
                                  "manifest": {"rows_exported": 1,
                                               "generated_at": "2026-07-01T10:00:00"},
                                  "error": None}):
            result = prepare_merge(tmp_path, "bucket-a", "brazil/current")

        assert result["fetch_error"] is None
        assert result["stats"] == {"added": 1, "updated": 0, "kept_from_existing": 1}
        merged_dir = tmp_path / "merged"
        merged_list = json.loads((merged_dir / "companies.list.json").read_text())
        by_id = {item["company_id"]: item for item in merged_list}
        assert set(by_id) == {"old-co", "new-co"}
        # New batch's bucket renumbered past the existing bucket 000.
        assert by_id["new-co"]["detail_bucket"] == "company-details-001.json"
        assert (merged_dir / "company-details-001.json").exists()
        assert not (merged_dir / "company-details-000.json").exists()
        manifest = json.loads((merged_dir / "export_manifest.json").read_text())
        assert manifest["rows_exported"] == 2
        assert manifest["merge"]["kept_from_existing"] == 1
