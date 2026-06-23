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
import sys
from typing import Any

from openpyxl import load_workbook
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
        f = float(v)
        return f
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
    data = [{headers[i]: rows[i + 1][i] if i < len(rows[i + 1]) else None
             for i in range(len(headers))}
            for _ in range(0)  # placeholder
            ]
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
    """Build a lookup dict from match-key → row-index.
    Returns (index, strategy_name).
    """
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
        # If too many collisions try simpler key
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


def _match_key_for_row(
    r: dict,
    strategy: str,
) -> str:
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


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--enriched-workbook",    required=True)
    ap.add_argument("--hq-recovery-workbook", required=True)
    ap.add_argument("--output",               required=True)
    ap.add_argument("--sheet",                default=DEFAULT_SHEET)
    args = ap.parse_args()

    sheet_name = args.sheet

    print(f"\n{'='*72}")
    print(f"HQ Score Recalculation")
    print(f"  enriched   : {args.enriched_workbook}")
    print(f"  hq-recovery: {args.hq_recovery_workbook}")
    print(f"  output     : {args.output}")
    print(f"  sheet      : {sheet_name}")
    print(f"  profile    : {SCORING_PROFILE}")
    print(f"{'='*72}\n")

    # ── Load workbooks ────────────────────────────────────────────────────────
    print("Loading enriched workbook…")
    wb_enr = load_workbook(args.enriched_workbook, read_only=True, data_only=True)
    enr_headers, enr_rows = _wb_to_rows(wb_enr, sheet_name)
    wb_enr.close()
    print(f"  {len(enr_rows)} rows, {len(enr_headers)} columns")

    print("Loading HQ Recovery workbook…")
    wb_hqr = load_workbook(args.hq_recovery_workbook, read_only=True, data_only=True)
    hqr_headers, hqr_rows = _wb_to_rows(wb_hqr, sheet_name)
    wb_hqr.close()
    print(f"  {len(hqr_rows)} rows, {len(hqr_headers)} columns")

    # ── Build match index ─────────────────────────────────────────────────────
    hqr_has_domain  = "domain" in hqr_headers
    hqr_has_company = ("company_name" in hqr_headers or "name" in hqr_headers)
    hqr_has_country = ("input_country_used" in hqr_headers or "country" in hqr_headers)

    hqr_index, strategy = _build_match_index(
        hqr_rows, hqr_has_domain, hqr_has_company, hqr_has_country
    )

    use_row_order = (strategy == "row_order_fallback")
    if use_row_order:
        if len(enr_rows) != len(hqr_rows):
            print(
                f"ERROR: Cannot use row-order fallback — "
                f"enriched has {len(enr_rows)} rows, HQ Recovery has {len(hqr_rows)} rows."
            )
            sys.exit(1)
        print(f"  Matching strategy : row_order_fallback (row counts match)")
    else:
        print(f"  Matching strategy : {strategy}")

    # ── Process rows ──────────────────────────────────────────────────────────
    # Determine output columns: enriched headers + audit cols (appended if missing)
    all_audit_set = set(AUDIT_COLS)
    out_headers = list(enr_headers)
    for ac in AUDIT_COLS:
        if ac not in out_headers:
            out_headers.append(ac)
    # Also ensure SCORE_OUTPUT_COLS exist
    for sc in SCORE_OUTPUT_COLS:
        if sc not in out_headers:
            out_headers.append(sc)

    out_rows: list[dict] = []

    n_matched      = 0
    n_eligible     = 0
    n_recalculated = 0
    n_upgrades     = 0   # 0/blank → 3
    n_downgrades   = 0   # 3 → 0
    n_other        = 0
    deltas: list[tuple[str, str, float, float, float]] = []  # company, domain, before, after, delta

    for i, enr_row in enumerate(enr_rows):
        row_out = dict(enr_row)

        # Find matching HQ Recovery row
        if use_row_order:
            hqr_row = hqr_rows[i]
            matched = True
        else:
            mk = _match_key_for_row(enr_row, strategy)
            hqr_idx = hqr_index.get(mk)
            if hqr_idx is None:
                matched = False
                hqr_row = {}
            else:
                hqr_row = hqr_rows[hqr_idx]
                matched = True

        if matched:
            n_matched += 1

        # Check eligibility
        eligible      = False
        recalc_reason = ""
        if matched:
            reviewed_raw  = hqr_row.get("sig_foreign_hq_score_reviewed")
            original_raw  = (
                enr_row.get("sig_foreign_hq_score")
                or hqr_row.get("sig_foreign_hq_score_original")
                or hqr_row.get("sig_foreign_hq_score_original_before_recovery")
            )
            reviewed_val  = _safe_float(reviewed_raw)
            original_val  = _safe_float(original_raw)

            if reviewed_raw is not None and reviewed_raw != "":
                if reviewed_val != original_val:
                    eligible = True
                    recalc_reason = (
                        f"HQ Recovery changed score "
                        f"{original_val!r} → {reviewed_val!r}"
                    )

        if eligible:
            n_eligible += 1

            # Read old scoring fields for audit
            old_cfs   = _safe_float(row_out.get("final_commercial_fit_score")) or 0.0
            old_score = original_val or 0.0
            new_score = reviewed_val if reviewed_val is not None else 0.0

            # Build row copy with updated HQ score
            row_copy = dict(row_out)
            row_copy["sig_foreign_hq_score"]               = new_score
            row_copy["sig_foreign_hq_score_for_next_scoring"] = new_score

            # Run scoring
            try:
                score_out = score_company(
                    row_copy,
                    {"scoring_profile": SCORING_PROFILE},
                )
                # Write scoring fields back
                for col in SCORE_OUTPUT_COLS:
                    if col in score_out:
                        row_out[col] = score_out[col]

                new_cfs = _safe_float(score_out.get("final_commercial_fit_score")) or 0.0
                delta   = round(new_cfs - old_cfs, 4)

                # Audit columns
                row_out["hq_recalc_applied"]   = "Yes"
                row_out["hq_recalc_reason"]    = recalc_reason
                row_out["hq_score_before_recalc"] = old_score
                row_out["hq_score_after_recalc"]  = new_score
                row_out["commercial_fit_score_before_hq_recalc"]       = old_cfs
                row_out["commercial_fit_score_after_hq_recalc"]        = new_cfs
                row_out["commercial_fit_score_delta_hq_recalc"]        = delta
                row_out["final_commercial_fit_score_before_hq_recalc"] = old_cfs
                row_out["final_commercial_fit_score_after_hq_recalc"]  = new_cfs
                row_out["final_commercial_fit_score_delta_hq_recalc"]  = delta
                # Also update sig_foreign_hq_score in output
                row_out["sig_foreign_hq_score"]               = new_score
                row_out["sig_foreign_hq_score_for_next_scoring"] = new_score

                n_recalculated += 1
                deltas.append((
                    str(enr_row.get("company_name") or enr_row.get("name") or "?"),
                    str(enr_row.get("domain") or "?"),
                    old_cfs, new_cfs, delta,
                ))

                if old_score in (0, None) and new_score == 3:
                    n_upgrades += 1
                elif old_score == 3 and new_score in (0, None):
                    n_downgrades += 1
                else:
                    n_other += 1

            except Exception as exc:
                row_out["hq_recalc_applied"] = f"Error: {exc}"
                row_out["hq_recalc_reason"]  = recalc_reason
        else:
            row_out["hq_recalc_applied"] = "No"

        out_rows.append(row_out)

    # ── Sort deltas ───────────────────────────────────────────────────────────
    top_positive = sorted(deltas, key=lambda x: -x[4])[:20]
    top_negative = sorted(deltas, key=lambda x: x[4])[:20]

    # ── Write output workbook ─────────────────────────────────────────────────
    print(f"\nWriting output workbook: {args.output}")
    from openpyxl import Workbook as _WB
    wb_out = _WB()

    # Sheet 1: updated data
    ws_data = wb_out.active
    ws_data.title = sheet_name

    ws_data.append(out_headers)
    for r in out_rows:
        ws_data.append([r.get(h) for h in out_headers])

    # Freeze header row
    ws_data.freeze_panes = "A2"

    # Sheet 2: summary
    ws_sum = wb_out.create_sheet(SUMMARY_SHEET)
    _h = Font(bold=True)

    def _add(label: str, value: Any) -> None:
        ws_sum.append([label, value])

    ws_sum.append(["HQ Score Recalculation Summary"])
    ws_sum["A1"].font = Font(bold=True, size=12)
    ws_sum.append([])
    _add("Enriched workbook",          args.enriched_workbook)
    _add("HQ Recovery workbook",       args.hq_recovery_workbook)
    _add("Sheet",                      sheet_name)
    _add("Scoring profile",            SCORING_PROFILE)
    _add("Matching strategy",          strategy)
    ws_sum.append([])
    _add("Total enriched rows",        len(enr_rows))
    _add("Total HQ Recovery rows",     len(hqr_rows))
    _add("Matched rows",               n_matched)
    _add("Eligible (score changed)",   n_eligible)
    _add("Recalculated rows",          n_recalculated)
    _add("Upgrades  0/blank → 3",      n_upgrades)
    _add("Downgrades 3 → 0",           n_downgrades)
    _add("Other numeric changes",      n_other)
    _add("Unchanged rows",             len(enr_rows) - n_recalculated)

    if deltas:
        ws_sum.append([])
        ws_sum.append(["Score delta statistics"])
        ws_sum[-1][0].font = _h
        all_d = [x[4] for x in deltas]
        _add("Max positive delta",     max(all_d))
        _add("Max negative delta",     min(all_d))
        _add("Mean delta",             round(sum(all_d) / len(all_d), 4))

    if top_positive:
        ws_sum.append([])
        ws_sum.append(["Top 20 positive score deltas (biggest increase)"])
        ws_sum[-1][0].font = _h
        ws_sum.append(["company", "domain", "cfs_before", "cfs_after", "delta"])
        for company, domain, before, after, delta in top_positive:
            ws_sum.append([company, domain, round(before, 4), round(after, 4), round(delta, 4)])

    if top_negative:
        ws_sum.append([])
        ws_sum.append(["Top 20 negative score deltas (biggest decrease)"])
        ws_sum[-1][0].font = _h
        ws_sum.append(["company", "domain", "cfs_before", "cfs_after", "delta"])
        for company, domain, before, after, delta in top_negative:
            ws_sum.append([company, domain, round(before, 4), round(after, 4), round(delta, 4)])

    # Column widths
    ws_sum.column_dimensions["A"].width = 40
    ws_sum.column_dimensions["B"].width = 50

    wb_out.save(args.output)
    print("Done.")

    # ── Console report ────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"RESULTS")
    print(f"  Input enriched       : {args.enriched_workbook}")
    print(f"  Input HQ Recovery    : {args.hq_recovery_workbook}")
    print(f"  Matching strategy    : {strategy}")
    print(f"  Rows matched         : {n_matched} / {len(enr_rows)}")
    print(f"  Eligible (changed)   : {n_eligible}")
    print(f"  Recalculated         : {n_recalculated}")
    print(f"  Upgrades  0→3        : {n_upgrades}")
    print(f"  Downgrades 3→0       : {n_downgrades}")
    print(f"  Other changes        : {n_other}")
    if deltas:
        all_d = [x[4] for x in deltas]
        print(f"  Biggest increase     : +{max(all_d):.4f}")
        print(f"  Biggest decrease     : {min(all_d):.4f}")
    print(f"  Output file          : {args.output}")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
