"""Focused tests for refresh_lead_caller_app_fields_in_workbook.

All tests use small synthetic Excel workbooks written to tmp_path with
pandas/openpyxl. No API calls, no network, no real sample data.
"""

from __future__ import annotations

import ast

import pandas as pd

from refresh_lead_caller_app_fields_in_workbook import (
    BAD_PHRASES,
    TARGET_FIELDS,
    main,
)

NON_TARGET_COLUMNS = [
    "source_index",
    "company_name",
    "domain",
    "input_country",
    "hq_detected_country",
    "hq_detected_city",
    "sig_foreign_hq_score_for_next_scoring",
    "sig_international_profile_score",
    "sig_onboarding_training_need_score",
    "evidence_count",
    "signal_count",
    "final_commercial_fit_score",
    "commercial_tier",
]


def enriched_row(**overrides) -> dict:
    row = {
        "source_index": 1,
        "company_name": "Acme Brasil",
        "domain": "acme.com.br",
        "input_country": "Brazil",
        "hq_detected_country": "Germany",
        "hq_detected_city": "Munich",
        "hq_confidence": "High",
        "needs_manual_review": False,
        "sig_foreign_hq_score_for_next_scoring": 3,
        "sig_international_profile_score": 2,
        "sig_onboarding_training_need_score": 1,
        "sig_company_size_complexity_score": 0,
        "sig_icp_keyword_match_score": 0,
        "evidence_count": 2,
        "signal_count": 2,
        "final_commercial_fit_score": 80,
        "commercial_tier": "A",
        "cold_caller_summary_app": "STALE",
        "parent_hq_summary_app": "STALE",
        "why_relevant_app": "Relevant because the lead shows International profile evidence found",
        "what_is_hot_app": "Onboarding/training need evidence found; ICP keyword evidence found",
        "what_is_not_app": "Company complexity evidence found",
        "caller_angle_app": "STALE",
        "call_starter_app": "STALE",
        "caution_app": "foreign HQ signal and international profile evidence",
    }
    row.update(overrides)
    return row


def write_workbook(path, enriched, evidence=None, signals=None, run_summary=None):
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(enriched).to_excel(writer, sheet_name="Enriched Leads", index=False)
        if evidence is not None:
            pd.DataFrame(evidence).to_excel(writer, sheet_name="Evidence", index=False)
        if signals is not None:
            pd.DataFrame(signals).to_excel(writer, sheet_name="Signals", index=False)
        if run_summary is not None:
            pd.DataFrame(run_summary).to_excel(writer, sheet_name="Run Summary", index=False)


def test_only_target_columns_change(tmp_path):
    input_xlsx = tmp_path / "input.xlsx"
    output_xlsx = tmp_path / "output.xlsx"
    write_workbook(
        input_xlsx,
        [enriched_row(source_index=1), enriched_row(source_index=2, company_name="Beta Ltd")],
        evidence=[{"source_index": 1, "evidence_id": "e1"}],
        signals=[{"source_index": 1, "signal_name": "s1"}],
        run_summary=[{"note": "ok"}],
    )

    rc = main(["--input-xlsx", str(input_xlsx), "--output-xlsx", str(output_xlsx)])
    assert rc == 0

    before = pd.read_excel(input_xlsx, sheet_name="Enriched Leads")
    after = pd.read_excel(output_xlsx, sheet_name="Enriched Leads")

    for col in NON_TARGET_COLUMNS:
        assert before[col].tolist() == after[col].tolist(), f"column {col} changed unexpectedly"

    changed_any = False
    for field in TARGET_FIELDS:
        if before[field].tolist() != after[field].tolist():
            changed_any = True
    assert changed_any, "expected at least one target field to change"

    # Other sheets preserved untouched.
    before_evidence = pd.read_excel(input_xlsx, sheet_name="Evidence")
    after_evidence = pd.read_excel(output_xlsx, sheet_name="Evidence")
    pd.testing.assert_frame_equal(before_evidence, after_evidence)

    before_signals = pd.read_excel(input_xlsx, sheet_name="Signals")
    after_signals = pd.read_excel(output_xlsx, sheet_name="Signals")
    pd.testing.assert_frame_equal(before_signals, after_signals)


def test_input_file_not_modified(tmp_path):
    input_xlsx = tmp_path / "input.xlsx"
    output_xlsx = tmp_path / "output.xlsx"
    write_workbook(input_xlsx, [enriched_row()])

    before_bytes = input_xlsx.read_bytes()
    main(["--input-xlsx", str(input_xlsx), "--output-xlsx", str(output_xlsx)])
    after_bytes = input_xlsx.read_bytes()

    assert before_bytes == after_bytes
    assert output_xlsx.exists()


def test_row_count_and_order_preserved(tmp_path):
    input_xlsx = tmp_path / "input.xlsx"
    output_xlsx = tmp_path / "output.xlsx"
    rows = [
        enriched_row(source_index=1, company_name="Zeta"),
        enriched_row(source_index=2, company_name="Alpha"),
        enriched_row(source_index=3, company_name="Middle"),
    ]
    write_workbook(input_xlsx, rows)

    main(["--input-xlsx", str(input_xlsx), "--output-xlsx", str(output_xlsx)])

    after = pd.read_excel(output_xlsx, sheet_name="Enriched Leads")
    assert len(after) == 3
    assert after["company_name"].tolist() == ["Zeta", "Alpha", "Middle"]
    assert after["source_index"].tolist() == [1, 2, 3]


def test_bad_phrase_replaced_in_sample_row(tmp_path, capsys):
    input_xlsx = tmp_path / "input.xlsx"
    output_xlsx = tmp_path / "output.xlsx"
    write_workbook(input_xlsx, [enriched_row()])

    main(["--input-xlsx", str(input_xlsx), "--output-xlsx", str(output_xlsx)])

    after = pd.read_excel(output_xlsx, sheet_name="Enriched Leads")
    combined_text = " ".join(
        str(after.at[0, field]) for field in TARGET_FIELDS if pd.notna(after.at[0, field])
    )
    for phrase in BAD_PHRASES:
        assert phrase not in combined_text

    captured = capsys.readouterr()
    assert "bad_phrases_before: " in captured.out
    assert "bad_phrases_after: 0" in captured.out


def test_no_network_or_api_calls_in_module_source():
    with open("refresh_lead_caller_app_fields_in_workbook.py", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    forbidden_modules = {"requests", "anthropic", "urllib", "httpx", "socket"}
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".")[0])

    assert not (imported_modules & forbidden_modules)


def test_missing_sheet_returns_error(tmp_path, capsys):
    input_xlsx = tmp_path / "input.xlsx"
    output_xlsx = tmp_path / "output.xlsx"
    with pd.ExcelWriter(input_xlsx, engine="openpyxl") as writer:
        pd.DataFrame([{"a": 1}]).to_excel(writer, sheet_name="Other Sheet", index=False)

    rc = main(["--input-xlsx", str(input_xlsx), "--output-xlsx", str(output_xlsx)])
    assert rc == 1
    assert not output_xlsx.exists()
