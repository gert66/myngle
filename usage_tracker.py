"""Process-global, thread-safe API usage tracker for one Lead Prioritizer run.

Counts the external API work a run actually does — Serper searches, Anthropic
calls + tokens, and Firecrawl scrapes — and estimates the cost from the shared
model pricing table. The pipeline already threads token usage through the HQ
interpreter; this module is the single place that AGGREGATES per run so the CLI
and the Streamlit app can show one clear summary instead of ad-hoc log reading.

Lifecycle (called by the entry points, never by the pure core):
    usage_tracker.reset()                 # start of a run/batch
    ... pipeline runs, low-level call sites record into the tracker ...
    snap = usage_tracker.snapshot()       # end of run
    print(usage_tracker.format_summary_text(snap))
    usage_tracker.append_history(companies=n, snapshot=snap)

Thread-safe (the batch runs rows via ThreadPoolExecutor, one process). NOT
shared across processes; a ProcessPool run would under-count. Every record_*
helper is defensive: instrumentation must never break an enrichment run.
"""
from __future__ import annotations

import csv
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Cost configuration — the ONE place to update when prices/rates change.
# ---------------------------------------------------------------------------
# Anthropic per-token pricing lives in ``lead_hq_ai_interpreter``
# (MODEL_PRICING_USD_PER_MTOK) and is reused here via estimate_ai_cost_usd, so
# token pricing has a single source of truth. The values below cover the parts
# that table does not: the USD->EUR display rate and per-call prices for the
# non-token APIs. All are PROVISIONAL — verify against the provider pricing
# pages and update here (nowhere else) when they change:
#   - Serper:    https://serper.dev/  (credit bundles; price/query plan-dependent)
#   - Firecrawl: https://firecrawl.dev/pricing
#   - FX rate:   any current USD->EUR reference
USD_TO_EUR: float = 0.92
SERPER_USD_PER_CALL: float = 0.00075
# Real account rate: EUR 87 / 100,000 calls = EUR 0.00087/call, converted to
# USD via USD_TO_EUR so it stays USD-denominated like SERPER_USD_PER_CALL.
FIRECRAWL_USD_PER_CALL: float = 0.00087 / USD_TO_EUR

# Default history log (append-only, one line per run). Kept tiny on purpose —
# no database. Overridable per call to append_history().
DEFAULT_HISTORY_PATH = os.path.join("logs", "usage_history.csv")

# Serper query kinds, for the optional breakdown.
SERPER_KINDS = ("hq", "non_hq", "icp_context", "sector", "other")

_lock = threading.Lock()


def _fresh_state() -> dict:
    return {
        "serper": defaultdict(int),      # kind -> count
        "anthropic_calls": 0,
        # model -> {"calls", "input_tokens", "output_tokens"}
        "anthropic_by_model": defaultdict(
            lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0}),
        "firecrawl_calls": 0,
        # Shared enrichment cache (enrichment_cache.py) hit/miss counts, keyed
        # by source ("serper" / "firecrawl"). Recorded at the exact same
        # low-level call sites as record_serper_call/record_firecrawl_call —
        # a hit means that call was skipped entirely.
        "cache_hits": defaultdict(int),
        "cache_misses": defaultdict(int),
    }


_state = _fresh_state()


def reset() -> None:
    """Clear all counters — call at the start of every run/batch."""
    global _state
    with _lock:
        _state = _fresh_state()


# ---------------------------------------------------------------------------
# Recording helpers (called from the low-level API call sites). Defensive:
# never raise, so instrumentation can never break a live enrichment run.
# ---------------------------------------------------------------------------

def record_serper_call(kind: str = "non_hq") -> None:
    try:
        k = kind if kind in SERPER_KINDS else "other"
        with _lock:
            _state["serper"][k] += 1
    except Exception:
        pass


def record_anthropic_call(model: Optional[str],
                          input_tokens: Optional[int],
                          output_tokens: Optional[int],
                          purpose: str = "") -> None:
    try:
        with _lock:
            _state["anthropic_calls"] += 1
            entry = _state["anthropic_by_model"][str(model or "unknown")]
            entry["calls"] += 1
            if input_tokens:
                entry["input_tokens"] += int(input_tokens)
            if output_tokens:
                entry["output_tokens"] += int(output_tokens)
    except Exception:
        pass


