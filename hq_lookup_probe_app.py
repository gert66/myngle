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
    # Recovery review columns
    "has_multilingual_site",
    "website_language_count",
    "website_languages_detected",
    "has_global_structure_signal",
    "global_structure_evidence",
    "hq_review_trigger",
    "hq_structure_type",
    "local_entity_hq_country",
    "local_entity_hq_city",
    "parent_group_hq_country",
    "parent_group_hq_city",
    "review_foreign_parent_score",
    "review_global_network_score",
    "review_multilingual_website_score",
    "review_recommended_hq_signal",
    "sig_foreign_hq_score_original",
    "sig_foreign_hq_score_reviewed",
    "sig_foreign_hq_review_reason",
    "sig_foreign_hq_review_confidence",
    "sig_foreign_hq_review_source",
    "sig_foreign_hq_review_evidence_url",
    "sig_foreign_hq_review_evidence_quote",
    "needs_anthropic_hq_review",
    "anthropic_hq_review_used",
    "anthropic_web_search_used",
    "anthropic_review_evidence_mode",
    "anthropic_web_search_queries",
    "anthropic_web_search_result_count",
    "anthropic_error",
    # Serper usage audit
    "serper_calls_used",
    "serper_queries_used",
    "serper_cache_hit",
    # Website fetch
    "website_fetch_count",
    # Manual Google mimic HQ check
    "manual_google_mimic_used",
    "plain_company_search_query",
    "plain_company_top_result_url",
    "plain_company_top_result_title",
    "plain_company_top_result_snippet",
    "plain_company_official_domain",
    "plain_company_official_result_rank",
    "official_domain_from_plain_search",
    "official_domain_matches_input_domain",
    "official_page_fetch_used",
    "official_page_fetch_url",
    "official_page_fetch_count",
    "official_page_fetch_error",
    "official_page_hq_evidence_found",
    "official_page_hq_evidence_quote",
    "official_page_hq_country",
    "official_page_hq_city",
    "official_page_hq_evidence_strength",
    "brand_hq_search_used",
    "brand_hq_search_queries",
    "brand_hq_top_result_url",
    "brand_hq_top_result_snippet",
    "brand_hq_evidence_found",
    "brand_hq_evidence_quote",
    "brand_hq_top_result_domain",
    "brand_hq_top_result_is_official_domain",
    "brand_hq_result_selection_reason",
    # Performance / run metadata
    "run_mode",
    "max_serper_calls_per_row",
    "early_stop_used",
    "early_stop_reason",
    "row_runtime_seconds",
]

