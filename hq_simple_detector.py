"""Simple HQ query builder for Lead Prioritizer v2.

Stage 1 — dormant foundation. No Serper or Anthropic calls are made here.

Query strategy
--------------
- If a domain is known, derive the domain root and return
  ``(domain_root, "{domain_root} headquarters")``.
- If no domain is known, derive a safe fallback root from ``company_name``
  and return ``(fallback_root, "{fallback_root} headquarters")``.
- Do not use the company legal name when a domain is available.
- Do not use extra query ladders or quoted company-name queries.
- Forbidden query terms: ``head office``, ``global headquarters``,
  ``corporate headquarters``, ``parent company``, ``sede legale``, ``site:``.

Return value
------------
Both functions return ``(root, query)`` — the root always comes first so
downstream pipeline steps can reference it independently.

Examples
--------
>>> derive_domain_root("ibm.com")
'ibm'
>>> build_simple_hq_query("IBM", "ibm.com")
('ibm', 'ibm headquarters')

>>> derive_domain_root("bodycote.com")
'bodycote'
>>> build_simple_hq_query("Bodycote", "bodycote.com")
('bodycote', 'bodycote headquarters')

>>> derive_domain_root("nadara.com")
'nadara'
>>> build_simple_hq_query("Nadara", "nadara.com")
('nadara', 'nadara headquarters')

>>> derive_domain_root("datwyler.com")
'datwyler'
>>> build_simple_hq_query("Datwyler", "datwyler.com")
('datwyler', 'datwyler headquarters')

>>> build_simple_hq_query("Some Company S.p.A.", None)
('some company', 'some company headquarters')
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse as _urlparse

# Legal-form suffixes to strip when building a company-name fallback root.
# Kept deliberately conservative — only unambiguous suffixes.
_LEGAL_SUFFIXES_RE = re.compile(
    r"""
    [\s,\.\-]+          # optional separator before suffix
    (?:
        s\.?p\.?a\.?    # S.p.A. / SPA
      | s\.?r\.?l\.?    # S.r.l. / SRL
      | s\.?a\.?s\.?    # S.a.s.
      | s\.?n\.?c\.?    # S.n.c.
      | b\.?v\.?        # B.V.
      | n\.?v\.?        # N.V.
      | gmbh            # GmbH
      | ag              # AG
      | ltd\.?          # Ltd
      | llc             # LLC
      | inc\.?          # Inc.
      | corp\.?         # Corp.
      | s\.?a\.?        # S.A.  (must come after s.a.s.)
      | aps             # ApS
      | a\/s            # A/S
      | oy              # Oy
      | ab              # AB
      | plc             # PLC
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Public second-level TLDs that look like TLDs but are actually
# "<registrable-label>.<pseudo-tld>" — e.g. example.co.uk → root is "example".
# Kept small and explicit; no external dependency required.
_PSEUDO_TLDS = frozenset({
    # United Kingdom
    "co.uk", "org.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk",
    "ac.uk", "gov.uk", "sch.uk",
    # Australia
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au",
    # Japan
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ed.jp",
    # New Zealand
    "co.nz", "org.nz", "net.nz", "govt.nz",
    # South Africa
    "co.za", "org.za", "net.za", "gov.za",
    # Brazil
    "com.br", "org.br", "net.br", "gov.br", "edu.br",
    # Argentina
    "com.ar", "org.ar", "net.ar",
    # Mexico
    "com.mx", "org.mx", "net.mx",
    # India
    "co.in", "org.in", "net.in", "gov.in",
    # Hong Kong
    "com.hk", "org.hk", "net.hk",
    # Singapore
    "com.sg", "org.sg", "net.sg",
    # Malaysia
    "com.my", "org.my", "net.my",
    # Philippines
    "com.ph", "org.ph",
    # Indonesia
    "co.id", "or.id",
    # South Korea
    "co.kr", "or.kr",
    # Turkey
    "com.tr", "org.tr",
    # Vietnam
    "com.vn", "org.vn",
    # Pakistan
    "com.pk", "org.pk",
    # Uruguay
    "com.uy", "net.uy", "org.uy", "edu.uy", "gub.uy",
})


def derive_domain_root(domain: str) -> str:
    """Return the registrable label of a domain (no TLD, no subdomains).

    Handles common messy inputs without crashing on blanks or malformed values.
    Recognises pseudo-TLDs (co.uk, com.au, co.jp, …) so the registrable label
    is always one level above the effective TLD.

    >>> derive_domain_root("ibm.com")
    'ibm'
    >>> derive_domain_root("www.bodycote.com")
    'bodycote'
    >>> derive_domain_root("https://nadara.com/about")
    'nadara'
    >>> derive_domain_root("http://www.datwyler.com")
    'datwyler'
    >>> derive_domain_root("example.co.uk")
    'example'
    >>> derive_domain_root("www.example.co.uk")
    'example'
    >>> derive_domain_root("example.com.au")
    'example'
    >>> derive_domain_root("example.co.jp")
    'example'
    >>> derive_domain_root("macromercado.com.uy")
    'macromercado'
    >>> derive_domain_root("")
    ''
    """
    if not domain or not domain.strip():
        return ""

    d = domain.strip()

    # Strip scheme
    d = re.sub(r"^https?://", "", d, flags=re.IGNORECASE)

    # Strip markdown link syntax: [text](url) — take the url part
    md_match = re.match(r"^\[.*?\]\((https?://)?([^)]+)\)", d)
    if md_match:
        d = md_match.group(2)

    # Drop path, query, fragment
    d = d.split("/")[0].split("?")[0].split("#")[0]

    # Drop port
    d = d.split(":")[0]

    d = d.strip().lower()
    if not d:
        return ""

    # Drop subdomains: keep the registrable label before the TLD.
    # For pseudo-TLDs like .co.uk, .com.au, .co.jp the last TWO labels form the
    # effective TLD, so the registrable label is parts[-3].
    parts = [p for p in d.split(".") if p]
    if len(parts) >= 3 and ".".join(parts[-2:]) in _PSEUDO_TLDS:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def _company_name_fallback_root(company_name: str) -> str:
    """Derive a safe, lowercase query root from a company name.

    Strips legal-form suffixes (S.p.A., Ltd, GmbH, …) and collapses whitespace.
    """
    root = company_name.strip()
    # Remove legal suffixes
    root = _LEGAL_SUFFIXES_RE.sub("", root).strip(" ,.")
    # Collapse internal whitespace; lowercase
    root = re.sub(r"\s+", " ", root).lower()
    return root


def build_simple_hq_query(
    company_name: str,
    domain: Optional[str],
) -> tuple[str, str]:
    """Return ``(root, query)`` for a simple HQ search.

    - When *domain* is provided the root is the domain's registrable label and
      the query is ``"{root} headquarters"``.  The company legal name is NOT used.
    - When *domain* is absent or blank the root is derived from *company_name*
      (legal suffixes stripped, lower-cased).

    Both elements of the returned tuple are plain strings — never ``None``.

    >>> build_simple_hq_query("IBM", "ibm.com")
    ('ibm', 'ibm headquarters')
    >>> build_simple_hq_query("Bodycote", "bodycote.com")
    ('bodycote', 'bodycote headquarters')
    >>> build_simple_hq_query("Datwyler", "datwyler.com")
    ('datwyler', 'datwyler headquarters')
    >>> build_simple_hq_query("Nadara", "nadara.com")
    ('nadara', 'nadara headquarters')
    >>> build_simple_hq_query("Some Company S.p.A.", None)
    ('some company', 'some company headquarters')
    """
    if domain and domain.strip():
        root = derive_domain_root(domain.strip())
        if root:
            return root, f"{root} headquarters"

    # No domain (or derive_domain_root returned empty) — use company name
    root = _company_name_fallback_root(company_name)
    if not root:
        root = company_name.strip().lower()
    return root, f"{root} headquarters"


# ---------------------------------------------------------------------------
# Step 2B — Dormant Serper-result parser
# No live API calls. Pure function operating on a supplied payload dict.
# ---------------------------------------------------------------------------

# ── Location lookup tables (subset extracted from probe app) ─────────────────
# TODO: future step — move country-specific location rules (_INTL_CITIES,
#       _COUNTRY_ALIASES, _ITALY_PROVINCE_CODES, _ITALY_*) into a dedicated
#       lead_country_config.py module so hq_simple_detector stays thin.

_INTL_CITIES: dict[str, tuple[str, str]] = {
    "berlin": ("Berlin", "Germany"), "munich": ("Munich", "Germany"),
    "münchen": ("München", "Germany"), "hamburg": ("Hamburg", "Germany"),
    "frankfurt": ("Frankfurt", "Germany"), "cologne": ("Cologne", "Germany"),
    "köln": ("Köln", "Germany"), "düsseldorf": ("Düsseldorf", "Germany"),
    "stuttgart": ("Stuttgart", "Germany"),
    "paris": ("Paris", "France"), "lyon": ("Lyon", "France"),
    "marseille": ("Marseille", "France"), "toulouse": ("Toulouse", "France"),
    "amsterdam": ("Amsterdam", "Netherlands"), "rotterdam": ("Rotterdam", "Netherlands"),
    "the hague": ("The Hague", "Netherlands"), "eindhoven": ("Eindhoven", "Netherlands"),
    "utrecht": ("Utrecht", "Netherlands"),
    "madrid": ("Madrid", "Spain"), "barcelona": ("Barcelona", "Spain"),
    "zurich": ("Zurich", "Switzerland"), "zürich": ("Zürich", "Switzerland"),
    "geneva": ("Geneva", "Switzerland"), "bern": ("Bern", "Switzerland"),
    "basel": ("Basel", "Switzerland"), "altdorf": ("Altdorf", "Switzerland"),
    "zug": ("Zug", "Switzerland"), "lucerne": ("Lucerne", "Switzerland"),
    "lausanne": ("Lausanne", "Switzerland"),
    "london": ("London", "United Kingdom"), "manchester": ("Manchester", "United Kingdom"),
    "birmingham": ("Birmingham", "United Kingdom"), "edinburgh": ("Edinburgh", "United Kingdom"),
    "macclesfield": ("Macclesfield", "United Kingdom"),
    "cheshire": ("Cheshire", "United Kingdom"),
    "leeds": ("Leeds", "United Kingdom"), "bristol": ("Bristol", "United Kingdom"),
    "glasgow": ("Glasgow", "United Kingdom"),
    "vienna": ("Vienna", "Austria"), "wien": ("Wien", "Austria"),
    "brussels": ("Brussels", "Belgium"), "antwerp": ("Antwerp", "Belgium"),
    "new york": ("New York", "United States"), "san francisco": ("San Francisco", "United States"),
    "chicago": ("Chicago", "United States"), "boston": ("Boston", "United States"),
    "los angeles": ("Los Angeles", "United States"), "seattle": ("Seattle", "United States"),
    "armonk": ("Armonk", "United States"),
    "stockholm": ("Stockholm", "Sweden"), "oslo": ("Oslo", "Norway"),
    "copenhagen": ("Copenhagen", "Denmark"), "helsinki": ("Helsinki", "Finland"),
    "tokyo": ("Tokyo", "Japan"), "beijing": ("Beijing", "China"),
    "shanghai": ("Shanghai", "China"), "singapore": ("Singapore", "Singapore"),
    "dublin": ("Dublin", "Ireland"), "warsaw": ("Warsaw", "Poland"),
    "lisbon": ("Lisbon", "Portugal"), "luxembourg": ("Luxembourg", "Luxembourg"),
    # Italian cities (common ones so Italy can be detected via city too)
    "rome": ("Rome", "Italy"), "roma": ("Roma", "Italy"),
    "milan": ("Milan", "Italy"), "milano": ("Milano", "Italy"),
    "turin": ("Turin", "Italy"), "torino": ("Torino", "Italy"),
    "naples": ("Naples", "Italy"), "napoli": ("Napoli", "Italy"),
    "florence": ("Florence", "Italy"), "firenze": ("Firenze", "Italy"),
    "bologna": ("Bologna", "Italy"), "genoa": ("Genoa", "Italy"),
    "genova": ("Genova", "Italy"), "verona": ("Verona", "Italy"),
    "venice": ("Venice", "Italy"), "venezia": ("Venezia", "Italy"),
    "padova": ("Padova", "Italy"), "bergamo": ("Bergamo", "Italy"),
    "brescia": ("Brescia", "Italy"), "modena": ("Modena", "Italy"),
}

_COUNTRY_ALIASES: dict[str, str] = {
    # Full names and safe aliases only — NO 2-letter ISO codes (too short for substring matching)
    "italy": "Italy", "italia": "Italy", "italian": "Italy", "ita": "Italy",
    "germany": "Germany", "deutschland": "Germany", "german": "Germany", "deu": "Germany",
    "france": "France", "french": "France", "fra": "France",
    "spain": "Spain", "españa": "Spain", "spanish": "Spain",
    "netherlands": "Netherlands", "holland": "Netherlands", "dutch": "Netherlands",
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

# Italian province codes → (city, country)
_ITALY_PROVINCE_CODES: dict[str, tuple[str, str]] = {
    "BO": ("Bologna", "Italy"), "BG": ("Bergamo", "Italy"), "BS": ("Brescia", "Italy"),
    "FI": ("Firenze", "Italy"), "GE": ("Genova", "Italy"), "MI": ("Milano", "Italy"),
    "MO": ("Modena", "Italy"), "NA": ("Napoli", "Italy"), "PA": ("Palermo", "Italy"),
    "RM": ("Roma", "Italy"), "TO": ("Torino", "Italy"), "VE": ("Venezia", "Italy"),
    "VR": ("Verona", "Italy"), "VI": ("Vicenza", "Italy"), "TS": ("Trieste", "Italy"),
    "PR": ("Parma", "Italy"), "PC": ("Piacenza", "Italy"), "RE": ("Reggio Emilia", "Italy"),
    "PD": ("Padova", "Italy"), "TV": ("Treviso", "Italy"), "UD": ("Udine", "Italy"),
    "GO": ("Gorizia", "Italy"), "TR": ("Terni", "Italy"), "PG": ("Perugia", "Italy"),
    "AN": ("Ancona", "Italy"), "MC": ("Macerata", "Italy"), "AP": ("Ascoli Piceno", "Italy"),
    "CH": ("Chieti", "Italy"), "AQ": ("L'Aquila", "Italy"), "PE": ("Pescara", "Italy"),
    "SA": ("Salerno", "Italy"), "AV": ("Avellino", "Italy"), "CE": ("Caserta", "Italy"),
    "BA": ("Bari", "Italy"), "TA": ("Taranto", "Italy"), "BR": ("Brindisi", "Italy"),
    "LE": ("Lecce", "Italy"), "CA": ("Cagliari", "Italy"), "CT": ("Catania", "Italy"),
    "ME": ("Messina", "Italy"), "VA": ("Varese", "Italy"), "CO": ("Como", "Italy"),
    "LC": ("Lecco", "Italy"), "SO": ("Sondrio", "Italy"), "CR": ("Cremona", "Italy"),
    "MN": ("Mantova", "Italy"), "PV": ("Pavia", "Italy"), "LO": ("Lodi", "Italy"),
    "MB": ("Monza", "Italy"), "BZ": ("Bolzano", "Italy"), "TN": ("Trento", "Italy"),
    "RO": ("Rovigo", "Italy"), "FE": ("Ferrara", "Italy"), "RA": ("Ravenna", "Italy"),
    "FC": ("Forlì", "Italy"), "RN": ("Rimini", "Italy"), "LU": ("Lucca", "Italy"),
    "PT": ("Pistoia", "Italy"), "PI": ("Pisa", "Italy"), "LI": ("Livorno", "Italy"),
    "GR": ("Grosseto", "Italy"), "SI": ("Siena", "Italy"), "AR": ("Arezzo", "Italy"),
    "PO": ("Prato", "Italy"), "MS": ("Massa", "Italy"), "SP": ("La Spezia", "Italy"),
    "IM": ("Imperia", "Italy"), "SV": ("Savona", "Italy"), "AL": ("Alessandria", "Italy"),
    "AT": ("Asti", "Italy"), "CN": ("Cuneo", "Italy"), "NO": ("Novara", "Italy"),
    "VB": ("Verbania", "Italy"), "VC": ("Vercelli", "Italy"), "BI": ("Biella", "Italy"),
    "AO": ("Aosta", "Italy"), "LT": ("Latina", "Italy"), "FR": ("Frosinone", "Italy"),
    "RI": ("Rieti", "Italy"), "VT": ("Viterbo", "Italy"), "PU": ("Pesaro", "Italy"),
    "PN": ("Pordenone", "Italy"), "BL": ("Belluno", "Italy"), "BN": ("Benevento", "Italy"),
    "IS": ("Isernia", "Italy"), "CB": ("Campobasso", "Italy"), "MT": ("Matera", "Italy"),
    "PZ": ("Potenza", "Italy"), "CS": ("Cosenza", "Italy"), "CZ": ("Catanzaro", "Italy"),
    "KR": ("Crotone", "Italy"), "VV": ("Vibo Valentia", "Italy"), "RC": ("Reggio Calabria", "Italy"),
    "EN": ("Enna", "Italy"), "CL": ("Caltanissetta", "Italy"), "AG": ("Agrigento", "Italy"),
    "RG": ("Ragusa", "Italy"), "SR": ("Siracusa", "Italy"), "SS": ("Sassari", "Italy"),
    "OR": ("Oristano", "Italy"), "NU": ("Nuoro", "Italy"), "OT": ("Olbia", "Italy"),
    "BT": ("Barletta", "Italy"), "FM": ("Fermo", "Italy"),
}

_PROVINCE_CODE_RE = re.compile(
    r"(?:[\(,\s])([A-Z]{2})(?:\)|(?:\s*[-,]\s*(?:Ital(?:y|ia)))|\s*$)",
    re.MULTILINE,
)
_ITALIAN_CAP_RE = re.compile(r"\b([1-9]\d{4})\b")
_ITALIAN_FISCAL_RE = re.compile(
    r"\b(p\.?\s*iva|partita\s+iva|codice\s+fiscale|c\.f\.)\b", re.IGNORECASE
)

# ── Pattern sets (mirrors proven logic from probe app) ───────────────────────

_OFFICIAL_HQ_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"its?\s+head\s+office\s+is\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n|\Z)", re.I), "Strong"),
    (re.compile(r"head\s+office\s+(?:is\s+)?(?:located\s+)?in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|,\s*(?:with|which)|\n|\Z)", re.I), "Strong"),
    (re.compile(r"headquartered\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n|\Z)", re.I), "Strong"),
    (re.compile(r"headquarters\s+(?:are\s+)?(?:is\s+)?(?:located\s+)?in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n|\Z)", re.I), "Strong"),
    (re.compile(r"corporate\s+headquarters\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n|\Z)", re.I), "Strong"),
    (re.compile(r"group\s+headquarters\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n|\Z)", re.I), "Strong"),
    (re.compile(r"operational\s+headquarters\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n|\Z)", re.I), "Strong"),
    (re.compile(r"registered\s+office\s+(?:in|at|:)\s*([A-Za-zÀ-ÿ\s,\(\)]{3,60}?)(?:\.|;|\n|\Z)", re.I), "Strong"),
    (re.compile(r"sede\s+(?:principale|legale|centrale)\s*[:\s]+(?:in\s+)?([A-Za-zÀ-ÿ\s,]{3,50}?)(?:\.|;|,|\n)", re.I), "Strong"),
    (re.compile(r"parent\s+company\s+is\s+([A-Za-zÀ-ÿ\s,]{3,60}?)(?:\.|;|\n)", re.I), "Medium"),
    (re.compile(r"part\s+of\s+the\s+([A-Za-zÀ-ÿ\s]{3,40}?)\s+group\b", re.I), "Medium"),
    (re.compile(r"subsidiary\s+of\s+([A-Za-zÀ-ÿ\s,]{3,60}?)(?:\.|;|\n)", re.I), "Medium"),
    (re.compile(r"owned\s+by\s+([A-Za-zÀ-ÿ\s,]{3,60}?)(?:\.|;|,|\n)", re.I), "Medium"),
    (re.compile(r"based\s+in\s+([A-Za-zÀ-ÿ\s,\(\)]{3,50}?)(?:\.|;|,\s*(?:and|with|since)|\n)", re.I), "Medium"),
    (re.compile(r"\bheadquarters\s+([A-Z][A-Za-zÀ-ÿ\s]{2,40}?)(?:\s+(?:Type|Founded|Employees?|Industry|Revenue|CEO|Chairman|Website|Phone|Email)|[,;.]|\s*$)", re.I), "Medium"),
]

