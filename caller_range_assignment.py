"""caller_range_assignment.py — Range/percentile/cohort-based cold-caller assignment
========================================================================================
Alternative to the flat round-robin in ``reallocate_callers_from_gcs.py``
(``caller = cold_callers[(rank - 1) % len(cold_callers)]``): an administrator
instead assigns each cold caller an explicit *range* of the score-ranked
company list, in whichever unit is most convenient:

- ``"count"``      — explicit rank numbers, e.g. rank 1-250.
- ``"percentile"`` — percentage of the ranked list, e.g. the top 0-25%.
- ``"cohort"``      — fixed-size blocks, e.g. cohort size 100, cohort 1
  (companies 1-100), cohort 2 (companies 101-200), etc.

Ranks are the same 1-based, score-descending rank ``reallocate_callers_from_gcs``
already uses (``compute_caller_ranks`` — rank 1 is the single best-scoring
company). Because a range is defined in relative terms (a percentage, or a
cohort index against a chosen cohort size), the *same* range definitions can
be reused across every country folder even though each country has a
different total company count — "the top 25%" always means the top quarter
of whichever country's ranked list it's applied to, matching how a caller's
focus is meant to stay consistent per the business rule that all callers get
equivalent treatment within one country's ranking.

This module is pure logic — no I/O, no GCS, no Streamlit — so it can be
unit-tested standalone and reused from ``reallocate_callers_streamlit_app.py``
or any future caller of ``reallocate_callers_from_gcs.build_reallocated_run_from_assignment``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from reallocate_callers_from_gcs import compute_caller_ranks, normalize_cold_callers

RANGE_MODES = ("count", "percentile", "cohort")


@dataclass(frozen=True)
class CallerRange:
    """One cold caller's configured slice of the ranked company list.

    ``start``/``end`` are inclusive and interpreted per ``mode``:
      - ``"count"``: raw rank numbers (1-based), e.g. ``start=1, end=250``.
      - ``"percentile"``: 0-100 percentages of the total, e.g.
        ``start=0, end=25`` for the top quartile.
      - ``"cohort"``: 1-based cohort indices against ``cohort_size``, e.g.
        ``cohort_size=100, start=1, end=1`` for ranks 1-100.
    """

    caller: str
    mode: str
    start: float
    end: float
    cohort_size: Optional[int] = None

    def __post_init__(self) -> None:
        if self.mode not in RANGE_MODES:
            raise ValueError(
                f"Unknown range mode {self.mode!r} for caller {self.caller!r}; "
                f"must be one of {RANGE_MODES}.")
        if self.mode == "cohort" and (not self.cohort_size or self.cohort_size < 1):
            raise ValueError(
                f"Cohort mode requires a positive cohort_size (caller {self.caller!r}).")
        if self.end < self.start:
            raise ValueError(
                f"Range end ({self.end}) must be >= start ({self.start}) "
                f"for caller {self.caller!r}.")


def resolve_range_bounds(range_: CallerRange, total: int) -> tuple[int, int]:
    """Resolve a ``CallerRange`` to 1-based inclusive rank bounds
    ``(start_rank, end_rank)`` against ``total`` companies, clamped to
    ``[1, total]``. Never raises: a range entirely outside ``[1, total]``
    (e.g. cohort 5 of size 100 against only 300 companies) resolves to an
    empty range, signalled by ``start_rank > end_rank``.
    """
    if total <= 0:
        return (1, 0)
    if range_.mode == "count":
        start_rank = int(math.floor(range_.start))
        end_rank = int(math.ceil(range_.end))
    elif range_.mode == "percentile":
        start_rank = int(math.floor(range_.start / 100.0 * total)) + 1
        end_rank = int(math.ceil(range_.end / 100.0 * total))
    else:  # "cohort"
        size = range_.cohort_size or 1
        start_rank = (int(math.floor(range_.start)) - 1) * size + 1
        end_rank = int(math.ceil(range_.end)) * size
    start_rank = max(1, start_rank)
    end_rank = min(total, end_rank)
    return (start_rank, end_rank)


def caller_ranges_coverage(ranges: list[CallerRange], total: int) -> dict:
    """Diagnostic for the admin UI: which of the ``1..total`` ranks are
    covered by zero, one, or more than one configured range.

    Returns ``{"gaps": [rank, ...], "overlaps": {rank: [caller, ...]},
    "coverage": {rank: [caller, ...]}}``. Empty ``ranges`` or ``total == 0``
    yields an all-gaps (or empty) result rather than raising.
    """
    coverage: dict[int, list[str]] = {rank: [] for rank in range(1, total + 1)}
    for cr in ranges:
        start_rank, end_rank = resolve_range_bounds(cr, total)
        for rank in range(start_rank, end_rank + 1):
            coverage[rank].append(cr.caller)
    gaps = [rank for rank, callers in coverage.items() if not callers]
    overlaps = {rank: callers for rank, callers in coverage.items() if len(callers) > 1}
    return {"gaps": gaps, "overlaps": overlaps, "coverage": coverage}


def assign_callers_by_ranges(
    list_items: list[dict],
    ranges: list[CallerRange],
    *,
    rerank_by_score: bool = False,
    unassigned_label: str = "",
) -> dict:
    """``company_id -> (caller, rank)`` from explicit per-caller ranges
    instead of round-robin.

    A rank not covered by any range gets ``unassigned_label`` as its caller
    (still with a rank) rather than being dropped — a deliberate partial
    rollout (e.g. configuring one caller's range before the others) is a
    legitimate, non-error state; ``caller_ranges_coverage`` is how gaps get
    surfaced to the admin before upload, not a hard failure here.

    When a rank falls inside more than one range, the LAST matching entry in
    ``ranges`` wins, so an admin can order a narrow override range after a
    broad one.
    """
    ranks = compute_caller_ranks(list_items, rerank_by_score=rerank_by_score)
    total = len(ranks)
    resolved = [(cr, resolve_range_bounds(cr, total)) for cr in ranges]
    result: dict = {}
    for cid, rank in ranks.items():
        caller = unassigned_label
        for cr, (start_rank, end_rank) in resolved:
            if start_rank <= rank <= end_rank:
                caller = cr.caller
        result[cid] = (caller, rank)
    return result


def resolve_cohort_window(
    cohort_size: int, cohort_start: int, cohort_end: int, total: int,
) -> tuple[int, int]:
    """Resolve a cohort ``cohort_size`` + 1-based ``[cohort_start,
    cohort_end]`` index range to 1-based inclusive rank bounds against
    ``total`` — the same cohort math as ``CallerRange``'s ``"cohort"`` mode,
    exposed without a per-caller ``CallerRange`` for callers (like the
    round-robin cohort window below) that share one window across the whole
    pool rather than assigning it to a single caller."""
    window = CallerRange(
        caller="", mode="cohort", start=cohort_start, end=cohort_end,
        cohort_size=cohort_size)
    return resolve_range_bounds(window, total)


def assign_callers_round_robin_by_cohort_window(
    list_items: list[dict],
    cold_callers: list[str],
    *,
    cohort_size: int,
    cohort_start: int,
    cohort_end: int,
    rerank_by_score: bool = False,
    unassigned_label: str = "",
) -> dict:
    """Round-robin assignment restricted to a single GLOBAL cohort window of
    the score ranking, shared across the whole caller pool.

    Only companies whose rank falls inside the resolved cohort window (see
    ``resolve_cohort_window``) are released and round-robin split across
    ``cold_callers`` with the exact same formula ``assign_callers`` uses
    (``cold_callers[(rank - 1) % len(cold_callers)]``, keyed off the GLOBAL
    rank, not a position within the window) — so a company's caller never
    changes as the window is advanced to release later cohorts. Every
    company outside the window gets ``unassigned_label`` (still with its
    rank) rather than being dropped, matching ``assign_callers_by_ranges``'s
    gap handling.

    Raises ``ValueError`` if ``cold_callers`` is empty, same as
    ``assign_callers``.
    """
    callers = normalize_cold_callers(cold_callers)
    if not callers:
        raise ValueError("At least one cold caller is required.")
    ranks = compute_caller_ranks(list_items, rerank_by_score=rerank_by_score)
    total = len(ranks)
    start_rank, end_rank = resolve_cohort_window(
        cohort_size, cohort_start, cohort_end, total)
    result: dict = {}
    for cid, rank in ranks.items():
        if start_rank <= rank <= end_rank:
            result[cid] = (callers[(rank - 1) % len(callers)], rank)
        else:
            result[cid] = (unassigned_label, rank)
    return result


def even_count_ranges(callers: list[str], total: int) -> list[CallerRange]:
    """Default ``"count"`` ranges that split ``1..total`` into contiguous,
    (near-)equal blocks in caller order — e.g. 4 callers over 1000 companies
    each get exactly 250, remainders go to the earliest callers. Used to seed
    the admin UI with a sane starting point when switching from round-robin
    to range mode. Returns ``[]`` for an empty caller list or ``total <= 0``.
    """
    if not callers or total <= 0:
        return []
    base, remainder = divmod(total, len(callers))
    ranges = []
    start = 1
    for i, caller in enumerate(callers):
        size = base + (1 if i < remainder else 0)
        end = start + size - 1
        if size > 0:
            ranges.append(CallerRange(caller=caller, mode="count", start=start, end=end))
        start = end + 1
    return ranges
