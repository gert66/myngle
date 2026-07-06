# Verbruiksoverzicht per run (tokens / searches / kosten)

Na elke Lead Prioritizer-run tonen CLI én Streamlit-app nu het daadwerkelijke API-verbruik van
die run: aantal Serper-searches (uitgesplitst), Anthropic-calls + tokens + model, Firecrawl-
scrapes, en een geschatte kostprijs in USD/EUR. Branch: `work`.

## Stap 0 — wat er al was

1. **Per-call telling?** Nee, er was geen run-teller. Wel: `lead_hq_ai_interpreter.py` ving al
   token-usage op voor de HQ-call (`_call_anthropic_hq`, regel ~452–473) en vulde
   `ai_hq_*_tokens` / `ai_hq_estimated_cost_usd` per rij. Serper/Firecrawl werden nergens geteld.
2. **Geven de responses tokens terug?** Ja — de Anthropic-response heeft `.usage.input_tokens/
   output_tokens`. Alleen de HQ-call las die uit; de vier andere AI-modules (icp_context,
   ai_signal_scorer, caller_content, legacy) gooiden de usage wég na `extract_anthropic_text()`.
3. **Pricing-config?** Ja: `lead_hq_ai_interpreter.MODEL_PRICING_USD_PER_MTOK` (USD per 1M tokens
   per model) + `estimate_ai_cost_usd(...)`. Die zijn hergebruikt (single source of truth voor
   token-prijzen).

## Wat is gebouwd

**Nieuw: `usage_tracker.py`** — een proces-globale, thread-veilige teller voor één run. De batch
draait rijen via `ThreadPoolExecutor` (één proces, gedeeld geheugen), dus een centrale teller met
`threading.Lock` is veilig; parallel-runs aggregeren correct over workers (getest). Alle
`record_*`-helpers zijn defensief (vangen elke fout op) — instrumentatie kan een run nooit breken.

API:
- `reset()` — begin van een run.
- `record_serper_call(kind)` — kind ∈ hq / non_hq / icp_context / other.
- `record_anthropic_response(response, model, purpose)` — leest `.usage` uit (één regel per
  `messages.create`-site).
- `record_firecrawl_call()`.
- `snapshot()` — dict met tellingen, tokens (som + gemiddelde), per-model-uitsplitsing en
  geschatte kosten (USD + EUR).
- `format_summary_text(snap)` — ASCII-only tabel voor de CLI (Windows cp1252-console-proof).
- `append_history(companies, snapshot, path)` — voegt één regel toe aan het historie-CSV.

### Gewijzigde bestanden (instrumentatie — één regel per call-site)

| Bestand | Toegevoegd |
|---|---|
| `usage_tracker.py` | **nieuw** — de teller + pricing/EUR-config + historie-CSV |
| `lead_hq_ai_interpreter.py` | `record_serper_call("hq")` in `call_serper_for_hq`; `record_anthropic_response(..,"hq")` |
| `lead_non_hq_enrichment.py` | `call_serper_for_enrichment(... usage_kind="non_hq")` → `record_serper_call(usage_kind)` |
| `lead_icp_context_composer.py` | Serper-call met `usage_kind="icp_context"`; `record_anthropic_response(..,"icp_context")` |
| `lead_ai_signal_scorer.py` / `lead_caller_content_composer.py` / `lead_legacy_enrichment.py` | `record_anthropic_response(...)` |
| `lead_hq_sonnet_adjudicator.py` | `record_anthropic_response(..,"hq_sonnet_adjudicator")` |
| `deep_dive_runner.py` | `record_serper_call("other")` + `record_firecrawl_call()` + 2× `record_anthropic_response` |
| `lead_prioritizer_batch_cli.py` | `reset()` vóór de batch; print `format_summary_text()` + `append_history()` erná |
| `lead_prioritizer_batch_app.py` | `reset()` vóór de run; `render_usage_summary()` (expander) + `append_history()` erná |

De scoring-logica en exporter zijn **niet** aangeraakt; `current/` is ongewijzigd.

## Waar de pricing-config staat (om later bij te werken)

- **Token-tarieven per model:** `lead_hq_ai_interpreter.py` → `MODEL_PRICING_USD_PER_MTOK`
  (USD per 1M tokens, `(input, output)`). Update hier wanneer Anthropic-tarieven wijzigen.
- **USD→EUR-koers en per-call-prijzen (Serper/Firecrawl):** `usage_tracker.py` bovenaan →
  `USD_TO_EUR`, `SERPER_USD_PER_CALL`, `FIRECRAWL_USD_PER_CALL` (allemaal PROVISORISCH; Serper/
  Firecrawl hebben geen token-tarief maar credits/per-call — pas aan naar je eigen plan). Een
  model zonder prijs krijgt een **blanco** Anthropic-kostenschatting (nooit een gegokte), maar
  tokens worden wél geteld.

## Waar/hoe je het overzicht bekijkt

- **CLI:** verschijnt automatisch aan het eind van elke run, bijv.:
  ```
  python lead_prioritizer_batch_cli.py --input <x>.xlsx --company-column company_name \
    --domain-column domain --input-country-column country --default-country Italy \
    --mode full --secrets-file .streamlit/secrets.toml --row-limit 3
  ```
  Onderaan de output staat het blok **"API usage - this run"** (Serper/Anthropic/Firecrawl +
  geschatte kosten USD/EUR).
- **Streamlit-app:** `streamlit run lead_prioritizer_batch_app.py` → na een enrichment-run
  verschijnt de expander **"Verbruik deze run (API-calls, tokens & geschatte kosten)"** met
  dezelfde cijfers (metric-tegels + per-model-tabel).
- **Historie (meerdere runs):** elke run voegt één regel toe aan **`logs/usage_history.csv`**
  (timestamp, bedrijven, serper-calls, anthropic-calls, tokens, geschatte kosten, model). Geen
  database; het bestand staat in `logs/` (gitignored, blijft lokaal).

## Testresultaat

- **`test_usage_tracker.py` (nieuw): 10 passed** — telling, cost-math t.o.v. de pricing-tabel,
  onbekend model → blanco kosten + tokens behouden, run-isolatie via `reset()`, historie-CSV
  groeit, ASCII-only CLI-output, en een render-smoke-test van de app-expander (gestubde streamlit).
- Regressiesuites (pipeline / batch-core / signal-extractor): **290 passed**, geen regressies.
- **CLI-verificatie** (3 bedrijven, `--rich-icp-context`): Serper **30** (hq=3, non_hq=18,
  icp_context=9) — exact 1 HQ + 6 non-HQ + 3 icp_context per bedrijf; Anthropic **6** (hq 3 +
  icp_context 3); kosten ~€0.039. Zonder `--rich-icp-context`: icp_context=0, Anthropic=hq-only.
- **Parallelle app-pad** (2 workers, threads): identieke tellingen (Serper 30 / Anthropic 6) →
  thread-veilige aggregatie bevestigd; app toont dezelfde cijfers als de CLI.
- **Historie-CSV** groeit correct met één regel per run (header eenmalig).

## Terugdraaien / uitbreiden

De instrumentatie is additief en defensief. Wil je 'm uitzetten, verwijder de `usage_tracker`-
aanroepen (of laat `reset()`/print/render weg in CLI/app). Nieuwe API-call-sites tel je mee door
er één `record_*`-regel aan toe te voegen.