# Token columns tracked internally but NOT exported to Excel/CSV visible columns.
# They are stored per-row only for the Run Summary sheet aggregation.
_INTERNAL_TOKEN_COLS = [
    "anthropic_model_used",
    "anthropic_input_tokens",
    "anthropic_output_tokens",
    "anthropic_total_tokens",
    "anthropic_stop_reason",
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
# Global structure / multilingual detection constants
# ---------------------------------------------------------------------------

_GLOBAL_STRUCTURE_TERMS = re.compile(
    r"\b(?:"
    # Strong parent/ownership signals
    r"parent\s+company|owned\s+by|controlled\s+by|part\s+of\s+\w"
    r"|member\s+firm|member\s+of\s+\w"
    r"|global\s+network|international\s+network"
    r"|subsidiar(?:y|ies)"
    r"|offices\s+worldwide|worldwide\s+presence|global\s+offices"
    r"|group\s+headquarters|group\s+hq\b"
    r"|belongs?\s+to\s+\w"
    # HQ in foreign location (must be followed by location, checked contextually)
    r"|head\s+office\s+in\s+[A-Z]|headquartered\s+in\s+[A-Z]"
    r"|corporate\s+headquarters\s+in\s+[A-Z]|hq\s+in\s+[A-Z]"
    r")\b",
    re.IGNORECASE,
)

_LANG_HREFLANG_RE = re.compile(r'hreflang=["\']([a-z]{2})(?:-[A-Za-z]{2,4})?["\']', re.IGNORECASE)
_LANG_URL_RE = re.compile(r'href=["\'][^"\']{0,200}/([a-z]{2})/', re.IGNORECASE)
_LANG_LABELS: dict[str, str] = {
    "english": "en", "italiano": "it", "deutsch": "de", "français": "fr",
    "español": "es", "português": "pt", "svenska": "sv", "русский": "ru",
    "中文": "zh", "한국인": "ko", "dutch": "nl", "polski": "pl",
    "română": "ro", "česky": "cs",
}
_LANG_KNOWN = {
    "en", "it", "de", "fr", "es", "pt", "sv", "ru", "zh", "ko",
    "nl", "pl", "ja", "da", "fi", "no", "cs", "ro", "tr", "ar", "hu", "el",
}
_FETCH_TIMEOUT = 8

_HIGH_INTL_COLS = [
    "sig_intl_footprint_score", "sig_multicultural_score",
    "ti_intercultural_score", "sig_lnd_onboarding_score",
    "ti_onboarding_score", "model_signal_overall_confidence_score",
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


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def detect_multilingual_site(domain: str) -> dict[str, Any]:
    """Cheap homepage fetch to detect multilingual website. Cached by caller."""
    out: dict[str, Any] = {
        "has_multilingual_site": False, "website_language_count": 0,
        "website_languages_detected": "", "website_fetch_count": 0,
    }
    if not domain:
        return out
    domain = domain.strip().lstrip("https://").lstrip("http://").rstrip("/")
    html = ""
    for scheme in ("https", "http"):
        out["website_fetch_count"] += 1
        try:
            resp = _requests.get(
                f"{scheme}://{domain}", timeout=_FETCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; HQProbe/1.0)"},
                allow_redirects=True,
            )
            html = resp.text[:80_000]
            break
        except Exception:
            continue
    if not html:
        return out

    langs: set[str] = set()
    for m in _LANG_HREFLANG_RE.finditer(html):
        lang = m.group(1).lower()
        if lang in _LANG_KNOWN:
            langs.add(lang)
    for m in _LANG_URL_RE.finditer(html):
        lang = m.group(1).lower()
        if lang in _LANG_KNOWN:
            langs.add(lang)
    html_lc = html.lower()
    for label, code in _LANG_LABELS.items():
        if label in html_lc:
            langs.add(code)

    count = len(langs)
    out["website_language_count"] = count
    out["website_languages_detected"] = ", ".join(sorted(langs))
    out["has_multilingual_site"] = count >= 2
    return out


def detect_global_structure(
    organic: list[dict],
    kg_location: str,
    answer_box: str,
    page_text: str = "",
) -> dict[str, Any]:
    """Scan Serper evidence for global/group/network structure signals."""
    out: dict[str, Any] = {"has_global_structure_signal": False, "global_structure_evidence": ""}
    texts: list[str] = []
    if kg_location:
        texts.append(kg_location)
    if answer_box:
        texts.append(answer_box)
    for item in organic[:5]:
        texts.append(f"{item.get('title', '')} {item.get('snippet', '')}")
    if page_text:
        texts.append(page_text[:3_000])

    for text in texts:
        m = _GLOBAL_STRUCTURE_TERMS.search(text)
        if m:
            start = max(0, m.start() - 60)
            end   = min(len(text), m.end() + 60)
            out["has_global_structure_signal"] = True
            out["global_structure_evidence"]   = text[start:end].strip()
            return out
    return out


# Strong HQ-related patterns that point to a specific foreign location
_FOREIGN_HQ_NEAR_RE = re.compile(
    r"\b(?:head\s+office\s+in|headquartered\s+in|headquarters\s+(?:is\s+)?in"
    r"|corporate\s+(?:hq|headquarters)\s+in|hq\s+in|main\s+office\s+in"
    r"|parent\s+company\s+(?:is\s+)?(?:in|based|located)"
    r"|owned\s+by|controlled\s+by|subsidiary\s+of|part\s+of\s+the\s+\w"
    r")\b",
    re.IGNORECASE,
)

# Short all-caps company name risk (e.g. IET, BEA)
_SHORT_ACRONYM_RE = re.compile(r"^[A-Z]{2,4}$")


def compute_review_trigger(
    probe: dict,
    input_row: dict,
    multilingual: dict,
    global_struct: dict,
    company_name: str = "",
) -> tuple[str, bool]:
    """Returns (hq_review_trigger, needs_anthropic_hq_review).

    Only returns True when at least one *strong* condition is met.
    Generic profile labels (Headquarters: Milan, Locations, Founded) do not qualify.
    """
    triggers: list[str] = []

    det_country  = _std_country(probe.get("hq_detected_country") or "")
    inp_country  = _std_country(probe.get("input_country_used") or "")
    confidence   = probe.get("hq_confidence", "")
    lang_count   = int(multilingual.get("website_language_count") or 0)

    # 1. Strong parent/group/network signal from detect_global_structure
    if global_struct.get("has_global_structure_signal"):
        triggers.append("strong_parent_group_signal")

    # 2. Foreign country explicitly mentioned near real HQ-intent phrases in organic snippets
    all_snip = " ".join(
        f"{probe.get(f'top_organic_title_{i}', '')} {probe.get(f'top_organic_snippet_{i}', '')}"
        for i in range(1, 4)
    ).lower()
    if _FOREIGN_HQ_NEAR_RE.search(all_snip):
        for alias, country in _COUNTRY_ALIASES.items():
            if alias in all_snip and _std_country(country).lower() != inp_country.lower():
                triggers.append("conflicting_hq_evidence")
                break

    # 3. HQ detected as foreign (non-input country) with any confidence
    if det_country and inp_country and det_country.lower() != inp_country.lower():
        triggers.append("conflicting_hq_evidence")

    # Deduplicate conflicting_hq_evidence if added twice
    triggers = list(dict.fromkeys(triggers))

    # 4. Multilingual + high international signals (language count >= 2 required,
    #    but alone is NOT enough — need additional international signal from input row)
    has_high_intl = any(
        _safe_float(input_row.get(col)) > 0 for col in _HIGH_INTL_COLS if col in input_row
    )
    if lang_count >= 4 and has_high_intl:
        triggers.append("multilingual_plus_high_international_signals")
    elif lang_count >= 2 and has_high_intl and global_struct.get("has_global_structure_signal"):
        triggers.append("multilingual_plus_high_international_signals")

    # 5. Low/medium confidence + global/parent/multilingual signals present
    if confidence in ("Medium", "Low", ""):
        has_any_signal = (
            global_struct.get("has_global_structure_signal")
            or lang_count >= 3
            or bool(triggers)
        )
        if has_any_signal and has_high_intl:
            triggers.append("medium_confidence_plus_global_signal")

    # 6. Old score = 0/blank but multiple high international signals
    try:
        old_score = float(input_row.get("sig_foreign_hq_score") or 0)
    except (TypeError, ValueError):
        old_score = 0.0
    high_intl_count = sum(
        1 for col in _HIGH_INTL_COLS
        if col in input_row and _safe_float(input_row.get(col)) > 0
    )
    if old_score == 0 and high_intl_count >= 2:
        triggers.append("zero_fhq_score_multiple_intl_signals")

    # 7. Short acronym with ambiguous/low evidence
    name = (company_name or "").strip()
    if _SHORT_ACRONYM_RE.match(name) and confidence in ("Low", ""):
        triggers.append("short_acronym_ambiguous_evidence")

    # Deduplicate
    seen_t: set[str] = set()
    triggers = [t for t in triggers if not (t in seen_t or seen_t.add(t))]  # type: ignore[func-returns-value]

    return "; ".join(triggers), bool(triggers)


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
        parsed["_usage"] = {
            "model": resp.model,
            "input_tokens":  getattr(resp.usage, "input_tokens",  0),
            "output_tokens": getattr(resp.usage, "output_tokens", 0),
            "stop_reason":   getattr(resp, "stop_reason", ""),
        }
        parsed["_usage"]["total_tokens"] = (
            parsed["_usage"]["input_tokens"] + parsed["_usage"]["output_tokens"]
        )
        return parsed
    except json.JSONDecodeError as exc:
        return {"probe_error": f"Model JSON parse error: {exc}"}
    except Exception as exc:
        return {"probe_error": f"Model error: {exc}"}

# ---------------------------------------------------------------------------
# Anthropic HQ structure adjudication (optional, for ambiguous cases)
# ---------------------------------------------------------------------------

_ANTHROPIC_HQ_REVIEW_PROMPT = """\
You are an expert HQ analyst reviewing company headquarters structure.

Based on the provided evidence classify the company's HQ structure:
- "domestic_italy": Company's own HQ is in Italy with no clear foreign parent or global network.
- "foreign_parent": Company has a non-Italian parent, or its own registered HQ is outside Italy.
- "global_network": Company belongs to a global professional network (e.g. KPMG, PwC networks) — \
local entity may be Italian but the brand/network is worldwide.
- "exporter_multilingual": Italian HQ, multilingual website or international activity, \
but no clear foreign parent or global network structure.
- "unclear": Evidence is ambiguous or insufficient.

Key rules:
- Multilingual website alone does NOT make "foreign_parent".
- A local entity in a professional services network should be "global_network", not "foreign_parent".
- If clear foreign registered office or foreign parent company → "foreign_parent".

Scoring 0–3:
- review_foreign_parent_score: 3 = clear foreign parent/HQ, 0 = no evidence
- review_global_network_score: 3 = clear global network, 0 = no evidence
- review_multilingual_website_score: 3 = clearly multilingual, 0 = Italian-only
- review_recommended_hq_signal: overall signal strength recommendation
- sig_foreign_hq_score_reviewed: 3 = clear foreign/global network; 1 = exporter/multilingual only; 0 = domestic Italy

Return ONLY valid JSON:
{
  "hq_structure_type": "domestic_italy|foreign_parent|global_network|exporter_multilingual|unclear",
  "local_entity_hq_country": "",
  "local_entity_hq_city": "",
  "parent_group_hq_country": "",
  "parent_group_hq_city": "",
  "review_foreign_parent_score": 0,
  "review_global_network_score": 0,
  "review_multilingual_website_score": 0,
  "review_recommended_hq_signal": 0,
  "sig_foreign_hq_score_reviewed": 0,
  "confidence": "High|Medium|Low|Unknown",
  "reason": "",
  "evidence_url": "",
  "evidence_quote": ""
}
"""


def _anthropic_hq_adjudicate(
    company_name: str,
    domain: str,
    probe: dict,
    multilingual: dict,
    global_struct: dict,
    anthropic_key: str,
    model: str = "claude-haiku-4-5-20251001",
) -> dict[str, Any]:
    try:
        import anthropic as _anthropic
    except ImportError:
        return {"probe_error": "anthropic package not installed (pip install anthropic)"}

    evidence = {
        "company": company_name,
        "domain": domain,
        "hq_detected_country": probe.get("hq_detected_country", ""),
        "hq_detected_city": probe.get("hq_detected_city", ""),
        "hq_confidence": probe.get("hq_confidence", ""),
        "hq_reason": probe.get("hq_reason", ""),
        "has_multilingual_site": multilingual.get("has_multilingual_site", False),
        "website_languages_detected": multilingual.get("website_languages_detected", ""),
        "has_global_structure_signal": global_struct.get("has_global_structure_signal", False),
        "global_structure_evidence": global_struct.get("global_structure_evidence", ""),
        "serper_snippets": [
            {
                "title":   probe.get(f"top_organic_title_{i}", ""),
                "snippet": probe.get(f"top_organic_snippet_{i}", ""),
                "url":     probe.get(f"top_organic_url_{i}", ""),
            }
            for i in range(1, 4)
            if probe.get(f"top_organic_snippet_{i}")
        ],
    }

    client = _anthropic.Anthropic(api_key=anthropic_key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
            system=_ANTHROPIC_HQ_REVIEW_PROMPT,
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        parsed["_usage"] = {
            "model":         resp.model,
            "input_tokens":  getattr(resp.usage, "input_tokens",  0),
            "output_tokens": getattr(resp.usage, "output_tokens", 0),
            "stop_reason":   getattr(resp, "stop_reason", ""),
        }
        parsed["_usage"]["total_tokens"] = (
            parsed["_usage"]["input_tokens"] + parsed["_usage"]["output_tokens"]
        )
        return parsed
    except json.JSONDecodeError as exc:
        return {"probe_error": f"Anthropic HQ review JSON parse error: {exc}"}
    except Exception as exc:
        return {"probe_error": f"Anthropic HQ review error: {exc}"}

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


# ---------------------------------------------------------------------------
# Manual Google Mimic HQ Check
# ---------------------------------------------------------------------------

_DIRECTORY_DOMAINS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "crunchbase.com", "bloomberg.com",
    "zoominfo.com", "dnb.com", "hoovers.com", "glassdoor.com", "indeed.com",
    "infobel.com", "paginegialle.it", "kompass.com", "europages.com",
    "europages.it", "registroimprese.it", "registro.imprese.it",
    "atoka.io", "cerved.com", "ilsole24ore.com", "corriere.it",
    "bizjournals.com", "wsj.com", "ft.com", "reuters.com",
    "yelp.com", "foursquare.com", "angellist.com", "pitchbook.com",
    "owler.com", "manta.com", "opencorporates.com", "sec.gov",
})

