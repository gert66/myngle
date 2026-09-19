"""Targeted, read-only classification evidence: a deterministic stratified
sampling plan over creation cohorts/waves/bursts, cached batch-read of
extended source/lifecycle/owner/activity/mYngle properties, and cached
association evidence (company-contact, company-deal, contact-deal) -- all
via ``hubspot_client.ReadOnlyHubSpotClient`` (or an injected test double
with the same shape).

Design goals, matching the rest of this package:

* **Never re-fetch the whole universe.** ``build_sampling_plan`` reads the
  compact per-record feature table already written by
  ``cohorts.build_cohort_analysis`` (``work/<type>_features.csv.gz``) and
  samples a bounded number of ids per stratum, never the full population.
* **Deterministic and explainable.** The same ``seed`` always produces the
  same plan; every stratum records its population size, sample size,
  method, and an explicit uncertainty note, so nothing here is presented
  as more confident than a sample warrants.
* **Resumable and idempotent.** Every live lookup goes through a
  JSONL, append-only, id-keyed on-disk cache under
  ``<output_dir>/cache/evidence/`` -- a second run against the same
  output-dir and snapshot performs zero API calls for ids already fetched.
* **Offline-safe by default.** With no token (or ``--offline``), the full
  sampling plan and property resolution are still computed and written;
  ``evidence.json["status"]`` is ``"skipped_offline"`` and gaps/next_actions
  explain exactly what a live run would fetch.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from .hubspot_client import ReadOnlyHubSpotClient
from .properties import resolve_properties
from .snapshot import Snapshot, read_jsonl

DEFAULT_FULL_FETCH_THRESHOLD = 200
DEFAULT_SAMPLE_SIZE = 200
DEFAULT_SEED = 20260918
DEFAULT_RECENT_ACTIVITY_DAYS = 180
DEFAULT_TOP_BURST_MINUTES = 10
# ``None`` means "cover the full sampling plan": the effective cap is
# computed from the plan itself (object ids + implied association lookups)
# before any fetch is attempted -- see ``run_evidence``'s ``lookup_budget``.
DEFAULT_MAX_LOOKUPS = None
DEFAULT_LOOKUP_RPS = 3.0
DEFAULT_TOKEN_ENV = "HUBSPOT_PRIVATE_APP_TOKEN"
BATCH_SIZE = 100
CHECKPOINT_EVERY_BATCHES = 10

SUPPORTED_SAMPLING_TYPES = ("companies", "contacts")
ASSOCIATION_PAIRS = (
    ("companies", "contacts"),
    ("companies", "deals"),
    ("contacts", "deals"),
)
LAST_SALES_ACTIVITY_FIELD = "hs_last_sales_activity_timestamp"
RECENCY_BUCKETS = ("<=30d", "<=90d", "<=365d", ">365d", "unknown")

# Extra per-object-type recency fields summarized alongside
# ``hs_last_sales_activity_timestamp``, gated on the field actually being in
# ``properties_requested`` for that object type (never assumed present).
EXTRA_RECENCY_FIELDS = {
    "companies": ["notes_last_updated", "notes_last_contacted"],
    "contacts": [
        "notes_last_updated",
        "notes_last_contacted",
        "hs_email_last_send_date",
        "hs_email_last_open_date",
    ],
}

# Association pairs whose cached "to" entries are summarized per stratum,
# keyed by object type of the sampled ("from") record.
ASSOCIATION_PAIRS_BY_OBJECT_TYPE = {
    "companies": [("companies_contacts", "companies_contacts"), ("companies_deals", "companies_deals")],
    "contacts": [("contacts_deals", "contacts_deals")],
}


# -- small utilities --------------------------------------------------------
def _parse_iso(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp_path, path)


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _recency_bucket(dt: Optional[datetime], reference_time: datetime) -> str:
    if dt is None:
        return "unknown"
    delta_days = max(0.0, (reference_time - dt).total_seconds() / 86400.0)
    if delta_days <= 30:
        return "<=30d"
    if delta_days <= 90:
        return "<=90d"
    if delta_days <= 365:
        return "<=365d"
    return ">365d"


def _margin_note(sample_n: int, population_n: int, method: str) -> str:
    if population_n == 0:
        return "Empty stratum: no lookups planned."
    if method == "full_fetch":
        return f"Full population fetched for this stratum (n={population_n}): no sampling uncertainty."
    if sample_n <= 0:
        return "Empty stratum: no lookups planned."
    half_width_points = 1.96 * math.sqrt(0.25 / sample_n) * 100
    return (
        f"Uniform random sample of {sample_n} of {population_n} (seeded): the 95% "
        f"confidence-interval half-width for an observed proportion in this stratum "
        f"is at most ~{half_width_points:.0f} points."
    )


# -- property resolution ------------------------------------------------------
def resolve_all_properties(snapshot: Snapshot, object_types: Iterable[str], *, custom_cap: int) -> dict:
    return {object_type: resolve_properties(snapshot, object_type, custom_cap=custom_cap) for object_type in object_types}


# -- sampling plan ------------------------------------------------------------
def _read_feature_rows(output_dir: str, object_type: str):
    path = os.path.join(output_dir, "work", f"{object_type}_features.csv.gz")
    if not os.path.exists(path):
        return
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            yield row


def build_sampling_plan(
    cohort_analysis: dict,
    output_dir: str,
    object_types: Iterable[str] = SUPPORTED_SAMPLING_TYPES,
    *,
    seed: int = DEFAULT_SEED,
    full_fetch_threshold: int = DEFAULT_FULL_FETCH_THRESHOLD,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    recent_activity_days: int = DEFAULT_RECENT_ACTIVITY_DAYS,
    top_burst_minutes: int = DEFAULT_TOP_BURST_MINUTES,
) -> dict:
    """Build a deterministic, seedable stratified sampling plan per object
    type from the cohort feature table. Every id may belong to several
    strata; the per-type ``unique_ids`` list is the deduplicated union of
    every stratum's sampled ids -- the actual fetch list."""
    plan = {}
    for object_type in object_types:
        analysis = cohort_analysis.get(object_type)
        if not analysis:
            continue
        reference_time = _parse_iso(analysis.get("reference_time")) or datetime.now(timezone.utc)
        recent_cutoff = reference_time - timedelta(days=recent_activity_days)

        stratum_keys = [("by_month", month_key) for month_key in analysis.get("by_month", {})]
        stratum_keys += [("wave", wave["wave_id"]) for wave in analysis.get("waves", [])]
        top_bursts = sorted(
            analysis.get("timestamp_bursts", {}).get("minutes", []),
            key=lambda row: row["count"],
            reverse=True,
        )[:top_burst_minutes]
        burst_minute_set = {row["minute"] for row in top_bursts}
        stratum_keys += [("burst_minute", minute) for minute in sorted(burst_minute_set)]
        stratum_keys.append(("recent_activity", "recent_activity"))

        members: dict = {key: [] for key in stratum_keys}
        for row in _read_feature_rows(output_dir, object_type):
            record_id = row["id"]
            month_key = row.get("cohort_month") or "unknown"
            if ("by_month", month_key) in members:
                members[("by_month", month_key)].append(record_id)
            wave_id = row.get("wave_id") or ""
            if wave_id and ("wave", wave_id) in members:
                members[("wave", wave_id)].append(record_id)
            minute_key = (row.get("createdate_iso") or "")[:16]
            if minute_key in burst_minute_set:
                members[("burst_minute", minute_key)].append(record_id)
            modified_dt = _parse_iso(row.get("last_modified_iso"))
            if modified_dt is not None and modified_dt >= recent_cutoff:
                members[("recent_activity", "recent_activity")].append(record_id)

        rng = random.Random(seed)
        strata = []
        unique_ids: set = set()
        for kind, key in sorted(stratum_keys):
            ids = sorted(set(members.get((kind, key), [])))
            population_size = len(ids)
            if population_size <= full_fetch_threshold:
                sample_ids = list(ids)
                method = "full_fetch"
            else:
                sample_ids = sorted(rng.sample(ids, min(sample_size, population_size)))
                method = "uniform_random_sample"
            unique_ids.update(sample_ids)
            strata.append(
                {
                    "stratum_id": f"{object_type}:{kind}:{key}",
                    "kind": kind,
                    "key": key,
                    "population_size": population_size,
                    "sample_size": len(sample_ids),
                    "method": method,
                    "uncertainty_note": _margin_note(len(sample_ids), population_size, method),
                    "sample_ids": sample_ids,
                }
            )

        plan[object_type] = {
            "reference_time": analysis.get("reference_time"),
            "seed": seed,
            "full_fetch_threshold": full_fetch_threshold,
            "sample_size": sample_size,
            "recent_activity_days": recent_activity_days,
            "strata": strata,
            "unique_ids": sorted(unique_ids),
            "total_unique_ids": len(unique_ids),
        }
    return plan


