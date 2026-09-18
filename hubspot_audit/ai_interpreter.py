"""Low-cost AI interpretation layer.

Hard constraints enforced here (see MISSION spec):
- AI never receives full datasets, only compact candidate payloads capped at
  ``MAX_CANDIDATE_ITEMS`` summarized rows.
- AI cannot modify CRM data -- this module has no HubSpot client reference.
- AI cannot declare deletion as fact or promote a hypothesis to "confirmed" --
  the interpreter can only return ``inconclusive``/``needs_human_context``/
  the caller decides whether evidence is strong enough for anything else.
- No hidden chain-of-thought is stored -- only a short ``reasoning_summary``
  is requested and persisted.
- The whole package works with AI disabled: ``NullAIInterpreter`` is the
  default and answers every question with "not run, AI disabled".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .cost_tracker import CostTracker
from .models import FindingStatus

MAX_CANDIDATE_ITEMS = 25
ALLOWED_AI_STATUSES = {
    FindingStatus.INCONCLUSIVE,
    FindingStatus.NEEDS_HUMAN_CONTEXT,
    FindingStatus.INVESTIGATING,
    FindingStatus.REJECTED,
    FindingStatus.DETECTED,
}


@dataclass
class AIFindingResult:
    finding_id: str
    hypothesis: str
    evidence_summary: str
    counter_evidence: str
    confidence: float
    status: str
    recommended_next_investigation: Optional[str]
    potential_remediation: Optional[str]
    reasoning_summary: str


class AIInterpreter:
    def interpret_cluster(self, finding_id: str, question: str, candidates: list) -> Optional[AIFindingResult]:
        raise NotImplementedError

    def budget_summary(self) -> dict:
        raise NotImplementedError


class NullAIInterpreter(AIInterpreter):
    """Used whenever AI is disabled. Deterministic checks are unaffected."""

    def __init__(self, ai_config):
        self._tracker = CostTracker(ai_config)

    def interpret_cluster(self, finding_id: str, question: str, candidates: list) -> Optional[AIFindingResult]:
        return None

    def budget_summary(self) -> dict:
        return self._tracker.budget_summary()


_SYSTEM_PROMPT = (
    "You are a careful CRM data-quality analyst helping interpret ambiguous "
    "clusters of HubSpot records. You never claim data was deleted, you never "
    "promote a hypothesis to 'confirmed' status yourself, and you only use the "
    "evidence given to you. Respond with strict JSON matching the requested "
    "schema and nothing else. Keep reasoning_summary to at most 2 sentences -- "
    "do not include private step-by-step reasoning."
)


class AnthropicAIInterpreter(AIInterpreter):
    """Real Anthropic-backed interpreter. Only used when ``ai.enabled`` is True.

    The ``anthropic`` package is imported lazily so the rest of this package
    works without it installed when AI is disabled (the default).
    """

    def __init__(self, ai_config):
        self.ai_config = ai_config
        self._tracker = CostTracker(ai_config)
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic  # optional dependency, only needed with AI enabled

            self._client = anthropic.Anthropic()
        return self._client

    def budget_summary(self) -> dict:
        return self._tracker.budget_summary()

    def interpret_cluster(
        self,
        finding_id: str,
        question: str,
        candidates: list,
        escalate: bool = False,
    ) -> Optional[AIFindingResult]:
        can_call, reason = self._tracker.can_make_call(is_strong_model=escalate)
        if not can_call:
            return AIFindingResult(
                finding_id=finding_id,
                hypothesis="(AI interpretation skipped)",
                evidence_summary=reason,
                counter_evidence="",
                confidence=0.0,
                status=FindingStatus.NEEDS_HUMAN_CONTEXT.value,
                recommended_next_investigation=None,
                potential_remediation=None,
                reasoning_summary=f"AI call not made: {reason}.",
            )

        compact_candidates = candidates[:MAX_CANDIDATE_ITEMS]
        model = self.ai_config.strong_model if escalate else self.ai_config.cheap_model
        user_payload = {
            "finding_id": finding_id,
            "question": question,
            "candidates": compact_candidates,
            "required_json_fields": [
                "hypothesis",
                "evidence_summary",
                "counter_evidence",
                "confidence",
                "status",
                "recommended_next_investigation",
                "potential_remediation",
                "reasoning_summary",
            ],
            "allowed_status_values": [s.value for s in ALLOWED_AI_STATUSES],
        }

        client = self._get_client()
        response = client.messages.create(
            model=model,
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(user_payload)}],
        )
        input_tokens = getattr(response.usage, "input_tokens", 0)
        output_tokens = getattr(response.usage, "output_tokens", 0)
        self._tracker.record_call(model, input_tokens, output_tokens, escalate, purpose=question[:80])

        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return AIFindingResult(
                finding_id=finding_id,
                hypothesis="(AI response was not valid JSON)",
                evidence_summary=text[:300],
                counter_evidence="",
                confidence=0.0,
                status=FindingStatus.INCONCLUSIVE.value,
                recommended_next_investigation=None,
                potential_remediation=None,
                reasoning_summary="Model output failed JSON parsing; treated as inconclusive.",
            )

        status = parsed.get("status", FindingStatus.INCONCLUSIVE.value)
        if status == FindingStatus.CONFIRMED.value:
            # AI must never self-promote to confirmed -- downgrade defensively.
            status = FindingStatus.INVESTIGATING.value

        return AIFindingResult(
            finding_id=finding_id,
            hypothesis=parsed.get("hypothesis", ""),
            evidence_summary=parsed.get("evidence_summary", ""),
            counter_evidence=parsed.get("counter_evidence", ""),
            confidence=float(parsed.get("confidence", 0.0)),
            status=status,
            recommended_next_investigation=parsed.get("recommended_next_investigation"),
            potential_remediation=parsed.get("potential_remediation"),
            reasoning_summary=parsed.get("reasoning_summary", ""),
        )


def build_interpreter(ai_config) -> AIInterpreter:
    if not ai_config.enabled:
        return NullAIInterpreter(ai_config)
    return AnthropicAIInterpreter(ai_config)
