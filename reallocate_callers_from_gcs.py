"""
reallocate_callers_from_gcs.py — Reassign cold callers on an already-exported GCS run
=====================================================================================
Reads the *current* run for one or more country folders in the Lovable GCS
bucket (``myngle-company-data-104527058436`` by default — see
``lovable_gcs_upload.py``), reassigns each company's ``assigned_cold_caller``
from a new caller list, and writes the result to a brand-new run folder:

    gs://<bucket>/<country_folder>/runs/<run_folder>/

This is the caller-allocation counterpart of ``rescore_from_gcs.py``: where
that module re-runs ``score_company()`` over the persisted ``scoring_inputs``,
this one leaves every score/tier untouched and only changes *which cold
caller* each company is assigned to. It reuses that module's GCS CLI
plumbing (``download_current_run`` / ``write_rescored_run`` /
``upload_rescored_run`` — same three-file layout: ``companies.list.json``,
``company-details-*.json``, manifest) verbatim.

Assignment mirrors the export pipeline exactly
-----------------------------------------------
``export_lead_prioritizer_to_lovable_json.export_workbook_to_lovable_json``
sorts companies by score descending and assigns callers round-robin by rank::

    caller = cold_callers[(rank - 1) % len(cold_callers)]

storing ``assigned_cold_caller`` + ``assigned_cold_caller_rank`` on both the
list item and the detail record. This module reproduces that formula. By
default it **preserves the existing ``assigned_cold_caller_rank``** already
stored at export time (so the allocation is identical to what a fresh export
would have produced for the same companies with a different caller pool) — it
does not need, and never re-derives, the raw score-precedence fields. Pass
``rerank_by_score=True`` to instead re-derive the ranking from each company's
current ``commercial_fit_score`` (useful after a re-score changed the scores),
tie-breaking on the previous rank then ``company_id`` for determinism.

It never touches ``current/`` or any existing run — promoting a reallocation
to ``current`` (the live Company Hub read path) is a deliberate, separate step
performed by the operator afterwards, so a bad reallocation always has a
fallback.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lovable_gcs_upload import DEFAULT_GCS_BUCKET
from rescore_from_gcs import (
    CURRENT_MANIFEST_FILENAME,
    LIST_FILENAME,
    download_current_run,
    gcs_current_dir,
    gcs_run_dir,
    list_country_folders,
    list_current_files,
    resolve_gcs_tool,
    upload_rescored_run,
    write_rescored_run,
)

REALLOCATE_SCHEMA_VERSION = 1

# Re-exported so callers/tests get the whole surface from one module even
# though the GCS plumbing lives in rescore_from_gcs.
__all__ = [
    "REALLOCATE_SCHEMA_VERSION",
    "normalize_cold_callers",
    "existing_cold_callers",
    "caller_distribution",
    "compute_caller_ranks",
    "assign_callers",
    "reassign_list_items",
    "reassign_detail_record",
    "reassign_details_bucket",
    "reallocation_movers",
    "build_reallocate_manifest",
    "build_reallocated_run",
    "default_reallocate_run_folder",
    "write_reallocated_run",
    "upload_reallocated_run",
    "reallocate_country",
    "reallocate_all_countries",
    # re-exported GCS plumbing
    "download_current_run",
    "list_country_folders",
    "list_current_files",
    "resolve_gcs_tool",
    "gcs_current_dir",
    "gcs_run_dir",
]

# =============================================================================
# Pure reallocation logic — no I/O, no subprocess
# =============================================================================


def _to_float(value: object) -> Optional[float]:
    """Best-effort float coercion; ``None`` when not numeric (mirrors the
    exporter's ``to_float`` for score sorting, kept local so this module has
    no dependency on the giant exporter)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _existing_rank(item: dict) -> Optional[int]:
    """The ``assigned_cold_caller_rank`` stored at export time, as a positive
    int, or ``None`` when absent/invalid."""
    rank = item.get("assigned_cold_caller_rank")
    if isinstance(rank, bool):
        return None
    if isinstance(rank, int) and rank >= 1:
        return rank
    if isinstance(rank, float) and rank >= 1 and rank.is_integer():
        return int(rank)
    return None


