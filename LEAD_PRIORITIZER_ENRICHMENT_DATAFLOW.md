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

## Step 3: deterministic non-HQ signal extraction

Implemented in `lead_non_hq_signal_extractor.py` and wired into
`prioritize_single_lead(..., extract_non_hq_signals_flag=True)`.

- **Uses collected evidence only.** Signals are extracted from
  `evidence_items`; this step never triggers a Serper call. If evidence was not
  collected, extraction runs over an empty list and yields empty signals — so
  network behavior stays explicit and controlled by the Step-2 flag.
- **Deterministic keyword rules only — no AI.** For each of the four supported
  signals (`international_profile`, `onboarding_training_need`,
  `company_size_complexity`, `icp_keyword_match`) the extractor counts distinct
  positive keyword hits in the evidence title + snippet:
  - `2.0` when ≥2 distinct keyword hits,
  - `1.0` when exactly 1,
  - `0.0` when evidence exists but no keyword matches.
  No signal is produced for a name with no evidence.
- **Confidence** is `High` (score 2.0 + a source URL), `Medium` (score 1.0 + a
  source URL), otherwise `Low`.
- **No competitor extraction** and **no rapid-growth positive signal.**
- **No final commercial scoring.** These are intermediate signal scores that
  populate the `sig_*` non-HQ fields and the `signals` list on
  `LeadPrioritizationResult`; they are NOT the final commercial fit score, and
  ranking is unchanged.
- **Evidence is copied verbatim** (URL / snippet / title) from the existing
  `LeadEvidence` — nothing is invented.

## Step 4: deterministic app/evidence summary fields

Implemented in `lead_app_summary_builder.py` and wired into
`prioritize_single_lead(..., build_app_summary_fields_flag=True)`.

- **Built only from existing signals/evidence.** The builder reads the already
  extracted `signals` and collected `evidence_items`; it never collects evidence
  or extracts signals implicitly, and network behavior stays controlled solely
  by `collect_non_hq_evidence`.
- **No AI, no final scoring, no ranking change.** It only fills
  `evidence_summary_app`, `key_source_links_app`, and `advanced_notes_app`.
- **No competitor display.** Only the four supported non-HQ signals contribute;
  any other (e.g. competitor-tagged) item is ignored. Rapid growth is never
  presented as a positive driver.
- **Traceable, nothing invented.** `evidence_summary_app` is one compact line
  per present signal (label, score, confidence, short reason).
  `key_source_links_app` deduplicates URLs (signal URLs first, then evidence
  URLs, capped at `max_links`, default 6) using only existing URLs/titles.
  `advanced_notes_app` is audit-only counts/flags (evidence count, signal count,
  signal names, low-confidence/zero-score signals, manual-review flags).

## Step 5: v2 scoring adapter

Implemented in `lead_v2_scoring_adapter.py` and wired into
`prioritize_single_lead(..., calculate_commercial_score_flag=True)`.

- **Explicit opt-in, single-lead flow only.** Scoring runs only when the flag is
  set; it never collects evidence, extracts signals, or builds summaries
  implicitly.
- **Uses `commercial_fit_scoring.score_company`** with the default profile
  `italy_register_icp_only`. Legacy scoring behavior is untouched.
- **Conservative signal mapping:**
  - `sig_foreign_hq_score` ← v2 foreign-HQ signal,
  - `sig_intl_footprint_score` ← `international_profile`,
  - `sig_lnd_onboarding_score` ← `onboarding_training_need`,
  - `sig_explicit_lnd_score` ← `icp_keyword_match`,
  - `sig_employer_branding_score` and `ti_onboarding_score` = 0.0 (not inferred
    yet).
- **Company size complexity is audit-only for now** — it is NOT used as an
  employee range, so the size fields are left blank.
- **Rapid growth is set to 0.0** and never presented as a positive driver.
- **Competitor is not mapped or scored.**
- Scoring runs even with only the HQ signal present (missing non-HQ signals map
  to 0.0).
- **This does not change batch ranking yet.**

## Step 6: deterministic caller/app fields

Implemented in `lead_caller_app_fields_builder.py` and wired into
`prioritize_single_lead(..., build_caller_app_fields_flag=True)`.

- **Built only from existing result fields** — HQ fields, non-HQ signal scores,
  evidence summaries, and the optional commercial-score fields already on the
  `LeadPrioritizationResult`.
- **No AI, no live search, no implicit scoring.** The flag never collects
  evidence, extracts signals, builds summaries, or scores; it only reads what is
  already present.
- **No competitor display** and **rapid growth is never presented as a positive
  driver.**
