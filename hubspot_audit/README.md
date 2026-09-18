# hubspot_audit

A self-contained, read-only HubSpot CRM audit framework for mYngle. It behaves
like an external CRM consultant: extract available data, measure facts,
detect anomalies deterministically, use low-cost AI selectively for
interpretation of ambiguous clusters, generate follow-up investigations,
preserve evidence, and produce a professional HTML report — plus a
plain-language `control_center.json` status feed for a non-technical reader.

## Safety contract

- **Read-only, always.** `hubspot_client.py` exposes only GET-shaped
  operations (`get_page`, `get_owners`, `get_properties`,
  `get_activity_page`, `capability_check`). There is no
  create/update/delete/merge/archive method anywhere in this package, and
  `LiveHubSpotReadClient` only ever calls `requests.get`.
- **Works fully with AI disabled.** `AIConfig.enabled` defaults to `False`.
  With AI disabled, `NullAIInterpreter` is used and every deterministic
  check still runs; nothing downstream depends on AI having run.
- **No hardcoded prices or secrets.** Every AI price comes from
  `AIConfig.pricing` (environment-configured); an unpriced model reports
  cost as `None` ("unpriced"), never a guessed number. The HubSpot token is
  read once from an environment variable named by
  `HubSpotConfig.token_env_var` and is never logged, printed, or written to
  any output artifact.
- **No hidden reasoning.** The AI interpreter only ever returns/stores a
  short `reasoning_summary`, never a raw chain-of-thought.
- **AI cannot self-confirm.** If a model tries to return `status: "confirmed"`,
  `AnthropicAIInterpreter` downgrades it to `investigating` before it ever
  reaches the evidence ledger — see `ai_interpreter.py`.
- **Bounded memory.** Every analysis streams normalized JSONL line-by-line
  (`extraction.read_jsonl` / `normalization.iter_normalized`) and only keeps
  the small set of fields each check needs in memory, not full records.

## Layers

| Layer | Module(s) |
|---|---|
| Data | `hubspot_client.py`, `extraction.py`, `normalization.py` |
| Detection | `analyses/*.py` |
| AI interpretation | `ai_interpreter.py`, `cost_tracker.py` |
| Audit controller | `runner.py`, `investigation_queue.py`, `evidence_ledger.py`, `control_center.py` |
| Reporting | `report/html_report.py` |

## Running (fixture / dry-run mode — no HubSpot token needed)

```bash
python3 -m hubspot_audit.cli run --mode fixture
```

This reads the bundled synthetic dataset in `hubspot_audit/fixtures/`
(regenerate it with `python3 -m hubspot_audit.fixtures.generate_fixtures`),
runs every phase, and writes a full run under
`./hubspot_audit_runs/<run_id>/`:

```
raw/            immutable extraction, resumable via raw/_checkpoint.json
normalized/     normalized JSONL per object
analyses/       consolidated deterministic metrics + signals
investigations/ investigation queue snapshot
evidence/       evidence + findings snapshot
reports/        index.html — the consultancy report
logs/           run.log
run_status.json, capability_matrix.json, gaps.json, findings.json,
evidence.json, investigations.json, next_actions.json, control_center.json
```

Poll `control_center.json` for the human-facing status contract (progress
percentage, current phase in plain language, confirmed findings, active
investigations, AI spend vs. budget, recent timeline/decisions).

## Running live (later execution prerequisite — not required to build/test this package)

1. Create a HubSpot private app token scoped to **read-only** CRM scopes
   (companies, contacts, deals, calls, meetings, emails, notes, tasks,
   owners, properties).
2. `export HUBSPOT_ACCESS_TOKEN=...` (or whatever `token_env_var` names).
3. `python3 -m hubspot_audit.cli run --mode live`

The CLI refuses to start a live run without that env var set — no live
token is ever required to build, test, or fixture-run this package.

## AI budget/cost configuration

All AI behavior is controlled by environment variables (see
`config.AIConfig.from_env`):

- `HSAUDIT_AI_ENABLED` (default `false`)
- `AI_BUDGET_EUR`, `MAX_AI_CALLS`, `MAX_STRONG_MODEL_ESCALATIONS`, `MAX_INVESTIGATION_DEPTH`
- `HSAUDIT_AI_STANDARD_MODEL` (default `claude-sonnet-5`)
- `HSAUDIT_AI_BULK_MODEL` (default `claude-haiku-4-5-20251001`, optional for simple repetitive classifications)
- `HSAUDIT_AI_STRONG_MODEL` (default `claude-sonnet-5`, reserved for explicit escalations)
- `HSAUDIT_PRICE_STANDARD_INPUT_PER_M` / `_OUTPUT_PER_M`,
  `HSAUDIT_PRICE_BULK_INPUT_PER_M` / `_OUTPUT_PER_M`,
  `HSAUDIT_PRICE_STRONG_INPUT_PER_M` / `_OUTPUT_PER_M` (EUR per 1M tokens;
  leave unset to report cost as "unpriced" instead of guessing)

## Tests

```bash
python3 -m unittest discover -s hubspot_audit/tests -p "test_*.py"
```

65 tests, stdlib `unittest` only (no `pytest` dependency), all running
against fixture data — no network access, no live token.

## Known gaps / later work

- This build implements the deterministic checks, the investigation queue,
  the evidence ledger, the cost-gated AI interpretation layer, the
  read-only live client interface, and the full report/Control Center
  contract, all verified against fixture data end-to-end.
- Deeper per-category nuance called for in the original brief (e.g. every
  named pipeline-duplicate-meaning heuristic, every named property-overlap
  heuristic) is implemented at a first, genuinely-working pass rather than
  exhaustively; `analyses/*.py` is the extension point for adding more
  specific heuristics without touching extraction, the ledger, the queue,
  or the report.
- HubSpot MCP supplementation for objects this package cannot read is a
  gap by design (recorded in `gaps.json`), not something invented or
  silently skipped.