def _serialize_plan(plan: dict) -> dict:
    """Drop the per-stratum ``sample_ids`` (kept only for the in-run fetch
    loop and the ``work/`` transparency file) before persisting to
    evidence.json, so the JSON artifact stays a bounded summary."""
    out = {}
    for object_type, type_plan in plan.items():
        out[object_type] = {
            key: value for key, value in type_plan.items() if key != "strata"
        }
        out[object_type]["strata"] = [
            {k: v for k, v in stratum.items() if k != "sample_ids"} for stratum in type_plan["strata"]
        ]
    return out


# -- disk caches --------------------------------------------------------------
class ObjectCache:
    """Resumable disk cache for batch-read object records, keyed by object
    id. Backed by an append-only JSONL file; the full file is read once at
    startup to rebuild the in-memory index (the "resume index"), so a
    second run against the same output-dir never refetches a cached id."""

    def __init__(self, cache_dir: str, object_type: str):
        os.makedirs(cache_dir, exist_ok=True)
        self.path = os.path.join(cache_dir, f"{object_type}.jsonl")
        self._records: dict = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._records[obj["id"]] = obj

    def has(self, record_id: str) -> bool:
        return record_id in self._records

    def get(self, record_id: str):
        return self._records.get(record_id)

    def put_many(self, records: Iterable[dict]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record))
                fh.write("\n")
                self._records[record["id"]] = record

    def __len__(self) -> int:
        return len(self._records)


