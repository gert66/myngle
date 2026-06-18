"""
patch_enriched_urls_from_cleaned.py

Copies corrected website_url values from a cleaned register file back into
existing enriched files for Italy100 and Italy200.

Usage:
  python patch_enriched_urls_from_cleaned.py \
      --source-cleaned "<path>" \
      --target-root "<path>" \
      --queues Italy100 Italy200 \
      --dry-run | --apply \
      [--in-place] [--overwrite] [--report-dir "<path>"] \
      [--match-threshold strict|fuzzy] [--only-changed]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------
try:
    import openpyxl
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")

try:
    import csv
except ImportError:
    csv = None  # stdlib, always available

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFERRED_SOURCE_SHEETS = ["Best Guess Input", "Cleaned Register Input"]

# Sheets in target files that are likely to contain company rows
ENRICHED_TARGET_SHEETS = [
    "Best Guess Input",
    "Commercial Input",
    "Cleaned Register Input",
]

ITALIAN_LEGAL_SUFFIXES = [
    r"s\.?p\.?a\.?",
    r"s\.?r\.?l\.?",
    r"societa[\s]*per[\s]*azioni",
    r"societa[\s]*a[\s]*responsabilita[\s]*limitata",
    r"in[\s]*forma[\s]*abbreviata",
    r"siglabile",
    r"s\.?n\.?c\.?",
    r"s\.?a\.?s\.?",
    r"s\.?c\.?a\.?r\.?l\.?",
    r"s\.?c\.?",
    r"onlus",
    r"ets",
    r"aps",
    r"asd",
    r"spa",
    r"srl",
    r"snc",
    r"sas",
    r"scarl",
]

URL_COLUMNS = [
    "website_url",
    "canonical_company_url",
]

DOMAIN_COLUMNS = [
    "domain",
    "validated_domain",
    "input_domain",
    "canonical_company_domain",
    "final_selected_domain",
    "recommended_domain",
]

AUDIT_COLUMNS = [
    "url_patch_status",
    "url_patch_old_website_url",
    "url_patch_new_website_url",
    "url_patch_old_domain",
    "url_patch_new_domain",
    "url_patch_match_key",
    "url_patch_source_sheet",
    "needs_reenrichment",
]

PROTECTED_COLUMNS = {
    "commercial_fit_score",
    "final_commercial_fit_score",
    "commercial_tier",
    "icp_evidence",
    "raw_google_evidence_json",
    "scoring_notes",
    "caller_angle",
}

STATUS_PATCHED_CHANGED = "PATCHED_URL_CHANGED"
STATUS_PATCHED_SAME = "PATCHED_URL_SAME"
STATUS_NO_MATCH = "NO_SOURCE_MATCH"
STATUS_SOURCE_EMPTY = "SOURCE_URL_EMPTY"
STATUS_DUPLICATE_SKIPPED = "DUPLICATE_SOURCE_MATCH_SKIPPED"
STATUS_TARGET_EMPTY = "TARGET_COMPANY_EMPTY"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _remove_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


_LEGAL_RE = re.compile(
    r"\b(" + "|".join(ITALIAN_LEGAL_SUFFIXES) + r")\b",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_company(name: Any) -> str:
    if not name or not str(name).strip():
        return ""
    s = str(name).strip()
    s = _remove_accents(s)
    s = s.lower()
    s = _LEGAL_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_city(value: Any) -> str:
    if not value:
        return ""
    s = _remove_accents(str(value).strip().lower())
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


# ---------------------------------------------------------------------------
# URL / domain helpers
# ---------------------------------------------------------------------------

def extract_domain(url_or_domain: Any) -> str:
    """Return bare domain from a full URL or plain domain string."""
    if not url_or_domain:
        return ""
    s = str(url_or_domain).strip()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    parsed = urlparse(s)
    domain = parsed.netloc or parsed.path
    domain = re.sub(r"^www\.", "", domain.lower()).strip("/")
    return domain


def to_full_url(url_or_domain: Any) -> str:
    """Return https://<domain> from a full URL or plain domain string."""
    domain = extract_domain(url_or_domain)
    if not domain:
        return ""
    return f"https://{domain}"


# ---------------------------------------------------------------------------
# Source lookup builder
# ---------------------------------------------------------------------------

