"""Maps Lusha's Main/Sub Industry taxonomy onto Lead Prioritizer v2's
EXISTING internal sector categories (the category vocabulary already used
by ``lead_non_hq_signal_extractor._SECTOR_KEYWORD_MAP``) — Stap 2 of the
Lusha enrichment plan.

Lusha labels are NEVER used 1:1 as the internal ``detected_industry``
value; every mapped label goes through the explicit tables below, and
every value produced is one of the categories already present in
``_SECTOR_KEYWORD_MAP`` — no new internal category is invented here.

Sector detection stays audit/app-only, exactly like
``lead_non_hq_signal_extractor.extract_sector_industry``: it never feeds a
score, C4, C5, HQ, or foreign-HQ filtering.

Two entry points, one per new sector tier:

- ``sector_from_lusha_industry()`` — Tier 1 (highest priority): Sub
  Industry tried first (more specific), then Main Industry. Returns
  ``None`` when neither maps, so the caller falls through to the existing
  Serper-evidence / own-domain-AI tiers unchanged.
- ``sector_from_lusha_text()`` — last-resort tier, tried only when every
  other tier (including the two above) found nothing: reuses
  ``extract_sector_industry``'s own keyword matcher on the Lusha Company
  Description + Company Specialties text via a synthetic
  ``LeadEvidence`` — no new keyword table, no API call.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Sub Industry (lowercase Lusha label) -> (internal industry, internal
# sub_industry or None). Tried BEFORE the main-industry table below — more
# specific. Built from the real Lusha taxonomy values observed in the Lusha
# Switzerland test export plus the plan's explicit fold/example rules.
# ---------------------------------------------------------------------------
LUSHA_SUB_INDUSTRY_MAP: dict[str, tuple[str, Optional[str]]] = {
    "industrial machinery & equipment": ("Industrial equipment and machinery", None),
    "higher education": ("Education", "Higher education"),
    "it consulting & it services": ("Technology", "IT services"),
    "hospitals & clinics": ("Healthcare", "Hospitals"),
    "business consulting & services": ("Consulting", None),
    "specialty trade contractors": ("Construction", "Specialty trade contractors"),
    "food & beverage": ("Food and beverage", None),
    "financial services": ("Financial services", None),
    "building construction": ("Construction", None),
    "hotels & accommodation services": ("Hospitality", "Hotels"),
    "sports": ("Media", "Sports"),
    "software development": ("Software", None),
    "pharmaceuticals manufacturing": ("Pharmaceuticals", None),
    "medical equipment": ("Healthcare", "Medical devices"),
    "insurance": ("Insurance", None),
    "luxury goods & jewelry retail": ("Retail", "Luxury goods and jewelry"),
    "staffing & recruiting": ("Consulting", "Staffing and recruiting"),
    "advertising, public relations & marketing services": ("Marketing and advertising", None),
    "banking": ("Financial services", "Banking"),
    "ground passenger transportation": ("Transportation", "Ground passenger transportation"),
    "food & beverage retail": ("Retail", "Food and beverage retail"),
    "freight & package transportation": ("Logistics", "Freight and package transportation"),
    "utilities": ("Energy", None),
    "wholesale import & export": ("Retail", "Wholesale import and export"),
    "environmental services": ("Consulting", "Environmental services"),
    "chemicals & related products": ("Chemicals", None),
    "computer & electronics manufacturing": ("Manufacturing", "Computer and electronics manufacturing"),
    "facilities services": ("Consulting", "Facilities services"),
    "biotechnology research services": ("Pharmaceuticals", "Biotechnology"),
    "investment": ("Financial services", "Investment"),
    "civil engineering": ("Construction", "Civil engineering"),
    "building & garden materials": ("Building materials", None),
    "motor vehicles": ("Automotive", None),
    "plastics & rubber products": ("Chemicals", "Plastics and rubber products"),
    "apparel & fashion retail": ("Retail", "Apparel and fashion"),
    "food & beverage services": ("Hospitality", "Food and beverage services"),
    "real estate investment and development": ("Real estate", "Investment and development"),
    "travel & reservation services": ("Hospitality", "Travel and reservation services"),
    "fabricated metal products": ("Industrial equipment and machinery", "Fabricated metal products"),
    "airlines, airports & air services": ("Transportation", "Airline"),
    "restaurants": ("Hospitality", "Restaurants"),
    "international trade & development": ("Financial services", "International trade and development"),
    "textile & apparel manufacturing": ("Manufacturing", "Textile and apparel manufacturing"),
    "architecture & planning": ("Consulting", "Architecture and planning"),
    "furniture": ("Manufacturing", "Furniture"),
    "oil & gas": ("Energy", "Oil and gas"),
    "general merchandise retail": ("Retail", "General merchandise"),
    "transportation equipment & machinery": ("Industrial equipment and machinery", "Transportation equipment and machinery"),
    "training": ("Education", "Training"),
    "events services": ("Hospitality", "Events services"),
    "law firms & legal services": ("Consulting", "Legal services"),
    "security & investigations": ("Security services", None),
    "truck transportation": ("Logistics", "Truck transportation"),
    "real estate agencies": ("Real estate", "Agencies"),
    "telecommunications": ("Telecommunications", None),
    "personal care products": ("Consumer goods", "Personal care products"),
    "glass, ceramics, clay & concrete": ("Building materials", "Glass, ceramics, clay and concrete"),
    "farming, ranching, forestry": ("Agriculture", None),
    "mining": ("Energy", "Mining"),
    "home & office equipment retail": ("Retail", "Home and office equipment"),
    "motor vehicle, parts dealers & tire stores": ("Automotive", "Dealers and parts"),
    "human resources services": ("Consulting", "Human resources services"),
    "wellness & fitness services": ("Media", "Wellness and fitness"),
    "entertainment providers": ("Media", "Entertainment providers"),
    "semiconductor & renewable energy semiconductor": ("Technology", "Semiconductor"),
    "aerospace & defense": ("Industrial equipment and machinery", "Aerospace and defense"),
    "digital information & data solutions": ("Technology", None),
    "maritime transportation": ("Transportation", "Maritime transportation"),
    "warehousing & storage": ("Logistics", "Warehousing and storage"),
    "health & personal care stores retail": ("Retail", "Health and personal care stores"),
    "paper & forest product": ("Manufacturing", "Paper and forest products"),
    "accounting & services": ("Consulting", "Accounting services"),
    "performing arts": ("Media", "Performing arts"),
    "recreational facilities": ("Media", "Recreational facilities"),
    "book & newspaper publishing": ("Media", "Publishing"),
    "broadcast media production & distribution": ("Media", "Broadcasting"),
    "computer & network security services": ("Technology", "Computer and network security"),
    "sport, music, books & hobbies retail": ("Retail", "Sport, music, books and hobbies"),
    "e-commerce & marketplace": ("Retail", "E-commerce"),
    "information services": ("Technology", "Information services"),
    "outsourcing & offshoring consulting": ("Consulting", "Outsourcing and offshoring"),
    "translation & localization": ("Consulting", "Translation and localization"),
    "sporting goods manufacturing": ("Manufacturing", "Sporting goods"),
    "e-learning providers": ("Education", "E-learning"),
    "venture capital & private equity principals": ("Financial services", "Venture capital and private equity"),
    "museums, historical sites, & zoos": ("Media", "Museums and historical sites"),
    "gambling facilities & casinos": ("Media", "Gambling facilities"),
    "arts & cultural creators": ("Media", "Arts and cultural creators"),
    "capital markets": ("Financial services", "Capital markets"),
    "internet publishing": ("Media", "Internet publishing"),
    "design services": ("Consulting", "Design services"),
    "property management": ("Real estate", "Property management"),
    "civic & social organizations": ("Public sector / government", "Civic and social organizations"),
    "international affairs": ("Public sector / government", "International affairs"),
    "education administration programs": ("Education", "Administration"),
    "computer & mobile games": ("Media", "Games"),
    "computer systems architectural, design & services": ("Technology", "Computer systems design"),
    "movies, videos & sound": ("Media", "Movies, videos and sound"),
    "photography services": ("Media", "Photography services"),
    "writing & editing": ("Media", "Writing and editing"),
    # "other" is deliberately absent — no reliable mapping; falls through.
}

# ---------------------------------------------------------------------------
# Main Industry (lowercase Lusha label) -> (internal industry, internal
# sub_industry or None). Fallback ONLY — tried when Sub Industry is blank or
# has no entry above. Broader by construction (one Lusha main bucket often
# spans several internal categories); picks the single best general label.
# ---------------------------------------------------------------------------
LUSHA_MAIN_INDUSTRY_MAP: dict[str, tuple[str, Optional[str]]] = {
    "manufacturing": ("Manufacturing", None),
    "business services": ("Consulting", None),
    "technology, information & media": ("Technology", None),
    "retail & wholesale trade": ("Retail", None),
    "finance": ("Financial services", None),
    "construction": ("Construction", None),
    "education": ("Education", None),
    "hospitality": ("Hospitality", None),
    "healthcare": ("Healthcare", None),
    "transportation & logistics": ("Logistics", None),
    "entertainment": ("Media", None),
    "real estate": ("Real estate", None),
    "oil, gas & mining": ("Energy", None),
    "utilities": ("Energy", None),
    "farming, ranching, forestry": ("Agriculture", None),
    "community & nonprofit organizations": ("Public sector / government", None),
    "government": ("Public sector / government", None),
}


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _build_lusha_mapped_result(
    industry: str, sub_industry: Optional[str], field_label: str, raw_value: str,
) -> dict:
    return {
        "detected_industry": industry,
        "detected_sub_industry": sub_industry,
        "detected_company_type": None,
        "sector_confidence": "High",
        "sector_reason": f"Mapped from Lusha {field_label} = {raw_value!r}.",
        "sector_evidence_url": None,
        "sector_evidence_quote": None,
        "sector_source_title": None,
        "sector_source": "lusha_mapped",
    }


def sector_from_lusha_industry(
    main_industry: Optional[str], sub_industry: Optional[str],
) -> Optional[dict]:
    """Tier 1 (highest priority): map Lusha's Sub Industry (tried first) or
    Main Industry onto an internal sector category.

    Returns a sector-summary-shaped dict (same keys as
    ``lead_non_hq_signal_extractor.extract_sector_industry``'s return
    value) on a hit, or ``None`` when neither field maps — the caller then
    falls through to the remaining tiers (own-domain Firecrawl+AI, then
    the Lusha Description/Specialties text fallback below — see Stap 4;
    there is no live Serper sector query/evidence tier anymore).
    """
    sub_key = _norm(sub_industry)
    if sub_key and sub_key in LUSHA_SUB_INDUSTRY_MAP:
        industry, sub = LUSHA_SUB_INDUSTRY_MAP[sub_key]
        return _build_lusha_mapped_result(industry, sub, "Sub Industry", sub_industry)

    main_key = _norm(main_industry)
    if main_key and main_key in LUSHA_MAIN_INDUSTRY_MAP:
        industry, sub = LUSHA_MAIN_INDUSTRY_MAP[main_key]
        return _build_lusha_mapped_result(industry, sub, "Main Industry", main_industry)

    return None


def sector_from_lusha_text(description: Optional[str], specialties: Optional[str]) -> dict:
    """Last-resort sector tier: reuses the existing, tested Serper-evidence
    keyword matcher (``lead_non_hq_signal_extractor.extract_sector_industry``)
    on the Lusha Company Description + Company Specialties text instead of
    Serper evidence, via a synthetic ``LeadEvidence`` — no new keyword
    table, no API call. Returns the same all-``None`` sector-summary shape
    as ``extract_sector_industry`` when neither field has usable text or
    nothing matches.
    """
    from lead_non_hq_signal_extractor import extract_sector_industry
    from lead_output_schema import LeadEvidence

    text = " ".join(t.strip() for t in (description or "", specialties or "") if t and t.strip())
    if not text:
        return extract_sector_industry([])

    synthetic = LeadEvidence(
        evidence_id="lusha_text:sector_industry:1",
        signal_name="sector_industry",
        source_snippet=text,
        source_type="lusha_text",
        parser_source="lusha_description_specialties",
    )
    result = extract_sector_industry([synthetic])
    if result["detected_industry"]:
        result = dict(result)
        result["sector_source"] = "lusha_text_fallback"
        result["sector_reason"] = (
            "Matched sector keyword(s) in Lusha Company Description/Specialties "
            "(no Lusha industry mapping hit, no Serper/own-domain evidence found "
            "first): " + result["sector_reason"]
        )
    return result
