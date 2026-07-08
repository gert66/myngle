"""Tests for lead_country_config.py — the unified country normalization/
alias/gl-hl config shared by hq_simple_detector.py and
lead_hq_ai_interpreter.py."""

from __future__ import annotations

import lead_country_config as cc


class TestCountryAliases:
    def test_std_country_known_alias(self):
        assert cc.std_country("italia") == "Italy"
        assert cc.std_country("ITALY") == "Italy"
        assert cc.std_country("swiss") == "Switzerland"

    def test_std_country_unknown_passthrough(self):
        assert cc.std_country("Narnia") == "Narnia"

    def test_std_country_blank(self):
        assert cc.std_country("") == ""
        assert cc.std_country(None) == ""


class TestNormalizeCountryForHq:
    def test_known_iso_codes(self):
        assert cc.normalize_country_for_hq("IT") == "italy"
        assert cc.normalize_country_for_hq("DE") == "germany"
        assert cc.normalize_country_for_hq("ch") == "switzerland"

    def test_full_names_and_adjectives(self):
        assert cc.normalize_country_for_hq("Italia") == "italy"
        assert cc.normalize_country_for_hq("Italian") == "italy"
        assert cc.normalize_country_for_hq("Great Britain") == "united kingdom"

    def test_brazil_and_uruguay_present_after_merge(self):
        # These previously only existed in lead_hq_ai_interpreter.py's own
        # copy of the map, not in hq_simple_detector.py's -- confirms the
        # union preserved both.
        assert cc.normalize_country_for_hq("BR") == "brazil"
        assert cc.normalize_country_for_hq("Brazilian") == "brazil"
        assert cc.normalize_country_for_hq("UY") == "uruguay"
        assert cc.normalize_country_for_hq("República Oriental del Uruguay") == "uruguay"

    def test_unknown_country_passthrough_lowercased(self):
        assert cc.normalize_country_for_hq("Narnia") == "narnia"

    def test_blank_or_none(self):
        assert cc.normalize_country_for_hq("") == ""
        assert cc.normalize_country_for_hq(None) == ""


class TestGlHlForHqCountry:
    def test_known_single_language_country(self):
        assert cc.gl_hl_for_hq_country("Italy") == ("it", "it")
        assert cc.gl_hl_for_hq_country("Netherlands") == ("nl", "nl")

    def test_accepts_iso_and_alias_forms(self):
        assert cc.gl_hl_for_hq_country("IT") == ("it", "it")
        assert cc.gl_hl_for_hq_country("Deutschland") == ("de", "de")

    def test_switzerland_sets_gl_but_omits_hl(self):
        # Switzerland is de/fr/it -- no single hl is correct.
        gl, hl = cc.gl_hl_for_hq_country("Switzerland")
        assert gl == "ch"
        assert hl is None

    def test_switzerland_via_alias_still_omits_hl(self):
        gl, hl = cc.gl_hl_for_hq_country("swiss")
        assert gl == "ch"
        assert hl is None

    def test_unknown_country_returns_none_none(self):
        assert cc.gl_hl_for_hq_country("Narnia") == (None, None)

    def test_blank_country_returns_none_none(self):
        assert cc.gl_hl_for_hq_country("") == (None, None)
        assert cc.gl_hl_for_hq_country(None) == (None, None)

    def test_brazil_and_uruguay_covered(self):
        assert cc.gl_hl_for_hq_country("Brazil") == ("br", "pt")
        assert cc.gl_hl_for_hq_country("Uruguay") == ("uy", "es")
