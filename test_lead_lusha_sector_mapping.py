"""Tests for lead_lusha_sector_mapping.py (Lusha enrichment plan, Stap 2).

Covers: sub-industry mapping wins over main-industry, the explicit fold
rules (Business Services family, Entertainment family, Community &
Nonprofit -> Public sector/government), every mapped category is one of
the existing internal _SECTOR_KEYWORD_MAP categories, no-hit returns
None, and the Description/Specialties text-fallback tier (reusing
extract_sector_industry via a synthetic LeadEvidence).
"""

from __future__ import annotations

import lead_lusha_sector_mapping as lsm
from lead_non_hq_signal_extractor import _SECTOR_KEYWORD_MAP

_CANONICAL_INDUSTRIES = {v[0] for v in _SECTOR_KEYWORD_MAP.values()}


class TestSubIndustryMapping:
    def test_sub_industry_takes_priority_over_main(self):
        result = lsm.sector_from_lusha_industry(
            "Manufacturing", "Industrial Machinery & Equipment")
        assert result["detected_industry"] == "Industrial equipment and machinery"
        assert result["sector_source"] == "lusha_mapped"

    def test_sub_industry_case_insensitive(self):
        result = lsm.sector_from_lusha_industry(None, "industrial machinery & equipment")
        assert result["detected_industry"] == "Industrial equipment and machinery"

    def test_falls_back_to_main_when_sub_blank(self):
        result = lsm.sector_from_lusha_industry("Healthcare", "")
        assert result["detected_industry"] == "Healthcare"

    def test_falls_back_to_main_when_sub_unmapped(self):
        result = lsm.sector_from_lusha_industry("Healthcare", "Some Unmapped Sub Label")
        assert result["detected_industry"] == "Healthcare"

    def test_no_hit_returns_none(self):
        assert lsm.sector_from_lusha_industry("Narnia Industry", "Narnia Sub") is None

    def test_blank_inputs_return_none(self):
        assert lsm.sector_from_lusha_industry(None, None) is None
        assert lsm.sector_from_lusha_industry("", "") is None


class TestExplicitFoldRules:
    def test_advertising_pr_folds_to_marketing_not_consulting(self):
        result = lsm.sector_from_lusha_industry(
            "Business Services", "Advertising, Public Relations & Marketing Services")
        assert result["detected_industry"] == "Marketing and advertising"

    def test_security_investigations_folds_to_security_services(self):
        result = lsm.sector_from_lusha_industry("Business Services", "Security & Investigations")
        assert result["detected_industry"] == "Security services"

    def test_other_business_services_subs_fold_to_consulting(self):
        for sub in (
            "Business Consulting & Services", "Staffing & Recruiting",
            "Human Resources Services", "Law Firms & Legal Services",
            "Architecture & Planning", "Environmental Services",
        ):
            result = lsm.sector_from_lusha_industry("Business Services", sub)
            assert result["detected_industry"] == "Consulting", f"sub={sub!r}"

    def test_business_services_main_only_folds_to_consulting(self):
        result = lsm.sector_from_lusha_industry("Business Services", None)
        assert result["detected_industry"] == "Consulting"

    def test_entertainment_subs_fold_to_media(self):
        for sub in (
            "Sports", "Wellness & Fitness Services", "Recreational Facilities",
            "Performing Arts", "Arts & Cultural Creators",
            "Museums, Historical Sites, & Zoos", "Gambling Facilities & Casinos",
        ):
            result = lsm.sector_from_lusha_industry("Entertainment", sub)
            assert result["detected_industry"] == "Media", f"sub={sub!r}"

    def test_entertainment_main_only_folds_to_media(self):
        result = lsm.sector_from_lusha_industry("Entertainment", None)
        assert result["detected_industry"] == "Media"

    def test_community_nonprofit_folds_to_public_sector(self):
        result = lsm.sector_from_lusha_industry("Community & Nonprofit Organizations", None)
        assert result["detected_industry"] == "Public sector / government"

    def test_government_maps_to_public_sector(self):
        result = lsm.sector_from_lusha_industry("Government", None)
        assert result["detected_industry"] == "Public sector / government"


