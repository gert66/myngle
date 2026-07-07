"""
merge_enricher_outputs.py
=========================
Merge multiple Client Enricher batch-output Excel files into one workbook
that matches the structure of an individual enricher output as closely as possible.

Sheet order in output (skipped if absent in all inputs):
  1.  Lead Scores
  2.  Company Profiles     — copied from first file only (layout sheet)
  3.  Opportunity Input    — tabular merge, metadata columns appended RIGHT
  4.  Input                — tabular merge
  5.  Advanced Evidence    — tabular merge
  6.  Scoring Settings     — copied from first file only (settings sheet)
  7.  Run Settings         — copied from first file only (settings sheet)
  8.  Enriched             — tabular merge
  9.  model_features       — tabular merge
  10. qa_evidence          — tabular merge
  11. Merge QA             — always last

Usage:
    python merge_enricher_outputs.py \\
        --input-dir "C:/Users/.../Italy200/02_lead_prioritized" \\
        --output "Italy200_merged_20260615_1400.xlsx" \\
        [--pattern "*.xlsx"]
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# =============================================================================
# SHEET DEFINITIONS
# =============================================================================

# Output sheet order.  Each entry: (name, treatment)
# treatment: "tabular" | "copy_first" | "qa"
SHEET_ORDER: list[tuple[str, str]] = [
    ("Lead Scores",       "tabular"),
    ("Company Profiles",  "copy_first"),
    ("Opportunity Input", "tabular"),
    ("Input",             "tabular"),
    ("Advanced Evidence", "tabular"),
    ("Scoring Settings",  "copy_first"),
    ("Run Settings",      "copy_first"),
    ("Enriched",          "tabular"),
    ("model_features",    "tabular"),
    ("qa_evidence",       "tabular"),
    ("Merge QA",          "qa"),
]

TABULAR_SHEET_NAMES: set[str] = {
    s for s, t in SHEET_ORDER if t == "tabular"
}
COPY_FIRST_SHEET_NAMES: set[str] = {
    s for s, t in SHEET_ORDER if t == "copy_first"
}

# Columns whose values must always be kept as plain text (no numeric coercion).
TEXT_COLUMNS: set[str] = {
    "raw_google_evidence_json",
    "raw_google_evidence_json_01",
    "raw_google_evidence_json_02",
    "raw_google_evidence_json_03",
    "raw_google_evidence_combined",
    "raw_google_evidence_urls",
    "raw_google_evidence_truncated",
    "raw_google_evidence_json_parts",
    "serper_query_summary",
    "serper_source_urls",
    "serper_result_titles",
    "serper_snippets",
    "raw_evidence_summary",
    "evidence_source_urls",
    "source_file",
    "source_batch",
}

# Long-text columns that wrap inside Excel cells.
WRAP_COLUMNS: set[str] = {
    "raw_google_evidence_combined",
    "raw_google_evidence_urls",
    "serper_snippets",
    "icp_evidence",
    "icp_why_relevant",
    "icp_buying_signals",
    "icp_likely_training_interest",
    "scoring_notes",
    "caller_angle",
    "business_output_reason",
}

# Duplicate-check columns on Opportunity Input.
DUP_COL_NUMBER = "company_number"
DUP_COL_DOMAIN = "canonical_company_domain"


# =============================================================================
# FORMATTING HELPERS
# =============================================================================

def _header_fill() -> PatternFill:
    return PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")


def _header_font() -> Font:
    return Font(bold=True, color="FFFFFF", size=10)


def _col_width(col_name: str) -> int:
    if col_name in WRAP_COLUMNS:
        return 40
    if "json" in col_name.lower():
        return 20
    if "snippet" in col_name.lower() or "evidence" in col_name.lower():
        return 36
    if "url" in col_name.lower():
        return 32
    return min(max(len(str(col_name)) + 2, 12), 48)


def _write_df_to_sheet(ws, df: pd.DataFrame) -> None:
    """Write a DataFrame to ws with header freeze, autofilter, and formatting."""
    hfill = _header_fill()
    hfont = _header_font()
    cols = list(df.columns)

    for ci, col in enumerate(cols, 1):
        cell = ws.cell(row=1, column=ci, value=str(col))
        cell.fill = hfill
        cell.font = hfont
        cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.column_dimensions[get_column_letter(ci)].width = _col_width(col)

    for ri, (_, row) in enumerate(df.iterrows(), 2):
        for ci, col in enumerate(cols, 1):
            val = row[col]
            if isinstance(val, float) and val != val:
                val = ""
            if col in TEXT_COLUMNS or str(col).startswith("google_snippet_"):
                val = str(val) if val not in ("", None) else ""
            cell = ws.cell(row=ri, column=ci, value=val)
            if col in WRAP_COLUMNS:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                cell.alignment = Alignment(vertical="top")

    ws.freeze_panes = "A2"
    if cols:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"
    ws.row_dimensions[1].height = 18


def _write_qa_sheet(ws, qa: dict) -> None:
    hfill = _header_fill()
    hfont = _header_font()
    bold  = Font(bold=True)

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 82

    for ci, val in enumerate(["Metric", "Value"], 1):
        c = ws.cell(row=1, column=ci, value=val)
        c.fill = hfill
        c.font = hfont
    ws.freeze_panes = "A2"

    rows: list[tuple[str, str]] = [
        ("Merge timestamp",         qa["timestamp"]),
        ("Input folder",            qa["input_dir"]),
        ("Output file",             qa["output_file"]),
        ("Input files found",       str(qa["n_files"])),
        ("", ""),
        ("Input files", ""),
    ]
    for fname in qa.get("input_files", []):
        rows.append((f"  {fname}", ""))

    rows += [("", ""), ("Sheets merged (tabular)", "")]
    for sname, nrows in qa.get("rows_per_sheet", {}).items():
        rows.append((f"  {sname}", f"{nrows} rows"))

    rows += [("", ""), ("Sheets copied from first file", "")]
    for sname, src in qa.get("copied_sheets", {}).items():
        rows.append((f"  {sname}", f"source: {src}"))

    rows += [("", ""), ("Sheets skipped", "")]
    for s in qa.get("sheets_skipped", []):
        rows.append((f"  {s}", ""))
    if not qa.get("sheets_skipped"):
        rows.append(("  —", "none"))

    rows += [("", ""), ("Warnings / notes", "")]
    for w in qa.get("warnings", []):
        rows.append(("  ⚠", w))
    if not qa.get("warnings"):
        rows.append(("  —", "No warnings"))

    for ri, (k, v) in enumerate(rows, 2):
        ck = ws.cell(row=ri, column=1, value=str(k))
        cv = ws.cell(row=ri, column=2, value=str(v))
        if k and not k.startswith(" ") and k != "":
            ck.font = bold
        cv.alignment = Alignment(wrap_text=True, vertical="top")

    ws.auto_filter.ref = "A1:B1"


# =============================================================================
# FILE HELPERS
# =============================================================================

def _derive_batch_label(filename: str) -> str:
    stem = Path(filename).stem
    cleaned = re.sub(
        r"_(lead_prioritized|enrichedResults|enriched_results)_\d{8}_?\d{4,6}$",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    return cleaned if cleaned != stem else stem


def collect_files(input_dir: Path, pattern: str) -> list[Path]:
    return sorted(
        p for p in input_dir.glob(pattern)
        if p.is_file() and not p.name.startswith("~$")
    )


def get_sheet_names(xlsx_path: Path) -> list[str]:
    try:
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        names = wb.sheetnames
        wb.close()
        return names
    except Exception as e:
        print(f"  [warn] Could not inspect {xlsx_path.name}: {e}")
        return []


def read_tabular_sheet(xlsx_path: Path, sheet_name: str) -> pd.DataFrame | None:
    """Read a tabular sheet as str-typed DataFrame (NaN → empty string)."""
    try:
        df = pd.read_excel(
            xlsx_path, sheet_name=sheet_name, dtype=str, engine="openpyxl"
        )
        df = df.fillna("").replace({"nan": "", "NaN": ""})
        # Drop fully-unnamed columns that openpyxl injects from empty header cells
        df = df.loc[:, ~df.columns.str.fullmatch(r"Unnamed: \d+")]
        return df
    except Exception as e:
        print(f"  [warn] Could not read '{sheet_name}' from {xlsx_path.name}: {e}")
        return None


def copy_sheet_raw(
    src_wb_path: Path,
    sheet_name: str,
    dest_wb: Workbook,
) -> bool:
    """
    Copy a sheet from src_wb_path into dest_wb preserving cell values only
    (no styles, no merged cells — avoids layout corruption on card-layout sheets).
    Returns True on success.
    """
    try:
        src_wb = load_workbook(src_wb_path, read_only=True, data_only=True)
        if sheet_name not in src_wb.sheetnames:
            src_wb.close()
            return False
        src_ws = src_wb[sheet_name]
        dst_ws = dest_wb.create_sheet(sheet_name)
        for row in src_ws.iter_rows(values_only=True):
            dst_ws.append([v if v is not None else "" for v in row])
        # Basic column widths
        max_col = src_ws.max_column or 1
        for ci in range(1, max_col + 1):
            dst_ws.column_dimensions[get_column_letter(ci)].width = 28
        src_wb.close()
        return True
    except Exception as e:
        print(f"  [warn] Could not copy sheet '{sheet_name}' from {src_wb_path.name}: {e}")
        return False


# =============================================================================
# MERGE HELPERS
# =============================================================================

def merge_tabular_frames(
    frames: list[tuple[str, str, pd.DataFrame]],
) -> pd.DataFrame:
    """
    Merge (source_file, source_batch, df) list into one DataFrame.

    Original columns come FIRST; source_file / source_batch / source_row
    are appended at the RIGHT end.
    """
    if not frames:
        return pd.DataFrame()

    # Union of original columns (insertion-order preserved)
    orig_cols: list[str] = []
    seen: set[str] = set()
    for _, _, df in frames:
        for c in df.columns:
            if c not in seen:
                orig_cols.append(c)
                seen.add(c)

    parts = []
    for source_file, source_batch, df in frames:
        aligned = df.reindex(columns=orig_cols, fill_value="")
        # Append metadata on the RIGHT
        aligned = aligned.copy()
        aligned["source_file"]  = source_file
        aligned["source_batch"] = source_batch
        aligned["source_row"]   = range(1, len(aligned) + 1)
        parts.append(aligned)

    return pd.concat(parts, ignore_index=True)


def add_duplicate_flags(df: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    """Append possible_duplicate_* columns at the far right."""
    for col, flag_col in [
        (DUP_COL_NUMBER, "possible_duplicate_company_number"),
        (DUP_COL_DOMAIN, "possible_duplicate_domain"),
    ]:
        if col in df.columns:
            vals = df[col].astype(str).str.strip()
            counts = vals.map(vals.value_counts())
            flags = (counts > 1) & (vals != "") & (vals.str.lower() != "nan")
            df[flag_col] = flags.map({True: "YES", False: ""})
        else:
            warnings.append(
                f"Duplicate check skipped: '{col}' not found in Opportunity Input."
            )
    return df


# =============================================================================
# MAIN MERGE LOGIC
# =============================================================================

def run_merge(
    input_dir: Path,
    output_path: Path,
    pattern: str = "*.xlsx",
) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[merge] Start:     {ts}")
    print(f"[merge] Input dir: {input_dir}")
    print(f"[merge] Output:    {output_path}")
    print(f"[merge] Pattern:   {pattern}")

    files = collect_files(input_dir, pattern)
    if not files:
        print(f"[merge] ERROR: No files matching '{pattern}' in {input_dir}")
        sys.exit(1)

    print(f"[merge] Found {len(files)} file(s):")
    for f in files:
        print(f"  {f.name}")

    # ── Discover which sheets exist across all files ───────────────────────────
    file_sheets: dict[Path, list[str]] = {}
    for f in files:
        file_sheets[f] = get_sheet_names(f)

    all_present: set[str] = {s for sheets in file_sheets.values() for s in sheets}

    # ── Collect tabular frames ────────────────────────────────────────────────
    # sheet_name -> [(source_file_label, batch_label, df), ...]
    tabular_data: dict[str, list[tuple[str, str, pd.DataFrame]]] = {}
    rows_per_file: dict[str, int] = {}
    warnings: list[str] = []

    for xlsx_path in files:
        label = xlsx_path.name
        batch = _derive_batch_label(xlsx_path.name)
        file_rows = 0
        for sname in TABULAR_SHEET_NAMES:
            if sname not in file_sheets.get(xlsx_path, []):
                continue
            df = read_tabular_sheet(xlsx_path, sname)
            if df is None or df.empty:
                continue
            tabular_data.setdefault(sname, []).append((label, batch, df))
            file_rows += len(df)
        rows_per_file[label] = file_rows

    # ── Identify first file that has each copy_first sheet ───────────────────
    copy_first_source: dict[str, Path] = {}
    for sname in COPY_FIRST_SHEET_NAMES:
        for f in files:
            if sname in file_sheets.get(f, []):
                copy_first_source[sname] = f
                break

    # ── Merge tabular sheets ──────────────────────────────────────────────────
    merged: dict[str, pd.DataFrame] = {}
    rows_per_sheet: dict[str, int] = {}
    sheets_merged: list[str] = []
    sheets_skipped: list[str] = []

    for sname in TABULAR_SHEET_NAMES:
        frames = tabular_data.get(sname, [])
        if not frames:
            if sname in all_present:
                warnings.append(f"'{sname}' found in file list but produced no rows — skipped.")
            sheets_skipped.append(sname)
            continue
        df_merged = merge_tabular_frames(frames)

        # Column-uniformity warning
        col_sets = [set(df.columns) for _, _, df in frames]
        if len(col_sets) > 1:
            union_all = set.union(*col_sets)
            for i, cs in enumerate(col_sets):
                missing = union_all - cs
                if missing:
                    fname = frames[i][0]
                    sample = ", ".join(sorted(missing)[:4])
                    more   = f" (+{len(missing)-4} more)" if len(missing) > 4 else ""
                    warnings.append(
                        f"'{sname}' in {fname}: {len(missing)} missing col(s): {sample}{more}"
                    )

        if sname == "Opportunity Input" and not df_merged.empty:
            df_merged = add_duplicate_flags(df_merged, warnings)

        merged[sname] = df_merged
        rows_per_sheet[sname] = len(df_merged)
        sheets_merged.append(sname)
        print(f"[merge] Tabular  '{sname}': {len(df_merged)} rows from {len(frames)} file(s)")

    # ── Report copy_first findings ────────────────────────────────────────────
    copied_sheets: dict[str, str] = {}
    for sname, src_path in copy_first_source.items():
        copied_sheets[sname] = src_path.name
        print(f"[merge] Copy     '{sname}' from {src_path.name}")
    for sname in COPY_FIRST_SHEET_NAMES:
        if sname not in copy_first_source:
            sheets_skipped.append(sname)
            print(f"[merge] Skipped  '{sname}' (not found in any file)")

    if not merged:
        print("[merge] ERROR: no tabular data merged — nothing to write.")
        sys.exit(1)

    # ── Write output workbook in defined sheet order ─────────────────────────
    print(f"[merge] Writing: {output_path}")
    wb = Workbook()
    wb.remove(wb.active)

    for sname, treatment in SHEET_ORDER:
        if sname == "Merge QA":
            continue  # written last

        if treatment == "tabular":
            if sname not in merged:
                continue
            ws = wb.create_sheet(sname)
            _write_df_to_sheet(ws, merged[sname])

        elif treatment == "copy_first":
            if sname not in copy_first_source:
                continue
            src_path = copy_first_source[sname]
            ok = copy_sheet_raw(src_path, sname, wb)
            if not ok:
                warnings.append(f"Failed to copy '{sname}' from {src_path.name}.")
                sheets_skipped.append(sname)
                if sname in copied_sheets:
                    del copied_sheets[sname]
            elif sname == "Company Profiles":
                # Add an explanatory note at the top of the raw copy
                pass  # values-only copy is already clean

    # Merge QA — always last
    ws_qa = wb.create_sheet("Merge QA")
    _write_qa_sheet(ws_qa, {
        "timestamp":      ts,
        "input_dir":      str(input_dir),
        "output_file":    str(output_path),
        "n_files":        len(files),
        "input_files":    [f.name for f in files],
        "rows_per_file":  rows_per_file,
        "rows_per_sheet": rows_per_sheet,
        "copied_sheets":  copied_sheets,
        "sheets_skipped": sheets_skipped,
        "warnings":       warnings,
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"[merge] Saved: {output_path}")

    # ── Console QA ───────────────────────────────────────────────────────────
    print()
    print(f"[QA] Output:          {output_path}")
    print(f"[QA] Sheets merged:   {sheets_merged}")
    print(f"[QA] Sheets copied:   {list(copied_sheets.keys())}")
    print(f"[QA] Sheets skipped:  {sheets_skipped}")
    print(f"[QA] Rows per sheet:  {rows_per_sheet}")
    if warnings:
        print(f"[QA] Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"     ⚠ {w}")
    else:
        print("[QA] No warnings.")


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge Client Enricher batch-output Excel files into one workbook.",
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
    parser.add_argument("--input-dir", required=True, help="Directory with enricher .xlsx files")
    parser.add_argument("--output",    required=True, help="Path for the merged output .xlsx")
    parser.add_argument("--pattern",   default="*.xlsx", help="Glob pattern (default: *.xlsx)")
    args = parser.parse_args()

    input_dir   = Path(args.input_dir).resolve()
    output_path = Path(args.output).resolve()

    if not input_dir.exists() or not input_dir.is_dir():
        print(f"ERROR: --input-dir not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    run_merge(input_dir, output_path, pattern=args.pattern)


if __name__ == "__main__":
    main()
