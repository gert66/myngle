"""Streaming creation-cohort analysis and heuristic bulk-import wave detection.

Builds, per object type, creation cohorts by day/ISO-week/month from the
raw snapshot, per-month cohort profiles (presence/recency/domain
characterization), exact-createdate-minute timestamp-burst detection, and a
heuristic (explicitly labelled *inferred*, never conclusive) bulk-import
wave detector. Also persists a compact per-record feature table for reuse
by later batches.

Design: two streaming passes per object type, each opening and re-reading
``raw/<object_type>.jsonl`` from disk via ``Snapshot.iter_raw_envelopes`` --
never materializing the full record list in memory.

  * Pass 1 dedupes by record id (first occurrence wins, matching
    ``reconcile.py``), builds day/week/month/minute counters, and
    per-month profile accumulators (presence, name, untouched, domain/email
    suffix counts). It also tracks each record's own reference time (max
    envelope ``extracted_at``).
  * Between passes, day-level counts (already in memory as small
    counters) are used to run the wave-detection heuristic and determine
    burst minutes -- no record-level data is needed for this step.
  * Pass 2 re-streams the file to fill in last-modified recency buckets
    (which depend on the reference time computed after pass 1), aggregate
    wave-restricted characterization (top domains/suffixes, presence and
    untouched rates *within* detected waves), and stream-write the gzip
    feature table one row at a time.

Memory note: only small per-key counters (per day/week/month/minute, and
per-month/per-wave suffix ``Counter``s) plus the set of unique record ids
(up to roughly 450k ints/strings for the live snapshot) are ever held in
memory at once. Per-record feature rows are written straight to the gzip
output stream and are never buffered as a list.
"""
from __future__ import annotations

import csv
import gzip
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from .snapshot import Snapshot

SUPPORTED_OBJECT_TYPES = ("companies", "contacts")

DEFAULT_BURST_MIN_RECORDS = 50
DEFAULT_BASELINE_WINDOW_DAYS = 90
DEFAULT_WAVE_FACTOR = 8.0
DEFAULT_WAVE_ABS_MIN = 500
DEFAULT_MIN_PRIOR_NONZERO_DAYS = 14
UNTOUCHED_THRESHOLD_SECONDS = 60

RECENCY_BUCKETS = ("<=30d", "<=90d", "<=365d", ">365d", "unknown")

FREEMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "outlook.com",
    "yahoo.com",
    "live.com",
    "icloud.com",
    "aol.com",
    "protonmail.com",
    "msn.com",
    "me.com",
}

ALTERNATIVE_EXPLANATIONS = [
    "genuine campaign/form spike",
    "integration sync/backfill",
    "CSV import",
    "enrichment tool bulk create",
]

CREATE_DATE_FIELD = "createdate"
LAST_MODIFIED_FIELD_BY_TYPE = {
    "companies": "hs_lastmodifieddate",
    "contacts": "lastmodifieddate",
}
NAME_FIELDS_BY_TYPE = {
    "companies": ("name",),
    "contacts": ("firstname", "lastname"),
}
DOMAIN_EMAIL_FIELD_BY_TYPE = {
    "companies": "domain",
    "contacts": "email",
}

ProgressCallback = Callable[[int], None]


def _parse_timestamp(value) -> Optional[datetime]:
    """Parse an ISO-8601 string, an epoch-millisecond string/int, or return
    None for missing/unparsable input. Never raises."""
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


def _format_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cohort_keys(dt: Optional[datetime]):
    if dt is None:
        return "unknown", "unknown", "unknown"
    day_key = dt.strftime("%Y-%m-%d")
    iso_year, iso_week, _ = dt.isocalendar()
    week_key = f"{iso_year:04d}-W{iso_week:02d}"
    month_key = dt.strftime("%Y-%m")
    return day_key, week_key, month_key


def _registrable_domain(raw: str) -> Optional[str]:
    """Naive registrable-domain heuristic (last two dot-separated labels,
    lowercased). Not public-suffix-list aware -- good enough for top-N
    reporting, not for exact duplicate/ownership detection."""
    if not raw:
        return None
    value = raw.strip().lower()
    if "@" in value:
        value = value.split("@", 1)[1]
    value = value.strip(".")
    if not value:
        return None
    parts = value.split(".")
    if len(parts) <= 2:
        return value
    return ".".join(parts[-2:])


def _suffix_for(object_type: str, properties: dict) -> Optional[str]:
    field = DOMAIN_EMAIL_FIELD_BY_TYPE[object_type]
    raw = properties.get(field)
    if not raw:
        return None
    return _registrable_domain(str(raw))