class TestFurtherCleanMappingExamples:
    def test_industrial_machinery_equipment(self):
        assert lsm.sector_from_lusha_industry(None, "Industrial Machinery & Equipment"
                                              )["detected_industry"] == "Industrial equipment and machinery"

    def test_chemicals_related_products(self):
        assert lsm.sector_from_lusha_industry(None, "Chemicals & Related Products"
                                              )["detected_industry"] == "Chemicals"

    def test_food_beverage(self):
        assert lsm.sector_from_lusha_industry(None, "Food & Beverage")["detected_industry"] == "Food and beverage"

    def test_pharmaceuticals_manufacturing(self):
        assert lsm.sector_from_lusha_industry(None, "Pharmaceuticals Manufacturing"
                                              )["detected_industry"] == "Pharmaceuticals"

    def test_medical_equipment(self):
        assert lsm.sector_from_lusha_industry(None, "Medical Equipment")["detected_industry"] == "Healthcare"

    def test_motor_vehicles(self):
        assert lsm.sector_from_lusha_industry(None, "Motor Vehicles")["detected_industry"] == "Automotive"

    def test_it_consulting_it_services(self):
        assert lsm.sector_from_lusha_industry(None, "IT Consulting & IT Services"
                                              )["detected_industry"] == "Technology"

    def test_software_development(self):
        assert lsm.sector_from_lusha_industry(None, "Software Development")["detected_industry"] == "Software"

    def test_telecommunications(self):
        assert lsm.sector_from_lusha_industry(None, "Telecommunications")["detected_industry"] == "Telecommunications"

    def test_book_newspaper_publishing_and_broadcast_to_media(self):
        assert lsm.sector_from_lusha_industry(None, "Book & Newspaper Publishing")["detected_industry"] == "Media"
        assert lsm.sector_from_lusha_industry(
            None, "Broadcast Media Production & Distribution")["detected_industry"] == "Media"

    def test_ecommerce_marketplace_and_retail_subs_to_retail(self):
        assert lsm.sector_from_lusha_industry(None, "E-Commerce & Marketplace")["detected_industry"] == "Retail"

    def test_building_garden_materials(self):
        assert lsm.sector_from_lusha_industry(
            None, "Building & Garden Materials")["detected_industry"] == "Building materials"

    def test_freight_truck_warehousing_to_logistics(self):
        for sub in ("Freight & Package Transportation", "Truck Transportation", "Warehousing & Storage"):
            assert lsm.sector_from_lusha_industry(None, sub)["detected_industry"] == "Logistics", sub

    def test_ground_airlines_maritime_to_transportation(self):
        for sub in ("Ground Passenger Transportation", "Airlines, Airports & Air Services",
                    "Maritime Transportation"):
            assert lsm.sector_from_lusha_industry(None, sub)["detected_industry"] == "Transportation", sub

    def test_financial_family_to_financial_services(self):
        for sub in ("Banking", "Investment", "International Trade & Development",
                    "Capital Markets", "Venture Capital & Private Equity Principals"):
            assert lsm.sector_from_lusha_industry(None, sub)["detected_industry"] == "Financial services", sub

    def test_insurance(self):
        assert lsm.sector_from_lusha_industry(None, "Insurance")["detected_industry"] == "Insurance"

    def test_oil_gas_mining_to_energy(self):
        assert lsm.sector_from_lusha_industry(None, "Oil & Gas")["detected_industry"] == "Energy"
        assert lsm.sector_from_lusha_industry("Oil, Gas & Mining", None)["detected_industry"] == "Energy"
        assert lsm.sector_from_lusha_industry("Utilities", None)["detected_industry"] == "Energy"

    def test_farming_ranching_forestry(self):
        assert lsm.sector_from_lusha_industry(
            None, "Farming, Ranching, Forestry")["detected_industry"] == "Agriculture"

    def test_construction(self):
        assert lsm.sector_from_lusha_industry("Construction", None)["detected_industry"] == "Construction"

    def test_real_estate(self):
        assert lsm.sector_from_lusha_industry("Real Estate", None)["detected_industry"] == "Real estate"

    def test_education(self):
        assert lsm.sector_from_lusha_industry("Education", None)["detected_industry"] == "Education"

    def test_hospitality(self):
        assert lsm.sector_from_lusha_industry("Hospitality", None)["detected_industry"] == "Hospitality"


class TestAllMappedCategoriesAreCanonical:
    """Every value this module can ever produce must already exist in
    lead_non_hq_signal_extractor._SECTOR_KEYWORD_MAP -- no new internal
    category is invented here."""

    def test_all_sub_industry_targets_are_canonical(self):
        for industry, _sub in lsm.LUSHA_SUB_INDUSTRY_MAP.values():
            assert industry in _CANONICAL_INDUSTRIES, industry

    def test_all_main_industry_targets_are_canonical(self):
        for industry, _sub in lsm.LUSHA_MAIN_INDUSTRY_MAP.values():
            assert industry in _CANONICAL_INDUSTRIES, industry


class TestSectorFromLushaText:
    def test_matches_keyword_in_description(self):
        result = lsm.sector_from_lusha_text(
            "We manufacture industrial machinery for factories worldwide.", "")
        assert result["detected_industry"] == "Industrial equipment and machinery"
        assert result["sector_source"] == "lusha_text_fallback"
        assert result["sector_confidence"] == "Low"  # no URL -> never High/Medium

    def test_matches_keyword_in_specialties_when_description_blank(self):
        result = lsm.sector_from_lusha_text("", "machinery, industrial equipment")
        assert result["detected_industry"] == "Industrial equipment and machinery"

    def test_combines_description_and_specialties(self):
        result = lsm.sector_from_lusha_text("A local company.", "chemicals, resins")
        assert result["detected_industry"] == "Chemicals"

    def test_no_keyword_match_returns_all_none(self):
        result = lsm.sector_from_lusha_text("A generic company doing generic things.", "")
        assert result["detected_industry"] is None
        assert result["sector_source"] is None

    def test_blank_inputs_return_all_none(self):
        result = lsm.sector_from_lusha_text("", None)
        assert result["detected_industry"] is None

    def test_never_raises_on_none(self):
        result = lsm.sector_from_lusha_text(None, None)
        assert result["detected_industry"] is None
