"""Deterministic builder for the always-shown ``hq_location_summary`` field.

A single, structured human-readable line giving the company's HQ location for
the app, ADDITIONAL to (and independent of) the
``foreign_ownership_or_group_structure`` driver's own
Strong/Moderate/Weak/Not-evidenced badge — that badge's meaning/logic is
untouched by this module.

The value is emitted in an English base form (localized downstream for NL/IT
in ``lovable_content_localization.py``, mirroring ``parent_hq_summary_app``),
using two fixed prefixes so the frontend can rely on presence/absence and on a
stable prefix rather than parsing free text:

  - Foreign parent found  → ``"Parent company headquarters: Tokyo, Japan"``
  - Domestic HQ evidenced → ``"Headquarters: Amsterdam, Netherlands"``
  - Neither known         → ``None`` (never a guess or placeholder)

City/country are resolved with the SAME priority chain the export already uses
for the parent-HQ country (``resolve_parent_hq_country_for_export`` in
``lead_prioritizer_batch_core.py``): C5 fields first, then the AI HQ fields,
then the detected HQ fields.
"""

from __future__ import annotations

from typing import Optional

# English base prefixes. Localized structurally downstream; kept as module
# constants so tests / the localizer share one source of truth.
FOREIGN_PARENT_PREFIX = "Parent company headquarters: "
DOMESTIC_HQ_PREFIX = "Headquarters: "


def _clean(value) -> str:
    return str(value or "").strip()


def _pick(*values) -> str:
    """First non-blank value, whitespace-trimmed (mirrors the C5>AI>detected
    priority ordering used by resolve_parent_hq_country_for_export)."""
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ""


def _format_location(city: str, country: str) -> str:
    """``"City, Country"`` when both known, else whichever single one is."""
    city, country = _clean(city), _clean(country)
    if city and country:
        return f"{city}, {country}"
    return country or city


def build_hq_location_summary(
    *,
    foreign_hq_simple: Optional[bool] = None,
    hq_structure_type: Optional[str] = None,
    c5_parent_hq_country: Optional[str] = None,
    c5_parent_hq_city: Optional[str] = None,
    ai_parent_hq_country: Optional[str] = None,
    ai_parent_hq_city: Optional[str] = None,
    hq_detected_country: Optional[str] = None,
    hq_detected_city: Optional[str] = None,
) -> Optional[str]:
    """Build the ``hq_location_summary`` string, or ``None`` when not derivable.

    ``foreign_hq_simple`` / ``hq_structure_type`` decide *which* line to build;
    they are the factual structure determination and are deliberately NOT the
    driver badge. When the structure is a foreign parent, the parent city/country
    are resolved via the C5 > AI > detected priority chain. When the structure is
    explicitly domestic, the detected HQ city/country are used. Any other
    structure (regional_branch_only, unclear, blank) with no confirmed parent
    yields ``None`` rather than a potentially misleading line.
    """
    structure = _clean(hq_structure_type).lower()
    is_foreign_parent = bool(foreign_hq_simple) or structure == "foreign_parent"

    if is_foreign_parent:
        country = _pick(c5_parent_hq_country, ai_parent_hq_country, hq_detected_country)
        city = _pick(c5_parent_hq_city, ai_parent_hq_city, hq_detected_city)
        location = _format_location(city, country)
        return FOREIGN_PARENT_PREFIX + location if location else None

    if structure == "domestic":
        location = _format_location(_clean(hq_detected_city), _clean(hq_detected_country))
        return DOMESTIC_HQ_PREFIX + location if location else None

    return None


def build_hq_location_summary_from_row(row: dict) -> Optional[str]:
    """Convenience wrapper reading a flattened Enriched-Leads row dict.

    Used by the batch C5 layer to recompute the summary once the ``c5_*``
    parent fields are present, so C5's richer parent HQ takes priority.
    """
    return build_hq_location_summary(
        foreign_hq_simple=row.get("foreign_hq_simple"),
        hq_structure_type=row.get("hq_structure_type"),
        c5_parent_hq_country=row.get("c5_parent_hq_country"),
        c5_parent_hq_city=row.get("c5_parent_hq_city"),
        ai_parent_hq_country=row.get("ai_parent_hq_country"),
        ai_parent_hq_city=row.get("ai_parent_hq_city"),
        hq_detected_country=row.get("hq_detected_country"),
        hq_detected_city=row.get("hq_detected_city"),
    )
