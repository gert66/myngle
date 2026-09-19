# hubspot_population_audit

A read-only HubSpot Population Audit: reconciles portal totals and unique
IDs, builds creation cohorts, characterizes cohorts with available
evidence, and classifies records into evidence-based buckets whose counts
sum exactly to the reconciled population -- all without ever writing to
HubSpot.

This batch (`pa-01-scaffold-reconcile`) implemented the package skeleton,
the snapshot loader, and independent portal-total/unique-ID reconciliation.
The `pa-02-cohorts-waves` batch added streaming creation-cohort analysis
(day/week/month), per-month cohort profiling, timestamp-burst detection,
and heuristic bulk-import wave detection. This batch
(`pa-03-targeted-evidence`) adds the targeted, read-only **evidence layer**
(`properties.py`, `evidence.py`): offline property resolution, a
deterministic stratified sampling plan, and -- only when a token is
available and `--offline` is not set -- cached batch-read and association
lookups (company-contact, company-deal, contact-deal) via
`ReadOnlyHubSpotClient`. Bucket classification (`population_map.json`)
remains an explicit `"status": "not_yet_implemented"` placeholder, to be
built in a follow-up batch that consumes `evidence.json`'s strata
summaries.

## Snapshot layout

The layout below is **verified read-only** against the live snapshot at
`/home/myngle/hubspot-audit-live-20260918-r2` (not inferred or assumed):

```
<snapshot_dir>/
  raw/
    companies.jsonl         one JSON envelope per line:
                             {"record": {"id": ..., "properties": {...}},
                              "extracted_at": ..., "page_index": ...}
                             Properties carry ONLY the default set: createdate,
                             domain, hs_lastmodifieddate, hs_object_id, name.
                             No associations, no hs_object_source, no
                             lifecyclestage, no hubspot_owner_id.
                             275478 envelopes; extraction was "resumed": true,
                             so duplicate envelopes are possible.
    contacts.jsonl           same envelope shape. Default properties only:
                             createdate, email, firstname, lastname,
                             hs_object_id, lastmodifieddate.
                             447150 envelopes; also "resumed": true.
    owners.jsonl             {"record": {...}, "extracted_at": ...}
    properties_companies.json / properties_contacts.json
                             {"properties": [...], "extracted_at": ...}
    _checkpoint.json         per-object pagination/completeness metadata.
  run_status.json            present, but carries NO "portal_totals" key.
```

**Confirmed absent from the live snapshot** (not "optional and unconfirmed"
-- actually checked and missing):

- `deals.jsonl` -- the deals extraction probe failed and `properties_deals`
  returned HTTP 403; there is no deal data anywhere in this snapshot, so no
  deal-based cohort, reconciliation, or association evidence can be built
  from it (`gaps.json`).
- `portal_totals.json` at the snapshot root or under `raw/` -- does not
  exist for any object type.
- Any activity object (`calls.jsonl`, `meetings.jsonl`, etc.) -- not
  present.

`snapshot.py` looks for portal totals in this priority order: root-level
`portal_totals.json`, `raw/portal_totals.json`, then
`run_status.json["portal_totals"]`. **None of these exist in the live
snapshot for any object type**, so every object type is reported as
unreconciled with an explicit note -- the loader never substitutes the raw
record count for the portal total, since that would make "reconciled"
trivially true by construction.

Because raw records carry only default properties, cohort characterization
built from the snapshot alone (`cohorts.py`) is limited to what those
fields can show: creation timing, last-modified recency, and domain/email
presence. It cannot see source/import properties, lifecycle stage, owner,
or associations -- those require targeted live lookups, planned for a
later batch.

## Creation cohorts and bulk-import wave detection (`cohorts.py`)

For each supported object type (companies, contacts -- deals are absent
from the live snapshot, see above), `cohorts.py` streams the snapshot in
two passes (never materializing the full record list) to build:

- **Creation cohorts** by day (`YYYY-MM-DD`), ISO week (`YYYY-Www`), and
  month (`YYYY-MM`). Records with a missing or unparsable `createdate` are
  counted under an explicit `unknown` cohort, never dropped -- so cohort
  totals always equal the unique-ID count from `reconcile.py`.
- **Per-month cohort profiles**: count, share of population,
  domain/email presence rate, name presence rate,
  untouched-since-creation rate (last-modified within 60s of createdate),
  last-modified recency buckets (`<=30d`, `<=90d`, `<=365d`, `>365d`,
  `unknown`) relative to a reference time (the max envelope
  `extracted_at` seen for that object type), top 10 domain/email suffixes,
  and (contacts only) freemail share against a small fixed provider list.
- **Timestamp bursts**: exact createdate-minute clusters with
  `>= burst_min_records` (default 50) records, reported as an observed
  fact -- this is a raw count, not an interpretation.
- **Heuristic wave detection** (explicitly labelled *inferred*, never a
  conclusion): a day is flagged when its count clears
  `max(wave_abs_min, wave_factor * baseline)`, where `baseline` is the
  median of non-zero daily counts over the preceding `baseline_window_days`
  (falling back to the overall non-zero-day median when fewer than
  `min_prior_nonzero_days` prior non-zero days exist). Flagged days within
  a 1-day gap of each other are merged into a single wave. Every wave
  entry carries an explicit `alternative_explanations` list (e.g. genuine
  campaign spike, integration sync/backfill, CSV import, enrichment tool
  bulk create) that a later classification batch must test before
  concluding anything -- this batch only detects and characterizes
  candidates. All parameters (`burst_min_records`, `baseline_window_days`,
  `wave_factor`, `wave_abs_min`, `min_prior_nonzero_days`) are keyword
  arguments to `cohorts.analyze_object_type` / `build_cohort_analysis`, are
  recorded verbatim in `cohort_analysis.json["parameters"]`, and default to
  the values above.
- A dedicated **`sep_2023_focus`** entry per object type: daily counts for
  September 2023, its share of the population, and whether it was detected
  as a wave -- since Sep 2023 is the specific anomaly this audit was
  commissioned to explain.

### Derived work product: per-record feature table

`cohorts.py` also streams a compact per-record feature table to
`<output_dir>/work/<object_type>_features.csv.gz` (columns: `id,
createdate_iso, cohort_month, cohort_day, has_domain_or_email, has_name,
untouched_since_creation, last_modified_iso, in_burst_minute, wave_id`),
written one gzip-compressed row at a time so memory stays bounded. This is
a **derived work product** for reuse by later batches (association/activity
evidence, classification) -- it is not part of the snapshot and is
regenerated on every run, never read from or written into the immutable
snapshot directory itself.

### Memory bound

Only small per-key counters (per day/week/month/minute, and per-month/
per-wave domain-suffix counters) and the set of unique record ids are held
in memory at once -- for the live snapshot, up to roughly 450k ints/strings
for the largest object type (contacts). No full record list is ever
materialized; see the module docstring in `cohorts.py` for the exact
two-pass streaming design.

## Evidence hierarchy

Every fact this audit produces is labelled with where it sits in this
hierarchy -- higher items are more directly observed, lower items are
increasingly inferred or sampled, and every artifact keeps them visibly
separate (`observed_facts` / `inferred` / `uncertainty` / `inaccessible`
in `cohort_analysis.json` and `evidence.json`, and matching sections in
`reports/index.html`):

1. **Observed snapshot facts** -- directly recomputed from the raw
   snapshot with no interpretation: raw record counts, unique-ID counts,
   duplicates, recorded portal totals and their deltas (`reconcile.py`);
   creation counts by day/week/month, per-month presence/recency profiles,
   and exact-minute timestamp bursts (`cohorts.py`); property availability
   resolved against `raw/properties_<object>.json` (`properties.py`).