class SourceLookup:
    """
    Builds three-level lookup tables from the cleaned source file:
      primary   -> normalized company name
      secondary -> normalized company name + normalized city
      tertiary  -> normalized company name + normalized province
    """

    def __init__(self) -> None:
        self.primary: dict[str, str] = {}           # key -> url
        self.secondary: dict[str, str] = {}          # key -> url
        self.tertiary: dict[str, str] = {}           # key -> url
        self.duplicates: set[str] = set()            # ambiguous primary keys
        self.source_sheet: str = ""
        self.total_rows = 0
        self.rows_with_url = 0

    def build(self, wb: openpyxl.Workbook) -> None:
        sheet = _pick_source_sheet(wb)
        self.source_sheet = sheet.title
        headers = _get_headers(sheet)

        col = _col_index(headers, "company_name")
        url_col = _col_index(headers, "website_url")
        city_col = _col_index(headers, "city")
        prov_col = _col_index(headers, "province")

        if col is None:
            sys.exit(f"Source sheet '{sheet.title}' has no 'company_name' column.")
        if url_col is None:
            sys.exit(f"Source sheet '{sheet.title}' has no 'website_url' column.")

        for row in sheet.iter_rows(min_row=2, values_only=True):
            self.total_rows += 1
            company_raw = row[col] if col < len(row) else None
            url_raw = row[url_col] if url_col < len(row) else None

            key = normalize_company(company_raw)
            if not key:
                continue

            url = str(url_raw).strip() if url_raw else ""
            if not url:
                continue

            self.rows_with_url += 1

            # Secondary key
            if city_col is not None and city_col < len(row):
                city = normalize_city(row[city_col])
                if city:
                    sec_key = f"{key}||{city}"
                    self.secondary[sec_key] = url

            # Tertiary key
            if prov_col is not None and prov_col < len(row):
                prov = normalize_city(row[prov_col])
                if prov:
                    ter_key = f"{key}||prov||{prov}"
                    self.tertiary[ter_key] = url

            # Primary key – track ambiguity
            if key in self.primary and self.primary[key] != url:
                self.duplicates.add(key)
            else:
                self.primary[key] = url

    def lookup(
        self,
        company: Any,
        city: Any = None,
        province: Any = None,
    ) -> tuple[str, str, str]:
        """
        Returns (url, status, match_key).
        status: FOUND | DUPLICATE | NOT_FOUND | EMPTY
        """
        key = normalize_company(company)
        if not key:
            return "", "EMPTY", ""

        # Try secondary (city) first for disambiguation
        if city:
            city_norm = normalize_city(city)
            sec_key = f"{key}||{city_norm}"
            if sec_key in self.secondary:
                return self.secondary[sec_key], "FOUND", sec_key

        # Try tertiary (province)
        if province:
            prov_norm = normalize_city(province)
            ter_key = f"{key}||prov||{prov_norm}"
            if ter_key in self.tertiary:
                return self.tertiary[ter_key], "FOUND", ter_key

        # Primary
        if key in self.duplicates:
            return "", "DUPLICATE", key

        if key in self.primary:
            return self.primary[key], "FOUND", key

        return "", "NOT_FOUND", key


# ---------------------------------------------------------------------------
# Workbook helpers
# ---------------------------------------------------------------------------

def _pick_source_sheet(wb: openpyxl.Workbook) -> openpyxl.worksheet.worksheet.Worksheet:
    for name in PREFERRED_SOURCE_SHEETS:
        if name in wb.sheetnames:
            return wb[name]
    return wb.active


def _get_headers(sheet) -> list[str]:
    first_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), [])
    return [str(h).strip() if h is not None else "" for h in first_row]


def _col_index(headers: list[str], name: str) -> int | None:
    try:
        return headers.index(name)
    except ValueError:
        return None


def _col_index_ci(headers: list[str], name: str) -> int | None:
    """Case-insensitive column lookup."""
    lo = name.lower()
    for i, h in enumerate(headers):
        if h.lower() == lo:
            return i
    return None


def _is_target_sheet(sheet_name: str, headers: list[str]) -> bool:
    """Return True if this sheet looks like it contains company rows."""
    if sheet_name in ENRICHED_TARGET_SHEETS:
        return True
    headers_lower = {h.lower() for h in headers}
    return "company_name" in headers_lower


# ---------------------------------------------------------------------------
# Core patching logic (single sheet)
# ---------------------------------------------------------------------------