def _has_domain_or_email(object_type: str, properties: dict) -> bool:
    field = DOMAIN_EMAIL_FIELD_BY_TYPE[object_type]
    value = properties.get(field)
    return bool(value and str(value).strip())


def _has_name(object_type: str, properties: dict) -> bool:
    fields = NAME_FIELDS_BY_TYPE[object_type]
    return any(properties.get(field) and str(properties.get(field)).strip() for field in fields)


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


def _new_month_profile() -> dict:
    return {
        "count": 0,
        "domain_or_email_present": 0,
        "name_present": 0,
        "untouched_since_creation": 0,
        "suffix_counts": Counter(),
        "freemail_count": 0,
        "last_modified_recency": Counter(),
    }


def _pass1(snapshot: Snapshot, object_type: str, *, progress_cb: Optional[ProgressCallback] = None,
           progress_every: int = 10000) -> dict:
    seen = set()
    raw_record_count = 0
    unknown_createdate_count = 0
    day_counts: Counter = Counter()
    week_counts: Counter = Counter()
    month_counts: Counter = Counter()
    minute_counts: Counter = Counter()
    month_profiles: dict = defaultdict(_new_month_profile)
    max_extracted_at: Optional[datetime] = None
    last_mod_field = LAST_MODIFIED_FIELD_BY_TYPE[object_type]

    for envelope in snapshot.iter_raw_envelopes(object_type):
        raw_record_count += 1
        extracted_dt = _parse_timestamp(envelope.get("extracted_at"))
        if extracted_dt is not None and (max_extracted_at is None or extracted_dt > max_extracted_at):
            max_extracted_at = extracted_dt

        if progress_cb and raw_record_count % progress_every == 0:
            progress_cb(raw_record_count)

        record = envelope.get("record", envelope)
        record_id = record.get("id")
        if record_id in seen:
            continue
        seen.add(record_id)

        properties = record.get("properties") or {}
        created_dt = _parse_timestamp(properties.get(CREATE_DATE_FIELD))
        day_key, week_key, month_key = _cohort_keys(created_dt)
        if created_dt is None:
            unknown_createdate_count += 1
        day_counts[day_key] += 1
        week_counts[week_key] += 1
        month_counts[month_key] += 1
        if created_dt is not None:
            minute_counts[created_dt.strftime("%Y-%m-%dT%H:%M")] += 1

        profile = month_profiles[month_key]
        profile["count"] += 1
        if _has_domain_or_email(object_type, properties):
            profile["domain_or_email_present"] += 1
        if _has_name(object_type, properties):
            profile["name_present"] += 1
        modified_dt = _parse_timestamp(properties.get(last_mod_field))
        if _is_untouched(created_dt, modified_dt):
            profile["untouched_since_creation"] += 1
        suffix = _suffix_for(object_type, properties)
        if suffix:
            profile["suffix_counts"][suffix] += 1
            if object_type == "contacts" and suffix in FREEMAIL_DOMAINS:
                profile["freemail_count"] += 1

    if progress_cb:
        progress_cb(raw_record_count)

    return {
        "raw_record_count": raw_record_count,
        "seen_ids": seen,
        "unique_id_count": len(seen),
        "unknown_createdate_count": unknown_createdate_count,
        "max_extracted_at": max_extracted_at,
        "day_counts": day_counts,
        "week_counts": week_counts,
        "month_counts": month_counts,
        "minute_counts": minute_counts,
        "month_profiles": month_profiles,
    }


