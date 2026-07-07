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
SERPER_USD_PER_CALL: float = 0.001
FIRECRAWL_USD_PER_CALL: float = 0.001

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
    }


def format_summary_text(snap: Optional[dict] = None) -> str:
    """Compact, human-readable usage table for the CLI."""
    s = snap if snap is not None else snapshot()
    models = ", ".join(sorted(s["anthropic_by_model"])) or "-"
    kinds = s["serper_by_kind"]
    kind_str = ", ".join(f"{k}={kinds[k]}" for k in SERPER_KINDS if kinds.get(k)) or "-"
    a_usd = s["estimated_anthropic_usd"]
    a_usd_str = f"${a_usd:.4f}" if a_usd is not None else "n/a (unpriced model)"
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