_LEGAL_SUFFIX_RE = re.compile(
    r"\s*\b(?:S\.?\s*P\.?\s*A\.?|S\.?\s*R\.?\s*L\.?|S\.?\s*A\.?\s*S\.?"
    r"|S\.?\s*N\.?\s*C\.?|S\.?\s*A\.?|GmbH|AG\b|Ltd\.?|LLC\.?|Corp\.?"
    r"|Corporation|Inc\.?|B\.?\s*V\.?\b|N\.?\s*V\.?\b"
    r"|ITALIA\b|ITALY\b|GROUP\b|GRUPPO\b|HOLDING\b|INTERNATIONAL\b)\s*$",
    re.IGNORECASE,
)

_OFFICIAL_PAGE_PATHS = [
    "/", "/about-us", "/about", "/company",
    "/en/about-us", "/en/about", "/ch/en/about-us",
    "/it/chi-siamo", "/chi-siamo", "/contatti", "/contact",
]

_OFFICIAL_HQ_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"its?\s+head\s+office\s+is\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n)", re.I), "Strong"),
    (re.compile(r"head\s+office\s+(?:is\s+)?(?:located\s+)?in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|,\s*(?:with|which)|\n)", re.I), "Strong"),
    (re.compile(r"headquartered\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n)", re.I), "Strong"),
    (re.compile(r"headquarters\s+(?:are\s+)?(?:is\s+)?(?:located\s+)?in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n)", re.I), "Strong"),
    (re.compile(r"corporate\s+headquarters\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n)", re.I), "Strong"),
    (re.compile(r"group\s+headquarters\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n)", re.I), "Strong"),
    (re.compile(r"operational\s+headquarters\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n)", re.I), "Strong"),
    (re.compile(r"registered\s+office\s+(?:in|at|:)\s*([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n)", re.I), "Strong"),
    (re.compile(r"sede\s+(?:principale|legale|centrale)\s*[:\s]+(?:in\s+)?([A-Za-zÀ-ÿ\s,]{3,50}?)(?:\.|;|,|\n)", re.I), "Strong"),
    (re.compile(r"parent\s+company\s+is\s+([A-Za-zÀ-ÿ\s,]{3,60}?)(?:\.|;|\n)", re.I), "Medium"),
    (re.compile(r"part\s+of\s+the\s+([A-Za-zÀ-ÿ\s]{3,40}?)\s+group\b", re.I), "Medium"),
    (re.compile(r"subsidiary\s+of\s+([A-Za-zÀ-ÿ\s,]{3,60}?)(?:\.|;|\n)", re.I), "Medium"),
    (re.compile(r"owned\s+by\s+([A-Za-zÀ-ÿ\s,]{3,60}?)(?:\.|;|,|\n)", re.I), "Medium"),
    (re.compile(r"based\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,50}?)(?:\.|;|,\s*(?:and|with|since)|\n)", re.I), "Medium"),
]


def _strip_html(html: str) -> str:
    html = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _is_directory_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        host = re.sub(r"^www\.", "", host)
        return any(host == d or host.endswith("." + d) for d in _DIRECTORY_DOMAINS)
    except Exception:
        return False