def _compute_waves(day_counts: dict, minute_counts: dict, unique_id_count: int, *,
                    burst_min_records: int, baseline_window_days: int, wave_factor: float,
                    wave_abs_min: int, min_prior_nonzero_days: int):
    counts_by_date = {}
    for key, count in day_counts.items():
        if key == "unknown":
            continue
        counts_by_date[date.fromisoformat(key)] = count
    sorted_dates = sorted(counts_by_date)
    all_values = list(counts_by_date.values())
    overall_median = statistics.median(all_values) if all_values else 0.0

    baseline_used_by_day = {}
    flagged = []
    for d in sorted_dates:
        window_start = d - timedelta(days=baseline_window_days)
        prior_values = [counts_by_date[pd] for pd in sorted_dates if window_start <= pd < d]
        if len(prior_values) >= min_prior_nonzero_days:
            baseline = statistics.median(prior_values)
        else:
            baseline = overall_median
        baseline_used_by_day[d] = baseline
        threshold = max(wave_abs_min, wave_factor * baseline)
        if counts_by_date[d] >= threshold:
            flagged.append(d)

    merged_ranges = []
    i = 0
    while i < len(flagged):
        start = flagged[i]
        end = flagged[i]
        j = i + 1
        while j < len(flagged) and (flagged[j] - end).days <= 2:
            end = flagged[j]
            j += 1
        merged_ranges.append((start, end))
        i = j

    burst_minutes = {m for m, c in minute_counts.items() if c >= burst_min_records}

    waves = []
    for idx, (start, end) in enumerate(merged_ranges, start=1):
        wave_id = f"wave-{idx}"
        days_in_range = []
        d = start
        while d <= end:
            days_in_range.append(d)
            d += timedelta(days=1)
        total = sum(counts_by_date.get(d, 0) for d in days_in_range)
        peak_day = max(days_in_range, key=lambda dd: counts_by_date.get(dd, 0))
        peak_count = counts_by_date.get(peak_day, 0)
        share = (total / unique_id_count) if unique_id_count else 0.0
        day_strs = {d.isoformat() for d in days_in_range}
        burst_total = sum(c for m, c in minute_counts.items() if m in burst_minutes and m[:10] in day_strs)
        share_in_bursts = (burst_total / total) if total else 0.0
        waves.append(
            {
                "wave_id": wave_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": len(days_in_range),
                "total_records": total,
                "share_of_population": share,
                "baseline_used": baseline_used_by_day.get(peak_day, overall_median),
                "peak_day": peak_day.isoformat(),
                "peak_count": peak_count,
                "share_in_timestamp_bursts": share_in_bursts,
            }
        )
    return waves, burst_minutes


def _sep_2023_focus(day_counts: dict, unique_id_count: int, day_to_wave: dict) -> dict:
    by_day = {k: v for k, v in day_counts.items() if k.startswith("2023-09")}
    total = sum(by_day.values())
    wave_ids = sorted({day_to_wave[k] for k in by_day if k in day_to_wave})
    return {
        "by_day": dict(sorted(by_day.items())),
        "count": total,
        "share_of_population": (total / unique_id_count) if unique_id_count else 0.0,
        "detected_as_wave": bool(wave_ids),
        "wave_ids": wave_ids,
    }


def _pass2(snapshot: Snapshot, object_type: str, *, seen_ids: set, month_profiles: dict,
           day_to_wave: dict, burst_minutes: set, reference_time: datetime, output_dir: str,
           wave_stats: dict) -> None:
    last_mod_field = LAST_MODIFIED_FIELD_BY_TYPE[object_type]
    feature_path = os.path.join(output_dir, "work", f"{object_type}_features.csv.gz")
    os.makedirs(os.path.dirname(feature_path), exist_ok=True)
    written_ids: set = set()

    with gzip.open(feature_path, "wt", encoding="utf-8", newline="") as gz:
        writer = csv.writer(gz)
        writer.writerow(
            [
                "id",
                "createdate_iso",
                "cohort_month",
                "cohort_day",
                "has_domain_or_email",
                "has_name",
                "untouched_since_creation",
                "last_modified_iso",
                "in_burst_minute",
                "wave_id",
            ]
        )
        for envelope in snapshot.iter_raw_envelopes(object_type):
            record = envelope.get("record", envelope)
            record_id = record.get("id")
            if record_id not in seen_ids or record_id in written_ids:
                continue
            written_ids.add(record_id)

            properties = record.get("properties") or {}
            created_dt = _parse_timestamp(properties.get(CREATE_DATE_FIELD))
            day_key, _week_key, month_key = _cohort_keys(created_dt)
            modified_dt = _parse_timestamp(properties.get(last_mod_field))
            untouched = _is_untouched(created_dt, modified_dt)
            has_domain_email = _has_domain_or_email(object_type, properties)
            has_name = _has_name(object_type, properties)
            minute_key = created_dt.strftime("%Y-%m-%dT%H:%M") if created_dt else None
            in_burst = bool(minute_key and minute_key in burst_minutes)
            wave_id = day_to_wave.get(day_key, "")

            bucket = _recency_bucket(modified_dt, reference_time)
            month_profiles[month_key]["last_modified_recency"][bucket] += 1

            if wave_id:
                stats = wave_stats[wave_id]
                stats["total"] += 1
                if has_domain_email:
                    stats["domain_or_email_present"] += 1
                if has_name:
                    stats["name_present"] += 1
                if untouched:
                    stats["untouched"] += 1
                suffix = _suffix_for(object_type, properties)
                if suffix:
                    stats["suffix_counts"][suffix] += 1

            writer.writerow(
                [
                    record_id,
                    _format_iso_z(created_dt) if created_dt else "",
                    month_key,
                    day_key,
                    int(has_domain_email),
                    int(has_name),
                    int(untouched),
                    _format_iso_z(modified_dt) if modified_dt else "",
                    int(in_burst),
                    wave_id,
                ]
            )


