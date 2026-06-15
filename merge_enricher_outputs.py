"""
merge_enricher_outputs.py
=========================
Merge multiple Client Enricher batch-output Excel files into one combined workbook.

Usage:
    python merge_enricher_outputs.py \\
        --input-dir "C:/Users/.../Italy200/02_lead_prioritized" \\
        --output "Italy200_merged_20260615_1400.xlsx" \\
        [--pattern "*.xlsx"]

Sheet order in output mirrors input sheet order (union of all encountered sheets).
Opportunity Input sheet is always written first if present.
A "Merge QA" sheet is appended last.
"""

import argparse
import fnmatch
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ── Sheet priority order ──────────────────────────────────────────────────────
# Sheets listed here are written first (in this order) if present.
# All other sheets follow in the order they were first encountered.
_PRIORITY_SHEETS = [
    "Opportunity Input",
    "Lead Scores",
    "Company Profiles",
    "Enriched",
    "Advanced Evidence",
    "qa_evidence",
    "model_features",
    "Input",
]

# Columns that must be written as plain text (no numeric coercion).
_TEXT_COLUMNS = {
    "raw_google_evidence_json",
    "raw_google_evidence_json_01",
    "raw_google_evidence_json_02",
    "raw_google_evidence_json_03",
    "raw_google_evidence_combined",
    "raw_google_evidence_urls",
    "raw_google_evidence_truncated",
    "source_file",
    "source_batch",
}

# Columns with long text content that should wrap in Excel.
_WRAP_COLUMNS = {
    "raw_google_evidence_combined",
    "raw_google_evidence_urls",
    "icp_evidence",
    "icp_why_relevant",
    "icp_buying_signals",
    "icp_likely_training_interest",
    "scoring_notes",
    "caller_angle",
    "business_output_reason",
}