def _usage_value(usage_obj, *names):
    for name in names:
        value = None
        if isinstance(usage_obj, dict):
            value = usage_obj.get(name)
        else:
            value = getattr(usage_obj, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def record_anthropic_response(response, model: Optional[str], purpose: str = "") -> None:
    """Extract token usage from an Anthropic SDK response and record it. One
    call at each ``messages.create`` site captures what would otherwise be lost."""
    try:
        usage = getattr(response, "usage", None)
        record_anthropic_call(
            model,
            _usage_value(usage, "input_tokens", "prompt_tokens"),
            _usage_value(usage, "output_tokens", "completion_tokens"),
            purpose,
        )
    except Exception:
        pass


def record_firecrawl_call() -> None:
    try:
        with _lock:
            _state["firecrawl_calls"] += 1
    except Exception:
        pass


def record_cache_hit(source: str) -> None:
    """A cached response was used instead of a live call — call this at the
    same low-level call site that would otherwise have called
    record_serper_call/record_firecrawl_call for this request."""
    try:
        with _lock:
            _state["cache_hits"][str(source or "unknown")] += 1
    except Exception:
        pass


def record_cache_miss(source: str) -> None:
    """No usable cache entry was found (absent, expired, or force-refreshed)
    — the caller proceeds to a live call right after this."""
    try:
        with _lock:
            _state["cache_misses"][str(source or "unknown")] += 1
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _estimate_anthropic_usd(by_model: dict) -> Optional[float]:
    """Sum estimated Anthropic USD across models, or None when nothing priced."""
    try:
        from lead_hq_ai_interpreter import estimate_ai_cost_usd
    except Exception:
        return None
    total = 0.0
    priced_any = False
    for model, e in by_model.items():
        cost = estimate_ai_cost_usd(model, e["input_tokens"], e["output_tokens"])
        if cost is not None:
            total += cost
            priced_any = True
    return round(total, 6) if priced_any else None


def snapshot() -> dict:
    """Immutable summary of the current run's usage + cost estimate."""
    with _lock:
        serper = dict(_state["serper"])
        by_model = {m: dict(e) for m, e in _state["anthropic_by_model"].items()}
        anthropic_calls = _state["anthropic_calls"]
        firecrawl_calls = _state["firecrawl_calls"]
        cache_hits = dict(_state["cache_hits"])
        cache_misses = dict(_state["cache_misses"])

    serper_total = sum(serper.values())
    in_tok = sum(e["input_tokens"] for e in by_model.values())
    out_tok = sum(e["output_tokens"] for e in by_model.values())

    anthropic_usd = _estimate_anthropic_usd(by_model)
    serper_usd = round(serper_total * SERPER_USD_PER_CALL, 6)
    firecrawl_usd = round(firecrawl_calls * FIRECRAWL_USD_PER_CALL, 6)
    total_usd = (anthropic_usd or 0.0) + serper_usd + firecrawl_usd
    total_eur = round(total_usd * USD_TO_EUR, 4)

    return {
        "serper_total": serper_total,
        "serper_by_kind": serper,
        "anthropic_calls": anthropic_calls,
        "anthropic_input_tokens": in_tok,
        "anthropic_output_tokens": out_tok,
        "anthropic_total_tokens": in_tok + out_tok,
        "anthropic_avg_input_tokens": round(in_tok / anthropic_calls, 1) if anthropic_calls else 0,
        "anthropic_avg_output_tokens": round(out_tok / anthropic_calls, 1) if anthropic_calls else 0,
        "anthropic_by_model": by_model,
        "firecrawl_calls": firecrawl_calls,
        "estimated_anthropic_usd": anthropic_usd,
        "estimated_serper_usd": serper_usd,
        "estimated_firecrawl_usd": firecrawl_usd,
        "estimated_total_usd": round(total_usd, 6),
        "estimated_total_eur": total_eur,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
    }


def merge_snapshots(snapshots: list) -> dict:
    """Combine multiple ``snapshot()``-shaped dicts (e.g. one per Cloud Run
    task, each running in its own subprocess/container with its own
    in-memory tracker) into one aggregate of the same shape, so
    ``format_summary_text`` can render a single run-wide report exactly like
    it does for one process. Non-dict/malformed entries are skipped rather
    than raising — a single corrupt task's usage file must never break the
    report for every other task. Recomputes averages/costs from the summed
    raw counters rather than averaging the per-task estimates, so this is
    exact, not an approximation.
    """
    serper: dict = defaultdict(int)
    by_model: dict = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})
    anthropic_calls = 0
    firecrawl_calls = 0
    cache_hits: dict = defaultdict(int)
    cache_misses: dict = defaultdict(int)

    for snap in snapshots or []:
        if not isinstance(snap, dict):
            continue
        for kind, count in (snap.get("serper_by_kind") or {}).items():
            serper[kind] += int(count or 0)
        for model, entry in (snap.get("anthropic_by_model") or {}).items():
            if not isinstance(entry, dict):
                continue
            dest = by_model[model]
            dest["calls"] += int(entry.get("calls") or 0)
            dest["input_tokens"] += int(entry.get("input_tokens") or 0)
            dest["output_tokens"] += int(entry.get("output_tokens") or 0)
        anthropic_calls += int(snap.get("anthropic_calls") or 0)
        firecrawl_calls += int(snap.get("firecrawl_calls") or 0)
        for source, count in (snap.get("cache_hits") or {}).items():
            cache_hits[source] += int(count or 0)
        for source, count in (snap.get("cache_misses") or {}).items():
            cache_misses[source] += int(count or 0)

    serper = dict(serper)
    by_model = {m: dict(e) for m, e in by_model.items()}
    serper_total = sum(serper.values())
    in_tok = sum(e["input_tokens"] for e in by_model.values())
    out_tok = sum(e["output_tokens"] for e in by_model.values())

    anthropic_usd = _estimate_anthropic_usd(by_model)
    serper_usd = round(serper_total * SERPER_USD_PER_CALL, 6)
    firecrawl_usd = round(firecrawl_calls * FIRECRAWL_USD_PER_CALL, 6)
    total_usd = (anthropic_usd or 0.0) + serper_usd + firecrawl_usd
    total_eur = round(total_usd * USD_TO_EUR, 4)

    return {
        "serper_total": serper_total,
        "serper_by_kind": serper,
        "anthropic_calls": anthropic_calls,
        "anthropic_input_tokens": in_tok,
        "anthropic_output_tokens": out_tok,
        "anthropic_total_tokens": in_tok + out_tok,
        "anthropic_avg_input_tokens": round(in_tok / anthropic_calls, 1) if anthropic_calls else 0,
        "anthropic_avg_output_tokens": round(out_tok / anthropic_calls, 1) if anthropic_calls else 0,
        "anthropic_by_model": by_model,
        "firecrawl_calls": firecrawl_calls,
        "estimated_anthropic_usd": anthropic_usd,
        "estimated_serper_usd": serper_usd,
        "estimated_firecrawl_usd": firecrawl_usd,
        "estimated_total_usd": round(total_usd, 6),
        "estimated_total_eur": total_eur,
        "cache_hits": dict(cache_hits),
        "cache_misses": dict(cache_misses),
    }


