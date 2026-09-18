import unittest

from hubspot_audit.evidence_ledger import EvidenceLedger, make_evidence
from hubspot_audit.models import FindingStatus


class EvidenceLedgerTests(unittest.TestCase):
    def test_add_finding_and_lookup(self):
        ledger = EvidenceLedger()
        ledger.add_finding(
            finding_id="f1",
            title="Test finding",
            category="companies",
            evidence=[make_evidence("companies", "desc", {"count": 3}, ["1", "2", "3"])],
            confidence=0.9,
        )
        finding = ledger.get("f1")
        self.assertEqual(finding.status, FindingStatus.DETECTED)
        self.assertEqual(len(finding.evidence), 1)

    def test_transition_updates_status_and_appends_evidence(self):
        ledger = EvidenceLedger()
        ledger.add_finding("f1", "t", "companies", [], 0.5)
        ledger.transition("f1", FindingStatus.CONFIRMED, extra_evidence=[make_evidence("x", "y", {})])
        finding = ledger.get("f1")
        self.assertEqual(finding.status, FindingStatus.CONFIRMED)
        self.assertEqual(len(finding.evidence), 1)

    def test_counts_by_status(self):
        ledger = EvidenceLedger()
        ledger.add_finding("f1", "t", "c", [], 0.5, status=FindingStatus.CONFIRMED)
        ledger.add_finding("f2", "t", "c", [], 0.5, status=FindingStatus.CONFIRMED)
        ledger.add_finding("f3", "t", "c", [], 0.5, status=FindingStatus.REJECTED)
        counts = ledger.counts_by_status()
        self.assertEqual(counts["confirmed"], 2)
        self.assertEqual(counts["rejected"], 1)

    def test_sample_record_ids_capped_at_ten(self):
        evidence = make_evidence("x", "y", {}, sample_record_ids=[str(i) for i in range(50)])
        self.assertEqual(len(evidence.sample_record_ids), 10)

    def test_to_evidence_list_flattens_finding_and_evidence(self):
        ledger = EvidenceLedger()
        ledger.add_finding("f1", "t", "companies", [make_evidence("src", "desc", {"count": 1})], 0.5)
        rows = ledger.to_evidence_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finding_id"], "f1")
        self.assertEqual(rows[0]["source"], "src")


if __name__ == "__main__":
    unittest.main()
