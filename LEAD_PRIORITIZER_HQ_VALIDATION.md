# Lead Prioritizer HQ validation

Status: accepted as canonical HQ basis after live validation.

Repository: `gert66/myngle`
Branch: `work`

## Scope

This note documents the accepted HQ detection behavior for Lead Prioritizer v2 after the C1-C3 implementation and live manual validation.

The Lead Prioritizer HQ engine is intended to become the clean production version of the HQ strategy that was first validated in `hq_lookup_probe_app.py` / HQ Recovery. The probe app remains an experimental/test UI. Production logic should live in the Lead Prioritizer modules.

## Production modules

The canonical HQ flow is implemented across:

- `hq_simple_detector.py`
- `lead_hq_ai_interpreter.py`
- `lead_prioritizer_core.py`
- `lead_output_schema.py`

The production modules must not import from `hq_lookup_probe_app.py`.

## Accepted canonical strategy

1. Use the run/default input country as `LeadInput.input_country`. For the Italy register flow this is normally `Italy`.
2. Build one simple HQ query from the domain root: `{domain_root} headquarters`.
3. If a domain exists, do not use the legal company name in the query.
4. Use one Serper call for HQ evidence.
5. Use AI-first interpretation of the Serper Knowledge Graph, answer box, and top organic results.
6. The only deterministic post-AI scoring step is normalized country comparison.
7. Score `3` only when AI classification is `foreign_parent`, confidence is `High` or `Medium`, and the parent HQ country differs from the input country.
8. Score `0` for domestic, regional/local branch, and low-confidence foreign parent cases.
9. Use manual review and no positive HQ score for unclear, blank country, AI error, or unusable AI response cases.
10. Competitor evidence is audit-only and must not trigger HQ scoring.

## Taxonomy decision

Keep the clean 4-class production taxonomy:

- `foreign_parent`
- `domestic`
- `regional_branch_only`
- `unclear`

Do not adopt the older 7-class probe taxonomy at this stage. In particular, do not automatically score separate experimental classes such as `global_professional_network` unless the AI classifies the company as `foreign_parent` under the production rules above.

## C1-C3 changes accepted

C1. Robust AI parser

- Handles markdown fenced JSON.
- Handles prose around JSON.
- Extracts the first usable JSON object.
- Uses conservative regex fallback for core fields when JSON is malformed or truncated.
- A malformed/truncated `reason` field should not make the whole row fail if classification, confidence, and country fields are recoverable.

C2. Default input country

- `prioritize_single_lead(...)` accepts `default_input_country="Italy"`.
- If `LeadInput.input_country` is blank/None, the default is used for AI interpretation and returned output.
- The interpreter itself should not hardcode the default country.

C3. Audit fields

Output should preserve enough debug information to understand the score decision, including:

- `domain_root`
- `query_used`
- `parser_source`
- `ai_hq_raw_json`
- `competitor_signal_excluded_from_next_scoring`

## Live validation results

The following live checks were run through `lead_prioritizer_test_app.py` with real Serper and Anthropic keys.

| Company | Domain | Expected | Observed | Accepted |
|---|---|---:|---:|---|
| BMW ITALIA S.P.A. | `bmw.it` | foreign HQ score 3 | Germany / Munich, score 3 | Yes |
| KNORR-BREMSE RAIL SYSTEMS ITALIA S.R.L. | `knorr-bremse.com` | foreign HQ score 3 | Germany / Munich, score 3 | Yes |
| DANFOSS S.R.L. | `danfoss.com` | foreign HQ score 3 | Denmark / Nordborg, score 3 | Yes |
| RICOH ITALIA S.R.L. | `ricoh.it` | foreign HQ score 3 | Japan / Tokyo, score 3 | Yes |
| BOSCH REXROTH S.P.A. | `boschrexroth.com` | foreign HQ score 3 | Germany / Lohr am Main, score 3 | Yes |
| CANNON BONO S.P.A. | `cannonbono.com` | domestic HQ score 0 | Italy / Peschiera Borromeo, score 0 | Yes |
| IET | `iet.it` | difficult acronym edge case | misrouted to Institution of Engineering and Technology, UK, score 3 | Known edge case, no fix now |

## Known edge case

Short or generic acronym domain roots can still misroute to a better-known unrelated entity. Example: `IET` / `iet.it` was interpreted as Institution of Engineering and Technology in the UK.

Decision: do not implement an acronym/domain guard now. The current behavior is accepted as good enough for the ranking use case, because normal foreign-HQ and domestic cases validate well and overcorrecting may damage valid short-brand cases.

Revisit only if multiple acronym/domain contamination cases appear in normal use.

## Current acceptance decision

The C1-C3 Lead Prioritizer HQ core is accepted as the canonical basis for the next stage.

Do not make further HQ-detection changes before integrating this core into the next Lead Prioritizer flow, unless new validation results show a repeated error pattern.