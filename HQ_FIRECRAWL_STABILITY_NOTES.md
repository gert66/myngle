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

---

# Addendum: "Confirmed domestic" driver badge (closing the domestic-evidence gap)

## The gap that was found

The Step-1/Step-2 work above added `hq_location_summary` as an always-shown,
informational field -- for a domestic company like Coolblue it correctly
showed "Headquarters: Rotterdam, Netherlands". But the
`foreign_ownership_or_group_structure` **driver card** in
`commercial_fit_drivers` (the thing an account manager actually scans) still
showed `strength: "Not evidenced"` with the generic `_NOT_EVIDENCED_NOTE`
("No reliable company-specific evidence found in the current sources.") for
that same row. That's misleading: there IS company-specific evidence, it just
confirms the OPPOSITE of foreign ownership. `detect_foreign_hq_for_export()`
only ever detected the foreign case; `_foreign_hq_driver()` returned `None`
for every non-foreign row with zero distinction between "genuinely nothing
either way" and "confirmed NOT foreign, real evidence exists";
`build_fixed_commercial_fit_drivers()`'s `foreign_hq` branch then fell
through to the same generic "Not evidenced" block for both cases.

## The fix

**New `strength` value: "Confirmed domestic"** -- a genuinely new badge string
the Lovable frontend does **not know today**. Whoever wires up the frontend
needs to add a badge style for it (it must not be silently mapped onto an
existing Strong/Moderate/Weak/Not evidenced/Rejected style, and it
deliberately does not reuse `_NOT_EVIDENCED_NOTE`, since that case has zero
evidence and this one has real evidence).

- `export_lead_prioritizer_to_lovable_json.py` -- new
  `detect_confirmed_domestic_hq_for_export(row, foreign_hq_detected)`,
  parallel to `detect_foreign_hq_for_export()`. Fires only when ALL of:
  1. `foreign_hq_detected` is False -- mutually exclusive with the existing
     foreign badge by construction (verified by a live test with
     contradictory row fields -- foreign wins, "Confirmed domestic" never
     appears alongside it).
  2. `hq_structure_type == "domestic"` -- the AI HQ interpreter's own factual
     structure determination (`lead_hq_ai_interpreter.interpret_hq_with_ai`),
     not a re-derivation of driver-badge logic.
  3. `hq_confidence` is High or Medium. This is a deliberately stricter bar
     than `hq_location_summary` itself uses -- that field populates the
     domestic line purely from `hq_structure_type == "domestic"` plus a
     resolved city/country, with no confidence gate at all. A "Confirmed"
     claim in a driver card is a stronger statement to the account manager
     than a quiet informational summary line, so a Low-confidence domestic
     call (which upstream already sets `needs_manual_review=True`, see
     `interpret_hq_with_ai`) never earns the driver badge, even though
     `hq_location_summary` may still show the softer summary line for that
     same row. This is the one place the two fields may legitimately differ
     in presence -- documented explicitly in the code comment above
     `_CONFIRMED_DOMESTIC_MIN_CONFIDENCE`.
  4. A real local HQ city/country actually resolves via
     `lead_hq_location_summary.build_hq_location_summary_from_row(row)` --
     the exact same C5 > AI > detected priority chain `hq_location_summary`
     itself uses (reused, not re-implemented), so whenever both fields ARE
     present for a row they can never disagree on content -- same function,
     same row, same answer. Verified by
     `test_location_matches_hq_location_summary_chain` and
     `test_driver_location_text_agrees_with_hq_location_summary`.
  A row with no HQ evidence in either direction (`hq_structure_type` blank)
  still returns `(False, None)` and keeps the plain "Not evidenced" card --
  confirmed-domestic is never a default for "just not foreign."

- `build_fixed_commercial_fit_drivers()` gained `confirmed_domestic` /
  `domestic_location_text` / `domestic_evidence_urls` parameters. The
  `foreign_hq` branch now has three outcomes instead of two: `hq_driver`
  present -> "Strong" (unchanged); else `confirmed_domestic` -> new
  "Confirmed domestic" card with evidence "{city}, {country} is the
  confirmed local headquarters; no foreign parent identified." and
  `evidence_source_url`/`evidence_sources` via the existing
  `build_evidence_sources` helper (own-domain URLs -- including a
  Firecrawl-crawled own-domain page from the Step-1 work above -- are
  already preferred first by that helper, so no extra "own domain" wiring
  was needed); else the original generic "Not evidenced" (unchanged).
- The evidence URL(s) reuse the row's own `hq_evidence_url`/`hq_evidence_urls`
  -- the exact same fields the foreign-side driver already draws from -- so a
  domestic classification's own-domain evidence page surfaces identically to
  how a foreign classification's does.

## Buying-signal isolation (verified, not just asserted)

