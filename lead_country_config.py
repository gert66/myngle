"""Central country normalization/alias config for Lead Prioritizer v2.

Unifies two previously separate, slightly-divergent tables that both lived
inline in other modules:

- ``hq_simple_detector.py``'s ``_COUNTRY_ALIASES`` / ``_std_country`` — a
  lowercase-alias-to-display-name table used to standardize a free-form
  country string for comparison/display.
- ``hq_simple_detector.py``'s and ``lead_hq_ai_interpreter.py``'s two
  independent (near-duplicate) ``_normalize_country_for_hq`` functions,
  used to compare two country strings for equality regardless of
  ISO-2/ISO-3/full-name/adjective form. ``lead_hq_ai_interpreter.py``'s
  version additionally covered Brazil/Uruguay; ``hq_simple_detector.py``'s
  did not. This module's ``_NORMALIZE_MAP`` is the union of both — no
  existing mapping is lost.

Both modules now import from here instead of defining their own copies.
``hq_lookup_probe_app.py`` and ``validate_hq_recovery_export.py`` keep
their own independent, pre-existing copies — they are standalone
probe/validation scripts outside the production ``prioritize_single_lead``
flow and are intentionally left untouched.
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Alias (lowercase) -> canonical DISPLAY country name. Moved verbatim from
# hq_simple_detector.py's _COUNTRY_ALIASES (no content change).
# ---------------------------------------------------------------------------
COUNTRY_ALIASES: dict[str, str] = {
    "italy": "Italy", "italia": "Italy", "italian": "Italy", "ita": "Italy",
    "germany": "Germany", "deutschland": "Germany", "german": "Germany", "deu": "Germany",
    "france": "France", "french": "France", "fra": "France",
    "spain": "Spain", "españa": "Spain", "spanish": "Spain",
    "netherlands": "Netherlands", "holland": "Netherlands", "dutch": "Netherlands",
    "the netherlands": "Netherlands", "nederland": "Netherlands",
    "belgium": "Belgium", "belgian": "Belgium",
    "switzerland": "Switzerland", "swiss": "Switzerland", "svizzera": "Switzerland",
    "austria": "Austria", "austrian": "Austria",
    "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "united states": "United States", "usa": "United States", "america": "United States",
    "japan": "Japan", "japanese": "Japan",
    "china": "China", "chinese": "China",
    "sweden": "Sweden", "swedish": "Sweden",
    "denmark": "Denmark", "danish": "Denmark",
    "norway": "Norway", "norwegian": "Norway",
    "finland": "Finland", "finnish": "Finland",
    "portugal": "Portugal", "portuguese": "Portugal",
    "poland": "Poland", "polish": "Poland",
    "luxembourg": "Luxembourg",
    "ireland": "Ireland", "irish": "Ireland",
    "singapore": "Singapore",
}


def std_country(raw: str) -> str:
    """Standardize a free-form country string to its display form, or
    return the (stripped) input unchanged when it has no known alias."""
    return COUNTRY_ALIASES.get((raw or "").strip().lower(), (raw or "").strip())


# ---------------------------------------------------------------------------
# Lowercase canonical KEY (for equality comparisons) -> normalized key.
# Union of hq_simple_detector.py's and lead_hq_ai_interpreter.py's two
# formerly-independent _MAP dicts. lead_hq_ai_interpreter.py's was already a
# strict superset (same entries + Brazil/Uruguay), so this is that dict,
# unchanged in content.
# ---------------------------------------------------------------------------
_NORMALIZE_MAP: dict[str, str] = {
    "it": "italy", "ita": "italy", "italia": "italy", "italy": "italy", "italian": "italy",
    "de": "germany", "deu": "germany", "germany": "germany",
    "deutschland": "germany", "german": "germany",
    "fr": "france", "fra": "france", "france": "france", "french": "france",
    "uk": "united kingdom", "gb": "united kingdom", "gbr": "united kingdom",
    "united kingdom": "united kingdom", "great britain": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom", "wales": "united kingdom",
    "us": "united states", "usa": "united states", "united states": "united states",
    "america": "united states",
    "ch": "switzerland", "switzerland": "switzerland", "swiss": "switzerland",
    "nl": "netherlands", "netherlands": "netherlands", "holland": "netherlands",
    "the netherlands": "netherlands", "nederland": "netherlands",
    "be": "belgium", "belgium": "belgium",
    "at": "austria", "austria": "austria",
    "es": "spain", "spain": "spain",
    "se": "sweden", "sweden": "sweden",
    "no": "norway", "norway": "norway",
    "dk": "denmark", "denmark": "denmark",
    "fi": "finland", "finland": "finland",
    "jp": "japan", "japan": "japan",
    "cn": "china", "china": "china",
    "pl": "poland", "poland": "poland",
    "pt": "portugal", "portugal": "portugal",
    "ie": "ireland", "ireland": "ireland",
    "lu": "luxembourg", "luxembourg": "luxembourg",
    "sg": "singapore", "singapore": "singapore",
    "br": "brazil", "bra": "brazil", "brasil": "brazil",
    "brazil": "brazil", "brazilian": "brazil",
    "uy": "uruguay", "ury": "uruguay", "uruguay": "uruguay",
    "uruguayan": "uruguay", "república oriental del uruguay": "uruguay",
}


def normalize_country_for_hq(value: object) -> str:
    """Lowercase canonical country key — handles ISO-2, ISO-3, full names."""
    text = re.sub(r"\s+", " ", re.sub(r"\.", "", str(value or "").strip().lower()))
    return _NORMALIZE_MAP.get(text, text)


# ---------------------------------------------------------------------------
# gl/hl localization for the HQ Serper query (call_serper_for_hq). A
# separate, dedicated table from lead_non_hq_enrichment.py's own
# gl_hl_for_country and deep_dive_runner.py's gl_hl_for_country — those
# stay untouched; this one is specific to the HQ search and additionally
# suppresses hl for genuinely multilingual countries (e.g. Switzerland has
# de/fr/it speakers, so no single hl is correct).
# ---------------------------------------------------------------------------

# Countries with no single dominant search language — hl is deliberately
# omitted (gl is still set) rather than guessing one language over another.
_MULTILINGUAL_HQ_COUNTRIES = frozenset({"switzerland"})

# Keyed on the SAME normalized key normalize_country_for_hq() produces, so
# lookups are consistent with the rest of this module. Value is (gl, hl);
# hl is None for multilingual countries.
_HQ_GL_HL_BY_NORMALIZED_COUNTRY: dict[str, tuple[str, Optional[str]]] = {
    "italy": ("it", "it"),
    "germany": ("de", "de"),
    "france": ("fr", "fr"),
    "spain": ("es", "es"),
    "netherlands": ("nl", "nl"),
    "belgium": ("be", "nl"),
    "switzerland": ("ch", None),
    "austria": ("at", "de"),
    "united kingdom": ("gb", "en"),
    "united states": ("us", "en"),
    "japan": ("jp", "ja"),
    "china": ("cn", "zh"),
    "sweden": ("se", "sv"),
    "norway": ("no", "no"),
    "denmark": ("dk", "da"),
    "finland": ("fi", "fi"),
    "portugal": ("pt", "pt"),
    "poland": ("pl", "pl"),
    "ireland": ("ie", "en"),
    "luxembourg": ("lu", "fr"),
    "singapore": ("sg", "en"),
    "brazil": ("br", "pt"),
    "uruguay": ("uy", "es"),
    "australia": ("au", "en"),
    "new zealand": ("nz", "en"),
    "south korea": ("kr", "ko"),
}


def gl_hl_for_hq_country(country: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Map a lead's effective country to Serper's ``gl``/``hl`` params for
    the HQ query. Returns ``(None, None)`` for an unrecognised/blank
    country so the caller omits both params — exactly today's (unlocalized)
    behavior for any country not explicitly listed here.

    ``gl`` is set whenever the country is recognised; ``hl`` is
    additionally omitted (``None``) for known multilingual countries (see
    ``_MULTILINGUAL_HQ_COUNTRIES``) rather than guessing a language.
    """
    key = normalize_country_for_hq(country)
    entry = _HQ_GL_HL_BY_NORMALIZED_COUNTRY.get(key)
    if entry is None:
        return None, None
    gl, hl = entry
    if key in _MULTILINGUAL_HQ_COUNTRIES:
        return gl, None
    return gl, hl
