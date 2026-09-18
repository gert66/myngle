"""Evidence ledger: every material finding traces to source, counts, safe
samples/record IDs, evidence, counter-evidence, confidence, analysis version
and timestamp.

Record IDs are the only "sample" ever stored -- never full record contents,
never PII beyond an ID that's already an internal HubSpot identifier.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .models import EvidenceItem, Finding, FindingStatus, now_iso

ANALYSIS_VERSION = "1.0.0"


class EvidenceLedger:
    def __init__(self):
        self._findings: dict = {}

    def add_finding(
        self,
        finding_id: str,
        title: str,
        category: str,
        evidence: list,
        confidence: float,
        status: FindingStatus = FindingStatus.DETECTED,
        hypothesis: Optional[str] = None,
        counter_evidence: Optional[list] = None,
        recommended_next_investigation: Optional[str] = None,
        potential_remediation: Optional[str] = None,
        reasoning_summary: Optional[str] = None,
    ) -> Finding:
        finding = Finding(
            finding_id=finding_id,
            title=title,
            category=category,
            status=status,
            confidence=confidence,
            analysis_version=ANALYSIS_VERSION,
            evidence=evidence,
            counter_evidence=counter_evidence or [],
            hypothesis=hypothesis,
            recommended_next_investigation=recommended_next_investigation,
            potential_remediation=potential_remediation,
            reasoning_summary=reasoning_summary,
        )
        self._findings[finding_id] = finding
        return finding

    def transition(self, finding_id: str, new_status: FindingStatus, extra_evidence: Optional[list] = None) -> None:
        finding = self._findings[finding_id]
        finding.status = new_status
        finding.updated_at = now_iso()
        if extra_evidence:
            finding.evidence.extend(extra_evidence)

    def get(self, finding_id: str) -> Finding:
        return self._findings[finding_id]

    def all(self) -> list:
        return list(self._findings.values())

    def by_status(self, status: FindingStatus) -> list:
        return [f for f in self._findings.values() if f.status == status]

    def counts_by_status(self) -> dict:
        counts: dict = {}
        for f in self._findings.values():
            counts[f.status.value] = counts.get(f.status.value, 0) + 1
        return counts

    def to_findings_list(self) -> list:
        return [f.to_dict() for f in self._findings.values()]

    def to_evidence_list(self) -> list:
        """Flattened evidence.json: one row per (finding, evidence item)."""
        rows = []
        for f in self._findings.values():
            for ev in f.evidence:
                ev_dict = ev.__dict__ if hasattr(ev, "__dict__") else ev
                rows.append(
                    {
                        "finding_id": f.finding_id,
                        "analysis_version": f.analysis_version,
                        "confidence": f.confidence,
                        "status": f.status.value,
                        **ev_dict,
                    }
                )
        return rows

    def write(self, findings_path: str, evidence_path: str) -> None:
        with open(findings_path, "w", encoding="utf-8") as fh:
            json.dump(self.to_findings_list(), fh, indent=2, default=str)
        with open(evidence_path, "w", encoding="utf-8") as fh:
            json.dump(self.to_evidence_list(), fh, indent=2, default=str)


def make_evidence(source: str, description: str, counts: dict, sample_record_ids: Optional[list] = None,
                   is_counter_evidence: bool = False) -> EvidenceItem:
    return EvidenceItem(
        source=source,
        description=description,
        counts=counts,
        sample_record_ids=(sample_record_ids or [])[:10],
        is_counter_evidence=is_counter_evidence,
    )
