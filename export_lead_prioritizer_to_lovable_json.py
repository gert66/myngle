"""Export Lead Prioritizer v2 Excel output to Lovable Company Hub JSON.

Converts a Lead Prioritizer workbook (sheets: "Enriched Leads", "Evidence",
"Signals", "Run Summary") into the static demo JSON layout the Lovable Company
Hub frontend expects:

  - companies.list.json          (array of light list items)
  - company-details-000.json     (detail bucket keyed by company_id)
  - company-details-001.json     ...
  - export_manifest.json         (counts, warnings, validation summary)

The workbook is treated as a small relational database: "Enriched Leads" holds
one row per company, "Evidence" and "Signals" hold many rows per company and
are joined on ``source_index``, and "Run Summary" carries run metadata.

This module is export/packaging only: it makes no API calls, performs no
enrichment, and does not change scoring, C4, C5, or HQ detection logic.

CLI:
    python export_lead_prioritizer_to_lovable_json.py \
        --input-xlsx lead_prioritizer_output.xlsx \
        --output-dir lovable_export \
        --country Brazil \
        --cold-callers "Jantje,Pietje,Marietje" \
        [--include-skipped] [--no-foreign-hq-only] [--bucket-size 500]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from lovable_content_localization import (
    localize_caller_angle_app,
    localize_caller_angle_app_it,
    localize_call_starter_app,
    localize_call_starter_app_it,
    localize_caution_app,
    localize_caution_app_it,
    localize_cold_caller_summary_app,
    localize_cold_caller_summary_app_it,
    localize_evidence_summary_app,
    localize_evidence_summary_app_it,
    localize_foreign_hq_evidence_text,
    localize_foreign_hq_evidence_text_it,
    localize_parent_hq_summary_app,
    localize_parent_hq_summary_app_it,
    localize_what_is_hot_item,
    localize_what_is_hot_item_it,
    localize_what_is_not_item,
    localize_what_is_not_item_it,
    localize_why_relevant_app,
    localize_why_relevant_app_it,
    translate_known_label,
    translate_known_label_it,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENRICHED_SHEET = "Enriched Leads"
EVIDENCE_SHEET = "Evidence"
SIGNALS_SHEET = "Signals"
RUN_SUMMARY_SHEET = "Run Summary"

FOREIGN_HQ_RUN_MODE = "full_foreign_hq_only"
FOREIGN_HQ_SIGNAL_LABEL = "Foreign ownership or group structure"

# Raw AI JSON columns are kept in debug only when they are not excessively
# large; above this size they are omitted with a manifest warning.
_MAX_RAW_JSON_CHARS = 20_000

# Score precedence for sorting before cold caller assignment.
SCORE_FIELD_PRECEDENCE = [
    "commercial_fit_score_app",
    "final_commercial_fit_score",
    "commercial_fit_score_after_hq_recalc",
    "final_commercial_fit_score_after_hq_recalc",
    "commercial_fit_score",
]

# Patterns that mark a signal_reason as internal/technical (not user-facing
# body text), e.g. "3 distinct keyword match(es) in evidence: training, ...".
_TECHNICAL_REASON_RE = re.compile(
    r"distinct\s+keyword\s+match"
    r"|keyword\s+match\(es\)"
    r"|\bC5\b"
    r"|c5_"
    r"|sig_"
    r"|parser_source"
    r"|adjudication"
    r"|\braw\b",
    re.IGNORECASE,
)


def is_technical_reason(text) -> bool:
    """True if a signal_reason string looks internal/technical rather than
    user-facing (e.g. keyword-match counts, c5_/sig_ field names)."""
    text = clean_str(text)
    if not text:
        return False
    return bool(_TECHNICAL_REASON_RE.search(text))


# Signals.signal_name -> Lovable display label (no technical sig_* labels).
SIGNAL_DISPLAY_LABELS = {
    "international_profile": "International business context",
    "onboarding_training_need": "L&D or onboarding signal",
    "company_size_complexity": "Possible onboarding need",
    "icp_keyword_match": "Explicit learning and development signal",
    "employer_branding": "Employer branding or employee satisfaction",
    "multicultural_workforce": "Multicultural or international workforce",
    "rapid_growth": "Rapid growth signal",
    "merger_acq": "Merger or acquisition signal",
}


class LovableExportError(ValueError):
    """Structural export error (missing required sheet, broken output)."""


# ---------------------------------------------------------------------------
# Value cleaning helpers
# ---------------------------------------------------------------------------

def clean_value(v):
    """NaN / blank strings -> None; numpy scalars -> Python scalars."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, str):
        v = v.strip()
        if not v or v.lower() in ("nan", "none"):
            return None
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def clean_str(v):
    v = clean_value(v)
    if v is None:
        return None
    return str(v)


def to_float(v):
    v = clean_value(v)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_bool(v):
    """Parse spreadsheet booleans: True/'yes'/'true'/1 -> True."""
    v = clean_value(v)
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    return str(v).strip().lower() in ("true", "yes", "y", "1")


def hostname_of(url) -> str:
    url = clean_str(url)
    if not url:
        return ""
    try:
        host = urlparse(url if "://" in url else f"https://{url}").netloc
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "company"


def normalize_source_index(v):
    """Excel roundtrips can turn ints into floats; normalize for joining."""
    v = clean_value(v)
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            f = float(v)
            if f.is_integer():
                return int(f)
        except ValueError:
            pass
    return v


# ---------------------------------------------------------------------------
# Text field parsing
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s|;,\"'<>)\]]+")
_BULLET_RE = re.compile(r"^[-*•·]\s*")


def parse_array_field(v) -> list[str]:
    """Parse an array-like text field from JSON, newline, pipe, or semicolon."""
    if isinstance(v, (list, tuple)):
        return [s for s in (clean_str(x) for x in v) if s]
    text = clean_str(v)
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, list):
                return [s for s in (clean_str(x) for x in loaded) if s]
        except (json.JSONDecodeError, TypeError):
            pass
    if "\n" in text:
        parts = text.split("\n")
    elif "|" in text:
        parts = text.split("|")
    elif ";" in text:
        parts = text.split(";")
    else:
        parts = [text]
    out = []
    for part in parts:
        part = _BULLET_RE.sub("", part.strip())
        if part:
            out.append(part)
    return out


def parse_key_source_links(v) -> list[dict]:
    """Parse key_source_links_app into a list of ``{label, url}`` items.

    A line like ``"International profile — Title: https://example.com"`` becomes
    ``{"label": "International profile — Title", "url": "https://example.com"}``.
    A bare URL gets its hostname as label; a line with multiple URLs yields one
    item per URL.
    """
    if isinstance(v, (list, tuple)):
        items = []
        for entry in v:
            if isinstance(entry, dict):
                url = clean_str(entry.get("url"))
                if url:
                    items.append({
                        "label": clean_str(entry.get("label")) or hostname_of(url),
                        "url": url,
                    })
            else:
                items.extend(parse_key_source_links(entry))
        return items

    text = clean_str(v)
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, list):
                return parse_key_source_links(loaded)
        except (json.JSONDecodeError, TypeError):
            pass

    items = []
    for line in parse_array_field(text):
        urls = _URL_RE.findall(line)
        if not urls:
            continue
        label = line[: line.find(urls[0])].strip().rstrip(":—–- ").strip()
        for url in urls:
            url = url.rstrip(".,")
            items.append({"label": label or hostname_of(url), "url": url})
    return items


# ---------------------------------------------------------------------------
# Company IDs
# ---------------------------------------------------------------------------

def make_company_id(row: dict, used_ids: set) -> str:
    """Stable slug id: normalized_domain > domain > company_name+source_index."""
    basis = clean_str(row.get("normalized_domain")) or clean_str(row.get("domain"))
    if not basis:
        name = clean_str(row.get("company_name")) or "company"
        basis = f"{name}-{row.get('source_index')}"
    company_id = slugify(basis)
    if company_id in used_ids:
        company_id = slugify(f"{basis}-{row.get('source_index')}")
        n = 2
        base = company_id
        while company_id in used_ids:
            company_id = f"{base}-{n}"
            n += 1
    used_ids.add(company_id)
    return company_id


# ---------------------------------------------------------------------------
# Foreign HQ detection (export-side classification only)
# ---------------------------------------------------------------------------

def detect_foreign_hq_for_export(row: dict, run_mode) -> tuple[bool, str | None]:
    """Classify a row as detected-foreign-HQ for export purposes.

    Deliberately broader than ``sig_foreign_hq_score_for_next_scoring == 3``:
    in full_foreign_hq_only outputs some C5-confirmed rows may carry score 0 or
    blank after conservative handling.
    """
    if to_float(row.get("sig_foreign_hq_score_for_next_scoring")) == 3.0:
        return True, "final_hq_score_3"
    if (clean_str(row.get("c5_adjudication")) or "").lower() == "foreign_parent_confirmed":
        return True, "c5_foreign_parent_confirmed"
    if to_float(row.get("c5_recommended_hq_score")) == 3.0:
        return True, "c5_recommended_score_3"
    if to_bool(row.get("foreign_hq_signal_used_in_app")):
        return True, "foreign_hq_signal_used_in_app"
    if not to_bool(row.get("enrichment_skipped")) and run_mode == FOREIGN_HQ_RUN_MODE:
        return True, "full_foreign_hq_only_enriched_row"
    return False, None


# ---------------------------------------------------------------------------
# Score sorting
# ---------------------------------------------------------------------------

def score_for_sort(row: dict) -> tuple[float, str | None]:
    for field in SCORE_FIELD_PRECEDENCE:
        value = to_float(row.get(field))
        if value is not None:
            return value, field
    return 0.0, None


# ---------------------------------------------------------------------------
# Evidence / signals derived structures
# ---------------------------------------------------------------------------