# Regional / divisional HQ — must NOT count as global parent HQ
_REGIONAL_HQ_RE = re.compile(
    r"\b(?:"
    r"(?:north\s*america[n]?|na|emea|apac|latam|asia[\s\-]pacific|european?|"
    r"regional?|division[al]?|segment|us|usa?)\s+(?:headquarters?|hq)|"
    r"headquarters?\s+for\s+(?:north\s*america[n]?|emea|apac|europe|us|usa?)|"
    r"usa?\s+corp(?:oration)?\s+headquarters?|"
    r"branch\s+(?:office|headquarters?)|local\s+(?:office|headquarters?)|"
    r"subsidiary\s+headquarters?|affiliate\s+headquarters?|"
    r"italy\s+headquarters?|italian\s+headquarters?|"
    r"sales\s+(?:office|headquarters?|hq)|"
    r"service\s+(?:center|centre|branch|headquarters?|hq)"
    r")\b",
    re.IGNORECASE,
)

# Subsidiary / branch phrases — downgrade to Low confidence + manual review
_SUBSIDIARY_SIGNAL_RE = re.compile(
    r"\b(?:usa?\s+corp(?:oration)?|subsidiary|affiliate|"
    r"branch\s+(?:office|headquarters?)|local\s+(?:office|headquarters?)|"
    r"regional\s+headquarters?|division\s+headquarters?)\b",
    re.IGNORECASE,
)