def patch_sheet(
    sheet,
    lookup: SourceLookup,
    dry_run: bool,
    only_changed: bool,
    source_sheet_name: str,
) -> list[dict]:
    """Patch URL/domain cells in a worksheet. Returns list of report rows."""
    headers = _get_headers(sheet)
    company_col = _col_index_ci(headers, "company_name")
    if company_col is None:
        return []

    city_col = _col_index_ci(headers, "city")
    prov_col = _col_index_ci(headers, "province")

    # Map column names to indices for URL and domain columns
    url_cols: dict[str, int] = {}
    for name in URL_COLUMNS:
        idx = _col_index_ci(headers, name)
        if idx is not None:
            url_cols[name] = idx

    domain_cols: dict[str, int] = {}
    for name in DOMAIN_COLUMNS:
        idx = _col_index_ci(headers, name)
        if idx is not None:
            domain_cols[name] = idx

    # Ensure audit columns exist (add at end if missing)
    audit_col_indices: dict[str, int] = {}
    for audit_col in AUDIT_COLUMNS:
        idx = _col_index_ci(headers, audit_col)
        if idx is None:
            # Append header
            new_col_idx = len(headers)
            headers.append(audit_col)
            # Write header cell
            col_letter = get_column_letter(new_col_idx + 1)
            sheet[f"{col_letter}1"] = audit_col
            audit_col_indices[audit_col] = new_col_idx
        else:
            audit_col_indices[audit_col] = idx

    report_rows = []

    for row_idx, row in enumerate(sheet.iter_rows(min_row=2), start=2):
        company_raw = row[company_col].value if company_col < len(row) else None
        city_raw = row[city_col].value if city_col is not None and city_col < len(row) else None
        prov_raw = row[prov_col].value if prov_col is not None and prov_col < len(row) else None

        if not company_raw or not str(company_raw).strip():
            _set_audit(sheet, row_idx, audit_col_indices, {
                "url_patch_status": STATUS_TARGET_EMPTY,
                "needs_reenrichment": False,
                "url_patch_match_key": "",
                "url_patch_source_sheet": source_sheet_name,
            }, dry_run)
            report_rows.append({
                "company_name": "",
                "status": STATUS_TARGET_EMPTY,
                "old_website_url": "",
                "new_website_url": "",
                "old_domain": "",
                "new_domain": "",
                "match_key": "",
                "needs_reenrichment": False,
                "row": row_idx,
            })
            continue

        new_url, find_status, match_key = lookup.lookup(company_raw, city_raw, prov_raw)

        old_website_url = ""
        if "website_url" in url_cols:
            old_website_url = str(row[url_cols["website_url"]].value or "").strip()

        old_domain = ""
        for dc in DOMAIN_COLUMNS:
            if dc in domain_cols:
                v = str(row[domain_cols[dc]].value or "").strip()
                if v:
                    old_domain = v
                    break

        if find_status == "EMPTY":
            status = STATUS_TARGET_EMPTY
            new_url = ""
        elif find_status == "NOT_FOUND":
            status = STATUS_NO_MATCH
            new_url = ""
        elif find_status == "DUPLICATE":
            status = STATUS_DUPLICATE_SKIPPED
            new_url = ""
        else:
            # FOUND
            if not new_url:
                status = STATUS_SOURCE_EMPTY
            else:
                new_domain = extract_domain(new_url)
                if new_domain == extract_domain(old_website_url) and new_domain:
                    status = STATUS_PATCHED_SAME
                else:
                    status = STATUS_PATCHED_CHANGED

        new_domain = extract_domain(new_url) if new_url else ""
        needs_reenrich = bool(new_domain and new_domain != extract_domain(old_website_url))

        # Apply changes
        if not dry_run and new_url and status in (STATUS_PATCHED_CHANGED, STATUS_PATCHED_SAME):
            if not only_changed or status == STATUS_PATCHED_CHANGED:
                full_url = to_full_url(new_url)
                domain_only = extract_domain(new_url)

                for col_name, col_idx in url_cols.items():
                    _set_cell(sheet, row_idx, col_idx, full_url)

                for col_name, col_idx in domain_cols.items():
                    _set_cell(sheet, row_idx, col_idx, domain_only)

        _set_audit(sheet, row_idx, audit_col_indices, {
            "url_patch_status": status,
            "url_patch_old_website_url": old_website_url,
            "url_patch_new_website_url": to_full_url(new_url) if new_url else "",
            "url_patch_old_domain": old_domain,
            "url_patch_new_domain": new_domain,
            "url_patch_match_key": match_key,
            "url_patch_source_sheet": source_sheet_name,
            "needs_reenrichment": needs_reenrich,
        }, dry_run)

        report_rows.append({
            "company_name": str(company_raw).strip(),
            "status": status,
            "old_website_url": old_website_url,
            "new_website_url": to_full_url(new_url) if new_url else "",
            "old_domain": old_domain,
            "new_domain": new_domain,
            "match_key": match_key,
            "needs_reenrichment": needs_reenrich,
            "row": row_idx,
        })

    return report_rows


