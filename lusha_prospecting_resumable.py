"""
lusha_prospecting_resumable.py
-------------------------------
Resumable, checkpointed bulk-download of Lusha companies/prospecting
results for one country + employee-size-band + industry-filter combo --
built for pulls large enough (thousands of pages) that a mid-run failure
(network blip, 429 storm, terminal closed, laptop sleep) must not mean
re-paying Lusha credits for pages already fetched.

Unlike lusha_prospecting_app.py's "Fetch everything" button -- which
holds results only in Streamlit session state and always restarts
pagination at page 0 -- this script:

  1. Flushes every fetched page's new companies to a local JSONL file
     immediately (with fsync), so anything already paid for is safely on
     disk even if the process dies mid-run.
  2. Writes a checkpoint.json after every page with the last completed
     page number and the exact filter fingerprint used.
  3. On (re)start, if a checkpoint for the SAME filter fingerprint
     already exists in --out-dir, resumes from checkpoint_page + 1
     instead of page 0 -- no re-paying for pages already fetched.
  4. Refuses to silently continue if an existing checkpoint's fingerprint
     doesn't match the current run's filters (that would silently mix
     two different datasets into one file) -- point --out-dir at a
     fresh directory for a different query instead.

Reuses lusha_prospecting_app.py's pure, Streamlit-free helpers
(build_prospecting_request, fetch_prospecting_page, find_locations, ...)
so both entry points stay in sync on request-shape and billing behaviour.

Only ever calls /v3/companies/prospecting (never /enrich or a contact
endpoint) -- same billing model as lusha_prospecting_app.py: ~1 credit
per 25 results, rounded up per page call.

Run with:
    python lusha_prospecting_resumable.py --country Italy \\
        --min-employees 51 --max-employees 200 \\
        --out-dir lusha_downloads/italy_51_200

Interrupted (Ctrl-C, crash, closed terminal)? Re-run the exact same
command -- it picks up where it left off.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import lusha_prospecting_app as app

_CHECKPOINT_NAME = "checkpoint.json"
_COMPANIES_NAME = "companies.jsonl"


def _load_streamlit_secrets() -> dict:
    """Best-effort ``.streamlit/secrets.toml`` loader for this standalone
    script -- no Streamlit import needed. Checked in the same two spots
    Streamlit itself uses: next to this script (repo-local secrets) and
    under the user's home directory (global secrets). Returns {} if
    neither exists or the file can't be parsed; never raises, mirroring
    ``lusha_prospecting_app.resolve_secret``'s "never raises" contract."""
    candidates = [
        Path(__file__).resolve().parent / ".streamlit" / "secrets.toml",
        Path.home() / ".streamlit" / "secrets.toml",
    ]
    for path in candidates:
        if path.is_file():
            try:
                with path.open("rb") as f:
                    return tomllib.load(f)
            except Exception:
                continue
    return {}


def _fingerprint(location: dict, size_bands: list[dict], excluded_industry_ids: list[int]) -> dict:
    """Stable, comparable description of the query a checkpoint belongs to
    -- used to detect "you changed the filters but reused --out-dir"."""
    return {
        "location": location,
        "size_bands": size_bands,
        "excluded_industry_ids": sorted(excluded_industry_ids),
    }


def _load_checkpoint(out_dir: Path) -> "dict | None":
    path = out_dir / _CHECKPOINT_NAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_checkpoint(out_dir: Path, checkpoint: dict) -> None:
    # Write-to-temp-then-replace so a crash mid-write never leaves a
    # half-written (unparseable) checkpoint.json behind.
    path = out_dir / _CHECKPOINT_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_seen_ids(out_dir: Path) -> set:
    """Company ids already on disk from a previous (partial) run --
    belt-and-braces dedup on top of the checkpointed page number."""
    path = out_dir / _COMPANIES_NAME
    seen: set = set()
    if not path.exists():
        return seen
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                cid = json.loads(line).get("id")
            except json.JSONDecodeError:
                continue
            if cid is not None:
                seen.add(cid)
    return seen


