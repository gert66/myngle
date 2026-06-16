"""
mYngle · LinkedIn Signal Demo
==============================
Public growth and hiring signal extractor using Serper Google Search,
optional Firecrawl website crawls, and Claude extraction.

This demo does NOT crawl LinkedIn directly, use cookies, browser automation,
or bypass any access controls.  It only uses public Google search snippets
that reference LinkedIn URLs.

Entry point:
    streamlit run linkedin_signal_demo.py

Secrets (.streamlit/secrets.toml):
    SERPER_API_KEY      = "..."
    ANTHROPIC_API_KEY   = "..."
    FIRECRAWL_API_KEY   = "..."   # optional
"""

import hashlib
import io
import json
import pathlib
import re
import time
from datetime import datetime

import anthropic
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_TITLE      = "mYngle · LinkedIn Signal Demo"
CACHE_DIR      = pathlib.Path("linkedin_signal_cache")
CACHE_VERSION  = "v1"
SERPER_URL     = "https://google.serper.dev/search"
DEFAULT_MAX    = 4
HARD_CAP       = 10

CAREERS_PATHS = [
    "/careers", "/jobs", "/career",
    "/lavora-con-noi", "/posizioni-aperte",
]

SIGNAL_SUMMARY_COLS = [
    "company_name", "company_url",
    "linkedin_url_given", "linkedin_url_found",
    "hiring_signal", "hiring_evidence", "hiring_source_url", "open_roles_hint",
    "growth_signal", "growth_evidence", "growth_source_url", "employee_growth_hint",
    "recent_activity_hint", "linkedin_signal_summary", "website_signal_summary",
    "confidence", "suggested_call_angle", "evidence_gaps",
]

COLD_CALLER_COLS = [
    "company_name", "company_url",
    "top_signal", "why_now", "suggested_call_angle",
    "evidence_1", "evidence_1_url",
    "evidence_2", "evidence_2_url",
    "confidence",
]

# Search query templates: (label, template)
# {name} and {domain} are substituted per company.
QUERY_TEMPLATES = [
    ("linkedin_company_domain",    'site:linkedin.com/company "{name}" "{domain}"'),
    ("linkedin_company_hiring",    'site:linkedin.com/company "{name}" hiring'),
    ("linkedin_company_jobs",      'site:linkedin.com/company "{name}" jobs'),
    ("linkedin_company_employees", 'site:linkedin.com/company "{name}" employees'),
    ("linkedin_posts_hiring",      'site:linkedin.com/posts "{name}" hiring'),
    ("linkedin_posts_growth",      'site:linkedin.com/posts "{name}" expansion OR growth OR hiring'),
    ("careers_general",            '"{name}" careers'),
    ("jobs_general",               '"{name}" jobs'),
    ("hiring_general",             '"{name}" hiring'),
    ("lavora_con_noi",             '"{name}" "lavora con noi"'),
    ("posizioni_aperte",           '"{name}" "posizioni aperte"'),
    ("expansion",                  '"{name}" expansion'),
    ("new_office",                 '"{name}" "new office"'),
    ("acquisition",                '"{name}" acquisition'),
    ("funding",                    '"{name}" funding'),
]

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Helpers — domain normalisation
# ---------------------------------------------------------------------------


def normalize_domain(raw: str) -> str:
    """https://www.example.com/path  →  example.com"""
    if not raw:
        return ""
    raw = raw.strip()
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = raw.split("/")[0].split("?")[0]
    if raw.lower().startswith("www."):
        raw = raw[4:]
    return raw.lower().strip()


