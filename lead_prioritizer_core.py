"""Lead Prioritizer v2 — orchestration core.

Implements AI-first HQ detection for a single lead:
  1. Build query via ``build_simple_hq_query()``
  2. Fetch Serper results via ``call_serper_for_hq()``
  3. Interpret HQ with Anthropic Haiku via ``interpret_hq_with_ai()``
  4. Map ``HQDetectionResult`` → ``LeadPrioritizationResult``
"""

from __future__ import annotations

from lead_output_schema import LeadInput, LeadPrioritizationResult, HQDetectionResult
from hq_simple_detector import build_simple_hq_query
from lead_hq_ai_interpreter import call_serper_for_hq, interpret_hq_with_ai

_DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"


def prioritize_single_lead(
    input_row: LeadInput,
    *,
    serper_api_key: str = "",
    anthropic_api_key: str = "",
    ai_model: str = _DEFAULT_AI_MODEL,
    default_input_country: str = "Italy",
) -> LeadPrioritizationResult:
    """Orchestrate HQ detection and scoring for a single lead.

    Requires ``serper_api_key`` and ``anthropic_api_key`` for live operation.
    In tests these can be mocked at the ``call_serper_for_hq`` /
    ``interpret_hq_with_ai`` level.

    ``default_input_country`` supplies the run-context local/entity country when
    ``input_row.input_country`` is blank/None; the effective country is what the
    interpreter compares against and what the result records.
    """
    effective_country = (input_row.input_country or "").strip() or default_input_country

    domain_root, query = build_simple_hq_query(input_row.company_name, input_row.domain)

    serper_payload = call_serper_for_hq(
        domain_root=domain_root,
        query=query,
        serper_api_key=serper_api_key,
    )

    # Use the effective (defaulted) country for interpretation without mutating
    # the caller's input row.
    interp_input = LeadInput(
        company_name=input_row.company_name,
        domain=input_row.domain,
        input_country=effective_country,
    )

    hq: HQDetectionResult = interpret_hq_with_ai(
        lead_input=interp_input,
        domain_root=domain_root,
        query=query,
        serper_payload=serper_payload,
        anthropic_api_key=anthropic_api_key,
        model=ai_model,
    )

    return LeadPrioritizationResult(
        company_name=input_row.company_name,
        domain=input_row.domain,
        input_country=effective_country,
        # HQ location
        hq_detected_country=hq.hq_detected_country,
        hq_detected_city=hq.hq_detected_city,
        hq_confidence=hq.hq_confidence,
        foreign_hq_simple=hq.foreign_hq_simple,
        needs_manual_review=hq.needs_manual_review,
        hq_reason=hq.hq_reason,
        hq_evidence_url=hq.hq_evidence_url,
        hq_evidence_quote=hq.hq_evidence_quote,
        hq_structure_type=hq.hq_structure_type,
        # Scoring
        sig_foreign_hq_score_for_next_scoring=hq.sig_foreign_hq_score_for_next_scoring,
        # Competitor evidence is audit-only and never enters HQ scoring.
        competitor_signal_excluded_from_next_scoring=(
            "Competitor evidence is audit-only and excluded from HQ scoring."
        ),
        # Query / parser provenance
        domain_root=hq.domain_root or domain_root,
        query_used=hq.query_used or query,
        parser_source=hq.parser_source,
        # AI audit
        ai_hq_model=hq.ai_hq_model,
        ai_hq_classification=hq.ai_hq_classification,
        ai_hq_confidence=hq.ai_hq_confidence,
        ai_parent_company=hq.ai_parent_company,
        ai_parent_hq_country=hq.ai_parent_hq_country,
        ai_parent_hq_city=hq.ai_parent_hq_city,
        ai_call_attempted=hq.ai_call_attempted,
        ai_call_success=hq.ai_call_success,
        ai_hq_error=hq.ai_hq_error,
        ai_hq_raw_json=hq.ai_hq_raw_json,
    )