# Duplicate-check columns in Opportunity Input
_DUP_COL_NUMBER = "company_number"
_DUP_COL_DOMAIN = "canonical_company_domain"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _derive_batch_label(filename: str) -> str:
    """
    Derive a short batch label from the filename.

    Italy200_02_R0501_1000_lead_prioritized_20260615_1142.xlsx
    -> Italy200_02_R0501_1000
    """
    stem = Path(filename).stem
    # Strip trailing _lead_prioritized_YYYYMMDD_HHMM or _enrichedResults_YYYYMMDD_HHMM
    cleaned = re.sub(
        r"_(lead_prioritized|enrichedResults|enriched_results)_\d{8}_?\d{4,6}$",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    return cleaned if cleaned != stem else stem


def _col_width(col_name: str, max_val_len: int = 20) -> int:
    """Compute a sensible column width."""
    base = max(len(str(col_name)) + 2, min(max_val_len + 2, 30))
    if col_name in _WRAP_COLUMNS:
        return 40
    if "json" in col_name.lower():
        return 20
    if "snippet" in col_name.lower() or "evidence" in col_name.lower():
        return 36
    if "url" in col_name.lower():
        return 32
    return min(base, 48)


def _header_fill() -> PatternFill:
    return PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")


def _header_font() -> Font:
    return Font(bold=True, color="FFFFFF", size=10)


def _write_df_to_sheet(ws, df: pd.DataFrame) -> None:
    """Write a DataFrame to an openpyxl worksheet with formatting."""
    hfill = _header_fill()
    hfont = _header_font()
    cols = list(df.columns)

    # Header row
    for ci, col in enumerate(cols, 1):
        cell = ws.cell(row=1, column=ci, value=str(col))
        cell.fill = hfill
        cell.font = hfont
        cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.column_dimensions[get_column_letter(ci)].width = _col_width(col)

    # Data rows
    for ri, (_, row) in enumerate(df.iterrows(), 2):
        for ci, col in enumerate(cols, 1):
            val = row[col]
            # NaN → empty string
            if isinstance(val, float) and val != val:
                val = ""
            # Force text for designated columns
            if col in _TEXT_COLUMNS:
                val = str(val) if val != "" else ""
            cell = ws.cell(row=ri, column=ci, value=val)
            if col in _WRAP_COLUMNS:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                cell.alignment = Alignment(vertical="top")

    ws.freeze_panes = "A2"
    if cols:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"
    ws.row_dimensions[1].height = 18


def _write_qa_sheet(ws, qa: dict) -> None:
    """Write the Merge QA sheet."""
    hfill = _header_fill()
    hfont = _header_font()

    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 80

    # Title row
    c = ws.cell(row=1, column=1, value="Metric")
    c.fill = hfill; c.font = hfont
    c = ws.cell(row=1, column=2, value="Value")
    c.fill = hfill; c.font = hfont
    ws.freeze_panes = "A2"

    rows = [
        ("Merge timestamp",    qa["timestamp"]),
        ("Input folder",       qa["input_dir"]),
        ("Output file",        qa["output_file"]),
        ("Input files found",  qa["n_files"]),
        ("", ""),
    ]
    # Per-file rows
    for fname, n_rows in qa.get("rows_per_file", {}).items():
        rows.append((f"  {fname}", f"{n_rows} rows"))
    rows.append(("", ""))
    # Per-sheet rows
    rows.append(("Rows per output sheet", ""))
    for sname, n_rows in qa.get("rows_per_sheet", {}).items():
        rows.append((f"  {sname}", str(n_rows)))
    rows.append(("", ""))
    # Warnings
    rows.append(("Warnings / notes", ""))
    for w in qa.get("warnings", []):
        rows.append(("  ⚠", w))
    if not qa.get("warnings"):
        rows.append(("  —", "No warnings"))

    bold_font = Font(bold=True)
    for ri, (k, v) in enumerate(rows, 2):
        ck = ws.cell(row=ri, column=1, value=str(k))
        cv = ws.cell(row=ri, column=2, value=str(v))
        if k and not k.startswith(" "):
            ck.font = bold_font
        cv.alignment = Alignment(wrap_text=True, vertical="top")

    ws.auto_filter.ref = "A1:B1"


# =============================================================================
# MAIN MERGE LOGIC
# =============================================================================

def collect_files(input_dir: Path, pattern: str) -> list[Path]:
    """Return sorted list of matching xlsx files, skipping lock files."""
    files = sorted(
        p for p in input_dir.glob(pattern)
        if p.is_file() and not p.name.startswith("~$")
    )
    return files


def read_all_sheets(xlsx_path: Path) -> dict[str, pd.DataFrame]:
    """Read all sheets from an Excel file as DataFrames (all values as object dtype)."""
    try:
        xf = pd.ExcelFile(xlsx_path, engine="openpyxl")
        result = {}
        for name in xf.sheet_names:
            try:
                df = xf.parse(name, dtype=str)
                # Replace literal 'nan' strings from dtype=str coercion
                df = df.fillna("").replace("nan", "").replace("NaN", "")
                result[name] = df
            except Exception as e:
                print(f"  [warn] Could not read sheet '{name}' in {xlsx_path.name}: {e}")
        return result
    except Exception as e:
        print(f"  [error] Could not open {xlsx_path.name}: {e}")
        return {}


def merge_sheet_frames(
    frames: list[tuple[str, str, pd.DataFrame]],
) -> pd.DataFrame:
    """
    Merge a list of (source_file, source_batch, df) tuples into one DataFrame.

    - Union of all columns; missing cells filled with "".
    - Prepends source_file, source_batch, source_row columns.
    """
    if not frames:
        return pd.DataFrame()

    all_cols: list[str] = []
    seen: set[str] = set()
    for _, _, df in frames:
        for c in df.columns:
            if c not in seen:
                all_cols.append(c)
                seen.add(c)

    parts = []
    for source_file, source_batch, df in frames:
        aligned = df.reindex(columns=all_cols, fill_value="")
        aligned.insert(0, "source_row",   range(1, len(aligned) + 1))
        aligned.insert(0, "source_batch", source_batch)
        aligned.insert(0, "source_file",  source_file)
        parts.append(aligned)

    return pd.concat(parts, ignore_index=True)


def add_duplicate_flags(df: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    """Add possible_duplicate_* columns to Opportunity Input if key columns exist."""
    for col, flag_col in [
        (_DUP_COL_NUMBER, "possible_duplicate_company_number"),
        (_DUP_COL_DOMAIN, "possible_duplicate_domain"),
    ]:
        if col in df.columns:
            vals = df[col].astype(str).str.strip()
            counts = vals.map(vals.value_counts())
            df[flag_col] = (counts > 1) & (vals != "") & (vals != "nan")
            df[flag_col] = df[flag_col].map({True: "YES", False: ""})
        else:
            warnings.append(
                f"Duplicate check skipped: column '{col}' not found in Opportunity Input."
            )
    return df


def run_merge(
    input_dir: Path,
    output_path: Path,
    pattern: str = "*.xlsx",
) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[merge] Start: {ts}")
    print(f"[merge] Input dir:  {input_dir}")
    print(f"[merge] Output:     {output_path}")
    print(f"[merge] Pattern:    {pattern}")

    files = collect_files(input_dir, pattern)
    if not files:
        print(f"[merge] ERROR: No files found matching '{pattern}' in {input_dir}")
        sys.exit(1)

    print(f"[merge] Found {len(files)} file(s):")
    for f in files:
        print(f"  {f.name}")

    # ── Read all files ────────────────────────────────────────────────────────
    # sheet_name -> list of (source_file, source_batch, df)
    sheet_data: dict[str, list[tuple[str, str, pd.DataFrame]]] = {}
    # sheet encounter order
    sheet_order: list[str] = []
    seen_sheets: set[str] = set()
    rows_per_file: dict[str, int] = {}
    warnings: list[str] = []

    for xlsx_path in files:
        label = xlsx_path.name
        batch = _derive_batch_label(xlsx_path.name)
        sheets = read_all_sheets(xlsx_path)
        if not sheets:
            warnings.append(f"No sheets read from {label} — file skipped.")
            continue
        file_row_count = 0
        for sname, df in sheets.items():
            if sname not in seen_sheets:
                sheet_order.append(sname)
                seen_sheets.add(sname)
            sheet_data.setdefault(sname, []).append((label, batch, df))
            file_row_count += len(df)
        rows_per_file[label] = file_row_count

    if not sheet_data:
        print("[merge] ERROR: no usable sheet data found.")
        sys.exit(1)

    # ── Determine output sheet order ─────────────────────────────────────────
    priority = [s for s in _PRIORITY_SHEETS if s in seen_sheets]
    rest = [s for s in sheet_order if s not in set(_PRIORITY_SHEETS)]
    ordered_sheets = priority + rest

    # ── Merge each sheet ─────────────────────────────────────────────────────
    merged: dict[str, pd.DataFrame] = {}
    rows_per_sheet: dict[str, int] = {}

    for sname in ordered_sheets:
        frames = sheet_data.get(sname, [])
        df_merged = merge_sheet_frames(frames)
        # Duplicate flags on Opportunity Input
        if sname == "Opportunity Input" and not df_merged.empty:
            df_merged = add_duplicate_flags(df_merged, warnings)
        # Column-count warning
        col_sets = [set(df.columns) for _, _, df in frames]
        if len(col_sets) > 1:
            union_cols = set.union(*col_sets)
            for i, cs in enumerate(col_sets):
                missing = union_cols - cs
                if missing:
                    fname = frames[i][0]
                    warnings.append(
                        f"Sheet '{sname}' in {fname} is missing "
                        f"{len(missing)} column(s) vs union: "
                        + ", ".join(sorted(missing)[:5])
                        + ("…" if len(missing) > 5 else "")
                    )
        merged[sname] = df_merged
        rows_per_sheet[sname] = len(df_merged)
        print(f"[merge] Sheet '{sname}': {len(df_merged)} rows merged from {len(frames)} file(s)")

    # Check expected sheets not found
    for expected in _PRIORITY_SHEETS:
        if expected not in seen_sheets:
            warnings.append(f"Expected sheet '{expected}' not found in any input file.")

    # ── Write output workbook ─────────────────────────────────────────────────
    print(f"[merge] Writing output: {output_path}")
    wb = Workbook()
    wb.remove(wb.active)  # remove default empty sheet

    for sname in ordered_sheets:
        ws = wb.create_sheet(sname)
        df = merged[sname]
        if df.empty:
            ws.cell(row=1, column=1, value="(No data)")
        else:
            _write_df_to_sheet(ws, df)

    # Merge QA sheet — always last
    ws_qa = wb.create_sheet("Merge QA")
    qa_data = {
        "timestamp":      ts,
        "input_dir":      str(input_dir),
        "output_file":    str(output_path),
        "n_files":        len(files),
        "rows_per_file":  rows_per_file,
        "rows_per_sheet": rows_per_sheet,
        "warnings":       warnings,
    }
    _write_qa_sheet(ws_qa, qa_data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"[merge] Saved: {output_path}")

    # ── Console QA summary ────────────────────────────────────────────────────
    print()
    print("[QA] Merge complete")
    print(f"[QA] Output:          {output_path}")
    print(f"[QA] Input files:     {len(files)}")
    print(f"[QA] Sheets written:  {list(ordered_sheets) + ['Merge QA']}")
    print(f"[QA] Rows per sheet:  {rows_per_sheet}")
    if warnings:
        print(f"[QA] Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"     ⚠ {w}")
    else:
        print("[QA] No warnings.")


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge multiple Client Enricher output Excel files into one workbook.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python merge_enricher_outputs.py \\
      --input-dir "C:/Users/gmeijer4/Nextcloud/Myngle/Italy200/02_lead_prioritized" \\
      --output "Italy200_merged_20260615.xlsx"

  python merge_enricher_outputs.py \\
      --input-dir ./outputs \\
      --output merged.xlsx \\
      --pattern "Italy200_*_lead_prioritized_*.xlsx"
""",
    )
    parser.add_argument(
        "--input-dir", required=True,
        help="Directory containing enricher output .xlsx files",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path for the merged output .xlsx file",
    )
    parser.add_argument(
        "--pattern", default="*.xlsx",
        help="Glob pattern to select input files (default: *.xlsx)",
    )
    args = parser.parse_args()

    input_dir   = Path(args.input_dir).resolve()
    output_path = Path(args.output).resolve()

    if not input_dir.exists():
        print(f"ERROR: --input-dir does not exist: {input_dir}", file=sys.stderr)
        sys.exit(1)
    if not input_dir.is_dir():
        print(f"ERROR: --input-dir is not a directory: {input_dir}", file=sys.stderr)
        sys.exit(1)

    run_merge(input_dir, output_path, pattern=args.pattern)


if __name__ == "__main__":
    main()