def base_url(raw: str) -> str:
    """Return scheme+host only: https://example.com"""
    raw = raw.strip()
    if not re.match(r"https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    m = re.match(r"(https?://[^/]+)", raw, re.IGNORECASE)
    return m.group(1).rstrip("/") if m else raw


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(name: str, domain: str) -> str:
    raw = f"{CACHE_VERSION}|{name.lower().strip()}|{domain.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


def cache_load(name: str, domain: str) -> dict | None:
    CACHE_DIR.mkdir(exist_ok=True)
    p = CACHE_DIR / f"{_cache_key(name, domain)}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def cache_save(name: str, domain: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    p = CACHE_DIR / f"{_cache_key(name, domain)}.json"
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Serper search
# ---------------------------------------------------------------------------


def serper_search(query: str, api_key: str, n: int = 5) -> list[dict]:
    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": n},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        out = []
        for item in data.get("organic", [])[:n]:
            out.append({
                "title":   item.get("title", ""),
                "link":    item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "date":    item.get("date", ""),
            })
        return out
    except Exception as exc:
        return [{"title": "", "link": "", "snippet": f"[search error: {exc}]", "date": ""}]


def run_all_searches(name: str, domain: str, serper_key: str) -> dict[str, list[dict]]:
    """Run all query templates; return {label: [results]}."""
    grouped: dict[str, list[dict]] = {}
    for label, template in QUERY_TEMPLATES:
        query = template.format(name=name, domain=domain)
        grouped[label] = serper_search(query, serper_key, n=5)
        time.sleep(0.25)
    return grouped


def search_results_to_text(grouped: dict[str, list[dict]]) -> str:
    parts = []
    for label, results in grouped.items():
        parts.append(f"=== {label.upper()} ===")
        if not results:
            parts.append("(no results)\n")
            continue
        for i, r in enumerate(results, 1):
            line = f"[{i}] {r['title']}"
            if r.get("date"):
                line += f"  ({r['date']})"
            line += f"\n    {r['link']}"
            if r.get("snippet"):
                line += f"\n    {r['snippet']}"
            parts.append(line)
        parts.append("")
    return "\n".join(parts)


def find_linkedin_url_in_results(grouped: dict[str, list[dict]]) -> str:
    """Return the first linkedin.com/company/... URL found in search results."""
    for results in grouped.values():
        for r in results:
            link = r.get("link", "")
            if "linkedin.com/company/" in link.lower():
                return link
    return ""


# ---------------------------------------------------------------------------
# Firecrawl website crawl
# ---------------------------------------------------------------------------


# Patterns that indicate a login/auth wall in crawled page text
_LOGIN_WALL_PATTERNS = [
    r"please[,\s]+log\s*in",
    r"user\s*name\s*password",
    r"the username and/or password you entered is invalid",
    r"i forgot my password",
    r"\blogin\b",
    r"sign\s+in\s+to\s+continue",
    r"you must be logged in",
]
_LOGIN_WALL_RE = re.compile(
    "|".join(_LOGIN_WALL_PATTERNS), re.IGNORECASE
)


def _is_login_wall(text: str) -> bool:
    return bool(_LOGIN_WALL_RE.search(text))


def firecrawl_scrape(url: str, api_key: str) -> tuple[str, str]:
    """
    Scrape a single URL with Firecrawl.
    Returns (extracted_text, error_message).
    Only used for the company's own public website and careers pages.
    Does NOT crawl linkedin.com or any gated resource.
    """
    if not api_key:
        return "", "No Firecrawl API key"
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            md = data.get("data", {}).get("markdown", "") or ""
            return md[:4000], ""  # cap to avoid huge prompts
        return "", f"HTTP {resp.status_code}"
    except Exception as exc:
        return "", str(exc)


def crawl_company_website(
    company_url: str, firecrawl_key: str, homepage_only: bool = False
) -> list[dict]:
    """
    Crawl company homepage + (optionally) standard careers paths.
    Returns list of {url_attempted, status, extracted_text_preview, error}.
    status is 'ok', 'error', or 'login_wall'.
    Login-wall pages are flagged and excluded from the evidence text sent to Claude.
    """
    crawls: list[dict] = []
    if not company_url or not firecrawl_key:
        return crawls

    urls_to_try = [company_url.rstrip("/")]
    if not homepage_only:
        base = base_url(company_url)
        for path in CAREERS_PATHS:
            urls_to_try.append(base + path)

    for url in urls_to_try:
        text, err = firecrawl_scrape(url, firecrawl_key)
        if text and _is_login_wall(text):
            status = "login_wall"
            preview = text[:500]
        elif text:
            status = "ok"
            preview = text[:500]
        else:
            status = "error"
            preview = ""
        crawls.append({
            "url_attempted":          url,
            "status":                 status,
            "extracted_text_preview": preview,
            "error":                  err,
        })
        time.sleep(0.2)
    return crawls


def crawls_to_text(crawls: list[dict]) -> str:
    """
    Serialise crawl results for the Claude prompt.
    Login-wall pages are noted as blocked — not included as evidence.
    """
    parts = []
    for c in crawls:
        status = c.get("status", "")
        parts.append(f"URL: {c['url_attempted']}  [{status}]")
        if status == "login_wall":
            parts.append("  [LOGIN WALL — page is gated, not usable as hiring/growth evidence]")
        elif c.get("error"):
            parts.append(f"  Error: {c['error']}")
        elif c.get("extracted_text_preview"):
            parts.append(c["extracted_text_preview"])
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """\
You are a B2B sales intelligence analyst for mYngle, which sells online language \
training and business communication support to international companies.

You have been given Google search snippets, optional website crawl text, and \
optionally some LinkedIn notes manually provided by the user. Your job is to \
extract factual hiring and growth signals for the company listed below.

IMPORTANT RULES:
- Only report signals explicitly supported by the provided text.
- Do not infer growth from generic company descriptions.
- Do not invent employee counts or job counts.
- If evidence is missing, use "Unknown" not "No".
- For Yes or Weak signals, always include the exact snippet or sentence as evidence.
- Prefer conservative scoring. When in doubt, use Weak or Unknown.
- "linkedin_url_found" should be a linkedin.com/company URL from the search results, or "".
- hiring_signal / growth_signal must be one of: Yes / Weak / No / Unknown
- confidence must be one of: High / Medium / Low
- Do NOT treat login-wall pages (marked [LOGIN WALL]) as evidence of hiring or growth.
- Do NOT infer current hiring from LinkedIn snippets that have no visible date. \
  If a LinkedIn snippet has no date, note "timing unclear" in the evidence field.
- Keep confidence LOW when all evidence comes only from search snippets \
  with no corroborating website text.
- suggested_call_angle must be phrased as a practical cold-caller opening sentence \
  (e.g. "I saw you are expanding into Germany — we help teams like yours get up to \
  speed in Business English quickly."). Do NOT write a general business summary.

TODAY: {today}
COMPANY NAME: {name}
COMPANY URL: {url}
LINKEDIN URL PROVIDED BY USER: {linkedin_url_given}
DOMAIN: {domain}

=== GOOGLE SEARCH SNIPPETS ===
{search_text}

=== WEBSITE CRAWL TEXT ===
{website_text}

=== USER-PROVIDED LINKEDIN NOTES ===
{linkedin_notes}

Respond with ONLY a valid JSON object matching this exact schema:
{{
  "company_name": "",
  "company_url": "",
  "linkedin_url_given": "",
  "linkedin_url_found": "",
  "hiring_signal": "Yes|Weak|No|Unknown",
  "hiring_evidence": "",
  "hiring_source_url": "",
  "open_roles_hint": "",
  "growth_signal": "Yes|Weak|No|Unknown",
  "growth_evidence": "",
  "growth_source_url": "",
  "employee_growth_hint": "",
  "recent_activity_hint": "",
  "linkedin_signal_summary": "",
  "website_signal_summary": "",
  "suggested_call_angle": "",
  "confidence": "High|Medium|Low",
  "evidence_gaps": ""
}}
"""


def extract_signals(
    name: str,
    url: str,
    domain: str,
    linkedin_url_given: str,
    linkedin_notes: str,
    search_text: str,
    website_text: str,
    anthropic_key: str,
) -> dict:
    prompt = EXTRACTION_PROMPT.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        name=name,
        url=url,
        domain=domain,
        linkedin_url_given=linkedin_url_given or "",
        search_text=search_text or "(none)",
        website_text=website_text or "(none)",
        linkedin_notes=linkedin_notes or "(none)",
    )
    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError:
        return _empty_signal(name, url, linkedin_url_given, error="JSON parse error from Claude")
    except Exception as exc:
        return _empty_signal(name, url, linkedin_url_given, error=str(exc))


def _empty_signal(name: str, url: str, linkedin_url_given: str, error: str = "") -> dict:
    return {
        "company_name": name,
        "company_url": url,
        "linkedin_url_given": linkedin_url_given,
        "linkedin_url_found": "",
        "hiring_signal": "Unknown",
        "hiring_evidence": error,
        "hiring_source_url": "",
        "open_roles_hint": "",
        "growth_signal": "Unknown",
        "growth_evidence": "",
        "growth_source_url": "",
        "employee_growth_hint": "",
        "recent_activity_hint": "",
        "linkedin_signal_summary": "",
        "website_signal_summary": "",
        "suggested_call_angle": "",
        "confidence": "Low",
        "evidence_gaps": "Extraction failed",
    }


# ---------------------------------------------------------------------------
# Per-company pipeline
# ---------------------------------------------------------------------------


def process_company(
    row: dict,
    serper_key: str,
    anthropic_key: str,
    firecrawl_key: str,
    force_fresh: bool = False,
    homepage_only: bool = False,
) -> tuple[dict, list[dict], list[dict]]:
    """
    Process one company.  Returns:
        (signal_dict, raw_search_rows, raw_crawl_rows)
    """
    name             = str(row.get("company_name", "")).strip()
    url              = str(row.get("company_url", "")).strip()
    linkedin_given   = str(row.get("linkedin_url", "") or "").strip()
    linkedin_notes   = str(row.get("linkedin_notes", "") or "").strip()
    domain           = normalize_domain(url)

    if not name:
        return _empty_signal("(empty)", url, linkedin_given, "Missing company_name"), [], []

    # Cache check
    if not force_fresh:
        cached = cache_load(name, domain)
        if cached:
            return (
                cached.get("signal", {}),
                cached.get("search_rows", []),
                cached.get("crawl_rows", []),
            )

    # 1. Serper searches
    grouped = run_all_searches(name, domain, serper_key)
    search_text = search_results_to_text(grouped)
    linkedin_found = find_linkedin_url_in_results(grouped)

    # Build raw search rows
    raw_search: list[dict] = []
    for label, results in grouped.items():
        template = dict(QUERY_TEMPLATES).get(label, "")
        query = template.format(name=name, domain=domain)
        for r in results:
            raw_search.append({
                "company_name": name,
                "query":        query,
                "title":        r.get("title", ""),
                "snippet":      r.get("snippet", ""),
                "link":         r.get("link", ""),
                "source_type":  "linkedin" if "linkedin.com" in r.get("link", "") else "web",
            })

    # 2. Firecrawl (own website only — never linkedin.com)
    raw_crawls: list[dict] = []
    website_text = ""
    if firecrawl_key and url:
        crawls = crawl_company_website(url, firecrawl_key, homepage_only=homepage_only)
        for c in crawls:
            raw_crawls.append({"company_name": name, **c})
        website_text = crawls_to_text(crawls)

    # 3. Claude extraction
    signal = extract_signals(
        name=name,
        url=url,
        domain=domain,
        linkedin_url_given=linkedin_given,
        linkedin_notes=linkedin_notes,
        search_text=search_text,
        website_text=website_text,
        anthropic_key=anthropic_key,
    )
    # Ensure company fields are always present
    signal.setdefault("company_name", name)
    signal.setdefault("company_url", url)
    signal.setdefault("linkedin_url_given", linkedin_given)
    if not signal.get("linkedin_url_found") and linkedin_found:
        signal["linkedin_url_found"] = linkedin_found

    # Save cache
    cache_save(name, domain, {
        "signal": signal,
        "search_rows": raw_search,
        "crawl_rows": raw_crawls,
    })

    return signal, raw_search, raw_crawls


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

_HEADER_FILL   = PatternFill("solid", fgColor="1F3864")
_HEADER_FONT   = Font(bold=True, color="FFFFFF")
_SUBHEAD_FILL  = PatternFill("solid", fgColor="D9E1F2")
_SUBHEAD_FONT  = Font(bold=True)


def _style_header_row(ws, row: int = 1) -> None:
    for cell in ws[row]:
        cell.fill  = _HEADER_FILL
        cell.font  = _HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _autofit(ws, max_width: int = 60) -> None:
    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells)
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(
            length + 4, max_width
        )


