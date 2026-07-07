# Public Source Signal Enrichment

Optional, evidence-only Firecrawl block: retrieves public company-level evidence for a
user-configured signal (e.g. "vacancies") from a single user-configured public source, and adds
it to `evidence_items`. Off by default. Branch: `work`.

## Pipeline placement (as specified)

```
HQ detection → Non-HQ evidence collection → Public Source Signal Enrichment
→ Signal extraction → Summary / scoring / app fields
```

Implemented in `lead_prioritizer_core.py` between `collect_non_hq_enrichment_evidence(...)` and
`extract_non_hq_signals(...)`.

## Why it can never move scoring

`signal_name="public_source_signal"` is deliberately **not** one of the five scored non-HQ
signal names (`international_profile`, `onboarding_training_need`, `company_size_complexity`,
`icp_keyword_match`, `employer_branding`). `extract_non_hq_signals` and `extract_sector_industry`
both filter evidence by exact `signal_name` match, so this evidence is silently ignored by both —
it can only ever show up as audit/app metadata (Evidence sheet), never a score. Verified with a
dedicated score-invariance test (see below).

## New module

`lead_public_source_signal_enrichment.py` — `collect_public_source_signal_evidence(company_name,
domain, signal_query, source_base_url, firecrawl_api_key, source_label="", max_pages=3) ->
list[LeadEvidence]`. Reuses `deep_dive_runner._firecrawl_scrape_page` verbatim (same hard-failure
vs. 404 distinction as the existing HQ-Firecrawl/Deep-Dive modules). Never raises; returns `[]`
on missing inputs, a blocked source, a Firecrawl hard failure (401/402/403/429/network error), or
no useful match.

**First-prototype guardrail (explicitly scoped-in despite "no guardrails yet"):** a minimal,
named block list (`_BLOCKED_SOURCE_DOMAINS`) rejects social/professional-network platforms
(LinkedIn, Facebook, Instagram, Twitter/X, TikTok, Glassdoor, Indeed) plus the existing hosted-ATS
list (Workday, Greenhouse, ...) as a `source_base_url` — required by acceptance test #3. Nothing
else is guarded yet (no domain-match-to-company check, no quote verification) — deferred to a
later iteration as instructed.

Retrieval stays strictly within the configured public source's own host: the base URL, plus (when
`max_pages` allows) `?q=`/`?search=` query-string variants of the same URL — never a different
domain, never a live web search.

## Gewijzigde bestanden

| Bestand | Wijziging |
|---|---|
| `lead_public_source_signal_enrichment.py` | **nieuw** — de collector + block-list + candidate-URL/snippet helpers |
| `lead_prioritizer_core.py` | 5 nieuwe parameters op `prioritize_single_lead`; aanroep tussen Step 2 en Step 3; toegevoegd aan de `v2_pipeline_mode`-detectie |
| `lead_prioritizer_batch_core.py` | 5 nieuwe `BatchRunConfig`-velden; doorgegeven via `ai_kwargs` in `run_batch_dataframe` (en dus ook de parallelle runner, die per chunk dezelfde `config` gebruikt) |
| `lead_prioritizer_batch_app.py` | Batch-configsectie "Public Source Signal Enrichment" (checkbox + 4 inputs) bij de andere opt-ins, mét validatie (missing Firecrawl-key / lege base-URL blokkeert de run-knop); losstaand testpaneel-expander "Test Public Source Signal Enrichment" vóór de upload-gate (werkt zonder bestand) |
| `test_lead_public_source_signal_enrichment.py` | **nieuw** — 29 unit tests |
| `test_lead_v2_full_pipeline.py` | 8 nieuwe integratietests (plaatsing, scoring-invariantie) |
| `test_lead_prioritizer_batch_core.py` | 3 nieuwe tests (config-defaults + doorgifte naar `prioritize_single_lead`) |

## Testresultaat

- `test_lead_public_source_signal_enrichment.py`: **29 passed**, dekt de 5 verplichte scenario's
  (disabled/missing inputs, missing source URL, blocked social source, Firecrawl hard failure,
  succesvolle retrieval) plus candidate-URL/snippet-helpertests.
- `test_lead_v2_full_pipeline.py`: **47 passed** (8 nieuw) — bevestigt plaatsing (evidence wordt
  toegevoegd ná non-HQ-evidence), `v2_pipeline_mode="partial_v2"` als enige actieve flag, en
  **scoring-invariantie**: alle `sig_*`/`final_commercial_fit_score`/`commercial_tier`-velden en
  de `signals`-lijst blijven byte-identiek met/zonder de feature aan — alleen `evidence_items`
  groeit.
- `test_lead_prioritizer_batch_core.py`: **195 passed** (3 nieuw) — config-doorgifte bevestigd,
  onafhankelijk van andere opt-in-flags.
- Volledige regressiesuite (13 bestanden, incl. exporter/usage-tracker/HQ/deep-dive): **797
  passed**, 0 regressies (1 bestaande, ongerelateerde FutureWarning).

**Live end-to-end verificatie** (echte Firecrawl-call, FB Balzanelli
`https://www.fb-balzanelli.it/careers/`, signal "curriculum"): 1 `LeadEvidence` teruggekregen met
`signal_name="public_source_signal"`, `source_type="public_source"`,
`parser_source="firecrawl_public_source"`, een echte snippet met het gematchte fragment, en
`final_commercial_fit_score` bleef `None` (niet aangevraagd) — bevestigt dat de evidence puur
additief is. Bevestigd dat het via `flatten_evidence_for_excel()` correct in de Evidence-sheet-rij
verschijnt.

## UI

- **Batch-config** (na uploaden, bij de andere opt-ins, vóór "Autosave"): checkbox "Run Public
  Source Signal Enrichment" + tekstvelden voor signal/base-URL/label + aantal pagina's. Bij
  aanvinken zonder `FIRECRAWL_API_KEY` of zonder base-URL: duidelijke foutmelding, run-knop
  geblokkeerd. Uitgeschakeld (default): geen wijziging in bestaand gedrag.
- **Test-paneel** (expander bovenaan, werkt zonder geüpload bestand): dezelfde
  `collect_public_source_signal_evidence(...)`-functie als de batch-pipeline (geen duplicaat-
  implementatie). Toont input-samenvatting, retrieval-plan (host + kandidaat-URL's), een
  evidence-tabel, ruwe `LeadEvidence`-JSON, en een status (`success` / `no_evidence_found` /
  `error`). Schrijft niets naar batch-session-state, Excel-output, autosave of scoring.

Geen wijzigingen aan `current/` (bevestigd via `git status`); geen deploy naar een gedeelde
omgeving.