def _finalize_month_profiles(month_profiles: dict, object_type: str, unique_id_count: int) -> dict:
    presence_field = DOMAIN_EMAIL_FIELD_BY_TYPE[object_type]
    result = {}
    for month_key, profile in month_profiles.items():
        count = profile["count"]
        entry = {
            "count": count,
            "share_of_population": (count / unique_id_count) if unique_id_count else 0.0,
            "presence_field": presence_field,
            "presence_rate": (profile["domain_or_email_present"] / count) if count else 0.0,
            "name_present_rate": (profile["name_present"] / count) if count else 0.0,
            "untouched_since_creation_rate": (profile["untouched_since_creation"] / count) if count else 0.0,
            "last_modified_recency": {
                bucket: profile["last_modified_recency"].get(bucket, 0) for bucket in RECENCY_BUCKETS
            },
            "top_suffixes": [
                {"suffix": suffix, "count": suffix_count}
                for suffix, suffix_count in profile["suffix_counts"].most_common(10)
            ],
        }
        if object_type == "contacts":
            entry["freemail_share"] = (profile["freemail_count"] / count) if count else 0.0
        result[month_key] = entry
    return dict(sorted(result.items()))


def _finalize_waves(waves_raw: list, wave_stats: dict, object_type: str) -> list:
    presence_field = DOMAIN_EMAIL_FIELD_BY_TYPE[object_type]
    out = []
    for wave in waves_raw:
        stats = wave_stats[wave["wave_id"]]
        total = stats["total"] or wave["total_records"]
        wave = dict(wave)
        wave["presence_field"] = presence_field
        wave["presence_rate"] = (stats["domain_or_email_present"] / total) if total else 0.0
        wave["name_present_rate"] = (stats["name_present"] / total) if total else 0.0
        wave["untouched_since_creation_rate"] = (stats["untouched"] / total) if total else 0.0
        wave["top_suffixes"] = [
            {"suffix": suffix, "count": suffix_count}
            for suffix, suffix_count in stats["suffix_counts"].most_common(10)
        ]
        wave["alternative_explanations"] = list(ALTERNATIVE_EXPLANATIONS)
        out.append(wave)
    return out


def analyze_object_type(
    snapshot: Snapshot,
    object_type: str,
    output_dir: str,
    *,
    reference_time: Optional[datetime] = None,
    burst_min_records: int = DEFAULT_BURST_MIN_RECORDS,
    baseline_window_days: int = DEFAULT_BASELINE_WINDOW_DAYS,
    wave_factor: float = DEFAULT_WAVE_FACTOR,
    wave_abs_min: int = DEFAULT_WAVE_ABS_MIN,
    min_prior_nonzero_days: int = DEFAULT_MIN_PRIOR_NONZERO_DAYS,
    progress_cb: Optional[ProgressCallback] = None,
) -> dict:
    """Run the two-pass cohort/wave analysis for a single object type and
    persist its feature table under ``output_dir/work/``. Returns the
    analysis dict (not yet wrapped with the cross-object-type envelope)."""
    if object_type not in SUPPORTED_OBJECT_TYPES:
        raise ValueError(f"cohorts.analyze_object_type does not support object_type={object_type!r}")

    stage1 = _pass1(snapshot, object_type, progress_cb=progress_cb)
    unique_id_count = stage1["unique_id_count"]
    effective_reference_time = reference_time or stage1["max_extracted_at"] or datetime.now(timezone.utc)

    waves_raw, burst_minutes = _compute_waves(
        stage1["day_counts"],
        stage1["minute_counts"],
        unique_id_count,
        burst_min_records=burst_min_records,
        baseline_window_days=baseline_window_days,
        wave_factor=wave_factor,
        wave_abs_min=wave_abs_min,
        min_prior_nonzero_days=min_prior_nonzero_days,
    )

    day_to_wave = {}
    for wave in waves_raw:
        start = date.fromisoformat(wave["start"])
        end = date.fromisoformat(wave["end"])
        d = start
        while d <= end:
            day_to_wave[d.isoformat()] = wave["wave_id"]
            d += timedelta(days=1)

    wave_stats = {
        wave["wave_id"]: {
            "total": 0,
            "domain_or_email_present": 0,
            "name_present": 0,
            "untouched": 0,
            "suffix_counts": Counter(),
        }
        for wave in waves_raw
    }

    _pass2(
        snapshot,
        object_type,
        seen_ids=stage1["seen_ids"],
        month_profiles=stage1["month_profiles"],
        day_to_wave=day_to_wave,
        burst_minutes=burst_minutes,
        reference_time=effective_reference_time,
        output_dir=output_dir,
        wave_stats=wave_stats,
    )

    month_profiles = _finalize_month_profiles(stage1["month_profiles"], object_type, unique_id_count)
    waves = _finalize_waves(waves_raw, wave_stats, object_type)

    all_burst_rows = sorted(
        ({"minute": m, "count": c} for m, c in stage1["minute_counts"].items() if c >= burst_min_records),
        key=lambda row: row["count"],
        reverse=True,
    )
    total_in_bursts = sum(row["count"] for row in all_burst_rows)

    sep_focus = _sep_2023_focus(stage1["day_counts"], unique_id_count, day_to_wave)

    return {
        "unique_id_count": unique_id_count,
        "raw_record_count": stage1["raw_record_count"],
        "unknown_createdate_count": stage1["unknown_createdate_count"],
        "reference_time": _format_iso_z(effective_reference_time),
        "by_day": dict(sorted(stage1["day_counts"].items())),
        "by_week": dict(sorted(stage1["week_counts"].items())),
        "by_month": dict(sorted(stage1["month_counts"].items())),
        "month_profiles": month_profiles,
        "timestamp_bursts": {
            "unit": "createdate minute (UTC)",
            "threshold": burst_min_records,
            "minutes": all_burst_rows[:100],
            "total_records_in_bursts": total_in_bursts,
        },
        "waves": waves,
        "waves_detected": len(waves),
        "sep_2023_focus": sep_focus,
    }


