import unittest

from hubspot_audit.control_center import ControlCenterState, build_control_center_payload, PHASES
from hubspot_audit.evidence_ledger import EvidenceLedger
from hubspot_audit.investigation_queue import InvestigationQueue
from hubspot_audit.models import FindingStatus

REQUIRED_CONTRACT_FIELDS = {
    "run_id", "status", "overall_progress_pct", "current_phase", "current_activity_plain_language",
    "latest_update_plain_language", "completed_phases", "next_phase", "confirmed_findings_count",
    "active_investigations_count", "rejected_hypotheses_count", "data_gaps_count", "ai_cost_estimate",
    "ai_budget", "last_heartbeat", "recent_timeline_events", "recent_consultant_decisions",
}


class ControlCenterTests(unittest.TestCase):
    def test_progress_pct_reflects_completed_phases(self):
        state = ControlCenterState(run_id="r1")
        state.complete_phase(PHASES[0][0])
        expected = round(100 / len(PHASES), 1)
        self.assertEqual(state.overall_progress_pct(), expected)

    def test_next_phase_is_first_incomplete(self):
        state = ControlCenterState(run_id="r1")
        state.complete_phase(PHASES[0][0])
        self.assertEqual(state.next_phase(), PHASES[1][0])

    def test_timeline_capped_at_max(self):
        state = ControlCenterState(run_id="r1")
        for i in range(80):
            state.add_timeline_event(f"event {i}")
        self.assertLessEqual(len(state.timeline_events), 50)
        self.assertEqual(state.timeline_events[-1]["text"], "event 79")

    def test_needs_human_sets_status(self):
        state = ControlCenterState(run_id="r1")
        state.mark_needs_human("cannot proceed safely")
        self.assertEqual(state.status, "needs_human")

    def test_payload_contains_all_required_contract_fields(self):
        state = ControlCenterState(run_id="r1")
        ledger = EvidenceLedger()
        ledger.add_finding("f1", "t", "c", [], 0.9, status=FindingStatus.CONFIRMED)
        queue = InvestigationQueue()
        queue.push("q1", "f1", [], 0.9, depth=0)
        cost_summary = {"ai_cost_estimate_eur": 0.0, "ai_budget_eur": 5.0}
        payload = build_control_center_payload(state, ledger, queue, cost_summary)
        missing = REQUIRED_CONTRACT_FIELDS - set(payload.keys())
        self.assertEqual(missing, set())
        self.assertEqual(payload["confirmed_findings_count"], 1)
        self.assertEqual(payload["active_investigations_count"], 1)
        state.data_gaps_count = 2
        payload = build_control_center_payload(state, ledger, queue, cost_summary)
        self.assertEqual(payload["data_gaps_count"], 2)


if __name__ == "__main__":
    unittest.main()