def _get_domain_body(url_or_domain: str) -> str:
    """Return the main token of a domain (e.g. 'repower' from 'repower.com')."""
    s = url_or_domain.strip()
    try:
        from urllib.parse import urlparse
        parsed = urlparse(s if "://" in s else "https://" + s)
        host = re.sub(r"^www\.", "", parsed.netloc.lower()) or s
    except Exception:
        host = s
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def _extract_brand(company_name: str, domain: str) -> list[str]:
    """Strip legal suffixes; also extract domain body. Returns up to 3 brand candidates."""
    brands: list[str] = []
    cleaned = company_name.strip()
    for _ in range(6):
        new = _LEGAL_SUFFIX_RE.sub("", cleaned).strip().rstrip(",. ")
        if not new or new == cleaned:
            break
        cleaned = new
    if cleaned and cleaned.lower() != company_name.strip().lower():
        brands.append(cleaned)
    # Title-case variant
    if cleaned:
        tc = cleaned.title()
        if tc not in brands:
            brands.append(tc)
    # Domain body
    if domain:
        body = _get_domain_body(domain)
        if body and len(body) > 2:
            tc_body = body.title()
            if tc_body.lower() not in {b.lower() for b in brands}:
                brands.append(tc_body)
    seen: set[str] = set()
    out: list[str] = []
    for b in brands:
        if b.lower() not in seen and len(b) > 2:
            seen.add(b.lower())
            out.append(b)
    return out[:3]


def _find_official_result(
    organic: list[dict], input_domain: str
) -> tuple[int, dict, str]:
    """Return (1-based rank, result, clean_netloc) of best non-directory organic result."""
    input_body = _get_domain_body(input_domain) if input_domain else ""
    first_rank, first_result, first_netloc = 0, {}, ""
    for i, item in enumerate(organic[:10], start=1):
        url = item.get("link", "")
        if not url or _is_directory_url(url):
            continue
        try:
            from urllib.parse import urlparse
            netloc = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
        except Exception:
            netloc = url
        body = _get_domain_body(netloc)
        if input_body and (input_body in body or body in input_body):
            return i, item, netloc
        if not first_rank:
            first_rank, first_result, first_netloc = i, item, netloc
    return first_rank, first_result, first_netloc