def build_cohort_analysis(
    snapshot: Snapshot,
    object_types: Iterable[str],
    output_dir: str,
    *,
    burst_min_records: int = DEFAULT_BURST_MIN_RECORDS,
    baseline_window_days: int = DEFAULT_BASELINE_WINDOW_DAYS,
    wave_factor: float = DEFAULT_WAVE_FACTOR,
    wave_abs_min: int = DEFAULT_WAVE_ABS_MIN,
    min_prior_nonzero_days: int = DEFAULT_MIN_PRIOR_NONZERO_DAYS,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> dict:
    """Run cohort/wave analysis for every supported object type present in
    ``object_types`` (currently companies and contacts -- the live snapshot
    carries no deals data). ``progress_cb(object_type, records_scanned)`` is
    invoked periodically (at least every 10000 records) if given."""
    supported = [ot for ot in object_types if ot in SUPPORTED_OBJECT_TYPES]
    per_type = {}
    for object_type in supported:
        def _cb(scanned, _object_type=object_type):
            if progress_cb:
                progress_cb(_object_type, scanned)

        per_type[object_type] = analyze_object_type(
            snapshot,
            object_type,
            output_dir,
            burst_min_records=burst_min_records,
            baseline_window_days=baseline_window_days,
            wave_factor=wave_factor,
            wave_abs_min=wave_abs_min,
            min_prior_nonzero_days=min_prior_nonzero_days,
            progress_cb=_cb,
        )

    reference_times = [analysis["reference_time"] for analysis in per_type.values()]
    reference_time = max(reference_times) if reference_times else _format_iso_z(datetime.now(timezone.utc))

    result = {
        "schema_version": 1,
        "status": "completed",
        "reference_time": reference_time,
        "parameters": {
            "burst_min_records": burst_min_records,
            "baseline_window_days": baseline_window_days,
            "wave_factor": wave_factor,
            "wave_abs_min": wave_abs_min,
            "min_prior_nonzero_days": min_prior_nonzero_days,
            "untouched_threshold_seconds": UNTOUCHED_THRESHOLD_SECONDS,
        },
        "labels": {
            "observed_facts": [
                "by_day",
                "by_week",
                "by_month",
                "unknown_createdate_count",
                "month_profiles.count",
                "month_profiles.share_of_population",
                "month_profiles.presence_rate",
                "month_profiles.name_present_rate",
                "month_profiles.untouched_since_creation_rate",
                "month_profiles.last_modified_recency",
                "month_profiles.top_suffixes",
                "month_profiles.freemail_share",
                "timestamp_bursts",
                "sep_2023_focus.by_day",
                "sep_2023_focus.count",
                "sep_2023_focus.share_of_population",
            ],
            "inferred": [
                "waves",
                "waves.alternative_explanations",
                "sep_2023_focus.detected_as_wave",
                "sep_2023_focus.wave_ids",
            ],
        },
    }
    for object_type, analysis in per_type.items():
        result[object_type] = analysis
    return result