def build_evidence_snippets(evidence_rows: list[dict]) -> list[dict]:
    snippets = []
    for ev in evidence_rows:
        snippets.append({
            "title": clean_str(ev.get("source_title")),
            "source_domain": hostname_of(ev.get("source_url")),
            "query_type": clean_str(ev.get("signal_name")),
            "text": clean_str(ev.get("source_snippet")),
            "snippet": clean_str(ev.get("source_snippet")),
            "url": clean_str(ev.get("source_url")),
            "source": clean_str(ev.get("source_type")),
            "parser_source": clean_str(ev.get("parser_source")),
            "confidence": clean_str(ev.get("confidence")),
            "query_used": clean_str(ev.get("query_used")),
            "notes": clean_str(ev.get("notes")),
        })
    return snippets


def build_source_urls(evidence_rows, key_source_links, signal_rows) -> list[str]:
    """Unique full URLs from Evidence, parsed key source links, and Signals."""
    urls: list[str] = []
    seen = set()

    def _add(url):
        url = clean_str(url)
        if url and url.lower().startswith("http") and url not in seen:
            seen.add(url)
            urls.append(url)

    for ev in evidence_rows:
        _add(ev.get("source_url"))
    for link in key_source_links:
        _add(link.get("url"))
    for sig in signal_rows:
        _add(sig.get("evidence_url"))
    return urls


def build_serper_result_titles(evidence_rows: list[dict]) -> list[str]:
    titles: list[str] = []
    seen = set()
    for ev in evidence_rows:
        title = clean_str(ev.get("source_title"))
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
    return titles


# Trailing internal C5 adjudication fragment appended for audit purposes,
# e.g. ". C5: foreign_parent_confirmed." or " C5: unclear." -- never
# caller-facing; stripped by sanitize_caller_facing_evidence() below.
_TRAILING_C5_FRAGMENT_RE = re.compile(r"\s+C5:\s*[A-Za-z0-9_]+\.?\s*$")


def sanitize_caller_facing_evidence(text) -> "str | None":
    """Strip trailing internal C5 adjudication fragments from evidence text
    meant for visible_icp_signal_scores. Leaves the useful text before the
    fragment intact; never blanks the text if nothing else is left."""
    text = clean_str(text)
    if not text:
        return text
    sanitized = _TRAILING_C5_FRAGMENT_RE.sub("", text).strip()
    return sanitized or text


def build_foreign_hq_evidence_text(row: dict) -> str:
    """Concise evidence line for the foreign-HQ visible signal row."""
    parent = clean_str(row.get("c5_parent_company"))
    country = (clean_str(row.get("c5_parent_hq_country"))
               or clean_str(row.get("foreign_hq_country_app")))
    city = clean_str(row.get("c5_parent_hq_city"))
    adjudication = clean_str(row.get("c5_adjudication"))
    reason = clean_str(row.get("c5_reason"))

    if parent:
        text = f"Confirmed foreign parent: {parent}"
        if country:
            text += f", HQ {country}"
        if city:
            text += f" ({city})"
        text += "."
    elif country:
        text = f"Foreign headquarters detected: {country}."
    else:
        text = "Foreign headquarters or group structure detected."
    if adjudication:
        text += f" C5: {adjudication}."
    elif reason:
        text += f" {reason[:200]}"
    return sanitize_caller_facing_evidence(text)


def build_visible_icp_signal_scores(
    row: dict,
    signal_rows: list[dict],
    foreign_hq_detected: bool,
) -> list[dict]:
    """Human-readable signal rows for the Lovable UI (no sig_* labels)."""
    visible = []

    add_foreign_hq_row = (
        to_float(row.get("sig_foreign_hq_score_for_next_scoring")) == 3.0
        or (clean_str(row.get("c5_adjudication")) or "").lower() == "foreign_parent_confirmed"
        or to_float(row.get("c5_recommended_hq_score")) == 3.0
        or to_bool(row.get("foreign_hq_signal_used_in_app"))
        or foreign_hq_detected
    )
    if add_foreign_hq_row:
        visible.append({
            "label": FOREIGN_HQ_SIGNAL_LABEL,
            "score": 3,
            "evidence": build_foreign_hq_evidence_text(row),
            "signal_name": "foreign_hq",
            "evidence_url": None,
        })

    for sig in signal_rows:
        name = clean_str(sig.get("signal_name")) or ""
        label = SIGNAL_DISPLAY_LABELS.get(name)
        if label is None:
            # Fall back to a readable label; never expose raw sig_* tokens.
            label = name.removeprefix("sig_").replace("_", " ").strip().capitalize()
        if not label:
            continue
        reason = clean_str(sig.get("signal_reason"))
        evidence = (clean_str(sig.get("evidence_quote"))
                    or (reason if not is_technical_reason(reason) else None)
                    or clean_str(sig.get("evidence_title")))
        visible.append({
            "label": label,
            "score": to_float(sig.get("signal_score")),
            "evidence": evidence,
            # Internal-only extras (not part of the historical {label, score,
            # evidence} contract) used by the ui_payload builders below to
            # tell distinct underlying signals apart (e.g. "onboarding" also
            # appears in company_size_complexity's display label) and to
            # check evidence against its source domain.
            "signal_name": name or None,
            "evidence_url": clean_str(sig.get("evidence_url")),
        })
    return visible


def build_evidence_audit(
    row: dict,
    evidence_rows: list[dict],
    signal_rows: list[dict],
    evidence_snippets: list[dict],
    source_urls: list[str],
    run_metadata: dict | None,
) -> dict:
    url_pools: dict[str, list[str]] = {}
    for ev in evidence_rows:
        signal = clean_str(ev.get("signal_name")) or "unknown"
        url = clean_str(ev.get("source_url"))
        if url:
            url_pools.setdefault(signal, [])
            if url not in url_pools[signal]:
                url_pools[signal].append(url)

    signal_evidence = []
    for sig in signal_rows:
        signal_evidence.append({
            "signal_name": clean_str(sig.get("signal_name")),
            "signal_score": to_float(sig.get("signal_score")),
            "signal_confidence": clean_str(sig.get("signal_confidence")),
            "signal_reason": clean_str(sig.get("signal_reason")),
            "evidence_url": clean_str(sig.get("evidence_url")),
            "evidence_quote": clean_str(sig.get("evidence_quote")),
            "evidence_title": clean_str(sig.get("evidence_title")),
        })

    c5_audit = {k: clean_value(v) for k, v in row.items() if k.startswith("c5_")}
    hq_audit = {
        k: clean_value(v) for k, v in row.items()
        if k.startswith("hq_") or k.startswith("sig_foreign_hq_")
    }

    audit = {
        "raw_google_evidence_count": len(evidence_rows),
        "evidence_snippet_count": len(evidence_snippets),
        "source_url_count": len(source_urls),
        "search_result_titles": build_serper_result_titles(evidence_rows),
        "search_snippets": [s for s in (clean_str(ev.get("source_snippet"))
                                        for ev in evidence_rows) if s],
        "raw_source_url_pools": url_pools,
        "signal_evidence": signal_evidence,
        "c5_audit": c5_audit,
        "hq_audit": hq_audit,
    }
    if run_metadata:
        audit["run_metadata"] = run_metadata
    return audit


def build_quality_flags(row: dict) -> list[str]:
    flags = []
    for column in (
        "needs_manual_review",
        "hq_query_risk_flag",
        "hq_evidence_domain_mismatch_warning",
        "hq_positive_score_suppressed_for_review",
        "c5_recommended_manual_review",
        "c5_possible_foreign_parent_for_review",
        "competitor_signal_suppressed",
    ):
        if to_bool(row.get(column)):
            flags.append(column)
    return flags


# ---------------------------------------------------------------------------
# ui_payload content builders — richer, curated, company-specific text built
# straight from already-computed row/visible-signal data. Independent from
# the frozen why_relevant_app/what_is_hot_app/... Italy-compatible templates
# in lead_caller_app_fields_builder.py (never touched here): this only adds
# to and refines the separate ui_payload copy so Lovable can render it
# literally, without needing to enrich or interpret anything itself.
# ---------------------------------------------------------------------------

# Raw quality_flags column name -> human-readable caution sentence. Keeps
# internal flag tokens (needs_manual_review, parser_source, ...) out of any
# caller-facing text.
_QUALITY_FLAG_CAUTION_TEXT: dict[str, str] = {
    "needs_manual_review": "Manual review recommended before outreach.",
    "hq_query_risk_flag": "The HQ search query carries some ambiguity; verify the HQ signal before relying on it.",
    "hq_evidence_domain_mismatch_warning": (
        "The HQ evidence source does not clearly match the lead's own "
        "domain; verify the HQ signal before relying on it."
    ),
    "hq_positive_score_suppressed_for_review": (
        "The foreign-HQ signal was flagged for manual review before being "
        "treated as confirmed."
    ),
    "c5_recommended_manual_review": "A manual review of the foreign-HQ signal is recommended.",
    "c5_possible_foreign_parent_for_review": (
        "A possible foreign parent was found but still needs manual confirmation."
    ),
    "competitor_signal_suppressed": "A competitor-related signal was detected and excluded.",
}

# A caution_app sentence is only split into separate ui_payload.caution items
# at a genuine item boundary — a period immediately followed by ";" (the
# exact separator _join() in lead_caller_app_fields_builder.py inserts
# between already-complete sentences). Some individual warnings (e.g. the
# domain-mismatch one) use "; " as an internal clause connector with no
# preceding period, so a naive text.split(";") would chop that one warning
# into two bogus fragments; this lookbehind never matches there.
_CAUTION_ITEM_SPLIT_RE = re.compile(r"(?<=\.)\s*;\s*")

# Fragments that mark generic/templated filler text — never promoted into
# ui_payload caller-facing bullets or driver evidence.
_GENERIC_TEXT_RE = re.compile(
    r"signals?\s+point\s+to"
    r"|combines\s+a\s+signal\s+with\s+evidence"
    r"|keyword\s+evidence\s+signals\s+alignment"
    r"|positive\s+signals\s*:"
    r"|buying\s+signals\s*:"
    r"|industry\s*:\s*unknown",
    re.IGNORECASE,
)

