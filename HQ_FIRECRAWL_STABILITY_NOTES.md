# HQ detection stability fix — Firecrawl own-domain as PRIMARY source + `hq_location_summary`

Branch: `work`. No PRs, nothing merged to `main`. No API keys logged anywhere.

## The bug (root cause, confirmed from the on-disk export artifacts)

The `foreign_ownership_or_group_structure` signal flipped between two enrichment
runs of the *exact same* company on the same day —
**FUJIFILM Manufacturing Europe B.V.** (`fujifilmtilburg.nl`, redirects to
`fujifilm.com/nl`):

| Export | `sig_foreign_hq_score` | classification | evidence_url (top) | quote |
|---|---|---|---|---|
| `lovable_json_exports/Netherlands/20260706_141403/company-details-000.json` | **3.0** | `foreign_parent` (High) | `https://nl.linkedin.com/company/fujifilm-europe-b.v.` | "Part of the FUJIFILM European headquarters, based in Tilburg - the Netherlands." |
| `lovable_json_exports/Netherlands/20260706_183337/company-details-000.json` | **0.0** | `regional_branch_only` | `https://www.fujifilm.com/ef/en/about/company-profile` | "Fujifilm was established in Tilburg. part of the European headquarters" |

Both runs fired the identical query `"fujifilmtilburg headquarters"` (verified
in the `query_used` field of both files) and — notably — both correctly
resolved `ai_parent_hq_country="Japan"`, `ai_parent_hq_city="Tokyo"`. The only
thing that changed was the **classification label**, and with it the score:

- Primary HQ detection rested on a **single** Serper query
  (`hq_simple_detector.build_simple_hq_query` → `"{domain_root} headquarters"`)
  whose top Knowledge-Graph/answer-box/organic-5 results were handed to a
  one-shot Haiku classifier (`lead_hq_ai_interpreter.interpret_hq_with_ai`).
- Serper is **not deterministic** for an identical query: run 1's top result was
  a LinkedIn snippet that reads as a foreign HQ; run 2's top result was a
  fujifilm.com snippet that, *in isolation*, reads like a plain regional branch.
- The classifier's rules overlapped: a regional/local branch of a
  foreign-headquartered group matched **both** `foreign_parent` and
  `regional_branch_only`, so the single top snippet decided which label won.

That is a fact that must never change run-to-run, so this is a real bug. The C5
Sonnet adjudicator (`lead_hq_sonnet_adjudicator` / `apply_c5_adjudication`) is a
later, opt-in "second opinion" that only judges whatever HQ fields the primary
step already produced — it never calls Serper, so it is not the source of the
instability (in the 14:14 run it merely enriched the already-`foreign_parent`
row with `c5_parent_company="FUJIFILM Holdings Corporation"`, Tokyo/Japan).

A second, related failure mode: **AEG Power Solutions** (`aegps.com`) was scored
**0.0 in BOTH** prior runs — the single Serper query never surfaced the German
parent at all, so a genuinely foreign-owned company was silently missed.

## The fix

### Step 1 — Firecrawl the company's own domain as the PRIMARY HQ source

Before the classifier runs, the company's own website (homepage + a few
about/company-profile-style pages, redirects followed) is crawled via Firecrawl
and fed to the classifier as the **first, most-trusted** source; the Serper
snippets become secondary corroboration. Own-domain page text is stable across
runs, which removes the instability.

- **New `lead_hq_firecrawl_source.py`** — `collect_own_domain_hq_pages()` reuses
  `deep_dive_runner._firecrawl_scrape_page` (same v1 scrape REST call, same
  hard-failure-vs-404 distinction, same `usage_tracker` audit) rather than
  copying it. Candidate paths: `("", "/about", "/about-us", "/company",
  "/company-profile", "/en/about")`, capped at 3 pages.