# Score-3 eligibility: evidence must contain a genuine global HQ phrase
_S3_HQ_PHRASE_RE = re.compile(
    r"\b(?:headquarters?|headquartered|head\s+office|global\s+hq|"
    r"corporate\s+(?:headquarters?|hq)|group\s+(?:headquarters?|hq)|"
    r"international\s+(?:headquarters?|hq)|world\s+headquarters?|"
    r"principal\s+(?:office|place\s+of\s+business))\b",
    re.IGNORECASE,
)

# Hard-reject patterns for score-3 eligibility
_S3_BRANCH_REJECT_RE = re.compile(
    r"\b(?:branch\s+(?:in|opens?|office|at)|office\s+in|regional\s+office|"
    r"sales\s+(?:office|headquarters?|hq)|"
    r"service\s+(?:center|centre|branch|headquarters?|hq)|"
    r"distribution\s+(?:center|centre)|"
    r"north\s+america(?:n)?(?:\s+(?:hq|inc\.?|headquarters?))?|"
    r"us[a]?\s+subsidiary|subsidiary|affiliate|"
    r"get\s+directions?|locations\s+primary|primary\s+location)\b",
    re.IGNORECASE,
)

_HQ_SIGNAL_RE = re.compile(
    r"\b(?:head\s+office|headquarters?|hq)\b", re.IGNORECASE
)

