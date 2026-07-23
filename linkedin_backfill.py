"""
linkedin_backfill.py
--------------------
Backfill genuine company ``/company/`` LinkedIn URLs into the GCS ``current``
detail records, so the Company Hub's contacts panel can show a direct
**"View on LinkedIn"** link (landing straight on the company page, where the
Lusha browser extension activates) instead of falling back to a name search.

Why this exists
    The live Italy dataset rarely carries a real company LinkedIn URL: the
    ``linkedin_url`` that IS present is frequently a scraped job/post/profile
    link (Audi Italia's pointed at an unrelated vacancy), which the frontend
    now correctly rejects — so almost every company falls back to
    "Search on LinkedIn". Lusha's ``/v3/companies/search`` returns the real
    company page under ``socialLinks.linkedin`` and that endpoint works even
    while the decision-makers endpoint is down, so it is the cheap way to
    fill the gap.

Design
    * The URL rule is IDENTICAL to the frontend's
      ``src/lib/linkedin-search.ts:isLinkedInCompanyUrl`` — only ``/company/``
      and ``/showcase/`` pages on ``linkedin.com`` (or a country subdomain)
      are accepted. A URL Lusha returns that isn't a company page is logged
      and rejected, never written.
    * The pure layer (validation + record transform) does no I/O and is fully
      unit-tested; the network lookup is injected, so tests never hit Lusha.
    * The CLI is DRY-RUN by default (no Lusha calls, just a report of what it
      *would* look up), resumable via a checkpoint file, and bounded by
      ``--limit`` — a full-country pass is an explicit, incremental decision,
      never a side effect. Going live (upload + promote to ``current/``) is a
      separate explicit step, reusing ``rescore_from_gcs``'s run tooling.

Credits
    One ``companies/search`` call per company that has a domain and no valid
    company URL yet. Companies already carrying a good ``/company/`` URL, or
    with no domain to search on, cost nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

# GCS + run tooling is reused wholesale from the re-score pipeline rather than
# re-implemented — same download/write/upload/promote semantics, same run
# shape ({"list_items", "detail_files", "manifest"}).
import rescore_from_gcs as rg
from rescore_from_gcs import (
    download_current_run,
    list_country_folders,
    promote_run_to_current,
    resolve_gcs_tool,
    upload_rescored_run,
    write_rescored_run,
)

BACKFILL_SCHEMA_VERSION = 1

# Status labels for every company the backfill considers. Exactly one applies
# per company; the manifest tallies them.
STATUS_ALREADY_VALID = "already_valid"        # kept a company URL it already had
STATUS_BACKFILLED = "backfilled"              # got a fresh /company/ URL from Lusha
STATUS_NO_DOMAIN = "no_domain"                # nothing to search Lusha on
STATUS_NO_LINKEDIN = "no_linkedin"            # Lusha returned no LinkedIn at all
STATUS_REJECTED_NOT_COMPANY = "rejected_not_company"  # Lusha URL wasn't a /company/ page
STATUS_SKIPPED_BUDGET = "skipped_budget"      # --limit reached before reaching this one


# ---------------------------------------------------------------------------
# Pure layer — no network, no GCS. All unit-tested.
# ---------------------------------------------------------------------------

def is_company_linkedin_url(url: object) -> bool:
    """True only when ``url`` is a real LinkedIn *company* page.

    Mirror of the frontend's ``isLinkedInCompanyUrl`` so the app and the
    backfill never disagree about what counts as a usable company link:
    accept ``/company/<slug>`` and ``/showcase/<slug>`` on ``linkedin.com``
    or a country subdomain (``it.linkedin.com`` …); reject job postings
    (``/jobs/``), feed posts (``/posts/``), personal profiles (``/in/``),
    schools (``/school/``), bare hosts, and anything that isn't LinkedIn.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return False
    parts = [p for p in parsed.path.split("/") if p]
    return len(parts) >= 2 and parts[0].lower() in ("company", "showcase")


def pick_detail_domain(detail: dict) -> str:
    """Best domain to search Lusha on for this detail record. Prefers an
    explicit ``domain`` field, then falls back to the website. Returns ""
    when the record has nothing searchable."""
    for key in ("domain", "company_domain", "website_url", "website"):
        val = detail.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def needs_backfill(detail: dict) -> bool:
    """Whether this record still needs a company URL. False when it already
    carries a valid ``/company/`` ``linkedin_url`` (nothing to spend a Lusha
    call on)."""
    return not is_company_linkedin_url(detail.get("linkedin_url"))