def _fetch_page_text(url: str, cache: dict) -> tuple[str, str]:
    """Fetch URL, strip HTML, return (text[:8000], error). Cached."""
    ck = ("fetch", url)
    if ck in cache:
        return cache[ck]
    try:
        resp = _requests.get(
            url, timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HQProbe/1.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        text = _strip_html(resp.text[:100_000])[:8_000]
        result: tuple[str, str] = (text, "")
    except Exception as exc:
        result = ("", str(exc)[:120])
    cache[ck] = result
    return result


def _scan_for_hq(text: str) -> tuple[str, str, str, str]:
    """Scan text with OFFICIAL_HQ_PATTERNS. Returns (quote, country, city, strength)."""
    for pat, strength in _OFFICIAL_HQ_PATTERNS:
        m = pat.search(text)
        if m:
            captured = m.group(1).strip(" ,.()")
            city, country = _resolve_city_country(captured)
            if country or city:
                start = max(0, m.start() - 30)
                end   = min(len(text), m.end() + 80)
                quote = text[start:end].strip()
                return quote[:300], country, city, strength
    return "", "", "", ""


def _mimic_google_hq_check(
    company_name: str,
    domain: str,
    input_country: str,
    serper_key: str,
    cache: dict,
    max_page_paths: int = 7,
    max_brand_queries: int = 4,
) -> dict[str, Any]:
    """Run manual Google mimic HQ check.

    Returns audit column dict plus internal keys:
        _mimic_serper_calls (int)
        _mimic_queries      (list[str])
        _mimic_serper_cache_hits (int)
        _mimic_hq_evidence  (dict)  — empty if nothing found
    """
    out: dict[str, Any] = {
        "manual_google_mimic_used":           True,
        "plain_company_search_query":         "",
        "plain_company_top_result_url":       "",
        "plain_company_top_result_title":     "",
        "plain_company_top_result_snippet":   "",
        "plain_company_official_domain":      "",
        "plain_company_official_result_rank": 0,
        "official_domain_from_plain_search":  "",
        "official_domain_matches_input_domain": False,
        "official_page_fetch_used":           False,
        "official_page_fetch_url":            "",
        "official_page_fetch_count":          0,
        "official_page_fetch_error":          "",
        "official_page_hq_evidence_found":    False,
        "official_page_hq_evidence_quote":    "",
        "official_page_hq_country":           "",
        "official_page_hq_city":              "",
        "official_page_hq_evidence_strength": "",
        "brand_hq_search_used":               False,
        "brand_hq_search_queries":            "",
        "brand_hq_top_result_url":            "",
        "brand_hq_top_result_snippet":        "",
        "brand_hq_evidence_found":            False,
        "brand_hq_evidence_quote":            "",
        "brand_hq_top_result_domain":         "",
        "brand_hq_top_result_is_official_domain": False,
        "brand_hq_result_selection_reason":   "",
        # Internal
        "_mimic_serper_calls":      0,
        "_mimic_queries":           [],
        "_mimic_serper_cache_hits": 0,
        "_mimic_hq_evidence":       {},
    }

    # ── 1. Plain company name search ──────────────────────────────────────
    plain_query = company_name.strip()
    out["plain_company_search_query"] = plain_query
    out["_mimic_queries"].append(plain_query)

    ck = ("serper", plain_query)
    if ck in cache:
        plain_data, plain_err = cache[ck]
        out["_mimic_serper_cache_hits"] += 1
    else:
        plain_data, plain_err = _serper_search(plain_query, serper_key)
        cache[ck] = (plain_data, plain_err)
        out["_mimic_serper_calls"] += 1
        time.sleep(_INTER_REQUEST_SLEEP)

    plain_organic = plain_data.get("organic", []) if plain_data else []

    # ── 2. Find official result ────────────────────────────────────────────
    rank, off_result, off_netloc = _find_official_result(plain_organic, domain)
    if off_result:
        out["plain_company_top_result_url"]       = off_result.get("link", "")
        out["plain_company_top_result_title"]     = off_result.get("title", "")
        out["plain_company_top_result_snippet"]   = off_result.get("snippet", "")
        out["plain_company_official_domain"]      = off_netloc
        out["plain_company_official_result_rank"] = rank
        out["official_domain_from_plain_search"]  = off_netloc
        input_body  = _get_domain_body(domain) if domain else ""
        off_body    = _get_domain_body(off_netloc)
        out["official_domain_matches_input_domain"] = bool(
            input_body and (input_body in off_body or off_body in input_body)
        )

    # ── 3. Fetch official pages ────────────────────────────────────────────
    fetch_domain = off_netloc or (
        domain.strip().lstrip("https://").lstrip("http://").rstrip("/") if domain else ""
    )
    combined_page_text = ""
    first_ok_url = ""
    fetch_count  = 0
    fetch_errors: list[str] = []

    if fetch_domain:
        out["official_page_fetch_used"] = True
        for path in _OFFICIAL_PAGE_PATHS[:max_page_paths]:
            url_try = f"https://{fetch_domain}{path}"
            text, err = _fetch_page_text(url_try, cache)
            fetch_count += 1
            if text:
                if not first_ok_url:
                    first_ok_url = url_try
                # Early-exit scan for Strong evidence
                q, country, city, strength = _scan_for_hq(text)
                if strength == "Strong" and country:
                    out["official_page_fetch_url"]           = url_try
                    out["official_page_fetch_count"]         = fetch_count
                    out["official_page_hq_evidence_found"]   = True
                    out["official_page_hq_evidence_quote"]   = q
                    out["official_page_hq_country"]          = country
                    out["official_page_hq_city"]             = city
                    out["official_page_hq_evidence_strength"]= strength
                    out["_mimic_hq_evidence"] = {
                        "hq_detected_city":    city,
                        "hq_detected_country": country,
                        "hq_confidence":       "High",
                        "hq_reason":           f"[mimic-official-page:{path}] {q[:150]}",
                        "hq_evidence_url":     url_try,
                        "hq_evidence_quote":   q,
                    }
                    break
                combined_page_text += " " + text
            else:
                fetch_errors.append(f"{path}: {err}")

        out["official_page_fetch_url"]   = out["official_page_fetch_url"] or first_ok_url
        out["official_page_fetch_count"] = fetch_count
        out["official_page_fetch_error"] = "; ".join(fetch_errors[:3])

    # Fallback: scan combined text if single-page scan missed
    if combined_page_text and not out["official_page_hq_evidence_found"]:
        q, country, city, strength = _scan_for_hq(combined_page_text)
        if country or city:
            out["official_page_hq_evidence_found"]    = True
            out["official_page_hq_evidence_quote"]    = q
            out["official_page_hq_country"]           = country
            out["official_page_hq_city"]              = city
            out["official_page_hq_evidence_strength"] = strength
            if strength in ("Strong", "Medium") and not out["_mimic_hq_evidence"]:
                out["_mimic_hq_evidence"] = {
                    "hq_detected_city":    city,
                    "hq_detected_country": country,
                    "hq_confidence":       "High" if strength == "Strong" else "Medium",
                    "hq_reason":           f"[mimic-official-page-combined] {q[:150]}",
                    "hq_evidence_url":     out["official_page_fetch_url"],
                    "hq_evidence_quote":   q,
                }

    # ── 4. Brand/domain HQ follow-up (only if no strong evidence yet) ──────
    if not out["_mimic_hq_evidence"]:
        brands = _extract_brand(company_name, fetch_domain or domain)
        brand_queries: list[str] = []
        if brands:
            primary = brands[0]
            brand_queries = [
                f'"{primary}" headquarters',
                f'"{primary}" head office',
                f'"{primary}" group headquarters',
                f'"{primary}" parent company',
            ]
        brand_queries = brand_queries[:max_brand_queries]
        if brand_queries:
            out["brand_hq_search_used"]    = True
            out["brand_hq_search_queries"] = " | ".join(brand_queries)
            for bq in brand_queries:
                out["_mimic_queries"].append(bq)
                ck_b = ("serper", bq)
                if ck_b in cache:
                    bdata, _ = cache[ck_b]
                    out["_mimic_serper_cache_hits"] += 1
                else:
                    bdata, _ = _serper_search(bq, serper_key)
                    cache[ck_b] = (bdata, "")
                    out["_mimic_serper_calls"] += 1
                    time.sleep(_INTER_REQUEST_SLEEP)
                if not bdata:
                    continue
                b_org = bdata.get("organic", [])
                b_kg  = bdata.get("knowledgeGraph", {})
                b_kg_loc = b_kg.get("address","") or b_kg.get("headquarters","") or b_kg.get("location","")
                b_ab  = bdata.get("answerBox", {})
                b_ab_t = b_ab.get("answer","") or b_ab.get("snippet","")
                all_b = " ".join([
                    b_kg_loc, b_ab_t,
                    *[f"{r.get('title','')} {r.get('snippet','')}" for r in b_org[:3]],
                ])
                q, country, city, strength = _scan_for_hq(all_b)
                if country or city:
                    out["brand_hq_evidence_found"]   = True
                    out["brand_hq_evidence_quote"]   = q
                    # Prefer official/input domain result over directories or unrelated domains
                    from urllib.parse import urlparse as _up
                    _official_netloc = (fetch_domain or domain or "").lstrip("www.").lower()
                    _plain_official  = out.get("plain_company_official_domain", "").lstrip("www.").lower()
                    def _brand_rank(r: dict) -> int:
                        lnk = r.get("link", "")
                        nl  = _up(lnk).netloc.lstrip("www.").lower()
                        if _is_directory_url(lnk):
                            return 10
                        if _official_netloc and (nl == _official_netloc or nl.endswith("." + _official_netloc)):
                            return 0
                        if _plain_official and (nl == _plain_official or nl.endswith("." + _plain_official)):
                            return 1
                        return 5
                    sorted_org = sorted(b_org, key=_brand_rank) if b_org else []
                    top_r = sorted_org[0] if sorted_org else {}
                    top_nl = _up(top_r.get("link", "")).netloc.lstrip("www.").lower() if top_r else ""
                    _is_off = bool(_official_netloc and (top_nl == _official_netloc or top_nl.endswith("." + _official_netloc)))
                    _sel_reason = (
                        "input_domain_match" if _is_off
                        else ("plain_official_match" if _plain_official and top_nl == _plain_official else "best_non_directory")
                    )
                    out["brand_hq_top_result_url"]     = top_r.get("link", "")
                    out["brand_hq_top_result_snippet"] = top_r.get("snippet", "")
                    out["brand_hq_top_result_domain"]              = top_nl
                    out["brand_hq_top_result_is_official_domain"]  = _is_off
                    out["brand_hq_result_selection_reason"]        = _sel_reason
                    if strength in ("Strong", "Medium") and not out["_mimic_hq_evidence"]:
                        out["_mimic_hq_evidence"] = {
                            "hq_detected_city":    city,
                            "hq_detected_country": country,
                            "hq_confidence":       "High" if strength == "Strong" else "Medium",
                            "hq_reason":           f"[mimic-brand-search:{bq[:60]}] {q[:150]}",
                            "hq_evidence_url":     out["brand_hq_top_result_url"],
                            "hq_evidence_quote":   q,
                        }
                    break  # stop after first brand query with evidence

    return out


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
    input_row: "dict | None" = None,
    use_multilingual_check: bool = True,
    use_anthropic_review: bool = False,
    use_mimic_check: bool = True,
    run_mode: str = "fast",
) -> dict[str, Any]:
    """Run all queries for one company; return probe column dict."""
    import time as _time_mod
    _row_start = _time_mod.monotonic()

    # ── Mode-based limits ───────────────────────────────────────────────────
    # fast: ≤3 real Serper calls, 1 page path, 2 brand queries, 5s fetch timeout
    # deep/debug: ≤8 Serper calls, 7 page paths, 4 brand queries
    if run_mode == "fast":
        _max_serper        = 3
        _max_page_paths    = 1   # only homepage in fast mode
        _max_brand_queries = 2
        _fetch_timeout     = 5
    else:
        _max_serper        = 8
        _max_page_paths    = 7
        _max_brand_queries = 4
        _fetch_timeout     = 10

    result: dict[str, Any] = {col: "" for col in PROBE_COLS}
    result["run_mode"]               = run_mode
    result["max_serper_calls_per_row"] = _max_serper
    result["early_stop_used"]        = False
    result["early_stop_reason"]      = ""

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

    # Usage tracking
    _serper_calls      = 0
    _serper_cache_hits = 0
    _queries_attempted: list[str] = []
    _anthr_input_tok  = 0
    _anthr_output_tok = 0
    _anthr_total_tok  = 0
    _anthr_model      = ""
    _anthr_stop       = ""
    _anthr_errors: list[str] = []

    # --- Manual Google Mimic HQ Check (runs before narrow queries) ---
    _mimic_audit: dict = {}
    _mimic_evidence: dict = {}
    if use_mimic_check and serper_key:
        _mimic_result = _mimic_google_hq_check(
            company_name, domain, input_country, serper_key, cache,
            max_page_paths=_max_page_paths,
            max_brand_queries=_max_brand_queries,
        )
        _mimic_serper_calls = _mimic_result.pop("_mimic_serper_calls", 0)
        _mimic_queries      = _mimic_result.pop("_mimic_queries", [])
        _mimic_cache_hits   = _mimic_result.pop("_mimic_serper_cache_hits", 0)
        _mimic_evidence     = _mimic_result.pop("_mimic_hq_evidence", {})
        # Accumulate into tracking
        _serper_calls      += _mimic_serper_calls
        _serper_cache_hits += _mimic_cache_hits
        _queries_attempted  = _mimic_queries + _queries_attempted
        # Store audit cols for later merge
        _mimic_audit = _mimic_result
        # If mimic found strong evidence, use it as best and skip narrow queries
        if _mimic_evidence.get("hq_detected_country"):
            best = {k: v for k, v in _mimic_evidence.items()}
            used_query = _mimic_queries[0] if _mimic_queries else ""
            result["early_stop_used"]   = True
            result["early_stop_reason"] = "clear_official_hq_evidence"
    else:
        _mimic_audit = {"manual_google_mimic_used": False}

    _narrow_serper_budget = max(0, _max_serper - _serper_calls)

    for query in queries:
        if best.get("hq_detected_country"):
            break  # mimic already found strong evidence — early stop
        if _narrow_serper_budget <= 0:
            break  # Serper budget exhausted for this row
        _queries_attempted.append(query)
        cache_key = ("serper", query)
        if cache_key in cache:
            data, err = cache[cache_key]
            _serper_cache_hits += 1
        else:
            if _serper_calls >= _max_serper:
                break  # double-guard
            data, err = _serper_search(query, serper_key)
            cache[cache_key] = (data, err)
            _serper_calls += 1
            _narrow_serper_budget -= 1
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
        _mu = model_result.pop("_usage", {})
        if _mu:
            _anthr_model      = _mu.get("model", "")
            _anthr_input_tok  += _mu.get("input_tokens", 0)
            _anthr_output_tok += _mu.get("output_tokens", 0)
            _anthr_total_tok  += _mu.get("total_tokens", 0)
            _anthr_stop        = _mu.get("stop_reason", "")
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
            _anthr_errors.append(model_result["probe_error"])

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

    # ----------------------------------------------------------------
    # Recovery review additions
    # ----------------------------------------------------------------
    _irow = input_row or {}

    # Preserve original sig_foreign_hq_score
    orig_score = _safe_float(_irow.get("sig_foreign_hq_score"))
    result["sig_foreign_hq_score_original"] = orig_score
    result["sig_foreign_hq_score_reviewed"] = orig_score  # default: keep original

    # Multilingual site detection
    ml_result: dict[str, Any] = {
        "has_multilingual_site": False, "website_language_count": 0,
        "website_languages_detected": "", "website_fetch_count": 0,
    }
    if use_multilingual_check and domain:
        _ml_key = ("multilingual", domain.strip().lstrip("https://").lstrip("http://").rstrip("/"))
        if _ml_key in cache:
            ml_result = cache[_ml_key]
        else:
            ml_result = detect_multilingual_site(domain)
            cache[_ml_key] = ml_result
    result.update(ml_result)

    # Global structure detection
    gs_result = detect_global_structure(all_organic, kg_location, answer_box_text)
    result.update(gs_result)

    # HQ review trigger
    trigger_str, needs_adj = compute_review_trigger(
        result, _irow, ml_result, gs_result, company_name=company_name
    )
    result["hq_review_trigger"]              = trigger_str
    result["needs_anthropic_hq_review"]      = "Yes" if needs_adj else "No"
    result["anthropic_hq_review_used"]       = "No"
    result["anthropic_web_search_used"]      = "No"
    result["anthropic_review_evidence_mode"] = ""
    result["anthropic_web_search_queries"]   = ""
    result["anthropic_web_search_result_count"] = 0

    # ---- Merge mimic audit columns (before Anthropic so we can inspect evidence) ----
    for _k, _v in _mimic_audit.items():
        if _k in result:
            result[_k] = _v

    # Suppress Anthropic when official page evidence is already strong and unambiguous
    _has_strong_official = (
        result.get("official_page_hq_evidence_strength") == "Strong"
        and result.get("official_page_hq_country")
    )
    _has_strong_brand = (
        result.get("brand_hq_evidence_found")
        and _mimic_evidence.get("hq_confidence") == "High"
        and _mimic_evidence.get("hq_detected_country")
    )
    if _has_strong_official or _has_strong_brand:
        _ev_src = "official_page_mimic_check" if _has_strong_official else "brand_hq_mimic_check"
        result["needs_anthropic_hq_review"] = "No"
        result["anthropic_hq_review_used"]  = "No"
        result["anthropic_web_search_used"] = "No"
        result["anthropic_review_evidence_mode"] = ""
        result["sig_foreign_hq_review_source"] = _ev_src
        if _has_strong_official:
            result["sig_foreign_hq_review_evidence_url"]   = result.get("official_page_fetch_url", "")
            result["sig_foreign_hq_review_evidence_quote"] = result.get("official_page_hq_evidence_quote", "")
        needs_adj = False

    # Optional Anthropic HQ adjudication
    if use_anthropic_review and needs_adj and anthropic_key:
        adj = _anthropic_hq_adjudicate(
            company_name, domain, result, ml_result, gs_result, anthropic_key
        )
        _au = adj.pop("_usage", {})
        if _au:
            _anthr_model      = _anthr_model or _au.get("model", "")
            _anthr_input_tok  += _au.get("input_tokens", 0)
            _anthr_output_tok += _au.get("output_tokens", 0)
            _anthr_total_tok  += _au.get("total_tokens", 0)
            _anthr_stop        = _anthr_stop or _au.get("stop_reason", "")
        if not adj.get("probe_error"):
            for _field in (
                "hq_structure_type", "local_entity_hq_country", "local_entity_hq_city",
                "parent_group_hq_country", "parent_group_hq_city",
                "review_foreign_parent_score", "review_global_network_score",
                "review_multilingual_website_score", "review_recommended_hq_signal",
                "sig_foreign_hq_score_reviewed",
            ):
                if _field in adj:
                    result[_field] = adj[_field]
            result["sig_foreign_hq_review_reason"]         = adj.get("reason", "")
            result["sig_foreign_hq_review_confidence"]     = adj.get("confidence", "")
            result["sig_foreign_hq_review_source"]         = "anthropic"
            result["sig_foreign_hq_review_evidence_url"]   = adj.get("evidence_url", "")
            result["sig_foreign_hq_review_evidence_quote"] = adj.get("evidence_quote", "")
            result["anthropic_hq_review_used"]             = "Yes"
            result["anthropic_web_search_used"]            = "No"
            result["anthropic_review_evidence_mode"]       = "serper_evidence_only"
        else:
            _adj_err = adj["probe_error"]
            _anthr_errors.append(_adj_err)
            result["probe_error"] = (result["probe_error"] + "; " + _adj_err).lstrip("; ") if result.get("probe_error") else _adj_err

    # ---- Populate usage columns ----
    result["serper_calls_used"]   = _serper_calls
    result["serper_queries_used"] = " | ".join(_queries_attempted)
    if _serper_calls > 0 and _serper_cache_hits > 0:
        result["serper_cache_hit"] = "partial"
    elif _serper_cache_hits > 0:
        result["serper_cache_hit"] = "True"
    else:
        result["serper_cache_hit"] = "False"
    result["website_fetch_count"] = ml_result.get("website_fetch_count", 0)
    result["anthropic_error"]     = "; ".join(_anthr_errors)
    result["row_runtime_seconds"] = round(_time_mod.monotonic() - _row_start, 2)
    # Internal token tracking (for run-summary aggregation only, not in Excel rows)
    result["_anthr_model"]        = _anthr_model
    result["_anthr_input_tok"]    = _anthr_input_tok
    result["_anthr_output_tok"]   = _anthr_output_tok
    result["_anthr_total_tok"]    = _anthr_total_tok
    result["_anthr_stop"]         = _anthr_stop

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
    run_usage: "dict | None" = None,
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

    # Run Summary sheet
    _ru = run_usage or {}
    ws_sum = wb.create_sheet("Run Summary")
    sum_rows = [
        ("Run Summary – hq_lookup_probe_app.py", ""),
        ("", ""),
        ("── run info ──", ""),
        ("timestamp",           qa_meta.get("timestamp", "")),
        ("input_file",          qa_meta.get("input_file", "")),
        ("rows_processed",      len(probe_results)),
        ("", ""),
        ("── API usage ──", ""),
        ("serper_calls_used",          _ru.get("serper_calls_used", "")),
        ("rows_with_serper_cache_hit", _ru.get("rows_with_cache_hit", "")),
        ("website_fetches_attempted",  _ru.get("website_fetches", "")),
        ("anthropic_reviews_used",     _ru.get("anthropic_reviews_used", "")),
        ("anthropic_web_search_used",  _ru.get("anthropic_web_search_used", "")),
        ("anthropic_errors",           _ru.get("anthropic_errors", "")),
        ("", ""),
        ("── options used ──", ""),
        ("run_mode",                    qa_meta.get("run_mode", "")),
        ("multilingual_check_enabled",  qa_meta.get("use_multilingual_check", "")),
        ("anthropic_hq_review_enabled", qa_meta.get("use_anthropic_review", "")),
        ("model_fallback_enabled",      qa_meta.get("use_model", "")),
        ("model",                       qa_meta.get("model", "")),
        ("row_limit",                   qa_meta.get("limit", "")),
    ]
    ws_sum.column_dimensions["A"].width = 32
    ws_sum.column_dimensions["B"].width = 50
    for r_idx, (k, v) in enumerate(sum_rows, start=1):
        cell_a = ws_sum.cell(row=r_idx, column=1, value=k)
        ws_sum.cell(row=r_idx, column=2, value=v)
        if r_idx == 1:
            cell_a.font = Font(bold=True, size=13)
        elif k and not k.startswith("─"):
            cell_a.font = Font(bold=True)
        ws_sum.row_dimensions[r_idx].height = 15

    return wb


