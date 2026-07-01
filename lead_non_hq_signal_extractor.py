"""Deterministic non-HQ signal extraction for Lead Prioritizer v2 (Step 3).

Converts collected ``LeadEvidence`` into ``LeadSignal`` objects using simple,
deterministic keyword rules — no AI, no live Serper calls, no competitor logic,
and no final commercial scoring.  The produced ``signal_score`` values are
intermediate signal scores, NOT the final commercial fit score.
"""

from __future__ import annotations

from typing import Optional

from lead_output_schema import LeadEvidence, LeadSignal


# Exactly the four supported non-HQ signals and their positive keyword groups.
# (No competitor / alternative-provider / rapid-growth keywords anywhere.)
_SIGNAL_KEYWORDS: dict[str, list[str]] = {
    "international_profile": [
        "international", "global", "worldwide", "countries", "offices",
        "subsidiaries", "locations", "export", "markets", "presence", "group",
    ],
    "onboarding_training_need": [
        "careers", "training", "onboarding", "academy", "learning",
        "development", "employees", "talent", "people", "team", "hiring",
    ],
    "company_size_complexity": [
        "employees", "revenue", "locations", "offices", "subsidiaries", "group",
        "company profile", "annual report", "global", "production sites", "plants",
    ],
    "icp_keyword_match": [
        "corporate training", "sales", "customer service", "support",
        "global teams", "multilingual", "language", "learning", "academy",
        "employees", "international teams",
    ],
}

SUPPORTED_SIGNALS: tuple[str, ...] = tuple(_SIGNAL_KEYWORDS.keys())


def _evidence_text(ev: LeadEvidence) -> str:
    """Human-readable evidence content used for deterministic keyword matching.

    Only the Serper-provided title and snippet are inspected (never the URL) so
    matches are defensible and free of URL-substring noise.
    """
    return " ".join(filter(None, [ev.source_title or "", ev.source_snippet or ""])).lower()


def _first(values: list[Optional[str]]) -> Optional[str]:
    for v in values:
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def extract_non_hq_signals(evidence_items: list[LeadEvidence]) -> list[LeadSignal]:
    """Extract deterministic non-HQ signals from collected evidence.

    Produces at most one ``LeadSignal`` per supported signal name, and only for
    signals that actually have evidence.  No signal is created for a name with
    no evidence.
    """
    signals: list[LeadSignal] = []

    for signal_name in SUPPORTED_SIGNALS:
        group = [e for e in (evidence_items or []) if e.signal_name == signal_name]
        if not group:
            continue  # no evidence → no signal

        keywords = _SIGNAL_KEYWORDS[signal_name]
        combined = " ".join(_evidence_text(e) for e in group)
        matched = [kw for kw in keywords if kw in combined]
        n_hits = len(matched)

        if n_hits >= 2:
            score, value = 2.0, "positive_evidence"
        elif n_hits == 1:
            score, value = 1.0, "weak_evidence"
        else:
            score, value = 0.0, "no_positive_match"

        first_url = _first([e.source_url for e in group])
        has_url = first_url is not None

        if score == 2.0 and has_url:
            confidence = "High"
        elif score == 1.0 and has_url:
            confidence = "Medium"
        else:
            confidence = "Low"

        if matched:
            reason = (
                f"{n_hits} distinct keyword match(es) in evidence: "
                + ", ".join(matched)
            )
        else:
            reason = "No positive keywords matched in available evidence."

        signals.append(LeadSignal(
            signal_name=signal_name,
            signal_value=value,
            signal_score=score,
            signal_confidence=confidence,
            signal_reason=reason,
            evidence_url=first_url,
            evidence_quote=_first([e.source_snippet for e in group]),
            evidence_title=_first([e.source_title for e in group]),
            query_used=_first([e.query_used for e in group]),
            parser_source=_first([e.parser_source for e in group]),
            needs_manual_review=False,
        ))

    return signals


# Result-field name templates per signal.
_RESULT_FIELD_MAP: dict[str, dict[str, str]] = {
    "international_profile": {
        "score": "sig_international_profile_score",
        "reason": "international_profile_reason",
        "evidence_url": "international_profile_evidence_url",
        "evidence_quote": "international_profile_evidence_quote",
    },
    "onboarding_training_need": {
        "score": "sig_onboarding_training_need_score",
        "reason": "onboarding_training_need_reason",
        "evidence_url": "onboarding_training_need_evidence_url",
        "evidence_quote": "onboarding_training_need_evidence_quote",
    },
    "company_size_complexity": {
        "score": "sig_company_size_complexity_score",
        "reason": "company_size_complexity_reason",
        "evidence_url": "company_size_complexity_evidence_url",
        "evidence_quote": "company_size_complexity_evidence_quote",
    },
    "icp_keyword_match": {
        "score": "sig_icp_keyword_match_score",
        "reason": "icp_keyword_match_reason",
        "evidence_url": "icp_keyword_match_evidence_url",
        "evidence_quote": "icp_keyword_match_evidence_quote",
    },
}


def summarize_non_hq_signals_for_result(signals: list[LeadSignal]) -> dict:
    """Map extracted signals onto the flat ``LeadPrioritizationResult`` fields.

    Missing signals map to ``None`` for every field.
    """
    out: dict = {}
    for fields in _RESULT_FIELD_MAP.values():
        for key in fields.values():
            out[key] = None

    by_name = {s.signal_name: s for s in (signals or [])}
    for signal_name, fields in _RESULT_FIELD_MAP.items():
        sig = by_name.get(signal_name)
        if sig is None:
            continue
        out[fields["score"]] = sig.signal_score
        out[fields["reason"]] = sig.signal_reason
        out[fields["evidence_url"]] = sig.evidence_url
        out[fields["evidence_quote"]] = sig.evidence_quote

    return out
