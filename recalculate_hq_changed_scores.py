"""
Recalculate commercial fit scores after HQ corrections and/or competitor signal removal.

Scoring note
------------
``competitor_signal_strength_score`` and ``language_competitor_strength_score`` are
NOT part of LEAN_COEFFICIENTS and therefore do NOT directly affect
``final_commercial_fit_score``.  They appear only in the display-only
COMMERCIAL_COMPLEXITY_FIELDS grouping.  The competitor-removal mode sets them to 0 in
the scoring row copy as an audit measure and to eliminate any indirect display effects,
but the ``final_commercial_fit_score`` delta for pure competitor removal will typically
be 0 unless some future model update adds these fields to the coefficients.

Usage (CLI)
-----------
python recalculate_hq_changed_scores.py \\
    --enriched-workbook   enriched.xlsx \\
    --hq-recovery-workbook  hq_recovery.xlsx \\
    --output              recalculated.xlsx \\
    [--sheet "Opportunity Input Full"] \\
    [--recalculation-scope hq|competitor|both] \\
    [--max-recalculated-rows 10]

Recalculation scopes
--------------------
hq          – rows where HQ Recovery changed sig_foreign_hq_score  (default)
competitor  – rows with non-zero competitor signal
both        – union of hq and competitor rows
"""

from __future__ import annotations

import argparse
import io
import sys
from typing import Any

from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from commercial_fit_scoring import SCORE_OUTPUT_COLS, score_company

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCORING_PROFILE = "italy_register_icp_only"
DEFAULT_SHEET   = "Opportunity Input Full"
SUMMARY_SHEET   = "HQ Score Recalc Summary"

SCOPE_HQ         = "hq"
SCOPE_COMPETITOR = "competitor"
SCOPE_BOTH       = "both"
VALID_SCOPES     = (SCOPE_HQ, SCOPE_COMPETITOR, SCOPE_BOTH)

# Competitor numeric fields zeroed in the scoring row copy when
# competitor removal is active.
# NOTE: these are NOT in LEAN_COEFFICIENTS, so zeroing them does not
# change final_commercial_fit_score unless the model is updated.
_COMPETITOR_NUMERIC_FIELDS = (
    "competitor_signal_strength_score",
    "language_competitor_strength_score",
)

# Text/evidence fields used to *detect* competitor signal (not zeroed).
_COMPETITOR_TEXT_FIELDS = (
    "competitor_customer_match",
    "competitor_customer_evidence",
    "competitor_signal",
    "competitor_mentions",
)

HQ_AUDIT_COLS = [
    "hq_recalc_applied",
    "hq_recalc_reason",
    "hq_score_before_recalc",
    "hq_score_after_recalc",
    "commercial_fit_score_before_hq_recalc",
    "commercial_fit_score_after_hq_recalc",
    "commercial_fit_score_delta_hq_recalc",
    "final_commercial_fit_score_before_hq_recalc",
    "final_commercial_fit_score_after_hq_recalc",
    "final_commercial_fit_score_delta_hq_recalc",
    "commercial_fit_score_before_source_column",
]

COMPETITOR_AUDIT_COLS = [
    "competitor_recalc_applied",
    "competitor_recalc_reason",
    "competitor_signal_before_recalc",
    "competitor_signal_after_recalc",
    "language_competitor_signal_before_recalc",
    "language_competitor_signal_after_recalc",
    "competitor_signal_neutralized_for_scoring",
    "competitor_signal_used_for_scoring",
    "competitor_signal_suppressed",
]