# Raw internal token fragments (sig_/ti_/c4_/c5_ field names, score math) that
# must never leak into ui_payload text.
_RAW_TOKEN_RE = re.compile(r"\bsig_|\bti_|\bc[45]_", re.IGNORECASE)

# Raw scraper formatting artifacts (e.g. "___THE NETHERLANDS ___Austria")
# left over from a poorly-cleaned location list dump.
_RAW_ARTIFACT_RE = re.compile(r"_{2,}")

# Marketing/event fragments picked up by mistake from an unrelated page
# section (careers/events feed) rather than genuine company evidence.
_EVENT_FRAGMENT_RE = re.compile(
    r"back\s+by\s+popular\s+demand"
    r"|coming\s+soon"
    r"|register\s+now"
    r"|sign\s+up\s+now"
    r"|read\s+more"
    r"|click\s+here"
    r"|\.\.\.\s*$",
    re.IGNORECASE,
)

# Above this length, a snippet is no longer "a concrete evidence sentence" —
# it reads as a dumped block of scraped text rather than a clean summary.
_MAX_VISIBLE_EVIDENCE_CHARS = 220

_EMPLOYEE_COUNT_RE = re.compile(r"([\d,]{1,7})\s*\+?\s*employees", re.IGNORECASE)


def _employee_range_bounds(employee_range) -> "tuple[int, int | None] | None":
    employee_range = clean_str(employee_range)
    if not employee_range:
        return None
    m = re.match(r"\s*([\d,]+)\s*-\s*([\d,]+)", employee_range)
    if m:
        return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    m = re.match(r"\s*([\d,]+)\s*\+", employee_range)
    if m:
        return int(m.group(1).replace(",", "")), None
    return None


def _looks_like_location_dump(text: str) -> bool:
    """True for a multiline text, or a run of 3+ short capitalized
    location-like tokens — a scraped "office locations" list rather than a
    concrete evidence sentence."""
    if "\n" in text or "\r" in text:
        return True
    segments = re.split(r"[|;]", text)
    capitalized_segments = [
        s for s in segments
        if re.match(r"^[A-Z][A-Za-z .'-]{1,40}$", s.strip())
    ]
    return len(capitalized_segments) >= 3


def is_generic_or_raw_text(text) -> bool:
    """True for banned generic filler phrases, raw internal field tokens,
    scraper formatting artifacts, event/marketing fragments, multiline
    location dumps, or text too long to be a clean evidence sentence."""
    text = clean_str(text)
    if not text:
        return False
    if _GENERIC_TEXT_RE.search(text) or _RAW_TOKEN_RE.search(text):
        return True
    if _RAW_ARTIFACT_RE.search(text) or _EVENT_FRAGMENT_RE.search(text):
        return True
    if _looks_like_location_dump(text):
        return True
    if len(text) > _MAX_VISIBLE_EVIDENCE_CHARS:
        return True
    return False


def is_suspicious_evidence(text, employee_range=None) -> bool:
    """True when evidence quotes an employee count wildly inconsistent with
    the lead's own employee_range (e.g. "5 employees" vs. a "10,001+" lead) —
    a strong sign the snippet is about a different company."""
    text = clean_str(text)
    if not text:
        return False
    match = _EMPLOYEE_COUNT_RE.search(text)
    if not match:
        return False
    bounds = _employee_range_bounds(employee_range)
    if not bounds:
        return False
    lo, hi = bounds
    count = int(match.group(1).replace(",", ""))
    if hi is not None:
        return count < lo * 0.1 or count > hi * 10
    return count < lo * 0.1


# Simplified registrable-domain heuristic (last two dot-separated labels) —
# no public-suffix-list handling, which is an accepted limitation for the
# .com/.nl/.com.br-style domains this exporter deals with.
def _registrable_domain(host: "str | None") -> str:
    host = (host or "").lower().removeprefix("www.")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


# Independent business-info sites worth keeping as "Third-party company
# profile" even though they're never the lead's own domain.
_KNOWN_THIRD_PARTY_DIRECTORY_DOMAINS = frozenset({
    "linkedin.com", "crunchbase.com", "bloomberg.com", "glassdoor.com",
    "zoominfo.com", "owler.com",
})


def _company_name_tokens(company_name: "str | None") -> list[str]:
    """First couple of significant (3+ letter) words of a company name, used
    as a light-touch check that a third-party page is actually about this
    lead rather than an unrelated company picked up by the scraper."""
    if not company_name:
        return []
    return re.findall(r"[A-Za-z]{3,}", company_name)[:2]


def _mentions_company(text: "str | None", company_name: "str | None") -> bool:
    if not text or not company_name:
        return False
    text_lower = text.lower()
    return any(tok.lower() in text_lower for tok in _company_name_tokens(company_name))


def is_domain_relevant_for_url(
    url: "str | None",
    own_domains: "set[str] | None",
    company_name: "str | None" = None,
    context_text: "str | None" = None,
) -> bool:
    """True when a URL's domain is the lead's own site, a known independent
    business directory, or its accompanying text actually mentions the lead
    — false for an unrelated domain such as a different company's careers
    page picked up by a scraper (e.g. careers.accor.com for a DORC lead)."""
    host = hostname_of(url)
    if not host:
        return False
    domain = _registrable_domain(host)
    if own_domains and domain in own_domains:
        return True
    if domain in _KNOWN_THIRD_PARTY_DIRECTORY_DOMAINS:
        return True
    if _mentions_company(context_text, company_name):
        return True
    # Nothing to compare against (no known own domain) — don't blanket-reject.
    return not own_domains


def _topic_pattern(words: "tuple[str, ...]") -> "re.Pattern":
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)


# Concrete topic vocabulary a signal's evidence must actually mention to be
# promoted into ui_payload — being domain-safe and not-generic-filler is not
# enough on its own: generic homepage/product sales copy (e.g. "Protect your
# site with construction site monitoring...") is domain-safe but says
# nothing about international operations or L&D, and must not be promoted
# just because it happened to get tagged under that signal_name.
_SIGNAL_TOPIC_PATTERNS: "dict[str, re.Pattern]" = {
    "international_profile": _topic_pattern((
        "countries", "country", "regions", "region", "markets", "market",
        "international", "global", "worldwide", "cross-border",
        "parent group", "parent company", "subsidiary", "subsidiaries",
        "group structure", "offices", "branches", "stores", "locations",
        "multinational",
    )),
    "onboarding_training_need": _topic_pattern((
        "training", "academy", "lms", "learning management system",
        "onboarding", "employee development", "leadership development",
        "talent development", "mandatory training", "upskilling",
        "career development", "learning program", "learning programs",
    )),
    "icp_keyword_match": _topic_pattern((
        "training", "academy", "lms", "learning management system",
        "onboarding", "employee development", "leadership development",
        "talent development", "mandatory training", "upskilling",
        "career development", "learning program", "learning programs",
    )),
    "company_size_complexity": _topic_pattern((
        "employees", "employee count", "workforce", "multi-site",
        "multi site", "branches", "stores", "locations",
        "distributed team", "distributed teams", "operational scale",
        "large-scale", "large scale",
    )),
    "employer_branding": _topic_pattern((
        "careers page", "employer branding", "employee value proposition",
        "evp", "team culture", "workplace", "employee testimonial",
        "employee testimonials", "great place to work", "workplace award",
        "workplace awards", "company culture",
    )),
}


def has_topical_keywords(text: str, signal_name: "str | None") -> bool:
    """True when evidence text actually mentions the concrete topic its
    signal claims. Signals without a defined topic vocabulary (foreign_hq,
    unmapped custom signals, ...) are not gated here."""
    pattern = _SIGNAL_TOPIC_PATTERNS.get(signal_name or "")
    if pattern is None:
        return True
    return bool(pattern.search(text))


def _clean_driver_evidence(
    signal: dict,
    employee_range: "str | None",
    own_domains: "set[str] | None" = None,
    company_name: "str | None" = None,
) -> "str | None":
    """Evidence text usable in ui_payload, or None when it's weak, generic,
    a raw/formatting artifact, suspicious, off-topic for its signal (e.g.
    generic homepage sales copy tagged as an L&D or international signal),
    or from an unrelated domain — the label/strength is kept regardless."""
    text = clean_str(signal.get("evidence"))
    if not text:
        return None
    if is_generic_or_raw_text(text) or is_suspicious_evidence(text, employee_range):
        return None
    if not has_topical_keywords(text, signal.get("signal_name")):
        return None
    url = signal.get("evidence_url")
    if url and not is_domain_relevant_for_url(url, own_domains, company_name, text):
        return None
    return text


def resolve_parent_company(row: dict) -> "str | None":
    return clean_str(row.get("c5_parent_company")) or clean_str(row.get("ai_parent_company"))


def resolve_parent_hq_country(row: dict) -> "str | None":
    return (clean_str(row.get("c5_parent_hq_country"))
            or clean_str(row.get("ai_parent_hq_country"))
            or clean_str(row.get("foreign_hq_country_app")))


# Natural caller-facing phrasing for known signal display labels — applied
# only to ui_payload text (why_relevant / what_is_hot / commercial_fit_drivers),
# never to the historical visible_icp_signal_scores.label used by the frozen
# Italy/Dutch label translations in lovable_content_localization.py.
_LABEL_NATURAL_OVERRIDES: dict[str, str] = {
    "L&D or onboarding signal": "learning and development or onboarding needs",
    "Explicit learning and development signal": "learning and development interest",
    "Rapid growth signal": "rapid growth",
    "Merger or acquisition signal": "merger or acquisition activity",
}


def _natural_label(label: "str | None") -> str:
    if not label:
        return ""
    mapped = _LABEL_NATURAL_OVERRIDES.get(label, label)
    mapped = re.sub(r"\bL&D\b", "learning and development", mapped, flags=re.IGNORECASE)
    return mapped


