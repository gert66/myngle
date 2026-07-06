"""Tests for generate_lovable_countries_index — manifest shape/content and the
local-write / optional-upload CLI. No real network calls, no real Google Cloud
SDK required."""

from __future__ import annotations

import json
from unittest.mock import patch

from lovable_gcs_upload import DEFAULT_GCS_BUCKET, country_folder_slug

from generate_lovable_countries_index import (
    DISABLED_COUNTRY_LABELS,
    MANIFEST_COUNTRY_LABELS,
    build_arg_parser,
    build_countries_manifest,
    main,
    write_manifest,
)


class TestCountryFolderSlugForManifestCountries:
    def test_new_countries_have_explicit_slugs(self):
        assert country_folder_slug("Japan") == "japan"
        assert country_folder_slug("South Korea") == "south-korea"
        assert country_folder_slug("Switzerland") == "switzerland"
        assert country_folder_slug("Test") == "test"


class TestBuildCountriesManifest:
    def test_manifest_has_top_level_countries_list(self):
        manifest = build_countries_manifest(DEFAULT_GCS_BUCKET)
        assert isinstance(manifest, dict)
        assert isinstance(manifest["countries"], list)
        assert len(manifest["countries"]) == len(MANIFEST_COUNTRY_LABELS)

    def test_every_entry_has_a_base_url(self):
        manifest = build_countries_manifest(DEFAULT_GCS_BUCKET)
        for entry in manifest["countries"]:
            assert entry["baseUrl"] == (
                f"https://storage.googleapis.com/{DEFAULT_GCS_BUCKET}/"
                f"{country_folder_slug(entry['label'])}/current"
            )

    def test_new_countries_are_disabled(self):
        manifest = build_countries_manifest(DEFAULT_GCS_BUCKET)
        by_id = {entry["id"]: entry for entry in manifest["countries"]}
        assert by_id["japan"]["enabled"] is False
        assert by_id["south-korea"]["enabled"] is False
        assert by_id["test"]["enabled"] is False

    def test_existing_countries_stay_enabled(self):
        manifest = build_countries_manifest(DEFAULT_GCS_BUCKET)
        by_id = {entry["id"]: entry for entry in manifest["countries"]}
        for country_id in (
            "australia", "brazil", "italy", "netherlands", "new-zealand",
            "switzerland", "uruguay",
        ):
            assert by_id[country_id]["enabled"] is True

    def test_ids_match_country_folder_slug_except_known_folder_quirks(self):
        # The manifest id is a stable, plain kebab-case of the label and is
        # normally identical to the GCS folder slug. New Zealand is the one
        # deliberate exception: its GCS bucket folder is "newzealand" (no
        # hyphen), but Lovable's id must stay "new-zealand".
        manifest = build_countries_manifest(DEFAULT_GCS_BUCKET)
        for entry in manifest["countries"]:
            if entry["label"] == "New Zealand":
                assert entry["id"] == "new-zealand"
                assert country_folder_slug(entry["label"]) == "newzealand"
            else:
                assert entry["id"] == country_folder_slug(entry["label"])

    def test_new_zealand_base_url_uses_newzealand_folder(self):
        manifest = build_countries_manifest(DEFAULT_GCS_BUCKET)
        by_id = {entry["id"]: entry for entry in manifest["countries"]}
        nz = by_id["new-zealand"]
        assert nz["label"] == "New Zealand"
        assert nz["enabled"] is True
        assert nz["baseUrl"] == (
            f"https://storage.googleapis.com/{DEFAULT_GCS_BUCKET}/newzealand/current"
        )

    def test_uses_the_given_bucket(self):
        manifest = build_countries_manifest("other-bucket")
        assert manifest["countries"][0]["baseUrl"].startswith(
            "https://storage.googleapis.com/other-bucket/")


class TestDisabledLabelsAreASubsetOfManifestLabels:
    def test_disabled_labels_are_known_countries(self):
        assert DISABLED_COUNTRY_LABELS <= set(MANIFEST_COUNTRY_LABELS)


