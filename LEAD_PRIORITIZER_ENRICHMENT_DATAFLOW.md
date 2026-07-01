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
