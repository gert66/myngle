"""Configuration for a hubspot_audit run.

Every budget/price knob is read from environment variables or explicit
overrides -- nothing here hardcodes an Anthropic or HubSpot price. Prices
default to ``None`` (unknown) so cost reporting can say "unpriced" instead of
silently pretending a number is accurate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ModelPricing:
    """Price per 1,000,000 tokens, in EUR. ``None`` means "not configured"."""

    input_price_per_million: Optional[float] = None
    output_price_per_million: Optional[float] = None


@dataclass
class AIConfig:
    enabled: bool = False
    budget_eur: float = 0.0
    max_ai_calls: int = 0
    max_strong_model_escalations: int = 0
    max_investigation_depth: int = 4
    cheap_model: str = "claude-haiku-4-5-20251001"
    strong_model: str = "claude-sonnet-5"
    pricing: dict = field(
        default_factory=lambda: {
            "claude-haiku-4-5-20251001": ModelPricing(),
            "claude-sonnet-5": ModelPricing(),
        }
    )

    @classmethod
    def from_env(cls) -> "AIConfig":
        cheap = os.environ.get("HSAUDIT_AI_CHEAP_MODEL", "claude-haiku-4-5-20251001")
        strong = os.environ.get("HSAUDIT_AI_STRONG_MODEL", "claude-sonnet-5")
        pricing = {
            cheap: ModelPricing(
                input_price_per_million=_env_float("HSAUDIT_PRICE_CHEAP_INPUT_PER_M", None),
                output_price_per_million=_env_float("HSAUDIT_PRICE_CHEAP_OUTPUT_PER_M", None),
            ),
            strong: ModelPricing(
                input_price_per_million=_env_float("HSAUDIT_PRICE_STRONG_INPUT_PER_M", None),
                output_price_per_million=_env_float("HSAUDIT_PRICE_STRONG_OUTPUT_PER_M", None),
            ),
        }
        return cls(
            enabled=_env_bool("HSAUDIT_AI_ENABLED", False),
            budget_eur=_env_float("AI_BUDGET_EUR", 0.0) or 0.0,
            max_ai_calls=_env_int("MAX_AI_CALLS", 0),
            max_strong_model_escalations=_env_int("MAX_STRONG_MODEL_ESCALATIONS", 0),
            max_investigation_depth=_env_int("MAX_INVESTIGATION_DEPTH", 4),
            cheap_model=cheap,
            strong_model=strong,
            pricing=pricing,
        )


@dataclass
class HubSpotConfig:
    """Read-only HubSpot access configuration.

    ``token_env_var`` names the environment variable holding the private-app
    access token. The token value itself is never stored on this object and
    never logged -- only the *name* of the variable is kept here.
    """

    base_url: str = "https://api.hubapi.com"
    token_env_var: str = "HUBSPOT_ACCESS_TOKEN"
    page_size: int = 100
    timeout_seconds: int = 30
    max_retries: int = 3

    def has_live_token(self) -> bool:
        return bool(os.environ.get(self.token_env_var))


@dataclass
class Thresholds:
    stale_company_days: int = 365
    stale_contact_days: int = 365
    stale_open_deal_days: int = 90
    near_unused_fill_rate: float = 0.02
    unused_fill_rate: float = 0.0
    creation_burst_min_records: int = 25
    creation_burst_window_days: int = 1


@dataclass
class AuditConfig:
    run_id: str
    output_dir: str
    mode: str = "fixture"  # "fixture" | "dry_run" | "live"
    hubspot: HubSpotConfig = field(default_factory=HubSpotConfig)
    ai: AIConfig = field(default_factory=AIConfig.from_env)
    thresholds: Thresholds = field(default_factory=Thresholds)
    fixtures_dir: Optional[str] = None
    max_investigation_queue_size: int = 200
    low_information_value_threshold: float = 0.15

    @classmethod
    def build(
        cls,
        run_id: str,
        output_dir: str,
        mode: str = "fixture",
        fixtures_dir: Optional[str] = None,
    ) -> "AuditConfig":
        return cls(
            run_id=run_id,
            output_dir=output_dir,
            mode=mode,
            fixtures_dir=fixtures_dir,
            ai=AIConfig.from_env(),
        )
