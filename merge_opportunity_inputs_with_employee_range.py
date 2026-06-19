"""
merge_opportunity_inputs_with_employee_range.py

Merges only the 'Opportunity Input' sheet from all .xlsx files in the
lead-prioritized output folders for Italy50, Italy100, Italy200.

Adds:
  - source_queue, source_file, source_row  (trace columns, prepended)
  - Chamber of Commerce Employee Range     (placed at output column O, i.e. col 15)

Usage:
  python merge_opportunity_inputs_with_employee_range.py \
      --target-root "C:\\Users\\...\\Myngle" \
      --queues Italy50 Italy100 Italy200

  # Use url-patched folder instead:
  python merge_opportunity_inputs_with_employee_range.py \
      --target-root "C:\\Users\\...\\Myngle" \
      --queues Italy50 Italy100 Italy200 \
      --input-subfolder "02_lead_prioritized_url_patched"
"""

from __future__ import annotations

import argparse
import csv
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
try:
    import openpyxl
    from openpyxl import load_workbook, Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_SHEET = "Opportunity Input"
EMP_RANGE_COL = "Chamber of Commerce Employee Range"
OUTPUT_COL_O = 15          # 1-based: column O
TRACE_COLS = ["source_queue", "source_file", "source_row"]

EMPLOYEE_RANGE_MAP: dict[str, str] = {
    "italy50":  "50-100 employees",
    "italy100": "100-200 employees",
    "italy200": "200+ employees",
}

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF")
TRACE_FILL  = PatternFill("solid", fgColor="D6E4F0")
EMP_FILL    = PatternFill("solid", fgColor="E2EFDA")

# ---------------------------------------------------------------------------
# Safe workbook loader
# ---------------------------------------------------------------------------

def _safe_load(path: Path) -> tuple[openpyxl.Workbook | None, str]:
    """Return (wb, error). wb is None if unreadable."""
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        return wb, ""
    except (zipfile.BadZipFile, KeyError, OSError, Exception) as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Safe output path (no overwrite unless --overwrite)
# ---------------------------------------------------------------------------

def _safe_output_path(path: Path, overwrite: bool) -> Path:
    if not path.exists() or overwrite:
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Column layout helpers
# ---------------------------------------------------------------------------

def _build_output_headers(source_headers: list[str], queue: str) -> list[str]:
    """
    Combine trace cols + source cols + employee range col at position O (15).

    Layout:
      [0]  source_queue
      [1]  source_file
      [2]  source_row
      [3..] original Opportunity Input columns  (excluding any existing emp range col)
      column O (index 14) = Chamber of Commerce Employee Range

    If there are fewer than 14 columns total before inserting at O, pad with
    empty-named columns so the emp range always lands at column 15.
    """
    # Strip existing emp range col from source headers to avoid duplication
    clean_source = [h for h in source_headers if h != EMP_RANGE_COL]

    combined = TRACE_COLS + clean_source   # indices 0..N

    # Ensure at least 14 positions exist before inserting emp range at index 14
    while len(combined) < OUTPUT_COL_O - 1:
        combined.append("")

    # Insert at index 14 (= column O, 1-based 15)
    combined.insert(OUTPUT_COL_O - 1, EMP_RANGE_COL)
    return combined


def _row_to_output(
    source_row: tuple,
    source_headers: list[str],
    output_headers: list[str],
    queue: str,
    filename: str,
    row_num: int,
) -> list[Any]:
    """Map one source data row to the output column layout."""
    emp_range_value = EMPLOYEE_RANGE_MAP.get(queue.lower(), "")

    # Build a dict from source headers to values
    src_dict: dict[str, Any] = {}
    clean_source = [h for h in source_headers if h != EMP_RANGE_COL]
    for i, h in enumerate(clean_source):
        src_dict[h] = source_row[i] if i < len(source_row) else None

    # Trace values
    trace_dict = {
        "source_queue": queue,
        "source_file": filename,
        "source_row": row_num,
    }

    out_row: list[Any] = []
    for col_name in output_headers:
        if col_name in trace_dict:
            out_row.append(trace_dict[col_name])
        elif col_name == EMP_RANGE_COL:
            out_row.append(emp_range_value)
        elif col_name in src_dict:
            out_row.append(src_dict[col_name])
        else:
            out_row.append(None)
    return out_row


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _discover_files(queue_dir: Path, subfolder: str) -> list[Path]:
    folder = queue_dir / subfolder
    if not folder.exists():
        return []
    return sorted(
        f for f in folder.glob("*.xlsx")
        if not f.name.startswith("~$")
    )


