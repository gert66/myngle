"""Dataclasses shared across the extraction, analysis and reporting layers."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    PARTIALLY_AVAILABLE = "partially_available"
    SCOPE_MISSING = "scope_missing"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class FindingStatus(str, Enum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    NEEDS_HUMAN_CONTEXT = "needs_human_context"


class InvestigationStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DEFERRED_LOW_VALUE = "deferred_low_value"
    NEEDS_HUMAN_CONTEXT = "needs_human_context"
    STOPPED_BUDGET = "stopped_budget"


@dataclass
class CapabilityRecord:
    object_name: str
    status: CapabilityStatus
    detail: str
    downstream_impact: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class Company:
    id: str
    name: Optional[str] = None
    domain: Optional[str] = None
    normalized_domain: Optional[str] = None
    createdate: Optional[str] = None
    hs_lastmodifieddate: Optional[str] = None
    owner_id: Optional[str] = None
    parent_company_id: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    source_properties: dict = field(default_factory=dict)


@dataclass
class Contact:
    id: str
    email: Optional[str] = None
    normalized_email: Optional[str] = None
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    createdate: Optional[str] = None
    hs_lastmodifieddate: Optional[str] = None
    owner_id: Optional[str] = None
    company_ids: list = field(default_factory=list)
    lifecycle_stage: Optional[str] = None
    source_properties: dict = field(default_factory=dict)


@dataclass
class Deal:
    id: str
    dealname: Optional[str] = None
    pipeline: Optional[str] = None
    dealstage: Optional[str] = None
    is_closed: bool = False
    amount: Optional[float] = None
    createdate: Optional[str] = None
    hs_lastmodifieddate: Optional[str] = None
    closedate: Optional[str] = None
    owner_id: Optional[str] = None
    company_ids: list = field(default_factory=list)
    contact_ids: list = field(default_factory=list)
    source_properties: dict = field(default_factory=dict)


@dataclass
class Activity:
    id: str
    kind: str  # call | meeting | email | note | task
    timestamp: Optional[str] = None
    owner_id: Optional[str] = None
    deal_ids: list = field(default_factory=list)
    contact_ids: list = field(default_factory=list)
    company_ids: list = field(default_factory=list)
    completed: Optional[bool] = None
    overdue: Optional[bool] = None
    source_properties: dict = field(default_factory=dict)


@dataclass
class PropertyDefinition:
    object_type: str
    name: str
    label: Optional[str] = None
    field_type: Optional[str] = None
    group_name: Optional[str] = None
    filled_count: int = 0
    total_count: int = 0

    @property
    def fill_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.filled_count / self.total_count


@dataclass
class EvidenceItem:
    source: str
    description: str
    counts: dict = field(default_factory=dict)
    sample_record_ids: list = field(default_factory=list)
    is_counter_evidence: bool = False
    created_at: str = field(default_factory=now_iso)


@dataclass
class Finding:
    finding_id: str
    title: str
    category: str
    status: FindingStatus
    confidence: float
    analysis_version: str
    evidence: list = field(default_factory=list)  # list[EvidenceItem]
    counter_evidence: list = field(default_factory=list)
    hypothesis: Optional[str] = None
    recommended_next_investigation: Optional[str] = None
    potential_remediation: Optional[str] = None
    reasoning_summary: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class Investigation:
    investigation_id: str
    question: str
    priority: float
    trigger_finding: Optional[str]
    required_data: list
    expected_information_value: float
    depth: int
    status: InvestigationStatus = InvestigationStatus.QUEUED
    outcome: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d
