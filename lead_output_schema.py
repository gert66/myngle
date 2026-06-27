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
    hq_confidence: Optional[str] = None          # "High" | "Medium" | "Low"
    foreign_hq_simple: Optional[bool] = None
    needs_manual_review: bool = False
    hq_reason: Optional[str] = None
    hq_evidence_url: Optional[str] = None
    hq_evidence_quote: Optional[str] = None
    # Parser provenance
    domain_root: Optional[str] = None
    query_used: Optional[str] = None
    parser_source: Optional[str] = None          # e.g. "knowledge_graph", "answer_box", "organic_1"
    # HQ structure type (set by AI path)
    hq_structure_type: Optional[str] = None      # "domestic", "foreign_parent", "regional_branch_only", …
    # Scoring signal
    sig_foreign_hq_score_for_next_scoring: Optional[float] = None
    # AI audit fields (populated only when AI-first path is used)
    ai_hq_model: Optional[str] = None
    ai_hq_classification: Optional[str] = None  # raw AI classification
    ai_hq_confidence: Optional[str] = None      # raw AI confidence
    ai_parent_company: Optional[str] = None
    ai_parent_hq_country: Optional[str] = None
    ai_parent_hq_city: Optional[str] = None
    ai_call_attempted: Optional[str] = None      # "Yes" | "No"
    ai_call_success: Optional[str] = None        # "Yes" | "No"
    ai_hq_error: Optional[str] = None


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

    # HQ structure type
    hq_structure_type: Optional[str] = None
    # Scoring input signals (not scores themselves)
    sig_foreign_hq_score_for_next_scoring: Optional[float] = None
    # Competitor evidence is audit-only; excluded from scoring
    competitor_signal_excluded_from_next_scoring: Optional[str] = None
    # AI audit fields
    ai_hq_model: Optional[str] = None
    ai_hq_classification: Optional[str] = None
    ai_hq_confidence: Optional[str] = None
    ai_parent_company: Optional[str] = None
    ai_parent_hq_country: Optional[str] = None
    ai_parent_hq_city: Optional[str] = None
    ai_call_attempted: Optional[str] = None
    ai_call_success: Optional[str] = None
    ai_hq_error: Optional[str] = None
