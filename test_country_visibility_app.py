"""Tests for country_visibility_app — pure logic only (no Streamlit import
required, same style as test_rescore_streamlit_app.py)."""

from __future__ import annotations

from unittest.mock import patch

from country_visibility_app import (
    default_entry,
    load_current_countries,
    merge_with_known_labels,
    parse_manifest_json,
)
from generate_lovable_countries_index import MANIFEST_COUNTRY_LABELS, _manifest_id


class TestDefaultEntry:
    def test_shape_and_enabled_by_default(self):
        entry = default_entry("Brazil", "my-bucket")
        assert entry["id"] == "brazil"
        assert entry["label"] == "Brazil"
        assert entry["enabled"] is True
        assert entry["baseUrl"] == "https://storage.googleapis.com/my-bucket/brazil/current"

    def test_multi_word_label_slug(self):
        entry = default_entry("New Zealand", "b")
        assert entry["id"] == "new-zealand"
        # country_folder_slug's curated mapping, not the id's kebab-case
        assert "/newzealand/current" in entry["baseUrl"]


class TestParseManifestJson:
    def test_valid_json_parses(self):
        raw = '{"countries": [{"id": "brazil", "enabled": true}]}'
        assert parse_manifest_json(raw) == {"countries": [{"id": "brazil", "enabled": True}]}

    def test_none_returns_none(self):
        assert parse_manifest_json(None) is None

    def test_empty_string_returns_none(self):
        assert parse_manifest_json("") is None

    def test_invalid_json_returns_none(self):
        assert parse_manifest_json("{not valid json") is None

    def test_valid_json_wrong_shape_returns_none(self):
        assert parse_manifest_json('{"foo": "bar"}') is None
        assert parse_manifest_json('[1, 2, 3]') is None
        assert parse_manifest_json('{"countries": "not a list"}') is None


class TestMergeWithKnownLabels:
    def test_none_manifest_enables_every_known_label(self):
        merged = merge_with_known_labels(None, "b")
        assert [e["label"] for e in merged] == MANIFEST_COUNTRY_LABELS
        assert all(e["enabled"] for e in merged)

    def test_preserves_live_disabled_state(self):
        manifest = {"countries": [
            {"id": "australia", "label": "Australia", "enabled": False,
             "baseUrl": "https://storage.googleapis.com/b/australia/current"},
        ]}
        merged = merge_with_known_labels(manifest, "b")
        australia = next(e for e in merged if e["id"] == "australia")
        assert australia["enabled"] is False
        # Every other known label still present, defaulted enabled.
        other_ids = {e["id"] for e in merged if e["id"] != "australia"}
        assert other_ids == {_manifest_id(l) for l in MANIFEST_COUNTRY_LABELS if l != "Australia"}
        assert all(e["enabled"] for e in merged if e["id"] != "australia")

    def test_preserves_live_baseurl_when_present(self):
        manifest = {"countries": [
            {"id": "brazil", "label": "Brazil", "enabled": True,
             "baseUrl": "https://custom.example.com/brazil/current"},
        ]}
        merged = merge_with_known_labels(manifest, "b")
        brazil = next(e for e in merged if e["id"] == "brazil")
        assert brazil["baseUrl"] == "https://custom.example.com/brazil/current"

    def test_country_missing_from_live_manifest_defaults_enabled(self):
        # A country that's in the code's label list but wasn't in an older
        # published manifest (e.g. newly added) shows up enabled, not
        # silently dropped.
        manifest = {"countries": [
            {"id": "brazil", "label": "Brazil", "enabled": False,
             "baseUrl": "https://storage.googleapis.com/b/brazil/current"},
        ]}
        merged = merge_with_known_labels(manifest, "b")
        assert len(merged) == len(MANIFEST_COUNTRY_LABELS)
        non_brazil_enabled = [e["enabled"] for e in merged if e["id"] != "brazil"]
        assert all(non_brazil_enabled)

    def test_entry_missing_enabled_key_falls_back_to_default(self):
        manifest = {"countries": [{"id": "brazil", "label": "Brazil"}]}
        merged = merge_with_known_labels(manifest, "b")
        brazil = next(e for e in merged if e["id"] == "brazil")
        assert brazil["enabled"] is True

    def test_unknown_country_in_live_manifest_is_dropped(self):
        manifest = {"countries": [
            {"id": "atlantis", "label": "Atlantis", "enabled": True,
             "baseUrl": "https://storage.googleapis.com/b/atlantis/current"},
        ]}
        merged = merge_with_known_labels(manifest, "b")
        assert "atlantis" not in {e["id"] for e in merged}
        assert len(merged) == len(MANIFEST_COUNTRY_LABELS)


class TestLoadCurrentCountries:
    def test_uses_live_manifest_when_it_exists(self):
        with patch("country_visibility_app.fetch_gcs_text") as mock_fetch:
            mock_fetch.return_value = {
                "success": True, "exists": True,
                "text": '{"countries": [{"id": "brazil", "label": "Brazil", '
                        '"enabled": false, "baseUrl": "https://x/brazil/current"}]}',
                "error": None,
            }
            countries = load_current_countries("b")
        brazil = next(c for c in countries if c["id"] == "brazil")
        assert brazil["enabled"] is False

    def test_falls_back_to_defaults_when_manifest_absent(self):
        with patch("country_visibility_app.fetch_gcs_text") as mock_fetch:
            mock_fetch.return_value = {
                "success": False, "exists": False, "text": None, "error": None,
            }
            countries = load_current_countries("b")
        assert len(countries) == len(MANIFEST_COUNTRY_LABELS)
        assert all(c["enabled"] for c in countries)

    def test_falls_back_to_defaults_on_fetch_error(self):
        with patch("country_visibility_app.fetch_gcs_text") as mock_fetch:
            mock_fetch.return_value = {
                "success": False, "exists": None, "text": None, "error": "boom",
            }
            countries = load_current_countries("b")
        assert len(countries) == len(MANIFEST_COUNTRY_LABELS)
