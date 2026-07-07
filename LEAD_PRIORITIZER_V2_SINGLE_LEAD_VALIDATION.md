# Lead Prioritizer v2 — Single-Lead Pipeline Validation

**Repo / branch:** `gert66/myngle`, branch `work`.

## Purpose

Manual validation checklist for the **full v2 single-lead pipeline**
(`prioritize_single_lead(..., run_full_v2_pipeline=True)`), or the equivalent
"Run full v2 single-lead pipeline" checkbox in `lead_prioritizer_test_app.py`.

This is a manual QA aid only. It does not run automatically and requires live
`SERPER_API_KEY` and `ANTHROPIC_API_KEY` (from `.streamlit/secrets.toml` or the
environment) to exercise the real HQ + non-HQ evidence flow. It adds no batch
processing and does not change legacy ranking.

## How to run

```bash
export SERPER_API_KEY=...
export ANTHROPIC_API_KEY=...
streamlit run lead_prioritizer_test_app.py
```

Tick **Run full v2 single-lead pipeline**, enter the company / domain / country,
and run. Confirm the headline shows `Pipeline mode: full_v2_single_lead`.

### CLI runner (headless)

`run_v2_single_lead_validation.py` runs all six cases below with
`run_full_v2_pipeline=True` (`input_country` defaults to Italy) and writes a
compact, secret-free JSON + CSV report to `validation_outputs/`.

```bash
export SERPER_API_KEY=...
export ANTHROPIC_API_KEY=...
python run_v2_single_lead_validation.py

# Optional: fall back to a TOML secrets file when the env vars are unset
python run_v2_single_lead_validation.py --secrets-file .streamlit/secrets.toml
```

Keys are read from the environment first, then from `--secrets-file` if given.
Key values are never printed or written to output. Each output record contains
only the compact fields, evidence/signal counts, at most six evidence URLs, and
`run_success` / `run_error` — raw AI JSON and raw Serper payloads are excluded.
Outputs land in `validation_outputs/` (gitignored). If a key is missing, cases
are still recorded with `run_success=false` and a `run_error`.

## Test cases (input_country = Italy)

| Company | Domain |
| --- | --- |
| BMW ITALIA S.P.A. | bmw.it |
| KNORR-BREMSE RAIL SYSTEMS ITALIA S.R.L. | knorr-bremse.com |
| DANFOSS S.R.L. | danfoss.com |
| RICOH ITALIA S.R.L. | ricoh.it |
| CANNON BONO S.P.A. | cannonbono.com |
| IET | iet.it |

Expected HQ behavior follows the existing notes in
`LEAD_PRIORITIZER_HQ_VALIDATION.md`; do not assume live Serper/AI outputs beyond
what is documented there.

## Per-test checklist

For each test case above, confirm:

- [ ] HQ result present (`hq_detected_country` / `hq_structure_type` populated or
      an explicit manual-review reason given)
- [ ] Input country used is **Italy** (`input_country` = Italy)
- [ ] Non-HQ evidence collected (`evidence_items` non-empty) when the full
      pipeline is on
- [ ] Signals extracted (`signals` populated for signals that have evidence)
- [ ] App summary fields populated (`evidence_summary_app` /
      `key_source_links_app` / `advanced_notes_app`)
- [ ] Commercial score populated (`final_commercial_fit_score` /
      `commercial_tier`)
- [ ] Caller/app fields populated (`what_is_hot_app`, `why_relevant_app`,
      `caller_angle_app`, `call_starter_app`, etc.)
- [ ] **No competitor wording** anywhere in the output
- [ ] **No rapid-growth-as-positive** wording anywhere in the output
- [ ] Evidence URLs / snippets are traceable back to `evidence_items`
      (nothing invented)
- [ ] Manual review flag (`needs_manual_review`) checked for suspicious or
      domain-mismatch cases (e.g. domain that does not match the legal entity)

## Notes

- `v2_pipeline_mode` should read `full_v2_single_lead` for these runs.
- A lead with no non-HQ evidence should still return an HQ result and a
  commercial score computed from the HQ signal plus zeros.
- Competitor evidence is never collected, mapped, displayed, or scored; rapid
  growth is never surfaced as a positive driver.