"Confirmed domestic" was **not** added to `_POSITIVE_DRIVER_STRENGTHS =
frozenset({"Strong", "Moderate", "Weak"})` -- confirmed by
`test_confirmed_domestic_not_in_positive_driver_strengths`. Every other place
in the file that branches on `foreign_hq_detected`
(`build_visible_icp_signal_scores`'s `add_foreign_hq_row`,
`build_curated_why_relevant`, `build_curated_what_is_hot` /
`build_curated_what_is_hot_items`, `apply_composed_driver_evidence`) was
checked: none of them gained a domestic branch, so a confirmed-domestic row's
`foreign_hq_detected` stays False straight through and none of those
functions ever add a positive foreign-HQ sentence for it -- verified live and
in `test_confirmed_domestic_never_positive_in_why_relevant_or_what_is_hot`
(no "foreign" substring in `why_relevant`, no "foreign"/"confirmed local
headquarters" bullet in `what_is_hot`). "Confirmed domestic" only ever
surfaces as the driver-card state itself, exactly as required -- it is
neutral/negative information for a sales pitch, not a buying-signal trigger.

## Italy-path decision (explicit)

**Out of scope for Italy.** Italy's `content_language == "Italian"` branch
uses a completely separate, frozen driver-building function
(`build_commercial_fit_drivers`, not `build_fixed_commercial_fit_drivers`)
that derives its `foreign_hq` driver entirely from whatever row already
exists in `visible_icp_signal_scores` -- and `build_visible_icp_signal_scores`
only ever adds a foreign-HQ row when foreign is actually detected
(`add_foreign_hq_row`). For a non-foreign row, Italy's driver list omits the
foreign_hq dimension entirely rather than showing "Not evidenced" (unlike the
fixed six-dimension non-Italy path). Since the Italy path is explicitly
documented elsewhere in this file as frozen/byte-for-byte and does not even
have a placeholder slot for an un-detected foreign_hq dimension, adding
"Confirmed domestic" there would require restructuring code that multiple
other tasks have deliberately left untouched. Decision: "Confirmed domestic"
is non-Italy only (English/Dutch/any future country using the fixed
six-driver path) -- Italy exports are unaffected by this addendum.

## Before / after (live re-verification, real keys)

Regenerated a fresh in-memory export for four Netherlands companies in one
run (FUJIFILM + AEG to re-confirm no regression on the prior fix, Coolblue +
NS International for the new badge):

| Company | hq_location_summary | Driver strength | Driver evidence | evidence_source_url |
|---|---|---|---|---|
| FUJIFILM Manufacturing Europe B.V. | Parent company headquarters: Tokyo, Japan | Strong (unchanged) | "Foreign headquarters or group structure detected." | https://www.fujifilm.com/ef/en/about/office (own domain) |
| AEG Power Solutions | Parent company headquarters: Warstein-Belecke, Germany | Strong (unchanged) | "Foreign headquarters or group structure detected." | https://www.aegps.com/en/contact/ (own domain) |
| Coolblue | Headquarters: Rotterdam, Netherlands | Before: Not evidenced -> After: Confirmed domestic | "Rotterdam, Netherlands is the confirmed local headquarters; no foreign parent identified." | https://www.coolblue.nl/... (own domain) |
| NS International | Headquarters: Amsterdam, Netherlands | Before: Not evidenced -> After: Confirmed domestic | "Amsterdam, Netherlands is the confirmed local headquarters; no foreign parent identified." | https://www.nsinternational.com/en/agents/contact-agent/contact (own domain) |

FUJIFILM and AEG are unaffected by this addendum (foreign path untouched);
the hq_location_summary city/country for Coolblue and NS International
matches the driver card's evidence text exactly, as required.

## Files changed (this addendum)

Modified only: `export_lead_prioritizer_to_lovable_json.py`,
`test_export_lead_prioritizer_to_lovable_json.py`. No changes to
`lead_hq_location_summary.py`, `lead_hq_ai_interpreter.py`, or any
HQ-detection logic -- this addendum is export-side classification only,
exactly like `detect_foreign_hq_for_export` already was.

## Tests

21 new tests in `test_export_lead_prioritizer_to_lovable_json.py`
(`TestDetectConfirmedDomesticHqForExportUnit`, `TestConfirmedDomesticDriverCard`)
covering: fires only with High/Medium confidence + real local-HQ evidence;
never fires when foreign is detected (mutual exclusivity, including a
contradictory-fields case); low confidence and structure-type mismatches
stay "Not evidenced"; a genuinely blank row stays "Not evidenced" with no
hq_location_summary; the driver's location text always agrees with
hq_location_summary's own C5>AI>detected chain; "Confirmed domestic" is
absent from `_POSITIVE_DRIVER_STRENGTHS`; and it never appears as a positive
signal in why_relevant/what_is_hot. `pytest -q`: 1411 passed, the same 7
pre-existing unrelated failures as the prior addendum (Windows
path-separator assertions and the usage_kind composer-test signature drift)
plus zero new failures -- the flaky
`TestContentLanguageEnglishUnchanged::test_explicit_english_matches_omitted_default`
timestamp test (documented in HANDOFF.md) surfaced once during a full-suite
run and passed cleanly on immediate re-run, as expected.
