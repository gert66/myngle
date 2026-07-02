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
    return text


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
        "industry": clean_str(row.get("industry")) or "Unknown",
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
    detail["ui_payload"] = {
        "why_relevant": detail["why_relevant_app"],
        "what_is_hot": detail["what_is_hot_app"],
        "what_is_not": detail["what_is_not_app"],
        "caller_angle": detail["caller_angle_app"],
        "call_starter": detail["call_starter_app"],
        "cold_caller_summary": detail["cold_caller_summary_app"],
        "parent_hq_summary": detail["parent_hq_summary_app"],
        "evidence_summary": detail["evidence_summary_app"],
        "source_urls": detail["source_urls"],
    }
    return detail


# ---------------------------------------------------------------------------
# Workbook reading
# ---------------------------------------------------------------------------

def _read_workbook(input_xlsx: Path, warnings: list[str]):
    """Read the workbook sheets. Enriched Leads is required."""
    xls = pd.ExcelFile(input_xlsx)
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
        warnings.append(f"Optional sheet {sheet!r} not found; continuing without it.")
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
            if FOREIGN_HQ_SIGNAL_LABEL not in labels:
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
) -> dict:
    """Convert a Lead Prioritizer workbook into Lovable Company Hub JSON files.

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

    for rank, (row, detected, reason) in enumerate(selected, start=1):
        caller = cold_callers[(rank - 1) % len(cold_callers)]
        caller_distribution[caller] += 1
        company_id = make_company_id(row, used_ids)
        item = _build_list_item(row, company_id, export_country,
                                detected, reason, now_iso)
        item["assigned_cold_caller"] = caller
        item["assigned_cold_caller_rank"] = rank

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