_LOCATION_SECTION_RE = re.compile(
    r"\b(?:locations?|get\s+directions?|other\s+offices?|all\s+offices?|"
    r"offices?\s+worldwide|global\s+locations?|primary\s+location)\b",
    re.IGNORECASE,
)

# Known directory / social domains (evidence from these gets lower trust)
_DIRECTORY_DOMAINS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "crunchbase.com", "bloomberg.com",
    "zoominfo.com", "dnb.com", "hoovers.com", "glassdoor.com", "indeed.com",
    "infobel.com", "paginegialle.it", "kompass.com", "europages.com",
    "europages.it", "registroimprese.it", "atoka.io", "cerved.com",
    "bizjournals.com", "wsj.com", "ft.com", "reuters.com",
    "yelp.com", "foursquare.com", "angellist.com", "pitchbook.com",
    "owler.com", "manta.com", "opencorporates.com", "sec.gov",
})


# ── Core helpers (no I/O) ────────────────────────────────────────────────────

def _std_country(raw: str) -> str:
    return _COUNTRY_ALIASES.get(raw.strip().lower(), raw.strip())


def _normalize_country_for_hq(value: object) -> str:
    """Lowercase canonical country key — handles ISO-2, ISO-3, full names."""
    _MAP = {
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
    }
    text = re.sub(r"\s+", " ", re.sub(r"\.", "", str(value or "").strip().lower()))
    return _MAP.get(text, text)