# ---------------------------------------------------------------------------
# Core merge
# ---------------------------------------------------------------------------

def merge_queues(
    target_root: Path,
    queues: list[str],
    input_subfolder: str,
) -> tuple[list[list[Any]], list[str] | None, list[dict], dict[str, int]]:
    """
    Returns:
      all_data_rows    – list of output rows (no header)
      output_headers   – final column header list (or None if no data)
      report_rows      – per-file status dicts
      rows_by_queue    – {queue: row_count}
    """
    all_data_rows: list[list[Any]] = []
    output_headers: list[str] | None = None
    report_rows: list[dict] = []
    rows_by_queue: dict[str, int] = {}

    for queue in queues:
        queue_dir = target_root / queue
        if not queue_dir.exists():
            print(f"  [WARN] Queue folder not found, skipping: {queue_dir}")
            continue

        files = _discover_files(queue_dir, input_subfolder)
        print(f"\n  Queue {queue}: {len(files)} file(s) in {queue_dir / input_subfolder}")
        rows_by_queue[queue] = 0

        for xlsx_path in files:
            wb, err = _safe_load(xlsx_path)
            if wb is None:
                print(f"    [SKIP-CORRUPT ] {xlsx_path.name}")
                report_rows.append({
                    "queue": queue,
                    "file": xlsx_path.name,
                    "status": "SKIPPED_INVALID_WORKBOOK",
                    "rows_read": 0,
                    "notes": err,
                })
                continue

            if TARGET_SHEET not in wb.sheetnames:
                wb.close()
                print(f"    [SKIP-NO-SHEET] {xlsx_path.name}")
                report_rows.append({
                    "queue": queue,
                    "file": xlsx_path.name,
                    "status": "SKIPPED_MISSING_OPPORTUNITY_INPUT",
                    "rows_read": 0,
                    "notes": f"Sheet '{TARGET_SHEET}' not found. "
                             f"Available: {wb.sheetnames}",
                })
                continue

            ws = wb[TARGET_SHEET]
            rows_iter = ws.iter_rows(values_only=True)

            # Read header row
            try:
                header_row = next(rows_iter)
            except StopIteration:
                wb.close()
                report_rows.append({
                    "queue": queue,
                    "file": xlsx_path.name,
                    "status": "SKIPPED_EMPTY_SHEET",
                    "rows_read": 0,
                    "notes": "Sheet has no rows",
                })
                continue

            source_headers = [str(h).strip() if h is not None else "" for h in header_row]

            # Build output headers on first encounter (union of all sheets may differ
            # between files; we use the first file's headers as the template and rely
            # on _row_to_output's dict-lookup to handle minor column differences)
            if output_headers is None:
                output_headers = _build_output_headers(source_headers, queue)

            rows_read = 0
            for src_row_idx, src_row in enumerate(rows_iter, start=2):
                # Skip entirely empty rows
                if all(v is None or str(v).strip() == "" for v in src_row):
                    continue
                out_row = _row_to_output(
                    source_row=src_row,
                    source_headers=source_headers,
                    output_headers=output_headers,
                    queue=queue,
                    filename=xlsx_path.name,
                    row_num=src_row_idx,
                )
                all_data_rows.append(out_row)
                rows_read += 1

            wb.close()
            rows_by_queue[queue] = rows_by_queue.get(queue, 0) + rows_read
            print(f"    [OK  {rows_read:>6} rows] {xlsx_path.name}")
            report_rows.append({
                "queue": queue,
                "file": xlsx_path.name,
                "status": "MERGED",
                "rows_read": rows_read,
                "notes": "",
            })

    return all_data_rows, output_headers, report_rows, rows_by_queue


