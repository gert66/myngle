# Lead Prioritizer v2 — Handoff: rich ICP context, Deep Dive, quote verification

Status as of this handoff: all three features below are complete, wired end-to-end
(core → batch → CLI → Streamlit app → Excel → Lovable JSON), and fully tested.

```
pytest -q
```
should report all tests passing (1227 at the time of writing). One pre-existing,
unrelated flaky test exists: `TestContentLanguageEnglishUnchanged::
test_explicit_english_matches_omitted_default` occasionally fails on a `last_updated`
timestamp differing by one second between two runs in the same test — re-run if you
see only that failure; it predates this work and is not caused by it.

Branch: `work`. No PRs opened, nothing merged to `main`, per repo convention.

**A later handoff addendum — multi-source evidence links, opt-in AI signal scoring,
and multilingual retrieval — is appended at the bottom of this file
("Addendum: evidence links, AI signal scoring, multilingual keywords"). Read that
section for the most recent work; the rest of this file is unchanged from the
original handoff below.**

## What's new, at a glance

Three independent opt-in layers were added to the v2 pipeline, all off the critical
scoring path:

1. **Rich ICP context** (`lead_icp_context_composer.py`) — AI-composed
   `icp_buying_signals` / `icp_likely_training_interest` / `icp_potential_buyer_function`,
   using broader thematic Serper queries plus the curated non-HQ signals already on the
   result. Independent of everything else below.
2. **Deep Dive** (`deep_dive_schema.py`, `deep_dive_runner.py`) — opt-in, triggered after
   scoring (score threshold and/or confirmed foreign HQ), collects deeper evidence
   (Firecrawl if a key is present, else localized Serper + plain fetches) and distills
   discrete, source-backed `DeepDiveClaim`s (category, statement, literal quote, source
   URL) via one Anthropic call.
3. **Quote verification** (`quote_verifier.py`) — mechanically re-checks every Deep Dive
   claim's quote against the actual page text (normalized exact match, then a
   `difflib.SequenceMatcher` fuzzy layer), catching AI hallucination/paraphrasing. Includes
   self-healing: a fuzzy match is auto-corrected to the real page text, and a "not found"
   quote gets one bundled re-extraction attempt per company that is itself mechanically
   re-verified before ever being trusted.

**Hard invariant, enforced and tested everywhere:** none of the three ever touch
`evidence_items`, `signals`, or `final_commercial_fit_score`. Score-invariance tests exist
for all three (see "Tests" below) — same input with the feature on vs. off yields byte-
identical score fields.

## 1. Rich ICP context

- `lead_icp_context_composer.py`: `compose_icp_context()` + `collect_icp_context_evidence()`.
  3 thematic Serper queries (general context, L&D/training, language/global teams — no
  competitor query), hosted-platform evidence filtered before the prompt, JSON-only
  Anthropic call, tolerant parsing, never raises.
- `lead_prioritizer_core.py`: `prioritize_single_lead(..., compose_icp_context=False)` —
  independent of `compose_caller_content_flag`. Fills `icp_buying_signals`,
  `icp_likely_training_interest`, `icp_potential_buyer_function`, `icp_context_by_ai`,
  `icp_context_content_note` on `LeadPrioritizationResult`.
- `lead_prioritizer_batch_core.py`: `BatchRunConfig.rich_icp_context`.
- CLI: `--rich-icp-context`. Streamlit: "Rijkere ICP-context via AI (opt-in)" checkbox.
- Excel: `icp_buying_signals` / `icp_likely_training_interest` / `icp_potential_buyer_function`
  / `icp_context_by_ai` / `icp_context_content_note` columns on Enriched Leads.
- Lovable JSON: nested `icp_context` object on the detail record, present only when at
  least one field is actually filled.

## 2. Deep Dive

