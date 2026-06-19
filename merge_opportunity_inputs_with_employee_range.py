"""
merge_opportunity_inputs_with_employee_range.py

Merges only the 'Opportunity Input' sheet from all .xlsx files in the
lead-prioritized output folders for Italy50, Italy100, Italy200.

Output workbook contains three sheets:
  1. Debug View              – compact outlier-review sheet, sorted by flags then score
  2. Opportunity Input Full  – all merged rows, all columns
  3. Merge QA                – run metadata and counts

Adds to every row:
  - source_queue, source_file, source_row  (trace columns, prepended)
  - Chamber of Commerce Employee Range     (placed at output column O, i.e. col 15)
  - debug_foreign_hq_flag, debug_score_flag, debug_domain_flag  (computed)

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
    from openpyxl.formatting.rule import CellIsRule, DataBarRule, Rule
    from openpyxl.styles.differential import DifferentialStyle
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_SHEET = "Opportunity Input"
FULL_SHEET_NAME = "Opportunity Input Full"
DEBUG_SHEET_NAME = "Debug View"
EMP_RANGE_COL = "Chamber of Commerce Employee Range"
OUTPUT_COL_O = 15          # 1-based: column O in the full merged sheet
TRACE_COLS = ["source_queue", "source_file", "source_row"]
DEBUG_FLAG_COLS = ["debug_foreign_hq_flag", "debug_score_flag", "debug_domain_flag"]

EMPLOYEE_RANGE_MAP: dict[str, str] = {
    "italy50":  "50-100 employees",
    "italy100": "100-200 employees",
    "italy200": "200+ employees",
}

# Header fills
HEADER_FILL  = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT  = Font(bold=True, color="FFFFFF")
TRACE_FILL   = PatternFill("solid", fgColor="D6E4F0")
EMP_FILL     = PatternFill("solid", fgColor="E2EFDA")
DEBUG_FILL   = PatternFill("solid", fgColor="FCE4D6")  # light orange for debug headers
FLAG_FILL    = PatternFill("solid", fgColor="FF0000")  # red for non-empty flag cells
FLAG_FONT    = Font(bold=True, color="FFFFFF")

# Debug View ordered columns (A–AK)
DEBUG_VIEW_COLS = [
    "company_name",
    "domain",
    "commercial_fit_score",
    "commercial_tier",
    "outreach_readiness_status",
    EMP_RANGE_COL,
    "sig_foreign_hq_score",
    "foreign_hq_original_score",
    "foreign_hq_sanitized",
    "foreign_hq_sanitizer_reason",
    "sig_foreign_hq_evidence",
    "evidence_source_urls",
    "serper_source_urls",
    "google_snippet_01_title",
    "google_snippet_01_url",
    "google_snippet_01_text",
    "sig_intl_footprint_score",
    "sig_intl_footprint_evidence",
    "sig_explicit_lnd_score",
    "sig_explicit_lnd_evidence",
    "sig_employer_branding_score",
    "sig_employer_branding_evidence",
    "sig_lnd_onboarding_score",
    "sig_lnd_onboarding_evidence",
    "top_positive_signals",
    "gaps_missing_signals",
    "caller_angle",
    "scoring_notes",
    "needs_manual_review",
    "possible_domain_mismatch",
    "domain_check_reason",
    "debug_foreign_hq_flag",
    "debug_score_flag",
    "debug_domain_flag",
    "source_queue",
    "source_file",
    "source_row",
]

# Column widths for Debug View
_DEBUG_COL_WIDTHS: dict[str, int] = {
    "company_name": 35,
    "domain": 25,
    "commercial_fit_score": 20,
    "commercial_tier": 16,
    "outreach_readiness_status": 22,
    EMP_RANGE_COL: 26,
    "sig_foreign_hq_score": 20,
    "foreign_hq_original_score": 22,
    "foreign_hq_sanitized": 20,
    "foreign_hq_sanitizer_reason": 30,
    "sig_foreign_hq_evidence": 55,
    "evidence_source_urls": 45,
    "serper_source_urls": 45,
    "google_snippet_01_title": 40,
    "google_snippet_01_url": 40,
    "google_snippet_01_text": 60,
    "sig_intl_footprint_score": 22,
    "sig_intl_footprint_evidence": 45,
    "sig_explicit_lnd_score": 20,
    "sig_explicit_lnd_evidence": 45,
    "sig_employer_branding_score": 24,
    "sig_employer_branding_evidence": 45,
    "sig_lnd_onboarding_score": 22,
    "sig_lnd_onboarding_evidence": 45,
    "top_positive_signals": 55,
    "gaps_missing_signals": 45,
    "caller_angle": 40,
    "scoring_notes": 60,
    "needs_manual_review": 20,
    "possible_domain_mismatch": 22,
    "domain_check_reason": 35,
    "debug_foreign_hq_flag": 38,
    "debug_score_flag": 38,
    "debug_domain_flag": 32,
    "source_queue": 14,
    "source_file": 48,
    "source_row": 12,
}

_WRAP_COLS = {
    "sig_foreign_hq_evidence",
    "google_snippet_01_text",
    "top_positive_signals",
    "scoring_notes",
    "sig_intl_footprint_evidence",
    "sig_explicit_lnd_evidence",
    "sig_employer_branding_evidence",
    "sig_lnd_onboarding_evidence",
    "gaps_missing_signals",
    "caller_angle",
}

# Domain-check warning keywords
_DOMAIN_WARN_WORDS = {"mismatch", "uncertain", "review", "fallback", "generic"}

# Columns that get blue data-bar conditional formatting
DATABAR_COLS = {"Final Commercial Fit Score", "commercial_fit_score"}

# All score columns (for future use / reference)
SCORE_COL_NAMES = {
    "Final Commercial Fit Score",
    "commercial_fit_score",
    "recommended_final_score",
    "sig_foreign_hq_score",
    "sig_explicit_lnd_score",
    "sig_intl_footprint_score",
    "sig_employer_branding_score",
    "sig_lnd_onboarding_score",
}

# Tier-based row fills (fallback row coloring)
HOT_FILL  = PatternFill("solid", fgColor="C6EFCE")  # light green
WARM_FILL = PatternFill("solid", fgColor="DDEBF7")  # light blue
COOL_FILL = PatternFill("solid", fgColor="FCE4D6")  # light orange
PASS_FILL = PatternFill("solid", fgColor="FFC7CE")  # light red/pink


# ---------------------------------------------------------------------------
# Safe workbook loader
# ---------------------------------------------------------------------------

def _safe_load(path: Path) -> tuple[openpyxl.Workbook | None, str]:
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        return wb, ""
    except (zipfile.BadZipFile, KeyError, OSError, Exception) as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Safe output path
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
# Formatting helpers
# ---------------------------------------------------------------------------

def _get_row_fill(row_values: list, headers: list[str]) -> "PatternFill | None":
    """Return tier-based fill for a data row, or None if no tier/score matches."""
    h_idx = {h: i for i, h in enumerate(headers)}
    tier = str(row_values[h_idx["commercial_tier"]] or "").strip().lower() \
        if "commercial_tier" in h_idx else ""
    score = None
    for score_col in ("commercial_fit_score", "Final Commercial Fit Score"):
        if score_col in h_idx:
            score = _to_float(row_values[h_idx[score_col]])
            break

    if tier == "hot" or (score is not None and score >= 8.5):
        return HOT_FILL
    if tier == "warm" or (score is not None and score >= 5.0):
        return WARM_FILL
    if tier == "cool":
        return COOL_FILL
    if tier == "pass":
        return PASS_FILL
    return None


def _apply_databar_formatting(ws, headers: list[str], first_row: int, last_row: int) -> None:
    """Add blue data-bar CF rules for DATABAR_COLS columns that exist in headers."""
    if first_row > last_row:
        return
    for col_idx, h in enumerate(headers, start=1):
        if h in DATABAR_COLS:
            col_letter = get_column_letter(col_idx)
            ws.conditional_formatting.add(
                f"{col_letter}{first_row}:{col_letter}{last_row}",
                DataBarRule(
                    start_type="num", start_value=0,
                    end_type="num", end_value=10,
                    color="638EC6",
                    showValue=True,
                ),
            )


# ---------------------------------------------------------------------------
# Column layout helpers (full merged sheet)
# ---------------------------------------------------------------------------

def _build_output_headers(source_headers: list[str]) -> list[str]:
    """
    TRACE_COLS + source cols (minus existing emp range) + EMP_RANGE_COL at col O.
    """
    clean_source = [h for h in source_headers if h != EMP_RANGE_COL]
    combined = TRACE_COLS + clean_source
    while len(combined) < OUTPUT_COL_O - 1:
        combined.append("")
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
    emp_range_value = EMPLOYEE_RANGE_MAP.get(queue.lower(), "")
    clean_source = [h for h in source_headers if h != EMP_RANGE_COL]
    src_dict: dict[str, Any] = {
        h: (source_row[i] if i < len(source_row) else None)
        for i, h in enumerate(clean_source)
    }
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
# Debug flag computation
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_truthy(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"true", "yes", "1", "1.0", "x"}


def _is_blank_or_short(v: Any, threshold: int = 10) -> bool:
    if v is None:
        return True
    return len(str(v).strip()) < threshold


def compute_debug_flags(row_dict: dict[str, Any]) -> tuple[str, str, str]:
    """Returns (debug_foreign_hq_flag, debug_score_flag, debug_domain_flag)."""

    # ── foreign HQ flag ─────────────────────────────────────────────────────
    fhq_reasons: list[str] = []
    fhq_score = _to_float(row_dict.get("sig_foreign_hq_score"))
    fhq_orig  = _to_float(row_dict.get("foreign_hq_original_score"))
    fhq_sanit = _is_truthy(row_dict.get("foreign_hq_sanitized"))
    fhq_evid  = row_dict.get("sig_foreign_hq_evidence")

    if fhq_score is not None and fhq_score >= 7 and _is_blank_or_short(fhq_evid):
        fhq_reasons.append("High foreign HQ score, weak evidence")
    if fhq_sanit:
        fhq_reasons.append("Foreign HQ sanitized")
    if (
        fhq_orig is not None
        and fhq_score is not None
        and fhq_orig - fhq_score >= 1.5
    ):
        fhq_reasons.append("Foreign HQ score reduced by sanitizer")

    # ── score flag ──────────────────────────────────────────────────────────
    score_reasons: list[str] = []
    fit_score    = _to_float(row_dict.get("commercial_fit_score"))
    lnd_score    = _to_float(row_dict.get("sig_explicit_lnd_score"))
    top_signals  = row_dict.get("top_positive_signals")

    if fit_score is not None and fit_score >= 8.5:
        low_fhq = fhq_score is None or fhq_score < 4
        low_lnd = lnd_score is None or lnd_score < 4
        if low_fhq and low_lnd:
            score_reasons.append("High score, check supporting signals")
        if _is_blank_or_short(top_signals, threshold=5):
            score_reasons.append("High score, no top positive signals")

    # ── domain flag ─────────────────────────────────────────────────────────
    domain_reasons: list[str] = []
    if _is_truthy(row_dict.get("possible_domain_mismatch")):
        domain_reasons.append("Possible domain mismatch")
    if _is_truthy(row_dict.get("needs_manual_review")):
        domain_reasons.append("Manual review needed")
    dcr = str(row_dict.get("domain_check_reason") or "").lower()
    if any(w in dcr for w in _DOMAIN_WARN_WORDS):
        domain_reasons.append("Domain check warning")

    return (
        "; ".join(fhq_reasons),
        "; ".join(score_reasons),
        "; ".join(domain_reasons),
    )


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
      all_data_rows  – each row includes all output_headers columns
                       PLUS debug flag values appended at the end
      output_headers – final column list for the full merged sheet
      report_rows    – per-file status dicts
      rows_by_queue  – {queue: row_count}
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
                report_rows.append({"queue": queue, "file": xlsx_path.name,
                                    "status": "SKIPPED_INVALID_WORKBOOK",
                                    "rows_read": 0, "notes": err})
                continue

            if TARGET_SHEET not in wb.sheetnames:
                wb.close()
                print(f"    [SKIP-NO-SHEET] {xlsx_path.name}")
                report_rows.append({"queue": queue, "file": xlsx_path.name,
                                    "status": "SKIPPED_MISSING_OPPORTUNITY_INPUT",
                                    "rows_read": 0,
                                    "notes": f"Sheet '{TARGET_SHEET}' not found. "
                                             f"Available: {wb.sheetnames}"})
                continue

            ws = wb[TARGET_SHEET]
            rows_iter = ws.iter_rows(values_only=True)
            try:
                header_row = next(rows_iter)
            except StopIteration:
                wb.close()
                report_rows.append({"queue": queue, "file": xlsx_path.name,
                                    "status": "SKIPPED_EMPTY_SHEET",
                                    "rows_read": 0, "notes": "Sheet has no rows"})
                continue

            source_headers = [str(h).strip() if h is not None else "" for h in header_row]

            if output_headers is None:
                output_headers = _build_output_headers(source_headers)

            rows_read = 0
            for src_row_idx, src_row in enumerate(rows_iter, start=2):
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
                # Compute debug flags; append as extra values beyond output_headers
                row_dict = dict(zip(output_headers, out_row))
                flags = compute_debug_flags(row_dict)
                out_row = out_row + list(flags)   # 3 extra values
                all_data_rows.append(out_row)
                rows_read += 1

            wb.close()
            rows_by_queue[queue] = rows_by_queue.get(queue, 0) + rows_read
            print(f"    [OK  {rows_read:>6} rows] {xlsx_path.name}")
            report_rows.append({"queue": queue, "file": xlsx_path.name,
                                 "status": "MERGED", "rows_read": rows_read, "notes": ""})

    return all_data_rows, output_headers, report_rows, rows_by_queue


# ---------------------------------------------------------------------------
# Row dict helper (for Debug View)
# ---------------------------------------------------------------------------

def _row_as_dict(row: list[Any], full_headers: list[str]) -> dict[str, Any]:
    """Map a merged data row (including trailing flag values) to a dict."""
    extended = full_headers + DEBUG_FLAG_COLS
    d: dict[str, Any] = {}
    for i, h in enumerate(extended):
        d[h] = row[i] if i < len(row) else None
    return d


# ---------------------------------------------------------------------------
# Sorting key for Debug View
# ---------------------------------------------------------------------------

def _debug_sort_key(row_dict: dict[str, Any]) -> tuple:
    has_flag = int(
        bool(row_dict.get("debug_foreign_hq_flag"))
        or bool(row_dict.get("debug_score_flag"))
        or bool(row_dict.get("debug_domain_flag"))
    )
    score = _to_float(row_dict.get("commercial_fit_score")) or 0.0
    fhq   = _to_float(row_dict.get("sig_foreign_hq_score")) or 0.0
    return (-has_flag, -score, -fhq)


# ---------------------------------------------------------------------------
# Output workbook writer
# ---------------------------------------------------------------------------

def _col_width_for_full(header: str) -> int:
    widths = {
        "source_queue": 14, "source_file": 50, "source_row": 10,
        EMP_RANGE_COL: 30, "company_name": 40,
        "website_url": 35, "domain": 28,
        "commercial_fit_score": 22, "commercial_tier": 16,
    }
    return widths.get(header, max(12, min(len(header) + 4, 40)))


def _write_full_sheet(ws, data_rows: list[list[Any]], output_headers: list[str]) -> None:
    """Write the Opportunity Input Full sheet."""
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
        ws.column_dimensions[get_column_letter(col_idx)].width = _col_width_for_full(header)

    for data_row_idx, row in enumerate(data_rows, start=2):
        row_values = row[:len(output_headers)]
        ws.append(row_values)
        fill = _get_row_fill(row_values, output_headers)
        if fill is not None:
            for col_idx in range(1, len(output_headers) + 1):
                ws.cell(row=data_row_idx, column=col_idx).fill = fill

    last_row = len(data_rows) + 1
    if last_row >= 2:
        _apply_databar_formatting(ws, output_headers, 2, last_row)

    ws.freeze_panes = "D2"
    ws.auto_filter.ref = ws.dimensions


def _write_debug_sheet(
    ws, data_rows: list[list[Any]], full_headers: list[str]
) -> tuple[int, int, int, int]:
    """
    Write the Debug View sheet.
    Returns (total_rows, fhq_flagged, score_flagged, domain_flagged).
    """
    # Build row dicts and sort
    row_dicts = [_row_as_dict(r, full_headers) for r in data_rows]
    row_dicts.sort(key=_debug_sort_key)

    # Write headers
    ws.append(DEBUG_VIEW_COLS)
    for col_idx, col_name in enumerate(DEBUG_VIEW_COLS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        is_flag = col_name in DEBUG_FLAG_COLS
        is_trace = col_name in TRACE_COLS
        is_emp = col_name == EMP_RANGE_COL

        if is_flag:
            cell.fill = DEBUG_FILL
            cell.font = Font(bold=True)
        elif is_trace:
            cell.fill = TRACE_FILL
            cell.font = Font(bold=True)
        elif is_emp:
            cell.fill = EMP_FILL
            cell.font = Font(bold=True)
        else:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", wrap_text=False)
        ws.column_dimensions[get_column_letter(col_idx)].width = (
            _DEBUG_COL_WIDTHS.get(col_name, 20)
        )

    # Data rows
    fhq_flagged = score_flagged = domain_flagged = 0
    for row_idx, rd in enumerate(row_dicts, start=2):
        row_values = [rd.get(col) for col in DEBUG_VIEW_COLS]
        ws.append(row_values)

        if rd.get("debug_foreign_hq_flag"):
            fhq_flagged += 1
        if rd.get("debug_score_flag"):
            score_flagged += 1
        if rd.get("debug_domain_flag"):
            domain_flagged += 1

        # Wrap text in evidence/long-text columns
        for col_idx, col_name in enumerate(DEBUG_VIEW_COLS, start=1):
            if col_name in _WRAP_COLS:
                ws.cell(row=row_idx, column=col_idx).alignment = Alignment(wrap_text=True)

        # Highlight non-empty flag cells
        flag_col_indices = {
            col_name: col_idx + 1
            for col_idx, col_name in enumerate(DEBUG_VIEW_COLS)
            if col_name in DEBUG_FLAG_COLS
        }
        for flag_col, flag_col_idx in flag_col_indices.items():
            if rd.get(flag_col):
                cell = ws.cell(row=row_idx, column=flag_col_idx)
                cell.fill = FLAG_FILL
                cell.font = FLAG_FONT

    ws.freeze_panes = "C2"   # freeze row 1 + cols A-B
    ws.auto_filter.ref = ws.dimensions

    last_row = len(row_dicts) + 1

    # Blue data bars for commercial_fit_score
    _apply_databar_formatting(ws, DEBUG_VIEW_COLS, 2, last_row)

    # CellIsRule highlights: green for high score, yellow for high fhq
    score_col_letter = get_column_letter(DEBUG_VIEW_COLS.index("commercial_fit_score") + 1)
    fhq_col_letter   = get_column_letter(DEBUG_VIEW_COLS.index("sig_foreign_hq_score") + 1)

    green_fill  = PatternFill("solid", fgColor="C6EFCE")
    yellow_fill = PatternFill("solid", fgColor="FFEB9C")

    ws.conditional_formatting.add(
        f"{score_col_letter}2:{score_col_letter}{last_row}",
        CellIsRule(operator="greaterThanOrEqual", formula=["8.5"],
                   fill=green_fill, font=Font(bold=True)),
    )
    ws.conditional_formatting.add(
        f"{fhq_col_letter}2:{fhq_col_letter}{last_row}",
        CellIsRule(operator="greaterThanOrEqual", formula=["7"],
                   fill=yellow_fill),
    )

    return len(row_dicts), fhq_flagged, score_flagged, domain_flagged


def write_output_workbook(
    output_path: Path,
    data_rows: list[list[Any]],
    output_headers: list[str],
    qa_meta: dict,
    rows_by_queue: dict[str, int],
) -> tuple[int, int, int, int]:
    """Write workbook with Debug View, Opportunity Input Full, Merge QA.
    Returns (debug_total, fhq_flagged, score_flagged, domain_flagged).
    """
    wb = Workbook()

    # ── Sheet 1: Debug View ─────────────────────────────────────────────────
    ws_debug = wb.active
    ws_debug.title = DEBUG_SHEET_NAME
    debug_total, fhq_flagged, score_flagged, domain_flagged = _write_debug_sheet(
        ws_debug, data_rows, output_headers
    )

    # ── Sheet 2: Opportunity Input Full ────────────────────────────────────
    ws_full = wb.create_sheet(FULL_SHEET_NAME)
    _write_full_sheet(ws_full, data_rows, output_headers)

    # ── Sheet 3: Merge QA ───────────────────────────────────────────────────
    ws_qa = wb.create_sheet("Merge QA")
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
        ("── debug view ──", ""),
        ("debug_view_created",            "YES"),
        ("debug_view_rows",               debug_total),
        ("rows_with_debug_foreign_hq_flag", fhq_flagged),
        ("rows_with_debug_score_flag",    score_flagged),
        ("rows_with_debug_domain_flag",   domain_flagged),
        ("", ""),
        ("output_path", qa_meta.get("output_path", "")),
    ]
    ws_qa.column_dimensions["A"].width = 36
    ws_qa.column_dimensions["B"].width = 80
    for r_idx, (k, v) in enumerate(qa_rows, start=1):
        ws_qa.cell(row=r_idx, column=1, value=k).font = Font(bold=True)
        ws_qa.cell(row=r_idx, column=2, value=v)

    wb.save(output_path)
    return debug_total, fhq_flagged, score_flagged, domain_flagged


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
            "and add Chamber of Commerce employee range + debug flag columns."
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
                   help="Full path for the output .xlsx (optional)")
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

    data_rows, output_headers, report_rows, rows_by_queue = merge_queues(
        target_root=target_root,
        queues=queues,
        input_subfolder=input_subfolder,
    )

    if output_headers is None:
        sys.exit(
            "\nNo data found. No files with an 'Opportunity Input' sheet were merged."
        )

    files_scanned   = len(report_rows)
    files_merged    = sum(1 for r in report_rows if r["status"] == "MERGED")
    files_no_sheet  = sum(1 for r in report_rows
                          if r["status"] == "SKIPPED_MISSING_OPPORTUNITY_INPUT")
    files_invalid   = sum(1 for r in report_rows
                          if r["status"] == "SKIPPED_INVALID_WORKBOOK")
    total_rows      = len(data_rows)

    merge_dir.mkdir(parents=True, exist_ok=True)
    debug_total, fhq_flagged, score_flagged, domain_flagged = write_output_workbook(
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

    csv_path = write_csv_report(report_rows, report_dir, ts)

    print("\n" + "=" * 66)
    print("MERGE SUMMARY")
    print("=" * 66)
    print(f"  Queues scanned                   : {len(queues)}")
    print(f"  Source folders                   : {input_subfolder}")
    print(f"  Files scanned                    : {files_scanned}")
    print(f"  Files merged                     : {files_merged}")
    print(f"  Total rows merged                : {total_rows}")
    for q, cnt in rows_by_queue.items():
        print(f"    {q:<20}           : {cnt}")
    print(f"  Skipped (no sheet)               : {files_no_sheet}")
    print(f"  Skipped (invalid wb)             : {files_invalid}")
    print()
    print(f"  Debug View rows                  : {debug_total}")
    print(f"    debug_foreign_hq_flag set      : {fhq_flagged}")
    print(f"    debug_score_flag set           : {score_flagged}")
    print(f"    debug_domain_flag set          : {domain_flagged}")
    print()
    print(f"  Output file                      : {output_path}")
    print(f"  CSV report                       : {csv_path}")
    print("=" * 66 + "\n")

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