def build_ui_payload_why_relevant(
    company_name: "str | None",
    export_country: "str | None",
    industry: "str | None",
    foreign_hq_detected: bool,
    parent_company: "str | None",
    parent_hq_country: "str | None",
    visible_signals: list[dict],
    employee_range: "str | None" = None,
    own_domains: "set[str] | None" = None,
) -> str:
    """Company-specific, concrete relevance sentence built from safe fields
    only (company name, country, parent/HQ, industry, strongest signals)."""
    company = company_name or "This company"
    country = export_country or "the region"
    industry_word = (f" {industry.lower()}" if industry and industry != "Unknown" else "")

    sentence = f"{company} is a {country}-based{industry_word} company"

    if foreign_hq_detected and parent_company and parent_hq_country:
        sentence += (
            f" operating as part of {parent_company}, headquartered in "
            f"{parent_hq_country}."
        )
    elif foreign_hq_detected and parent_hq_country:
        sentence += f" with a confirmed foreign parent headquartered in {parent_hq_country}."
    elif foreign_hq_detected:
        sentence += " with a confirmed foreign parent or HQ context."
    else:
        sentence += "."

    strongest_labels = [
        _natural_label(s.get("label")) for s in visible_signals
        if s.get("label") and s.get("label") != FOREIGN_HQ_SIGNAL_LABEL
        and (s.get("score") or 0) > 0
        and _clean_driver_evidence(s, employee_range, own_domains, company_name) is not None
    ]
    if strongest_labels:
        signal_phrase = " and ".join(label.lower() for label in strongest_labels[:2])
        sentence += f" It also shows {signal_phrase}, relevant to language and training support."

    return sentence


# Canonical signal_name groupings for the what_is_hot summary line — keyed by
# the underlying Signals.signal_name, never by display-label substrings.
# Using label text (e.g. "Possible onboarding need") would let an unrelated
# signal (company_size_complexity) falsely trigger the L&D summary claim
# just because its *label* happens to also contain the word "onboarding".
_SUMMARY_SIGNAL_NAMES = {
    "international": {"international_profile"},
    "learning_development": {"onboarding_training_need", "icp_keyword_match"},
    "size_complexity": {"company_size_complexity"},
}

# The same signal_names what_is_hot's summary line makes a topic claim
# about. commercial_fit_drivers must not show one of these as a weak/
# evidence-less driver while what_is_hot stays silent on it (or vice versa)
# — see build_commercial_fit_drivers.
_BUCKETED_SIGNAL_NAMES = frozenset().union(*_SUMMARY_SIGNAL_NAMES.values())


def build_ui_payload_what_is_hot(
    foreign_hq_detected: bool,
    parent_hq_country: "str | None",
    employee_range: "str | None",
    visible_signals: list[dict],
    own_domains: "set[str] | None" = None,
    company_name: "str | None" = None,
    max_bullets: int = 5,
) -> list[str]:
    """Max-5-bullet Italy-style but controlled list: a compact summary line
    first, then concrete, evidence-backed bullets. Never raw tokens, score
    math, or generic/suspicious/unrelated-domain snippets."""
    positive_signals = [
        s for s in visible_signals
        if s.get("label") and s.get("label") != FOREIGN_HQ_SIGNAL_LABEL
        and (s.get("score") or 0) > 0
    ]

    def _has_signal(names: set) -> bool:
        # A bucket only "counts" for the summary line when at least one of
        # its positively-scored signals also has curated evidence — the same
        # test the per-signal bullet loop and commercial_fit_drivers use, so
        # what_is_hot never claims a topic the driver panel can't back up.
        return any(
            s.get("signal_name") in names
            and _clean_driver_evidence(s, employee_range, own_domains, company_name) is not None
            for s in positive_signals
        )

    summary_parts = []
    if foreign_hq_detected:
        summary_parts.append(
            f"Foreign HQ: {parent_hq_country}" if parent_hq_country else "Foreign HQ confirmed"
        )
    if _has_signal(_SUMMARY_SIGNAL_NAMES["international"]):
        summary_parts.append("International footprint")
    if _has_signal(_SUMMARY_SIGNAL_NAMES["learning_development"]):
        summary_parts.append("Learning and development")
    if _has_signal(_SUMMARY_SIGNAL_NAMES["size_complexity"]) or clean_str(employee_range):
        summary_parts.append("Large-scale operation")

    bullets: list[str] = []
    if summary_parts:
        bullets.append(" | ".join(summary_parts))

    if foreign_hq_detected:
        bullets.append(
            f"Foreign ownership or group structure: headquartered in {parent_hq_country}."
            if parent_hq_country else "Foreign ownership or group structure confirmed."
        )

    _PREFIXES = (
        (("international",), "International business context"),
        (("learning", "onboarding", "l&d"), "Learning and development"),
        (("size", "complexity"), "Company complexity"),
    )
    for signal in positive_signals:
        if len(bullets) >= max_bullets:
            break
        # No curated evidence -> no standalone bullet at all (never a bare
        # "Learning and development." claim with nothing behind it).
        evidence = _clean_driver_evidence(signal, employee_range, own_domains, company_name)
        if evidence is None:
            continue
        label = signal.get("label") or ""
        label_lower = label.lower()
        prefix = next(
            (text for keywords, text in _PREFIXES
             if any(kw in label_lower for kw in keywords)),
            _natural_label(label),
        )
        bullet = f"{prefix}: {evidence}"
        if bullet not in bullets:
            bullets.append(bullet)

    return bullets[:max_bullets]


def _strength_for_score(score) -> str:
    if score is None:
        return "Unknown"
    if score >= 2:
        return "Strong"
    if score >= 1:
        return "Moderate"
    return "Weak"


def build_commercial_fit_drivers(
    visible_signals: list[dict],
    employee_range: "str | None" = None,
    own_domains: "set[str] | None" = None,
    company_name: "str | None" = None,
) -> list[dict]:
    """Curated {label, strength, evidence} rows from the same signal source
    as visible_icp_signal_scores. For the topics what_is_hot's summary line
    can claim (international / learning & development / size-complexity),
    a driver is only included when it's positively scored AND has curated
    evidence — never shown as a weak/evidence-less "Absent" row that would
    contradict a positive what_is_hot claim (or sit there when what_is_hot
    made no claim at all). Other signals (foreign HQ, employer branding,
    custom, ...) keep the label/strength even when evidence is weak."""
    drivers = []
    for signal in visible_signals:
        label = signal.get("label")
        if not label:
            continue
        signal_name = signal.get("signal_name")
        score = signal.get("score")
        evidence = _clean_driver_evidence(signal, employee_range, own_domains, company_name)
        if signal_name in _BUCKETED_SIGNAL_NAMES and (not (score and score > 0) or evidence is None):
            continue
        driver = {
            "label": _natural_label(label),
            "strength": _strength_for_score(score),
        }
        if evidence:
            driver["evidence"] = evidence
        drivers.append(driver)
    return drivers


def build_ui_payload_caution(quality_flags: list[str], caution_app: "str | None") -> list[str]:
    """Human-readable warnings only, one sentence per distinct warning —
    raw quality-flag column names (e.g. hq_evidence_domain_mismatch_warning,
    needs_manual_review) are always mapped to plain text, never exposed
    verbatim, and never split mid-sentence into bogus extra fragments."""
    caution: list[str] = []
    for flag in quality_flags:
        text = _QUALITY_FLAG_CAUTION_TEXT.get(flag)
        if text and text not in caution:
            caution.append(text)
    for part in _CAUTION_ITEM_SPLIT_RE.split(caution_app or ""):
        part = part.strip()
        if part and not is_generic_or_raw_text(part) and part not in caution:
            caution.append(part)
    return caution


def build_ui_payload_source_urls(
    website_url: "str | None",
    careers_url: "str | None",
    linkedin_url: "str | None",
    source_urls: list[str],
    own_domains: "set[str] | None" = None,
    company_name: "str | None" = None,
    url_context: "dict[str, str] | None" = None,
) -> list[dict]:
    """Deduplicated (by normalized URL) {label, url} rows with stable
    labels: the lead's own domain is "Official website" (or "Careers page"
    for a careers URL/subdomain on that same domain), LinkedIn is
    "LinkedIn", and only genuinely unrelated third-party domains are
    excluded outright (e.g. careers.accor.com for a DORC lead) — everything
    else third-party is kept, labeled "Third-party company profile"."""
    url_context = url_context or {}
    own_domains = own_domains or set()
    seen_norm: set[str] = set()
    items: list[dict] = []

    def _normalized(url: str) -> str:
        u = re.sub(r"^https?://", "", url.strip(), flags=re.IGNORECASE).rstrip("/")
        return u[4:].lower() if u.lower().startswith("www.") else u.lower()

    def _add(url, label) -> None:
        url = clean_str(url)
        if not url:
            return
        norm = _normalized(url)
        if norm in seen_norm:
            return
        seen_norm.add(norm)
        items.append({"label": label, "url": url})

    _add(website_url, "Official website")
    _add(careers_url, "Careers page")
    _add(linkedin_url, "LinkedIn")

    for url in source_urls:
        url = clean_str(url)
        if not url:
            continue
        norm = _normalized(url)
        if norm in seen_norm:
            continue
        host = hostname_of(url)
        if "linkedin.com" in host:
            _add(url, "LinkedIn")
            continue
        domain = _registrable_domain(host)
        if domain in own_domains:
            label = "Careers page" if "career" in url.lower() else "Official website"
            _add(url, label)
            continue
        if not is_domain_relevant_for_url(url, own_domains, company_name, url_context.get(url)):
            continue  # unrelated domain (e.g. careers.accor.com) — excluded
        _add(url, "Third-party company profile")

    return items


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

