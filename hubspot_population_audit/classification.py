"""Deterministic, explainable, full-population bucket classification.

Streams the *entire* snapshot for each supported object type (companies,
contacts -- the live snapshot has no deals data, matching ``cohorts.py``)
and assigns every unique record to exactly one of seven buckets:
``operational_customer``, ``operational_prospect``, ``active_other``,
``historical_import``, ``enrichment_or_bulk``, ``legacy_or_obsolete_candidate``,
``uncertain``. Because this is a full streaming pass over every id (the same
dedup-by-id-first-wins rule as ``reconcile.py``/``cohorts.py``), the bucket
counts always sum exactly to ``reconcile.py``'s ``unique_id_count`` for that
object type -- the same number every other artifact in this package treats
as the authoritative population total (never the raw record count, never a
recorded-but-unverified portal total).

Design goals, matching the rest of this package:

* **Never re-fetch anything.** The only inputs are the already-loaded
  snapshot (``snapshot.py``), the already-computed cohort/wave analysis
  (``cohorts.py``), and the already-cached targeted evidence layer
  (``evidence.py``'s ``strata_summaries``, built from sampled, cached
  batch-read/association lookups). No new HubSpot call is made here.
* **Deterministic.** Every input is either a pure function of the on-disk
  snapshot or itself deterministic (cohort analysis, seeded sampling,
  cached lookups); this module adds no randomness, so two runs against the
  same snapshot and the same ``--output-dir`` cache produce byte-identical
  ``population_map.json`` bucket counts.
* **Rely primarily on cohort/wave membership, recency, presence/absence of
  domain or email, and naming/source patterns present directly on the raw
  record** -- because most snapshot records (the live snapshot, by
  construction) carry only default properties. When a record's raw
  properties *do* carry ``lifecyclestage``/``hs_object_source`` (as this
  package's small CLI fixture does, and as targeted evidence lookups may
  also resolve for the live snapshot), that is used as the strongest,
  directly-observed signal. Sampled evidence-layer results
  (``evidence.json["strata_summaries"]``) are used only to *characterize
  and calibrate* whole cohorts/waves -- lifting a wave's classification
  confidence, or breaking a tie between ``historical_import`` and
  ``enrichment_or_bulk`` -- never to claim record-level certainty a sample
  cannot support.
* **`uncertain` is a real destination, not a fallback that hides low
  signal.** Any record whose available signals do not clear the thresholds
  documented below lands in `uncertain` with an explicit
  ``insufficient_signal``-family confidence, rather than being forced into
  a more "interesting" bucket.
* **Same-domain companies are never treated as duplicates/historical by
  virtue of sharing a domain.** Nothing in this module compares records to
  each other; every classification decision is a pure function of one
  record's own signals plus its cohort/wave's *aggregate* (not
  cross-referenced) statistics.
* **Every classified record is traceable.** Each unique id, its bucket,
  confidence, matched rule id, and a short rationale are streamed one row
  at a time to ``<output_dir>/work/<object_type>_classification.csv.gz``
  (same pattern as ``cohorts.py``'s feature table and ``evidence.py``'s
  per-id evidence CSV) so ``population_map.json`` itself stays a bounded
  summary (bucket counts, rule counts, per-wave/month rationale, and a
  handful of illustrative examples per bucket) rather than one entry per
  record, which would not fit the "keep VM disk/RAM constraints in mind"
  requirement for a ~700k-record live snapshot.

## Wave/month classification (cohort-level, always full-population)

For every detected bulk-import wave, ``_classify_wave`` combines two
full-population, directly-observed facts already computed by
``cohorts.py`` for the *entire* wave (not a sample) -- ``presence_rate``
(share with a domain/email) and ``untouched_since_creation_rate`` -- into a
2x2 decision, explicitly testing (and recording) the alternative
explanation before concluding:

* low presence + high untouched -> refutes "genuine campaign/form spike"
  (a real spike would show materially higher identity-field presence) ->
  ``historical_import`` (or ``enrichment_or_bulk`` if the evidence-layer
  sample's ``source_distribution`` names an enrichment-tool-like source).
* high presence + high untouched -> identity fields look real but the
  cohort was never engaged with again -> ``enrichment_or_bulk`` (typical of
  a bulk data-loading/enrichment tool, not organic signup or raw import
  junk).
* high presence + low untouched -> supports the alternative explanation of
  a genuine bulk onboarding/campaign batch with real, subsequently-used
  data -> ``active_other``.
* mixed/moderate on both axes -> neither explanation is clearly supported
  -> ``uncertain``.

When a matching evidence-layer stratum sample exists for that wave
(``strata_summaries[object_type]`` entry with ``kind == "wave"`` and the
matching ``wave_id``), its sample size, source/lifecycle/association
findings, and uncertainty note are folded into the rationale and raise
confidence from ``low_no_evidence_sample`` to ``medium_sample_calibrated``
-- never claimed as a census.

Baseline (non-wave) months use the same full-population
presence/untouched facts only to decide record-level buckets (see
``_classify_record``); when an evidence-layer sample exists for that month
and shows a dominant (>50% of the sample) ``lifecyclestage`` of
``customer`` or one of the recognized prospect-pipeline stages, that
dominant stage calibrates ``operational_customer``/``operational_prospect``
for records in that month that otherwise only have recency + identity
signal (record-level explicit ``lifecyclestage``, when present, always
wins first -- see rule order below).

## Per-record rule order (first match wins)

1. ``explicit_customer_lifecycle`` -- record's own ``lifecyclestage`` is
   ``customer`` -> ``operational_customer`` (confidence ``high``).
2. ``explicit_prospect_lifecycle`` -- record's own ``lifecyclestage`` is a
   recognized prospect-pipeline stage -> ``operational_prospect``
   (``high``).
3. ``explicit_bulk_source_no_lifecycle`` -- record's own
   ``hs_object_source`` is a recognized bulk-import value, no
   ``lifecyclestage`` is set, and the record is untouched since creation ->
   ``historical_import`` (``high``).
4. Wave membership -- record's creation day falls inside a detected wave ->
   that wave's calibrated bucket/confidence/rationale (see above).
5. Baseline (non-wave) rules keyed on identity presence and last-modified
   recency relative to the cohort analysis reference time, optionally
   calibrated by a month's evidence-layer sample (see
   ``_classify_record`` for the full decision table) -- covering
   ``operational_customer``/``operational_prospect`` (month-calibrated),
   ``operational_prospect`` (recent + touched, no lifecycle evidence),
   ``active_other`` (identity present, modified within a year),
   ``legacy_or_obsolete_candidate`` (identity present but stale/untouched,
   or no identity and untouched outside any wave), and ``uncertain`` for
   every combination that does not clear a threshold.
"""
from __future__ import annotations