def _resolve_city_country(text: str) -> tuple[str, str]:
    """Return (city, country) from a free-form location string."""
    text_lc = text.lower().strip(" ,.")
    # International cities first
    for alias, (city, country) in _INTL_CITIES.items():
        if alias in text_lc:
            return city, country
    # Italian province codes
    for m in _PROVINCE_CODE_RE.finditer(text):
        code = m.group(1)
        if code in _ITALY_PROVINCE_CODES:
            return _ITALY_PROVINCE_CODES[code]
    # Italian postal code (5-digit starting with non-zero)
    if _ITALIAN_CAP_RE.search(text):
        return "", "Italy"
    # Italian fiscal/VAT marker
    if _ITALIAN_FISCAL_RE.search(text):
        return "", "Italy"
    # Country aliases
    for alias, country in _COUNTRY_ALIASES.items():
        if alias in text_lc:
            return "", country
    return "", ""


def _is_directory_url(url: str) -> bool:
    try:
        host = _urlparse(url).netloc.lower()
        host = re.sub(r"^www\.", "", host)
        return any(host == d or host.endswith("." + d) for d in _DIRECTORY_DOMAINS)
    except Exception:
        return False


def _get_domain_netloc(url_or_domain: str) -> str:
    """Return bare netloc (no www.) from a URL or domain string."""
    s = url_or_domain.strip()
    try:
        parsed = _urlparse(s if "://" in s else "https://" + s)
        host = re.sub(r"^www\.", "", parsed.netloc.lower())
    except Exception:
        host = re.sub(r"^www\.", "", s.lower()).split("/")[0]
    return host