def build_excel_bytes(
    input_rows: list[dict],
    probe_results: list[dict],
    present_old_cols: list[str],
    company_col: str,
    domain_col: str,
    country_col: str,
    qa_meta: dict,
    run_usage: "dict | None" = None,
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
        run_usage=run_usage,
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

    limit = st.number_input("Row limit", min_value=1, max_value=2000, value=50, step=50)

    only_fhq_signal = st.checkbox(
        "Only rows with old foreign HQ signal",
        value=False,
        help="Filter input to rows where sig_foreign_hq_score > 0 or foreign_hq_sanitized = True/Yes.",
    )

    only_recovery = st.checkbox(
        "Only recovery candidates (zero/blank old FHQ score)",
        value=False,
        help="Focus on companies where sig_foreign_hq_score is 0 or blank — likely under-scored.",
    )

    run_mode = st.radio(
        "Run mode",
        options=["fast", "deep", "debug"],
        index=0,
        help=(
            "**Fast** (default): ≤3 Serper calls/row, 1 page fetch, 2 brand queries, 5 s timeout. "
            "Stop immediately when official HQ evidence is clear.\n\n"
            "**Deep**: ≤8 Serper calls/row, up to 7 page paths, 4 brand queries, 10 s timeout. "
            "Enables Anthropic review for ambiguous cases.\n\n"
            "**Debug**: same as Deep but keeps all audit detail visible in the results table."
        ),
        horizontal=True,
    )

    use_mimic_check = True  # always on; mode controls depth

    use_multilingual_check = st.checkbox(
        "Detect multilingual website",
        value=(run_mode != "fast"),
        help="Fetches company homepage to detect language switchers and hreflang tags. Disabled by default in Fast mode.",
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

    use_anthropic_review = st.checkbox(
        "Use Anthropic HQ review for ambiguous/global cases",
        value=(run_mode in ("deep", "debug")),
        help="Calls Claude to adjudicate hq_structure_type for rows with needs_anthropic_hq_review=Yes. Auto-enabled in Deep/Debug mode.",
    )

    anthropic_key = ""
    if use_model or use_anthropic_review:
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

_mode_max = 3 if run_mode == "fast" else 8
_mode_label = {"fast": "Fast", "deep": "Deep", "debug": "Debug"}.get(run_mode, run_mode)
st.markdown(
    f"**Mode: {_mode_label}** — up to **{_mode_max} Serper calls/row**"
    + (", + 1 website fetch for multilingual detection" if use_multilingual_check else "")
    + (", + Anthropic review for ambiguous rows" if use_anthropic_review else "")
    + f". With limit={int(limit)}, that is up to **{int(limit) * _mode_max:,} Serper calls**."
    + (" Early stopping is active: rows with clear official HQ evidence skip further queries." if run_mode == "fast" else "")
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

    # Old-FHQ signal filter
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

    # Recovery candidates filter (zero/blank FHQ score)
    if only_recovery:
        def _is_recovery_candidate(row: dict) -> bool:
            try:
                return float(row.get("sig_foreign_hq_score") or 0) == 0
            except (TypeError, ValueError):
                return True

        filtered_r = [r for r in input_rows if _is_recovery_candidate(r)]
        if filtered_r:
            st.info(f"Recovery filter: {len(filtered_r)} / {len(input_rows)} rows have zero/blank FHQ score.")
            input_rows = filtered_r
        else:
            st.warning("No recovery candidates found (all rows have non-zero FHQ score). Running on all rows.")

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
            input_row=row,
            use_multilingual_check=use_multilingual_check,
            use_anthropic_review=use_anthropic_review,
            use_mimic_check=use_mimic_check,
            run_mode=run_mode,
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

    # Run-level usage aggregation
    run_usage = {
        "rows_processed":           len(probe_results),
        "serper_calls_used":        sum(int(p.get("serper_calls_used") or 0) for p in probe_results),
        "rows_with_cache_hit":      sum(1 for p in probe_results if p.get("serper_cache_hit") in ("True", "partial")),
        "website_fetches":          sum(int(p.get("website_fetch_count") or 0) for p in probe_results),
        "anthropic_reviews_used":   sum(1 for p in probe_results if p.get("anthropic_hq_review_used") == "Yes"),
        "anthropic_web_search_used":sum(1 for p in probe_results if p.get("anthropic_web_search_used") == "Yes"),
        "anthropic_input_tokens":   sum(int(p.get("_anthr_input_tok") or 0) for p in probe_results),
        "anthropic_output_tokens":  sum(int(p.get("_anthr_output_tok") or 0) for p in probe_results),
        "anthropic_total_tokens":   sum(int(p.get("_anthr_total_tok") or 0) for p in probe_results),
        "anthropic_errors":         sum(1 for p in probe_results if p.get("anthropic_error")),
    }

    # Store in session state
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.session_state[_KEY_RESULTS]    = probe_results
    st.session_state[_KEY_INPUT_ROWS] = input_rows
    st.session_state[_KEY_OLD_COLS]   = present_old_cols
    st.session_state[_KEY_COLS_CFG]   = (company_col, domain_col, country_col or "(default)")
    st.session_state[_KEY_META] = {
        "timestamp":              ts,
        "input_file":             file_label,
        "use_model":              use_model,
        "model":                  "claude-haiku-4-5-20251001" if (use_model or use_anthropic_review) else "",
        "serper_available":       bool(serper_key),
        "limit":                  int(limit),
        "use_multilingual_check": use_multilingual_check,
        "use_anthropic_review":   use_anthropic_review,
        "use_mimic_check":        use_mimic_check,
        "run_mode":               run_mode,
    }
    st.session_state["hq_probe_run_usage"] = run_usage

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

needs_adj_cnt    = sum(1 for p in probe_results if p.get("needs_anthropic_hq_review") == "Yes")
multilingual_cnt = sum(1 for p in probe_results if p.get("has_multilingual_site"))
global_net_cnt   = sum(1 for p in probe_results if p.get("has_global_structure_signal"))
run_usage        = st.session_state.get("hq_probe_run_usage", {})

st.markdown("---")
st.subheader("Results")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Rows processed",    len(probe_results))
c2.metric("Italy HQ detected", detected_italy)
c3.metric("Foreign HQ",        detected_foreign)
c4.metric("Unknown",           detected_unknown)
c5.metric("Needs review",      needs_review_cnt)

r1, r2, r3 = st.columns(3)
r1.metric("Multilingual site",       multilingual_cnt)
r2.metric("Global structure signal", global_net_cnt)
r3.metric("Needs Anthropic review",  needs_adj_cnt)

if run_usage:
    with st.expander("Usage / API call summary", expanded=False):
        u1, u2, u3, u4, u5 = st.columns(5)
        u1.metric("Serper calls",              run_usage.get("serper_calls_used", 0))
        u2.metric("Rows with cache hit",       run_usage.get("rows_with_cache_hit", 0))
        u3.metric("Website fetches",           run_usage.get("website_fetches", 0))
        u4.metric("Anthropic reviews used",    run_usage.get("anthropic_reviews_used", 0))
        u5.metric("Anthropic web search used", run_usage.get("anthropic_web_search_used", 0))
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("Anthropic input tokens",    run_usage.get("anthropic_input_tokens", 0))
        t2.metric("Anthropic output tokens",   run_usage.get("anthropic_output_tokens", 0))
        t3.metric("Anthropic total tokens",    run_usage.get("anthropic_total_tokens", 0))
        t4.metric("Anthropic errors",          run_usage.get("anthropic_errors", 0))

# Display columns
_KEY_VIEW_COLS_RAW = [
    company_col_r, domain_col_r, country_col_r,
    "sig_foreign_hq_score_original",
    "sig_foreign_hq_score_reviewed",
    "sig_foreign_hq_score", "sig_foreign_hq_evidence",
    "foreign_hq_sanitized", "foreign_hq_sanitizer_reason",
    "hq_detected_city", "hq_detected_region", "hq_detected_country",
    "input_country_used",
    "hq_confidence", "foreign_hq_simple", "needs_manual_review",
    "hq_structure_type",
    "parent_group_hq_country", "parent_group_hq_city",
    "local_entity_hq_country", "local_entity_hq_city",
    "has_multilingual_site", "website_languages_detected",
    "has_global_structure_signal", "global_structure_evidence",
    "hq_review_trigger",
    "needs_anthropic_hq_review", "anthropic_hq_review_used",
    "anthropic_web_search_used", "anthropic_review_evidence_mode",
    "anthropic_error",
    "sig_foreign_hq_review_reason",
    "sig_foreign_hq_review_evidence_url",
    "run_mode", "early_stop_used", "early_stop_reason",
    "serper_calls_used", "serper_cache_hit",
    "manual_google_mimic_used",
    "plain_company_official_domain", "official_domain_matches_input_domain",
    "official_page_hq_evidence_found", "official_page_hq_evidence_strength",
    "official_page_hq_country", "official_page_hq_city",
    "brand_hq_evidence_found", "brand_hq_evidence_quote",
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
            run_usage=run_usage,
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