- **`lead_hq_ai_interpreter.py`** — `interpret_hq_with_ai(..., crawled_pages=…)`
  and `_build_user_message` present the crawled own-domain content as the
  `PRIMARY SOURCE` block, Serper as `SECONDARY SOURCE`. The system/user prompt
  now:
  - treats own-website content as most authoritative and prefers it on conflict;
  - forbids inventing information not present in *either* source;
  - disambiguates the overlapping rule: being a local/regional branch of a
    group whose ultimate parent HQ is in a **different** country is
    `foreign_parent`, not `regional_branch_only` — `regional_branch_only` is now
    reserved for when the ultimate parent HQ is in the *same* country as the
    input country, or genuinely undeterminable.
  - Evidence-URL validation was extended (`_known_urls_for_hq`) so the AI can
    cite a crawled own-domain URL (e.g. `https://www.aegps.com/en/contact/`) as
    evidence — but still can never invent a URL that was in neither source.
- **`lead_prioritizer_core.py`** — crawls the own domain when a
  `firecrawl_api_key` is present and the input `domain` is not a hosted careers
  platform, then passes `crawled_pages` to the interpreter. **No key, a hard
  Firecrawl failure, or a hosted-platform domain falls back to exactly today's
  Serper-only behavior** — a missing key is never an error (mirrors the Deep
  Dive precedent). Wired through `run_batch_dataframe` (`firecrawl_api_key` was
  already threaded for Deep Dive), the parallel runner, the CLI
  (`load_firecrawl_key`), and the Streamlit app.

### Step 2 — new always-shown `hq_location_summary` field

A single, structured HQ-location line, ADDITIONAL to and independent of the
`foreign_ownership_or_group_structure` driver's Strong/Moderate/Weak/Not-evidenced
badge (that badge's logic is untouched). English base form (localized for NL/IT
in the export, mirroring `parent_hq_summary_app`), two fixed prefixes so the
frontend relies on presence/absence and a stable prefix, never on free-text
parsing:

- Foreign parent found → `"Parent company headquarters: Tokyo, Japan"`
  → NL `"Hoofdkantoor moederbedrijf: Tokio, Japan"` / IT
  `"Sede centrale della capogruppo: Tokyo, Giappone"`
- Domestic HQ evidenced → `"Headquarters: Amsterdam, Netherlands"`
  → NL `"Hoofdkantoor: Amsterdam, Nederland"`
- Neither → field absent/`null` (never a guess or placeholder).

**Field-priority fallback chain** (`lead_hq_location_summary.build_hq_location_summary`,
mirroring `resolve_parent_hq_country_for_export`): for a foreign parent the
city/country are `c5_parent_hq_{country,city}` → `ai_parent_hq_{country,city}`
→ `hq_detected_{country,city}` (first non-blank wins); for a domestic HQ the
`hq_detected_{country,city}` are used. Which line is chosen is driven by the
factual structure (`hq_structure_type` / `foreign_hq_simple`), NOT by the driver
badge or score. Built in `prioritize_single_lead` from the AI/detected fields,
then **recomputed in `apply_c5_adjudication`** once the richer `c5_parent_*`
fields exist so C5 takes priority. Added to `LeadPrioritizationResult`
(`lead_output_schema.py`), the Excel `_RESULT_FLAT_FIELDS`, and the Lovable JSON
per-company detail record (top-level, next to `parent_hq_summary_app`).

## Before / after evidence (live runs, real keys)

Each company run **twice back-to-back** with Firecrawl enabled; outcomes now
stable across both runs.

**FUJIFILM Manufacturing Europe B.V. (`fujifilmtilburg.nl`)**
- Before: flipped 3.0 ↔ 0.0 (see table above).
- After — both runs identical classification/score:
  `foreign_parent` (High), score **3.0**, parent `Fujifilm Holdings Corporation`
  Tokyo/Japan, evidence on the crawled own domain
  (`https://www.fujifilm.com/ef/en/about/company-profile` /
  `.../about/office`), `hq_location_summary = "Parent company headquarters:
  Tokyo, Japan"`. (The specific own-domain page cited varies slightly, but the
  classification and score no longer do — that is the fix.)