def _scan_for_hq(text: str) -> tuple[str, str, str, str]:
    """Scan text with OFFICIAL_HQ_PATTERNS. Returns (quote, country, city, strength)."""
    for pat, strength in _OFFICIAL_HQ_PATTERNS:
        m = pat.search(text)
        if m:
            captured = m.group(1).strip(" ,.()")
            city, country = _resolve_city_country(captured)
            if country or city:
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 80)
                quote = text[start:end].strip()
                return quote[:300], country, city, strength
    return "", "", "", ""


def _scan_hq_title_snippet(organic: list[dict]) -> tuple[str, str, str, str]:
    """Scan organic result titles/snippets for HQ evidence using proximity.

    Regional HQ guard: skip results whose only HQ signal is regional.
    Returns (quote, country, city, strength="Medium").
    """
    for item in organic[:5]:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        combined = f"{title} {snippet}"

        sig_m = _HQ_SIGNAL_RE.search(combined)
        if not sig_m:
            continue

        # Skip purely regional HQ signals
        remainder = re.sub(_REGIONAL_HQ_RE.pattern, "", combined, flags=re.IGNORECASE)
        if _REGIONAL_HQ_RE.search(combined) and not _HQ_SIGNAL_RE.search(remainder):
            continue

        sig_end = sig_m.end()
        window = combined[sig_end: sig_end + 80]
        loc_boundary = _LOCATION_SECTION_RE.search(window)
        if loc_boundary:
            window = window[:loc_boundary.start()]

        sig_start = sig_m.start()
        pre_window = combined[max(0, sig_start - 60): sig_start]

        for chunk in (window, pre_window):
            chunk = chunk.strip(" ,-–")
            if not chunk:
                continue
            city, country = _resolve_city_country(chunk)
            if country or city:
                return combined[:300].strip(), country, city, "Medium"

    return "", "", "", ""


