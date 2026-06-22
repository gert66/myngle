"""Shared output schema for Lead Prioritizer v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LeadInput:
    company_name: str
    domain: Optional[str] = None
    input_country: Optional[str] = None


@dataclass
class HQDetectionResult:
    hq_detected_country: Optional[str] = None
    hq_detected_city: Optional[str] = None
    hq_confidence: Optional[str] = None          # e.g. "high", "medium", "low"
    foreign_hq_simple: Optional[bool] = None
    needs_manual_review: bool = False
    hq_reason: Optional[str] = None
    hq_evidence_url: Optional[str] = None
    hq_evidence_quote: Optional[str] = None


@dataclass
class LeadPrioritizationResult:
    company_name: str
    domain: Optional[str] = None
    input_country: Optional[str] = None

    # HQ detection fields
    hq_detected_country: Optional[str] = None
    hq_detected_city: Optional[str] = None
    hq_confidence: Optional[str] = None
    foreign_hq_simple: Optional[bool] = None
    needs_manual_review: bool = False
    hq_reason: Optional[str] = None
    hq_evidence_url: Optional[str] = None
    hq_evidence_quote: Optional[str] = None

    # Scoring input signals (not scores themselves)
    sig_foreign_hq_score_for_next_scoring: Optional[float] = None
    # Competitor evidence is audit-only; excluded from scoring
    competitor_signal_excluded_from_next_scoring: Optional[str] = None
