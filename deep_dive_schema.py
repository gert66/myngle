"""Schema for the Lead Prioritizer v2 Deep Dive (Step B, opt-in).

The Deep Dive layer collects deeper, presentable evidence for a lead —
with an exact quote and source URL per claim — for high-scoring or
confirmed-foreign-HQ companies. See ``deep_dive_runner.py`` for the
collection/distillation logic.

Deliberately a pure, standalone dataclass module (no Streamlit, no batch,
no network) so it can be shared unchanged between the batch pipeline and a
future FastAPI on-demand endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The five fixed claim categories. A claim with any other category is
# dropped by the runner rather than mislabeled.
DEEP_DIVE_CATEGORIES: tuple[str, ...] = (
    "hq_structure",
    "locations",
    "training_infrastructure",
    "workforce",
    "recent_developments",
)

DEEP_DIVE_SOURCE_KINDS: tuple[str, ...] = ("own_domain", "parent_domain", "external")

DEEP_DIVE_RETRIEVAL_METHODS: tuple[str, ...] = (
    "firecrawl", "serper_localized", "plain_fetch",
)

# Documented trigger_reason values (informational — not enforced).
DEEP_DIVE_TRIGGER_REASONS: tuple[str, ...] = ("score_threshold", "foreign_hq", "manual")


@dataclass
class DeepDiveClaim:
    """One discrete, source-backed claim distilled from deep-dive material."""
    claim_id: str
    category: str
    statement: str
    quote: str
    source_url: str
    source_title: Optional[str] = None
    source_kind: str = "external"
    domain_verified: bool = False
    retrieval_method: str = "serper_localized"

    def to_json_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "category": self.category,
            "statement": self.statement,
            "quote": self.quote,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "source_kind": self.source_kind,
            "domain_verified": self.domain_verified,
            "retrieval_method": self.retrieval_method,
        }


@dataclass
class DeepDiveResult:
    """Result of one deep-dive attempt for a single company.

    ``error`` is a per-company failure (never batch-fatal): the batch layer
    must always be able to continue processing other rows even when a deep
    dive fails outright.
    """
    company_name: str
    domain: Optional[str] = None
    parent_domain: Optional[str] = None
    trigger_reason: str = ""
    claims: list = field(default_factory=list)          # list[DeepDiveClaim]
    pages_crawled: list = field(default_factory=list)    # list[dict] audit trail
    firecrawl_used: bool = False
    localized_queries_used: list = field(default_factory=list)
    error: str = ""
    generated_at: str = ""

    def to_json_dict(self) -> dict:
        return {
            "company_name": self.company_name,
            "domain": self.domain,
            "parent_domain": self.parent_domain,
            "trigger_reason": self.trigger_reason,
            "claims": [
                c.to_json_dict() if isinstance(c, DeepDiveClaim) else dict(c)
                for c in (self.claims or [])
            ],
            "pages_crawled": list(self.pages_crawled or []),
            "firecrawl_used": self.firecrawl_used,
            "localized_queries_used": list(self.localized_queries_used or []),
            "error": self.error,
            "generated_at": self.generated_at,
        }
