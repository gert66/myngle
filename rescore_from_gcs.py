"""
rescore_from_gcs.py — Offline re-scoring of already-exported GCS signals
=========================================================================
Reads the *current* run for one or more country folders in the Lovable GCS
bucket (``myngle-company-data-104527058436`` by default — see
``lovable_gcs_upload.py``), re-runs ``commercial_fit_scoring.score_company()``
over each company's persisted ``scoring_inputs`` block (see
``export_lead_prioritizer_to_lovable_json._build_scoring_inputs``) with a
caller-supplied ``params`` dict (coefficients, model_weight, size_weight,
sigmoid_k, tier_thresholds, scoring_profile — see
``commercial_fit_scoring.SCORING_PROFILES`` for the shape), and writes the
result to a brand-new run folder:

    gs://<bucket>/<country_folder>/runs/<run_folder>/

It never touches ``current/`` or any existing run — promoting a re-score to
``current`` (the live Company Hub read path) is a deliberate, separate step
performed by the operator afterwards, so a bad re-score always has a
fallback.

Uses the same ``gcloud storage``/``gsutil`` CLI approach as
``lovable_gcs_upload.py`` — no ``google-cloud-storage`` Python dependency,
no ``shell=True``, no secrets ever printed.

Missing vs. genuine-zero signals
---------------------------------
A signal that was never enriched for a company is stored in
``scoring_inputs.signals`` as an explicit ``None`` (not coerced to 0.0) — see
``_build_scoring_inputs``'s docstring. This module reads that block straight
through into ``score_company()`` without ever turning a ``None`` into a
number, so ``score_company``'s own missing-data notes and
``missing_scoring_fields`` audit stay accurate for the re-scored run too.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from commercial_fit_scoring import score_company
from lovable_gcs_upload import DEFAULT_GCS_BUCKET

RESCORE_SCHEMA_VERSION = 1

CURRENT_MANIFEST_FILENAME = "export_manifest.json"
LIST_FILENAME = "companies.list.json"
_DETAIL_FILENAME_RE = re.compile(r"^company-details-.*\.json$")

# =============================================================================
# Pure re-scoring logic — no I/O, no subprocess
# =============================================================================


def rehydrate_scoring_row(scoring_inputs: dict) -> dict:
    """Rebuild the row shape ``score_company()`` expects from a persisted
    ``scoring_inputs`` block: the raw sig_*/ti_* signals plus
    ``employee_range`` under the same key ``score_company``'s size resolver
    reads (see ``commercial_fit_scoring._resolve_size_score``).

    A signal stored as ``None`` (never enriched) is passed through
    unchanged — ``score_company``'s own ``_is_missing()`` check needs to see
    it as missing, not as a genuine zero.
    """
    row = dict(scoring_inputs.get("signals") or {})
    row["employee_range"] = scoring_inputs.get("employee_range")
    return row


def rescore_detail_record(detail: dict, params: dict, *, now_iso: str) -> dict:
    """Re-score one company-details record from its persisted
    ``scoring_inputs``. Returns a NEW dict — ``detail`` is never mutated.

    The new ``final_commercial_fit_score``/``commercial_tier`` become the
    record's ``commercial_fit_score``/``commercial_tier`` fields (these ARE
    the new numbers — that is the point of a re-score run); a
    ``rescore_audit`` block alongside them keeps the previous values, the
    params used, and which signals were missing, for a full before/after
    trail.
    """
    scoring_inputs = detail.get("scoring_inputs")
    if not scoring_inputs or "signals" not in scoring_inputs:
        raise ValueError(
            f"detail record {detail.get('company_id')!r} has no scoring_inputs "
            "block to re-score from"
        )

    row = rehydrate_scoring_row(scoring_inputs)
    result = score_company(row, params=params)

    missing_raw = result.get("missing_scoring_fields") or ""
    missing_signals = [f.strip() for f in missing_raw.split(",") if f.strip()]

    new_detail = dict(detail)
    new_detail["commercial_fit_score"] = result["final_commercial_fit_score"]
    new_detail["commercial_tier"] = result["commercial_tier"]
    new_detail["rescore_audit"] = {
        "schema_version": RESCORE_SCHEMA_VERSION,
        "rescored_at": now_iso,
        "params": params or {},
        "previous_commercial_fit_score": detail.get("commercial_fit_score"),
        "previous_commercial_tier": detail.get("commercial_tier"),
        "final_commercial_fit_score": result["final_commercial_fit_score"],
        "commercial_tier": result["commercial_tier"],
        "missing_scoring_signals": missing_signals,
        "scoring_notes": result["scoring_notes"],
    }
    return new_detail


def rescore_details_bucket(bucket: dict, params: dict, *, now_iso: str) -> dict:
    """Re-score every ``company_id -> detail`` record in one
    ``company-details-*.json`` bucket file. Returns a NEW dict."""
    return {
        company_id: rescore_detail_record(detail, params, now_iso=now_iso)
        for company_id, detail in bucket.items()
    }


def rescore_list_items(list_items: list[dict], rescored_by_id: dict) -> list[dict]:
    """Mirror the new ``commercial_fit_score``/``commercial_tier`` from the
    rescored detail records onto the lightweight ``companies.list.json``
    entries, so list and detail stay consistent in the new run folder. An
    item whose ``company_id`` has no rescored counterpart is passed through
    unchanged rather than dropped."""
    updated = []
    for item in list_items:
        new_item = dict(item)
        rescored = rescored_by_id.get(item.get("company_id"))
        if rescored is not None:
            new_item["commercial_fit_score"] = rescored["commercial_fit_score"]
            new_item["commercial_tier"] = rescored["commercial_tier"]
        updated.append(new_item)
    return updated


def tier_distribution(details_by_id: dict) -> dict:
    """``commercial_tier -> count`` across a set of detail records, used for
    the before/after audit in the rescore manifest."""
    dist: dict = {}
    for detail in details_by_id.values():
        tier = detail.get("commercial_tier")
        dist[tier] = dist.get(tier, 0) + 1
    return dist


def build_rescore_manifest(
    *,
    country_folder: str,
    source_current_manifest: "dict | None",
    params: dict,
    run_folder: str,
    original_details_by_id: dict,
    rescored_details_by_id: dict,
    generated_at: str,
) -> dict:
    """Manifest for the new run folder — analogous to
    ``export_lead_prioritizer_to_lovable_json``'s ``export_manifest.json``,
    but for a re-score: records the params used and a before/after tier
    distribution instead of re-running enrichment counts."""
    return {
        "schema_version": RESCORE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "country_folder": country_folder,
        "run_folder": run_folder,
        "source_current_manifest": source_current_manifest,
        "params": params or {},
        "companies_rescored": len(rescored_details_by_id),
        "tier_distribution_before": tier_distribution(original_details_by_id),
        "tier_distribution_after": tier_distribution(rescored_details_by_id),
        "promoted_to_current": False,
    }


def default_rescore_run_folder(now: "datetime | None" = None) -> str:
    """Default GCS run folder for a re-score run: ``YYYY-MM-DD_rescore``."""
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%d')}_rescore"


# =============================================================================
# GCS I/O — same gcloud storage/gsutil CLI approach as lovable_gcs_upload.py
# =============================================================================


def resolve_gcs_tool() -> "Optional[list[str]]":
    """argv prefix used for ``ls``/``cp`` alike: prefers ``gcloud storage``,
    falls back to ``gsutil``. Returns ``None`` if neither is on PATH.

    Uses the exact ``shutil.which`` path as ``command[0]`` (Windows-safe
    ``.cmd`` shim handling), same as ``lovable_gcs_upload.resolve_gcs_upload_tool``.
    """
    gcloud_path = shutil.which("gcloud")
    if gcloud_path:
        return [gcloud_path, "storage"]
    gsutil_path = shutil.which("gsutil")
    if gsutil_path:
        return [gsutil_path]
    return None


def gcs_current_dir(bucket: str, country_folder: str) -> str:
    return f"gs://{bucket}/{country_folder}/current"


def gcs_run_dir(bucket: str, country_folder: str, run_folder: str) -> str:
    return f"gs://{bucket}/{country_folder}/runs/{run_folder}"


def list_country_folders(bucket: str) -> list[str]:
    """Country-folder slugs currently present at the bucket root. Returns an
    empty list (never raises) if no CLI tool is on PATH or the listing
    fails."""
    tool = resolve_gcs_tool()
    if tool is None:
        return []
    try:
        proc = subprocess.run(
            [*tool, "ls", f"gs://{bucket}/"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    pattern = re.compile(rf"^gs://{re.escape(bucket)}/([^/]+)/$")
    folders = []
    for line in proc.stdout.splitlines():
        m = pattern.match(line.strip())
        if m:
            folders.append(m.group(1))
    return folders


def list_current_files(bucket: str, country_folder: str) -> list[str]:
    """Filenames present in ``<country_folder>/current/`` right now. Returns
    an empty list (never raises) if no CLI tool is on PATH or the listing
    fails."""
    tool = resolve_gcs_tool()
    if tool is None:
        return []
    current_dir = gcs_current_dir(bucket, country_folder)
    try:
        proc = subprocess.run(
            [*tool, "ls", f"{current_dir}/"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    prefix = f"{current_dir}/"
    names = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith(prefix) and not line.endswith("/"):
            names.append(line[len(prefix):])
    return names


def download_file(tool: list[str], source: str, local_path: str) -> dict:
    """Download one GCS object via subprocess (no ``shell=True``). Never
    raises — any failure comes back as ``{"success": False, ...}``."""
    try:
        proc = subprocess.run(
            [*tool, "cp", source, local_path],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:
        return {
            "success": False, "source": source, "local_path": local_path,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "success": proc.returncode == 0,
        "source": source, "local_path": local_path,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:],
    }


def upload_file(tool: list[str], local_path: str, destination: str) -> dict:
    """Upload one local file via subprocess (no ``shell=True``). Mirrors
    ``lovable_gcs_upload.upload_file`` — verifies the local file exists
    before shelling out; never raises."""
    if not Path(local_path).exists():
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": f"Local file not found: {local_path}",
        }
    try:
        proc = subprocess.run(
            [*tool, "cp", local_path, destination],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "success": proc.returncode == 0,
        "local_path": local_path, "destination": destination,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:],
    }


def download_current_run(bucket: str, country_folder: str, work_dir) -> dict:
    """Download ``<country_folder>/current/``'s manifest, ``companies.list.json``
    and every ``company-details-*.json`` into ``work_dir``.

    Returns ``{"manifest": dict|None, "list_items": list, "detail_files":
    {filename: dict}}``. Raises ``RuntimeError`` (with a clear message, no
    secrets) if no GCS CLI tool is available, the current folder is empty,
    or any single download fails — a partial re-score input is worse than
    no re-score at all.
    """
    tool = resolve_gcs_tool()
    if tool is None:
        raise RuntimeError(
            "Neither gcloud nor gsutil was found on PATH. Install/authenticate "
            "the Google Cloud SDK and try again."
        )

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    current_dir = gcs_current_dir(bucket, country_folder)

    filenames = list_current_files(bucket, country_folder)
    relevant = [
        f for f in filenames
        if f in (CURRENT_MANIFEST_FILENAME, LIST_FILENAME) or _DETAIL_FILENAME_RE.match(f)
    ]
    if not relevant:
        raise RuntimeError(
            f"No re-scorable files found under {current_dir}/ — is "
            f"{country_folder!r} a valid country folder with a published "
            "current run?"
        )

    downloaded: dict = {}
    for filename in relevant:
        local_path = work_dir / filename
        result = download_file(tool, f"{current_dir}/{filename}", str(local_path))
        if not result["success"]:
            raise RuntimeError(
                f"Failed to download {filename} from {current_dir}: "
                f"{result.get('stderr') or result.get('error')}"
            )
        downloaded[filename] = local_path

    manifest = None
    if CURRENT_MANIFEST_FILENAME in downloaded:
        manifest = json.loads(
            downloaded[CURRENT_MANIFEST_FILENAME].read_text(encoding="utf-8"))

    list_items = []
    if LIST_FILENAME in downloaded:
        list_items = json.loads(downloaded[LIST_FILENAME].read_text(encoding="utf-8"))

    detail_files = {
        name: json.loads(path.read_text(encoding="utf-8"))
        for name, path in downloaded.items()
        if _DETAIL_FILENAME_RE.match(name)
    }
    return {"manifest": manifest, "list_items": list_items, "detail_files": detail_files}


def rescore_country(
    bucket: str,
    country_folder: str,
    params: dict,
    *,
    run_folder: "str | None" = None,
    work_dir: "str | Path | None" = None,
    upload: bool = True,
    now: "datetime | None" = None,
) -> dict:
    """End-to-end re-score for one country folder.

    Downloads ``<country_folder>/current/``, re-scores every company's
    persisted ``scoring_inputs`` with ``params``, and writes the result to
    ``gs://<bucket>/<country_folder>/runs/<run_folder>/`` — a brand-new run
    folder, never ``current/`` and never an existing run. Returns the new
    run's manifest (with an added ``upload_results`` list; empty when
    ``upload=False``).
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_folder = run_folder or default_rescore_run_folder(now)

    cleanup = work_dir is None
    staging = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="rescore_from_gcs_"))
    try:
        current = download_current_run(bucket, country_folder, staging / "current")

        rescored_details_by_file = {
            filename: rescore_details_bucket(bucket_dict, params, now_iso=now_iso)
            for filename, bucket_dict in current["detail_files"].items()
        }
        original_by_id = {
            cid: detail
            for bucket_dict in current["detail_files"].values()
            for cid, detail in bucket_dict.items()
        }
        rescored_by_id = {
            cid: detail
            for bucket_dict in rescored_details_by_file.values()
            for cid, detail in bucket_dict.items()
        }
        new_list_items = rescore_list_items(current["list_items"], rescored_by_id)

        manifest = build_rescore_manifest(
            country_folder=country_folder,
            source_current_manifest=current["manifest"],
            params=params,
            run_folder=run_folder,
            original_details_by_id=original_by_id,
            rescored_details_by_id=rescored_by_id,
            generated_at=now_iso,
        )

        out_dir = staging / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / LIST_FILENAME).write_text(
            json.dumps(new_list_items, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        for filename, bucket_dict in rescored_details_by_file.items():
            (out_dir / filename).write_text(
                json.dumps(bucket_dict, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        (out_dir / CURRENT_MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

        upload_results: list[dict] = []
        if upload:
            tool = resolve_gcs_tool()
            if tool is None:
                raise RuntimeError(
                    "Neither gcloud nor gsutil was found on PATH. "
                    "Install/authenticate the Google Cloud SDK and try again."
                )
            run_dir = gcs_run_dir(bucket, country_folder, run_folder)
            for path in sorted(out_dir.iterdir()):
                upload_results.append(
                    upload_file(tool, str(path), f"{run_dir}/{path.name}"))

        manifest["upload_results"] = upload_results
        manifest["local_output_dir"] = None if cleanup else str(out_dir)
        return manifest
    finally:
        if cleanup:
            shutil.rmtree(staging, ignore_errors=True)


def rescore_all_countries(
    bucket: str,
    params: dict,
    *,
    countries: "list[str] | None" = None,
    run_folder: "str | None" = None,
    upload: bool = True,
    now: "datetime | None" = None,
) -> dict:
    """Re-score every requested country folder (or every folder currently in
    the bucket when ``countries`` is ``None``). Returns
    ``{country_folder: manifest_or_error}`` — one country failing never stops
    the others."""
    now = now or datetime.now(timezone.utc)
    run_folder = run_folder or default_rescore_run_folder(now)
    if countries is None:
        countries = list_country_folders(bucket)

    results: dict = {}
    for country_folder in countries:
        try:
            results[country_folder] = rescore_country(
                bucket, country_folder, params,
                run_folder=run_folder, upload=upload, now=now,
            )
        except Exception as exc:
            results[country_folder] = {"error": str(exc)}
    return results


# =============================================================================
# CLI
# =============================================================================


def _load_params(args) -> dict:
    if args.params_json:
        return json.loads(Path(args.params_json).read_text(encoding="utf-8"))
    params: dict = {}
    if args.scoring_profile:
        params["scoring_profile"] = args.scoring_profile
    return params


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=DEFAULT_GCS_BUCKET)
    ap.add_argument(
        "--countries", default="all",
        help="Comma-separated country-folder slugs, or 'all' to re-score "
             "every folder currently in the bucket.")
    ap.add_argument(
        "--params-json",
        help="Path to a JSON file with score_company() params (coefficients, "
             "model_weight, size_weight, sigmoid_k, tier_thresholds, "
             "scoring_profile — see commercial_fit_scoring.SCORING_PROFILES).")
    ap.add_argument(
        "--scoring-profile", default=None,
        help='Shortcut for {"scoring_profile": <name>} when no --params-json '
             "is given.")
    ap.add_argument(
        "--run-folder", default=None,
        help="Target run folder name (default: YYYY-MM-DD_rescore).")
    ap.add_argument(
        "--work-dir", default=None,
        help="Local staging directory to keep after the run (default: an "
             "auto-cleaned temp dir).")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Re-score and write local output only; never uploads to GCS.")
    args = ap.parse_args()

    params = _load_params(args)
    run_folder = args.run_folder or default_rescore_run_folder()

    if args.countries.strip().lower() == "all":
        countries = list_country_folders(args.bucket)
        if not countries:
            print(f"No country folders found in gs://{args.bucket}/", file=sys.stderr)
            sys.exit(1)
    else:
        countries = [c.strip() for c in args.countries.split(",") if c.strip()]

    print(f"\n{'='*72}")
    print("Re-score from GCS signals")
    print(f"  bucket      : {args.bucket}")
    print(f"  countries   : {', '.join(countries)}")
    print(f"  run folder  : {run_folder}")
    print(f"  params      : {json.dumps(params)}")
    print(f"  dry run     : {args.dry_run}")
    print(f"{'='*72}\n")

    exit_code = 0
    for country_folder in countries:
        try:
            manifest = rescore_country(
                args.bucket, country_folder, params,
                run_folder=run_folder, work_dir=args.work_dir,
                upload=not args.dry_run,
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
            f"{manifest['companies_rescored']} companies rescored -> "
            f"gs://{args.bucket}/{country_folder}/runs/{run_folder}/")
        if n_failed:
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
