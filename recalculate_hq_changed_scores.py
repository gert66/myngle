"""
Recalculate commercial fit scores for rows where HQ Recovery changed sig_foreign_hq_score.

Usage:
    python recalculate_hq_changed_scores.py \\
        --enriched-workbook  enriched.xlsx \\
        --hq-recovery-workbook  hq_recovery_output.xlsx \\
        --output  recalculated.xlsx \\
        [--sheet "Opportunity Input Full"]

Scoring profile used: italy_register_icp_only
"""

import argparse
import io
import sys
from typing import Any

from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from commercial_fit_scoring import SCORE_OUTPUT_COLS, score_company

SCORING_PROFILE = "italy_register_icp_only"
DEFAULT_SHEET   = "Opportunity Input Full"
SUMMARY_SHEET   = "HQ Score Recalc Summary"

AUDIT_COLS = [
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
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _norm_key(s: Any) -> str:
    return str(s or "").strip().lower()


def _wb_to_rows(wb, sheet_name: str) -> tuple[list[str], list[dict]]:
    target = sheet_name if sheet_name in wb.sheetnames else wb.sheetnames[0]
    ws = wb[target]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(c or "").strip() for c in rows[0]]
    data = []
    for row in rows[1:]:
        data.append({headers[i]: (row[i] if i < len(row) else None)
                     for i in range(len(headers))})
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
    _MAX_WIDTH = 50
    _SAMPLE_ROWS = 25
    if ws_data.max_column <= 250:
        for col_idx in range(1, ws_data.max_column + 1):
            header = ws_data.cell(row=1, column=col_idx).value
            max_len = len(str(header or ""))
            for row_idx in range(2, min(ws_data.max_row, _SAMPLE_ROWS + 1) + 1):
                v = ws_data.cell(row=row_idx, column=col_idx).value
                if v is not None:
                    max_len = max(max_len, min(len(str(v)), _MAX_WIDTH))
            ws_data.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, _MAX_WIDTH)

    ws_sum = wb_out.create_sheet(SUMMARY_SHEET)
    bold = Font(bold=True)

    def _add(label: str, value: Any) -> None:
        ws_sum.append([label, value])

    ws_sum.append(["HQ Score Recalculation Summary"])
    ws_sum["A1"].font = Font(bold=True, size=12)
    ws_sum.append([])
    _add("Sheet",               sheet_name)
    _add("Scoring profile",     SCORING_PROFILE)
    _add("Matching strategy",   summary["strategy"])
    ws_sum.append([])
    _add("Total enriched rows",       summary["n_enr"])
    _add("Total HQ Recovery rows",    summary["n_hqr"])
    _add("Matched rows",              summary["n_matched"])
    _add("Eligible (score changed)",  summary["n_eligible"])
    _add("Recalculated rows",         summary["n_recalculated"])
    _add("Upgrades  0/blank → 3",     summary["n_upgrades"])
    _add("Downgrades 3 → 0",          summary["n_downgrades"])
    _add("Other numeric changes",     summary["n_other"])
    _add("Unchanged rows",            summary["n_enr"] - summary["n_recalculated"])

    if deltas:
        ws_sum.append([])
        ws_sum.append(["Score delta statistics"])
        ws_sum.cell(ws_sum.max_row, 1).font = bold
        all_d = [x[4] for x in deltas]
        _add("Max positive delta",  max(all_d))
        _add("Max negative delta",  min(all_d))
        _add("Mean delta",          round(sum(all_d) / len(all_d), 4))

    top_positive = sorted(deltas, key=lambda x: -x[4])[:20]
    top_negative = sorted(deltas, key=lambda x:  x[4])[:20]

    for title, subset in [
        ("Top 20 positive score deltas (biggest increase)", top_positive),
        ("Top 20 negative score deltas (biggest decrease)", top_negative),
    ]:
        if subset:
            ws_sum.append([])
            ws_sum.append([title])
            ws_sum.cell(ws_sum.max_row, 1).font = bold
            ws_sum.append(["company", "domain", "cfs_before", "cfs_after", "delta"])
            for company, domain, before, after, delta in subset:
                ws_sum.append([company, domain,
                                round(before, 4), round(after, 4), round(delta, 4)])

    ws_sum.column_dimensions["A"].width = 40
    ws_sum.column_dimensions["B"].width = 50
    return wb_out