# Columns consumed into named list/detail fields; everything else from the
# Enriched Leads row is preserved under debug.lead_prioritizer_row.
_CONSUMED_COLUMNS = {
    "source_index", "company_name", "domain", "normalized_domain",
    "input_country",
    "industry", "employee_range", "size_category_app",
    "display_size_category_app",
    "commercial_fit_score", "commercial_tier",
    "commercial_fit_score_app", "commercial_tier_app", "display_tier_app",
    "outreach_readiness_status", "has_detail", "has_contacts",
    "last_updated", "last_updated_at",
    "enrichment_skipped", "enrichment_skip_reason",
    "website_url", "linkedin_url", "careers_url",
    "why_relevant_app", "what_is_hot_app", "what_is_not_app",
    "caller_angle_app", "call_starter_app", "caution_app",
    "cold_caller_summary_app", "parent_hq_summary_app",
    "evidence_summary_app", "key_source_links_app", "advanced_notes_app",
    "buyer_route_app", "likely_training_interest_app",
    "foreign_hq_signal_used_in_app", "competitor_signal_used_in_app",
    "competitor_signal_suppressed",
    "foreign_hq_country_app", "foreign_hq_city_app",
    "domain_quality",
}


# Input-column aliases for industry/sector, tried in this order. Every real
# input-column alias is tried BEFORE "detected_industry" (v2 sector
# detection), so a usable input industry is never overwritten by the
# detector — detected_industry is purely a last-resort fallback.
_INDUSTRY_COLUMN_ALIASES: tuple[str, ...] = (
    "industry", "Industry",
    "sector", "Sector",
    "industry_name", "Industry Name",
    "company_industry", "Company Industry",
    "main_industry", "Main Industry", "main industry",
    "lusha_industry", "Lusha Industry", "Lusha industry",
    "detected_industry",
)

# Placeholder values that mean "no usable industry" — checked case-insensitively.
_UNUSABLE_INDUSTRY_VALUES: frozenset = frozenset({"unknown", "n/a", "na", "none", "nan"})


def _first_non_unknown(row: dict, columns) -> tuple[str | None, str | None]:
    """``(value, source_column)`` for the first usable value across ``columns``.

    Returns ``(None, None)`` when nothing usable is found. "Usable" excludes
    blank/whitespace-only strings and common placeholder text in any casing
    ("Unknown", "N/A", "None", "nan") — ``clean_str`` already collapses blank/
    NaN/"none"/"nan" to ``None``; this adds "unknown" and "n/a" on top.
    """
    for column in columns:
        value = clean_str(row.get(column))
        if value and value.strip().lower() not in _UNUSABLE_INDUSTRY_VALUES:
            return value, column
    return None, None


def _resolve_industry(row: dict) -> str:
    """Best usable industry: keep the input value when present, otherwise fall
    back to v2 sector detection, then "Unknown". Never overwrites a usable
    input industry with detected_industry."""
    value, _source = _first_non_unknown(row, _INDUSTRY_COLUMN_ALIASES)
    return value or "Unknown"


def _build_list_item(row: dict, company_id: str, export_country: str,
                     foreign_hq_detected: bool, foreign_hq_reason,
                     now_iso: str) -> dict:
    score = to_float(row.get("commercial_fit_score"))
    if score is None:
        score = to_float(row.get("commercial_fit_score_app"))
    if score is None:
        score = 0.0

    tier = clean_str(row.get("commercial_tier"))
    if tier is None:
        tier = clean_str(row.get("commercial_tier_app"))
    if tier is None:
        tier = "D"

    return {
        "company_id": company_id,
        "company_name": clean_str(row.get("company_name")),
        "domain": clean_str(row.get("domain")),
        # The UI/CLI-selected export country is authoritative for the export.
        "country": export_country,
        "input_country": export_country,
        "display_country_app": export_country,
        "original_input_country": clean_str(row.get("input_country")),
        "export_country": export_country,
        "assigned_cold_caller": None,       # filled in after sorting
        "assigned_cold_caller_rank": None,  # filled in after sorting
        "foreign_hq_detected_for_export": foreign_hq_detected,
        "foreign_hq_export_reason": foreign_hq_reason,
        "industry": _resolve_industry(row),
        "employee_range": clean_str(row.get("employee_range")) or "",
        "size_category_app": clean_str(row.get("size_category_app")),
        "display_size_category_app": clean_str(row.get("display_size_category_app")),
        "commercial_fit_score": score,
        "commercial_tier": tier,
        "commercial_fit_score_app": to_float(row.get("commercial_fit_score_app")),
        "commercial_tier_app": clean_str(row.get("commercial_tier_app")),
        "display_tier_app": clean_str(row.get("display_tier_app")),
        "outreach_readiness_status": clean_str(row.get("outreach_readiness_status")) or "ready",
        "has_detail": to_bool(row.get("has_detail")) if clean_value(row.get("has_detail")) is not None else True,
        "has_contacts": to_bool(row.get("has_contacts")) if clean_value(row.get("has_contacts")) is not None else False,
        "last_updated": clean_str(row.get("last_updated")) or now_iso,
        "last_updated_at": clean_str(row.get("last_updated_at")) or now_iso,
        "detail_bucket": None,              # filled in during bucketing
        "enrichment_skipped": to_bool(row.get("enrichment_skipped")),
        "enrichment_skip_reason": clean_str(row.get("enrichment_skip_reason")),
        "sig_foreign_hq_score_for_next_scoring": to_float(
            row.get("sig_foreign_hq_score_for_next_scoring")),
        "c5_adjudication": clean_str(row.get("c5_adjudication")),
        "c5_confidence": clean_str(row.get("c5_confidence")),
        "c5_parent_company": clean_str(row.get("c5_parent_company")),
        "c5_parent_hq_country": clean_str(row.get("c5_parent_hq_country")),
        "c5_parent_hq_city": clean_str(row.get("c5_parent_hq_city")),
        "foreign_hq_country_app": clean_str(row.get("foreign_hq_country_app")),
        "foreign_hq_city_app": clean_str(row.get("foreign_hq_city_app")),
    }


def _build_debug_row(row: dict, warnings: list[str]) -> dict:
    """Preserve unmapped Enriched Leads columns under debug.lead_prioritizer_row.

    Never includes secrets (the workbook contains none); oversized raw AI JSON
    columns are omitted with a manifest warning.
    """
    debug_row = {}
    for key, value in row.items():
        if key in _CONSUMED_COLUMNS:
            continue
        cleaned = clean_value(value)
        if cleaned is None:
            continue
        if isinstance(cleaned, str) and len(cleaned) > _MAX_RAW_JSON_CHARS:
            warnings.append(
                f"Omitted oversized field {key!r} "
                f"({len(cleaned)} chars) for source_index={row.get('source_index')}"
            )
            continue
        debug_row[key] = cleaned
    return debug_row


def _own_domains_for_row(row: dict) -> "set[str]":
    """Registrable domains that count as "the lead's own" for source/evidence
    relevance checks: its domain/normalized_domain plus website/careers URLs."""
    domains: set[str] = set()
    for key in ("normalized_domain", "domain", "website_url", "careers_url"):
        value = clean_str(row.get(key))
        if not value:
            continue
        host = hostname_of(value) if ("://" in value or "/" in value) else value
        host = (host or value).lower().removeprefix("www.")
        if host:
            domains.add(_registrable_domain(host))
    return domains


def _build_url_context(evidence_rows: list[dict], signal_rows: list[dict]) -> "dict[str, str]":
    """url -> nearby title/snippet/quote text, used only to check whether a
    third-party page actually mentions the lead's company name."""
    context: dict[str, str] = {}

    def _merge(url, *texts) -> None:
        url = clean_str(url)
        if not url:
            return
        text = " ".join(t for t in (clean_str(t) for t in texts) if t)
        if text:
            context[url] = (context.get(url, "") + " " + text).strip()

    for ev in evidence_rows:
        _merge(ev.get("source_url"), ev.get("source_title"), ev.get("source_snippet"))
    for sig in signal_rows:
        _merge(sig.get("evidence_url"), sig.get("evidence_title"),
                sig.get("evidence_quote"), sig.get("signal_reason"))
    return context


