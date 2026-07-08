"""Lusha employee/revenue -> "company_size_complexity" signal priority
(Lusha enrichment plan, Stap 3).

Lusha's own Company Number of Employees / Company Revenue bucket fields
are structured, ground-truth input — when present and parseable, they
take PRIORITY over the existing deterministic Serper-keyword-based
``company_size_complexity`` signal in
``lead_non_hq_signal_extractor.py``. That Serper query/evidence/
extraction path is deliberately NOT removed: it is unchanged and stays
the fallback whenever Lusha data is missing, blank, or not parseable as
a recognisable employee/revenue range/bucket.

``company_size_complexity`` remains an AUDIT-ONLY signal — see
``lead_v2_scoring_adapter.py``'s explicit "H. Size" note ("company_size_
complexity is a v2 AUDIT signal only, not an employee-range replacement").
Neither this module nor the Serper fallback it defers to ever feeds
``final_commercial_fit_score``.
"""

from __future__ import annotations

import re
from typing import Optional

from lead_output_schema import LeadSignal

# A Lusha employee-count bucket upper bound this large is a known Lusha
# sentinel for "no real upper bound" (observed real value:
# "100001-10000000") rather than a literal headcount — collapsed to an
# open-ended "{lower:,}+" label for display/reason text.
_ABSURD_UPPER_THRESHOLD = 1_000_000

_EMPLOYEE_RANGE_RE = re.compile(r"^\s*([\d,]+)\s*-\s*([\d,]+)\s*$")
_EMPLOYEE_OPEN_RE = re.compile(r"^\s*([\d,]+)\s*\+\s*$")
_EMPLOYEE_SINGLE_RE = re.compile(r"^\s*([\d,]+)\s*$")

# Loose validity check for a Lusha revenue bucket string, e.g.
# "$50M - $100M", "$1B - $10B", "$100B+", "$1 - $1M". Not a precise
# parser — just enough to reject blank/placeholder/garbage values so they
# correctly fall back to Serper instead of being trusted as real data.
_REVENUE_BUCKET_RE = re.compile(
    r"^\s*\$?[\d.,]+\s*[KMB]?\s*(?:-\s*\$?[\d.,]+\s*[KMB]?)?\+?\s*$", re.IGNORECASE)

_BLANK_PLACEHOLDER_VALUES = frozenset({
    "", "nan", "none", "unknown", "n/a", "na", "-", "--", "null",
})


def _clean(raw: Optional[str]) -> str:
    s = str(raw or "").strip()
    return "" if s.lower() in _BLANK_PLACEHOLDER_VALUES else s


def normalize_employee_range_label(raw: Optional[str]) -> Optional[str]:
    """Readable display label for a Lusha employee-count bucket.

    The pathological top-bucket upper bound (>= ``_ABSURD_UPPER_THRESHOLD``)
    collapses to ``"{lower:,}+"``; a plain range gets thousands-separators;
    a single value or an already-open ``"N+"`` bucket is normalized the
    same way. Returns ``None`` when blank, a known placeholder ("Unknown",
    "N/A", ...), or not recognisable as any of the above — the caller
    treats that as "not usable" and falls back to Serper.
    """
    s = _clean(raw)
    if not s:
        return None

    m = _EMPLOYEE_RANGE_RE.match(s)
    if m:
        lower = int(m.group(1).replace(",", ""))
        upper = int(m.group(2).replace(",", ""))
        if upper >= _ABSURD_UPPER_THRESHOLD:
            return f"{lower:,}+"
        return f"{lower:,}-{upper:,}"

    m = _EMPLOYEE_OPEN_RE.match(s)
    if m:
        return f"{int(m.group(1).replace(',', '')):,}+"

    m = _EMPLOYEE_SINGLE_RE.match(s)
    if m:
        return f"{int(m.group(1).replace(',', '')):,}"

    return None


def normalize_revenue_label(raw: Optional[str]) -> Optional[str]:
    """Cleaned Lusha revenue bucket string when it looks like a real
    Lusha revenue bucket (e.g. ``"$50M - $100M"``), or ``None`` when
    blank, a known placeholder, or unparseable garbage."""
    s = _clean(raw)
    if not s:
        return None
    return s if _REVENUE_BUCKET_RE.match(s) else None


def lusha_size_signal(
    employees_raw: Optional[str], revenue_raw: Optional[str],
) -> Optional[LeadSignal]:
    """Build a ``company_size_complexity`` ``LeadSignal`` from Lusha
    employee/revenue data.

    Returns ``None`` when NEITHER field is present and parseable — the
    caller then leaves whatever the existing deterministic Serper-based
    extractor already produced for this signal untouched (unchanged
    fallback path, never removed).

    A hit always scores ``2.0`` / ``"positive_evidence"`` / confidence
    ``"High"``: structured Lusha data is higher-trust ground truth than a
    keyword match on a web snippet, even though (matching the existing
    deterministic extractor's own confidence rule) there is no backing
    URL — ``evidence_url`` stays blank; ``parser_source`` records
    ``"lusha_size_data"`` so the origin is always auditable.
    """
    employees_label = normalize_employee_range_label(employees_raw)
    revenue_label = normalize_revenue_label(revenue_raw)

    if not employees_label and not revenue_label:
        return None

    parts = []
    if employees_label:
        parts.append(f"{employees_label} employees")
    if revenue_label:
        parts.append(f"{revenue_label} revenue")
    reason = "Lusha company size data: " + ", ".join(parts) + "."

    return LeadSignal(
        signal_name="company_size_complexity",
        signal_value="positive_evidence",
        signal_score=2.0,
        signal_confidence="High",
        signal_reason=reason,
        evidence_url=None,
        evidence_urls=[],
        evidence_quote=None,
        evidence_title=None,
        query_used=None,
        parser_source="lusha_size_data",
        needs_manual_review=False,
    )
