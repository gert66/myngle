# hubspot_population_audit

A read-only HubSpot Population Audit: reconciles portal totals and unique
IDs, builds creation cohorts, characterizes cohorts with available
evidence, and classifies records into evidence-based buckets whose counts
sum exactly to the reconciled population -- all without ever writing to
HubSpot.

This batch (`pa-01-scaffold-reconcile`) implements the package skeleton,
the snapshot loader, and independent portal-total/unique-ID reconciliation.
Cohort detection, association/activity evidence, and classification are
explicit `"status": "not_yet_implemented"` placeholders, to be built in
follow-up batches.

## Snapshot layout

**Important caveat first:** this batch was built in a sandboxed session
whose filesystem access was restricted to this repository checkout
(`/home/myngle/hubspot-audit-build-repo`); the actual live snapshot at
`/home/myngle/hubspot-audit-live-20260918-r2` could not be listed or read
from this session, so its layout was not directly inspected here. That is
a build-environment limitation, not a design choice -- the loader below
must be validated against the real snapshot directory before any live
population audit runs (tracked in `gaps.json` / `next_actions.json`).

The layout the loader assumes is inferred from `hubspot_audit/extraction.py`
in this same repository, which is the extractor that produces exactly this
directory shape (and whose naming convention -- `raw/`, `_checkpoint.json`
-- strongly matches the `hubspot-audit-live-*` snapshot naming). That
module documents, and this package assumes:

```
<snapshot_dir>/
  raw/
    companies.jsonl        one JSON envelope per line:
                            {"record": {"id": ..., "properties": {...},
                             "associations": {...}}, "extracted_at": ...,
                             "page_index": ...}
    contacts.jsonl          same envelope shape
    deals.jsonl             same envelope shape
    calls.jsonl / meetings.jsonl / emails.jsonl / notes.jsonl / tasks.jsonl
                            activity objects, same envelope shape (optional --
                            only companies/contacts/deals are required for
                            population reconciliation)
    owners.jsonl            {"record": {...}, "extracted_at": ...}
    properties_<object>.json   {"properties": [...], "extracted_at": ...}
    _checkpoint.json        per-object {"next_cursor", "record_count", "pages",
                             "updated_at", "complete"} -- written after every
                             page during extraction, so a resumed/partial
                             extraction is distinguishable from a complete one
  portal_totals.json         OPTIONAL, independently recorded HubSpot-reported
                              totals per object type, e.g. {"companies": 1234,
                              "contacts": 5678}. This is the reconciliation
                              baseline. `hubspot_audit`'s own extractor does
                              not produce this file today (it paginates GET
                              /crm/v3/objects/<type> to exhaustion rather than
                              calling a search/count endpoint), so its presence
                              in the live snapshot is unconfirmed.
  run_status.json            OPTIONAL; if present and it carries a
                              "portal_totals" dict, that is used as a fallback
                              source for the same data.
```

`snapshot.py` looks for portal totals in this priority order: root-level
`portal_totals.json`, `raw/portal_totals.json`, then
`run_status.json["portal_totals"]`. **If none of these exist for an object
type, that object type is reported as unreconciled with an explicit note**
-- the loader never substitutes the raw record count for the portal total,
since that would make "reconciled" trivially true by construction.

### Open question for the live run

Confirm, before running against the real snapshot: (a) whether
`portal_totals.json` (or an equivalent) actually exists in
`hubspot-audit-live-20260918-r2`, and if not, what did record the
HubSpot-side total independently (e.g. a `/crm/v3/objects/<type>/search`
call with an empty filter, which returns a `total` field) so one can be
added without a live call in this build; (b) whether the live extraction
used the exact `raw/<object>.jsonl` envelope shape above, or something
adjacent (e.g. `_checkpoint.json` naming, additional object types).

## Evidence hierarchy

1. **Observed facts** -- directly recomputed from the raw snapshot with no
   interpretation: raw record counts, unique-ID counts, duplicates,
   recorded portal totals and their deltas. This batch implements this
   layer only (`reconcile.py`).
2. **Inferred classifications** -- population buckets derived from
   observed facts plus heuristics (source/import properties, lifecycle,
   owner, domain/email presence, recency, association evidence). Not yet
   implemented; every classification will carry an explicit rationale and
   confidence, and remain reversible (traceable back to the observed facts
   it was derived from).
3. **Uncertainty** -- any object type without an independently recorded
   portal total, any record whose classification evidence is ambiguous or
   conflicting, is surfaced explicitly rather than defaulted into a
   confident bucket. `uncertain` is a first-class bucket, not a fallback
   that gets silently dropped.
4. **Inaccessible data** -- capability gaps (e.g. an association type or
   activity object HubSpot doesn't expose to this scope) are recorded in
   `gaps.json`, never silently absent from the output.

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
on-disk lookup cache, atomic `progress.json` writes, and an end-to-end CLI
fixture run producing every required artifact.

## Known gaps (this batch)

- Live snapshot layout unverified against the real directory (see above).
- Cohort detection (creation waves by day/week/month, Sep 2023 anomaly),
  association evidence, activity/recency evidence, and the classification
  buckets are not implemented; `population_map.json`, `cohort_analysis.json`,
  and parts of `evidence.json` are structurally valid
  `"status": "not_yet_implemented"` placeholders.
- No live HubSpot call is made or attempted in this batch.
