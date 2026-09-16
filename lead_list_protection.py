"""Pure protection decisions for self-service Sales Cockpit lead-list intake.

No network or database I/O lives here. Callers supply already-resolved evidence
from HubSpot, Account Management and the live Cockpit dataset. The policy is
conservative: definite current customers are blocked, ambiguous customer
signals require review, and an existing Cockpit prospect is reused rather than
duplicated or re-enriched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Any

ACTIVE_CUSTOMER_TYPES = frozenset({
    "Customer-New-NB (Company)",
    "Customer-New-AM (Company)",
    "Customer-Success-BCG Japan AM (Company)",
    "Customer-Success-BCG Global AM (Company)",
    "Customer-Success-AM (Company)",
})
LOST_CUSTOMER_TYPE = "Customer-Lost-NB (Company)"


@dataclass(frozen=True)
class ProtectionDecision:
    status: str
    reason: str
    may_enrich: bool
    may_publish_as_new: bool
    existing_company_id: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _active_customer_record(records: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for record in records:
        if _text(record.get("customer_type")) in ACTIVE_CUSTOMER_TYPES:
            return record
    return None


def _lost_customer_record(records: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for record in records:
        if _text(record.get("customer_type")) == LOST_CUSTOMER_TYPE:
            return record
    return None


def _ambiguous_customer_record(records: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for record in records:
        customer_type = _text(record.get("customer_type"))
        if customer_type in ACTIVE_CUSTOMER_TYPES or customer_type == LOST_CUSTOMER_TYPE:
            continue
        if _text(record.get("type")).upper() == "CUSTOMER":
            return record
        if _text(record.get("lifecyclestage")).lower() == "customer":
            return record
    return None


def decide_protection(
    *,
    hubspot_records: Iterable[Mapping[str, Any]] = (),
    am_exact_match: bool = False,
    cockpit_exact_matches: Iterable[Mapping[str, Any]] = (),
    ambiguous_identity: bool = False,
) -> ProtectionDecision:
    """Return the next safe action for one resolved company identity.

    ``hs_current_customer`` is deliberately not a hard-block signal. Live portal
    data shows that property is also true on many prospect/lead records.
    """
    records = list(hubspot_records)
    cockpit = list(cockpit_exact_matches)

    if am_exact_match:
        return ProtectionDecision(
            "blocked_current_customer", "exact Account Management account match", False, False
        )

    active = _active_customer_record(records)
    if active is not None:
        return ProtectionDecision(
            "blocked_current_customer",
            f"HubSpot active customer type: {_text(active.get('customer_type'))}",
            False,
            False,
        )

    lost = _lost_customer_record(records)
    if lost is not None:
        return ProtectionDecision(
            "review_required", "HubSpot former/lost customer", False, False
        )

    if len(cockpit) == 1:
        company_id = _text(cockpit[0].get("company_id"))
        return ProtectionDecision(
            "reuse_existing_cockpit",
            "exact existing Sales Cockpit company match",
            False,
            False,
            company_id,
        )
    if len(cockpit) > 1 or ambiguous_identity:
        return ProtectionDecision(
            "review_required", "multiple or ambiguous company identity matches", False, False
        )

    ambiguous_customer = _ambiguous_customer_record(records)
    if ambiguous_customer is not None:
        return ProtectionDecision(
            "review_required",
            "HubSpot customer signal without an explicit active customer_type",
            False,
            False,
        )

    return ProtectionDecision("clear_new", "no protected exact match", True, True)