class AssociationCache:
    """Resumable disk cache for association lookups, keyed by the "from"
    object id, one file per (from_type, to_type) pair."""

    def __init__(self, cache_dir: str, from_type: str, to_type: str):
        os.makedirs(cache_dir, exist_ok=True)
        self.path = os.path.join(cache_dir, f"assoc_{from_type}_{to_type}.jsonl")
        self._records: dict = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._records[obj["from_id"]] = obj

    def has(self, from_id: str) -> bool:
        return from_id in self._records

    def get(self, from_id: str):
        return self._records.get(from_id)

    def put_many(self, records: Iterable[dict]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record))
                fh.write("\n")
                self._records[record["from_id"]] = record

    def __len__(self) -> int:
        return len(self._records)


class RateLimiter:
    """Simple fixed-interval throttle: sleeps as needed so calls to
    ``wait()`` never happen faster than ``rps`` times per second. Counts
    how many times it actually had to sleep, for the progress counters."""

    def __init__(self, rps: float):
        self.min_interval = (1.0 / rps) if rps and rps > 0 else 0.0
        self._last_call: Optional[float] = None
        self.waits = 0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if self._last_call is not None:
            remaining = self.min_interval - (now - self._last_call)
            if remaining > 0:
                self.waits += 1
                time.sleep(remaining)
        self._last_call = time.monotonic()