2. **HubSpot source/lifecycle/owner properties** -- targeted, cached
   batch-read of `hs_object_source*`, `lifecyclestage`, `hubspot_owner_id`,
   and related fields for a sampled subset of each cohort/wave/burst
   stratum (`evidence.py`). This is still a directly-observed HubSpot
   value, one level below the snapshot because it requires a live,
   targeted lookup rather than being present in the immutable snapshot.
3. **Associations** -- company-contact, company-deal, and contact-deal
   association presence, fetched via `POST
   /crm/v4/associations/{from}/{to}/batch/read`. One level further removed:
   presence/absence of a link is an observed fact, but what it *means* for
   classification is inferred later.
4. **Activity recency** -- `hs_last_sales_activity_timestamp`,
   `notes_last_updated`, `notes_last_contacted` bucketed relative to the
   cohort analysis's `reference_time`. Recency is observed; "still active"
   is an interpretation left to the (not yet implemented) classification
   batch.
5. **Heuristics** -- bulk-import wave detection (`cohorts.py`) and any
   future classification bucket. Every heuristic output carries an
   explicit `alternative_explanations` list (wave detection) or will carry
   an explicit rationale (classification) -- never presented as a bare
   conclusion.

`uncertain` is a first-class bucket for the future classification batch,
not a fallback that silently drops ambiguous records. Any object type
without an independently recorded portal total, and any stratum sampled
rather than fully fetched, is surfaced explicitly (see "Stratified
sampling plan" below) rather than defaulted into a confident conclusion.

## Targeted evidence (`properties.py`, `evidence.py`)

This batch adds the targeted, read-only evidence layer that fills the gap
called out above ("raw records carry only default properties"):

### Property resolution (`properties.py`)

For each object type, a **fixed requested-property list** (source,
lifecycle, owner, activity, and mYngle-relevant fields -- see
`properties.REQUESTED_PROPERTIES`) is intersected with what the
snapshot's own `raw/properties_<object>.json` actually reports as
available; anything requested-but-absent is recorded in
`properties_unavailable`, never silently dropped or assumed present.
Separately, **custom ("mYngle") property candidates** are discovered by a
documented 3-condition rule (`properties.matched_custom_rules`): a property
counts as custom if `hubspotDefined` is explicitly `False` (rule
`hubspotDefined_false`), or `createdUserId` is set (rule `createdUserId`),
or its `groupName` doesn't start with the default
`companyinformation`/`contactinformation` group prefixes **and**
`hubspotDefined` is not explicitly `True` (rule `non_default_group`). That
last exclusion matters on a live portal: there are dozens of HubSpot-shipped
default property groups other than `companyinformation`/
`contactinformation` (`emailinformation`, `socialmediainformation`,
`web_analytics`, `conversioninformation`, ...) -- without excluding
`hubspotDefined == True`, rule 3 alone would flag all of them as "custom".

Discovered custom fields are capped at a configurable maximum (`custom_cap`,
default 40); anything beyond the cap is recorded in
`custom_properties_overflow` and surfaced as a gap, not silently truncated.
When the cap has to choose, fields matched by rule `hubspotDefined_false`
or `createdUserId` (a human-created or explicitly-non-stock property -- the
strongest signal) are included **ahead of** any field matched only by
`non_default_group`, so a plausible default-group false positive can never
displace a genuine mYngle field; within each tier, candidates are ordered
alphabetically for determinism. The rule(s) matched by every *included*
property are persisted in `custom_properties_rules` (e.g.
`{"myngle_signup_source": ["createdUserId"]}`).

### Stratified sampling plan (`evidence.build_sampling_plan`)

Rather than re-fetching the whole population, evidence is gathered from a
**deterministic, seeded, stratified sample**. Strata are: every `by_month`
cohort, every detected bulk-import wave, the top N exact-minute timestamp
bursts, and a `recent_activity` stratum (last-modified within the last 180
days, configurable, of the cohort analysis's `reference_time`). Per
stratum: populations at or below
`--full-fetch-threshold` (default 200) are fetched in full; larger
populations get a uniform random sample of `--sample-size` (default 200,
seeded by `--seed`, default `20260918` for reproducibility). Every stratum
records its population size, sample size, method, and an explicit
**uncertainty note** (for `n=200`, the 95% CI half-width for an observed
proportion is stated directly, ~7 points) -- so no distribution derived
from a sample is ever presented without its margin. Ids overlapping
multiple strata (e.g. a wave record is also in its `by_month` cohort) are
deduplicated into one fetch list per object type.

### Cached, resumable lookups

All live access goes through `hubspot_client.ReadOnlyHubSpotClient`
(`batch_read`, and the new `associations_batch_read` for
`/crm/v4/associations/{from}/{to}/batch/read`, both already covered by the
existing `/batch/read` allowlist). Every id/association lookup is cached
to an append-only, id-keyed JSONL file under
`<output_dir>/cache/evidence/` -- a second run against the same
`--output-dir` performs **zero** API calls for anything already cached.
Ids HubSpot's batch-read does not return in a given batch (e.g. deleted or
merged since the snapshot was taken) are cached as an explicit
`{"not_found": true, "properties": {}}` marker, so a resume never
re-requests them; per-stratum aggregation counts these in a `not_found`
count and excludes them from every distribution.

**`--max-lookups` counts only *new* lookups actually sent to HubSpot in
this run** (`ids_new_fetched.<type>` / `associations_new_fetched.<pair>`)
-- cache hits never consume the budget. `ids_fetched.<type>` /
`associations_fetched.<pair>` are the *resolved* totals (cache hits + new)
used for progress reporting; they are always `>=` the new-fetched
counters. This matters for resumability: a run capped below the full plan
size can be resumed indefinitely, and each resume makes forward progress
on the parts of the plan not yet cached, rather than being blocked by ids
it already resolved in a prior run. Batches are trimmed to the remaining
*new*-lookup budget so a run never overshoots it, and every attempted
batch-read/association call is counted in `api_calls` even if the call
itself raises. `--lookup-rps` throttles request rate.

`--max-lookups` defaults to `None`, meaning the effective cap covers the
**full sampling plan** (every object id plus every implied association
lookup, computed from the plan before any fetch is attempted) --
`evidence.json["lookup_budget"]` records `{"requested", "effective",
"plan_total", "truncated"}` so this is always explicit. Passing an
explicit `--max-lookups` smaller than `plan_total` sets `truncated: true`,
adds a gap line and a `next_actions` entry naming the exact `--max-lookups`
value that would complete the plan, and makes `evidence.json["status"]`
`"partial"`. Pass `--max-lookups 0` to perform no fetches at all (the
sampling plan and property resolution are still written in full).

On the **first** 401/403/404 from an association endpoint for a given
`(from_type, to_type)` pair (e.g. deals not in scope), exactly one gap is
recorded in `gaps.json`, `association_denied.<pair>` is set, and that pair
is skipped for the rest of the run -- later batches for the same pair are
never retried, so a large denied population produces one gap line, not one
per 100-id batch. The same skip-once behaviour applies to the **object**
batch-read endpoint itself: on the first 401/403 for a given object type, a
gap line is recorded (naming the type, the HTTP status, and the number of
ids affected), `object_denied.<type>` is set, and no further batch-reads
are attempted for that type this run. Whenever any object type or
association pair is denied, any batch-read error was counted, or the
lookup budget was truncated, `evidence.json["status"]` is `"partial"` (not
`"completed"`) and `uncertainty` names exactly which strata have
incomplete -- not zero -- evidence because of it.

### Per-stratum characterization

For every stratum, `evidence.aggregate_stratum` summarizes: the
`hs_object_source`/`hs_object_source_detail_1` distribution, lifecycle
stage distribution, owner presence and top owners (display names only, via
`raw/owners.jsonl` -- never emails), association presence rates
(`has_contacts`/`has_company`/`has_deals`) plus a per-pair
`association_types` breakdown of association-type labels and counts,
activity recency for `hs_last_sales_activity_timestamp`, and an additional
`activity_recency_by_field` recency-bucket breakdown for
`notes_last_updated`, `notes_last_contacted`, and (contacts only)
`hs_email_last_send_date`/`hs_email_last_open_date` -- each field is
bucketed only when it is actually present in `properties_requested` for
that object type; otherwise the summary reports `"unavailable"` for that
field rather than a misleading all-zero bucket.

### Offline-safe by default

With no token present in the env var named by `--token-env` (default
`HUBSPOT_PRIVATE_APP_TOKEN`), or with `--offline`/`--no-live-lookups`,
**no live call is attempted**: `evidence.json["status"]` is
`"skipped_offline"`, but the full property resolution and sampling plan
are still computed and written, and `gaps.json`/`next_actions.json`
explain exactly what a live run would fetch. This is also what every test
in this repository exercises -- no test ever sets a real token or talks to
the network; live-shaped tests inject
`fixtures/fake_client.FakeReadOnlyClient` instead.

## Read-only guarantee

`hubspot_client.py` exposes `get` (any path) and `post` (validated against
an **allowlist** -- only paths ending in `/search` or containing
`/batch/read` are permitted, which also covers the v4 associations
batch-read endpoint added in this batch; every other POST, and
`put`/`patch`/`delete` unconditionally, raise `WriteOperationBlocked`
*before* any HTTP request is constructed). Only `429` (rate limit) and
`5xx` (transient server error) responses are retried with backoff; any
other `4xx` (e.g. `401`/`403` for a missing scope) raises immediately on
the first attempt, so a denied endpoint costs exactly one request, not
`max_retries` pointless ones. Neither this batch's CLI path
nor its test suite ever makes a live HubSpot call: by default (no token,
no `--offline` flag needed) `evidence.py` falls back to the offline
sampling-plan-only path, and every test injects a fake client or exercises
pure functions with fixture data. No file in this package contains a
`requests.put(`, `requests.patch(`, or `requests.delete(` call (enforced
by `test_cli_end_to_end.py::NoWriteEndpointSourceScanTests`).

## Running (fixture snapshot)

```bash
python3 -m hubspot_population_audit.cli run \
  --snapshot hubspot_population_audit/fixtures/sample_snapshot \
  --output-dir /tmp/hubspot_population_audit_out
```

Writes `progress.json`, `population_map.json`, `cohort_analysis.json`,
`evidence.json`, `gaps.json`, `next_actions.json`, and
`reports/index.html` into `--output-dir`. Relevant flags added in this
batch:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--offline` / `--no-live-lookups` | off | Force the offline evidence path even if a token is present. |
| `--token-env` | `HUBSPOT_PRIVATE_APP_TOKEN` | Env var holding a private-app token for live lookups. |
| `--max-lookups` | `None` (covers the full plan) | Cap on total *new* live id/association lookups this run; cache hits never count against it. `0` performs no fetches. See `evidence.json["lookup_budget"]`. |
| `--lookup-rps` | 3.0 | Max live lookup requests per second. |
| `--sample-size` | 200 | Per-stratum sample size above the full-fetch threshold. |
| `--full-fetch-threshold` | 200 | Strata at or below this population are fetched in full. |
| `--seed` | 20260918 | Seed for the deterministic stratified sampling plan. |

With no token in the environment (the default in this repo and in CI),
the run above takes the offline evidence path automatically -- no flag is
required to guarantee no live call is made.

## Tests

```bash
python3 -m unittest discover -s hubspot_population_audit/tests
```

Covers: snapshot loading and streaming, portal-total/unique-ID
reconciliation (exact match, mismatch, and "not recorded" cases), the
read-only client's write-blocking guarantee including the new
`associations_batch_read` allowlist coverage (mocked HTTP, no network),
the on-disk lookup cache, atomic `progress.json` writes, an end-to-end CLI
fixture run producing every required artifact, (`test_cohorts.py`) cohort
totals/unknown-createdate handling, Sep-2023 wave detection and its
suppression at higher thresholds, a moderate spike that must *not* be
flagged, timestamp-burst detection, hand-computed presence/untouched
rates, duplicate-envelope dedup, and the feature table's row count and
`wave_id` assignment against a deterministic synthetic snapshot built by
`fixtures/cohort_snapshot_builder.py`; and (`test_properties.py`,
`test_evidence.py`) the custom-field rule's three conditions and its cap
and overflow, `properties_unavailable` reporting, deterministic seeded
sampling (full-fetch vs. sampled, seed stability, a different seed
changing the sample, overlap dedup, an uncertainty note on every stratum),
disk-cache resume (a second run against the same cache performs zero API
calls, for both object and association lookups), the read-only invariant
(every call the fake client records is `POST` to an allowlisted path),
403 handling on the companies-deals association endpoint and on the
contacts object batch-read endpoint (both recorded as a gap plus a
skip-once `*_denied.*` counter, run continues with `status: "partial"`),
the cap-accounting invariant that cache hits never consume `--max-lookups`
(a pre-populated companies cache plus a tiny cap still reaches the
contacts and companies-contacts association lookups; a second full run's
new-fetched counters are all zero while its resolved counters equal the
plan size), the `lookup_budget` semantics (`--max-lookups None` covers the
full plan exactly; an explicit smaller cap sets `truncated: true` plus a
`next_actions` hint naming the exact cap needed), the custom-property
cap-tiering fix (rule-1/2 matches always survive a small cap ahead of any
rule-3-only match; `hubspotDefined == True` default-group properties are
never flagged custom), the non-retryable-4xx client behaviour (a mocked
403 results in exactly one request), the offline path (no token /
`--offline` still writes a full `evidence.json` with `status:
"skipped_offline"`), `--max-lookups 0` still performing zero fetches, and
an end-to-end CLI run with `fixtures/fake_client.FakeReadOnlyClient`
injected producing every required artifact including the per-id evidence
CSVs -- all against `fixtures/sample_snapshot` and
`fixtures/evidence_responses/*.json`, never the network.

## Known gaps (this batch)

- The classification buckets (`population_map.json`) are not implemented;
  `population_map.json["status"]` remains `"not_yet_implemented"`.
  `evidence.json`'s strata summaries are the intended input for that
  future batch.
- `deals.jsonl` is absent from the live snapshot (probe failed,
  `properties_deals` returned 403); no deal-based cohort or reconciliation
  exists in the snapshot itself. Deal *association* evidence
  (company-deal, contact-deal) is attempted via the live API where a token
  is present, but any 403/404 there is recorded as a gap, not treated as a
  crash.
- No independently recorded portal total exists for any object type in the
  live snapshot; every object type is reported as unreconciled.
- Raw records carry only default properties (no `hs_object_source`,
  `lifecyclestage`, `hubspot_owner_id`, or associations); the targeted
  evidence layer in this batch exists specifically to fill that gap via
  sampled, live, cached lookups -- but only when a token is present and
  `--offline` is not set.
- Evidence distributions (source, lifecycle, owner, association presence,
  activity recency) are computed from a **sample**, not a census, for any
  stratum above `--full-fetch-threshold`; every such stratum's uncertainty
  note states the resulting margin explicitly.
- Bulk-import wave candidates are a heuristic signal, not a conclusion --
  each wave's `alternative_explanations` must still be tested against
  evidence from a later batch before any record is classified.
- No live HubSpot call is made or attempted while building or testing this
  batch (no token is ever set in this repo's test environment); the live
  lookup code path exists and is unit-tested against a fake client, but is
  only exercised for real by a separate, explicitly authorized run.