def format_summary_text(snap: Optional[dict] = None) -> str:
    """Compact, human-readable usage table for the CLI."""
    s = snap if snap is not None else snapshot()
    models = ", ".join(sorted(s["anthropic_by_model"])) or "-"
    kinds = s["serper_by_kind"]
    kind_str = ", ".join(f"{k}={kinds[k]}" for k in SERPER_KINDS if kinds.get(k)) or "-"
    a_usd = s["estimated_anthropic_usd"]
    a_usd_str = f"${a_usd:.4f}" if a_usd is not None else "n/a (unpriced model)"
    cache_hits = s.get("cache_hits") or {}
    cache_misses = s.get("cache_misses") or {}
    cache_sources = sorted(set(cache_hits) | set(cache_misses))
    # ASCII-only: this is printed to the CLI, which may run on a Windows cp1252
    # console that cannot encode box-drawing chars, the euro sign, or "~=".
    bar = "-" * 52
    lines = [
        bar,
        "  API usage - this run",
        bar,
        f"  Serper searches   : {s['serper_total']}   ({kind_str})",
        f"  Anthropic calls   : {s['anthropic_calls']}   [model(s): {models}]",
        f"    input tokens    : {s['anthropic_input_tokens']:,}  (avg {s['anthropic_avg_input_tokens']}/call)",
        f"    output tokens   : {s['anthropic_output_tokens']:,}  (avg {s['anthropic_avg_output_tokens']}/call)",
        f"  Firecrawl scrapes : {s['firecrawl_calls']}",
    ]
    if cache_sources:
        lines.append("  -- shared enrichment cache --")
        for source in cache_sources:
            hits = cache_hits.get(source, 0)
            misses = cache_misses.get(source, 0)
            total = hits + misses
            rate = f"{100 * hits / total:.0f}%" if total else "n/a"
            lines.append(
                f"    {source:<9} : {hits} hit / {misses} miss  (hit rate {rate})")
    lines += [
        "  -- estimated cost --",
        f"    Anthropic       : {a_usd_str}",
        f"    Serper          : ${s['estimated_serper_usd']:.4f}",
        f"    Firecrawl       : ${s['estimated_firecrawl_usd']:.4f}",
        f"    TOTAL           : ${s['estimated_total_usd']:.4f}  =~  EUR {s['estimated_total_eur']:.4f}",
        bar,
    ]
    return "\n".join(lines)


_HISTORY_HEADER = [
    "timestamp_utc", "companies", "serper_calls", "anthropic_calls",
    "anthropic_input_tokens", "anthropic_output_tokens", "firecrawl_calls",
    "estimated_total_usd", "estimated_total_eur", "models",
]


def append_history(companies: Optional[int] = None,
                   snapshot: Optional[dict] = None,
                   path: Optional[str] = None) -> str:
    """Append one summary row to the append-only history CSV (created with a
    header on first write). Returns the path written. Best-effort: never raises."""
    s = snapshot if snapshot is not None else globals()["snapshot"]()
    log_path = Path(path or DEFAULT_HISTORY_PATH)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not log_path.exists()
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(_HISTORY_HEADER)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                companies if companies is not None else "",
                s["serper_total"], s["anthropic_calls"],
                s["anthropic_input_tokens"], s["anthropic_output_tokens"],
                s["firecrawl_calls"], s["estimated_total_usd"],
                s["estimated_total_eur"],
                "|".join(sorted(s["anthropic_by_model"])),
            ])
    except Exception:
        pass
    return str(log_path)