def _build_detail_record(
    row: dict,
    list_item: dict,
    evidence_rows: list[dict],
    signal_rows: list[dict],
    run_metadata: dict | None,
    warnings: list[str],
) -> dict:
    key_source_links = parse_key_source_links(row.get("key_source_links_app"))
    evidence_snippets = build_evidence_snippets(evidence_rows)
    source_urls = build_source_urls(evidence_rows, key_source_links, signal_rows)

    detail = dict(list_item)
    detail.update({
        "website_url": clean_str(row.get("website_url")),
        "linkedin_url": clean_str(row.get("linkedin_url")),
        "careers_url": clean_str(row.get("careers_url")),
        "why_relevant_app": clean_str(row.get("why_relevant_app")),
        "what_is_hot_app": parse_array_field(row.get("what_is_hot_app")),
        "what_is_not_app": parse_array_field(row.get("what_is_not_app")),
        "caller_angle_app": clean_str(row.get("caller_angle_app")),
        "call_starter_app": clean_str(row.get("call_starter_app")),
        "caution_app": clean_str(row.get("caution_app")),
        "cold_caller_summary_app": clean_str(row.get("cold_caller_summary_app")),
        "parent_hq_summary_app": clean_str(row.get("parent_hq_summary_app")),
        "evidence_summary_app": clean_str(row.get("evidence_summary_app")),
        "key_source_links_app": key_source_links,
        "advanced_notes_app": clean_str(row.get("advanced_notes_app")),
        "buyer_route_app": parse_array_field(row.get("buyer_route_app")),
        "likely_training_interest_app": parse_array_field(
            row.get("likely_training_interest_app")),
        "foreign_hq_signal_used_in_app": to_bool(row.get("foreign_hq_signal_used_in_app")),
        "competitor_signal_used_in_app": to_bool(row.get("competitor_signal_used_in_app")),
        "competitor_signal_suppressed": to_bool(row.get("competitor_signal_suppressed")),
        # Sector / industry detection (audit & app metadata — never scoring)
        "detected_industry": clean_str(row.get("detected_industry")),
        "detected_sub_industry": clean_str(row.get("detected_sub_industry")),
        "detected_company_type": clean_str(row.get("detected_company_type")),
        "sector_confidence": clean_str(row.get("sector_confidence")),
        "sector_reason": clean_str(row.get("sector_reason")),
        "sector_evidence_url": clean_str(row.get("sector_evidence_url")),
        "sector_evidence_quote": clean_str(row.get("sector_evidence_quote")),
        "sector_source_title": clean_str(row.get("sector_source_title")),
        "visible_icp_signal_scores": build_visible_icp_signal_scores(
            row, signal_rows, list_item["foreign_hq_detected_for_export"]),
        "evidence_snippets": evidence_snippets,
        "source_urls": source_urls,
        "serper_result_titles": build_serper_result_titles(evidence_rows),
        "raw_google_evidence_count": len(evidence_rows),
        "evidence_audit": build_evidence_audit(
            row, evidence_rows, signal_rows, evidence_snippets, source_urls,
            run_metadata),
        "quality_flags": build_quality_flags(row),
        "domain_quality": clean_str(row.get("domain_quality")),
        "debug": {
            "lead_prioritizer_row": _build_debug_row(row, warnings),
            "evidence_rows_count": len(evidence_rows),
            "signals_rows_count": len(signal_rows),
        },
    })
    foreign_hq_detected = list_item["foreign_hq_detected_for_export"]
    visible_signals = detail["visible_icp_signal_scores"]
    employee_range = list_item.get("employee_range")
    parent_company = resolve_parent_company(row)
    parent_hq_country = resolve_parent_hq_country(row)
    company_name = detail["company_name"]
    own_domains = _own_domains_for_row(row)
    url_context = _build_url_context(evidence_rows, signal_rows)

    detail["ui_payload"] = {
        "why_relevant": build_ui_payload_why_relevant(
            company_name, list_item.get("export_country"),
            list_item.get("industry"), foreign_hq_detected,
            parent_company, parent_hq_country, visible_signals, employee_range,
            own_domains,
        ),
        "what_is_hot": build_ui_payload_what_is_hot(
            foreign_hq_detected, parent_hq_country, employee_range, visible_signals,
            own_domains, company_name,
        ),
        "what_is_not": detail["what_is_not_app"],
        "caller_angle": detail["caller_angle_app"],
        "call_starter": detail["call_starter_app"],
        "cold_caller_summary": detail["cold_caller_summary_app"],
        "parent_hq_summary": detail["parent_hq_summary_app"],
        "evidence_summary": detail["evidence_summary_app"],
        "commercial_fit_drivers": build_commercial_fit_drivers(
            visible_signals, employee_range, own_domains, company_name),
        "caution": build_ui_payload_caution(detail["quality_flags"], detail["caution_app"]),
        "source_urls": build_ui_payload_source_urls(
            detail["website_url"], detail["careers_url"], detail["linkedin_url"],
            detail["source_urls"], own_domains, company_name, url_context,
        ),
    }
    return detail


# ---------------------------------------------------------------------------
# Optional Dutch content localization (DEMO ONLY)
# ---------------------------------------------------------------------------
# Deterministic template rebuild (see lovable_content_localization.py) — no
# AI translation, no external calls, no new dependency. English stays the
# byte-for-byte default behavior. Only caller-facing text values in the
# detail records are ever touched; JSON field names (schema) never change,
# and IDs/domains/URLs/scores/tiers/source titles/snippets/evidence_audit/
# debug fields are always copied through unchanged.

SUPPORTED_CONTENT_LANGUAGES: tuple[str, ...] = ("English", "Dutch", "Italian")
DEFAULT_CONTENT_LANGUAGE = "English"

# Flat caller-facing text fields on a detail record eligible for demo
# localization, mapped to their whole-template rebuilder. Everything else
# (IDs, URLs, scores, evidence_audit, debug, source titles/snippets,
# advanced_notes_app, buyer_route_app, likely_training_interest_app, ...) is
# left alone — there is no known safe template to rebuild them from.
_APP_FIELD_LOCALIZERS_NL: dict[str, "Callable[[object], object]"] = {
    "why_relevant_app": localize_why_relevant_app,
    "caller_angle_app": localize_caller_angle_app,
    "call_starter_app": localize_call_starter_app,
    "caution_app": localize_caution_app,
    "cold_caller_summary_app": localize_cold_caller_summary_app,
    "parent_hq_summary_app": localize_parent_hq_summary_app,
    "evidence_summary_app": localize_evidence_summary_app,
}
_APP_FIELD_LOCALIZERS_IT: dict[str, "Callable[[object], object]"] = {
    "why_relevant_app": localize_why_relevant_app_it,
    "caller_angle_app": localize_caller_angle_app_it,
    "call_starter_app": localize_call_starter_app_it,
    "caution_app": localize_caution_app_it,
    "cold_caller_summary_app": localize_cold_caller_summary_app_it,
    "parent_hq_summary_app": localize_parent_hq_summary_app_it,
    "evidence_summary_app": localize_evidence_summary_app_it,
}

# List fields (already split into individual items by parse_array_field)
# localized item-by-item.
_APP_LIST_FIELD_ITEM_LOCALIZERS_NL: dict[str, "Callable[[object], object]"] = {
    "what_is_hot_app": localize_what_is_hot_item,
    "what_is_not_app": localize_what_is_not_item,
}
_APP_LIST_FIELD_ITEM_LOCALIZERS_IT: dict[str, "Callable[[object], object]"] = {
    "what_is_hot_app": localize_what_is_hot_item_it,
    "what_is_not_app": localize_what_is_not_item_it,
}

# Nested ui_payload mirror fields eligible for the same demo localization —
# same rebuild logic as the matching flat *_app field above.
_UI_PAYLOAD_FIELD_LOCALIZERS_NL: dict[str, "Callable[[object], object]"] = {
    "why_relevant": localize_why_relevant_app,
    "caller_angle": localize_caller_angle_app,
    "call_starter": localize_call_starter_app,
    "cold_caller_summary": localize_cold_caller_summary_app,
    "parent_hq_summary": localize_parent_hq_summary_app,
    "evidence_summary": localize_evidence_summary_app,
}
_UI_PAYLOAD_FIELD_LOCALIZERS_IT: dict[str, "Callable[[object], object]"] = {
    "why_relevant": localize_why_relevant_app_it,
    "caller_angle": localize_caller_angle_app_it,
    "call_starter": localize_call_starter_app_it,
    "cold_caller_summary": localize_cold_caller_summary_app_it,
    "parent_hq_summary": localize_parent_hq_summary_app_it,
    "evidence_summary": localize_evidence_summary_app_it,
}
_UI_PAYLOAD_LIST_FIELD_ITEM_LOCALIZERS_NL: dict[str, "Callable[[object], object]"] = {
    "what_is_hot": localize_what_is_hot_item,
    "what_is_not": localize_what_is_not_item,
}
_UI_PAYLOAD_LIST_FIELD_ITEM_LOCALIZERS_IT: dict[str, "Callable[[object], object]"] = {
    "what_is_hot": localize_what_is_hot_item_it,
    "what_is_not": localize_what_is_not_item_it,
}


def normalize_content_language(language) -> str:
    """Canonical content-language name ("English", "Dutch", or "Italian").

    Case/whitespace-insensitive; anything unrecognized (blank, typo, None)
    falls back to "English" — a demo option must never break the export.
    """
    text = str(language or "").strip()
    for supported in SUPPORTED_CONTENT_LANGUAGES:
        if text.lower() == supported.lower():
            return supported
    return DEFAULT_CONTENT_LANGUAGE


def should_localize_content(language) -> bool:
    """True only when the (normalized) language is "Dutch" or "Italian".

    Every other value means "keep English" — including unrecognized input,
    so English output is always the safe default.
    """
    return normalize_content_language(language) in ("Dutch", "Italian")


def _localize_detail_record(
    detail: dict,
    app_field_localizers: dict,
    app_list_field_item_localizers: dict,
    ui_payload_field_localizers: dict,
    ui_payload_list_field_item_localizers: dict,
    label_translator,
    foreign_hq_evidence_localizer,
) -> tuple[dict, int, int]:
    """Return a localized copy of one detail record for the demo, given a
    specific language's localizer functions.

    Only the caller-facing fields in scope are touched: the flat ``*_app``
    text/list fields, their ``ui_payload`` mirrors, and the
    ``visible_icp_signal_scores`` label/evidence (the evidence only for the
    app-generated foreign-HQ row — never another signal's evidence_quote or
    reason, which may hold external source text). Every field is rebuilt
    from a matched whole English template into a complete target-language
    sentence (see ``lovable_content_localization.py``); unmatched/custom
    text is left in English untouched rather than guessed at. Every other
    key (IDs, domain, URLs, scores, tiers, ``source_urls``,
    ``evidence_snippets``, ``evidence_audit``, ``debug``, sector/HQ/C5
    technical fields, ...) is carried over unchanged — the input ``detail``
    dict itself is never mutated. Returns
    ``(localized_detail, localized_field_count, unchanged_field_count)`` for
    the manifest's audit summary.
    """
    localized = dict(detail)
    localized_count = 0
    unchanged_count = 0

    def _apply_flat(container: dict, field: str, localizer) -> None:
        nonlocal localized_count, unchanged_count
        value = container.get(field)
        if not value:
            return
        new_value = localizer(value)
        container[field] = new_value
        if new_value != value:
            localized_count += 1
        else:
            unchanged_count += 1

    def _apply_list(container: dict, field: str, item_localizer) -> None:
        nonlocal localized_count, unchanged_count
        values = container.get(field)
        if not values:
            return
        new_values = []
        for value in values:
            new_value = item_localizer(value)
            new_values.append(new_value)
            if new_value != value:
                localized_count += 1
            else:
                unchanged_count += 1
        container[field] = new_values

    for field, localizer in app_field_localizers.items():
        _apply_flat(localized, field, localizer)
    for field, item_localizer in app_list_field_item_localizers.items():
        _apply_list(localized, field, item_localizer)

    ui_payload = localized.get("ui_payload")
    if isinstance(ui_payload, dict):
        new_ui_payload = dict(ui_payload)
        for field, localizer in ui_payload_field_localizers.items():
            _apply_flat(new_ui_payload, field, localizer)
        for field, item_localizer in ui_payload_list_field_item_localizers.items():
            _apply_list(new_ui_payload, field, item_localizer)
        localized["ui_payload"] = new_ui_payload

    signal_scores = localized.get("visible_icp_signal_scores")
    if isinstance(signal_scores, list):
        new_scores = []
        for entry in signal_scores:
            if not isinstance(entry, dict):
                new_scores.append(entry)
                continue
            new_entry = dict(entry)
            label = new_entry.get("label")
            if label:
                translated_label = label_translator(label)
                new_entry["label"] = translated_label
                if translated_label != label:
                    localized_count += 1
                else:
                    unchanged_count += 1
            if label == FOREIGN_HQ_SIGNAL_LABEL:
                evidence = new_entry.get("evidence")
                if evidence:
                    new_evidence = foreign_hq_evidence_localizer(evidence)
                    new_entry["evidence"] = new_evidence
                    if new_evidence != evidence:
                        localized_count += 1
                    else:
                        unchanged_count += 1
            new_scores.append(new_entry)
        localized["visible_icp_signal_scores"] = new_scores

    return localized, localized_count, unchanged_count