def fetch_resumable(
    key: str, *, location: dict, size_bands: list[dict],
    excluded_industry_ids: list[int], out_dir: Path,
) -> dict:
    """Page through companies/prospecting, checkpointing to out_dir after
    every page. Safe to interrupt (Ctrl-C, crash, network loss) and
    re-run with identical arguments -- picks up where it left off instead
    of re-paying for already-fetched pages.

    Returns ``{"total_reported", "credits_charged", "companies_collected",
    "out_dir"}``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = _fingerprint(location, size_bands, excluded_industry_ids)
    checkpoint = _load_checkpoint(out_dir)

    if checkpoint is not None and checkpoint.get("fingerprint") != fingerprint:
        raise RuntimeError(
            f"Existing checkpoint in {out_dir} was started with different "
            "filters. Point --out-dir at a fresh, empty directory for a "
            "different query, or delete that directory's checkpoint.json "
            "and companies.jsonl if you really mean to restart this one "
            "from scratch (you will re-pay credits for every page)."
        )

    start_page = (checkpoint["last_completed_page"] + 1) if checkpoint else 0
    total_reported = checkpoint.get("total_reported") if checkpoint else None
    credits_charged = checkpoint.get("credits_charged", 0) if checkpoint else 0
    seen_ids = _load_seen_ids(out_dir)

    if start_page > 0:
        print(
            f"Resuming from page {start_page} — {len(seen_ids)} companies "
            f"already on disk, {credits_charged} credits already spent on "
            "this query."
        )

    companies_path = out_dir / _COMPANIES_NAME
    page = start_page
    with companies_path.open("a", encoding="utf-8") as companies_file:
        while True:
            body = app.build_prospecting_request(
                location=location, size_bands=size_bands, page=page,
                excluded_industry_ids=excluded_industry_ids,
            )
            try:
                data = app.fetch_prospecting_page(key, body)
            except app.RateLimited as exc:
                wait = float(exc.retry_after or 5)
                print(f"Rate limited — waiting {wait:.0f}s…")
                time.sleep(wait)
                continue
            except Exception as exc:
                print(
                    f"Error on page {page}: {exc}\n"
                    f"{len(seen_ids)} companies already saved to "
                    f"{companies_path} — re-run the exact same command to "
                    "resume from this page; nothing already paid for is lost."
                )
                raise

            results = data.get("results") or []
            pagination = data.get("pagination") or {}
            billing = data.get("billing") or {}
            total_reported = pagination.get("total", total_reported)
            credits_charged += billing.get("creditsCharged", 0) or 0

            new_count = 0
            for c in results:
                cid = c.get("id")
                if cid is not None:
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                companies_file.write(json.dumps(c) + "\n")
                new_count += 1
            companies_file.flush()
            os.fsync(companies_file.fileno())

            _save_checkpoint(out_dir, {
                "fingerprint": fingerprint,
                "last_completed_page": page,
                "total_reported": total_reported,
                "credits_charged": credits_charged,
                "companies_collected": len(seen_ids),
            })

            print(
                f"Page {page + 1} — {len(seen_ids)} of {total_reported or '?'} "
                f"companies, {credits_charged} credits so far."
            )

            if not results or new_count == 0:
                break
            if total_reported is not None and (page + 1) * app._PAGE_SIZE >= total_reported:
                break
            page += 1

    return {
        "total_reported": total_reported,
        "credits_charged": credits_charged,
        "companies_collected": len(seen_ids),
        "out_dir": str(out_dir),
    }


def export_xlsx(out_dir: Path) -> Path:
    """Convert the checkpointed companies.jsonl into a single Excel file,
    same shape as lusha_prospecting_app.py's own download button. Safe to
    re-run at any point -- including on a still-partial companies.jsonl,
    e.g. to check progress without waiting for the full run to finish."""
    import pandas as pd

    companies_path = out_dir / _COMPANIES_NAME
    rows = []
    with companies_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    df = pd.DataFrame(rows)
    xlsx_path = out_dir / "companies.xlsx"
    df.to_excel(xlsx_path, index=False)
    return xlsx_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--country", required=True, help='e.g. "Italy"')
    parser.add_argument("--min-employees", type=int, required=True)
    parser.add_argument(
        "--max-employees", type=int, default=None,
        help="Omit for an open-ended band (e.g. 100001+).")
    parser.add_argument(
        "--exclude-industries", type=int, nargs="*",
        default=list(app.DEFAULT_EXCLUDED_INDUSTRY_IDS),
        help="Main industry IDs to exclude "
             f"(default: {app.DEFAULT_EXCLUDED_INDUSTRY_IDS} = Government, Community).")
    parser.add_argument(
        "--out-dir", required=True, type=Path,
        help="Checkpoint + output directory. Re-run with the SAME path to resume.")
    parser.add_argument(
        "--api-key", default=None, help="Defaults to the LUSHA_API_KEY env var.")
    parser.add_argument(
        "--check-only", action="store_true",
        help="Cheapest possible call (page size 10, ~1 credit) to show the "
             "matching company count and estimated total cost, then exit "
             "without downloading anything.")
    args = parser.parse_args()

    api_key = args.api_key or app.resolve_secret(_load_streamlit_secrets(), "LUSHA_API_KEY")
    if not api_key:
        sys.exit(
            "No Lusha API key: pass --api-key, set the LUSHA_API_KEY env var, "
            "or add it to .streamlit/secrets.toml."
        )

    matches = app.find_locations(api_key, args.country)
    if not matches:
        sys.exit(f"No Lusha location match for {args.country!r}.")
    if len(matches) > 1:
        print(f"Multiple location matches for {args.country!r} — using the first:")
        for i, m in enumerate(matches):
            marker = "→" if i == 0 else " "
            print(f"  {marker} {app.location_match_label(m)}")
    location = matches[0]

    size_band: dict = {"min": args.min_employees}
    if args.max_employees is not None:
        size_band["max"] = args.max_employees
    size_bands = [size_band]

    print(f"Location: {app.location_match_label(location)}")
    print(f"Size band: {app.size_band_label(size_band)}")
    print(f"Excluding industry IDs: {args.exclude_industries}")

    if args.check_only:
        body = app.build_prospecting_request(
            location=location, size_bands=size_bands, page=0,
            excluded_industry_ids=args.exclude_industries,
        )
        body["pagination"]["size"] = app._PREVIEW_PAGE_SIZE
        data = app.fetch_prospecting_page(api_key, body)
        total = (data.get("pagination") or {}).get("total") or 0
        check_cost = (data.get("billing") or {}).get("creditsCharged", 0) or 0
        estimate = app.estimate_credits_for_download(total)
        print(
            f"\nMatching companies: {total}\n"
            f"Credits spent on this check: {check_cost}\n"
            f"Estimated credits for the full download: ~{estimate}"
        )
        return

    try:
        stats = fetch_resumable(
            api_key, location=location, size_bands=size_bands,
            excluded_industry_ids=args.exclude_industries, out_dir=args.out_dir,
        )
    except KeyboardInterrupt:
        sys.exit(
            "\nInterrupted — already-fetched companies are safely saved in "
            f"{args.out_dir}. Re-run the exact same command to resume."
        )

    print(
        f"\nDone: {stats['companies_collected']} companies, "
        f"{stats['credits_charged']} credits spent, in {stats['out_dir']}."
    )
    xlsx_path = export_xlsx(args.out_dir)
    print(f"Exported: {xlsx_path}")


if __name__ == "__main__":
    main()