# ── core logic (reusable) ─────────────────────────────────────────────────────

def recalculate_hq_changed_scores_workbook(
    enriched_workbook_file,
    hq_recovery_workbook_file,
    sheet_name: str = DEFAULT_SHEET,
) -> tuple[bytes, dict]:
    """Process two workbook file-like objects (or paths) and return
    (excel_bytes, summary_dict).

    summary_dict keys: strategy, n_enr, n_hqr, n_matched, n_eligible,
    n_recalculated, n_upgrades, n_downgrades, n_other, deltas, error.
    """
    wb_enr = load_workbook(enriched_workbook_file, read_only=True, data_only=True)
    enr_headers, enr_rows = _wb_to_rows(wb_enr, sheet_name)
    wb_enr.close()

    wb_hqr = load_workbook(hq_recovery_workbook_file, read_only=True, data_only=True)
    hqr_headers, hqr_rows = _wb_to_rows(wb_hqr, sheet_name)
    wb_hqr.close()

    hqr_has_domain  = "domain" in hqr_headers
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

    out_headers = list(enr_headers)
    for ac in AUDIT_COLS:
        if ac not in out_headers:
            out_headers.append(ac)
    for sc in SCORE_OUTPUT_COLS:
        if sc not in out_headers:
            out_headers.append(sc)

    out_rows: list[dict] = []
    n_matched = n_eligible = n_recalculated = n_upgrades = n_downgrades = n_other = 0
    deltas: list[tuple[str, str, float, float, float]] = []

    for i, enr_row in enumerate(enr_rows):
        row_out = dict(enr_row)

        if use_row_order:
            hqr_row, matched = hqr_rows[i], True
        else:
            hqr_idx = hqr_index.get(_match_key_for_row(enr_row, strategy))
            if hqr_idx is None:
                hqr_row, matched = {}, False
            else:
                hqr_row, matched = hqr_rows[hqr_idx], True

        if matched:
            n_matched += 1

        eligible, recalc_reason = False, ""
        if matched:
            reviewed_raw = hqr_row.get("sig_foreign_hq_score_reviewed")
            original_raw = (
                enr_row.get("sig_foreign_hq_score")
                or hqr_row.get("sig_foreign_hq_score_original")
                or hqr_row.get("sig_foreign_hq_score_original_before_recovery")
            )
            reviewed_val = _safe_float(reviewed_raw)
            original_val = _safe_float(original_raw)
            if reviewed_raw is not None and reviewed_raw != "" and reviewed_val != original_val:
                eligible = True
                recalc_reason = f"HQ Recovery changed score {original_val!r} → {reviewed_val!r}"

        if eligible:
            n_eligible += 1
            old_cfs   = _safe_float(row_out.get("final_commercial_fit_score")) or 0.0
            old_score = original_val or 0.0
            new_score = reviewed_val if reviewed_val is not None else 0.0

            row_copy = dict(row_out)
            row_copy["sig_foreign_hq_score"]                  = new_score
            row_copy["sig_foreign_hq_score_for_next_scoring"] = new_score

            try:
                score_out = score_company(row_copy, {"scoring_profile": SCORING_PROFILE})
                for col in SCORE_OUTPUT_COLS:
                    if col in score_out:
                        row_out[col] = score_out[col]

                new_cfs = _safe_float(score_out.get("final_commercial_fit_score")) or 0.0
                delta   = round(new_cfs - old_cfs, 4)

                row_out.update({
                    "hq_recalc_applied":                          "Yes",
                    "hq_recalc_reason":                           recalc_reason,
                    "hq_score_before_recalc":                     old_score,
                    "hq_score_after_recalc":                      new_score,
                    "commercial_fit_score_before_hq_recalc":      old_cfs,
                    "commercial_fit_score_after_hq_recalc":       new_cfs,
                    "commercial_fit_score_delta_hq_recalc":       delta,
                    "final_commercial_fit_score_before_hq_recalc": old_cfs,
                    "final_commercial_fit_score_after_hq_recalc":  new_cfs,
                    "final_commercial_fit_score_delta_hq_recalc":  delta,
                    "sig_foreign_hq_score":                       new_score,
                    "sig_foreign_hq_score_for_next_scoring":      new_score,
                })

                n_recalculated += 1
                deltas.append((
                    str(enr_row.get("company_name") or enr_row.get("name") or "?"),
                    str(enr_row.get("domain") or "?"),
                    old_cfs, new_cfs, delta,
                ))
                if old_score in (0.0, None) and new_score == 3.0:
                    n_upgrades += 1
                elif old_score == 3.0 and new_score in (0.0, None):
                    n_downgrades += 1
                else:
                    n_other += 1

            except Exception as exc:
                row_out["hq_recalc_applied"] = f"Error: {exc}"
                row_out["hq_recalc_reason"]  = recalc_reason
        else:
            row_out["hq_recalc_applied"] = "No"

        out_rows.append(row_out)

    summary = {
        "error":          "",
        "strategy":       strategy,
        "n_enr":          len(enr_rows),
        "n_hqr":          len(hqr_rows),
        "n_matched":      n_matched,
        "n_eligible":     n_eligible,
        "n_recalculated": n_recalculated,
        "n_upgrades":     n_upgrades,
        "n_downgrades":   n_downgrades,
        "n_other":        n_other,
        "deltas":         deltas,
    }

    wb_out = _build_output_wb(out_headers, out_rows, sheet_name, summary, deltas)
    buf = io.BytesIO()
    wb_out.save(buf)
    return buf.getvalue(), summary


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--enriched-workbook",    required=True)
    ap.add_argument("--hq-recovery-workbook", required=True)
    ap.add_argument("--output",               required=True)
    ap.add_argument("--sheet",                default=DEFAULT_SHEET)
    args = ap.parse_args()

    print(f"\n{'='*72}")
    print(f"HQ Score Recalculation")
    print(f"  enriched   : {args.enriched_workbook}")
    print(f"  hq-recovery: {args.hq_recovery_workbook}")
    print(f"  output     : {args.output}")
    print(f"  sheet      : {args.sheet}")
    print(f"  profile    : {SCORING_PROFILE}")
    print(f"{'='*72}\n")

    excel_bytes, summary = recalculate_hq_changed_scores_workbook(
        args.enriched_workbook,
        args.hq_recovery_workbook,
        sheet_name=args.sheet,
    )

    if summary.get("error"):
        print(f"ERROR: {summary['error']}")
        sys.exit(1)

    with open(args.output, "wb") as fh:
        fh.write(excel_bytes)

    deltas = summary["deltas"]
    print(f"\n{'='*72}")
    print("RESULTS")
    print(f"  Matching strategy    : {summary['strategy']}")
    print(f"  Rows matched         : {summary['n_matched']} / {summary['n_enr']}")
    print(f"  Eligible (changed)   : {summary['n_eligible']}")
    print(f"  Recalculated         : {summary['n_recalculated']}")
    print(f"  Upgrades  0→3        : {summary['n_upgrades']}")
    print(f"  Downgrades 3→0       : {summary['n_downgrades']}")
    print(f"  Other changes        : {summary['n_other']}")
    if deltas:
        all_d = [x[4] for x in deltas]
        print(f"  Biggest increase     : +{max(all_d):.4f}")
        print(f"  Biggest decrease     : {min(all_d):.4f}")
    print(f"  Output file          : {args.output}")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
