"""Aggregate Sales Cockpit API usage into billing-aware daily cost events."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

USD_TO_EUR = 0.92
SERPER_USD_PER_CALL = 0.00075
FIRECRAWL_EUR_PER_CALL = 0.00087
GEMINI_PRICING = {
    "flex": (0.375, 1.875),
    "standard": (0.75, 3.75),
}

DEFAULT_ENRICHMENT_HISTORIES = (
    Path("/home/myngle/worktrees/current-client-protection/logs/usage_history.csv"),
    Path("/home/myngle/worktrees/austria-zyte-run/logs/usage_history.csv"),
)

def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return None

def _jsonl(path: Path):
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                continue
    except OSError:
        return

def _event(date, provider, model=None, *, billed_usd=0.0, equivalent_usd=0.0,
           calls=0, input_tokens=0, output_tokens=0, source=""):
    return {
        "date": date,
        "provider": provider,
        "model": model,
        "estimated_billed_usd": round(float(billed_usd or 0.0), 6),
        "api_equivalent_usd": round(float(equivalent_usd or 0.0), 6),
        "calls": int(calls or 0),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "source": source,
    }

def _claude_events(jobs_dir: Path):
    rows = []
    for path in jobs_dir.glob("*/calls.jsonl"):
        for call in _jsonl(path):
            day = _date(call.get("ts"))
            if not day:
                continue
            costs = call.get("cost_usd_per_model") or {}
            if costs:
                for model, cost in costs.items():
                    rows.append(_event(day, "Claude Max", str(model), equivalent_usd=cost,
                                       calls=1, source="orchestrator_cli"))
            elif call.get("total_cost_usd"):
                rows.append(_event(day, "Claude Max", call.get("model_requested"),
                                   equivalent_usd=call.get("total_cost_usd"), calls=1,
                                   input_tokens=call.get("input_tokens"), output_tokens=call.get("output_tokens"),
                                   source="orchestrator_cli"))
    return rows

def _gemini_events(path: Path):
    rows = []
    for call in _jsonl(path):
        day = _date(call.get("ts"))
        if not day:
            continue
        tier = str(call.get("service_tier") or "standard").lower()
        in_rate, out_rate = GEMINI_PRICING.get(tier, GEMINI_PRICING["standard"])
        in_tok = int(call.get("input_tokens") or 0)
        out_tok = int(call.get("output_tokens") or 0) + int(call.get("thinking_tokens") or 0)
        cost = (in_tok * in_rate + out_tok * out_rate) / 1_000_000
        rows.append(_event(day, "Gemini API", call.get("model"), billed_usd=cost, calls=1,
                           input_tokens=in_tok, output_tokens=out_tok,
                           source=f"gemini_{tier}"))
    return rows

def _enrichment_events(histories):
    rows = []
    for path in histories:
        try:
            handle = path.open(newline="", encoding="utf-8-sig")
        except OSError:
            continue
        with handle:
            for item in csv.DictReader(handle):
                day = _date(item.get("timestamp_utc"))
                if not day:
                    continue
                serper_calls = int(item.get("serper_calls") or 0)
                firecrawl_calls = int(item.get("firecrawl_calls") or 0)
                total = float(item.get("estimated_total_usd") or 0.0)
                serper = serper_calls * SERPER_USD_PER_CALL
                firecrawl = firecrawl_calls * FIRECRAWL_EUR_PER_CALL / USD_TO_EUR
                anthropic = max(0.0, total - serper - firecrawl)
                rows.append(_event(day, "Serper", "Search API", billed_usd=serper,
                                   calls=serper_calls, source="enrichment_history"))
                rows.append(_event(day, "Firecrawl", "Scrape API", billed_usd=firecrawl,
                                   calls=firecrawl_calls, source="enrichment_history"))
                rows.append(_event(day, "Anthropic API", item.get("models") or "mixed",
                                   billed_usd=anthropic, calls=int(item.get("anthropic_calls") or 0),
                                   input_tokens=int(item.get("anthropic_input_tokens") or 0),
                                   output_tokens=int(item.get("anthropic_output_tokens") or 0),
                                   source="enrichment_history"))
    return rows

def collect_cost_overview(jobs_dir: Path, gemini_ledger: Path,
                          enrichment_histories=DEFAULT_ENRICHMENT_HISTORIES):
    events = _claude_events(Path(jobs_dir))
    events += _gemini_events(Path(gemini_ledger))
    events += _enrichment_events(tuple(Path(p) for p in enrichment_histories))
    grouped = defaultdict(lambda: {"estimated_billed_usd": 0.0, "api_equivalent_usd": 0.0,
                                   "calls": 0, "input_tokens": 0, "output_tokens": 0})
    for event in events:
        key = (event["date"], event["provider"], event.get("model"))
        bucket = grouped[key]
        for name in bucket:
            bucket[name] += event.get(name, 0) or 0
    daily = []
    for (day, provider, model), values in sorted(grouped.items()):
        daily.append({"date": day, "provider": provider, "model": model,
                      **{k: round(v, 6) if "usd" in k else int(v) for k, v in values.items()}})
    return {
        "currency": "USD",
        "usd_to_eur_reference": USD_TO_EUR,
        "daily": daily,
        "plans": [
            {"provider": "Serper", "plan": "Standard", "pricing": "$375 / 500,000 credits",
             "unit_cost_usd": SERPER_USD_PER_CALL, "billing_kind": "prepaid_credits"},
            {"provider": "Firecrawl", "plan": "Configured account rate", "pricing": "€87 / 100,000 calls",
             "unit_cost_eur": FIRECRAWL_EUR_PER_CALL, "billing_kind": "prepaid_or_subscription"},
            {"provider": "Claude Max", "plan": "5x", "pricing": "€90 / month",
             "billing_kind": "subscription", "usage_cost_mode": "api_equivalent_only"},
            {"provider": "Zyte", "plan": "Account tier", "pricing": "usage varies by site/request tier",
             "billing_kind": "commitment_or_payg", "usage_tracking": "pending_instrumentation"},
            {"provider": "Lusha", "plan": "Credit account", "pricing": "account credit plan",
             "billing_kind": "credit_subscription", "usage_tracking": "pending_instrumentation"},
            {"provider": "OpenAI API", "plan": "API account", "pricing": "model/token based",
             "billing_kind": "usage", "usage_tracking": "pending_instrumentation"},
        ],
        "notes": [
            "Claude Max usage is shown as API-equivalent value and is excluded from estimated billed API usage.",
            "Gemini costs are estimated from recorded tokens using the recorded Flex/Standard service tier.",
            "Enrichment API costs are estimated from the existing per-run usage history.",
        ],
    }