def _pick_best_evidence_url(
    organic: list[dict],
    input_domain: Optional[str],
) -> str:
    """Return the best non-directory evidence URL: prefer official domain."""
    input_netloc = _get_domain_netloc(input_domain) if input_domain else ""

    # Prefer official domain result
    for r in organic:
        link = r.get("link", "")
        if not link or _is_directory_url(link):
            continue
        netloc = _get_domain_netloc(link)
        if input_netloc and (netloc == input_netloc or netloc.endswith("." + input_netloc)):
            return link

    # Fall back to first non-directory result
    for r in organic:
        link = r.get("link", "")
        if link and not _is_directory_url(link):
            return link
    return ""


def _is_score3_eligible(
    quote: str,
    detected_country: str,
    input_country: str,
    evidence_url: str,
) -> tuple[bool, str]:
    """Return (eligible, reason). Score-3 requires a clear HQ phrase and no hard-reject signals."""
    if not (detected_country or "").strip():
        return False, "no_detected_country"
    det_n = _normalize_country_for_hq(detected_country)
    inp_n = _normalize_country_for_hq(input_country)
    if det_n and inp_n and det_n == inp_n:
        return False, "domestic_hq_not_foreign"
    if _S3_BRANCH_REJECT_RE.search(quote or ""):
        return False, "branch_subsidiary_or_regional_hq"
    if not _S3_HQ_PHRASE_RE.search(quote or ""):
        return False, "no_clear_hq_phrase_in_evidence"
    return True, ""


# ── Main dormant parser function ─────────────────────────────────────────────

