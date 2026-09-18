"""Plain-language status contract for the mYngle CRM Audit Control Center.

This module is the only place that translates technical phase/finding
events into the plain-language sentences a non-technical CEO reads.
Everything technical (raw counts, record IDs, stack traces) stays in
run_status.json / logs; this file only ever emits short human sentences plus
counters.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .models import now_iso

PHASES = [
    ("capability_probe", "Checking which HubSpot data the consultant is allowed to read."),
    ("extraction", "Pulling a safe, read-only copy of the CRM data."),
    ("normalization", "Cleaning and standardizing the extracted data for analysis."),
    ("company_analysis", "Checking company records for duplicates and missing information."),
    ("contact_analysis", "Checking contact records for duplicates and missing information."),
    ("deal_analysis", "Checking deals for missing links to companies, contacts and owners."),
    ("activity_analysis", "Reviewing calls, meetings, emails, notes and tasks for gaps."),
    ("property_analysis", "Reviewing which CRM fields are actually used."),
    ("pipeline_analysis", "Reviewing sales pipeline stages for stuck or unused steps."),
    ("historical_imports", "Looking for signs of old data imports (e.g. from Salesforce or Apollo)."),
    ("cross_object", "Cross-checking findings across companies, contacts and deals."),
    ("ai_interpretation", "Using low-cost AI to interpret ambiguous patterns, only where useful."),
    ("report_generation", "Writing the final audit report."),
    ("done", "Audit complete."),
]
_PHASE_LABELS = dict(PHASES)
_PHASE_ORDER = [p[0] for p in PHASES]

MAX_TIMELINE_EVENTS = 50
MAX_DECISIONS = 50


@dataclass
class ControlCenterState:
    run_id: str
    status: str = "running"  # running | needs_human | done | failed
    current_phase: str = PHASES[0][0]
    completed_phases: list = field(default_factory=list)
    timeline_events: list = field(default_factory=list)
    consultant_decisions: list = field(default_factory=list)
    data_volumes: dict = field(default_factory=dict)
    data_gaps_count: int = 0

    def start_phase(self, phase: str) -> None:
        self.current_phase = phase
        self.add_timeline_event(_PHASE_LABELS.get(phase, phase))

    def complete_phase(self, phase: str, plain_language_result: Optional[str] = None) -> None:
        if phase not in self.completed_phases:
            self.completed_phases.append(phase)
        if plain_language_result:
            self.add_timeline_event(plain_language_result)

    def add_timeline_event(self, plain_language_text: str) -> None:
        self.timeline_events.append({"timestamp": now_iso(), "text": plain_language_text})
        self.timeline_events = self.timeline_events[-MAX_TIMELINE_EVENTS:]

    def add_decision(self, decision_text: str) -> None:
        self.consultant_decisions.append({"timestamp": now_iso(), "text": decision_text})
        self.consultant_decisions = self.consultant_decisions[-MAX_DECISIONS:]

    def mark_needs_human(self, reason_plain_language: str) -> None:
        self.status = "needs_human"
        self.add_timeline_event(f"NEEDS_HUMAN: {reason_plain_language}")

    def overall_progress_pct(self) -> float:
        return round(100 * len(self.completed_phases) / len(_PHASE_ORDER), 1)

    def next_phase(self) -> Optional[str]:
        for phase in _PHASE_ORDER:
            if phase not in self.completed_phases:
                return phase
        return None


def build_control_center_payload(
    state: ControlCenterState,
    evidence_ledger,
    investigation_queue,
    cost_tracker_summary: dict,
) -> dict:
    finding_counts = evidence_ledger.counts_by_status()
    investigation_counts = investigation_queue.counts_by_status()

    latest_update = state.timeline_events[-1]["text"] if state.timeline_events else "Audit not started yet."

    return {
        "run_id": state.run_id,
        "status": state.status,
        "overall_progress_pct": state.overall_progress_pct(),
        "current_phase": state.current_phase,
        "current_activity_plain_language": _PHASE_LABELS.get(state.current_phase, state.current_phase),
        "latest_update_plain_language": latest_update,
        "completed_phases": state.completed_phases,
        "next_phase": state.next_phase(),
        "confirmed_findings_count": finding_counts.get("confirmed", 0),
        "active_investigations_count": investigation_counts.get("queued", 0) + investigation_counts.get("in_progress", 0),
        "rejected_hypotheses_count": finding_counts.get("rejected", 0),
        "data_gaps_count": state.data_gaps_count,
        "ai_cost_estimate": cost_tracker_summary.get("ai_cost_estimate_eur"),
        "ai_budget": cost_tracker_summary.get("ai_budget_eur"),
        "last_heartbeat": now_iso(),
        "recent_timeline_events": state.timeline_events[-10:],
        "recent_consultant_decisions": state.consultant_decisions[-10:],
        "data_volumes": state.data_volumes,
    }


def write_control_center(payload: dict, path: str) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp_path, path)