def apply_linkedin_url(
    detail: dict, fetched_url: str, *, now_iso: str,
) -> "tuple[dict, str]":
    """Given a URL fetched from Lusha for this record, decide what to do.

    Pure: no network. Returns ``(record, status)``. The record is a NEW dict
    with ``linkedin_url`` set and an audit trail ONLY when a genuine company
    URL was written; otherwise the original record is returned unchanged.

    * ""                     -> (detail, ``no_linkedin``)
    * a non-company URL      -> (detail, ``rejected_not_company``)
    * a real /company/ URL   -> (new_detail, ``backfilled``)
    """
    if not fetched_url:
        return detail, STATUS_NO_LINKEDIN
    if not is_company_linkedin_url(fetched_url):
        return detail, STATUS_REJECTED_NOT_COMPANY

    new_detail = dict(detail)
    new_detail["linkedin_url"] = fetched_url
    new_detail["linkedin_backfill_audit"] = {
        "schema_version": BACKFILL_SCHEMA_VERSION,
        "backfilled_at": now_iso,
        "source": "lusha_companies_search",
        "previous_linkedin_url": detail.get("linkedin_url") or "",
        "linkedin_url": fetched_url,
    }
    return new_detail, STATUS_BACKFILLED


# ---------------------------------------------------------------------------
# Orchestration — walks the detail buckets, spends the (bounded) lookup budget
# ---------------------------------------------------------------------------

class LookupBudget:
    """Mutable remaining-lookup counter shared across a whole country run so
    ``--limit`` caps the number of Lusha calls, not the number per file."""

    def __init__(self, limit: Optional[int]):
        # None == unlimited
        self.remaining = limit if limit is not None else -1

    def take(self) -> bool:
        """Consume one lookup slot; False when the budget is exhausted."""
        if self.remaining < 0:
            return True  # unlimited
        if self.remaining == 0:
            return False
        self.remaining -= 1
        return True


def backfill_details_bucket(
    bucket: dict,
    lookup: Callable[[str], str],
    *,
    now_iso: str,
    budget: LookupBudget,
    counters: dict,
    checkpoint: "Optional[dict]" = None,
) -> dict:
    """Backfill one ``company_id -> detail`` bucket. Returns a NEW dict.

    ``lookup`` maps a domain to a LinkedIn URL string (Lusha in production, a
    stub in tests). ``counters`` is a status->count dict mutated in place.
    ``checkpoint`` (optional) maps ``company_id -> {"status", "linkedin_url"}``
    for companies already resolved in a previous run — those are re-applied
    without spending a lookup.
    """
    out: dict = {}
    for company_id, detail in bucket.items():
        if not needs_backfill(detail):
            counters[STATUS_ALREADY_VALID] = counters.get(STATUS_ALREADY_VALID, 0) + 1
            out[company_id] = detail
            continue

        # Resume: a prior run already resolved this company — re-apply its
        # result without another Lusha call.
        if checkpoint and company_id in checkpoint:
            prior = checkpoint[company_id]
            record, status = apply_linkedin_url(
                detail, prior.get("linkedin_url", ""), now_iso=now_iso)
            counters[status] = counters.get(status, 0) + 1
            out[company_id] = record
            continue

        domain = pick_detail_domain(detail)
        if not domain:
            counters[STATUS_NO_DOMAIN] = counters.get(STATUS_NO_DOMAIN, 0) + 1
            out[company_id] = detail
            continue

        if not budget.take():
            counters[STATUS_SKIPPED_BUDGET] = counters.get(STATUS_SKIPPED_BUDGET, 0) + 1
            out[company_id] = detail
            continue

        fetched = lookup(domain) or ""
        record, status = apply_linkedin_url(detail, fetched, now_iso=now_iso)
        counters[status] = counters.get(status, 0) + 1
        if checkpoint is not None:
            checkpoint[company_id] = {"status": status, "linkedin_url": fetched if status == STATUS_BACKFILLED else ""}
        out[company_id] = record

    return out