def localize_detail_record_for_dutch(detail: dict) -> tuple[dict, int, int]:
    """Dutch-localized copy of one detail record — see ``_localize_detail_record``."""
    return _localize_detail_record(
        detail,
        _APP_FIELD_LOCALIZERS_NL,
        _APP_LIST_FIELD_ITEM_LOCALIZERS_NL,
        _UI_PAYLOAD_FIELD_LOCALIZERS_NL,
        _UI_PAYLOAD_LIST_FIELD_ITEM_LOCALIZERS_NL,
        translate_known_label,
        localize_foreign_hq_evidence_text,
    )


def localize_detail_record_for_italian(detail: dict) -> tuple[dict, int, int]:
    """Italian-localized copy of one detail record — see ``_localize_detail_record``."""
    return _localize_detail_record(
        detail,
        _APP_FIELD_LOCALIZERS_IT,
        _APP_LIST_FIELD_ITEM_LOCALIZERS_IT,
        _UI_PAYLOAD_FIELD_LOCALIZERS_IT,
        _UI_PAYLOAD_LIST_FIELD_ITEM_LOCALIZERS_IT,
        translate_known_label_it,
        localize_foreign_hq_evidence_text_it,
    )


# ---------------------------------------------------------------------------
# Workbook reading
# ---------------------------------------------------------------------------

def _read_workbook(input_xlsx: Path, warnings: list[str]):
    """Read the workbook sheets. Enriched Leads is required.

    Uses ``pd.ExcelFile`` as a context manager so the underlying file handle
    is closed before this returns — on Windows a lingering open handle makes
    the source .xlsx appear locked ([WinError 32]) to any follow-up
    operation (cleanup, re-export, download).
    """
    with pd.ExcelFile(input_xlsx) as xls:
        sheets_found = list(xls.sheet_names)

        if ENRICHED_SHEET not in xls.sheet_names:
            raise LovableExportError(
                f"Required sheet {ENRICHED_SHEET!r} not found in "
                f"{input_xlsx.name!r}. Sheets present: {sheets_found}"
            )
        enriched = xls.parse(ENRICHED_SHEET)

        def _optional(sheet):
            if sheet in xls.sheet_names:
                return xls.parse(sheet)
            warnings.append(
                f"Optional sheet {sheet!r} not found; continuing without it.")
            return pd.DataFrame()

        evidence = _optional(EVIDENCE_SHEET)
        signals = _optional(SIGNALS_SHEET)
        run_summary = _optional(RUN_SUMMARY_SHEET)
    return enriched, evidence, signals, run_summary, sheets_found


def _rows_by_source_index(df: pd.DataFrame) -> dict:
    grouped: dict = {}
    if df.empty or "source_index" not in df.columns:
        return grouped
    for record in df.to_dict(orient="records"):
        idx = normalize_source_index(record.get("source_index"))
        if idx is None:
            continue
        grouped.setdefault(idx, []).append(record)
    return grouped