GENERAL_AUDIT_COLS = [
    "recalc_scope_applied",
    "cfs_before_recalc",
    "cfs_after_recalc",
    "cfs_delta_recalc",
    "cfs_source_col_used",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _get_existing_commercial_fit_score(row: dict) -> tuple[float, str]:
    """Return (score, column_name) from the first populated CFS column found."""
    for col in (
        "final_commercial_fit_score",
        "commercial_fit_score",
        "commercial_fit_score_final",
        "Commercial Fit Score",
        "cfs",
        "score",
    ):
        if col in row:
            v = _safe_float(row.get(col))
            if v is not None:
                return v, col
    return 0.0, ""


def _norm_key(s: Any) -> str:
    return str(s or "").strip().lower()


def _has_competitor_signal(row: dict) -> bool:
    """True if the row carries any non-zero / non-empty competitor signal."""
    for col in _COMPETITOR_NUMERIC_FIELDS:
        v = _safe_float(row.get(col))
        if v is not None and v > 0:
            return True
    for col in _COMPETITOR_TEXT_FIELDS:
        v = row.get(col)
        if v is not None and str(v).strip():
            return True
    return False


# ---------------------------------------------------------------------------
# Excel I/O helpers
# ---------------------------------------------------------------------------

def _wb_to_rows(wb, sheet_name: str) -> tuple[list[str], list[dict]]:
    target = sheet_name if sheet_name in wb.sheetnames else wb.sheetnames[0]
    ws = wb[target]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(c or "").strip() for c in rows[0]]
    data = [
        {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
        for row in rows[1:]
    ]
    return headers, data


def _build_match_index(
    rows: list[dict],
    has_domain: bool,
    has_company: bool,
    has_country: bool,
) -> tuple[dict, str]:
    if has_domain and has_company and has_country:
        strategy = "domain+company_name+input_country"
        idx: dict[str, int] = {}
        for i, r in enumerate(rows):
            k = (
                _norm_key(r.get("domain"))
                + "|" + _norm_key(r.get("company_name") or r.get("name"))
                + "|" + _norm_key(r.get("input_country_used") or r.get("country"))
            )
            idx.setdefault(k, i)
        if len(idx) >= len(rows) * 0.9:
            return idx, strategy

    if has_domain and has_company:
        strategy = "domain+company_name"
        idx = {}
        for i, r in enumerate(rows):
            k = (
                _norm_key(r.get("domain"))
                + "|" + _norm_key(r.get("company_name") or r.get("name"))
            )
            idx.setdefault(k, i)
        return idx, strategy

    return {}, "row_order_fallback"


def _match_key_for_row(r: dict, strategy: str) -> str:
    if "input_country" in strategy:
        return (
            _norm_key(r.get("domain"))
            + "|" + _norm_key(r.get("company_name") or r.get("name"))
            + "|" + _norm_key(r.get("input_country_used") or r.get("country"))
        )
    if "company_name" in strategy:
        return (
            _norm_key(r.get("domain"))
            + "|" + _norm_key(r.get("company_name") or r.get("name"))
        )
    return ""


def _build_output_wb(
    out_headers: list[str],
    out_rows: list[dict],
    sheet_name: str,
    summary: dict,
    deltas: list[tuple],
    fast_output: bool = True,
) -> Workbook:
    _hdr_fill = PatternFill("solid", fgColor="D9EAF7")
    _hdr_font = Font(bold=True)

    wb_out = Workbook()
    ws_data = wb_out.active
    ws_data.title = sheet_name
    ws_data.append(out_headers)
    for r in out_rows:
        ws_data.append([r.get(h) for h in out_headers])
    ws_data.freeze_panes = "A2"
    ws_data.auto_filter.ref = ws_data.dimensions
    ws_data.row_dimensions[1].height = 22
    for cell in ws_data[1]:
        cell.font = _hdr_font
        cell.fill = _hdr_fill

    if not fast_output and ws_data.max_column <= 250:
        _MAX_WIDTH, _SAMPLE = 50, 25
        for col_idx in range(1, ws_data.max_column + 1):
            header = ws_data.cell(row=1, column=col_idx).value
            max_len = len(str(header or ""))
            for row_idx in range(2, min(ws_data.max_row, _SAMPLE + 1) + 1):
                v = ws_data.cell(row=row_idx, column=col_idx).value
                if v is not None:
                    max_len = max(max_len, min(len(str(v)), _MAX_WIDTH))
            ws_data.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, _MAX_WIDTH)

    # ── Summary sheet ─────────────────────────────────────────────────────────
    ws_sum = wb_out.create_sheet(SUMMARY_SHEET)
    bold = Font(bold=True)

    def _add(label: str, value: Any) -> None:
        ws_sum.append([label, value])

    def _section(title: str) -> None:
        ws_sum.append([])
        ws_sum.append([title])
        ws_sum.cell(ws_sum.max_row, 1).font = bold

    ws_sum.append(["HQ Score Recalculation Summary"])
    ws_sum["A1"].font = Font(bold=True, size=12)
    _section("Run parameters")
    _add("Sheet",                   sheet_name)
    _add("Recalculation scope",     summary.get("scope", "hq"))
    _add("Scoring profile",         SCORING_PROFILE)
    _add("Matching strategy",       summary.get("strategy", ""))
    _add("Test mode active",        "Yes" if summary.get("test_mode_active") else "No")
    _add("Max recalculated rows",   summary.get("max_recalculated_rows", 0) or "unlimited")

    _section("Row counts")
    _add("Total enriched rows",             summary.get("n_enr", 0))
    _add("Total HQ Recovery rows",          summary.get("n_hqr", 0))
    _add("Matched rows",                    summary.get("n_matched", 0))
    _add("Recalculated rows",               summary.get("n_recalculated", 0))
    _add("Skipped by row limit",            summary.get("skipped_by_recalc_limit", 0))
    _add("Unchanged rows",                  summary.get("n_enr", 0) - summary.get("n_recalculated", 0))

    if summary.get("scope") in (SCOPE_HQ, SCOPE_BOTH):
        _section("HQ changes")
        _add("Eligible HQ-changed rows",    summary.get("n_hq_eligible", 0))
        _add("HQ upgrades  0/blank → 3",    summary.get("n_upgrades", 0))
        _add("HQ downgrades 3 → 0",         summary.get("n_downgrades", 0))
        _add("Other HQ numeric changes",    summary.get("n_other", 0))

    if summary.get("scope") in (SCOPE_COMPETITOR, SCOPE_BOTH):
        _section("Competitor signal")
        _add("Competitor-signal rows detected",      summary.get("n_competitor_detected", 0))
        _add("Competitor rows recalculated",         summary.get("n_competitor_recalculated", 0))
        _add("Competitor rows skipped by limit",     summary.get("n_competitor_skipped_limit", 0))
        _add("Avg competitor signal before (non-zero rows)",
             round(summary.get("avg_competitor_before", 0.0), 4))
        _add("Avg competitor signal after  (should be 0)",
             round(summary.get("avg_competitor_after", 0.0), 4))
        _add("Note: competitor fields are NOT in LEAN_COEFFICIENTS",
             "final_commercial_fit_score delta will be 0 unless model changes")

    if deltas:
        _section("Score delta statistics")
        all_d = [x[4] for x in deltas]
        pos_d = [d for d in all_d if d > 0]
        neg_d = [d for d in all_d if d < 0]
        _add("Score increases",  len(pos_d))
        _add("Score decreases",  len(neg_d))
        _add("Max positive delta", max(all_d))
        _add("Max negative delta", min(all_d))
        _add("Mean delta",         round(sum(all_d) / len(all_d), 4))

        top_pos = sorted([d for d in deltas if d[4] > 0], key=lambda x: -x[4])[:20]
        top_neg = sorted([d for d in deltas if d[4] < 0], key=lambda x:  x[4])[:20]
        for title, subset in [
            ("Top 20 positive score deltas (biggest increase)", top_pos),
            ("Top 20 negative score deltas (biggest decrease)", top_neg),
        ]:
            if subset:
                ws_sum.append([])
                ws_sum.append([title])
                ws_sum.cell(ws_sum.max_row, 1).font = bold
                ws_sum.append(["company", "domain", "score_before", "score_after", "delta"])
                for company, domain, before, after, delta in subset:
                    ws_sum.append([company, domain,
                                   round(before, 4), round(after, 4), round(delta, 4)])

    ws_sum.column_dimensions["A"].width = 50
    ws_sum.column_dimensions["B"].width = 50
    return wb_out


