"""
hq_lookup_probe_app.py

Streamlit UI for HQ headquarters detection.
Self-contained: all runtime logic is inlined here.

Run with:
    streamlit run hq_lookup_probe_app.py

This app is experimental and does not modify any production enrichment output.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

# ---------------------------------------------------------------------------
# Dependency guard
# ---------------------------------------------------------------------------
try:
    from openpyxl import load_workbook, Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    st.error("openpyxl is required:  pip install openpyxl")
    st.stop()

try:
    import requests as _requests
except ImportError:
    st.error("requests is required:  pip install requests")
    st.stop()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERPER_URL = "https://google.serper.dev/search"
_REQUEST_TIMEOUT = 12
_INTER_REQUEST_SLEEP = 0.5

DEFAULT_COMPANY_COL   = "company_name"
DEFAULT_DOMAIN_COL    = "domain"
DEFAULT_COUNTRY_COL   = "input_country"
DEFAULT_INPUT_COUNTRY = "Italy"
DEFAULT_LIMIT         = 50

OLD_ENRICHMENT_COLS = [
    "sig_foreign_hq_score",
    "sig_foreign_hq_evidence",
    "foreign_hq_sanitized",
    "foreign_hq_sanitizer_reason",
    "foreign_hq_original_score",
    "final_commercial_fit_score",
    "commercial_fit_score",
]

PROBE_COLS = [
    "hq_detected_city",
    "hq_detected_region",
    "hq_detected_country",
    "hq_confidence",
    "foreign_hq_simple",
    "input_country_used",
    "needs_manual_review",
    "hq_reason",
    "hq_evidence_url",
    "hq_evidence_quote",
    "hq_query_used",
    "serper_knowledge_graph_location",
    "serper_answer_box",
    "top_organic_title_1",
    "top_organic_snippet_1",
    "top_organic_url_1",
    "top_organic_title_2",
    "top_organic_snippet_2",
    "top_organic_url_2",
    "top_organic_title_3",
    "top_organic_snippet_3",
    "top_organic_url_3",
    "probe_error",
]

# ---------------------------------------------------------------------------
# Italian city / province maps
# ---------------------------------------------------------------------------

_ITALY_CITIES: dict[str, tuple[str, str]] = {
    "milan":          ("Milan",          "Italy"),
    "milano":         ("Milano",         "Italy"),
    "rome":           ("Rome",           "Italy"),
    "roma":           ("Roma",           "Italy"),
    "turin":          ("Turin",          "Italy"),
    "torino":         ("Torino",         "Italy"),
    "naples":         ("Naples",         "Italy"),
    "napoli":         ("Napoli",         "Italy"),
    "bologna":        ("Bologna",        "Italy"),
    "bergamo":        ("Bergamo",        "Italy"),
    "brescia":        ("Brescia",        "Italy"),
    "verona":         ("Verona",         "Italy"),
    "padova":         ("Padova",         "Italy"),
    "padua":          ("Padua",          "Italy"),
    "vicenza":        ("Vicenza",        "Italy"),
    "treviso":        ("Treviso",        "Italy"),
    "modena":         ("Modena",         "Italy"),
    "parma":          ("Parma",          "Italy"),
    "firenze":        ("Firenze",        "Italy"),
    "florence":       ("Florence",       "Italy"),
    "genova":         ("Genova",         "Italy"),
    "genoa":          ("Genoa",          "Italy"),
    "venice":         ("Venice",         "Italy"),
    "venezia":        ("Venezia",        "Italy"),
    "trieste":        ("Trieste",        "Italy"),
    "bari":           ("Bari",           "Italy"),
    "catania":        ("Catania",        "Italy"),
    "palermo":        ("Palermo",        "Italy"),
    "perugia":        ("Perugia",        "Italy"),
    "ancona":         ("Ancona",         "Italy"),
    "trento":         ("Trento",         "Italy"),
    "bolzano":        ("Bolzano",        "Italy"),
    "reggio":         ("Reggio",         "Italy"),
    "cagliari":       ("Cagliari",       "Italy"),
    "lecce":          ("Lecce",          "Italy"),
    "pescara":        ("Pescara",        "Italy"),
    "pisa":           ("Pisa",           "Italy"),
    "siena":          ("Siena",          "Italy"),
    "udine":          ("Udine",          "Italy"),
    "novara":         ("Novara",         "Italy"),
    "monza":          ("Monza",          "Italy"),
    "bentivoglio":    ("Bentivoglio",    "Italy"),
    "interporto":     ("Interporto Bologna", "Italy"),
    "castel maggiore":("Castel Maggiore","Italy"),
    "calderara":      ("Calderara di Reno", "Italy"),
    "imola":          ("Imola",          "Italy"),
    "faenza":         ("Faenza",         "Italy"),
    "rimini":         ("Rimini",         "Italy"),
    "ravenna":        ("Ravenna",        "Italy"),
    "ferrara":        ("Ferrara",        "Italy"),
    "forlì":          ("Forlì",          "Italy"),
    "forli":          ("Forlì",          "Italy"),
    "piacenza":       ("Piacenza",       "Italy"),
    "reggio emilia":  ("Reggio Emilia",  "Italy"),
    "mantova":        ("Mantova",        "Italy"),
    "mantua":         ("Mantua",         "Italy"),
    "cremona":        ("Cremona",        "Italy"),
    "como":           ("Como",           "Italy"),
    "varese":         ("Varese",         "Italy"),
    "lecco":          ("Lecco",          "Italy"),
    "pavia":          ("Pavia",          "Italy"),
    "lodi":           ("Lodi",           "Italy"),
    "sesto san giovanni": ("Sesto San Giovanni", "Italy"),
    "cinisello balsamo":  ("Cinisello Balsamo",  "Italy"),
    "busto arsizio":  ("Busto Arsizio",  "Italy"),
    "gallarate":      ("Gallarate",      "Italy"),
    "saronno":        ("Saronno",        "Italy"),
    "rho":            ("Rho",            "Italy"),
    "segrate":        ("Segrate",        "Italy"),
    "assago":         ("Assago",         "Italy"),
    "cernusco":       ("Cernusco sul Naviglio", "Italy"),
    "agrate brianza": ("Agrate Brianza", "Italy"),
    "vimercate":      ("Vimercate",      "Italy"),
    "cassina de' pecchi": ("Cassina de' Pecchi", "Italy"),
    "sesto fiorentino": ("Sesto Fiorentino", "Italy"),
    "prato":          ("Prato",          "Italy"),
    "livorno":        ("Livorno",        "Italy"),
    "lucca":          ("Lucca",          "Italy"),
    "pistoia":        ("Pistoia",        "Italy"),
    "arezzo":         ("Arezzo",         "Italy"),
    "grosseto":       ("Grosseto",       "Italy"),
    "la spezia":      ("La Spezia",      "Italy"),
    "savona":         ("Savona",         "Italy"),
    "imperia":        ("Imperia",        "Italy"),
    "salerno":        ("Salerno",        "Italy"),
    "caserta":        ("Caserta",        "Italy"),
    "foggia":         ("Foggia",         "Italy"),
    "taranto":        ("Taranto",        "Italy"),
    "brindisi":       ("Brindisi",       "Italy"),
    "cosenza":        ("Cosenza",        "Italy"),
    "catanzaro":      ("Catanzaro",      "Italy"),
    "reggio calabria": ("Reggio Calabria", "Italy"),
    "messina":        ("Messina",        "Italy"),
    "agrigento":      ("Agrigento",      "Italy"),
    "ragusa":         ("Ragusa",         "Italy"),
    "siracusa":       ("Siracusa",       "Italy"),
    "trapani":        ("Trapani",        "Italy"),
    "sassari":        ("Sassari",        "Italy"),
    "nuoro":          ("Nuoro",          "Italy"),
    "oristano":       ("Oristano",       "Italy"),
    "macerata":       ("Macerata",       "Italy"),
    "pesaro":         ("Pesaro",         "Italy"),
    "ascoli piceno":  ("Ascoli Piceno",  "Italy"),
    "teramo":         ("Teramo",         "Italy"),
    "chieti":         ("Chieti",         "Italy"),
    "campobasso":     ("Campobasso",     "Italy"),
    "potenza":        ("Potenza",        "Italy"),
    "matera":         ("Matera",         "Italy"),
}

_INTL_CITIES: dict[str, tuple[str, str]] = {
    "berlin": ("Berlin", "Germany"), "munich": ("Munich", "Germany"),
    "münchen": ("München", "Germany"), "hamburg": ("Hamburg", "Germany"),
    "frankfurt": ("Frankfurt", "Germany"), "cologne": ("Cologne", "Germany"),
    "köln": ("Köln", "Germany"), "düsseldorf": ("Düsseldorf", "Germany"),
    "stuttgart": ("Stuttgart", "Germany"), "dortmund": ("Dortmund", "Germany"),
    "mannheim": ("Mannheim", "Germany"),
    "paris": ("Paris", "France"), "lyon": ("Lyon", "France"),
    "marseille": ("Marseille", "France"), "toulouse": ("Toulouse", "France"),
    "bordeaux": ("Bordeaux", "France"), "strasbourg": ("Strasbourg", "France"),
    "amsterdam": ("Amsterdam", "Netherlands"), "rotterdam": ("Rotterdam", "Netherlands"),
    "the hague": ("The Hague", "Netherlands"), "den haag": ("Den Haag", "Netherlands"),
    "eindhoven": ("Eindhoven", "Netherlands"), "utrecht": ("Utrecht", "Netherlands"),
    "madrid": ("Madrid", "Spain"), "barcelona": ("Barcelona", "Spain"),
    "valencia": ("Valencia", "Spain"), "seville": ("Seville", "Spain"),
    "sevilla": ("Sevilla", "Spain"),
    "zurich": ("Zurich", "Switzerland"), "zürich": ("Zürich", "Switzerland"),
    "geneva": ("Geneva", "Switzerland"), "genève": ("Genève", "Switzerland"),
    "bern": ("Bern", "Switzerland"), "basel": ("Basel", "Switzerland"),
    "london": ("London", "United Kingdom"), "manchester": ("Manchester", "United Kingdom"),
    "birmingham": ("Birmingham", "United Kingdom"), "edinburgh": ("Edinburgh", "United Kingdom"),
    "vienna": ("Vienna", "Austria"), "wien": ("Wien", "Austria"),
    "graz": ("Graz", "Austria"), "salzburg": ("Salzburg", "Austria"),
    "brussels": ("Brussels", "Belgium"), "bruxelles": ("Bruxelles", "Belgium"),
    "antwerp": ("Antwerp", "Belgium"), "antwerpen": ("Antwerpen", "Belgium"),
    "new york": ("New York", "United States"), "san francisco": ("San Francisco", "United States"),
    "chicago": ("Chicago", "United States"), "boston": ("Boston", "United States"),
    "los angeles": ("Los Angeles", "United States"), "seattle": ("Seattle", "United States"),
    "stockholm": ("Stockholm", "Sweden"), "oslo": ("Oslo", "Norway"),
    "copenhagen": ("Copenhagen", "Denmark"), "helsinki": ("Helsinki", "Finland"),
    "tokyo": ("Tokyo", "Japan"), "beijing": ("Beijing", "China"),
    "shanghai": ("Shanghai", "China"), "singapore": ("Singapore", "Singapore"),
    "dublin": ("Dublin", "Ireland"), "warsaw": ("Warsaw", "Poland"),
    "lisbon": ("Lisbon", "Portugal"), "lisboa": ("Lisboa", "Portugal"),
    "luxembourg": ("Luxembourg", "Luxembourg"),
}

_COUNTRY_ALIASES: dict[str, str] = {
    "italy": "Italy", "italia": "Italy", "italian": "Italy",
    "germany": "Germany", "deutschland": "Germany", "german": "Germany",
    "france": "France", "french": "France",
    "spain": "Spain", "españa": "Spain", "spanish": "Spain",
    "netherlands": "Netherlands", "holland": "Netherlands", "dutch": "Netherlands",
    "belgium": "Belgium", "belgian": "Belgium",
    "switzerland": "Switzerland", "swiss": "Switzerland", "svizzera": "Switzerland",
    "austria": "Austria", "austrian": "Austria",
    "united kingdom": "United Kingdom", "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "united states": "United States", "usa": "United States", "us": "United States",
    "america": "United States",
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
}

_ITALY_PROVINCE_CODES: dict[str, tuple[str, str]] = {
    "AG": ("Agrigento", "Italy"), "AL": ("Alessandria", "Italy"),
    "AN": ("Ancona",    "Italy"), "AO": ("Aosta",       "Italy"),
    "AR": ("Arezzo",    "Italy"), "AP": ("Ascoli Piceno","Italy"),
    "AT": ("Asti",      "Italy"), "AV": ("Avellino",    "Italy"),
    "BA": ("Bari",      "Italy"), "BT": ("Barletta",    "Italy"),
    "BL": ("Belluno",   "Italy"), "BN": ("Benevento",   "Italy"),
    "BG": ("Bergamo",   "Italy"), "BI": ("Biella",      "Italy"),
    "BO": ("Bologna",   "Italy"), "BZ": ("Bolzano",     "Italy"),
    "BS": ("Brescia",   "Italy"), "BR": ("Brindisi",    "Italy"),
    "CA": ("Cagliari",  "Italy"), "CL": ("Caltanissetta","Italy"),
    "CB": ("Campobasso","Italy"), "CE": ("Caserta",     "Italy"),
    "CT": ("Catania",   "Italy"), "CZ": ("Catanzaro",   "Italy"),
    "CH": ("Chieti",    "Italy"), "CO": ("Como",        "Italy"),
    "CS": ("Cosenza",   "Italy"), "CR": ("Cremona",     "Italy"),
    "KR": ("Crotone",   "Italy"), "CN": ("Cuneo",       "Italy"),
    "EN": ("Enna",      "Italy"), "FM": ("Fermo",       "Italy"),
    "FE": ("Ferrara",   "Italy"), "FI": ("Firenze",     "Italy"),
    "FG": ("Foggia",    "Italy"), "FC": ("Forlì",       "Italy"),
    "FR": ("Frosinone", "Italy"), "GE": ("Genova",      "Italy"),
    "GO": ("Gorizia",   "Italy"), "GR": ("Grosseto",    "Italy"),
    "IM": ("Imperia",   "Italy"), "IS": ("Isernia",     "Italy"),
    "SP": ("La Spezia", "Italy"), "AQ": ("L'Aquila",    "Italy"),
    "LT": ("Latina",    "Italy"), "LE": ("Lecce",       "Italy"),
    "LC": ("Lecco",     "Italy"), "LI": ("Livorno",     "Italy"),
    "LO": ("Lodi",      "Italy"), "LU": ("Lucca",       "Italy"),
    "MC": ("Macerata",  "Italy"), "MN": ("Mantova",     "Italy"),
    "MS": ("Massa",     "Italy"), "MT": ("Matera",      "Italy"),
    "ME": ("Messina",   "Italy"), "MI": ("Milano",      "Italy"),
    "MO": ("Modena",    "Italy"), "MB": ("Monza",       "Italy"),
    "NA": ("Napoli",    "Italy"), "NO": ("Novara",      "Italy"),
    "NU": ("Nuoro",     "Italy"), "OR": ("Oristano",    "Italy"),
    "PD": ("Padova",    "Italy"), "PA": ("Palermo",     "Italy"),
    "PR": ("Parma",     "Italy"), "PV": ("Pavia",       "Italy"),
    "PG": ("Perugia",   "Italy"), "PU": ("Pesaro",      "Italy"),
    "PE": ("Pescara",   "Italy"), "PC": ("Piacenza",    "Italy"),
    "PI": ("Pisa",      "Italy"), "PT": ("Pistoia",     "Italy"),
    "PN": ("Pordenone", "Italy"), "PZ": ("Potenza",     "Italy"),
    "PO": ("Prato",     "Italy"), "RG": ("Ragusa",      "Italy"),
    "RA": ("Ravenna",   "Italy"), "RC": ("Reggio Calabria","Italy"),
    "RE": ("Reggio Emilia","Italy"),"RI": ("Rieti",     "Italy"),
    "RN": ("Rimini",    "Italy"), "RM": ("Roma",        "Italy"),
    "RO": ("Rovigo",    "Italy"), "SA": ("Salerno",     "Italy"),
    "SS": ("Sassari",   "Italy"), "SV": ("Savona",      "Italy"),
    "SI": ("Siena",     "Italy"), "SR": ("Siracusa",    "Italy"),
    "SO": ("Sondrio",   "Italy"), "TA": ("Taranto",     "Italy"),
    "TE": ("Teramo",    "Italy"), "TR": ("Terni",       "Italy"),
    "TO": ("Torino",    "Italy"), "TP": ("Trapani",     "Italy"),
    "TN": ("Trento",    "Italy"), "TV": ("Treviso",     "Italy"),
    "TS": ("Trieste",   "Italy"), "UD": ("Udine",       "Italy"),
    "VA": ("Varese",    "Italy"), "VE": ("Venezia",     "Italy"),
    "VB": ("Verbania",  "Italy"), "VC": ("Vercelli",    "Italy"),
    "VR": ("Verona",    "Italy"), "VV": ("Vibo Valentia","Italy"),
    "VI": ("Vicenza",   "Italy"), "VT": ("Viterbo",     "Italy"),
}

_PROVINCE_CODE_RE = re.compile(
    r"(?:[\(,\s])([A-Z]{2})(?:\)|(?:\s*[-,]\s*(?:Ital(?:y|ia)))|\s*$)",
    re.MULTILINE,
)

_ITALIAN_CAP_RE = re.compile(r"\b([1-9]\d{4})\b")

_ITALIAN_FISCAL_RE = re.compile(
    r"\b(p\.?\s*iva|partita\s+iva|codice\s+fiscale|c\.f\.)\b", re.IGNORECASE
)

_ITALIAN_ADDR_PHRASES = (
    "sede legale", "sede principale", "sede amministrativa", "sede operativa",
    "ufficio", "uffici", "registered office in italy", "registered in italy",
    "incorporata in italia", "societa italiana", "società italiana",
)

_GROUP_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"part\s+of|owned\s+by|subsidiary\s+of|member\s+of|division\s+of"
    r"|affiliated\s+(?:with|to)|belongs?\s+to"
    r"|parent\s+company|holding\s+company|holding\s+group"
    r"|gruppo\b|gruppo\s+\w+|appartenente\s+(?:a|al)"
    r"|fa\s+parte\s+(?:del|di)|filiale\s+di|controllata\s+da"
    r")\b",
    re.IGNORECASE,
)

_HQ_PATTERNS = [
    re.compile(
        r"headquarter(?:s|ed)\s+(?:in|at)\s+([A-Z][A-Za-zÀ-ÿ\s,]{2,40}?)(?:\.|,|\s*\b(?:with|and|since|in\s+\d))",
        re.IGNORECASE,
    ),
    re.compile(r"head\s+office\s+(?:in|at|is\s+in)\s+([A-Z][A-Za-zÀ-ÿ\s,]{2,40}?)(?:\.|,)", re.IGNORECASE),
    re.compile(r"based\s+in\s+([A-Z][A-Za-zÀ-ÿ\s,]{2,35}?)(?:\.|,|\s+(?:and|with|since))", re.IGNORECASE),
    re.compile(r"Headquarters?\s*:\s*([A-Za-zÀ-ÿ\s,]{2,50}?)(?:\n|$|\.)", re.IGNORECASE),
    re.compile(r"Head\s+[Oo]ffice\s*:\s*([A-Za-zÀ-ÿ\s,]{2,50}?)(?:\n|$|\.)", re.IGNORECASE),
    re.compile(r"registered\s+office\s*(?:in|at|:)\s*([A-Za-zÀ-ÿ\s,]{2,50}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"corporate\s+office\s*(?:in|at|:)\s*([A-Za-zÀ-ÿ\s,]{2,50}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"principal\s+office\s*(?:in|at|:)\s*([A-Za-zÀ-ÿ\s,]{2,50}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"registered\s+in\s+([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"sede\s+legale\s*[:\s]+(?:in\s+)?([A-Za-zÀ-ÿ\s,]{2,50}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"sede\s+principale\s*[:\s]+(?:in\s+)?([A-Za-zÀ-ÿ\s,]{2,50}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"sede\s+amministrativa\s+(?:in\s+)?([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"sede\s+operativa\s+(?:in\s+)?([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"con\s+sede\s+(?:a|in)\s+([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"ha\s+sede\s+(?:a|in)\s+([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"(?:la\s+)?(?:sua\s+)?sede\s+(?:è\s+)?(?:a|in)\s+([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"uffici\s+(?:a|in|di)\s+([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"ufficio\s+(?:a|in|di)\s+([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"c/o\s+[A-Za-zÀ-ÿ\s]+?,\s*([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
    re.compile(r"located\s+(?:in|at)\s+([A-Za-zÀ-ÿ\s,]{2,40}?)(?:\n|$|\.|,)", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Core location / country resolution
# ---------------------------------------------------------------------------

def _resolve_city_country(text: str) -> tuple[str, str]:
    """Given a location string, identify city and country. Returns ("", "") if unrecognised."""
    text_lc = text.lower().strip(" ,.")
    for alias, (city, country) in _ITALY_CITIES.items():
        if alias in text_lc:
            return city, country
    for m in _PROVINCE_CODE_RE.finditer(text):
        code = m.group(1)
        if code in _ITALY_PROVINCE_CODES:
            return _ITALY_PROVINCE_CODES[code]
    if _ITALIAN_CAP_RE.search(text):
        return "", "Italy"
    if _ITALIAN_FISCAL_RE.search(text):
        return "", "Italy"
    for phrase in _ITALIAN_ADDR_PHRASES:
        if phrase in text_lc:
            return "", "Italy"
    for alias, (city, country) in _INTL_CITIES.items():
        if alias in text_lc:
            return city, country
    for alias, country in _COUNTRY_ALIASES.items():
        if alias in text_lc:
            return "", country
    return "", ""


def resolve_country_from_location(location_text: str) -> tuple[str, str]:
    """
    Public helper: (country, city) from free-form location text.

    Examples:
        resolve_country_from_location("Milano")           -> ("Italy", "Milano")
        resolve_country_from_location("Bentivoglio (BO)") -> ("Italy", "Bentivoglio")
        resolve_country_from_location("Prato, Tuscany")   -> ("Italy", "Prato")
        resolve_country_from_location("Berlin, Germany")  -> ("Germany", "Berlin")
    """
    city, country = _resolve_city_country(location_text)
    return country, city


def _has_group_context(text: str) -> bool:
    return bool(_GROUP_CONTEXT_RE.search(text))


def _italy_in_text(text: str) -> bool:
    text_lc = text.lower()
    if "italy" in text_lc or "italia" in text_lc:
        return True
    for alias in _ITALY_CITIES:
        if alias in text_lc:
            return True
    m = _PROVINCE_CODE_RE.search(text)
    if m and m.group(1) in _ITALY_PROVINCE_CODES:
        return True
    if _ITALIAN_CAP_RE.search(text):
        return True
    return False


def _std_country(raw: str) -> str:
    """Normalise a country string via aliases."""
    return _COUNTRY_ALIASES.get(raw.strip().lower(), raw.strip())

# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------

def _extract_deterministic(
    organic: list[dict],
    kg_location: str,
    answer_box: str,
    input_country: str = "",
) -> dict[str, Any]:
    texts: list[tuple[str, str]] = []
    if kg_location:
        texts.append((kg_location, ""))
    if answer_box:
        texts.append((answer_box, ""))
    for item in organic:
        combined = f"{item.get('title', '')} {item.get('snippet', '')}"
        texts.append((combined, item.get("link", "")))

    input_country_std = _std_country(input_country)
    fallback_group_hit: dict[str, Any] = {}

    for text, url in texts:
        is_group = _has_group_context(text)
        for pat in _HQ_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            captured = m.group(1).strip(" ,.")
            city, country = _resolve_city_country(captured)
            if not city and not country:
                continue

            candidate = {
                "hq_detected_city":    city,
                "hq_detected_country": country,
                "hq_confidence":       "High",
                "hq_reason":           f"Pattern match: '{m.group(0)[:80]}'",
                "hq_evidence_url":     url,
                "hq_evidence_quote":   text[:250].strip(),
            }

            country_std = _std_country(country)
            if country_std.lower() == input_country_std.lower():
                return candidate

            if is_group:
                if _italy_in_text(text) and input_country_std == "Italy":
                    pass  # skip, look for cleaner Italian hit
                elif not fallback_group_hit:
                    candidate["hq_confidence"] = "Low"
                    candidate["hq_reason"] = "[group context detected] " + candidate["hq_reason"]
                    fallback_group_hit = candidate
                continue

            return candidate

    # Direct Italian city scan (handles addresses without explicit HQ pattern)
    for text, url in texts:
        if _italy_in_text(text):
            text_lc = text.lower()
            for alias, (city, country) in _ITALY_CITIES.items():
                if alias in text_lc:
                    return {
                        "hq_detected_city":    city,
                        "hq_detected_country": "Italy",
                        "hq_confidence":       "Medium",
                        "hq_reason":           f"Italian city '{city}' found in evidence (no explicit HQ pattern)",
                        "hq_evidence_url":     url,
                        "hq_evidence_quote":   text[:250].strip(),
                    }

    return fallback_group_hit

# ---------------------------------------------------------------------------
# Model extraction (optional Claude fallback)
# ---------------------------------------------------------------------------

_MODEL_EXTRACTION_PROMPT = """\
You are a headquarters-location extractor.