def _run_metadata_from_summary(run_summary: pd.DataFrame) -> dict:
    if run_summary.empty:
        return {}
    record = run_summary.to_dict(orient="records")[0]
    return {k: clean_value(v) for k, v in record.items()
            if clean_value(v) is not None}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_export(
    output_dir: Path,
    list_items: list[dict],
    details_by_bucket: dict[str, dict],
    evidence_by_index: dict,
    exported_rows: list[dict],
    foreign_hq_only: bool,
    warnings: list[str],
) -> dict:
    """Post-export validation. Structural errors raise; soft issues warn."""
    errors: list[str] = []

    list_path = output_dir / "companies.list.json"
    if not list_path.exists():
        errors.append("companies.list.json was not written.")
    else:
        try:
            json.loads(list_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"companies.list.json is not valid JSON: {exc}")

    details_all: dict[str, dict] = {}
    for bucket_file, bucket in details_by_bucket.items():
        if not (output_dir / bucket_file).exists():
            errors.append(f"Detail bucket file missing: {bucket_file}")
        details_all.update(bucket)

    for item in list_items:
        cid = item["company_id"]
        bucket_file = item.get("detail_bucket")
        if not bucket_file:
            errors.append(f"List item {cid} has no detail_bucket.")
            continue
        bucket = details_by_bucket.get(bucket_file)
        if bucket is None:
            errors.append(f"List item {cid} references unknown bucket {bucket_file}.")
        elif cid not in bucket:
            errors.append(f"Company {cid} missing from bucket {bucket_file}.")
        if not item.get("assigned_cold_caller"):
            errors.append(f"Company {cid} has no assigned_cold_caller.")
        if foreign_hq_only and not item.get("foreign_hq_detected_for_export"):
            errors.append(
                f"foreign_hq_only export contains non-foreign-HQ row: {cid}")

    for detail in details_all.values():
        cid = detail["company_id"]
        for array_field in ("evidence_snippets", "source_urls",
                            "visible_icp_signal_scores"):
            if not isinstance(detail.get(array_field), list):
                errors.append(f"Company {cid}: {array_field} is not an array.")
        if detail.get("foreign_hq_detected_for_export"):
            labels = [s.get("label") for s in detail.get("visible_icp_signal_scores", [])]
            # Accept the Dutch/Italian demo translation of the label too —
            # this check is about the signal row being present, not its
            # display language.
            _valid_foreign_hq_labels = {
                FOREIGN_HQ_SIGNAL_LABEL,
                translate_known_label(FOREIGN_HQ_SIGNAL_LABEL),
                translate_known_label_it(FOREIGN_HQ_SIGNAL_LABEL),
            }
            if not any(label in _valid_foreign_hq_labels for label in labels):
                errors.append(
                    f"Foreign-HQ company {cid} missing "
                    f"{FOREIGN_HQ_SIGNAL_LABEL!r} in visible_icp_signal_scores.")

    # Evidence coverage checks (warn — data issues, not structural errors).
    detail_by_id = details_all
    for row, item in exported_rows:
        idx = normalize_source_index(row.get("source_index"))
        ev_rows = evidence_by_index.get(idx, [])
        detail = detail_by_id.get(item["company_id"], {})
        if any(clean_str(ev.get("source_url")) for ev in ev_rows):
            if not detail.get("source_urls"):
                warnings.append(
                    f"Company {item['company_id']} has Evidence source_url rows "
                    "but empty source_urls.")
        if any(clean_str(ev.get("source_snippet")) for ev in ev_rows):
            if not detail.get("evidence_snippets"):
                warnings.append(
                    f"Company {item['company_id']} has Evidence source_snippet "
                    "rows but empty evidence_snippets.")

    if errors:
        raise LovableExportError(
            "Export validation failed:\n" + "\n".join(f"- {e}" for e in errors))

    return {
        "list_items_validated": len(list_items),
        "detail_records_validated": len(details_all),
        "structural_errors": 0,
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export_workbook_to_lovable_json(
    input_xlsx: str | Path,
    output_dir: str | Path,
    export_country: str,
    cold_callers: list[str],
    include_skipped: bool = False,
    foreign_hq_only: bool = True,
    bucket_size: int = 500,
    content_language: str = DEFAULT_CONTENT_LANGUAGE,
) -> dict:
    """Convert a Lead Prioritizer workbook into Lovable Company Hub JSON files.

    ``content_language`` is a small demo option ("English" default, "Dutch",
    or "Italian"): when Dutch or Italian is selected, only caller-facing text
    values in the detail records are localized via deterministic
    whole-template rebuild (see ``localize_detail_record_for_dutch``,
    ``localize_detail_record_for_italian``, and
    ``lovable_content_localization.py``) — no AI translation, no external
    calls. "English" (or any unrecognized value) leaves behavior
    byte-for-byte identical to before this option existed. The JSON schema
    (field names) never changes either way.

    Returns the export manifest dict (also written to export_manifest.json).
    """
    input_xlsx = Path(input_xlsx)
    output_dir = Path(output_dir)

    export_country = (export_country or "").strip()
    if not export_country:
        raise LovableExportError("export_country is required.")
    cold_callers = [c.strip() for c in (cold_callers or []) if c and c.strip()]
    if not cold_callers:
        raise LovableExportError("At least one cold caller is required.")
    if bucket_size < 1:
        raise LovableExportError("bucket_size must be >= 1.")
    content_language = normalize_content_language(content_language)

    warnings: list[str] = []
    enriched, evidence, signals, run_summary, sheets_found = _read_workbook(
        input_xlsx, warnings)

    evidence_by_index = _rows_by_source_index(evidence)
    signals_by_index = _rows_by_source_index(signals)
    run_metadata = _run_metadata_from_summary(run_summary)
    run_mode = clean_str(run_metadata.get("run_mode"))
    now_iso = datetime.now().isoformat(timespec="seconds")

    total_rows_read = len(enriched)
    skipped_rows_excluded = 0
    non_foreign_hq_rows_excluded = 0

    selected: list[tuple[dict, bool, str | None]] = []
    for row in enriched.to_dict(orient="records"):
        if to_bool(row.get("enrichment_skipped")) and not include_skipped:
            skipped_rows_excluded += 1
            continue
        detected, reason = detect_foreign_hq_for_export(row, run_mode)
        if foreign_hq_only and not detected:
            non_foreign_hq_rows_excluded += 1
            continue
        selected.append((row, detected, reason))

    # Sort by score descending before cold caller assignment.
    selected.sort(key=lambda entry: score_for_sort(entry[0])[0], reverse=True)
    score_fields_used = [f for f in SCORE_FIELD_PRECEDENCE
                         if any(to_float(row.get(f)) is not None
                                for row, _, _ in selected)]
    score_sort_field_used = score_fields_used[0] if score_fields_used else None

    used_ids: set = set()
    list_items: list[dict] = []
    detail_records: list[dict] = []
    exported_rows: list[tuple[dict, dict]] = []
    caller_distribution = {caller: 0 for caller in cold_callers}

    # Audit-only diagnostics for industry/sector resolution (never affects the
    # exported schema) — which input-column alias supplied the industry, or
    # "Unknown" when none did.
    industry_unknown_count = 0
    industry_known_count = 0
    industry_source_counts: dict[str, int] = {}

    for rank, (row, detected, reason) in enumerate(selected, start=1):
        caller = cold_callers[(rank - 1) % len(cold_callers)]
        caller_distribution[caller] += 1
        company_id = make_company_id(row, used_ids)
        item = _build_list_item(row, company_id, export_country,
                                detected, reason, now_iso)
        item["assigned_cold_caller"] = caller
        item["assigned_cold_caller_rank"] = rank

        _, industry_source = _first_non_unknown(row, _INDUSTRY_COLUMN_ALIASES)
        if industry_source is None:
            industry_unknown_count += 1
        else:
            industry_known_count += 1
            industry_source_counts[industry_source] = (
                industry_source_counts.get(industry_source, 0) + 1)

        idx = normalize_source_index(row.get("source_index"))
        detail = _build_detail_record(
            row, item,
            evidence_by_index.get(idx, []),
            signals_by_index.get(idx, []),
            run_metadata or None,
            warnings,
        )
        list_items.append(item)
        detail_records.append(detail)
        exported_rows.append((row, item))

    # ── Optional Dutch/Italian content localization (demo only) ───────────
    # Applied only to detail records — the light list items never carry
    # caller-facing free text. English (the default) leaves detail_records
    # completely untouched.
    localized_field_count = 0
    unchanged_field_count = 0
    if should_localize_content(content_language):
        localize_detail_record = (
            localize_detail_record_for_dutch if content_language == "Dutch"
            else localize_detail_record_for_italian)
        localized_details = []
        for detail in detail_records:
            localized_detail, localized_n, unchanged_n = (
                localize_detail_record(detail))
            localized_details.append(localized_detail)
            localized_field_count += localized_n
            unchanged_field_count += unchanged_n
        detail_records = localized_details

    # Bucketing: assign detail_bucket and write bucket files.
    output_dir.mkdir(parents=True, exist_ok=True)
    details_by_bucket: dict[str, dict] = {}
    for i in range(0, len(detail_records), bucket_size):
        bucket_no = i // bucket_size
        bucket_file = f"company-details-{bucket_no:03d}.json"
        bucket = {}
        for item, detail in zip(list_items[i:i + bucket_size],
                                detail_records[i:i + bucket_size]):
            item["detail_bucket"] = bucket_file
            detail["detail_bucket"] = bucket_file
            bucket[item["company_id"]] = detail
        details_by_bucket[bucket_file] = bucket

    output_files = []
    list_path = output_dir / "companies.list.json"
    list_path.write_text(
        json.dumps(list_items, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    output_files.append(str(list_path))
    for bucket_file, bucket in details_by_bucket.items():
        bucket_path = output_dir / bucket_file
        bucket_path.write_text(
            json.dumps(bucket, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        output_files.append(str(bucket_path))

    validation_summary = _validate_export(
        output_dir, list_items, details_by_bucket, evidence_by_index,
        exported_rows, foreign_hq_only, warnings)

    manifest = {
        "generated_at": now_iso,
        "input_xlsx_name": input_xlsx.name,
        "export_country": export_country,
        "total_rows_read": total_rows_read,
        "rows_exported": len(list_items),
        "skipped_rows_excluded": skipped_rows_excluded,
        "include_skipped": include_skipped,
        "foreign_hq_only": foreign_hq_only,
        "foreign_hq_rows_exported": sum(
            1 for item in list_items if item["foreign_hq_detected_for_export"]),
        "non_foreign_hq_rows_excluded": non_foreign_hq_rows_excluded,
        "bucket_size": bucket_size,
        "bucket_count": len(details_by_bucket),
        "cold_callers": cold_callers,
        "caller_distribution": caller_distribution,
        "score_sort_field_used": score_sort_field_used,
        "source_sheets_found": sheets_found,
        "warnings": warnings,
        "validation_summary": validation_summary,
        "industry_resolution_summary": {
            "unknown_count": industry_unknown_count,
            "known_count": industry_known_count,
            "source_counts": industry_source_counts,
        },
        "content_language": content_language,
        "localization": (
            {
                "enabled": True,
                "mode": "deterministic_demo",
                "localized_field_count": localized_field_count,
                "unchanged_field_count": unchanged_field_count,
            }
            if should_localize_content(content_language)
            else {"enabled": False}
        ),
        "output_files": output_files,
    }

    manifest_path = output_dir / "export_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    manifest["output_files"].append(str(manifest_path))
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return manifest


# ---------------------------------------------------------------------------
# In-memory batch output tables -> Lovable JSON (no separate saved .xlsx step)
# ---------------------------------------------------------------------------

def export_batch_output_tables_to_lovable_json(
    output_tables: dict,
    output_dir: str | Path,
    export_country: str,
    cold_callers: list[str],
    include_skipped: bool = False,
    foreign_hq_only: bool = True,
    bucket_size: int = 500,
    content_language: str = DEFAULT_CONTENT_LANGUAGE,
) -> dict:
    """Export Lead Prioritizer batch output tables straight to Lovable JSON.

    ``output_tables`` is the same ``{"enriched_leads": ..., "evidence": ...,
    "signals": ..., "run_summary": ...}`` dict of DataFrames the Streamlit
    batch app already builds before writing the Excel workbook (see
    ``lead_prioritizer_batch_core.build_excel_workbook_bytes``). This avoids
    the manual "download Excel, then re-upload it to a separate exporter"
    step without duplicating any of the exporter logic above: it writes the
    tables to a temporary workbook and delegates straight to
    ``export_workbook_to_lovable_json``. ``content_language`` is passed
    through unchanged (see that function's docstring for the demo Dutch
    localization option).
    """
    import tempfile
    # Lazy import: keeps this module's CLI/workbook-path entry point free of
    # a hard dependency on the Streamlit batch core (mirrors the C5 layer's
    # lazy-import pattern in lead_prioritizer_batch_core.py).
    from lead_prioritizer_batch_core import build_excel_workbook_bytes

    excel_bytes = build_excel_workbook_bytes(output_tables)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(excel_bytes)
        tmp_path = Path(tmp.name)
    try:
        return export_workbook_to_lovable_json(
            input_xlsx=tmp_path,
            output_dir=output_dir,
            export_country=export_country,
            cold_callers=cold_callers,
            include_skipped=include_skipped,
            foreign_hq_only=foreign_hq_only,
            bucket_size=bucket_size,
            content_language=content_language,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a Lead Prioritizer v2 workbook to Lovable "
                    "Company Hub JSON files.")
    parser.add_argument("--input-xlsx", required=True,
                        help="Lead Prioritizer output workbook (.xlsx)")
    parser.add_argument("--output-dir", default="lovable_export",
                        help="Directory for the JSON output files")
    parser.add_argument("--country", required=True,
                        help="Authoritative export country, e.g. Brazil")
    parser.add_argument("--cold-callers", required=True,
                        help='Comma-separated caller names, e.g. "Jantje,Pietje"')
    parser.add_argument("--include-skipped", action="store_true",
                        help="Include rows where enrichment_skipped is True")
    parser.add_argument("--no-foreign-hq-only", dest="foreign_hq_only",
                        action="store_false",
                        help="Also export rows without a detected foreign HQ")
    parser.add_argument("--bucket-size", type=int, default=500,
                        help="Companies per detail bucket file (default 500)")
    parser.add_argument("--content-language", default=DEFAULT_CONTENT_LANGUAGE,
                        choices=list(SUPPORTED_CONTENT_LANGUAGES),
                        help="Demo option: localize caller-facing JSON text "
                             "values (default English).")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    manifest = export_workbook_to_lovable_json(
        input_xlsx=args.input_xlsx,
        output_dir=args.output_dir,
        export_country=args.country,
        cold_callers=[c for c in args.cold_callers.split(",")],
        include_skipped=args.include_skipped,
        foreign_hq_only=args.foreign_hq_only,
        bucket_size=args.bucket_size,
        content_language=args.content_language,
    )
    print(f"Rows read:                 {manifest['total_rows_read']}")
    print(f"Rows exported:             {manifest['rows_exported']}")
    print(f"Skipped rows excluded:     {manifest['skipped_rows_excluded']}")
    print(f"Foreign-HQ rows exported:  {manifest['foreign_hq_rows_exported']}")
    print(f"Non-foreign-HQ excluded:   {manifest['non_foreign_hq_rows_excluded']}")
    print(f"Bucket count:              {manifest['bucket_count']}")
    print(f"Caller distribution:       {manifest['caller_distribution']}")
    for path in manifest["output_files"]:
        print(f"Wrote: {path}")
    for warning in manifest["warnings"]:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