**AEG Power Solutions (`aegps.com`)**
- Before: **0.0 in both** prior Serper-only runs (foreign parent missed).
- After — both runs identical: `foreign_parent` (High), score **3.0**, parent
  `AEG Power Solutions GmbH`, HQ Warstein-Belecke, Germany, evidence on the
  crawled own domain `https://www.aegps.com/en/contact/`
  ("...identifies AEG Power Solutions GmbH as headquartered in Warstein-Belecke,
  Germany..."), `hq_location_summary = "Parent company headquarters:
  Warstein-Belecke, Germany"`.

**Regression — Samsung Climate Solutions (`samsung-climatesolutions.com`)**
Stably foreign (3.0) in both prior exports; still `foreign_parent` (High), score
3.0, parent `Samsung Electronics`, Seoul, South Korea,
`hq_location_summary = "Parent company headquarters: Seoul, South Korea"` — not
destabilized by this change.

**Domestic (no foreign parent)**
- Coolblue (`coolblue.nl`): `domestic`, score 0.0,
  `hq_location_summary = "Headquarters: Rotterdam, Netherlands"`.
- NS International (`nsinternational.com`): `domestic`, score 0.0,
  `hq_location_summary = "Headquarters: Amsterdam, Netherlands"`.
Blank/`null` `hq_location_summary` is emitted (not a guess) for `unclear` /
`regional_branch_only` rows with no confirmed location — see the unit tests.

## Files changed

New: `lead_hq_firecrawl_source.py`, `lead_hq_location_summary.py`,
`test_lead_hq_firecrawl_source.py`, `test_lead_hq_location_summary.py`.
Modified: `lead_hq_ai_interpreter.py`, `lead_prioritizer_core.py`,
`lead_output_schema.py`, `lead_prioritizer_batch_core.py`,
`lovable_content_localization.py`, `export_lead_prioritizer_to_lovable_json.py`,
`test_lead_prioritizer_ai_hq.py`, `test_lead_prioritizer_batch_core.py`,
`test_export_lead_prioritizer_to_lovable_json.py`.

## Reproducing a fresh export

With `SERPER_API_KEY` / `ANTHROPIC_API_KEY` / `FIRECRAWL_API_KEY` in
`.streamlit/secrets.toml` (the Firecrawl key alone activates the own-domain HQ
crawl — no extra flag), for a one-row-per-company input xlsx with `company_name`
and `domain` columns:

```bash
python lead_prioritizer_batch_cli.py \
  --input your_leads.xlsx --company-column company_name --domain-column domain \
  --default-country Netherlands --mode full --secrets-file .streamlit/secrets.toml

python export_lead_prioritizer_to_lovable_json.py \
  --input-xlsx <output>.xlsx --output-dir lovable_json_exports/Netherlands/<stamp> \
  --country Netherlands --cold-callers "Jantje,Pietje"
```

The per-company detail record then carries a top-level `hq_location_summary`
(localized to Dutch for a Netherlands export), and the FUJIFILM/AEG rows come
out `foreign_parent` / score 3.0 with the parent HQ grounded on their own
crawled domain — identically on repeat runs.

## Tests / pre-existing failures

`pytest -q`: **1396 passed**. The only failures (7) are pre-existing and
unrelated to this work — confirmed by re-running them with this change stashed
(identical failures): Windows path-separator assertions in
`test_lead_prioritizer_batch_app.py` / `test_lead_prioritizer_lovable_json_export_app.py`
/ `test_lead_prioritizer_batch_cli.py::TestHelpers::test_output_path_generation`,
and a `usage_kind` fake-signature drift in
`test_lead_icp_context_composer.py::TestCollectIcpContextEvidence` (that module
is untouched here). This change introduces **zero** new failures.
