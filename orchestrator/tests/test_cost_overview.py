import json
from pathlib import Path
from core.cost_overview import collect_cost_overview


def test_cost_overview_separates_billed_and_equivalent(tmp_path):
    jobs = tmp_path / "jobs"
    job = jobs / "j1"
    job.mkdir(parents=True)
    (job / "calls.jsonl").write_text(json.dumps({
        "ts":"2026-09-16T10:00:00Z",
        "cost_usd_per_model":{"claude-sonnet-5":1.25},
    }) + "\n")
    gemini = tmp_path / "gemini.jsonl"
    gemini.write_text(json.dumps({
        "ts":"2026-09-16T11:00:00Z","service_tier":"flex","model":"gemini-3.8-flash",
        "input_tokens":1000000,"output_tokens":100000,"thinking_tokens":100000,
    }) + "\n")
    history = tmp_path / "usage.csv"
    history.write_text("timestamp_utc,companies,serper_calls,anthropic_calls,anthropic_input_tokens,anthropic_output_tokens,firecrawl_calls,estimated_total_usd,estimated_total_eur,models\n2026-09-16T12:00:00+00:00,1,1000,1,1000,100,100,2.0,1.84,claude-haiku\n")
    data = collect_cost_overview(jobs, gemini, [history])
    claude = next(r for r in data["daily"] if r["provider"] == "Claude Max")
    assert claude["api_equivalent_usd"] == 1.25
    assert claude["estimated_billed_usd"] == 0
    gem = next(r for r in data["daily"] if r["provider"] == "Gemini API")
    assert gem["estimated_billed_usd"] == 0.75
