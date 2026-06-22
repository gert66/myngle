"""Lead Prioritizer v2 — orchestration skeleton.

Stage 1 — dormant foundation. No API calls are made here yet.
"""

from __future__ import annotations

from lead_output_schema import LeadInput, LeadPrioritizationResult
from hq_simple_detector import build_simple_hq_query


def prioritize_single_lead(input_row: LeadInput) -> LeadPrioritizationResult:
    """Orchestrate HQ detection and scoring for a single lead.

    Not yet implemented — placeholder only.
    """
    raise NotImplementedError(
        "Lead Prioritizer v2 core is not yet implemented. "
        "This is a stage-1 dormant foundation."
    )