# ---------------------------------------------------------------------------
# Core recalculation logic
# ---------------------------------------------------------------------------

def recalculate_hq_changed_scores_workbook(
    enriched_workbook_file,
    hq_recovery_workbook_file,
    sheet_name: str = DEFAULT_SHEET,
    fast_output: bool = True,
    max_eligible_rows: int = 0,   # kept for backwards compat; alias of max_recalculated_rows
    max_recalculated_rows: int = 0,
    scope: str = SCOPE_HQ,
) -> tuple[bytes, dict]:
    """Process two workbook file-like objects (or paths) and return (excel_bytes, summary).

    scope: "hq" | "competitor" | "both"
    max_recalculated_rows: 0 = unlimited
    """
    if scope not in VALID_SCOPES:
        scope = SCOPE_HQ
    # max_eligible_rows is the old name — honour it if the new param wasn't set
    _limit = max_recalculated_rows or max_eligible_rows

    # ── Load workbooks ────────────────────────────────────────────────────────
    wb_enr = load_workbook(enriched_workbook_file, read_only=True, data_only=True)
    enr_headers, enr_rows = _wb_to_rows(wb_enr, sheet_name)
    wb_enr.close()

    wb_hqr = load_workbook(hq_recovery_workbook_file, read_only=True, data_only=True)
    hqr_headers, hqr_rows = _wb_to_rows(wb_hqr, sheet_name)
    wb_hqr.close()

    # ── Build HQ Recovery match index ────────────────────────────────────────
    hqr_has_domain  = "domain"        in hqr_headers
    hqr_has_company = ("company_name" in hqr_headers or "name" in hqr_headers)
    hqr_has_country = ("input_country_used" in hqr_headers or "country" in hqr_headers)
    hqr_index, strategy = _build_match_index(
        hqr_rows, hqr_has_domain, hqr_has_company, hqr_has_country
    )
    use_row_order = (strategy == "row_order_fallback")

    if use_row_order and len(enr_rows) != len(hqr_rows):
        return b"", {
            "error": (
                f"Cannot use row-order fallback: enriched has {len(enr_rows)} rows, "
                f"HQ Recovery has {len(hqr_rows)} rows."
            ),
        }

    # ── Build output header list ──────────────────────────────────────────────
    out_headers = list(enr_headers)
    for cols in (GENERAL_AUDIT_COLS, HQ_AUDIT_COLS, COMPETITOR_AUDIT_COLS, SCORE_OUTPUT_COLS):
        for c in cols:
            if c not in out_headers:
                out_headers.append(c)

    # ── Row loop ──────────────────────────────────────────────────────────────
    out_rows: list[dict] = []
    n_matched = 0
    n_hq_eligible = n_upgrades = n_downgrades = n_other_hq = 0
    n_competitor_detected = 0
    n_recalculated = n_skipped_limit = 0
    n_competitor_recalculated = n_competitor_skipped_limit = 0
    competitor_before_vals: list[float] = []
    competitor_after_vals:  list[float] = []
    deltas: list[tuple[str, str, float, float, float]] = []

    for i, enr_row in enumerate(enr_rows):
        row_out = dict(enr_row)

        # ── Match to HQ Recovery ──────────────────────────────────────────────
        if use_row_order:
            hqr_row, matched = hqr_rows[i], True
        else:
            hqr_idx = hqr_index.get(_match_key_for_row(enr_row, strategy))
            hqr_row, matched = (hqr_rows[hqr_idx], True) if hqr_idx is not None else ({}, False)

        if matched:
            n_matched += 1

        # ── Determine HQ eligibility ──────────────────────────────────────────
        hq_eligible      = False
        hq_reviewed_val  = None
        hq_original_val  = None
        hq_reason        = ""
        if matched and scope in (SCOPE_HQ, SCOPE_BOTH):
            reviewed_raw = hqr_row.get("sig_foreign_hq_score_reviewed")
            original_raw = (
                enr_row.get("sig_foreign_hq_score")
                or hqr_row.get("sig_foreign_hq_score_original")
                or hqr_row.get("sig_foreign_hq_score_original_before_recovery")
            )
            hq_reviewed_val = _safe_float(reviewed_raw)
            hq_original_val = _safe_float(original_raw)
            if reviewed_raw is not None and reviewed_raw != "" and hq_reviewed_val != hq_original_val:
                hq_eligible = True
                hq_reason   = f"HQ Recovery changed score {hq_original_val!r} → {hq_reviewed_val!r}"
                n_hq_eligible += 1

        # ── Determine competitor eligibility ──────────────────────────────────
        competitor_eligible = False
        if scope in (SCOPE_COMPETITOR, SCOPE_BOTH):
            if _has_competitor_signal(enr_row):
                competitor_eligible = True
                n_competitor_detected += 1

        row_is_eligible = hq_eligible or competitor_eligible

        if not row_is_eligible:
            row_out["recalc_scope_applied"] = "No"
            row_out["hq_recalc_applied"]    = "No"
            row_out["competitor_recalc_applied"] = "No"
            out_rows.append(row_out)
            continue

        # ── Test-mode limit check ─────────────────────────────────────────────
        if _limit > 0 and n_recalculated >= _limit:
            n_skipped_limit += 1
            if hq_eligible:
                row_out["hq_recalc_applied"] = "No - skipped by test row limit"
                n_competitor_skipped_limit += 1 if competitor_eligible else 0
            if competitor_eligible:
                row_out["competitor_recalc_applied"] = "No - skipped by test row limit"
                n_competitor_skipped_limit += 1
            row_out["recalc_scope_applied"] = "Skipped (test row limit)"
            out_rows.append(row_out)
            continue

        # ── Build scoring row copy ────────────────────────────────────────────
        old_cfs, _cfs_col = _get_existing_commercial_fit_score(row_out)
        row_copy = dict(row_out)

        if hq_eligible:
            new_hq = hq_reviewed_val if hq_reviewed_val is not None else 0.0
            row_copy["sig_foreign_hq_score"]                  = new_hq
            row_copy["sig_foreign_hq_score_for_next_scoring"] = new_hq

        comp_before = {f: _safe_float(row_out.get(f)) or 0.0 for f in _COMPETITOR_NUMERIC_FIELDS}
        if competitor_eligible:
            for f in _COMPETITOR_NUMERIC_FIELDS:
                row_copy[f] = 0.0

        # ── Single score_company call ─────────────────────────────────────────
        try:
            score_out = score_company(row_copy, {"scoring_profile": SCORING_PROFILE})
            for col in SCORE_OUTPUT_COLS:
                if col in score_out:
                    row_out[col] = score_out[col]

            new_cfs = _safe_float(score_out.get("final_commercial_fit_score")) or 0.0
            delta   = round(new_cfs - old_cfs, 4)

            row_out["recalc_scope_applied"] = scope
            row_out["cfs_before_recalc"]    = old_cfs
            row_out["cfs_after_recalc"]     = new_cfs
            row_out["cfs_delta_recalc"]     = delta
            row_out["cfs_source_col_used"]  = _cfs_col

            # HQ audit
            if hq_eligible:
                new_hq = hq_reviewed_val if hq_reviewed_val is not None else 0.0
                old_hq = hq_original_val or 0.0
                row_out.update({
                    "hq_recalc_applied":                           "Yes",
                    "hq_recalc_reason":                            hq_reason,
                    "hq_score_before_recalc":                      old_hq,
                    "hq_score_after_recalc":                       new_hq,
                    "commercial_fit_score_before_hq_recalc":       old_cfs,
                    "commercial_fit_score_after_hq_recalc":        new_cfs,
                    "commercial_fit_score_delta_hq_recalc":        delta,
                    "final_commercial_fit_score_before_hq_recalc": old_cfs,
                    "final_commercial_fit_score_after_hq_recalc":  new_cfs,
                    "final_commercial_fit_score_delta_hq_recalc":  delta,
                    "commercial_fit_score_before_source_column":   _cfs_col,
                    "sig_foreign_hq_score":                        new_hq,
                    "sig_foreign_hq_score_for_next_scoring":       new_hq,
                })
                if old_hq in (0.0, None) and new_hq == 3.0:
                    n_upgrades += 1
                elif old_hq == 3.0 and new_hq in (0.0, None):
                    n_downgrades += 1
                else:
                    n_other_hq += 1
            else:
                row_out["hq_recalc_applied"] = "No"

            # Competitor audit
            if competitor_eligible:
                row_out.update({
                    "competitor_recalc_applied":               "Yes",
                    "competitor_recalc_reason":                "Competitor signal neutralized for scoring",
                    "competitor_signal_before_recalc":         comp_before.get("competitor_signal_strength_score", 0.0),
                    "competitor_signal_after_recalc":          0.0,
                    "language_competitor_signal_before_recalc": comp_before.get("language_competitor_strength_score", 0.0),
                    "language_competitor_signal_after_recalc": 0.0,
                    "competitor_signal_neutralized_for_scoring": "Yes",
                    "competitor_signal_used_for_scoring":      "No",
                    "competitor_signal_suppressed":            "Yes",
                })
                for f, bval in comp_before.items():
                    if bval > 0:
                        competitor_before_vals.append(bval)
                competitor_after_vals.append(0.0)
                n_competitor_recalculated += 1
            else:
                row_out["competitor_recalc_applied"] = "No"

            n_recalculated += 1
            deltas.append((
                str(enr_row.get("company_name") or enr_row.get("name") or "?"),
                str(enr_row.get("domain") or "?"),
                old_cfs, new_cfs, delta,
            ))

        except Exception as exc:
            row_out["hq_recalc_applied"]         = f"Error: {exc}"
            row_out["competitor_recalc_applied"]  = f"Error: {exc}"
            row_out["recalc_scope_applied"]       = f"Error: {exc}"

        out_rows.append(row_out)

    avg_comp_before = (sum(competitor_before_vals) / len(competitor_before_vals)
                       if competitor_before_vals else 0.0)

    summary = {
        "error":                     "",
        "scope":                     scope,
        "strategy":                  strategy,
        "n_enr":                     len(enr_rows),
        "n_hqr":                     len(hqr_rows),
        "n_matched":                 n_matched,
        "n_hq_eligible":             n_hq_eligible,
        "n_competitor_detected":     n_competitor_detected,
        "n_recalculated":            n_recalculated,
        "skipped_by_recalc_limit":   n_skipped_limit,
        "n_upgrades":                n_upgrades,
        "n_downgrades":              n_downgrades,
        "n_other":                   n_other_hq,
        "n_competitor_recalculated": n_competitor_recalculated,
        "n_competitor_skipped_limit": n_competitor_skipped_limit,
        "avg_competitor_before":     round(avg_comp_before, 4),
        "avg_competitor_after":      0.0,
        "deltas":                    deltas,
        "test_mode_active":          _limit > 0,
        "max_recalculated_rows":     _limit,
    }

    wb_out = _build_output_wb(
        out_headers, out_rows, sheet_name, summary, deltas, fast_output=fast_output
    )
    buf = io.BytesIO()
    wb_out.save(buf)
    return buf.getvalue(), summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enriched-workbook",    required=True)
    ap.add_argument("--hq-recovery-workbook", required=True)
    ap.add_argument("--output",               required=True)
    ap.add_argument("--sheet",                default=DEFAULT_SHEET)
    ap.add_argument("--recalculation-scope",  default=SCOPE_HQ,
                    choices=list(VALID_SCOPES),
                    help="hq | competitor | both  (default: hq)")
    ap.add_argument("--max-recalculated-rows", type=int, default=0,
                    help="Limit recalculated rows (0 = unlimited)")
    args = ap.parse_args()

    print(f"\n{'='*72}")
    print("Score Recalculation")
    print(f"  enriched   : {args.enriched_workbook}")
    print(f"  hq-recovery: {args.hq_recovery_workbook}")
    print(f"  output     : {args.output}")
    print(f"  sheet      : {args.sheet}")
    print(f"  scope      : {args.recalculation_scope}")
    print(f"  profile    : {SCORING_PROFILE}")
    print(f"  row limit  : {args.max_recalculated_rows or 'unlimited'}")
    print(f"{'='*72}\n")

    excel_bytes, summary = recalculate_hq_changed_scores_workbook(
        args.enriched_workbook,
        args.hq_recovery_workbook,
        sheet_name=args.sheet,
        scope=args.recalculation_scope,
        max_recalculated_rows=args.max_recalculated_rows,
    )

    if summary.get("error"):
        print(f"ERROR: {summary['error']}")
        sys.exit(1)

    with open(args.output, "wb") as fh:
        fh.write(excel_bytes)

    deltas = summary["deltas"]
    print(f"\n{'='*72}")
    print("RESULTS")
    print(f"  Scope                : {summary['scope']}")
    print(f"  Matching strategy    : {summary['strategy']}")
    print(f"  Rows matched         : {summary['n_matched']} / {summary['n_enr']}")
    if summary["scope"] in (SCOPE_HQ, SCOPE_BOTH):
        print(f"  HQ-eligible rows     : {summary['n_hq_eligible']}")
        print(f"  HQ upgrades  0→3     : {summary['n_upgrades']}")
        print(f"  HQ downgrades 3→0    : {summary['n_downgrades']}")
    if summary["scope"] in (SCOPE_COMPETITOR, SCOPE_BOTH):
        print(f"  Competitor detected  : {summary['n_competitor_detected']}")
        print(f"  Competitor recalc'd  : {summary['n_competitor_recalculated']}")
    print(f"  Recalculated total   : {summary['n_recalculated']}")
    print(f"  Skipped (row limit)  : {summary['skipped_by_recalc_limit']}")
    if deltas:
        all_d = [x[4] for x in deltas]
        print(f"  Biggest increase     : +{max(all_d):.4f}")
        print(f"  Biggest decrease     : {min(all_d):.4f}")
    print(f"  Output file          : {args.output}")
    print(f"{'='*72}\n")


def _running_under_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


if __name__ == "__main__":
    if _running_under_streamlit():
        import streamlit as st
        st.error(
            "This is the command-line backend script. "
            "Please run `streamlit run hq_score_recalc_app.py` for the browser UI."
        )
        st.stop()
    main()
