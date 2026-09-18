# hubspot_population_audit

A read-only HubSpot Population Audit: reconciles portal totals and unique
IDs, builds creation cohorts, characterizes cohorts with available
evidence, and classifies records into evidence-based buckets whose counts
sum exactly to the reconciled population -- all without ever writing to
HubSpot.

This batch (`pa-01-scaffold-reconcile`) implemented the package skeleton,
the snapshot loader, and independent portal-total/unique-ID reconciliation.
This batch (`pa-02-cohorts-waves`) adds streaming creation-cohort analysis
(day/week/month), per-month cohort profiling, timestamp-burst detection,
and heuristic bulk-import wave detection. Association/activity evidence and
classification remain explicit `"status": "not_yet_implemented"`
placeholders, to be built in follow-up batches.

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

1. **Observed facts** -- directly recomputed from the raw snapshot with no
   interpretation: raw record counts, unique-ID counts, duplicates,
   recorded portal totals and their deltas (`reconcile.py`); creation
   counts by day/week/month, per-month presence/recency profiles, and
   exact-minute timestamp bursts (`cohorts.py`).
2. **Inferred classifications** -- population buckets and bulk-import wave
   candidates derived from observed facts plus heuristics. This batch adds
   heuristic wave detection (`cohorts.py`) -- every wave is explicitly
   labelled inferred and carries an `alternative_explanations` list that
   must be tested, never treated as a conclusion. Bucket classification
   (source/import properties, lifecycle, owner, association evidence) is
   not yet implemented; every future classification will carry an explicit
   rationale and confidence, and remain reversible (traceable back to the
   observed facts it was derived from).
3. **Uncertainty** -- any object type without an independently recorded
   portal total, any record whose classification evidence is ambiguous or
   conflicting, is surfaced explicitly rather than defaulted into a
   confident bucket. `uncertain` is a first-class bucket, not a fallback
   that gets silently dropped.
4. **Inaccessible data** -- capability gaps (deals absent from the
   snapshot, no recorded portal total, default-properties-only records
   limiting cohort characterization to presence/recency/domain signals;
   see "Snapshot layout" above) are recorded in `gaps.json`, never
   silently absent from the output.

## Read-only guarantee

`hubspot_client.py` exposes `get` (any path) and `post` (validated against
an **allowlist** -- only paths ending in `/search` or containing
`/batch/read` are permitted; every other POST, and `put`/`patch`/`delete`
unconditionally, raise `WriteOperationBlocked` *before* any HTTP request is
constructed). No live call is made anywhere in this batch's CLI path.

## Running (fixture snapshot)

```bash
python3 -m hubspot_population_audit.cli run \
  --snapshot hubspot_population_audit/fixtures/sample_snapshot \
  --output-dir /tmp/hubspot_population_audit_out
```

Writes `progress.json`, `population_map.json`, `cohort_analysis.json`,
`evidence.json`, `gaps.json`, `next_actions.json`, and
`reports/index.html` into `--output-dir`. `--no-live-lookups` is accepted
for forward interface compatibility with the batch that adds targeted live
lookups; no live lookup exists yet, so it has no effect today.

## Tests

```bash
python3 -m unittest discover -s hubspot_population_audit/tests
```

Covers: snapshot loading and streaming, portal-total/unique-ID
reconciliation (exact match, mismatch, and "not recorded" cases), the
read-only client's write-blocking guarantee (mocked HTTP, no network), the
on-disk lookup cache, atomic `progress.json` writes, an end-to-end CLI
fixture run producing every required artifact, and (`test_cohorts.py`)
cohort totals/unknown-createdate handling, Sep-2023 wave detection and its
suppression at higher thresholds, a moderate spike that must *not* be
flagged, timestamp-burst detection, hand-computed presence/untouched
rates, duplicate-envelope dedup, and the feature table's row count and
`wave_id` assignment -- against a deterministic synthetic snapshot built
by `fixtures/cohort_snapshot_builder.py` (generated at test time, not
checked into the repo as static data, to keep it small).

## Known gaps (this batch)

- Association evidence, activity/recency evidence, and the classification
  buckets are not implemented; `population_map.json` and parts of
  `evidence.json` are structurally valid `"status": "not_yet_implemented"`
  placeholders.
- `deals.jsonl` is absent from the live snapshot (probe failed,
  `properties_deals` returned 403); no deal-based evidence exists anywhere
  in this package.
- No independently recorded portal total exists for any object type in the
  live snapshot; every object type is reported as unreconciled.
- Raw records carry only default properties (no `hs_object_source`,
  `lifecyclestage`, `hubspot_owner_id`, or associations); cohort
  characterization from the snapshot alone is limited to
  presence/recency/domain signals.
- Bulk-import wave candidates are a heuristic signal, not a conclusion --
  each wave's `alternative_explanations` must still be tested against
  evidence from a later batch before any record is classified.
- No live HubSpot call is made or attempted in this batch.