- `deep_dive_schema.py`: `DeepDiveClaim` (`claim_id`, `category`, `statement`, `quote`,
  `source_url`, `source_title`, `source_kind`, `domain_verified`, `retrieval_method`, plus
  the quote-verification fields below) and `DeepDiveResult` (`company_name`, `domain`,
  `parent_domain`, `trigger_reason`, `claims`, `pages_crawled`, `firecrawl_used`,
  `localized_queries_used`, `error`, `generated_at`), both with `to_json_dict()`.
- `deep_dive_runner.py`: `run_deep_dive()`.
  - Collection: Firecrawl (own + parent domain, candidate about/careers/locations/
    newsroom-style paths) when a key is given and reachable; a hard failure (network error
    or a 401/402/403/429 key/quota status) discards everything from that attempt and falls
    back. Fallback: 3–5 localized Serper queries (`gl`/`hl` derived from the lead's
    country via `gl_hl_for_country` — never a hardcoded `gl=us`) plus a bare `urllib` fetch
    of the own/parent homepage.
  - Distillation: one Anthropic call extracts claims from whatever pages were collected.
    A claim is dropped if its `source_url` isn't one of the URLs actually supplied (never
    trust an invented URL), its category isn't one of the five fixed values, or the URL
    resolves to a hosted careers/job platform (`hq_simple_detector.is_hosted_careers_platform_domain`).
  - Never raises: any failure (collection, missing key, AI call/parse error) sets
    `DeepDiveResult.error` instead of throwing.
- `lead_prioritizer_batch_core.py`: `BatchRunConfig.deep_dive` / `deep_dive_min_score`
  (default 8.0) / `deep_dive_on_foreign_hq` (default True) / `deep_dive_max_pages`
  (default 6). `should_run_deep_dive()` is the trigger gate, evaluated per row AFTER
  `prioritize_single_lead()` returns — score threshold takes priority over foreign-HQ.
  Claims are flattened one-row-per-claim into a new `"deep_dive"` output table.
- CLI: `--deep-dive`, `--deep-dive-min-score`, `--deep-dive-max-pages`. Optional
  `FIRECRAWL_API_KEY` (env, then `--secrets-file`) — missing is not an error, it only
  switches Deep Dive to its fallback path (`load_firecrawl_key()` in
  `lead_prioritizer_batch_cli.py`).
- Streamlit: "Deep dive voor top-leads (opt-in)" checkbox + score-threshold number_input +
  foreign-HQ toggle; `FIRECRAWL_API_KEY` shown in the sidebar key status.
- Excel: a `"Deep Dive"` sheet, written **only when it has at least one row**
  (`build_excel_workbook_bytes` in `lead_prioritizer_batch_core.py`). Columns:
  `source_index`, `company_name`, `trigger_reason`, `category`, `statement`, `quote`,
  `source_url`, `source_kind`, `domain_verified`, `retrieval_method`,
  `quote_verified`, `quote_verification_status`, `quote_match_score`, `original_quote`,
  `error`.