def detect_hq_from_serper_payload(
    *,
    company_name: str,
    domain: Optional[str],
    input_country: str,
    serper_payload: dict,
) -> "HQDetectionResult":
    """Parse a Serper search-result payload and return an HQ detection result.

    Pure function — no live API calls, no I/O.

    Uses ``build_simple_hq_query`` only to populate ``domain_root`` and
    ``query_used`` on the result; the actual payload must already be supplied
    by the caller (Step 2C wiring will handle the live call).

    Priority order for evidence sources:
        knowledgeGraph > answerBox > organic[0..4] (pattern then proximity) > places

    Guards applied:
    - Regional HQ (EMEA, NA, Italy HQ, sales office, …) → needs_manual_review
    - Unrelated domain (evidence URL not from input domain, not a known directory,
      not a known global brand) → needs_manual_review, confidence Low
    - Subsidiary / branch signal in quote → needs_manual_review, confidence Low
    - No clear HQ phrase → needs_manual_review, score 0
    """
    from lead_output_schema import HQDetectionResult  # local import keeps module import-safe

    domain_root, query_used = build_simple_hq_query(company_name, domain)

    # Defensive — must be a dict
    payload = serper_payload if isinstance(serper_payload, dict) else {}

    kg: dict = payload.get("knowledgeGraph") or {}
    kg_loc: str = (
        kg.get("address", "")
        or kg.get("headquarters", "")
        or kg.get("location", "")
        or ""
    )
    ab: dict = payload.get("answerBox") or {}
    ab_text: str = (ab.get("answer", "") or ab.get("snippet", "") or "")
    organic: list[dict] = payload.get("organic") or []
    places: list[dict] = payload.get("places") or payload.get("local") or []

    input_country_std = _std_country(input_country or "")
    input_netloc = _get_domain_netloc(domain) if domain else ""

    # ── Scan sources in priority order; first hit wins ───────────────────────
    quote = country = city = strength = ""
    parser_source = ""

    def _try(text: str, label: str) -> bool:
        nonlocal quote, country, city, strength, parser_source
        q, co, ci, st = _scan_for_hq(text)
        if co or ci:
            quote, country, city, strength, parser_source = q, co, ci, st, label
            return True
        return False

    if kg_loc:
        # First try explicit HQ phrase patterns
        if not _try(kg_loc, "knowledge_graph"):
            # KG headquarters/address is often a bare location string like "Armonk, New York, US".
            # Try direct city/country lookup on the raw value.
            # Prefix the quote with "Headquarters: " so score-3 eligibility phrase check passes.
            kg_city, kg_country = _resolve_city_country(kg_loc)
            if kg_country or kg_city:
                quote = f"Headquarters: {kg_loc[:280]}"
                country, city = kg_country, kg_city
                strength, parser_source = "Strong", "knowledge_graph"

    if not (country or city) and ab_text:
        _try(ab_text, "answer_box")

    for rank_i, item in enumerate(organic[:5], start=1):
        if country or city:
            break
        item_text = f"{item.get('title', '')} {item.get('snippet', '')}"
        if not _try(item_text, f"organic_{rank_i}"):
            # Proximity fallback for this single result
            fb_q, fb_co, fb_ci, fb_st = _scan_hq_title_snippet([item])
            if fb_co or fb_ci:
                quote, country, city = fb_q, fb_co, fb_ci
                strength, parser_source = fb_st, f"organic_{rank_i}_proximity"

    if not (country or city):
        for rank_p, place in enumerate(places[:3], start=1):
            place_text = f"{place.get('title', '')} {place.get('address', '')}"
            if _try(place_text, f"places_{rank_p}"):
                break

    # ── No evidence found ────────────────────────────────────────────────────
    if not (country or city):
        return HQDetectionResult(
            domain_root=domain_root,
            query_used=query_used,
            parser_source="none",
            hq_reason="no_hq_evidence_found",
            needs_manual_review=True,
            sig_foreign_hq_score_for_next_scoring=0.0,
        )

    # ── Country normalisation ────────────────────────────────────────────────
    country_std = _std_country(country)

    # ── Regional HQ guard ────────────────────────────────────────────────────
    if _REGIONAL_HQ_RE.search(quote):
        return HQDetectionResult(
            domain_root=domain_root,
            query_used=query_used,
            parser_source=parser_source,
            hq_detected_country=country_std,
            hq_detected_city=city,
            hq_confidence="Low",
            hq_reason="regional_hq_guard",
            hq_evidence_quote=quote,
            needs_manual_review=True,
            sig_foreign_hq_score_for_next_scoring=0.0,
        )

    # ── Pick best evidence URL ───────────────────────────────────────────────
    best_url = _pick_best_evidence_url(organic, domain)

    # ── Directory-only evidence guard ────────────────────────────────────────
    # If every organic link is a known directory domain (linkedin, zoominfo, …)
    # and no non-directory URL could be found, the evidence is untrustworthy.
    organic_links = [r.get("link", "") for r in organic if r.get("link", "")]
    all_links_are_dirs = bool(
        organic_links
        and all(_is_directory_url(lnk) for lnk in organic_links)
    )

    # ── Domain mismatch guard ────────────────────────────────────────────────
    domain_mismatch = False
    if best_url and input_netloc:
        evidence_netloc = _get_domain_netloc(best_url)
        official = evidence_netloc == input_netloc or evidence_netloc.endswith("." + input_netloc)
        is_dir = _is_directory_url(best_url)
        domain_mismatch = not official and not is_dir

    # ── Subsidiary / branch signal in evidence ───────────────────────────────
    is_subsidiary = bool(_SUBSIDIARY_SIGNAL_RE.search(quote))

    # ── Determine confidence ─────────────────────────────────────────────────
    if strength == "Strong":
        confidence = "High"
    elif strength == "Medium":
        confidence = "Medium"
    else:
        confidence = "Low"

    needs_review = False
    trust_reason = ""
    if all_links_are_dirs:
        confidence = "Low"
        needs_review = True
        trust_reason = "directory_only_evidence"
    if domain_mismatch:
        confidence = "Low"
        needs_review = True
        trust_reason = (trust_reason + ";unrelated_domain") if trust_reason else "unrelated_domain"
    if is_subsidiary:
        confidence = "Low"
        needs_review = True
        trust_reason = (trust_reason + ";subsidiary_or_regional_hq") if trust_reason else "subsidiary_or_regional_hq"

    # ── Is HQ foreign? ───────────────────────────────────────────────────────
    det_norm = _normalize_country_for_hq(country_std)
    inp_norm = _normalize_country_for_hq(input_country_std)
    is_foreign = bool(det_norm and inp_norm and det_norm != inp_norm)

    if is_foreign:
        s3_ok, s3_reason = _is_score3_eligible(
            quote=quote,
            detected_country=country_std,
            input_country=input_country_std,
            evidence_url=best_url,
        )
        if s3_ok and not needs_review:
            score = 3.0
            foreign_hq = True
        elif s3_ok and needs_review:
            # Evidence suggests foreign HQ but trust is low — manual review
            score = 0.0
            foreign_hq = True
        else:
            # Missing HQ phrase or hard-reject → conservative
            score = 0.0
            foreign_hq = True
            needs_review = True
            trust_reason = (trust_reason + ";" + s3_reason) if trust_reason else s3_reason
    else:
        # Domestic or unknown
        score = 0.0
        foreign_hq = False if det_norm else None

    hq_reason_parts = [f"[simple-hq:{query_used}]", quote[:150]]
    if trust_reason:
        hq_reason_parts.append(f"({trust_reason})")

    return HQDetectionResult(
        domain_root=domain_root,
        query_used=query_used,
        parser_source=parser_source,
        hq_detected_country=country_std or None,
        hq_detected_city=city or None,
        hq_confidence=confidence,
        foreign_hq_simple=foreign_hq,
        needs_manual_review=needs_review,
        hq_reason=" ".join(hq_reason_parts),
        hq_evidence_url=best_url or None,
        hq_evidence_quote=quote or None,
        sig_foreign_hq_score_for_next_scoring=score,
    )