def _freeze(ws) -> None:
    ws.freeze_panes = ws["A2"]


def build_excel(
    signals: list[dict],
    raw_search: list[dict],
    raw_crawls: list[dict],
) -> bytes:
    wb = openpyxl.Workbook()

    # ── Sheet 1: Signal Summary ──────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Signal Summary"
    if signals:
        df1 = pd.DataFrame(signals)
        for col in SIGNAL_SUMMARY_COLS:
            if col not in df1.columns:
                df1[col] = ""
        df1 = df1[SIGNAL_SUMMARY_COLS]
        ws1.append(list(df1.columns))
        _style_header_row(ws1)
        for _, r in df1.iterrows():
            ws1.append([str(v) if v is not None else "" for v in r])
    _autofit(ws1)
    _freeze(ws1)

    # ── Sheet 2: Raw Search Results ──────────────────────────────────────────
    ws2 = wb.create_sheet("Raw Search Results")
    search_cols = ["company_name", "query", "title", "snippet", "link", "source_type"]
    if raw_search:
        df2 = pd.DataFrame(raw_search)
        for col in search_cols:
            if col not in df2.columns:
                df2[col] = ""
        df2 = df2[search_cols]
        ws2.append(list(df2.columns))
        _style_header_row(ws2)
        for _, r in df2.iterrows():
            ws2.append([str(v) if v is not None else "" for v in r])
    else:
        ws2.append(search_cols)
        _style_header_row(ws2)
    _autofit(ws2)
    _freeze(ws2)

    # ── Sheet 3: Raw Website Crawls ──────────────────────────────────────────
    ws3 = wb.create_sheet("Raw Website Crawls")
    crawl_cols = ["company_name", "url_attempted", "status", "extracted_text_preview", "error"]
    if raw_crawls:
        df3 = pd.DataFrame(raw_crawls)
        for col in crawl_cols:
            if col not in df3.columns:
                df3[col] = ""
        df3 = df3[crawl_cols]
        ws3.append(list(df3.columns))
        _style_header_row(ws3)
        for _, r in df3.iterrows():
            ws3.append([str(v) if v is not None else "" for v in r])
    else:
        ws3.append(crawl_cols)
        _style_header_row(ws3)
    _autofit(ws3)
    _freeze(ws3)

    # ── Sheet 4: Cold Caller Input ───────────────────────────────────────────
    ws4 = wb.create_sheet("Cold Caller Input")

    def _trim(text: str, max_len: int) -> str:
        text = str(text or "").strip()
        return text[:max_len] + "…" if len(text) > max_len else text

    cold_rows = []
    for s in signals:
        hiring_ev  = s.get("hiring_evidence", "")
        growth_ev  = s.get("growth_evidence", "")
        hiring_url = s.get("hiring_source_url", "")
        growth_url = s.get("growth_source_url", "")
        top_signal = ""
        if s.get("hiring_signal") in ("Yes", "Weak"):
            top_signal = f"Hiring: {s.get('open_roles_hint', '')}".strip()
        elif s.get("growth_signal") in ("Yes", "Weak"):
            top_signal = f"Growth: {s.get('employee_growth_hint', '')}".strip()
        why_now = s.get("recent_activity_hint", "") or s.get("linkedin_signal_summary", "")
        cold_rows.append({
            "company_name":         s.get("company_name", ""),
            "company_url":          s.get("company_url", ""),
            "top_signal":           _trim(top_signal, 180),
            "why_now":              _trim(why_now, 300),
            "suggested_call_angle": _trim(s.get("suggested_call_angle", ""), 350),
            "evidence_1":           _trim(hiring_ev, 350),
            "evidence_1_url":       hiring_url,
            "evidence_2":           _trim(growth_ev, 350),
            "evidence_2_url":       growth_url,
            "confidence":           s.get("confidence", ""),
        })
    if cold_rows:
        df4 = pd.DataFrame(cold_rows)[COLD_CALLER_COLS]
        ws4.append(list(df4.columns))
        _style_header_row(ws4)
        for _, r in df4.iterrows():
            ws4.append([str(v) if v is not None else "" for v in r])
    else:
        ws4.append(COLD_CALLER_COLS)
        _style_header_row(ws4)
    _autofit(ws4)
    _freeze(ws4)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------