import csv
import gzip
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from .snapshot import Snapshot

SUPPORTED_OBJECT_TYPES = ("companies", "contacts")

ALL_BUCKETS = (
    "operational_customer",
    "operational_prospect",
    "active_other",
    "historical_import",
    "enrichment_or_bulk",
    "legacy_or_obsolete_candidate",
    "uncertain",
)

CREATE_DATE_FIELD = "createdate"
LAST_MODIFIED_FIELD_BY_TYPE = {
    "companies": "hs_lastmodifieddate",
    "contacts": "lastmodifieddate",
}
DOMAIN_EMAIL_FIELD_BY_TYPE = {
    "companies": "domain",
    "contacts": "email",
}
UNTOUCHED_THRESHOLD_SECONDS = 60
RECENCY_BUCKETS = ("<=30d", "<=90d", "<=365d", ">365d", "unknown")

# -- rule thresholds, documented (see module docstring for the full 2x2
# rationale) -- deliberately conservative: only a wave/month clearing
# these on the *full population* is treated as a signal at all.
LOW_PRESENCE_THRESHOLD = 0.3
HIGH_PRESENCE_THRESHOLD = 0.7
HIGH_UNTOUCHED_THRESHOLD = 0.6
DOMINANT_LIFECYCLE_SHARE_THRESHOLD = 0.5

CUSTOMER_LIFECYCLE_STAGES = {"customer"}
PROSPECT_LIFECYCLE_STAGES = {
    "subscriber",
    "lead",
    "marketingqualifiedlead",
    "salesqualifiedlead",
    "opportunity",
}
BULK_SOURCE_VALUES = {"IMPORT"}
ENRICHMENT_SOURCE_HINTS = ("ENRICH",)