- Lovable JSON: `export_lead_prioritizer_to_lovable_json.py` reads the optional
  `"Deep Dive"` sheet (no warning when absent — most workbooks won't have it) and adds a
  nested `deep_dive` object (`trigger_reason` + `claims`) to a company's detail record,
  present only when that company has at least one claim row.

## 3. Quote verification (badge)

- `quote_verifier.py`: pure matching module, no network/AI calls.
  - `verify_quote_on_page(quote, page_text) -> QuoteVerification` (`status` /
    `match_score` / `matched_snippet`): normalized exact match first (lowercase, Unicode
    NFKC, whitespace collapsed, typographic quotes/dashes folded to ASCII) → `"verified"`
    (1.0); else a sliding-window fuzzy match via stdlib `difflib.SequenceMatcher` across
    ~80/100/120% of the quote's length → `"fuzzy_match"` at ratio ≥ 0.85, else
    `"not_found"`. Quotes under 25 normalized characters skip the fuzzy layer entirely
    (too many false positives on short strings) — only an exact hit counts.
  - `verify_claims(claims, page_cache, fetch_fn, max_verify_fetches=5)`: mutates each
    claim's `quote_verified` / `quote_verification_status` / `quote_match_score` /
    `quote_matched_snippet` in place. Reuses already-fully-fetched pages
    (`retrieval_method in ("firecrawl", "plain_fetch")`) as the cache — a
    `serper_localized` "page" is really just a short Serper snippet, never trusted as the
    verification cache, so its claims always trigger a fresh targeted fetch. Fetches are
    capped at `max_verify_fetches`; over the cap a claim is left `"not_checked"`, not
    marked failed. A fetch exception/empty result → `"fetch_failed"` on that claim only.
- `deep_dive_schema.py`: `DeepDiveClaim` gained `quote_verified` (default `False`),
  `quote_verification_status` (default `"not_checked"`), `quote_match_score` (default
  `0.0`), `quote_matched_snippet` (default `""`), `original_quote` (default `""`), and a
  derived `badge` property (`"confirmed"` for `verified`/`verified_corrected`/a
  high-confidence `fuzzy_match`, else `"unconfirmed"`) — all included in `to_json_dict()`.
- `deep_dive_runner.py` self-healing (in `run_deep_dive()`, after distillation):
  - `"fuzzy_match"` → automatically corrected: `quote` replaced by the mechanically
    matched page text, the AI's original text preserved in `original_quote`, status →
    `"verified_corrected"`.
  - `"not_found"` → **at most one** bundled Anthropic re-extraction call per company
    (never per claim) is made, handing over the actual page text and asking for a literal
    supporting quote or `null` per claim; every returned candidate is re-verified via
    `verify_quote_on_page()` before ever being accepted — the AI never gets the final
    word, the mechanical matcher does. Rejected/`null` candidates leave the claim
    untouched as `"not_found"`.
  - `"fetch_failed"` / `"not_checked"` are never touched (no page text to correct
    against).
  - `run_deep_dive(..., verify_quotes=True, auto_correct_quotes=True, max_verify_fetches=5)`
    — both default on; `auto_correct_quotes` only takes effect when `verify_quotes` is on.
- `lead_prioritizer_batch_core.py`: `BatchRunConfig.verify_quotes` / `auto_correct_quotes`
  (both default `True`).
- CLI: `--no-verify-quotes`, `--no-auto-correct-quotes`.
- Streamlit: "Verify Deep Dive quotes against the source page" checkbox under the
  deep-dive checkbox, with a nested "Auto-correct fuzzy/not-found quotes" checkbox shown
  only while verification is on.
- Excel "Deep Dive" sheet and Lovable JSON both carry `quote_verified` /
  `quote_verification_status` / `quote_match_score` / `original_quote`; Lovable JSON
  additionally derives `badge` per claim (mirrors `DeepDiveClaim.badge`) so the frontend
  never has to re-derive the 0.85 confirm threshold.
- **`not_found` claims are shown, never dropped** — it's a trust signal (`badge:
  "unconfirmed"`), not a filter. The only reason a claim disappears entirely is the
  existing hosted-careers-platform guard rejecting its source URL outright.

## Design choices worth knowing about

- **No `parent_domain` anywhere in the v2 pipeline.** Only the parent company's
  name/country/city are ever resolved (`ai_parent_company` / `ai_parent_hq_country` /
  `ai_parent_hq_city`). Deep Dive's `parent_domain` parameter is always `None` when called
  from batch core — it still works from `parent_company` alone via the fallback query
  builder. If a real parent-domain resolution step is ever added upstream, wiring it
  through is a small, localized change (`lead_prioritizer_batch_core.py`'s
  `run_deep_dive(...)` call).
- **`quote_matched_snippet` / a self-healed `quote` is the *normalized* page excerpt**
  (lowercase, whitespace-collapsed, typographic punctuation folded to ASCII) — not the
  original page's exact casing/punctuation. This was a deliberate simplicity trade-off:
  precisely mapping a normalized match position back to the original page's casing would
  need an offset-preserving normalizer, which wasn't worth the complexity for what is
  fundamentally an audit string. If a future task wants original casing preserved, that's
  the place to revisit (`quote_verifier._match_normalized`).
- **Every AI composition step in this family follows the same shape**: an explicit
  `anthropic_api_key` param, a JSON-only system prompt, tolerant fence-stripped parsing
  duplicated locally per module (not shared via import — this mirrors the pre-existing
  convention in `lead_hq_ai_interpreter.py` / `lead_hq_sonnet_adjudicator.py` /
  `lead_caller_content_composer.py`), and a function that never raises. If you add a
  fourth AI step, copy this shape rather than inventing a new one.
- **Deep Dive and quote verification only wire into the standard `run_batch_dataframe`
  path**, not into the `full_foreign_hq_only` / `full_non_english_foreign_hq_only`
  specialty modes (`run_batch_foreign_hq_only` / `run_batch_non_english_foreign_hq_only`).
  This matches the pre-existing precedent: `compose_caller_content` wasn't wired into
  those modes either. `build_excel_workbook_bytes` / the parallel-run combiner both
  degrade gracefully (via `.get("deep_dive")`) when a mode doesn't produce that table.

## Tests

All new/changed test files (run individually or via the full `pytest -q`):

- `test_lead_icp_context_composer.py`, `test_lead_v2_full_pipeline.py`
  (`TestComposeIcpContextFlag`, `TestComposeFlagsAreIndependent`,
  `TestComposeIcpContextScoreInvariance`) — rich ICP context.
- `test_deep_dive_schema.py`, `test_deep_dive_runner.py` — Deep Dive collection,
  distillation, guards, and (in the later classes) quote-verification wiring +
  self-healing end-to-end.
- `test_quote_verifier.py` — the matcher and `verify_claims()` orchestration in isolation.
- `test_lead_prioritizer_batch_core.py` — trigger gate (`TestShouldRunDeepDive`),
  Excel flattening, the conditional "Deep Dive" sheet, score-invariance for both Deep
  Dive and quote verification (`TestDeepDiveScoreInvariance`), and the A×B independence
  test (`TestRichIcpContextAndDeepDiveIndependence`, all four `rich_icp_context` ×
  `deep_dive` on/off combinations through the real `run_batch_dataframe`).
- `test_lead_prioritizer_batch_cli.py` — every new flag's parsing and end-to-end
  passthrough, including all `--rich-icp-context` × `--deep-dive` combinations and the
  Firecrawl-key resolution helper (`load_firecrawl_key`).
- `test_export_lead_prioritizer_to_lovable_json.py` — `icp_context` and `deep_dive`
  nested-object presence/absence rules, cross-row isolation by `source_index`, no leakage
  into `ui_payload`, and the exported `badge`/quote-verification fields.

## Not done / explicitly out of scope

- No changes to `enrich_clients_claude.py` or `input_cleaner_register_edition.py` (per
  instruction — Deep Dive's Firecrawl call mirrors the latter's request/response shape
  without importing it, and its own single-key call has no multi-key failover/health
  tracking).
- No FastAPI on-demand endpoint yet — `deep_dive_runner.run_deep_dive()` was deliberately
  kept dependency-free (no Streamlit, no batch-core import) so it can be dropped behind
  one later without refactoring.
- Deep Dive / quote verification are not wired into the `full_foreign_hq_only` /
  `full_non_english_foreign_hq_only` batch modes (see "Design choices" above).
- No UI for browsing Deep Dive claims yet — the `badge` field exists precisely so a
  future Lovable frontend doesn't have to re-derive the confirm threshold, but no such
  frontend has been built here.

---

# Addendum: evidence links, AI signal scoring, multilingual keywords

Status as of this addendum: all three parts below (Onderdeel 1, 2, 3) are complete,
wired end-to-end, and fully tested.

```
pytest -q
```
should report all tests passing (1295 at the time of writing; the pre-existing flaky
`TestContentLanguageEnglishUnchanged` test noted above is unrelated and unaffected).

Branch: `work`. No PRs opened, nothing merged to `main`. No API keys were logged
anywhere in this work.

## Onderdeel 1 — multiple clickable evidence links per signal/driver

Never changes scores; purely additive.

- `lead_output_schema.py`: `LeadSignal.evidence_urls` (ordered, deduplicated list;
  `evidence_urls[0] == evidence_url` whenever non-empty — `evidence_url` itself is
  untouched). Same pattern for the HQ signal: `HQDetectionResult.hq_evidence_urls` /
  `LeadPrioritizationResult.hq_evidence_urls`, plus five new semicolon-joined
  `*_evidence_urls` string fields on `LeadPrioritizationResult` (one per non-HQ signal),
  mirroring the existing singular `*_evidence_url` fields.
- `lead_non_hq_signal_extractor.py`: `evidence_urls` is built strictly from the
  `usable` evidence list per signal (same guards as everything else — hosted-platform
  and external-training evidence is never included, even in the rare case where the
  singular `evidence_url` itself falls back to an excluded item for audit purposes).
- `lead_hq_ai_interpreter.py`: the AI is asked for `evidence_urls` alongside the
  existing single URL; every returned URL is mechanically checked against
  `_known_urls_from_serper_payload()` (KG/answerBox/organic links actually present in
  the Serper payload) before being kept — an invented URL is silently dropped, exactly
  like Deep Dive's claim-URL validation.
- `export_lead_prioritizer_to_lovable_json.py`: `build_evidence_sources(urls,
  own_domains, primary_title, max_sources=5)` turns a URL list into
  `[{"url", "domain", "title"}, ...]` — own-domain URLs first, then others, hosted
  platform URLs excluded, capped at 5. Wired into:
  - `commercial_fit_drivers[]` — each driver gets `evidence_source_url` /
    `evidence_source_domain` (unchanged singular fields, now always equal to the first
    array element) plus a new `evidence_sources` array (omitted entirely when empty).
  - `what_is_hot_items[]` — a new array parallel to the existing `what_is_hot` (same
    bullet text and order, deliberately a separate function
    `build_curated_what_is_hot_items()` so the well-tested original is never touched),
    each item optionally carrying its own `evidence_sources`. Non-Italy only — Italy's
    `ui_payload` omits `what_is_hot_items` entirely, exactly like every other
    curated-layer addition in this pipeline.
- Excel: one semicolon-joined `*_evidence_urls` column per signal, plus
  `hq_evidence_urls`, alongside (never replacing) the existing singular columns.

### Mini-JSON example of `evidence_sources` (for the Lovable frontend prompt)

```json
{
  "commercial_fit_drivers": [
    {
      "id": "international_business_context",
      "label": "International business context",
      "strength": "Strong",
      "evidence": "Operates across 11 countries with 40+ offices worldwide.",
      "note": "",
      "evidence_source_url": "https://www.acmeglobal.com/about",
      "evidence_source_domain": "acmeglobal.com",
      "evidence_sources": [
        {"url": "https://www.acmeglobal.com/about", "domain": "acmeglobal.com", "title": null},
        {"url": "https://en.wikipedia.org/wiki/Acme_Global", "domain": "wikipedia.org", "title": null}
      ]
    }
  ],
  "what_is_hot_items": [
    {
      "text": "International business context: Operates across 11 countries with 40+ offices worldwide.",
      "evidence_sources": [
        {"url": "https://www.acmeglobal.com/about", "domain": "acmeglobal.com", "title": null}
      ]
    }
  ]
}
```

Frontend notes: `evidence_source_url` / `evidence_source_domain` (singular) always
equal `evidence_sources[0]` when present — safe to keep using the singular fields for
a simple "source" link and use the array only when a "show all sources" affordance is
wanted. Both `evidence_source_url` and `evidence_sources` are entirely absent from a
driver/item object when there is no usable evidence — always check for the key, don't
assume an empty array.

## Onderdeel 2 — opt-in AI signal scoring (changes scores, explicitly opt-in only)

- `lead_ai_signal_scorer.py` (new): `score_signals_with_ai(company_name, country,
  evidence_items, anthropic_api_key, ai_model=...)`. Reuses the deterministic
  extractor's own guard (`_usable_evidence_for_signal` — hosted-platform and external-
  training evidence already excluded, not re-implemented) to build the evidence pool,
  then makes one Anthropic call asking for a `verdict`
  (`positive_evidence`/`weak_evidence`/`no_positive_match`), a short `reason`, and
  `supporting_evidence_ids` per supported signal name. Judgment is semantic (any
  language, synonyms, derived facts like "11 countries" = international); a parent/
  ownership claim requires the evidence to recognizably name the company; ties go to
  the lower verdict.
- **Mechanical validation, always applied, regardless of what the AI said:**
  `supporting_evidence_ids` not present in that signal's own supplied evidence are
  dropped; a `positive_evidence`/`weak_evidence` verdict left with zero valid ids is
  downgraded to `no_positive_match`. The AI never gets the final say on which sources
  exist. `evidence_url(s)` are then derived purely from the validated ids, so evidence
  links work identically to Onderdeel 1 in both scoring modes.
- Any call/parse failure (including no API key) yields `call_success=False` with an
  `error` string and an empty `signals` list — the caller always falls back to the
  deterministic extractor, so a row is never shipped with no signals.
- `lead_prioritizer_core.py`: `prioritize_single_lead(..., ai_signal_scoring=False)`.
  When `True` and `extract_non_hq_signals_flag` is also on, AI verdicts replace the
  deterministic ones **before** the existing score mapping — `lead_v2_scoring_adapter.py`
  and `commercial_fit_scoring.py` are completely unchanged (same formula, same weights,
  only the signal input differs). `False` (the default) is byte-for-byte identical to
  today, covered by a regression test. `run_full_v2_pipeline=True` does **not** enable
  this flag — it stays a separate, explicit opt-in as required.
- `result.signal_scoring_mode` is `"deterministic"` or `"ai"` — recorded in the result,
  the Excel "Enriched Leads" sheet, and the Lovable JSON `evidence_audit` block, so a
  deterministic- and AI-scored dataset are never silently mixed.
- `lead_prioritizer_batch_core.py`: `BatchRunConfig.ai_signal_scoring` (default
  `False`), independent of every other opt-in flag.
- CLI: `--ai-signal-scoring`. Streamlit: a checkbox with an explicit warning that this
  changes `final_commercial_fit_score` versus the default mode.

## Onderdeel 3 — multilingual keywords + localized retrieval (deterministic mode)

This one **is** allowed to change scores in the default (deterministic) path — more
local-language snippets now match — which is why it bumps a version field.

- `lead_non_hq_enrichment.py`: `call_serper_for_enrichment(query, serper_api_key, gl=None,
  hl=None)` — `gl`/`hl` are only added to the Serper request body when explicitly given,
  so any existing caller that doesn't pass them keeps today's exact request shape.
  `gl_hl_for_country(country)` maps at least Netherlands→nl/nl, Italy→it/it,
  Germany→de/de, France→fr/fr, Spain→es/es, Belgium→be/nl; any other/unknown country
  returns `(None, None)` (today's unlocalized behavior). `collect_non_hq_enrichment_evidence(...,
  country=None)` derives `gl`/`hl` from the lead's effective input country and passes
  them through. `lead_prioritizer_core.py` now passes `country=effective_country` at
  its one call site.
- `lead_non_hq_signal_extractor.py`: `_SIGNAL_KEYWORDS` gained a short, deliberately
  narrow set of NL/IT/DE/FR/ES equivalents per signal (e.g. medewerkers/dipendenti/
  Mitarbeiter/employés/empleados for "employees"; opleiding/formazione/Schulung/
  formation/formación for "training"; internationaal/internazionale/internacional for
  "international" — DE/FR already share the English spelling so aren't duplicated;
  vestigingen/sedi/Standorte/sedes for "locations/sites"). Only words judged safe from
  false positives were added — no ambiguous short tokens.
- `SIGNAL_EXTRACTOR_VERSION = "v2-multilingual"` (new constant) is exposed as
  `result.signal_extractor_version` (also on Excel and in the Lovable JSON
  `evidence_audit` block) so a keyword-set change is never silently invisible in old
  vs. new datasets.
- `lead_icp_context_composer.py` was **not** touched — it calls
  `call_serper_for_enrichment` without `gl`/`hl` and keeps its existing unlocalized
  behavior, since Onderdeel 3 only asked for the non-HQ enrichment path.

## Tests (this addendum)

- `test_lead_non_hq_signal_extractor.py`: `TestEvidenceUrls` (extractor
  `evidence_urls`), `TestMultilingualKeywords` (one test per new language per signal
  family, plus an English-still-matches regression), `TestSummary`'s two new
  `signal_extractor_version` tests.
- `test_lead_prioritizer_ai_hq.py`: `TestHqEvidenceUrls` (7 tests — ordering,
  singular/array parity, invented-URL rejection, dedupe, missing-key fallback, no-URL
  case, score/classification invariance).
- `test_export_lead_prioritizer_to_lovable_json.py`: driver `evidence_sources`
  (ordering/dedupe/own-domain-first/hosted-platform exclusion/5-cap/singular-equals-
  first/omitted-when-empty), `what_is_hot_items` (parallel to `what_is_hot`, Italy
  absence), and an explicit score/backward-compat invariance test comparing a run with
  and without `evidence_urls` populated.
- `test_lead_non_hq_enrichment.py`: `TestGlHlLocalization` (country mapping,
  case-insensitivity, unknown-country fallback, `gl`/`hl` included/omitted on the
  actual request body, passthrough from `collect_non_hq_enrichment_evidence`), plus a
  `TestCoreFlagGating` case proving `effective_country` reaches the collector.
- `test_lead_ai_signal_scorer.py` (new, 17 tests): prompt/evidence filtering
  (guard reuse proven — hosted-platform/external-training evidence never reaches the
  prompt), verdict translation into `LeadSignal`, the full mechanical-validation suite
  (invented id dropped, positive verdict downgraded when no valid id, cross-signal id
  rejected), and API-failure fallback.
- `test_lead_v2_full_pipeline.py`: `TestAiSignalScoringFlag` (off by default and
  byte-identical to today, full-preset does not enable it, AI success replaces
  signals and marks `signal_scoring_mode`, AI failure falls back to the deterministic
  extractor with matching signal names/scores, and same-signal-input-in ⇒
  same-score-out proving the scoring formula itself is untouched).
- `test_lead_prioritizer_batch_cli.py` / `test_lead_prioritizer_batch_core.py`: flag
  parsing/defaults and independent passthrough for `--ai-signal-scoring` /
  `ai_signal_scoring`, alongside the existing compose-flag combinations.

## Design choices worth knowing about (this addendum)

- **Per-signal id validation, not global.** The task text said "ids not present in
  the input are removed," which could be read as validating against every evidence
  item handed to the AI across all signals. This implementation validates each
  signal's `supporting_evidence_ids` only against the evidence collected *for that
  same signal* — so one signal's verdict can never be backed by another signal's
  evidence, which matches how the deterministic extractor already scopes evidence per
  signal. See `test_lead_ai_signal_scorer.py::TestMechanicalValidation::
  test_evidence_id_from_a_different_signal_is_not_accepted`.
- **A signal with zero usable evidence never reaches the AI prompt and never gets a
  `LeadSignal`** — mirroring the deterministic extractor's "no evidence group → no
  signal" rule. The one edge-case divergence: if a signal's evidence exists but every
  item is guard-excluded, the deterministic extractor still emits an explicit
  score-0 signal with an "excluded" reason, while AI mode simply omits that signal
  entirely. Functionally equivalent for scoring (both contribute nothing), but the
  audit trail differs slightly in that specific edge case.
- **`ai_signal_scoring` reuses the pipeline's single `ai_model` parameter** rather than
  introducing a dedicated model override, matching the existing precedent
  (`compose_icp_context` / `compose_caller_content_flag` do the same).