def _set_cell(sheet, row_idx: int, col_idx: int, value: Any) -> None:
    col_letter = get_column_letter(col_idx + 1)
    sheet[f"{col_letter}{row_idx}"] = value


def _set_audit(sheet, row_idx: int, audit_indices: dict, values: dict, dry_run: bool) -> None:
    if dry_run:
        return
    for col_name, col_idx in audit_indices.items():
        v = values.get(col_name, "")
        _set_cell(sheet, row_idx, col_idx, v)


# ---------------------------------------------------------------------------
# File-level processing
# ---------------------------------------------------------------------------

def _resolve_output_path(output_dir: Path, filename: str, overwrite: bool) -> Path:
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = output_dir / filename
    if not candidate.exists() or overwrite:
        return candidate
    # Add numeric suffix to avoid overwriting
    n = 2
    while True:
        candidate = output_dir / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def process_file(
    xlsx_path: Path,
    output_dir: Path,
    lookup: SourceLookup,
    dry_run: bool,
    in_place: bool,
    overwrite: bool,
    only_changed: bool,
) -> list[dict]:
    """Process a single enriched file. Returns report rows."""
    wb = load_workbook(xlsx_path, data_only=False)
    all_report_rows = []

    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]
        headers = _get_headers(sheet)
        if not _is_target_sheet(sheet_name, headers):
            continue

        rows = patch_sheet(
            sheet, lookup, dry_run, only_changed, lookup.source_sheet
        )
        for r in rows:
            r["file"] = xlsx_path.name
            r["sheet"] = sheet_name
        all_report_rows.extend(rows)

    if not dry_run:
        if in_place:
            wb.save(xlsx_path)
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            out_path = _resolve_output_path(output_dir, xlsx_path.name, overwrite)
            wb.save(out_path)

    return all_report_rows


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_report(report_rows: list[dict], report_dir: Path, queues: list[str]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    queues_str = "_".join(queues)
    report_path = report_dir / f"url_patch_report_{ts}_{queues_str}.csv"

    fieldnames = [
        "queue", "file", "sheet", "row", "company_name",
        "old_website_url", "new_website_url",
        "old_domain", "new_domain",
        "status", "needs_reenrichment", "match_key", "notes",
    ]

    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in report_rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    return report_path


def print_summary(lookup: SourceLookup, report_rows: list[dict]) -> None:
    status_counts: dict[str, int] = {}
    for r in report_rows:
        s = r.get("status", "")
        status_counts[s] = status_counts.get(s, 0) + 1

    needs_reenrich = sum(1 for r in report_rows if r.get("needs_reenrichment"))

    print("\n" + "=" * 60)
    print("URL PATCH SUMMARY")
    print("=" * 60)
    print(f"  Source rows loaded          : {lookup.total_rows}")
    print(f"  Source rows with URL        : {lookup.rows_with_url}")
    print(f"  Duplicate source keys       : {len(lookup.duplicates)}")
    print(f"  Target rows seen            : {len(report_rows)}")
    print(f"  Rows patched (URL changed)  : {status_counts.get(STATUS_PATCHED_CHANGED, 0)}")
    print(f"  Rows patched (URL same)     : {status_counts.get(STATUS_PATCHED_SAME, 0)}")
    print(f"  Rows no source match        : {status_counts.get(STATUS_NO_MATCH, 0)}")
    print(f"  Rows skipped (duplicate)    : {status_counts.get(STATUS_DUPLICATE_SKIPPED, 0)}")
    print(f"  Rows source URL empty       : {status_counts.get(STATUS_SOURCE_EMPTY, 0)}")
    print(f"  Rows target company empty   : {status_counts.get(STATUS_TARGET_EMPTY, 0)}")
    print(f"  Rows needing re-enrichment  : {needs_reenrich}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Patch website_url in enriched files from a cleaned register.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source-cleaned", required=True, help="Path to the cleaned register .xlsx")
    p.add_argument("--target-root", required=True, help="Root folder, e.g. C:\\Users\\...\\Myngle")
    p.add_argument("--queues", nargs="+", default=["Italy100", "Italy200"],
                   help="Queue folder names under target-root")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview changes without writing any files")
    p.add_argument("--apply", action="store_true",
                   help="Write patched files to output folders")
    p.add_argument("--in-place", action="store_true", default=False,
                   help="Overwrite original enriched files (dangerous)")
    p.add_argument("--overwrite", action="store_true", default=False,
                   help="Overwrite existing patched output files")
    p.add_argument("--report-dir", default=None,
                   help="Directory for the CSV report (default: <target-root>\\_url_patch_reports)")
    p.add_argument("--match-threshold", default="strict", choices=["strict"],
                   help="Matching mode (currently only strict exact normalised match)")
    p.add_argument("--only-changed", action="store_true", default=False,
                   help="Only write cells that actually change (skip PATCHED_URL_SAME rows)")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Default to dry-run if neither flag is given
    if not args.dry_run and not args.apply:
        print("Neither --dry-run nor --apply specified. Defaulting to --dry-run.")
        args.dry_run = True

    if args.dry_run and args.apply:
        sys.exit("Pass either --dry-run or --apply, not both.")

    source_path = Path(args.source_cleaned)
    if not source_path.exists():
        sys.exit(f"Source file not found: {source_path}")

    target_root = Path(args.target_root)
    if not target_root.exists():
        sys.exit(f"Target root not found: {target_root}")

    report_dir = Path(args.report_dir) if args.report_dir else target_root / "_url_patch_reports"

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"\n[{mode}] Loading source: {source_path.name}")

    # Build lookup
    source_wb = load_workbook(source_path, data_only=True, read_only=True)
    lookup = SourceLookup()
    lookup.build(source_wb)
    source_wb.close()

    print(f"  Source sheet   : '{lookup.source_sheet}'")
    print(f"  Rows loaded    : {lookup.total_rows}")
    print(f"  Rows with URL  : {lookup.rows_with_url}")
    print(f"  Duplicate keys : {len(lookup.duplicates)}")
    if lookup.duplicates:
        sample = sorted(lookup.duplicates)[:5]
        print(f"  Sample dupes   : {sample}")

    all_report_rows: list[dict] = []
    files_processed = 0

    for queue in args.queues:
        input_dir = target_root / queue / "02_lead_prioritized"
        output_dir = target_root / queue / "02_lead_prioritized_url_patched"

        if not input_dir.exists():
            print(f"\n[WARN] Input folder not found, skipping: {input_dir}")
            continue

        xlsx_files = [
            f for f in input_dir.glob("*.xlsx")
            if not f.name.startswith("~$")
        ]

        print(f"\nQueue: {queue}  |  {len(xlsx_files)} file(s) in {input_dir}")

        for xlsx_path in sorted(xlsx_files):
            print(f"  Processing: {xlsx_path.name}")
            rows = process_file(
                xlsx_path=xlsx_path,
                output_dir=output_dir,
                lookup=lookup,
                dry_run=args.dry_run,
                in_place=args.in_place,
                overwrite=args.overwrite,
                only_changed=args.only_changed,
            )
            for r in rows:
                r["queue"] = queue
            all_report_rows.extend(rows)
            files_processed += 1

            changed = sum(1 for r in rows if r.get("status") == STATUS_PATCHED_CHANGED)
            no_match = sum(1 for r in rows if r.get("status") == STATUS_NO_MATCH)
            print(f"    rows={len(rows)}  changed={changed}  no_match={no_match}")

    print(f"\nTotal files processed: {files_processed}")

    # Write report
    report_path = write_report(all_report_rows, report_dir, args.queues)
    print(f"Report written to   : {report_path}")

    print_summary(lookup, all_report_rows)

    if args.dry_run:
        print("Dry-run complete. No files were written.")
        print("Re-run with --apply to apply patches.\n")
    else:
        print("Apply complete.\n")


if __name__ == "__main__":
    main()