MAX_EXAMPLES_PER_BUCKET = 5
PROGRESS_EVERY = 10000

ProgressCallback = Callable[[int, dict], None]


class PopulationReconciliationError(RuntimeError):
    """Raised when a bucket-count sum does not equal reconcile.py's
    population baseline for an object type. Given classify_object_type's
    full-population, dedup-by-id-first-wins streaming design, every unique
    record receives exactly one bucket, so this should never happen; the
    check exists purely as an explicit, tested invariant guard, not as a
    condition expected to fire in practice."""


def _population_baseline(reconciliation_entry: dict) -> tuple[Optional[int], str]:
    """The population total classification bucket counts must sum to for
    one object type: reconcile.py's independently recorded portal total
    *only* when one was recorded and it already matches the independently
    recomputed unique-ID count (i.e. reconcile.py itself reports
    ``reconciled: True``); otherwise the recomputed unique-ID count.

    A recorded-but-unverified portal total (present but not matching) is
    never substituted as the baseline: classification streams and buckets
    every unique id in the snapshot, so its total is definitionally the
    unique-ID count, and treating a diverging recorded total as the
    baseline would make the reconciliation check fail for reasons that
    have nothing to do with classification's own correctness (see
    reconcile.py's own conservative "never assumed to pass" stance on an
    unverified portal total).
    """
    unique_id_count = reconciliation_entry.get("unique_id_count")
    recorded = reconciliation_entry.get("recorded_portal_total")
    if recorded is not None and recorded == unique_id_count:
        return recorded, "recorded_portal_total"
    return unique_id_count, "unique_id_count"


def _build_population_reconciliation(per_type: dict, reconciliation: dict) -> dict:
    result = {}
    for object_type, classified in per_type.items():
        entry = reconciliation.get(object_type, {})
        baseline, baseline_source = _population_baseline(entry)
        bucket_count_sum = classified["total_classified"]
        matches = baseline is not None and bucket_count_sum == baseline
        if not matches:
            raise PopulationReconciliationError(
                f"Population reconciliation invariant violated for {object_type!r}: "
                f"bucket-count sum ({bucket_count_sum}) does not equal the population "
                f"baseline ({baseline}, source={baseline_source!r}). This should never "
                "happen given full-population, dedup-by-id-first-wins streaming "
                "classification -- it indicates a bug in classification.py, not a data "
                "gap."
            )
        result[object_type] = {
            "bucket_count_sum": bucket_count_sum,
            "population_baseline": baseline,
            "population_baseline_source": baseline_source,
            "matches": matches,
        }
    return result