def build_backfill_manifest(
    *,
    country_folder: str,
    source_current_manifest: "Optional[dict]",
    run_folder: str,
    counters: dict,
    generated_at: str,
) -> dict:
    """Manifest for a backfill run — same envelope shape the re-score/
    reallocate runs use, with backfill-specific counts."""
    total = sum(counters.values())
    return {
        "run_type": "linkedin_backfill",
        "schema_version": BACKFILL_SCHEMA_VERSION,
        "country_folder": country_folder,
        "run_folder": run_folder,
        "generated_at": generated_at,
        "companies_total": total,
        "companies_backfilled": counters.get(STATUS_BACKFILLED, 0),
        "companies_already_valid": counters.get(STATUS_ALREADY_VALID, 0),
        "companies_no_domain": counters.get(STATUS_NO_DOMAIN, 0),
        "companies_no_linkedin": counters.get(STATUS_NO_LINKEDIN, 0),
        "companies_rejected_not_company": counters.get(STATUS_REJECTED_NOT_COMPANY, 0),
        "companies_skipped_budget": counters.get(STATUS_SKIPPED_BUDGET, 0),
        "source_current_manifest": source_current_manifest or {},
        "promoted_to_current": False,
    }


def build_backfilled_run(
    current: dict,
    lookup: Callable[[str], str],
    *,
    country_folder: str,
    run_folder: str,
    now_iso: str,
    budget: LookupBudget,
    checkpoint: "Optional[dict]" = None,
) -> dict:
    """Pure-ish core: turn a loaded ``current`` bundle (from
    ``download_current_run``) into a new run ``{"list_items", "detail_files",
    "manifest"}``. ``list_items`` pass through unchanged — ``linkedin_url`` is
    a detail-only field the frontend reads off the detail record."""
    counters: dict = {}
    new_detail_files = {
        filename: backfill_details_bucket(
            bucket_dict, lookup, now_iso=now_iso, budget=budget,
            counters=counters, checkpoint=checkpoint)
        for filename, bucket_dict in current["detail_files"].items()
    }
    manifest = build_backfill_manifest(
        country_folder=country_folder,
        source_current_manifest=current.get("manifest"),
        run_folder=run_folder,
        counters=counters,
        generated_at=now_iso,
    )
    return {
        "list_items": current["list_items"],
        "detail_files": new_detail_files,
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# Lookup construction (the only network-touching part)
# ---------------------------------------------------------------------------

def make_lusha_lookup(sleep_seconds: float = 0.3) -> Callable[[str], str]:
    """A domain->URL lookup backed by the live Lusha company search, with a
    small pause between calls to stay well under rate limits. Imported lazily
    so a dry run never imports the client or needs ``LUSHA_API_KEY``."""
    import lusha_client

    def _lookup(domain: str) -> str:
        try:
            url = lusha_client.find_company_linkedin_by_domain(domain)
        except RuntimeError as exc:
            print(f"    lookup failed for {domain}: {exc}", file=sys.stderr)
            url = ""
        if sleep_seconds:
            time.sleep(sleep_seconds)
        return url

    return _lookup


def count_backfill_candidates(current: dict) -> "tuple[int, int, int]":
    """(needs_lookup, already_valid, no_domain) across a loaded current run —
    what a dry run reports without spending a single Lusha call."""
    needs = already = no_domain = 0
    for bucket in current["detail_files"].values():
        for detail in bucket.values():
            if not needs_backfill(detail):
                already += 1
            elif not pick_detail_domain(detail):
                no_domain += 1
            else:
                needs += 1
    return needs, already, no_domain


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def default_backfill_run_folder(now: "datetime | None" = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y-%m-%d}_linkedin_backfill"


def _load_checkpoint(path: "Optional[str]") -> "Optional[dict]":
    if not path:
        return None
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"⚠️  Could not read checkpoint {path}: {exc}", file=sys.stderr)
    return {}


def _save_checkpoint(path: "Optional[str]", checkpoint: "Optional[dict]") -> None:
    if not path or checkpoint is None:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"⚠️  Could not write checkpoint {path}: {exc}", file=sys.stderr)


