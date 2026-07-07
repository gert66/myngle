"""Tests for lead_hq_location_summary.build_hq_location_summary and the
NL/IT localizers in lovable_content_localization.py."""

from __future__ import annotations

from lead_hq_location_summary import (
    DOMESTIC_HQ_PREFIX,
    FOREIGN_PARENT_PREFIX,
    build_hq_location_summary,
    build_hq_location_summary_from_row,
)
from lovable_content_localization import (
    localize_hq_location_summary,
    localize_hq_location_summary_it,
)


class TestForeignParent:
    def test_foreign_parent_city_and_country(self):
        out = build_hq_location_summary(
            hq_structure_type="foreign_parent",
            ai_parent_hq_country="Japan", ai_parent_hq_city="Tokyo")
        assert out == "Parent company headquarters: Tokyo, Japan"

    def test_foreign_hq_simple_true_also_triggers_foreign_line(self):
        out = build_hq_location_summary(
            foreign_hq_simple=True, hq_structure_type="",
            ai_parent_hq_country="Germany", ai_parent_hq_city="Munich")
        assert out.startswith(FOREIGN_PARENT_PREFIX)
        assert "Munich, Germany" in out

    def test_country_only_when_city_blank(self):
        out = build_hq_location_summary(
            hq_structure_type="foreign_parent", ai_parent_hq_country="Japan")
        assert out == "Parent company headquarters: Japan"

    def test_c5_takes_priority_over_ai_and_detected(self):
        out = build_hq_location_summary(
            hq_structure_type="foreign_parent",
            c5_parent_hq_country="Japan", c5_parent_hq_city="Tokyo",
            ai_parent_hq_country="United States", ai_parent_hq_city="New York",
            hq_detected_country="France", hq_detected_city="Paris")
        assert out == "Parent company headquarters: Tokyo, Japan"

    def test_falls_back_to_detected_when_ai_and_c5_blank(self):
        out = build_hq_location_summary(
            hq_structure_type="foreign_parent",
            hq_detected_country="Japan", hq_detected_city="Tokyo")
        assert out == "Parent company headquarters: Tokyo, Japan"

    def test_foreign_parent_without_any_location_is_none(self):
        assert build_hq_location_summary(hq_structure_type="foreign_parent") is None


class TestDomestic:
    def test_domestic_uses_detected_hq(self):
        out = build_hq_location_summary(
            hq_structure_type="domestic",
            hq_detected_country="Netherlands", hq_detected_city="Amsterdam")
        assert out == "Headquarters: Amsterdam, Netherlands"

    def test_domestic_without_location_is_none(self):
        assert build_hq_location_summary(hq_structure_type="domestic") is None


class TestNeitherKnown:
    def test_regional_branch_only_is_none(self):
        # Even with a (foreign) detected country, a non-confirmed regional
        # branch never emits a potentially misleading line.
        out = build_hq_location_summary(
            hq_structure_type="regional_branch_only",
            hq_detected_country="Japan", hq_detected_city="Tokyo")
        assert out is None

    def test_unclear_is_none(self):
        assert build_hq_location_summary(hq_structure_type="unclear") is None

    def test_all_blank_is_none(self):
        assert build_hq_location_summary() is None


class TestFromRow:
    def test_from_row_reads_expected_keys(self):
        row = {
            "hq_structure_type": "foreign_parent",
            "c5_parent_hq_country": "Japan", "c5_parent_hq_city": "Tokyo",
        }
        assert build_hq_location_summary_from_row(row) == (
            FOREIGN_PARENT_PREFIX + "Tokyo, Japan")

    def test_from_row_domestic(self):
        row = {
            "hq_structure_type": "domestic",
            "hq_detected_country": "Netherlands", "hq_detected_city": "Amsterdam",
        }
        assert build_hq_location_summary_from_row(row) == (
            DOMESTIC_HQ_PREFIX + "Amsterdam, Netherlands")


class TestLocalizationNL:
    def test_foreign_parent_dutch(self):
        out = localize_hq_location_summary(
            "Parent company headquarters: Tokyo, Japan")
        assert out == "Hoofdkantoor moederbedrijf: Tokio, Japan"

    def test_domestic_dutch_translates_country(self):
        out = localize_hq_location_summary(
            "Headquarters: Amsterdam, Netherlands")
        assert out == "Hoofdkantoor: Amsterdam, Nederland"

    def test_unknown_country_passes_through(self):
        out = localize_hq_location_summary(
            "Parent company headquarters: Atlantis City, Atlantis")
        assert out == "Hoofdkantoor moederbedrijf: Atlantis City, Atlantis"

    def test_country_only(self):
        out = localize_hq_location_summary("Parent company headquarters: Japan")
        assert out == "Hoofdkantoor moederbedrijf: Japan"

    def test_non_matching_text_unchanged(self):
        assert localize_hq_location_summary("some other text") == "some other text"

    def test_blank_passthrough(self):
        assert localize_hq_location_summary(None) is None


class TestLocalizationIT:
    def test_foreign_parent_italian(self):
        out = localize_hq_location_summary_it(
            "Parent company headquarters: Tokyo, Japan")
        assert out == "Sede centrale della capogruppo: Tokyo, Giappone"

    def test_domestic_italian(self):
        out = localize_hq_location_summary_it(
            "Headquarters: Amsterdam, Netherlands")
        assert out == "Sede centrale: Amsterdam, Paesi Bassi"
