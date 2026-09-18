"""AI usage/cost tracking.

Prices are never hardcoded here or anywhere else in the package -- they come
from ``AIConfig.pricing`` (itself sourced from environment variables). If a
model has no configured price, cost is reported as ``None`` ("unpriced")
rather than silently defaulting to zero or a guessed number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AICall:
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_eur: Optional[float]
    is_strong_model: bool
    purpose: str


class CostTracker:
    def __init__(self, ai_config):
        self.ai_config = ai_config
        self.calls: list = []
        self.strong_model_escalations = 0

    def _estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
        pricing = self.ai_config.pricing.get(model)
        if pricing is None or pricing.input_price_per_million is None or pricing.output_price_per_million is None:
            return None
        return (
            input_tokens / 1_000_000 * pricing.input_price_per_million
            + output_tokens / 1_000_000 * pricing.output_price_per_million
        )

    def can_make_call(self, is_strong_model: bool = False) -> tuple:
        if not self.ai_config.enabled:
            return False, "AI disabled by configuration"
        if self.ai_config.max_ai_calls and len(self.calls) >= self.ai_config.max_ai_calls:
            return False, "MAX_AI_CALLS reached"
        if is_strong_model and self.ai_config.max_strong_model_escalations and (
            self.strong_model_escalations >= self.ai_config.max_strong_model_escalations
        ):
            return False, "MAX_STRONG_MODEL_ESCALATIONS reached"
        spent = self.cumulative_cost_eur()
        if spent is not None and self.ai_config.budget_eur and spent >= self.ai_config.budget_eur:
            return False, "AI_BUDGET_EUR reached"
        return True, "ok"

    def record_call(self, model: str, input_tokens: int, output_tokens: int, is_strong_model: bool, purpose: str) -> AICall:
        cost = self._estimate_cost(model, input_tokens, output_tokens)
        call = AICall(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_eur=cost,
            is_strong_model=is_strong_model,
            purpose=purpose,
        )
        self.calls.append(call)
        if is_strong_model:
            self.strong_model_escalations += 1
        return call

    def cumulative_cost_eur(self) -> Optional[float]:
        costs = [c.estimated_cost_eur for c in self.calls]
        if any(c is None for c in costs):
            # Any unpriced call makes the cumulative total unknown rather than
            # understating it by silently skipping it.
            return None if costs else 0.0
        return sum(costs)

    def budget_summary(self) -> dict:
        spent = self.cumulative_cost_eur()
        budget = self.ai_config.budget_eur
        pct = None
        remaining = None
        if spent is not None and budget:
            pct = round(100 * spent / budget, 1) if budget else None
            remaining = round(budget - spent, 4)
        return {
            "provider": "anthropic",
            "cheap_model": self.ai_config.cheap_model,
            "strong_model": self.ai_config.strong_model,
            "ai_enabled": self.ai_config.enabled,
            "num_calls": len(self.calls),
            "num_strong_model_escalations": self.strong_model_escalations,
            "total_input_tokens": sum(c.input_tokens for c in self.calls),
            "total_output_tokens": sum(c.output_tokens for c in self.calls),
            "ai_cost_estimate_eur": spent,
            "ai_budget_eur": budget,
            "pct_budget_consumed": pct,
            "remaining_budget_eur": remaining,
            "cost_unpriced": spent is None and len(self.calls) > 0,
        }