def _print_manifest_summary(country_folder: str, manifest: dict) -> None:
    print(
        f"  {country_folder}: "
        f"{manifest['companies_backfilled']} backfilled, "
        f"{manifest['companies_already_valid']} already valid, "
        f"{manifest['companies_rejected_not_company']} rejected (not /company/), "
        f"{manifest['companies_no_linkedin']} no LinkedIn, "
        f"{manifest['companies_no_domain']} no domain, "
        f"{manifest['companies_skipped_budget']} skipped (budget)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill /company/ LinkedIn URLs into GCS current detail records.")
    parser.add_argument("--bucket", default=rg.DEFAULT_GCS_BUCKET,
                        help="GCS bucket (default: the pipeline's default).")
    parser.add_argument("--country", action="append", dest="countries",
                        help="Country folder (repeatable). Default: all country folders.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max Lusha lookups this run (credit cap). Omit for unlimited.")
    parser.add_argument("--apply-lookups", action="store_true",
                        help="Actually call Lusha. Without this it's a DRY RUN "
                             "(reports how many companies WOULD be looked up, spends nothing).")
    parser.add_argument("--sleep", type=float, default=0.3,
                        help="Seconds between Lusha calls (rate limiting; default 0.3).")
    parser.add_argument("--out-dir", default=None,
                        help="Write the new run's JSON files here (per country subfolder).")
    parser.add_argument("--checkpoint", default=None,
                        help="Resume file: records resolved company_ids so a rerun "
                             "doesn't re-spend lookups.")
    parser.add_argument("--upload", action="store_true",
                        help="Upload the new run to <country>/runs/<run-folder>/ in GCS.")
    parser.add_argument("--run-folder", default=None,
                        help="Run folder name for --upload/--promote (default: today's).")
    parser.add_argument("--promote", action="store_true",
                        help="Promote the uploaded run to current/ (makes it live). "
                             "Requires --upload.")
    args = parser.parse_args()

    if args.promote and not args.upload:
        parser.error("--promote requires --upload (nothing to promote otherwise).")

    run_folder = args.run_folder or default_backfill_run_folder()
    now_iso = datetime.now(timezone.utc).isoformat()

    countries = args.countries or list_country_folders(args.bucket)
    if not countries:
        print("No country folders found.", file=sys.stderr)
        sys.exit(1)

    lookup = make_lusha_lookup(args.sleep) if args.apply_lookups else None
    checkpoint = _load_checkpoint(args.checkpoint) if args.apply_lookups else None

    mode = "APPLY" if args.apply_lookups else "DRY RUN"
    budget_note = "unlimited" if args.limit is None else str(args.limit)
    print(f"\n{'='*72}\nLinkedIn backfill - {mode} - lookup budget: {budget_note}\n{'='*72}\n")

    budget = LookupBudget(args.limit)
    exit_code = 0

    for country_folder in countries:
        try:
            current = download_current_run(
                args.bucket, country_folder, rg.tempfile.mkdtemp())
        except RuntimeError as exc:
            print(f"  ⚠️  {country_folder}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if not args.apply_lookups:
            needs, already, no_domain = count_backfill_candidates(current)
            print(
                f"  {country_folder}: {needs} companies WOULD be looked up "
                f"({already} already have a company URL, {no_domain} have no domain). "
                f"Estimated Lusha calls this run: "
                f"{needs if args.limit is None else min(needs, args.limit)}")
            continue

        run = build_backfilled_run(
            current, lookup, country_folder=country_folder, run_folder=run_folder,
            now_iso=now_iso, budget=budget, checkpoint=checkpoint)
        _print_manifest_summary(country_folder, run["manifest"])
        _save_checkpoint(args.checkpoint, checkpoint)

        if args.out_dir:
            dest = Path(args.out_dir) / country_folder
            write_rescored_run(run, dest)
            print(f"    written to {dest}/")

        if args.upload:
            if not args.out_dir:
                # upload needs files on disk; stage them in a temp dir
                dest = Path(rg.tempfile.mkdtemp()) / country_folder
                write_rescored_run(run, dest)
            upload_rescored_run(dest, args.bucket, country_folder, run_folder)
            print(f"    uploaded to gs://{args.bucket}/{country_folder}/runs/{run_folder}/")
            if args.promote:
                promote_run_to_current(args.bucket, country_folder, run_folder)
                print(f"    promoted to gs://{args.bucket}/{country_folder}/current/ (live)")

    if not args.apply_lookups:
        print("\nDry run only — no Lusha calls made, nothing written. "
              "Re-run with --apply-lookups (and a --limit) to spend credits.")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