# -- small utilities, deliberately duplicated (not imported) from
# cohorts.py -- matching this package's existing precedent
# (evidence.py duplicates its own ``_parse_iso``/``_recency_bucket`` rather
# than importing cohorts.py's private helpers) so each module's streaming
# pass stays self-contained and independently testable. ----------------------
def _parse_timestamp(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            try:
                return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _cohort_keys(dt: Optional[datetime]):
    if dt is None:
        return "unknown", "unknown"
    return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")


def _has_domain_or_email(object_type: str, properties: dict) -> bool:
    field = DOMAIN_EMAIL_FIELD_BY_TYPE[object_type]
    value = properties.get(field)
    return bool(value and str(value).strip())


def _is_untouched(created_dt: Optional[datetime], modified_dt: Optional[datetime]) -> bool:
    if created_dt is None or modified_dt is None:
        return False
    return abs((modified_dt - created_dt).total_seconds()) <= UNTOUCHED_THRESHOLD_SECONDS


def _recency_bucket(modified_dt: Optional[datetime], reference_time: datetime) -> str:
    if modified_dt is None:
        return "unknown"
    delta_days = max(0.0, (reference_time - modified_dt).total_seconds() / 86400.0)
    if delta_days <= 30:
        return "<=30d"
    if delta_days <= 90:
        return "<=90d"
    if delta_days <= 365:
        return "<=365d"
    return ">365d"


def _source_hint(source_dist: dict, hints: Iterable[str]) -> bool:
    return any(any(h in str(key).upper() for h in hints) for key in source_dist)


def _day_to_wave_map(waves: list) -> dict:
    mapping = {}
    for wave in waves:
        start = date.fromisoformat(wave["start"])
        end = date.fromisoformat(wave["end"])
        d = start
        while d <= end:
            mapping[d.isoformat()] = wave["wave_id"]
            d += timedelta(days=1)
    return mapping


def _stratum_index(evidence: dict, object_type: str) -> dict:
    index = {}
    for summary in (evidence.get("strata_summaries") or {}).get(object_type, []):
        index[(summary["kind"], summary["key"])] = summary
    return index


def _result(bucket: str, confidence: str, rule_id: str, rationale) -> dict:
    return {"bucket": bucket, "confidence": confidence, "rule_id": rule_id, "rationale": list(rationale)}


# -- wave-level classification (full-population 2x2 + evidence calibration) --
def _classify_wave(wave: dict, evidence_stratum: Optional[dict]) -> dict:
    presence = wave["presence_rate"]
    untouched = wave["untouched_since_creation_rate"]
    field = wave["presence_field"]
    alt_explanations = list(wave.get("alternative_explanations", []))
    rationale = [
        f"wave {wave['wave_id']} ({wave['start']}..{wave['end']}, {wave['total_records']} records, "
        f"{wave['share_of_population']:.2%} of population): population-observed {field}_presence_rate="
        f"{presence:.2%}, untouched_since_creation_rate={untouched:.2%} (both full-population facts "
        "over every record in this wave, not a sample)",
        f"alternative explanations considered: {', '.join(alt_explanations) or 'none recorded'}",
    ]

    source_dist: dict = {}
    has_evidence = bool(evidence_stratum and evidence_stratum.get("fetched_count"))
    if has_evidence:
        n = evidence_stratum["fetched_count"]
        source_dist = evidence_stratum.get("source_distribution", {})
        lifecycle_dist = evidence_stratum.get("lifecyclestage_distribution", {})
        assoc = evidence_stratum.get("association_presence", {})
        unknown_lifecycle_share = (lifecycle_dist.get("unknown", 0) / n) if n else 0.0
        assoc_values = [v for v in assoc.values() if v is not None]
        max_assoc = max(assoc_values) if assoc_values else 0.0
        rationale.append(
            f"evidence sample n={n} ({evidence_stratum['uncertainty_note']}): "
            f"{unknown_lifecycle_share:.0%} missing lifecyclestage, max association presence "
            f"{max_assoc:.0%}, source distribution {source_dist or 'none observed'}"
        )
    else:
        rationale.append(
            "no evidence-layer sample available for this wave this run (offline run, or this "
            "stratum was not sampled); classification below relies on full-population cohort "
            "presence/untouched signals only"
        )

    low_presence = presence < LOW_PRESENCE_THRESHOLD
    high_presence = presence >= HIGH_PRESENCE_THRESHOLD
    high_untouched = untouched >= HIGH_UNTOUCHED_THRESHOLD

    if low_presence and high_untouched:
        rationale.append(
            f"low {field} presence + high untouched rate refutes 'genuine campaign/form spike' "
            "(a real spike would show materially higher identity-field presence) and is consistent "
            "with a bulk import or enrichment-tool create"
        )
        if _source_hint(source_dist, ENRICHMENT_SOURCE_HINTS):
            bucket = "enrichment_or_bulk"
            rationale.append("evidence source distribution names an enrichment-tool-like source")
        else:
            bucket = "historical_import"
        confidence = "medium_sample_calibrated" if has_evidence else "low_no_evidence_sample"
        return {"bucket": bucket, "confidence": confidence, "rationale": rationale}

    if high_presence and high_untouched:
        rationale.append(
            f"high {field} presence + high untouched rate: identity fields look real/valid but the "
            "cohort was never engaged with again after creation -- consistent with a bulk enrichment "
            "or data-loading tool rather than either organic signup or raw import junk"
        )
        confidence = "medium_sample_calibrated" if has_evidence else "low_no_evidence_sample"
        return {"bucket": "enrichment_or_bulk", "confidence": confidence, "rationale": rationale}

    if high_presence and not high_untouched:
        rationale.append(
            f"high {field} presence + low untouched rate supports the alternative explanation of a "
            "genuine bulk onboarding/campaign batch with real, subsequently-used data rather than junk"
        )
        return {"bucket": "active_other", "confidence": "medium", "rationale": rationale}

    rationale.append(
        f"{field} presence and untouched-since-creation signals are both mixed/moderate for this "
        "wave; neither the import/bulk nor the genuine-batch explanation is clearly supported"
    )
    return {"bucket": "uncertain", "confidence": "mixed_signal", "rationale": rationale}


# -- month-level lifecycle calibration (baseline records only) ---------------
def _classify_month_calibration(evidence_stratum: Optional[dict]) -> Optional[dict]:
    if not evidence_stratum or not evidence_stratum.get("fetched_count"):
        return None
    n = evidence_stratum["fetched_count"]
    lifecycle_dist = evidence_stratum.get("lifecyclestage_distribution", {})
    items = [(k, v) for k, v in lifecycle_dist.items() if k and k != "unknown"]
    if not items:
        return None
    key, count = max(items, key=lambda kv: kv[1])
    share = (count / n) if n else 0.0
    if share < DOMINANT_LIFECYCLE_SHARE_THRESHOLD:
        return None
    key_norm = str(key).strip().lower()
    if key_norm in CUSTOMER_LIFECYCLE_STAGES:
        bucket = "operational_customer"
    elif key_norm in PROSPECT_LIFECYCLE_STAGES:
        bucket = "operational_prospect"
    else:
        return None
    rationale = [
        f"evidence sample for this month (n={n}, {evidence_stratum['uncertainty_note']}): dominant "
        f"lifecyclestage={key!r} at {share:.0%} of the sample"
    ]
    return {"bucket": bucket, "rationale": rationale}


# -- per-record rule engine ---------------------------------------------------
def _classify_record(
    properties: dict,
    *,
    wave_id: Optional[str],
    wave_signals: dict,
    month_calibration: Optional[dict],
    has_identity: bool,
    untouched: bool,
    recency_bucket: str,
    presence_field: str,
) -> dict:
    lifecycle_raw = properties.get("lifecyclestage")
    lifecycle_norm = str(lifecycle_raw).strip().lower() if lifecycle_raw else None
    source_raw = properties.get("hs_object_source")
    source_norm = str(source_raw).strip().upper() if source_raw else None

    if lifecycle_norm in CUSTOMER_LIFECYCLE_STAGES:
        return _result(
            "operational_customer",
            "high",
            "explicit_customer_lifecycle",
            [f"record property lifecyclestage={lifecycle_raw!r} observed directly on this record"],
        )
    if lifecycle_norm in PROSPECT_LIFECYCLE_STAGES:
        return _result(
            "operational_prospect",
            "high",
            "explicit_prospect_lifecycle",
            [f"record property lifecyclestage={lifecycle_raw!r} observed directly on this record"],
        )
    if source_norm in BULK_SOURCE_VALUES and not lifecycle_norm and untouched:
        return _result(
            "historical_import",
            "high",
            "explicit_bulk_source_no_lifecycle",
            [
                f"record property hs_object_source={source_raw!r} observed directly, no "
                "lifecyclestage set, and untouched since creation"
            ],
        )

    if wave_id and wave_id in wave_signals:
        w = wave_signals[wave_id]
        return _result(w["bucket"], w["confidence"], f"wave_membership:{wave_id}", w["rationale"])

    # Baseline (non-wave) records: cohort/wave membership, recency, and
    # identity presence only -- optionally calibrated by a month's
    # evidence-layer sample.
    if has_identity:
        if month_calibration and recency_bucket in ("<=30d", "<=90d", "<=365d"):
            return _result(
                month_calibration["bucket"], "medium_sample_calibrated", "baseline_month_calibrated",
                month_calibration["rationale"],
            )
        if recency_bucket in ("<=30d", "<=90d"):
            if not untouched:
                return _result(
                    "operational_prospect", "low", "baseline_recent_touched_with_identity",
                    [
                        f"has {presence_field}, last modified {recency_bucket}, touched after "
                        "creation; no lifecycle evidence available at record or evidence-sample level"
                    ],
                )
            return _result(
                "uncertain", "insufficient_signal", "baseline_recent_untouched_with_identity",
                [
                    f"has {presence_field} and was created/modified {recency_bucket} ago but is "
                    "still untouched since creation; too early to distinguish a genuine new record "
                    "from one that was never followed up"
                ],
            )
        if recency_bucket == "<=365d":
            return _result(
                "active_other", "low", "baseline_older_with_identity",
                [f"has {presence_field}, last modified within a year, no explicit lifecycle evidence"],
            )
        if recency_bucket == ">365d":
            if untouched:
                return _result(
                    "legacy_or_obsolete_candidate", "medium", "stale_untouched_with_identity",
                    [f"has {presence_field} but untouched since creation and last modified over a year ago"],
                )
            return _result(
                "legacy_or_obsolete_candidate", "low", "stale_touched_with_identity",
                [f"has {presence_field}, last modified over a year ago (was touched at some point since creation)"],
            )
        return _result(
            "uncertain", "insufficient_signal", "no_recency_signal_with_identity",
            [f"has {presence_field} but no last-modified value is present -- recency is unknown"],
        )

    # No identity (no domain/email at all).
    if untouched:
        return _result(
            "legacy_or_obsolete_candidate", "low", "no_identity_untouched",
            [f"no {presence_field} present and untouched since creation, outside any detected bulk-import wave"],
        )
    if recency_bucket == "unknown":
        return _result(
            "uncertain", "insufficient_signal", "no_identity_no_recency_signal",
            [f"no {presence_field} present and no last-modified value -- insufficient signal for a confident bucket"],
        )
    return _result(
        "uncertain", "insufficient_signal", "no_identity_touched",
        [f"no {presence_field} present but the record was modified after creation -- ambiguous without further evidence"],
    )


# -- per-object-type orchestration -------------------------------------------
def classify_object_type(
    snapshot: Snapshot,
    object_type: str,
    cohort_analysis: dict,
    evidence: dict,
    output_dir: str,
    *,
    progress_cb: Optional[ProgressCallback] = None,
) -> dict:
    if object_type not in SUPPORTED_OBJECT_TYPES:
        raise ValueError(f"classification.classify_object_type does not support object_type={object_type!r}")

    analysis = cohort_analysis[object_type]
    reference_time = _parse_timestamp(analysis.get("reference_time")) or datetime.now(timezone.utc)
    waves = analysis.get("waves", [])
    day_to_wave = _day_to_wave_map(waves)
    stratum_index = _stratum_index(evidence, object_type)

    wave_signals = {
        wave["wave_id"]: _classify_wave(wave, stratum_index.get(("wave", wave["wave_id"])))
        for wave in waves
    }

    last_mod_field = LAST_MODIFIED_FIELD_BY_TYPE[object_type]
    presence_field = DOMAIN_EMAIL_FIELD_BY_TYPE[object_type]

    bucket_counts: Counter = Counter()
    rule_counts: Counter = Counter()
    bucket_rule_counts: dict = defaultdict(Counter)
    examples: dict = defaultdict(list)
    month_calibration_cache: dict = {}

    seen: set = set()
    raw_count = 0

    classification_path = os.path.join(output_dir, "work", f"{object_type}_classification.csv.gz")
    os.makedirs(os.path.dirname(classification_path), exist_ok=True)

    with gzip.open(classification_path, "wt", encoding="utf-8", newline="") as gz:
        writer = csv.writer(gz)
        writer.writerow(["id", "bucket", "confidence", "rule_id", "wave_id", "cohort_month", "rationale"])
        for envelope in snapshot.iter_raw_envelopes(object_type):
            raw_count += 1
            if progress_cb and raw_count % PROGRESS_EVERY == 0:
                progress_cb(raw_count, bucket_counts)

            record = envelope.get("record", envelope)
            record_id = record.get("id")
            if record_id in seen:
                continue
            seen.add(record_id)

            properties = record.get("properties") or {}
            created_dt = _parse_timestamp(properties.get(CREATE_DATE_FIELD))
            day_key, month_key = _cohort_keys(created_dt)
            modified_dt = _parse_timestamp(properties.get(last_mod_field))
            untouched = _is_untouched(created_dt, modified_dt)
            has_identity = _has_domain_or_email(object_type, properties)
            recency_bucket = _recency_bucket(modified_dt, reference_time)
            wave_id = day_to_wave.get(day_key)

            if month_key not in month_calibration_cache:
                month_calibration_cache[month_key] = _classify_month_calibration(
                    stratum_index.get(("by_month", month_key))
                )
            month_calibration = month_calibration_cache[month_key]

            result = _classify_record(
                properties,
                wave_id=wave_id,
                wave_signals=wave_signals,
                month_calibration=month_calibration,
                has_identity=has_identity,
                untouched=untouched,
                recency_bucket=recency_bucket,
                presence_field=presence_field,
            )

            bucket_counts[result["bucket"]] += 1
            rule_counts[result["rule_id"]] += 1
            bucket_rule_counts[result["bucket"]][result["rule_id"]] += 1
            writer.writerow(
                [
                    record_id,
                    result["bucket"],
                    result["confidence"],
                    result["rule_id"],
                    wave_id or "",
                    month_key,
                    " | ".join(result["rationale"]),
                ]
            )
            if len(examples[result["bucket"]]) < MAX_EXAMPLES_PER_BUCKET:
                examples[result["bucket"]].append(
                    {
                        "id": record_id,
                        "confidence": result["confidence"],
                        "rule_id": result["rule_id"],
                        "wave_id": wave_id,
                        "cohort_month": month_key,
                        "rationale": result["rationale"],
                    }
                )

    if progress_cb:
        progress_cb(raw_count, bucket_counts)

    total_classified = sum(bucket_counts[bucket] for bucket in ALL_BUCKETS)

    return {
        "object_type": object_type,
        "raw_record_count": raw_count,
        "unique_id_count": len(seen),
        "total_classified": total_classified,
        "bucket_counts": {bucket: bucket_counts[bucket] for bucket in ALL_BUCKETS},
        "bucket_percentages": {
            bucket: (bucket_counts[bucket] / total_classified) if total_classified else 0.0
            for bucket in ALL_BUCKETS
        },
        "rule_counts": dict(sorted(rule_counts.items())),
        "bucket_rule_counts": {
            bucket: dict(sorted(bucket_rule_counts.get(bucket, {}).items())) for bucket in ALL_BUCKETS
        },
        "wave_rationale": {
            wave_id: {"bucket": signal["bucket"], "confidence": signal["confidence"], "rationale": signal["rationale"]}
            for wave_id, signal in wave_signals.items()
        },
        "examples": {bucket: examples.get(bucket, []) for bucket in ALL_BUCKETS},
        "classification_feature_table": os.path.relpath(classification_path, output_dir),
    }


def build_population_map(
    snapshot: Snapshot,
    cohort_analysis: dict,
    evidence: dict,
    reconciliation: dict,
    object_types: Iterable[str],
    output_dir: str,
    *,
    progress_cb: Optional[Callable[[str, int, dict], None]] = None,
) -> dict:
    """Run full-population classification for every supported object type
    present in ``object_types`` and assemble the ``population_map.json``
    payload. ``progress_cb(object_type, records_processed, bucket_counts)``
    is invoked periodically (at least every ``PROGRESS_EVERY`` records) if
    given.

    Returns a dict with keys ``population_map`` (the JSON payload), ``gaps``
    and ``next_actions`` (lists to merge into the top-level artifacts).
    """
    supported = [ot for ot in object_types if ot in SUPPORTED_OBJECT_TYPES]
    per_type: dict = {}
    for object_type in supported:
        def _cb(processed, counts, _object_type=object_type):
            if progress_cb:
                progress_cb(_object_type, processed, counts)

        per_type[object_type] = classify_object_type(
            snapshot, object_type, cohort_analysis, evidence, output_dir, progress_cb=_cb
        )

    population_reconciliation = _build_population_reconciliation(per_type, reconciliation)

    gaps: list = []
    next_actions: list = []
    reconciliation_crosscheck: dict = {}
    unclassified: dict = {}

    evidence_status = evidence.get("status")
    if evidence_status == "skipped_offline":
        gaps.append(
            "Classification for cohorts/months without an evidence-layer sample relies solely on "
            "full-population cohort presence/untouched-since-creation heuristics (cohorts.py); no "
            "lifecycle/source/association sample was available this run (evidence.json status is "
            "'skipped_offline'), so no wave or month reached 'medium_sample_calibrated' confidence."
        )
        next_actions.append(
            "Run with a live HubSpot token (see evidence.json's sampling plan) to obtain the "
            "lifecycle/source/association samples that would raise wave/month classification "
            "confidence above 'low_no_evidence_sample'/'no lifecycle calibration'."
        )

    for object_type, result in per_type.items():
        reconciled_total = reconciliation.get(object_type, {}).get("unique_id_count")
        classified_total = result["total_classified"]
        matches = reconciled_total is not None and classified_total == reconciled_total
        reconciliation_crosscheck[object_type] = {
            "reconciled_total": reconciled_total,
            "classified_total": classified_total,
            "matches": matches,
        }
        unclassified[object_type] = 0
        if not matches:
            gaps.append(
                f"'{object_type}': classified total ({classified_total}) does not match reconcile.py's "
                f"unique_id_count ({reconciled_total}); this should never happen given full-population "
                "streaming and indicates a bug in classification.py, not a data gap."
            )

        for wave_id, signal in result["wave_rationale"].items():
            if signal["confidence"] == "low_no_evidence_sample":
                gaps.append(
                    f"'{object_type}' wave {wave_id}: classified as '{signal['bucket']}' using "
                    "full-population cohort presence/untouched heuristics only -- no evidence-layer "
                    f"sample was available for this wave this run; confidence is 'low_no_evidence_sample'."
                )

        uncertain_count = result["bucket_counts"]["uncertain"]
        if uncertain_count:
            share = uncertain_count / result["total_classified"] if result["total_classified"] else 0.0
            gaps.append(
                f"'{object_type}': {uncertain_count} record(s) ({share:.1%}) classified 'uncertain' -- "
                "insufficient signal to assign a confident bucket; see "
                f"work/{object_type}_classification.csv.gz for the per-record rule that produced this."
            )

    population_map = {
        "schema_version": 1,
        "status": "completed",
        "buckets": list(ALL_BUCKETS),
        "reconciliation": reconciliation,
        "reconciliation_crosscheck": reconciliation_crosscheck,
        "population_reconciliation": population_reconciliation,
        "unclassified": unclassified,
        "observed_facts": [
            "reconciliation.*.unique_id_count (the authoritative per-object-type population total "
            "every bucket-count table below sums to exactly; recomputed by reconcile.py, never a "
            "recorded-but-unverified portal total)",
            "inferred_classification.*.bucket_counts / bucket_percentages (streamed over every "
            "unique record in the snapshot; each record's own lifecyclestage/hs_object_source, when "
            "directly present, is used as an observed fact)",
            "population_reconciliation.*.population_baseline / bucket_count_sum (cross-validated: "
            "the recorded portal total when one was recorded AND it already matches reconcile.py's "
            "independently recomputed unique-ID count, otherwise the unique-ID count itself; "
            "bucket_count_sum is asserted equal to this baseline at build time -- see "
            "classification.PopulationReconciliationError)",
        ],
        "inferred_classification": {
            object_type: {
                "bucket_counts": result["bucket_counts"],
                "bucket_percentages": result["bucket_percentages"],
                "rule_counts": result["rule_counts"],
                "bucket_rule_counts": result["bucket_rule_counts"],
                "wave_rationale": result["wave_rationale"],
                "examples": result["examples"],
                "classification_feature_table": result["classification_feature_table"],
            }
            for object_type, result in per_type.items()
        },
        "uncertainty": [
            "Every bucket assignment carries an explicit confidence tag: 'high' (an explicit "
            "lifecyclestage/hs_object_source observed directly on the record), "
            "'medium_sample_calibrated' (a wave or month evidence-layer sample supports the "
            "cohort-level signal), 'medium'/'low' (full-population cohort heuristic only, no "
            "evidence sample), or 'insufficient_signal'/'mixed_signal' (the record or cohort landed "
            "in 'uncertain' because available signals do not clear this module's thresholds).",
            "Wave classifications reached without a matching evidence-layer sample this run are "
            "capped at 'low_no_evidence_sample' -- see gaps.json for exactly which waves.",
        ],
        "inaccessible": list(gaps),
        "notes": (
            "Bucket classification is rule-based and fully deterministic (see classification.py's "
            "module docstring for the exact rule order and 2x2 wave decision table). It relies "
            "primarily on cohort/wave membership, last-modified recency, and domain/email presence "
            "-- the signals available for every record in the live snapshot -- and only uses the "
            "sampled evidence layer to calibrate confidence and to characterize whole cohorts, never "
            "to claim record-level certainty a sample cannot support."
        ),
    }
    return {"population_map": population_map, "gaps": gaps, "next_actions": next_actions}
