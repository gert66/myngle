"""Investigation queue: priority, de-duplication, depth cap, low-value stop.

Fields per spec: priority, question, trigger_finding, required_data,
expected_information_value, depth, status, outcome. Defaults: max depth 4,
de-duplicate repeated questions, stop when evidence is sufficient, stop when
information value is low, mark needs_human_context when business context is
essential.
"""
from __future__ import annotations

import heapq
import itertools
import json
import re
from typing import Optional

from .models import Investigation, InvestigationStatus


def _normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower())


class InvestigationQueue:
    def __init__(self, max_depth: int = 4, low_value_threshold: float = 0.15, max_size: int = 200):
        self.max_depth = max_depth
        self.low_value_threshold = low_value_threshold
        self.max_size = max_size
        self._heap: list = []
        self._counter = itertools.count()
        self._seen_questions: set = set()
        self._by_id: dict = {}
        self._next_id = itertools.count(1)

    def push(
        self,
        question: str,
        trigger_finding: Optional[str],
        required_data: list,
        expected_information_value: float,
        depth: int = 0,
        needs_human_context: bool = False,
    ) -> Optional[Investigation]:
        key = _normalize_question(question)
        if key in self._seen_questions:
            return None
        if depth > self.max_depth:
            return None
        if len(self._by_id) >= self.max_size:
            return None

        status = InvestigationStatus.NEEDS_HUMAN_CONTEXT if needs_human_context else InvestigationStatus.QUEUED
        if not needs_human_context and expected_information_value < self.low_value_threshold:
            status = InvestigationStatus.DEFERRED_LOW_VALUE

        investigation_id = f"inv-{next(self._next_id):04d}"
        investigation = Investigation(
            investigation_id=investigation_id,
            question=question,
            priority=expected_information_value,
            trigger_finding=trigger_finding,
            required_data=required_data,
            expected_information_value=expected_information_value,
            depth=depth,
            status=status,
        )
        self._seen_questions.add(key)
        self._by_id[investigation_id] = investigation
        if status == InvestigationStatus.QUEUED:
            heapq.heappush(self._heap, (-expected_information_value, next(self._counter), investigation_id))
        return investigation

    def pop_next(self) -> Optional[Investigation]:
        while self._heap:
            _, _, investigation_id = heapq.heappop(self._heap)
            investigation = self._by_id[investigation_id]
            if investigation.status == InvestigationStatus.QUEUED:
                investigation.status = InvestigationStatus.IN_PROGRESS
                return investigation
        return None

    def resolve(self, investigation_id: str, status: InvestigationStatus, outcome: str) -> None:
        from .models import now_iso

        investigation = self._by_id[investigation_id]
        investigation.status = status
        investigation.outcome = outcome
        investigation.updated_at = now_iso()

    def all(self) -> list:
        return list(self._by_id.values())

    def counts_by_status(self) -> dict:
        counts: dict = {}
        for inv in self._by_id.values():
            counts[inv.status.value] = counts.get(inv.status.value, 0) + 1
        return counts

    def to_list(self) -> list:
        return [inv.to_dict() for inv in self._by_id.values()]

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_list(), fh, indent=2, default=str)