class TestSyncWithBatchAppCountryList:
    def test_matches_supported_default_input_countries(self):
        # Slow import (pulls in the full enrichment stack) — only done here,
        # once, to guard against the two lists drifting apart.
        from lead_prioritizer_batch_app import SUPPORTED_DEFAULT_INPUT_COUNTRIES
        assert set(MANIFEST_COUNTRY_LABELS) == set(SUPPORTED_DEFAULT_INPUT_COUNTRIES)


class TestWriteManifest:
    def test_writes_pretty_json_and_returns_path(self, tmp_path):
        manifest = build_countries_manifest(DEFAULT_GCS_BUCKET)
        output_dir = tmp_path / "countries_index"
        path = write_manifest(manifest, output_dir)
        assert path == output_dir / "countries.index.json"
        assert json.loads(path.read_text(encoding="utf-8")) == manifest

    def test_creates_missing_output_dir(self, tmp_path):
        output_dir = tmp_path / "nested" / "countries_index"
        write_manifest(build_countries_manifest(DEFAULT_GCS_BUCKET), output_dir)
        assert output_dir.is_dir()


class TestArgParser:
    def test_output_dir_is_required(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--output-dir", "some/dir"])
        assert args.output_dir == "some/dir"
        assert args.bucket == DEFAULT_GCS_BUCKET
        assert args.upload is False

    def test_upload_flag(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--output-dir", "some/dir", "--upload"])
        assert args.upload is True


class TestMainWithoutUpload:
    def test_writes_manifest_and_skips_upload_by_default(self, tmp_path, capsys):
        output_dir = tmp_path / "countries_index"
        rc = main(["--output-dir", str(output_dir)])
        assert rc == 0
        assert (output_dir / "countries.index.json").exists()
        out = capsys.readouterr().out
        assert "Upload skipped" in out


class TestMainWithUpload:
    def test_fails_cleanly_when_gcloud_and_gsutil_missing(self, tmp_path, capsys):
        output_dir = tmp_path / "countries_index"
        with patch(
            "generate_lovable_countries_index.check_gcloud_available",
            return_value={"available": False, "tool": None, "version": ""},
        ):
            rc = main(["--output-dir", str(output_dir), "--upload"])
        assert rc == 2
        assert (output_dir / "countries.index.json").exists()
        err = capsys.readouterr().err
        assert "gcloud" in err.lower()

    def test_uploads_and_prints_public_url_when_tool_available(self, tmp_path, capsys):
        output_dir = tmp_path / "countries_index"
        with patch(
            "generate_lovable_countries_index.check_gcloud_available",
            return_value={"available": True, "tool": "gcloud", "version": "1.0"},
        ), patch(
            "generate_lovable_countries_index.describe_gcloud_environment",
            return_value={"account": "user@example.com", "project": "my-project"},
        ), patch(
            "generate_lovable_countries_index.resolve_gcs_upload_tool",
            return_value=["gcloud", "storage", "cp"],
        ), patch(
            "generate_lovable_countries_index.upload_file",
            return_value={"success": True},
        ) as mock_upload:
            rc = main(["--output-dir", str(output_dir), "--upload"])
        assert rc == 0
        mock_upload.assert_called_once()
        _, args, kwargs = mock_upload.mock_calls[0]
        assert args[2] == f"gs://{DEFAULT_GCS_BUCKET}/countries.index.json"
        out = capsys.readouterr().out
        assert (
            f"https://storage.googleapis.com/{DEFAULT_GCS_BUCKET}/countries.index.json"
            in out
        )

    def test_upload_failure_is_reported_and_exits_nonzero(self, tmp_path, capsys):
        output_dir = tmp_path / "countries_index"
        with patch(
            "generate_lovable_countries_index.check_gcloud_available",
            return_value={"available": True, "tool": "gcloud", "version": "1.0"},
        ), patch(
            "generate_lovable_countries_index.describe_gcloud_environment",
            return_value={"account": "", "project": ""},
        ), patch(
            "generate_lovable_countries_index.resolve_gcs_upload_tool",
            return_value=["gcloud", "storage", "cp"],
        ), patch(
            "generate_lovable_countries_index.upload_file",
            return_value={"success": False, "error": "boom"},
        ):
            rc = main(["--output-dir", str(output_dir), "--upload"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "boom" in err