def normalize_cold_callers(cold_callers: "list[str] | None") -> list[str]:
    """Trim/blank-drop a caller list, preserving order and de-duplicating
    (first occurrence wins) — the same shape the exporter expects, so an
    empty result is a caller-required error at the call site."""
    seen: set[str] = set()
    result: list[str] = []
    for caller in cold_callers or []:
        name = (caller or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def existing_cold_callers(list_items: list[dict]) -> list[str]:
    """Distinct ``assigned_cold_caller`` values already present in a run, in
    rank order (rank 1 first) — the "before" caller pool for the audit/UI.
    Items with no rank sort last, by company_id, for a stable result."""
    ordered = sorted(
        list_items,
        key=lambda it: (
            _existing_rank(it) if _existing_rank(it) is not None else float("inf"),
            str(it.get("company_id") or ""),
        ),
    )
    seen: set[str] = set()
    callers: list[str] = []
    for item in ordered:
        caller = (item.get("assigned_cold_caller") or "").strip()
        if caller and caller not in seen:
            seen.add(caller)
            callers.append(caller)
    return callers


def caller_distribution(list_items: list[dict]) -> dict:
    """``assigned_cold_caller -> count`` across list items — the before/after
    workload histogram. ``None``/blank callers are counted under the empty
    string so nothing silently vanishes from the totals."""
    dist: dict = {}
    for item in list_items:
        caller = (item.get("assigned_cold_caller") or "").strip()
        dist[caller] = dist.get(caller, 0) + 1
    return dist


def compute_caller_ranks(
    list_items: list[dict], *, rerank_by_score: bool = False,
) -> dict:
    """``company_id -> rank`` for the round-robin assignment.

    ``rerank_by_score=False`` (default) preserves each company's existing
    ``assigned_cold_caller_rank``. Companies whose stored rank is missing or
    invalid are given fresh ranks continuing after the current maximum, in
    ``company_id`` order, so every company ends up with a unique rank.

    ``rerank_by_score=True`` re-derives ranks purely from the current
    ``commercial_fit_score`` (descending), tie-breaking on the previous rank
    then ``company_id`` — mirrors the export pipeline's own
    sort-by-score-then-assign, so it stays correct after a re-score changed
    the scores.
    """
    if rerank_by_score:
        ordered = sorted(
            list_items,
            key=lambda it: (
                -(_to_float(it.get("commercial_fit_score")) or 0.0),
                _existing_rank(it) if _existing_rank(it) is not None else float("inf"),
                str(it.get("company_id") or ""),
            ),
        )
        return {
            str(item.get("company_id")): rank
            for rank, item in enumerate(ordered, start=1)
        }

    ranks: dict = {}
    unranked: list[dict] = []
    for item in list_items:
        rank = _existing_rank(item)
        if rank is None:
            unranked.append(item)
        else:
            ranks[str(item.get("company_id"))] = rank
    next_rank = (max(ranks.values()) + 1) if ranks else 1
    for item in sorted(unranked, key=lambda it: str(it.get("company_id") or "")):
        ranks[str(item.get("company_id"))] = next_rank
        next_rank += 1
    return ranks


def assign_callers(
    list_items: list[dict],
    cold_callers: list[str],
    *,
    rerank_by_score: bool = False,
) -> dict:
    """``company_id -> (caller, rank)`` for a new caller pool, using the
    exact export formula ``caller = cold_callers[(rank - 1) % len]``.

    Raises ``ValueError`` if ``cold_callers`` is empty — a reallocation with
    no callers would leave every company unassigned, which the export
    validator rejects downstream.
    """
    callers = normalize_cold_callers(cold_callers)
    if not callers:
        raise ValueError("At least one cold caller is required.")
    ranks = compute_caller_ranks(list_items, rerank_by_score=rerank_by_score)
    return {
        cid: (callers[(rank - 1) % len(callers)], rank)
        for cid, rank in ranks.items()
    }


def reassign_list_items(list_items: list[dict], assignment: dict) -> list[dict]:
    """New list with each item's ``assigned_cold_caller`` /
    ``assigned_cold_caller_rank`` replaced from ``assignment``. Input items
    are never mutated; a company_id missing from ``assignment`` passes
    through unchanged rather than being dropped."""
    updated = []
    for item in list_items:
        new_item = dict(item)
        entry = assignment.get(str(item.get("company_id")))
        if entry is not None:
            caller, rank = entry
            new_item["assigned_cold_caller"] = caller
            new_item["assigned_cold_caller_rank"] = rank
        updated.append(new_item)
    return updated


def reassign_detail_record(
    detail: dict, caller: str, rank: int, *, now_iso: str, rerank_by_score: bool,
) -> dict:
    """Return a NEW detail record with the new caller/rank and a
    ``caller_reallocation_audit`` block preserving the previous values —
    ``detail`` is never mutated. Score/tier and every other field are left
    exactly as-is (this is a reallocation, not a re-score)."""
    new_detail = dict(detail)
    new_detail["caller_reallocation_audit"] = {
        "schema_version": REALLOCATE_SCHEMA_VERSION,
        "reallocated_at": now_iso,
        "rerank_by_score": rerank_by_score,
        "previous_cold_caller": detail.get("assigned_cold_caller"),
        "previous_cold_caller_rank": detail.get("assigned_cold_caller_rank"),
        "assigned_cold_caller": caller,
        "assigned_cold_caller_rank": rank,
    }
    new_detail["assigned_cold_caller"] = caller
    new_detail["assigned_cold_caller_rank"] = rank
    return new_detail


def reassign_details_bucket(
    bucket: dict, assignment: dict, *, now_iso: str, rerank_by_score: bool,
) -> dict:
    """Reassign every ``company_id -> detail`` record in one
    ``company-details-*.json`` bucket. Returns a NEW dict. A company_id
    absent from ``assignment`` is carried over unchanged (no audit block)."""
    result = {}
    for company_id, detail in bucket.items():
        entry = assignment.get(str(company_id))
        if entry is None:
            result[company_id] = dict(detail)
            continue
        caller, rank = entry
        result[company_id] = reassign_detail_record(
            detail, caller, rank, now_iso=now_iso, rerank_by_score=rerank_by_score)
    return result


def reallocation_movers(
    original_list_items: list[dict], assignment: dict,
) -> list[dict]:
    """Companies whose ``assigned_cold_caller`` changed, for the before/after
    audit table. Sorted by new rank ascending. Companies missing from
    ``assignment`` or whose caller is unchanged are excluded."""
    movers = []
    for item in original_list_items:
        cid = str(item.get("company_id"))
        entry = assignment.get(cid)
        if entry is None:
            continue
        new_caller, new_rank = entry
        old_caller = item.get("assigned_cold_caller")
        if old_caller == new_caller:
            continue
        movers.append({
            "company_id": cid,
            "company_name": item.get("company_name", ""),
            "commercial_fit_score": item.get("commercial_fit_score"),
            "commercial_tier": item.get("commercial_tier"),
            "caller_before": old_caller,
            "caller_after": new_caller,
            "rank": new_rank,
        })
    movers.sort(key=lambda m: (m["rank"], m["company_id"]))
    return movers


def build_reallocate_manifest(
    *,
    country_folder: str,
    source_current_manifest: "dict | None",
    run_folder: str,
    original_list_items: list[dict],
    rescaled_list_items: list[dict],
    new_cold_callers: list[str],
    rerank_by_score: bool,
    generated_at: str,
    assignment: dict,
) -> dict:
    """Manifest for the new run folder — analogous to the re-score manifest,
    but recording the caller reallocation: the before/after caller pools and
    workload distributions, how many companies actually moved, and whether
    the ranking was re-derived from score."""
    n_moved = len(reallocation_movers(original_list_items, assignment))
    return {
        "schema_version": REALLOCATE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "country_folder": country_folder,
        "run_folder": run_folder,
        "source_current_manifest": source_current_manifest,
        "rerank_by_score": rerank_by_score,
        "previous_cold_callers": existing_cold_callers(original_list_items),
        "cold_callers": new_cold_callers,
        "companies_total": len(rescaled_list_items),
        "companies_reallocated": n_moved,
        "companies_unchanged": len(rescaled_list_items) - n_moved,
        "caller_distribution_before": caller_distribution(original_list_items),
        "caller_distribution_after": caller_distribution(rescaled_list_items),
        "promoted_to_current": False,
    }


def default_reallocate_run_folder(now: "datetime | None" = None) -> str:
    """Default GCS run folder for a reallocation run:
    ``YYYY-MM-DD_reallocate``."""
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%d')}_reallocate"


def build_reallocated_run(
    current: dict,
    cold_callers: list[str],
    *,
    country_folder: str,
    run_folder: str,
    now_iso: str,
    rerank_by_score: bool = False,
) -> dict:
    """Pure, no-I/O core of a reallocation: turn an already-loaded ``current``
    bundle (the ``dict`` returned by ``download_current_run`` — or an
    equivalent one built in memory) into the new run's
    ``{"list_items", "detail_files", "manifest"}``.

    Kept separate from ``reallocate_country`` so an interactive tool can
    re-run it on every caller-list edit without re-hitting GCS.
    """
    callers = normalize_cold_callers(cold_callers)
    if not callers:
        raise ValueError("At least one cold caller is required.")

    original_list_items = current["list_items"]
    assignment = assign_callers(
        original_list_items, callers, rerank_by_score=rerank_by_score)

    new_list_items = reassign_list_items(original_list_items, assignment)
    new_detail_files = {
        filename: reassign_details_bucket(
            bucket, assignment, now_iso=now_iso, rerank_by_score=rerank_by_score)
        for filename, bucket in current["detail_files"].items()
    }

    manifest = build_reallocate_manifest(
        country_folder=country_folder,
        source_current_manifest=current.get("manifest"),
        run_folder=run_folder,
        original_list_items=original_list_items,
        rescaled_list_items=new_list_items,
        new_cold_callers=callers,
        rerank_by_score=rerank_by_score,
        generated_at=now_iso,
        assignment=assignment,
    )
    return {
        "list_items": new_list_items,
        "detail_files": new_detail_files,
        "manifest": manifest,
    }


# =============================================================================
# GCS I/O — reuses rescore_from_gcs's write/upload (identical file layout)
# =============================================================================


def write_reallocated_run(reallocated_run: dict, out_dir) -> Path:
    """Write a ``build_reallocated_run`` result to local JSON files. Thin
    alias over ``rescore_from_gcs.write_rescored_run`` — the on-disk layout
    (``companies.list.json`` + ``company-details-*.json`` + manifest) is
    identical, so there is deliberately no second copy of that logic."""
    return write_rescored_run(reallocated_run, out_dir)


def upload_reallocated_run(
    out_dir, bucket: str, country_folder: str, run_folder: str,
) -> list[dict]:
    """Upload a written reallocation run to
    ``gs://<bucket>/<country_folder>/runs/<run_folder>/``. Thin alias over
    ``rescore_from_gcs.upload_rescored_run`` — never touches ``current/``."""
    return upload_rescored_run(out_dir, bucket, country_folder, run_folder)


def reallocate_country(
    bucket: str,
    country_folder: str,
    cold_callers: list[str],
    *,
    run_folder: "str | None" = None,
    work_dir: "str | Path | None" = None,
    upload: bool = True,
    rerank_by_score: bool = False,
    now: "datetime | None" = None,
) -> dict:
    """End-to-end caller reallocation for one country folder.

    Downloads ``<country_folder>/current/``, reassigns every company's cold
    caller from ``cold_callers``, and writes the result to
    ``gs://<bucket>/<country_folder>/runs/<run_folder>/`` — a brand-new run
    folder, never ``current/`` and never an existing run. Returns the new
    run's manifest (with an added ``upload_results`` list; empty when
    ``upload=False``).
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_folder = run_folder or default_reallocate_run_folder(now)

    cleanup = work_dir is None
    staging = Path(work_dir) if work_dir else Path(
        tempfile.mkdtemp(prefix="reallocate_from_gcs_"))
    try:
        current = download_current_run(bucket, country_folder, staging / "current")
        reallocated_run = build_reallocated_run(
            current, cold_callers, country_folder=country_folder,
            run_folder=run_folder, now_iso=now_iso, rerank_by_score=rerank_by_score,
        )
        out_dir = write_reallocated_run(reallocated_run, staging / "out")

        upload_results: list[dict] = []
        if upload:
            upload_results = upload_reallocated_run(
                out_dir, bucket, country_folder, run_folder)

        manifest = reallocated_run["manifest"]
        manifest["upload_results"] = upload_results
        manifest["local_output_dir"] = None if cleanup else str(out_dir)
        return manifest
    finally:
        if cleanup:
            shutil.rmtree(staging, ignore_errors=True)


def reallocate_all_countries(
    bucket: str,
    cold_callers: list[str],
    *,
    countries: "list[str] | None" = None,
    run_folder: "str | None" = None,
    upload: bool = True,
    rerank_by_score: bool = False,
    now: "datetime | None" = None,
) -> dict:
    """Reallocate every requested country folder (or every folder currently
    in the bucket when ``countries`` is ``None``) to the same caller pool.
    Returns ``{country_folder: manifest_or_error}`` — one country failing
    never stops the others."""
    now = now or datetime.now(timezone.utc)
    run_folder = run_folder or default_reallocate_run_folder(now)
    if countries is None:
        countries = list_country_folders(bucket)

    results: dict = {}
    for country_folder in countries:
        try:
            results[country_folder] = reallocate_country(
                bucket, country_folder, cold_callers,
                run_folder=run_folder, upload=upload,
                rerank_by_score=rerank_by_score, now=now,
            )
        except Exception as exc:
            results[country_folder] = {"error": str(exc)}
    return results


# =============================================================================
# CLI
# =============================================================================


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=DEFAULT_GCS_BUCKET)
    ap.add_argument(
        "--countries", default="all",
        help="Comma-separated country-folder slugs, or 'all' to reallocate "
             "every folder currently in the bucket.")
    ap.add_argument(
        "--cold-callers", required=True,
        help='Comma-separated new caller pool, e.g. "Vanessa,Francesca,Lorenzo".')
    ap.add_argument(
        "--rerank-by-score", action="store_true",
        help="Re-derive the ranking from each company's current "
             "commercial_fit_score instead of preserving the export-time rank "
             "(use after a re-score changed the scores).")
    ap.add_argument(
        "--run-folder", default=None,
        help="Target run folder name (default: YYYY-MM-DD_reallocate).")
    ap.add_argument(
        "--work-dir", default=None,
        help="Local staging directory to keep after the run (default: an "
             "auto-cleaned temp dir).")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Reallocate and write local output only; never uploads to GCS.")
    args = ap.parse_args()

    cold_callers = normalize_cold_callers(args.cold_callers.split(","))
    if not cold_callers:
        print("At least one cold caller is required (--cold-callers).", file=sys.stderr)
        sys.exit(2)
    run_folder = args.run_folder or default_reallocate_run_folder()

    if args.countries.strip().lower() == "all":
        countries = list_country_folders(args.bucket)
        if not countries:
            print(f"No country folders found in gs://{args.bucket}/", file=sys.stderr)
            sys.exit(1)
    else:
        countries = [c.strip() for c in args.countries.split(",") if c.strip()]

    print(f"\n{'='*72}")
    print("Reallocate cold callers from GCS run")
    print(f"  bucket        : {args.bucket}")
    print(f"  countries     : {', '.join(countries)}")
    print(f"  cold callers  : {', '.join(cold_callers)}")
    print(f"  rerank by score: {args.rerank_by_score}")
    print(f"  run folder    : {run_folder}")
    print(f"  dry run       : {args.dry_run}")
    print(f"{'='*72}\n")

    exit_code = 0
    for country_folder in countries:
        try:
            manifest = reallocate_country(
                args.bucket, country_folder, cold_callers,
                run_folder=run_folder, work_dir=args.work_dir,
                upload=not args.dry_run, rerank_by_score=args.rerank_by_score,
            )
        except Exception as exc:
            print(f"  ERROR                    {country_folder}: {exc}")
            exit_code = 1
            continue
        n_failed = sum(1 for r in manifest.get("upload_results", []) if not r.get("success"))
        status = "OK" if not args.dry_run and n_failed == 0 else (
            "DRY RUN" if args.dry_run else f"{n_failed} upload(s) FAILED")
        print(
            f"  {status:<24} {country_folder}: "
            f"{manifest['companies_reallocated']} of {manifest['companies_total']} "
            f"companies moved caller -> "
            f"gs://{args.bucket}/{country_folder}/runs/{run_folder}/")
        if n_failed:
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
