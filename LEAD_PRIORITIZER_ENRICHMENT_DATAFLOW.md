# Lead Prioritizer v2 — Enrichment Dataflow

This document describes the intended v2 dataflow and the guarantees it must
keep. It is the reference for how enrichment, signals, evidence and scoring fit
together.

## v2 flow

```
clean input
  → HQ enrichment            (one Serper call + AI-first HQ interpretation)
  → non-HQ enrichment        (international profile, onboarding/training need,
                              company size complexity, ICP keyword match)
  → signal extraction        (each signal carries its own evidence)
  → scoring                  (country-aware; HQ decided before scoring)
  → app fields               (evidence_summary_app, key_source_links_app, …)
```

- **HQ detection is the canonical basis and happens before scoring.** No score
  is produced until the HQ structure (domestic / foreign_parent /
  regional_branch_only / unclear) is decided.
- **Non-HQ enrichment is layered after HQ**, never the other way around.

## Evidence must flow downstream with every signal

Every `LeadSignal` carries its backing evidence (`evidence_url`,
`evidence_quote`, `evidence_title`, `query_used`, `parser_source`). Standalone
`LeadEvidence` items are collected in `evidence_items` so any score can be
traced back to the exact search result / parser output it came from. A signal
without traceable evidence is not a scorable signal.

## Hard rules

- **Competitor signal is excluded from scoring.** Competitor evidence, if ever
  kept, is audit-only and must never be a score driver in v2. For this step no
  competitor fields are added at all.
- **Rapid growth must not be presented as a positive score driver.** It may be
  recorded as context/caution, but never surfaced as a positive reason to
  prioritize a lead.
- **Production logic must not import from `hq_lookup_probe_app.py`** and must not
  import or modify the old HQ sanitizer logic.
- **`enrich_clients_claude.py` is not changed** by this work.

## Step 2: non-HQ evidence collection

Implemented in `lead_non_hq_enrichment.py` and wired into
`prioritize_single_lead(..., collect_non_hq_evidence=True)`.

- **Evidence only — no non-HQ scores yet.** All `sig_*` non-HQ score fields and
  their reasons stay `None`; `signals` stays empty. Only `evidence_items` is
  populated.
- **Runs strictly after HQ detection.** HQ is decided first; this step never
  affects the HQ flow, and the flag defaults to `False` so existing behavior is
  unchanged.
- **At most 4 Serper queries per lead** — one per non-HQ signal
  (`international_profile`, `onboarding_training_need`, `company_size_complexity`,
  `icp_keyword_match`), built domain-root-first.
- **No competitor collection.** No competitor, alternative-provider,
  vendor-comparison or rapid-growth queries or fields are collected.
- **Deterministic extraction.** Evidence comes verbatim from Serper
  knowledgeGraph / answerBox / top organic results — no AI interpretation and no
  invented quotes at this step.
- Collected evidence is attached to `LeadPrioritizationResult.evidence_items` as
  `LeadEvidence` objects.

## Scope of the current step

This step adds **schema and safe placeholders only**:

- New dataclasses `LeadEvidence`, `LeadSignal`, and the grouped
  `LeadEnrichmentResult` in `lead_output_schema.py`.
- New optional non-HQ signal fields on `LeadPrioritizationResult`
  (`sig_international_profile_score`, `sig_onboarding_training_need_score`,
  `sig_company_size_complexity_score`, `sig_icp_keyword_match_score`, plus their
  reason / evidence-url / evidence-quote fields), the app-text placeholders
  (`evidence_summary_app`, `key_source_links_app`, `advanced_notes_app`), and the
  structured `evidence_items` / `signals` lists.
- `lead_prioritizer_core.py` returns these new fields with safe empty defaults
  (scores `None`, reasons/evidence `None`, lists empty).

**No live non-HQ enrichment is implemented yet, and scoring is unchanged.**
The placeholders exist so the downstream contract is stable before enrichment
and scoring are wired in.