You will receive a JSON array of search result snippets for a specific company.
Your task is to identify the EXACT headquarters location of THIS specific company.

Rules:
- Extract headquarters of the EXACT company named, not of a parent group or subsidiary.
- Do NOT infer HQ from: international activity, branches, distributors, customers, \
training providers, competitor mentions, or group ownership.
- If the evidence says the company is PART OF a foreign group but also gives an Italian \
registered office or corporate address for the exact company itself, return Italy — \
group context is not the same as the company's own HQ.
- If the evidence is ONLY about a parent/holding group with no specific address for the \
exact company, set is_group_or_parent_only to true and confidence to "Low".
- entity_match: "Exact" if evidence explicitly names this company, "Likely" if very \
similar name, "Weak" if indirect, "No" if evidence is about a different entity entirely.
- If uncertain, set confidence to "Low" or "Unknown".

Return ONLY valid JSON, no other text:
{
  "hq_city": "",
  "hq_region": "",
  "hq_country": "",
  "confidence": "High|Medium|Low|Unknown",
  "reason": "",
  "evidence_url": "",
  "evidence_quote": "",
  "entity_match": "Exact|Likely|Weak|No",
  "is_group_or_parent_only": false
}
"""


def _model_extract(
    company_name: str,
    snippets: list[dict],
    anthropic_key: str,
    model: str = "claude-haiku-4-5-20251001",
) -> dict[str, Any]:
    try:
        import anthropic as _anthropic
    except ImportError:
        return {"probe_error": "anthropic package not installed (pip install anthropic)"}

    evidence = json.dumps(snippets[:5], ensure_ascii=False)
    client = _anthropic.Anthropic(api_key=anthropic_key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": f"Company: {company_name}\n\nSearch results:\n{evidence}"}],
            system=_MODEL_EXTRACTION_PROMPT,
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        parsed.setdefault("entity_match", "")
        parsed.setdefault("is_group_or_parent_only", False)
        return parsed
    except json.JSONDecodeError as exc:
        return {"probe_error": f"Model JSON parse error: {exc}"}
    except Exception as exc:
        return {"probe_error": f"Model error: {exc}"}

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(
    hq_country: str,
    input_country: str,
    confidence: str,
    reason: str,
    organic: list[dict],
) -> tuple[str, bool]:
    """Returns (foreign_hq_simple, needs_manual_review)."""
    if not hq_country or confidence in ("Unknown", ""):
        return "", True

    hq_std    = _std_country(hq_country)
    input_std = _std_country(input_country)
    foreign   = hq_std.lower() != input_std.lower()

    needs_review = confidence in ("Low", "Unknown")
    if not needs_review and organic:
        all_text = " ".join(
            f"{r.get('title','')} {r.get('snippet','')}" for r in organic[:3]
        ).lower()
        if sum(1 for alias in _COUNTRY_ALIASES if alias in all_text) > 2:
            needs_review = True

    return ("True" if foreign else "False"), needs_review

# ---------------------------------------------------------------------------
# Serper search
# ---------------------------------------------------------------------------

def _serper_search(query: str, api_key: str) -> tuple[dict, str]:
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        resp = _requests.post(
            _SERPER_URL, headers=headers,
            json={"q": query, "num": 5},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json(), ""
    except _requests.HTTPError as exc:
        return {}, f"HTTP {exc.response.status_code}: {exc.response.text[:120]}"
    except Exception as exc:
        return {}, str(exc)[:200]


def _build_queries(company_name: str, domain: str) -> list[str]:
    n = company_name.strip()
    d = (domain or "").strip().lstrip("https://").lstrip("http://").rstrip("/")
    queries = [
        f'"{n}" headquarters',
        f'"{n}" head office',
        f'"{n}" sede legale',
        f'"{n}" sede principale',
        f'"{n}" sede amministrativa',
    ]
    if d:
        queries += [f"site:{d} sede", f"site:{d} headquarters", f"site:{d} head office"]
    return queries

# ---------------------------------------------------------------------------
# Per-company probe
# ---------------------------------------------------------------------------

def probe_company(
    company_name: str,
    domain: str,
    input_country: str,
    serper_key: str,
    use_model: bool,
    anthropic_key: str,
    cache: dict,
) -> dict[str, Any]:
    """Run all queries for one company; return probe column dict."""
    result: dict[str, Any] = {col: "" for col in PROBE_COLS}

    if not company_name.strip():
        result["probe_error"] = "blank company name"
        return result

    queries    = _build_queries(company_name, domain)
    best: dict = {}
    used_query = ""
    all_organic: list[dict] = []
    kg_location = ""
    answer_box_text = ""
    errors: list[str] = []

    for query in queries:
        cache_key = ("serper", query)
        if cache_key in cache:
            data, err = cache[cache_key]
        else:
            data, err = _serper_search(query, serper_key)
            cache[cache_key] = (data, err)
            time.sleep(_INTER_REQUEST_SLEEP)

        if err:
            errors.append(f"{query!r}: {err}")
            continue

        organic = data.get("organic", [])

        if not kg_location:
            kg = data.get("knowledgeGraph", {})
            kg_location = (
                kg.get("address", "") or kg.get("headquarters", "") or kg.get("location", "")
            )
        if not answer_box_text:
            ab = data.get("answerBox", {})
            answer_box_text = ab.get("answer", "") or ab.get("snippet", "")
        if not all_organic and organic:
            all_organic = organic

        extracted = _extract_deterministic(
            organic, kg_location, answer_box_text, input_country=input_country
        )
        if extracted and extracted.get("hq_detected_country"):
            best = extracted
            used_query = query
            all_organic = organic
            break

    # Model fallback
    if not best.get("hq_detected_country") and use_model and anthropic_key:
        snippets = [
            {"title": r.get("title", ""), "snippet": r.get("snippet", ""), "url": r.get("link", "")}
            for r in all_organic[:5]
        ]
        model_result = _model_extract(company_name, snippets, anthropic_key)
        is_group_only = model_result.get("is_group_or_parent_only", False)
        entity_match  = model_result.get("entity_match", "")
        if model_result.get("hq_country") and not is_group_only and entity_match != "No":
            prefix = f"[model entity={entity_match}] " if entity_match else "[model] "
            best = {
                "hq_detected_city":    model_result.get("hq_city", ""),
                "hq_detected_region":  model_result.get("hq_region", ""),
                "hq_detected_country": model_result.get("hq_country", ""),
                "hq_confidence":       model_result.get("confidence", ""),
                "hq_reason":           prefix + model_result.get("reason", ""),
                "hq_evidence_url":     model_result.get("evidence_url", ""),
                "hq_evidence_quote":   model_result.get("evidence_quote", ""),
            }
            if not used_query and queries:
                used_query = queries[0]
        elif model_result.get("hq_country") and is_group_only:
            best = {
                "hq_detected_city":    model_result.get("hq_city", ""),
                "hq_detected_region":  model_result.get("hq_region", ""),
                "hq_detected_country": model_result.get("hq_country", ""),
                "hq_confidence":       "Low",
                "hq_reason":           "[model – group/parent only] " + model_result.get("reason", ""),
                "hq_evidence_url":     model_result.get("evidence_url", ""),
                "hq_evidence_quote":   model_result.get("evidence_quote", ""),
            }
            if not used_query and queries:
                used_query = queries[0]
        if model_result.get("probe_error"):
            errors.append(model_result["probe_error"])

    for k, v in best.items():
        if k in result:
            result[k] = v

    result["hq_query_used"]                    = used_query
    result["serper_knowledge_graph_location"]  = kg_location
    result["serper_answer_box"]                = answer_box_text

    for i, item in enumerate(all_organic[:3], start=1):
        result[f"top_organic_title_{i}"]   = item.get("title", "")
        result[f"top_organic_snippet_{i}"] = item.get("snippet", "")
        result[f"top_organic_url_{i}"]     = item.get("link", "")

    foreign_str, needs_review = _classify(
        result["hq_detected_country"],
        input_country,
        result["hq_confidence"],
        result["hq_reason"],
        all_organic,
    )
    result["input_country_used"] = _std_country(input_country)
    result["foreign_hq_simple"]  = foreign_str
    result["needs_manual_review"] = "Yes" if needs_review else "No"

    if errors:
        result["probe_error"] = "; ".join(errors)

    return result

# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def get_excel_sheet_names(fileobj: "io.IOBase | Path") -> list[str]:
    wb = load_workbook(fileobj, read_only=True, data_only=True)
    names = wb.sheetnames
    wb.close()
    return names


def read_input_from_fileobj(
    fileobj: "io.IOBase",
    suffix: str,
    limit: int = DEFAULT_LIMIT,
    sheet_name: "str | None" = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    suffix = suffix.lower()
    if suffix in (".xlsx", ".xls"):
        wb = load_workbook(fileobj, read_only=True, data_only=True)
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        iter_rows = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h is not None else "" for h in next(iter_rows)]
        for raw in iter_rows:
            if all(v is None or str(v).strip() == "" for v in raw):
                continue
            rows.append({headers[i]: raw[i] for i in range(len(headers))})
            if len(rows) >= limit:
                break
        wb.close()
    elif suffix == ".csv":
        text = fileobj.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            rows.append(dict(row))
            if len(rows) >= limit:
                break
    return rows

# ---------------------------------------------------------------------------
# Output builder
# ---------------------------------------------------------------------------

_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_PROBE_FILL  = PatternFill("solid", fgColor="E2EFDA")
_PROBE_FONT  = Font(bold=True)
_OLD_FILL    = PatternFill("solid", fgColor="D6E4F0")
_WARN_FILL   = PatternFill("solid", fgColor="FCE4D6")

_WRAP_COLS = {
    "hq_reason", "hq_evidence_quote",
    "top_organic_snippet_1", "top_organic_snippet_2", "top_organic_snippet_3",
    "sig_foreign_hq_evidence",
}

_COL_WIDTHS: dict[str, int] = {
    "source_row": 10, "company_name": 38, "domain": 28, "input_country": 14,
    "hq_detected_city": 18, "hq_detected_region": 18, "hq_detected_country": 18,
    "input_country_used": 18,
    "hq_confidence": 12, "foreign_hq_simple": 16, "needs_manual_review": 18,
    "hq_reason": 50, "hq_evidence_url": 45, "hq_evidence_quote": 60,
    "hq_query_used": 45, "serper_knowledge_graph_location": 35, "serper_answer_box": 45,
    "top_organic_title_1": 45, "top_organic_snippet_1": 60, "top_organic_url_1": 45,
    "top_organic_title_2": 45, "top_organic_snippet_2": 60, "top_organic_url_2": 45,
    "top_organic_title_3": 45, "top_organic_snippet_3": 60, "top_organic_url_3": 45,
    "probe_error": 40,
    "sig_foreign_hq_score": 20, "sig_foreign_hq_evidence": 55,
    "foreign_hq_sanitized": 18, "foreign_hq_sanitizer_reason": 35,
    "foreign_hq_original_score": 22, "final_commercial_fit_score": 24,
    "commercial_fit_score": 22,
}


def _build_workbook(
    input_rows: list[dict],
    probe_results: list[dict],
    present_old_cols: list[str],
    company_col: str,
    domain_col: str,
    country_col: str,
    qa_meta: dict,
    output_label: str = "",
) -> "Workbook":
    wb = Workbook()
    ws = wb.active
    ws.title = "HQ Probe Results"

    input_identity_cols = ["source_row", company_col, domain_col, country_col]
    seen: set[str] = set()
    all_cols: list[str] = []
    for c in input_identity_cols + present_old_cols + PROBE_COLS:
        if c not in seen:
            all_cols.append(c)
            seen.add(c)

    ws.append(all_cols)
    ws.row_dimensions[1].height = 18
    probe_col_set = set(PROBE_COLS)
    old_col_set   = set(present_old_cols)
    for col_idx, h in enumerate(all_cols, start=1):
        cell = ws.cell(row=1, column=col_idx)
        if h in probe_col_set:
            cell.fill = _PROBE_FILL; cell.font = _PROBE_FONT
        elif h in old_col_set:
            cell.fill = _OLD_FILL; cell.font = Font(bold=True)
        else:
            cell.fill = _HEADER_FILL; cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", wrap_text=False)
        ws.column_dimensions[get_column_letter(col_idx)].width = (
            _COL_WIDTHS.get(h, max(14, min(len(h) + 4, 35)))
        )

    for row_idx, (in_row, probe) in enumerate(zip(input_rows, probe_results), start=2):
        values: list[Any] = []
        for h in all_cols:
            if h == "source_row":
                values.append(row_idx - 1)
            elif h in probe:
                values.append(probe[h])
            else:
                v = in_row.get(h)
                values.append(v if v is not None else "")
        ws.append(values)
        ws.row_dimensions[row_idx].height = 15
        if probe.get("needs_manual_review") == "Yes":
            for col_idx in range(1, len(all_cols) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = _WARN_FILL
        for col_idx, h in enumerate(all_cols, start=1):
            if h in _WRAP_COLS:
                ws.cell(row=row_idx, column=col_idx).alignment = Alignment(wrap_text=True)

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions

    # QA sheet
    ws_qa = wb.create_sheet("Run QA")
    detected_italy   = sum(1 for p in probe_results if _std_country(p.get("hq_detected_country") or "") == "Italy")
    detected_foreign = sum(1 for p in probe_results if p.get("foreign_hq_simple") == "True")
    detected_unknown = sum(1 for p in probe_results if not p.get("hq_detected_country"))
    needs_review_cnt = sum(1 for p in probe_results if p.get("needs_manual_review") == "Yes")
    has_errors       = sum(1 for p in probe_results if p.get("probe_error"))

    qa_rows_data = [
        ("Run QA – hq_lookup_probe_app.py", ""),
        ("", ""),
        ("timestamp",           qa_meta.get("timestamp", "")),
        ("input_file",          qa_meta.get("input_file", "")),
        ("output_file",         output_label or qa_meta.get("output_file", "")),
        ("company_col",         company_col),
        ("domain_col",          domain_col),
        ("country_col",         country_col or "(default)"),
        ("rows_processed",      len(probe_results)),
        ("", ""),
        ("── detection summary ──", ""),
        ("detected_italy_hq",   detected_italy),
        ("detected_foreign_hq", detected_foreign),
        ("detected_unknown",    detected_unknown),
        ("needs_manual_review", needs_review_cnt),
        ("rows_with_errors",    has_errors),
        ("", ""),
        ("── settings ──", ""),
        ("model_extraction_used", qa_meta.get("use_model", False)),
        ("model_used",          qa_meta.get("model", "")),
        ("serper_available",    qa_meta.get("serper_available", False)),
        ("limit",               qa_meta.get("limit", "")),
    ]
    ws_qa.column_dimensions["A"].width = 30
    ws_qa.column_dimensions["B"].width = 80
    for r_idx, (k, v) in enumerate(qa_rows_data, start=1):
        cell_a = ws_qa.cell(row=r_idx, column=1, value=k)
        ws_qa.cell(row=r_idx, column=2, value=v)
        if r_idx == 1:
            cell_a.font = Font(bold=True, size=13)
        elif k and not k.startswith(" "):
            cell_a.font = Font(bold=True)
        ws_qa.row_dimensions[r_idx].height = 15

    return wb


def build_excel_bytes(
    input_rows: list[dict],
    probe_results: list[dict],
    present_old_cols: list[str],
    company_col: str,
    domain_col: str,
    country_col: str,
    qa_meta: dict,
) -> bytes:
    wb = _build_workbook(
        input_rows=input_rows,
        probe_results=probe_results,
        present_old_cols=present_old_cols,
        company_col=company_col,
        domain_col=domain_col,
        country_col=country_col,
        qa_meta=qa_meta,
        output_label="(in-memory)",
    )
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Streamlit app
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="mYngle HQ Lookup Probe",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 mYngle HQ Lookup Probe")
st.caption(
    "This tool tests a simple headquarters lookup approach. "
    "It is **experimental** and does not change production enrichment output."
)

# ---------------------------------------------------------------------------
# Column guessers
# ---------------------------------------------------------------------------

_COMPANY_GUESSES = ["company_name", "company", "naam", "azienda", "name"]
_DOMAIN_GUESSES  = ["domain", "website", "url", "website_url", "domein"]
_COUNTRY_GUESSES = ["inferred_input_country", "input_country", "country", "paese", "land"]

# Columns that must never be auto-selected as the country column
_NOT_COUNTRY_COLS = {
    "company_name", "company", "naam", "azienda", "name",
    "domain", "website", "url", "website_url", "domein",
}


def _guess_col(columns: list[str], guesses: list[str]) -> str:
    """Return best matching column from guesses, or first column as fallback."""
    cols_lower = {c.lower(): c for c in columns}
    for g in guesses:
        if g.lower() in cols_lower:
            return cols_lower[g.lower()]
    return columns[0] if columns else ""


def _guess_country_col(columns: list[str]) -> str:
    """Return best country column, or '' if none found (caller defaults to '(use default)')."""
    cols_lower = {c.lower(): c for c in columns}
    for g in _COUNTRY_GUESSES:
        match = cols_lower.get(g.lower())
        if match and match.lower() not in _NOT_COUNTRY_COLS:
            return match
    return ""  # → sidebar will show "(use default)"

# ---------------------------------------------------------------------------
# Session-state keys
# ---------------------------------------------------------------------------

_KEY_RESULTS    = "hq_probe_results"
_KEY_INPUT_ROWS = "hq_probe_input_rows"
_KEY_OLD_COLS   = "hq_probe_old_cols"
_KEY_META       = "hq_probe_meta"
_KEY_COLS_CFG   = "hq_probe_cols_cfg"

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Input")

    upload_tab, path_tab = st.tabs(["Upload file", "Local path"])

    uploaded_file  = None
    local_path_str = ""

    with upload_tab:
        uploaded_file = st.file_uploader(
            "Excel (.xlsx) or CSV",
            type=["xlsx", "csv"],
            help="Upload a file with company names and domains.",
        )

    with path_tab:
        local_path_str = st.text_input(
            "Path on disk",
            placeholder=r"C:\Users\...\input.xlsx",
            help="Use this for large files already on your machine.",
        )

    # Resolve file source
    file_source: str | None = None
    file_suffix  = ""
    file_label   = ""
    sheet_names: list[str] = []
    selected_sheet: str | None = None

    if uploaded_file is not None:
        file_suffix = Path(uploaded_file.name).suffix
        file_label  = uploaded_file.name
        if file_suffix.lower() in (".xlsx", ".xls"):
            try:
                sheet_names = get_excel_sheet_names(uploaded_file)
                uploaded_file.seek(0)
            except Exception:
                sheet_names = []
        file_source = "upload"

    elif local_path_str.strip():
        p = Path(local_path_str.strip())
        if p.exists():
            file_suffix = p.suffix
            file_label  = p.name
            if file_suffix.lower() in (".xlsx", ".xls"):
                try:
                    sheet_names = get_excel_sheet_names(p)
                except Exception:
                    sheet_names = []
            file_source = "path"
        else:
            st.warning(f"Path not found: {p}")

    if sheet_names and len(sheet_names) > 1:
        selected_sheet = st.selectbox("Sheet", sheet_names)
    elif sheet_names:
        selected_sheet = sheet_names[0]

    st.header("Columns")

    peeked_headers: list[str] = []
    peek_error = ""

    if file_source == "upload" and uploaded_file is not None:
        try:
            uploaded_file.seek(0)
            preview_rows = read_input_from_fileobj(
                uploaded_file, file_suffix, limit=1, sheet_name=selected_sheet
            )
            peeked_headers = list(preview_rows[0].keys()) if preview_rows else []
            uploaded_file.seek(0)
        except Exception as exc:
            peek_error = str(exc)

    elif file_source == "path":
        try:
            with open(local_path_str.strip(), "rb") as f:
                preview_rows = read_input_from_fileobj(
                    f, file_suffix, limit=1, sheet_name=selected_sheet
                )
            peeked_headers = list(preview_rows[0].keys()) if preview_rows else []
        except Exception as exc:
            peek_error = str(exc)

    if peek_error:
        st.warning(f"Could not read headers: {peek_error}")

    if peeked_headers:
        company_col = st.selectbox(
            "Company column",
            peeked_headers,
            index=peeked_headers.index(_guess_col(peeked_headers, _COMPANY_GUESSES)),
        )
        domain_col = st.selectbox(
            "Domain column",
            peeked_headers,
            index=peeked_headers.index(_guess_col(peeked_headers, _DOMAIN_GUESSES)),
        )
        # Country column: "(use default)" is always the safe first option
        country_opts = ["(use default)"] + peeked_headers
        country_guess = _guess_country_col(peeked_headers)
        country_guess_idx = (
            country_opts.index(country_guess)
            if country_guess and country_guess in country_opts
            else 0  # default to "(use default)" when no country column found
        )
        country_col_sel = st.selectbox(
            "Input country column (optional)",
            country_opts,
            index=country_guess_idx,
            help="Select the column with ISO country or country name. "
                 "If not in file, choose '(use default)' and set the fallback below.",
        )
        # Use "" as sentinel for "no column" so run logic uses default_country
        country_col = country_col_sel if country_col_sel != "(use default)" else ""
    else:
        company_col = st.text_input("Company column", DEFAULT_COMPANY_COL)
        domain_col  = st.text_input("Domain column",  DEFAULT_DOMAIN_COL)
        country_col = ""  # no file loaded yet, use default

    default_country = st.text_input(
        "Default country (fallback)",
        DEFAULT_INPUT_COUNTRY,
        help="Used for every row when no country column is selected or when the column is blank.",
    )

    st.header("Options")

    limit = st.number_input("Row limit", min_value=1, max_value=200, value=50, step=10)

    only_fhq_signal = st.checkbox(
        "Only rows with old foreign HQ signal",
        value=False,
        help="Filter input to rows where sig_foreign_hq_score > 0 or foreign_hq_sanitized = True/Yes.",
    )

    st.header("API keys")

    serper_key = st.text_input(
        "Serper API key",
        value=os.environ.get("SERPER_API_KEY", "") or os.environ.get("SERPER_KEY", ""),
        type="password",
        help="Required for live searches. Get one at serper.dev.",
    )

    use_model = st.checkbox(
        "Use model fallback for unresolved cases",
        value=False,
        help="Calls Claude Haiku for companies where pattern matching finds nothing.",
    )

    anthropic_key = ""
    if use_model:
        anthropic_key = st.text_input(
            "Anthropic API key",
            value=os.environ.get("ANTHROPIC_API_KEY", ""),
            type="password",
        )

# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------

if not file_source:
    st.info("Upload a file or enter a local path in the sidebar to get started.")
    st.stop()

st.markdown(
    "⚠️ **Each row may use up to 8 Serper search calls.** "
    f"With limit={int(limit)}, that is up to **{int(limit) * 8:,} calls**."
)

run_btn = st.button("▶ Run HQ Probe", type="primary", disabled=(not serper_key))
if not serper_key:
    st.warning("Enter a Serper API key in the sidebar to enable the run button.")

if run_btn:
    # Load input rows
    with st.spinner("Reading input file…"):
        try:
            if file_source == "upload":
                uploaded_file.seek(0)
                input_rows = read_input_from_fileobj(
                    uploaded_file, file_suffix, limit=int(limit), sheet_name=selected_sheet,
                )
            else:
                with open(local_path_str.strip(), "rb") as f:
                    input_rows = read_input_from_fileobj(
                        f, file_suffix, limit=int(limit), sheet_name=selected_sheet,
                    )
        except Exception as exc:
            st.error(f"Failed to read input: {exc}")
            st.stop()

    if not input_rows:
        st.warning("No rows found in the input file.")
        st.stop()

    # Old-FHQ filter
    if only_fhq_signal:
        def _has_fhq_signal(row: dict) -> bool:
            score = row.get("sig_foreign_hq_score")
            sanitized = str(row.get("foreign_hq_sanitized") or "").strip().lower()
            try:
                score_val = float(score)
            except (TypeError, ValueError):
                score_val = 0.0
            return score_val > 0 or sanitized in {"true", "yes", "1"}

        filtered = [r for r in input_rows if _has_fhq_signal(r)]
        if filtered:
            st.info(f"Old FHQ filter: {len(filtered)} / {len(input_rows)} rows have a signal.")
            input_rows = filtered
        else:
            st.warning("No rows matched the old FHQ signal filter. Running on all rows.")

    # Detect old enrichment cols
    sample_keys = set(input_rows[0].keys()) if input_rows else set()
    present_old_cols = [c for c in OLD_ENRICHMENT_COLS if c in sample_keys]

    # Run probe
    progress_bar = st.progress(0.0, text="Starting…")
    probe_results: list[dict] = []
    total = len(input_rows)
    cache: dict = {}
    error_rows: list[str] = []

    for i, row in enumerate(input_rows):
        company = str(row.get(company_col) or "").strip()
        domain  = str(row.get(domain_col)  or "").strip()
        # Country: use column if selected and non-blank, else default
        if country_col:
            country = str(row.get(country_col) or "").strip() or default_country
        else:
            country = default_country

        probe = probe_company(
            company_name=company,
            domain=domain,
            input_country=country,
            serper_key=serper_key,
            use_model=use_model,
            anthropic_key=anthropic_key,
            cache=cache,
        )

        # Sanity guard: if detected country == input country, force foreign_hq_simple = False
        det = _std_country(probe.get("hq_detected_country") or "")
        inp = _std_country(probe.get("input_country_used") or "")
        if det and inp and det.lower() == inp.lower():
            probe["foreign_hq_simple"] = "False"

        probe_results.append(probe)
        if probe.get("probe_error"):
            error_rows.append(f"Row {i+1} ({company}): {probe['probe_error']}")

        pct = (i + 1) / total
        country_hit = probe.get("hq_detected_country") or "…"
        progress_bar.progress(pct, text=f"[{i+1}/{total}] {company[:45]} → {country_hit}")

    progress_bar.empty()

    # Store in session state
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.session_state[_KEY_RESULTS]    = probe_results
    st.session_state[_KEY_INPUT_ROWS] = input_rows
    st.session_state[_KEY_OLD_COLS]   = present_old_cols
    st.session_state[_KEY_COLS_CFG]   = (company_col, domain_col, country_col or "(default)")
    st.session_state[_KEY_META] = {
        "timestamp":        ts,
        "input_file":       file_label,
        "use_model":        use_model,
        "model":            "claude-haiku-4-5-20251001" if use_model else "",
        "serper_available": bool(serper_key),
        "limit":            int(limit),
    }

    if error_rows:
        with st.expander(f"⚠️ {len(error_rows)} row error(s)", expanded=False):
            for e in error_rows:
                st.text(e)

# ---------------------------------------------------------------------------
# Show results (persists across reruns via session state)
# ---------------------------------------------------------------------------

if _KEY_RESULTS not in st.session_state:
    st.stop()

probe_results: list[dict]   = st.session_state[_KEY_RESULTS]
input_rows: list[dict]      = st.session_state[_KEY_INPUT_ROWS]
present_old_cols: list[str] = st.session_state[_KEY_OLD_COLS]
company_col_r, domain_col_r, country_col_r = st.session_state[_KEY_COLS_CFG]
qa_meta: dict               = st.session_state[_KEY_META]

# Summary metrics
detected_italy   = sum(1 for p in probe_results if _std_country(p.get("hq_detected_country") or "") == "Italy")
detected_foreign = sum(1 for p in probe_results if p.get("foreign_hq_simple") == "True")
detected_unknown = sum(1 for p in probe_results if not p.get("hq_detected_country"))
needs_review_cnt = sum(1 for p in probe_results if p.get("needs_manual_review") == "Yes")

st.markdown("---")
st.subheader("Results")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Rows processed",    len(probe_results))
c2.metric("Italy HQ detected", detected_italy)
c3.metric("Foreign HQ",        detected_foreign)
c4.metric("Unknown",           detected_unknown)
c5.metric("Needs review",      needs_review_cnt)

# Display columns: input_country_used appears between hq_detected_country and foreign_hq_simple
_KEY_VIEW_COLS_RAW = [
    company_col_r, domain_col_r, country_col_r,
    "sig_foreign_hq_score", "sig_foreign_hq_evidence",
    "foreign_hq_sanitized", "foreign_hq_sanitizer_reason",
    "hq_detected_city", "hq_detected_region", "hq_detected_country",
    "input_country_used",
    "hq_confidence", "foreign_hq_simple", "needs_manual_review",
    "hq_reason", "hq_evidence_url", "hq_evidence_quote", "hq_query_used",
]
_seen_kvc: set[str] = set()
_KEY_VIEW_COLS: list[str] = []
for _c in _KEY_VIEW_COLS_RAW:
    if _c and _c not in _seen_kvc:
        _KEY_VIEW_COLS.append(_c)
        _seen_kvc.add(_c)


def _build_display_rows(
    input_rows: list[dict],
    probe_results: list[dict],
    cols: list[str],
) -> list[dict]:
    rows = []
    for i, (in_row, probe) in enumerate(zip(input_rows, probe_results), start=1):
        r: dict[str, Any] = {"#": i}
        for c in cols:
            r[c] = probe[c] if c in probe else in_row.get(c, "")
        rows.append(r)
    return rows


# Filters
st.markdown("**Filters**")
f1, f2, f3, f4 = st.columns(4)
f_review   = f1.checkbox("Needs manual review only")
f_foreign  = f2.checkbox("Foreign HQ only (new)")
f_unknown  = f3.checkbox("Unknown only")
f_disagree = f4.checkbox("Old/new disagreement")

all_display = _build_display_rows(input_rows, probe_results, _KEY_VIEW_COLS)


def _apply_filters(rows: list[dict]) -> list[dict]:
    out = rows
    if f_review:
        out = [r for r in out if r.get("needs_manual_review") == "Yes"]
    if f_foreign:
        out = [r for r in out if r.get("foreign_hq_simple") == "True"]
    if f_unknown:
        out = [r for r in out if not r.get("hq_detected_country")]
    if f_disagree:
        def _disagree(r: dict) -> bool:
            try:
                old_score = float(r.get("sig_foreign_hq_score") or 0)
            except (ValueError, TypeError):
                old_score = 0.0
            return old_score > 0 and r.get("foreign_hq_simple", "") in ("False", "")
        out = [r for r in out if _disagree(r)]
    return out


filtered_display = _apply_filters(all_display)
st.caption(f"Showing {len(filtered_display)} of {len(all_display)} rows")

# Only include columns that have data; deduplicate
_pvc_seen: set[str] = {"#"}
present_view_cols = ["#"]
for _c in _KEY_VIEW_COLS:
    if _c not in _pvc_seen and any(str(r.get(_c, "")).strip() for r in filtered_display):
        present_view_cols.append(_c)
        _pvc_seen.add(_c)

try:
    import pandas as pd
    df = pd.DataFrame(filtered_display)[present_view_cols]
    st.dataframe(df, use_container_width=True, height=420)
except ImportError:
    st.table(filtered_display)

# Downloads
st.markdown("---")
st.subheader("Download results")

dl1, dl2 = st.columns(2)

with dl1:
    @st.cache_data(show_spinner=False)
    def _make_excel(_input_key: int, _probe_key: int) -> bytes:
        return build_excel_bytes(
            input_rows=input_rows,
            probe_results=probe_results,
            present_old_cols=present_old_cols,
            company_col=company_col_r,
            domain_col=domain_col_r,
            country_col=country_col_r,
            qa_meta=qa_meta,
        )

    ts = qa_meta.get("timestamp", "")
    dl1.download_button(
        label="⬇️ Download Excel",
        data=_make_excel(id(input_rows), id(probe_results)),
        file_name=f"hq_probe_results_{ts}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

with dl2:
    def _make_csv() -> bytes:
        if not all_display:
            return b""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(all_display[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_display)
        return buf.getvalue().encode("utf-8-sig")

    dl2.download_button(
        label="⬇️ Download CSV",
        data=_make_csv(),
        file_name=f"hq_probe_results_{ts}.csv",
        mime="text/csv",
    )