def _http_status(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


# -- lookups --------------------------------------------------------------
def fetch_object_records(
    client,
    object_type: str,
    ids,
    properties,
    cache: ObjectCache,
    rate_limiter: RateLimiter,
    counters: dict,
    max_new_lookups: Optional[int],
    gaps: Optional[list] = None,
    checkpoint_cb: Optional[Callable[[], None]] = None,
) -> dict:
    """Batch-read ``ids`` (<=100 per HubSpot call) via ``client``. Ids
    already present in ``cache`` are never refetched. Returns
    id -> {"id", "properties"} for every id found (cached or freshly
    fetched).

    ``counters[f"ids_new_fetched.{object_type}"]`` counts only ids actually
    sent to HubSpot in *this* call (consumed against ``max_new_lookups`` /
    the run's overall lookup budget); cache hits never consume it. The
    caller is expected to set ``counters[f"ids_fetched.{object_type}"]`` to
    ``len(result)`` afterwards -- the *resolved* total (cache hits + new).

    On a batch-read failure, a gap line naming ``object_type``, the HTTP
    status (when available), and the number of ids affected is appended to
    ``gaps``. On the first 401/403 for this object type,
    ``counters[f"object_denied.{object_type}"]`` is set to the status code
    and no further batch-reads are attempted for this call (mirroring the
    association 401/403 skip-once behaviour)."""
    if gaps is None:
        gaps = []
    new_fetched_key = f"ids_new_fetched.{object_type}"
    counters.setdefault(new_fetched_key, 0)

    result = {}
    to_fetch = []
    for record_id in ids:
        if cache.has(record_id):
            result[record_id] = cache.get(record_id)
            counters["cache_hits"] = counters.get("cache_hits", 0) + 1
        else:
            to_fetch.append(record_id)

    ids_sent = 0
    batch_count = 0
    for batch in _chunks(to_fetch, BATCH_SIZE):
        if max_new_lookups is not None:
            remaining = max_new_lookups - ids_sent
            if remaining <= 0:
                break
            if len(batch) > remaining:
                batch = batch[:remaining]
        ids_sent += len(batch)
        counters[new_fetched_key] = counters.get(new_fetched_key, 0) + len(batch)
        rate_limiter.wait()
        counters["rate_limit_waits"] = rate_limiter.waits
        counters["api_calls"] = counters.get("api_calls", 0) + 1
        try:
            response = client.batch_read(
                object_type, {"inputs": [{"id": rid} for rid in batch], "properties": list(properties)}
            )
        except Exception as exc:  # a single failed batch must not abort the run
            status_code = _http_status(exc)
            counters["errors"] = counters.get("errors", 0) + 1
            gaps.append(
                f"Batch-read for '{object_type}' failed (HTTP "
                f"{status_code if status_code is not None else 'unknown'}); "
                f"{len(batch)} id(s) affected and not fetched this run."
            )
            if status_code in (401, 403):
                counters[f"object_denied.{object_type}"] = status_code
                break
            continue
        new_records = []
        returned_ids = set()
        for item in response.get("results", []):
            record = {"id": item.get("id"), "properties": item.get("properties", {})}
            result[record["id"]] = record
            new_records.append(record)
            returned_ids.add(record["id"])
        # ids HubSpot did not return (deleted/merged since the snapshot) are
        # cached as explicit not_found markers so a resume never refetches them.
        for record_id in batch:
            if record_id not in returned_ids:
                marker = {"id": record_id, "not_found": True, "properties": {}}
                result[record_id] = marker
                new_records.append(marker)
        cache.put_many(new_records)
        batch_count += 1
        if checkpoint_cb and batch_count % CHECKPOINT_EVERY_BATCHES == 0:
            checkpoint_cb()

    if checkpoint_cb:
        checkpoint_cb()
    return result


def fetch_associations(
    client,
    from_type: str,
    to_type: str,
    from_ids,
    cache: AssociationCache,
    rate_limiter: RateLimiter,
    counters: dict,
    max_new_lookups: Optional[int],
    gaps: list,
    checkpoint_cb: Optional[Callable[[], None]] = None,
) -> dict:
    """Batch-read associations for ``from_ids`` (<=100 per call). A 403/404
    from the association endpoint (e.g. deals inaccessible) is recorded in
    ``gaps`` and the pair is skipped -- never aborts the run.

    ``counters[f"associations_new_fetched.{pair_key}"]`` counts only ids
    actually sent to HubSpot in *this* call (consumed against
    ``max_new_lookups`` / the run's overall lookup budget); cache hits
    never consume it. ``counters[f"associations_fetched.{pair_key}"]`` is
    the *resolved* total (cache hits + new), set once from ``len(result)``
    before returning. On the first 401/403 for this pair,
    ``counters[f"association_denied.{pair_key}"]`` is set to the status
    code."""
    pair_key = f"{from_type}_{to_type}"
    new_fetched_key = f"associations_new_fetched.{pair_key}"
    counters.setdefault(new_fetched_key, 0)

    result = {}
    to_fetch = []
    for from_id in from_ids:
        if cache.has(from_id):
            result[from_id] = cache.get(from_id)
            counters["cache_hits"] = counters.get("cache_hits", 0) + 1
        else:
            to_fetch.append(from_id)

    ids_sent = 0
    batch_count = 0
    for batch in _chunks(to_fetch, BATCH_SIZE):
        if max_new_lookups is not None:
            remaining = max_new_lookups - ids_sent
            if remaining <= 0:
                break
            if len(batch) > remaining:
                batch = batch[:remaining]
        ids_sent += len(batch)
        counters[new_fetched_key] = counters.get(new_fetched_key, 0) + len(batch)
        rate_limiter.wait()
        counters["rate_limit_waits"] = rate_limiter.waits
        counters["api_calls"] = counters.get("api_calls", 0) + 1
        try:
            response = client.associations_batch_read(from_type, to_type, batch)
        except Exception as exc:
            status_code = _http_status(exc)
            if status_code in (401, 403, 404):
                # The pair is inaccessible for this run (e.g. deals not granted in
                # the private-app scope): record exactly one gap and stop hitting
                # this endpoint for the remaining batches, rather than re-raising
                # the same error once per 100-id batch.
                gaps.append(
                    f"Association lookup {from_type}->{to_type} is inaccessible "
                    f"(HTTP {status_code}); pair skipped for the remainder of this run."
                )
                counters[f"association_denied.{pair_key}"] = status_code
                break
            counters["errors"] = counters.get("errors", 0) + 1
            gaps.append(f"Association lookup {from_type}->{to_type} failed: {exc}")
            continue
        by_from = defaultdict(list)
        for item in response.get("results", []):
            from_id = (item.get("from") or {}).get("id")
            by_from[from_id].extend(item.get("to", []))
        new_records = []
        for from_id in batch:
            record = {"from_id": from_id, "to": by_from.get(from_id, [])}
            result[from_id] = record
            new_records.append(record)
        cache.put_many(new_records)
        batch_count += 1
        if checkpoint_cb and batch_count % CHECKPOINT_EVERY_BATCHES == 0:
            checkpoint_cb()

    counters[f"associations_fetched.{pair_key}"] = len(result)
    if checkpoint_cb:
        checkpoint_cb()
    return result


# -- owners -----------------------------------------------------------------
def _load_owner_names(snapshot: Snapshot) -> dict:
    owners_path = os.path.join(snapshot.raw_dir, "owners.jsonl")
    names = {}
    for envelope in read_jsonl(owners_path):
        record = envelope.get("record", envelope)
        owner_id = record.get("id")
        if not owner_id:
            continue
        display = " ".join(part for part in (record.get("firstName"), record.get("lastName")) if part)
        names[owner_id] = display or f"owner:{owner_id}"
    return names


# -- aggregation --------------------------------------------------------------
def _association_type_summary(pair_results: dict, record_ids: Iterable[str]) -> dict:
    """Summarize association-type labels/counts from cached ``to`` entries
    for the given ``record_ids`` (the "from" side of one association pair)."""
    label_counts: Counter = Counter()
    ids_with_any = 0
    for record_id in record_ids:
        to_entries = (pair_results.get(record_id) or {}).get("to") or []
        if to_entries:
            ids_with_any += 1
        for entry in to_entries:
            types = entry.get("associationTypes") or []
            if not types:
                label_counts["(unlabeled)"] += 1
                continue
            for assoc_type in types:
                label = assoc_type.get("label") or assoc_type.get("typeId") or "(unlabeled)"
                label_counts[str(label)] += 1
    return {"label_counts": dict(label_counts.most_common(10)), "ids_with_any": ids_with_any}


def aggregate_stratum(
    object_type: str,
    stratum: dict,
    fetched_records: dict,
    association_results: dict,
    owner_names: dict,
    reference_time: datetime,
    custom_property_names: Iterable[str],
    properties_requested: Iterable[str] = (),
) -> dict:
    custom_property_names = list(custom_property_names)
    properties_requested = set(properties_requested)
    sample_ids = stratum["sample_ids"]
    fetched_all = [fetched_records[rid] for rid in sample_ids if rid in fetched_records]
    not_found_count = sum(1 for r in fetched_all if r.get("not_found"))
    fetched = [r for r in fetched_all if not r.get("not_found")]
    n = len(fetched)

    source_counts: Counter = Counter()
    detail1_counts: Counter = Counter()
    lifecycle_counts: Counter = Counter()
    owner_present = 0
    owner_counts: Counter = Counter()
    activity_recency: Counter = Counter()
    extra_recency_counters = {
        field: Counter() for field in EXTRA_RECENCY_FIELDS.get(object_type, []) if field in properties_requested
    }
    merged_present = 0
    custom_present_total = 0
    has_contacts = has_deals = has_company = 0

    companies_contacts = association_results.get("companies_contacts", {})
    companies_deals = association_results.get("companies_deals", {})
    contacts_deals = association_results.get("contacts_deals", {})

    for record in fetched:
        props = record.get("properties") or {}
        source = props.get("hs_object_source")
        if source:
            source_counts[source] += 1
        detail1 = props.get("hs_object_source_detail_1")
        if detail1:
            detail1_counts[detail1] += 1
        lifecycle_counts[props.get("lifecyclestage") or "unknown"] += 1
        owner_id = props.get("hubspot_owner_id")
        if owner_id:
            owner_present += 1
            owner_counts[owner_names.get(owner_id, owner_id)] += 1
        if props.get("hs_merged_object_ids"):
            merged_present += 1
        activity_dt = _parse_iso(props.get(LAST_SALES_ACTIVITY_FIELD))
        activity_recency[_recency_bucket(activity_dt, reference_time)] += 1
        for field, counter in extra_recency_counters.items():
            counter[_recency_bucket(_parse_iso(props.get(field)), reference_time)] += 1
        custom_present_total += sum(1 for name in custom_property_names if props.get(name))

        record_id = record.get("id")
        if object_type == "companies":
            if (companies_contacts.get(record_id) or {}).get("to"):
                has_contacts += 1
            if (companies_deals.get(record_id) or {}).get("to"):
                has_deals += 1
        elif object_type == "contacts":
            if props.get("associatedcompanyid"):
                has_company += 1
            if (contacts_deals.get(record_id) or {}).get("to"):
                has_deals += 1

    association_presence = (
        {"has_contacts_rate": (has_contacts / n) if n else None, "has_deals_rate": (has_deals / n) if n else None}
        if object_type == "companies"
        else {"has_company_rate": (has_company / n) if n else None, "has_deals_rate": (has_deals / n) if n else None}
    )

    activity_recency_by_field = {}
    for field in EXTRA_RECENCY_FIELDS.get(object_type, []):
        if field not in properties_requested:
            activity_recency_by_field[field] = "unavailable"
            continue
        counter = extra_recency_counters[field]
        activity_recency_by_field[field] = {bucket: counter.get(bucket, 0) for bucket in RECENCY_BUCKETS}

    fetched_ids = [r["id"] for r in fetched]
    association_types = {
        summary_key: _association_type_summary(association_results.get(pair_key, {}), fetched_ids)
        for summary_key, pair_key in ASSOCIATION_PAIRS_BY_OBJECT_TYPE.get(object_type, [])
    }

    return {
        "stratum_id": stratum["stratum_id"],
        "kind": stratum["kind"],
        "key": stratum["key"],
        "population_size": stratum["population_size"],
        "sample_size": stratum["sample_size"],
        "fetched_count": n,
        "not_found_count": not_found_count,
        "method": stratum["method"],
        "uncertainty_note": stratum["uncertainty_note"],
        "source_distribution": dict(source_counts.most_common(10)),
        "top_import_detail_1": dict(detail1_counts.most_common(10)),
        "lifecyclestage_distribution": dict(lifecycle_counts),
        "owner_presence_rate": (owner_present / n) if n else None,
        "top_owners": [name for name, _ in owner_counts.most_common(5)],
        "association_presence": association_presence,
        "association_types": association_types,
        "activity_recency": {bucket: activity_recency.get(bucket, 0) for bucket in RECENCY_BUCKETS},
        "activity_recency_by_field": activity_recency_by_field,
        "merged_object_presence_rate": (merged_present / n) if n else None,
        "custom_field_presence_rate": (
            (custom_present_total / (n * len(custom_property_names)))
            if n and custom_property_names
            else None
        ),
    }


def _write_evidence_csv(
    output_dir: str,
    object_type: str,
    strata: list,
    fetched_records: dict,
    association_results: dict,
    custom_property_names: Iterable[str] = (),
) -> None:
    path = os.path.join(output_dir, "work", f"{object_type}_evidence.csv.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    custom_property_names = list(custom_property_names)
    id_to_strata: dict = defaultdict(list)
    for stratum in strata:
        for record_id in stratum["sample_ids"]:
            id_to_strata[record_id].append(stratum["stratum_id"])

    companies_contacts = association_results.get("companies_contacts", {})
    companies_deals = association_results.get("companies_deals", {})
    contacts_deals = association_results.get("contacts_deals", {})

    with gzip.open(path, "wt", encoding="utf-8", newline="") as gz:
        writer = csv.writer(gz)
        writer.writerow(
            [
                "id",
                "strata",
                "hs_object_source",
                "hs_object_source_detail_1",
                "lifecyclestage",
                "hubspot_owner_id",
                "has_contacts",
                "has_company",
                "has_deals",
                "hs_last_sales_activity_timestamp",
                "notes_last_updated",
                "notes_last_contacted",
                "custom_field_present_count",
                "hs_merged_object_ids_present",
            ]
        )
        for record_id, record in sorted(fetched_records.items()):
            props = record.get("properties") or {}
            if object_type == "companies":
                has_contacts = int(bool((companies_contacts.get(record_id) or {}).get("to")))
                has_company = ""
                has_deals = int(bool((companies_deals.get(record_id) or {}).get("to")))
            else:
                has_contacts = ""
                has_company = int(bool(props.get("associatedcompanyid")))
                has_deals = int(bool((contacts_deals.get(record_id) or {}).get("to")))
            writer.writerow(
                [
                    record_id,
                    ";".join(id_to_strata.get(record_id, [])),
                    props.get("hs_object_source") or "",
                    props.get("hs_object_source_detail_1") or "",
                    props.get("lifecyclestage") or "",
                    props.get("hubspot_owner_id") or "",
                    has_contacts,
                    has_company,
                    has_deals,
                    props.get(LAST_SALES_ACTIVITY_FIELD) or "",
                    props.get("notes_last_updated") or "",
                    props.get("notes_last_contacted") or "",
                    sum(1 for name in custom_property_names if props.get(name) not in (None, "")),
                    int(bool(props.get("hs_merged_object_ids"))),
                ]
            )


# -- orchestration --------------------------------------------------------------
def run_evidence(
    snapshot: Snapshot,
    cohort_analysis: dict,
    output_dir: str,
    *,
    token_env: str = DEFAULT_TOKEN_ENV,
    offline: bool = False,
    max_lookups: Optional[int] = DEFAULT_MAX_LOOKUPS,
    lookup_rps: float = DEFAULT_LOOKUP_RPS,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    full_fetch_threshold: int = DEFAULT_FULL_FETCH_THRESHOLD,
    seed: int = DEFAULT_SEED,
    custom_property_cap: int = 40,
    client=None,
    progress=None,
) -> dict:
    """Run the full targeted-evidence layer: property resolution, sampling
    plan, (if a client is available) cached batch-read + association
    lookups, aggregation, and the per-id evidence CSV. Returns a dict with
    keys ``evidence`` (the evidence.json payload), ``gaps`` and
    ``next_actions`` (lists to merge into the top-level artifacts).

    ``client`` may be injected (a ``ReadOnlyHubSpotClient`` or a
    test double with the same ``batch_read``/``associations_batch_read``
    shape) -- used by tests so no live HubSpot call is ever required to
    build or test this package. When ``client`` is None, a live client is
    only constructed if a token is present in ``token_env`` and
    ``offline`` is False.
    """
    object_types = [ot for ot in SUPPORTED_SAMPLING_TYPES if ot in cohort_analysis]
    properties_by_type = resolve_all_properties(snapshot, object_types, custom_cap=custom_property_cap)
    plan = build_sampling_plan(
        cohort_analysis,
        output_dir,
        object_types,
        seed=seed,
        full_fetch_threshold=full_fetch_threshold,
        sample_size=sample_size,
    )
    _write_json(os.path.join(output_dir, "work", "evidence_sampling_plan.json"), plan)

    gaps: list = []
    next_actions: list = []
    for object_type, resolved in properties_by_type.items():
        if resolved["properties_unavailable"]:
            gaps.append(
                f"'{object_type}': {len(resolved['properties_unavailable'])} requested evidence "
                f"propert(y/ies) not present in this snapshot's property definitions: "
                f"{', '.join(resolved['properties_unavailable'])}."
            )
        if resolved["custom_properties_overflow"]:
            gaps.append(
                f"'{object_type}': {len(resolved['custom_properties_overflow'])} discovered custom "
                f"(mYngle) propert(y/ies) exceeded the cap of {resolved['custom_property_cap']} and were "
                f"not requested: {', '.join(resolved['custom_properties_overflow'])}."
            )

    # The full plan's implied lookup total: every object id plus one
    # association lookup per sampled "from" id per pair. When --max-lookups
    # is None (the default), the effective cap covers this exactly.
    plan_total = sum(plan[ot]["total_unique_ids"] for ot in object_types)
    for from_type, to_type in ASSOCIATION_PAIRS:
        plan_total += len(plan.get(from_type, {}).get("unique_ids", []))

    if max_lookups is None:
        effective_cap: Optional[int] = plan_total
        truncated = False
    else:
        effective_cap = max_lookups
        truncated = max_lookups < plan_total
    lookup_budget = {
        "requested": max_lookups,
        "effective": effective_cap,
        "plan_total": plan_total,
        "truncated": truncated,
    }
    if truncated:
        gaps.append(
            f"--max-lookups={max_lookups} is smaller than the full sampling plan's {plan_total} "
            "planned lookup(s) (object ids + implied association lookups); some strata will have "
            "incomplete or absent evidence this run."
        )
        next_actions.append(
            f"Re-run with --max-lookups {plan_total} (or omit --max-lookups, which now defaults to "
            f"covering the full plan) to complete it; only {effective_cap} of {plan_total} planned "
            "lookups were budgeted for this run."
        )

    counters: dict = {"cache_hits": 0, "api_calls": 0, "errors": 0, "rate_limit_waits": 0}
    for object_type in object_types:
        counters[f"ids_planned.{object_type}"] = plan[object_type]["total_unique_ids"]
        counters[f"ids_fetched.{object_type}"] = 0
        counters[f"ids_new_fetched.{object_type}"] = 0
    for from_type, to_type in ASSOCIATION_PAIRS:
        counters[f"associations_fetched.{from_type}_{to_type}"] = 0
        counters[f"associations_new_fetched.{from_type}_{to_type}"] = 0

    def _publish_progress(phase: str) -> None:
        if not progress:
            return
        progress.set_subprocess_status("evidence", phase)
        for key, value in counters.items():
            progress.set_counter(f"evidence.{key}", value)

    token_present = bool(os.environ.get(token_env))
    use_client = client is not None or (token_present and not offline)

    if not use_client:
        reason = "explicit --offline" if offline else f"no token in env var {token_env!r}"
        gaps.append(
            f"Live evidence lookups were skipped ({reason}); the sampling plan below documents "
            "exactly what a live run would fetch (properties, ids per stratum, and associations) "
            "once a private-app token is available."
        )
        next_actions.append(
            f"Set a HubSpot private-app token in the env var named by --token-env (default "
            f"{DEFAULT_TOKEN_ENV!r}) and re-run without --offline to execute the batch-read and "
            "association lookups already planned in evidence.json['sampling_plan']."
        )
        _publish_progress("skipped_offline")
        evidence = {
            "schema_version": 1,
            "status": "skipped_offline",
            "properties": properties_by_type,
            "sampling_plan": _serialize_plan(plan),
            "strata_summaries": {},
            "observed_facts": [
                "properties.*.properties_requested / properties_unavailable (resolved offline "
                "against the snapshot's property definitions)",
                "sampling_plan.*.strata (population_size, sample_size, method, uncertainty_note; "
                "computed offline from the cohort feature table)",
            ],
            "inferred": [],
            "uncertainty": [
                "No live evidence was fetched in this run: every strata_summaries entry is absent, "
                "not zero -- source/lifecycle/owner/association/activity distributions remain unknown "
                "until a live run executes the plan above.",
            ],
            "inaccessible": list(gaps),
            "lookup_budget": lookup_budget,
        }
        return {"evidence": evidence, "gaps": gaps, "next_actions": next_actions, "counters": counters}

    if client is None:
        client = ReadOnlyHubSpotClient(token_env_var=token_env)

    cache_dir = os.path.join(output_dir, "cache", "evidence")
    rate_limiter = RateLimiter(lookup_rps)
    _publish_progress("running")

    def _remaining_cap() -> Optional[int]:
        if effective_cap is None:
            return None
        total_new = sum(v for k, v in counters.items() if k.startswith("ids_new_fetched.")) + sum(
            v for k, v in counters.items() if k.startswith("associations_new_fetched.")
        )
        return max(0, effective_cap - total_new)

    fetched_records: dict = {}
    for object_type in object_types:
        cache = ObjectCache(cache_dir, object_type)
        props = properties_by_type[object_type]["properties_requested"]
        ids = plan[object_type]["unique_ids"]
        records = fetch_object_records(
            client, object_type, ids, props, cache, rate_limiter, counters, _remaining_cap(),
            gaps=gaps, checkpoint_cb=lambda: _publish_progress("running"),
        )
        fetched_records[object_type] = records
        counters[f"ids_fetched.{object_type}"] = len(records)
        _publish_progress("running")

    association_results: dict = {}
    for from_type, to_type in ASSOCIATION_PAIRS:
        pair_key = f"{from_type}_{to_type}"
        from_ids = plan.get(from_type, {}).get("unique_ids", [])
        if not from_ids:
            association_results[pair_key] = {}
            continue
        cache = AssociationCache(cache_dir, from_type, to_type)
        results = fetch_associations(
            client, from_type, to_type, from_ids, cache, rate_limiter, counters, _remaining_cap(), gaps,
            checkpoint_cb=lambda: _publish_progress("running"),
        )
        association_results[pair_key] = results
        _publish_progress("running")

    owner_names = _load_owner_names(snapshot)
    strata_summaries: dict = {}
    uncertainty: list = []
    for object_type in object_types:
        analysis = cohort_analysis[object_type]
        reference_time = _parse_iso(analysis.get("reference_time")) or datetime.now(timezone.utc)
        custom_names = properties_by_type[object_type]["custom_properties_included"]
        requested_names = properties_by_type[object_type]["properties_requested"]
        summaries = [
            aggregate_stratum(
                object_type, stratum, fetched_records[object_type], association_results, owner_names,
                reference_time, custom_names, requested_names,
            )
            for stratum in plan[object_type]["strata"]
        ]
        strata_summaries[object_type] = summaries
        uncertainty.extend(f"{object_type}/{s['stratum_id']}: {s['uncertainty_note']}" for s in summaries)
        _write_evidence_csv(
            output_dir, object_type, plan[object_type]["strata"], fetched_records[object_type],
            association_results, custom_names,
        )

    denied_object_types = [ot for ot in object_types if f"object_denied.{ot}" in counters]
    denied_pairs = [
        f"{ft}_{tt}" for ft, tt in ASSOCIATION_PAIRS if f"association_denied.{ft}_{tt}" in counters
    ]
    for object_type in denied_object_types:
        uncertainty.append(
            f"'{object_type}': batch-read stopped early (HTTP {counters[f'object_denied.{object_type}']}); "
            "strata relying on unfetched ids for this type carry incomplete evidence, not zero evidence."
        )
    for pair_key in denied_pairs:
        uncertainty.append(
            f"association pair '{pair_key}': lookups stopped early "
            f"(HTTP {counters[f'association_denied.{pair_key}']}); affected strata's association-presence "
            "rates are based on partial data, not the full sample."
        )

    status = (
        "partial"
        if (denied_object_types or denied_pairs or counters.get("errors", 0) > 0 or truncated)
        else "completed"
    )

    _publish_progress(status)

    evidence = {
        "schema_version": 1,
        "status": status,
        "properties": properties_by_type,
        "sampling_plan": _serialize_plan(plan),
        "strata_summaries": strata_summaries,
        "lookup_budget": lookup_budget,
        "observed_facts": [
            "properties.*.properties_requested / properties_unavailable",
            "strata_summaries.*.source_distribution / top_import_detail_1 / lifecyclestage_distribution "
            "(directly read from fetched HubSpot batch-read responses)",
            "strata_summaries.*.activity_recency (from hs_last_sales_activity_timestamp, bucketed "
            "relative to the cohort analysis reference_time)",
        ],
        "inferred": [
            "strata_summaries.*.association_presence (has_contacts/has_company/has_deals rates -- "
            "sampled, not exhaustive)",
            "strata_summaries.*.top_owners (resolved display names from raw/owners.jsonl; no emails)",
            "strata_summaries.*.custom_field_presence_rate (mYngle-field fill rate within the sample)",
        ],
        "uncertainty": uncertainty,
        "inaccessible": list(gaps),
    }
    return {"evidence": evidence, "gaps": gaps, "next_actions": next_actions, "counters": counters}