def _get_secret(key: str) -> str:
    try:
        return st.secrets[key]
    except (KeyError, FileNotFoundError):
        return ""


def _load_uploaded(uploaded) -> pd.DataFrame | None:
    name = uploaded.name.lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded)
        xf = pd.ExcelFile(uploaded)
        # Prefer first sheet with company_name column
        for sheet in xf.sheet_names:
            df = xf.parse(sheet)
            lower = {c.lower() for c in df.columns}
            if "company_name" in lower or "company name" in lower:
                return df
        return xf.parse(xf.sheet_names[0])
    except Exception as exc:
        st.error(f"Kon bestand niet lezen: {exc}")
        return None


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase and strip column names; map common variants."""
    df = df.copy()
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    rename = {
        "company": "company_name",
        "name": "company_name",
        "url": "company_url",
        "website": "company_url",
        "domain": "company_url",
        "linkedin": "linkedin_url",
        "notes": "linkedin_notes",
    }
    df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)
    return df


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="🔗", layout="wide")
    st.title("🔗 " + APP_TITLE)
    st.markdown(
        "> **This demo checks public growth and hiring signals from search snippets, "
        "company websites and optionally pasted LinkedIn notes. "
        "It does not crawl LinkedIn directly.**"
    )

    # ── Sidebar: API keys ────────────────────────────────────────────────────
    st.sidebar.header("API-sleutels")
    serper_key    = _get_secret("SERPER_API_KEY")    or st.sidebar.text_input("Serper API key",    type="password")
    anthropic_key = _get_secret("ANTHROPIC_API_KEY") or st.sidebar.text_input("Anthropic API key", type="password")
    firecrawl_key = _get_secret("FIRECRAWL_API_KEY") or st.sidebar.text_input("Firecrawl API key (optioneel)", type="password")

    for label, val in [("Serper", serper_key), ("Anthropic", anthropic_key)]:
        if val:
            masked = val[:4] + "****" + val[-4:] if len(val) > 8 else "****"
            st.sidebar.success(f"{label}: `{masked}`")
        else:
            st.sidebar.error(f"{label}: niet ingesteld")
    if firecrawl_key:
        masked = firecrawl_key[:4] + "****" + firecrawl_key[-4:] if len(firecrawl_key) > 8 else "****"
        st.sidebar.info(f"Firecrawl: `{masked}`")
    else:
        st.sidebar.caption("Firecrawl: niet ingesteld — website crawl overgeslagen.")

    st.sidebar.markdown("---")
    max_rows      = st.sidebar.number_input("Max. rijen", min_value=1, max_value=HARD_CAP, value=DEFAULT_MAX)
    force_fresh   = st.sidebar.checkbox("Cache negeren (force fresh)", value=False)
    homepage_only = st.sidebar.checkbox("Alleen hoofdwebsite crawlen", value=False)
    if homepage_only:
        st.sidebar.caption("Career-URL varianten worden overgeslagen.")

    # ── Input mode ───────────────────────────────────────────────────────────
    st.markdown("### Bedrijfsinput")
    input_mode = st.radio("Invoermethode", ["Handmatig invoeren", "CSV / XLSX uploaden"], horizontal=True)

    df_input: pd.DataFrame | None = None

    if input_mode == "Handmatig invoeren":
        st.caption(
            "Vul minimaal `company_name` en `company_url` in. "
            "`linkedin_url` en `linkedin_notes` zijn optioneel."
        )
        default_rows = [
            {"company_name": "", "company_url": "", "linkedin_url": "", "linkedin_notes": ""},
        ] * DEFAULT_MAX
        df_input = st.data_editor(
            pd.DataFrame(default_rows),
            num_rows="dynamic",
            use_container_width=True,
            key="manual_input",
        )
        # Drop empty rows
        df_input = df_input[df_input["company_name"].str.strip().astype(bool)]

    else:
        uploaded = st.file_uploader("Upload CSV of XLSX", type=["csv", "xlsx"])
        if uploaded:
            raw_df = _load_uploaded(uploaded)
            if raw_df is not None:
                df_input = _normalise_columns(raw_df)
                st.success(f"{len(df_input)} rijen geladen uit `{uploaded.name}`.")
                st.dataframe(df_input.head(10), use_container_width=True)

    # ── Run button ───────────────────────────────────────────────────────────
    st.markdown("---")
    run_btn = st.button("🚀 Start signaalanalyse", type="primary")

    if not run_btn:
        return

    # Validation
    if not serper_key or not anthropic_key:
        st.error("Serper API key en Anthropic API key zijn verplicht.")
        return

    if df_input is None or df_input.empty:
        st.error("Geen bedrijven opgegeven.")
        return

    if "company_name" not in df_input.columns or "company_url" not in df_input.columns:
        st.error("Verplichte kolommen `company_name` en `company_url` ontbreken.")
        return

    rows = df_input.to_dict("records")[: int(max_rows)]
    n = len(rows)
    if n > HARD_CAP:
        rows = rows[:HARD_CAP]
        st.warning(f"Harde limiet: analyse beperkt tot {HARD_CAP} rijen.")

    # ── Processing ────────────────────────────────────────────────────────────
    all_signals:  list[dict] = []
    all_search:   list[dict] = []
    all_crawls:   list[dict] = []

    progress = st.progress(0, text="Bezig…")
    status_container = st.empty()

    for i, row in enumerate(rows):
        name = str(row.get("company_name", "")).strip() or f"rij {i+1}"
        status_container.info(f"🔍 Verwerken: **{name}** ({i+1}/{len(rows)})")
        try:
            signal, search_rows, crawl_rows = process_company(
                row=row,
                serper_key=serper_key,
                anthropic_key=anthropic_key,
                firecrawl_key=firecrawl_key,
                force_fresh=force_fresh,
                homepage_only=homepage_only,
            )
            all_signals.append(signal)
            all_search.extend(search_rows)
            all_crawls.extend(crawl_rows)
        except Exception as exc:
            st.warning(f"Fout bij {name}: {exc} — rij overgeslagen.")
            all_signals.append(_empty_signal(name, str(row.get("company_url", "")), "", str(exc)))
        progress.progress((i + 1) / len(rows), text=f"{i+1}/{len(rows)} verwerkt")

    status_container.success(f"✅ Analyse klaar — {len(all_signals)} bedrijven verwerkt.")
    progress.empty()

    # ── Results display ───────────────────────────────────────────────────────
    st.markdown("### Signaaloverzicht")
    df_summary = pd.DataFrame(all_signals)
    display_cols = [c for c in SIGNAL_SUMMARY_COLS if c in df_summary.columns]
    st.dataframe(df_summary[display_cols], use_container_width=True)

    # ── Excel download ────────────────────────────────────────────────────────
    st.markdown("### Download")
    xlsx_bytes = build_excel(all_signals, all_search, all_crawls)
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M")
    st.download_button(
        "⬇ Download Excel (4 tabbladen)",
        data=xlsx_bytes,
        file_name=f"linkedin_signals_{timestamp}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    main()