- Fills the app-facing payload for Lovable / Company Hub: `commercial_fit_score_app`,
  `commercial_tier_app`, `what_is_hot_app`, `what_is_not_app`, `why_relevant_app`,
  `caller_angle_app`, `call_starter_app`, `caution_app`,
  `foreign_hq_signal_used_in_app`, `foreign_hq_country_app`, `foreign_hq_city_app`.
- Text is short, practical, and traceable; `what_is_not_app` states factual gaps
  (e.g. "Commercial score not calculated") without framing un-run features as
  failures.
- **This prepares the payload but does not change batch ranking yet.**

## Full v2 single-lead pipeline preset

Wired into `prioritize_single_lead(..., run_full_v2_pipeline=True)`.

- **Explicit opt-in preset.** When enabled it turns on every optional v2 step
  (2–6): non-HQ evidence collection, signal extraction, app/evidence summaries,
  commercial scoring, and caller/app fields.
- **Runs the complete current v2 single-lead flow** end-to-end for one lead.
- **Does not add batch processing** and **does not change legacy ranking** or
  legacy score outputs.
- **Keeps the canonical HQ-first order**: (1) HQ detection → (2) non-HQ evidence
  → (3) signal extraction → (4) app/evidence summaries → (5) commercial scoring
  → (6) caller/app fields.
- The result records `v2_pipeline_mode`: `"hq_only"` (no optional steps),
  `"partial_v2"` (some optional steps), or `"full_v2_single_lead"` (preset on).
- Intended for **manual validation** and future frontend/API wiring. See
  `LEAD_PRIORITIZER_V2_SINGLE_LEAD_VALIDATION.md`.

## Batch CLI runner

`lead_prioritizer_batch_cli.py` is a thin command-line wrapper over the shared
batch core (`lead_prioritizer_batch_core.py`). It reads an Excel file, maps
columns, runs the selected mode, and writes an enriched workbook. It adds no
enrichment logic and does not duplicate batch logic.

Examples:

Full mode:
```bash
python lead_prioritizer_batch_cli.py --input Italy_500.xlsx --sheet "Opportunity Input Full" --company-column company_name --domain-column domain --default-country Italy --mode full --row-limit 10
```

HQ only:
```bash
python lead_prioritizer_batch_cli.py --input Italy_500.xlsx --sheet "Opportunity Input Full" --company-column company_name --domain-column domain --default-country Italy --mode hq_only --row-limit 50
```

Large run:
```bash
python lead_prioritizer_batch_cli.py --input Italy_500.xlsx --sheet "Opportunity Input Full" --company-column company_name --domain-column domain --default-country Italy --mode full --row-limit 500 --yes
```

Notes:
- Default `--row-limit` is **10**; `0` means all rows.
- `--yes` is required when more than **50** rows are selected (full mode makes
  multiple Serper + Anthropic calls per row).
- Keys come from the environment (`SERPER_API_KEY`, `ANTHROPIC_API_KEY`) first,
  then an optional `--secrets-file` TOML fallback. Key values are never printed
  or written to output.
- Modes: `full`, `hq_only`, `evidence_only`, `signals_no_score`, `full_no_score`.
- The output workbook has sheets: **Enriched Leads**, **Evidence**, **Signals**,
  **Run Summary**. Raw Serper payloads are never written; raw AI JSON only with
  `--include-raw-ai-json`.

## Streamlit batch Excel app

`lead_prioritizer_batch_app.py` is a local Streamlit app over the shared batch
core (`lead_prioritizer_batch_core.py`). Upload an Excel file, map columns, pick
a run mode, run the batch, and download the enriched workbook. It adds no
enrichment logic and does not duplicate batch logic; it does not import legacy
apps.

Run:
```bash
streamlit run lead_prioritizer_batch_app.py
```

Keys — local secrets in `.streamlit/secrets.toml`:
```toml
SERPER_API_KEY = "..."
ANTHROPIC_API_KEY = "..."
```
Falls back to the `SERPER_API_KEY` / `ANTHROPIC_API_KEY` environment variables.
Key values are never shown in the UI or written to output; the run button is
disabled until both keys are present.

- Modes: **Full v2 enrichment** (`full`), **HQ only** (`hq_only`), **Evidence
  only** (`evidence_only`), **Signals, no score** (`signals_no_score`),
  **Full, no score** (`full_no_score`). Default is Full v2 enrichment.
- Default row limit is **10**; `0` means all remaining rows.
- When more than **50** rows are selected, a warning shows and an explicit
  confirmation checkbox is required before the run button is enabled.
- Output workbook sheets: **Enriched Leads**, **Evidence**, **Signals**,
  **Run Summary**. Raw Serper payloads are never written; raw AI JSON only when
  "Include raw AI JSON" is checked.
- Intended for **synchronous local runs**. Large async Anthropic Message Batch
  processing will be designed separately later.

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