# ---------------------------------------------------------------------------
# Output workbook writer
# ---------------------------------------------------------------------------

def _col_width_for(header: str) -> int:
    widths = {
        "source_queue": 14,
        "source_file": 50,
        "source_row": 10,
        EMP_RANGE_COL: 30,
        "company_name": 40,
        "website_url": 35,
        "domain": 28,
        "commercial_fit_score": 22,
        "commercial_tier": 16,
    }
    return widths.get(header, max(12, min(len(header) + 4, 40)))


def write_output_workbook(
    output_path: Path,
    data_rows: list[list[Any]],
    output_headers: list[str],
    qa_meta: dict,
    rows_by_queue: dict[str, int],
) -> None:
    wb = Workbook()

    # ── Sheet 1: Opportunity Input ──────────────────────────────────────────
    ws = wb.active
    ws.title = TARGET_SHEET

    # Header row
    ws.append(output_headers)
    for col_idx, header in enumerate(output_headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        if header in TRACE_COLS:
            cell.fill = TRACE_FILL
            cell.font = Font(bold=True)
        elif header == EMP_RANGE_COL:
            cell.fill = EMP_FILL
            cell.font = Font(bold=True)
        else:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = _col_width_for(header)

    # Data rows
    for row in data_rows:
        ws.append(row)

    ws.freeze_panes = "D2"
    ws.auto_filter.ref = ws.dimensions

    # ── Sheet 2: Merge QA ───────────────────────────────────────────────────
    qa = wb.create_sheet("Merge QA")
    qa_rows = [
        ("timestamp",                    qa_meta.get("timestamp", "")),
        ("target_root",                  qa_meta.get("target_root", "")),
        ("input_subfolder",              qa_meta.get("input_subfolder", "")),
        ("queues_included",              ", ".join(qa_meta.get("queues", []))),
        ("files_scanned",                qa_meta.get("files_scanned", 0)),
        ("files_merged",                 qa_meta.get("files_merged", 0)),
        ("files_skipped_no_sheet",       qa_meta.get("files_skipped_no_sheet", 0)),
        ("files_skipped_invalid",        qa_meta.get("files_skipped_invalid", 0)),
        ("total_rows_merged",            qa_meta.get("total_rows", 0)),
        ("", ""),
        ("── rows by queue ──", ""),
    ]
    for q, cnt in rows_by_queue.items():
        qa_rows.append((f"  {q}", cnt))
    qa_rows += [
        ("", ""),
        ("output_path", qa_meta.get("output_path", "")),
    ]

    qa.column_dimensions["A"].width = 32
    qa.column_dimensions["B"].width = 80
    for r_idx, (k, v) in enumerate(qa_rows, start=1):
        qa.cell(row=r_idx, column=1, value=k).font = Font(bold=True)
        qa.cell(row=r_idx, column=2, value=v)

    wb.save(output_path)


# ---------------------------------------------------------------------------
# CSV report
# ---------------------------------------------------------------------------

def write_csv_report(report_rows: list[dict], report_dir: Path, ts: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / f"merge_opportunity_inputs_report_{ts}.csv"
    fields = ["queue", "file", "status", "rows_read", "notes"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in report_rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return csv_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Merge 'Opportunity Input' sheets from lead-prioritized folders "
            "and add Chamber of Commerce employee range column."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--target-root", required=True,
                   help="Root folder, e.g. C:\\Users\\...\\Myngle")
    p.add_argument("--queues", nargs="+", default=["Italy50", "Italy100", "Italy200"],
                   help="Queue folder names (default: Italy50 Italy100 Italy200)")
    p.add_argument("--input-subfolder", default="02_lead_prioritized",
                   help="Subfolder inside each queue to read from "
                        "(default: 02_lead_prioritized)")
    p.add_argument("--output", default=None,
                   help="Full path for the output .xlsx (optional; "
                        "default: <target-root>\\_merge_opportunity_inputs\\<auto-name>)")
    p.add_argument("--overwrite", action="store_true", default=False,
                   help="Overwrite existing output file")
    p.add_argument("--report-dir", default=None,
                   help="Directory for the CSV report "
                        "(default: <target-root>\\_merge_opportunity_inputs)")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    target_root = Path(args.target_root)
    if not target_root.exists():
        sys.exit(f"Target root not found: {target_root}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    queues: list[str] = args.queues
    input_subfolder: str = args.input_subfolder

    # Output path
    merge_dir = target_root / "_merge_opportunity_inputs"
    report_dir = Path(args.report_dir) if args.report_dir else merge_dir

    if args.output:
        raw_output = Path(args.output)
    else:
        queues_str = "_".join(queues)
        raw_output = merge_dir / (
            f"{queues_str}_ALL_opportunity_input_with_employee_range_{ts}.xlsx"
        )

    output_path = _safe_output_path(raw_output, args.overwrite)

    print(f"\n[MERGE] merge_opportunity_inputs_with_employee_range.py")
    print(f"  Target root      : {target_root}")
    print(f"  Queues           : {queues}")
    print(f"  Input subfolder  : {input_subfolder}")
    print(f"  Output           : {output_path}")

    # ── Run merge ────────────────────────────────────────────────────────────
    data_rows, output_headers, report_rows, rows_by_queue = merge_queues(
        target_root=target_root,
        queues=queues,
        input_subfolder=input_subfolder,
    )

    if output_headers is None:
        sys.exit(
            "\nNo data found. No files with an 'Opportunity Input' sheet were merged."
        )

    # ── Stats ─────────────────────────────────────────────────────────────────
    files_scanned = len(report_rows)
    files_merged  = sum(1 for r in report_rows if r["status"] == "MERGED")
    files_no_sheet = sum(1 for r in report_rows
                         if r["status"] == "SKIPPED_MISSING_OPPORTUNITY_INPUT")
    files_invalid  = sum(1 for r in report_rows
                         if r["status"] == "SKIPPED_INVALID_WORKBOOK")
    total_rows = len(data_rows)

    # ── Write output workbook ────────────────────────────────────────────────
    merge_dir.mkdir(parents=True, exist_ok=True)
    write_output_workbook(
        output_path=output_path,
        data_rows=data_rows,
        output_headers=output_headers,
        qa_meta={
            "timestamp": ts,
            "target_root": str(target_root),
            "input_subfolder": input_subfolder,
            "queues": queues,
            "files_scanned": files_scanned,
            "files_merged": files_merged,
            "files_skipped_no_sheet": files_no_sheet,
            "files_skipped_invalid": files_invalid,
            "total_rows": total_rows,
            "output_path": str(output_path),
        },
        rows_by_queue=rows_by_queue,
    )

    # ── Write CSV report ─────────────────────────────────────────────────────
    csv_path = write_csv_report(report_rows, report_dir, ts)

    # ── Console summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("MERGE SUMMARY")
    print("=" * 64)
    print(f"  Queues scanned            : {len(queues)}")
    print(f"  Source folders            : {input_subfolder}")
    print(f"  Files scanned             : {files_scanned}")
    print(f"  Files merged              : {files_merged}")
    print(f"  Total rows merged         : {total_rows}")
    for q, cnt in rows_by_queue.items():
        print(f"    {q:<18}       : {cnt}")
    print(f"  Skipped (no sheet)        : {files_no_sheet}")
    print(f"  Skipped (invalid wb)      : {files_invalid}")
    print(f"  Output file               : {output_path}")
    print(f"  CSV report                : {csv_path}")
    print("=" * 64 + "\n")

    if files_no_sheet or files_invalid:
        print("Skipped files:")
        for r in report_rows:
            if r["status"] != "MERGED":
                print(f"  [{r['status']}] {r['queue']} / {r['file']}")
                if r["notes"]:
                    print(f"    {r['notes']}")
        print()


if __name__ == "__main__":
    main()
