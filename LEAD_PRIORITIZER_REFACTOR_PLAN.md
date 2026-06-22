# Lead Prioritizer v2 — Refactor Plan

## Direction

Build a cleaner, country-aware Lead Prioritizer v2 as a separate application.
The legacy `enrich_clients_claude.py` remains untouched and continues to run independently.

## Design Decisions

### HQ Detection
- HQ detection must happen **before** scoring.
- The new simple HQ detector (`hq_simple_detector.py`) replaces the old HQ sanitizer for v2.
- The old HQ sanitizer is legacy only and must not be imported into v2.

### Competitor Signal
- Competitor signal must **not** be a v2 score driver.
- Competitor evidence may be retained as audit/evidence only (for human review), not fed into the scoring model.

### Rapid Growth
- Rapid growth must **not** be presented as a positive score driver.
- The current scoring model has a **negative coefficient** for rapid growth.
- Presenting it as positive in the UI would be misleading. Exclude it from positive signal display in v2.

## Staged Plan

| Stage | Scope |
|-------|-------|
| 1 | Dormant foundation — schema, skeleton files, Streamlit shell (this stage) |
| 2 | Strict simple HQ detector — domain-root query, country detection, confidence |
| 3 | Serper parser extraction — clean search result parsing, no legacy parser |
| 4 | Signal extraction module — foreign HQ, growth, keywords; competitor as audit only |
| 5 | Scoring v2 — country-aware model, no competitor driver, rapid growth handled correctly |
| 6 | Streamlit integration — file upload, results table, manual review flags |

## Files

| File | Role |
|------|------|
| `lead_output_schema.py` | Shared dataclasses / TypedDicts |
| `hq_simple_detector.py` | Simple HQ query builder (dormant) |
| `lead_prioritizer_core.py` | Orchestration skeleton (dormant) |
| `lead_prioritizer_app.py` | Streamlit shell (dormant) |

## Legacy Files (do not modify)

- `enrich_clients_claude.py`
- `commercial_fit_scoring.py`
- `hq_lookup_probe_app.py` (runtime behavior must not change)
