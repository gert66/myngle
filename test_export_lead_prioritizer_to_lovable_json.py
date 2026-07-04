"""Tests for export_lead_prioritizer_to_lovable_json.

All tests use small synthetic Excel workbooks written to tmp_path with
pandas/openpyxl. No API calls, no network, no real sample data.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from export_lead_prioritizer_to_lovable_json import (
    FOREIGN_HQ_SIGNAL_LABEL,
    LovableExportError,
    export_batch_output_tables_to_lovable_json,
    export_workbook_to_lovable_json,
    is_technical_reason,
    parse_key_source_links,
    sanitize_caller_facing_evidence,
)

# ---------------------------------------------------------------------------
# Synthetic workbook helpers
# ---------------------------------------------------------------------------

def enriched_row(**overrides) -> dict:
    row = {
        "source_index": 1,
        "company_name": "Acme Brasil",
        "domain": "acme.com.br",
        "input_country": "Brazil",
        "enrichment_skipped": False,
        "enrichment_skip_reason": "",
        "sig_foreign_hq_score_for_next_scoring": 3,
        "commercial_fit_score_app": 80,
        "commercial_tier_app": "A",
        "industry": "Manufacturing",
        "employee_range": "201-500",
    }
    row.update(overrides)
    return row


def write_workbook(path, enriched, evidence=None, signals=None,
                   run_summary=None, skip_enriched=False):
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if not skip_enriched:
            pd.DataFrame(enriched).to_excel(
                writer, sheet_name="Enriched Leads", index=False)
        if evidence is not None:
            pd.DataFrame(evidence).to_excel(
                writer, sheet_name="Evidence", index=False)
        if signals is not None:
            pd.DataFrame(signals).to_excel(
                writer, sheet_name="Signals", index=False)
        if run_summary is not None:
            pd.DataFrame(run_summary).to_excel(
                writer, sheet_name="Run Summary", index=False)


def run_export(tmp_path, enriched, evidence=None, signals=None,
               run_summary=None, **kwargs):
    xlsx = tmp_path / "workbook.xlsx"
    write_workbook(xlsx, enriched, evidence, signals, run_summary)
    out_dir = tmp_path / "lovable_export"
    options = dict(
        export_country="Brazil",
        cold_callers=["Jantje", "Pietje", "Marietje"],
        include_skipped=False,
        foreign_hq_only=True,
        bucket_size=500,
    )
    options.update(kwargs)
    manifest = export_workbook_to_lovable_json(xlsx, out_dir, **options)
    return manifest, out_dir


def load_list(out_dir) -> list[dict]:
    return json.loads((out_dir / "companies.list.json").read_text(encoding="utf-8"))


def load_all_details(out_dir) -> dict:
    details = {}
    for path in sorted(out_dir.glob("company-details-*.json")):
        details.update(json.loads(path.read_text(encoding="utf-8")))
    return details


def detail_for(out_dir, company_name) -> dict:
    for detail in load_all_details(out_dir).values():
        if detail["company_name"] == company_name:
            return detail
    raise AssertionError(f"No detail record for {company_name!r}")


# ---------------------------------------------------------------------------
# 1–2: reading, joining, output structure
# ---------------------------------------------------------------------------

def test_reads_sheets_and_joins_by_source_index(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Alpha", domain="alpha.com"),
        enriched_row(source_index=2, company_name="Beta", domain="beta.com"),
    ]
    evidence = [
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://alpha.com/about",
         "source_title": "Alpha about", "source_snippet": "Alpha is global."},
    ]
    signals = [
        {"source_index": 2, "signal_name": "rapid_growth", "signal_score": 2,
         "signal_reason": "Beta is growing fast.",
         "evidence_url": "https://news.example.com/beta"},
    ]
    manifest, out_dir = run_export(tmp_path, enriched, evidence, signals)

    alpha = detail_for(out_dir, "Alpha")
    beta = detail_for(out_dir, "Beta")
    assert len(alpha["evidence_snippets"]) == 1
    assert alpha["evidence_snippets"][0]["url"] == "https://alpha.com/about"
    assert alpha["debug"]["evidence_rows_count"] == 1
    assert alpha["debug"]["signals_rows_count"] == 0
    assert beta["evidence_snippets"] == []
    assert beta["debug"]["signals_rows_count"] == 1
    assert manifest["rows_exported"] == 2


def test_builds_list_json_and_detail_buckets(tmp_path):
    enriched = [enriched_row(source_index=i, company_name=f"Co{i}",
                             domain=f"co{i}.com") for i in range(1, 6)]
    manifest, out_dir = run_export(tmp_path, enriched, bucket_size=2)

    list_items = load_list(out_dir)
    assert len(list_items) == 5
    assert manifest["bucket_count"] == 3
    for item in list_items:
        bucket_file = item["detail_bucket"]
        assert bucket_file is not None
        bucket_path = out_dir / bucket_file
        assert bucket_path.exists()
        bucket = json.loads(bucket_path.read_text(encoding="utf-8"))
        assert item["company_id"] in bucket
        detail = bucket[item["company_id"]]
        assert detail["detail_bucket"] == bucket_file
    assert (out_dir / "company-details-000.json").exists()
    assert (out_dir / "export_manifest.json").exists()


# ---------------------------------------------------------------------------
# 3–4: skipped row handling
# ---------------------------------------------------------------------------

def test_excludes_skipped_rows_by_default(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Kept"),
        enriched_row(source_index=2, company_name="Skipped",
                     enrichment_skipped=True,
                     enrichment_skip_reason="Not confirmed foreign HQ"),
    ]
    manifest, out_dir = run_export(tmp_path, enriched)

    names = [item["company_name"] for item in load_list(out_dir)]
    assert names == ["Kept"]
    assert manifest["total_rows_read"] == 2
    assert manifest["rows_exported"] == 1
    assert manifest["skipped_rows_excluded"] == 1


def test_includes_skipped_rows_when_requested(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Kept"),
        enriched_row(source_index=2, company_name="Skipped",
                     enrichment_skipped=True,
                     enrichment_skip_reason="Not confirmed foreign HQ",
                     sig_foreign_hq_score_for_next_scoring=None),
    ]
    manifest, out_dir = run_export(tmp_path, enriched, include_skipped=True,
                                   foreign_hq_only=False)

    items = {item["company_name"]: item for item in load_list(out_dir)}
    assert set(items) == {"Kept", "Skipped"}
    assert items["Skipped"]["enrichment_skipped"] is True
    assert items["Skipped"]["enrichment_skip_reason"] == "Not confirmed foreign HQ"
    assert manifest["skipped_rows_excluded"] == 0
    assert manifest["include_skipped"] is True


# ---------------------------------------------------------------------------
# 5–6: foreign-HQ-only filtering
# ---------------------------------------------------------------------------

def test_foreign_hq_only_filters_to_detected_rows(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Foreign",
                     sig_foreign_hq_score_for_next_scoring=3),
        enriched_row(source_index=2, company_name="Domestic",
                     sig_foreign_hq_score_for_next_scoring=0),
    ]
    manifest, out_dir = run_export(tmp_path, enriched, foreign_hq_only=True)

    items = load_list(out_dir)
    assert [item["company_name"] for item in items] == ["Foreign"]
    assert all(item["foreign_hq_detected_for_export"] for item in items)
    assert manifest["foreign_hq_only"] is True
    assert manifest["foreign_hq_rows_exported"] == 1
    assert manifest["non_foreign_hq_rows_excluded"] == 1


def test_foreign_hq_only_false_includes_non_foreign_rows(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Foreign",
                     sig_foreign_hq_score_for_next_scoring=3),
        enriched_row(source_index=2, company_name="Domestic",
                     sig_foreign_hq_score_for_next_scoring=0),
    ]
    manifest, out_dir = run_export(tmp_path, enriched, foreign_hq_only=False)

    items = {item["company_name"]: item for item in load_list(out_dir)}
    assert set(items) == {"Foreign", "Domestic"}
    assert items["Foreign"]["foreign_hq_detected_for_export"] is True
    assert items["Domestic"]["foreign_hq_detected_for_export"] is False
    assert items["Domestic"]["foreign_hq_export_reason"] is None
    assert manifest["non_foreign_hq_rows_excluded"] == 0


# ---------------------------------------------------------------------------
# 7–10: foreign-HQ detection paths
# ---------------------------------------------------------------------------

def test_detects_foreign_hq_by_sig_score_3(tmp_path):
    enriched = [enriched_row(sig_foreign_hq_score_for_next_scoring=3)]
    _, out_dir = run_export(tmp_path, enriched)
    item = load_list(out_dir)[0]
    assert item["foreign_hq_detected_for_export"] is True
    assert item["foreign_hq_export_reason"] == "final_hq_score_3"


def test_detects_foreign_hq_by_c5_foreign_parent_confirmed(tmp_path):
    enriched = [enriched_row(
        sig_foreign_hq_score_for_next_scoring=0,
        c5_adjudication="foreign_parent_confirmed",
        c5_parent_company="Nissan Motor Corporation",
        c5_parent_hq_country="Japan",
    )]
    _, out_dir = run_export(tmp_path, enriched)
    item = load_list(out_dir)[0]
    assert item["foreign_hq_detected_for_export"] is True
    assert item["foreign_hq_export_reason"] == "c5_foreign_parent_confirmed"


def test_detects_foreign_hq_by_c5_recommended_score_3(tmp_path):
    enriched = [enriched_row(
        sig_foreign_hq_score_for_next_scoring=None,
        c5_recommended_hq_score=3,
    )]
    _, out_dir = run_export(tmp_path, enriched)
    item = load_list(out_dir)[0]
    assert item["foreign_hq_detected_for_export"] is True
    assert item["foreign_hq_export_reason"] == "c5_recommended_score_3"


def test_detects_foreign_hq_by_full_foreign_hq_only_enriched_row(tmp_path):
    # Conservative handling can leave score 0/blank on C5-confirmed rows in
    # full_foreign_hq_only outputs; a non-skipped row in that run mode counts.
    enriched = [
        enriched_row(source_index=1, company_name="Enriched",
                     sig_foreign_hq_score_for_next_scoring=0),
        enriched_row(source_index=2, company_name="Skipped",
                     enrichment_skipped=True,
                     sig_foreign_hq_score_for_next_scoring=0),
    ]
    run_summary = [{"run_mode": "full_foreign_hq_only", "processed_rows": 2}]
    manifest, out_dir = run_export(tmp_path, enriched,
                                   run_summary=run_summary)
    items = load_list(out_dir)
    assert [item["company_name"] for item in items] == ["Enriched"]
    assert items[0]["foreign_hq_export_reason"] == "full_foreign_hq_only_enriched_row"
    assert manifest["foreign_hq_rows_exported"] == 1


def test_detects_foreign_hq_by_signal_used_in_app(tmp_path):
    enriched = [enriched_row(
        sig_foreign_hq_score_for_next_scoring=None,
        foreign_hq_signal_used_in_app="Yes",
    )]
    _, out_dir = run_export(tmp_path, enriched)
    item = load_list(out_dir)[0]
    assert item["foreign_hq_detected_for_export"] is True
    assert item["foreign_hq_export_reason"] == "foreign_hq_signal_used_in_app"


# ---------------------------------------------------------------------------
# 11–12: sorting and cold caller assignment
# ---------------------------------------------------------------------------

def test_sorts_by_score_descending_before_assignment(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Low", domain="low.com",
                     commercial_fit_score_app=10),
        enriched_row(source_index=2, company_name="High", domain="high.com",
                     commercial_fit_score_app=90),
        enriched_row(source_index=3, company_name="Mid", domain="mid.com",
                     commercial_fit_score_app=None,
                     final_commercial_fit_score=50),
    ]
    manifest, out_dir = run_export(tmp_path, enriched)

    items = load_list(out_dir)
    assert [item["company_name"] for item in items] == ["High", "Mid", "Low"]
    assert [item["assigned_cold_caller_rank"] for item in items] == [1, 2, 3]
    assert manifest["score_sort_field_used"] == "commercial_fit_score_app"


def test_assigns_cold_callers_round_robin(tmp_path):
    enriched = [
        enriched_row(source_index=i, company_name=f"Co{i}",
                     domain=f"co{i}.com", commercial_fit_score_app=100 - i)
        for i in range(1, 5)
    ]
    manifest, out_dir = run_export(
        tmp_path, enriched, cold_callers=["Jantje", "Pietje", "Marietje"])

    items = load_list(out_dir)
    assert [item["assigned_cold_caller"] for item in items] == [
        "Jantje", "Pietje", "Marietje", "Jantje"]
    assert manifest["caller_distribution"] == {
        "Jantje": 2, "Pietje": 1, "Marietje": 1}
    details = load_all_details(out_dir)
    for item in items:
        assert details[item["company_id"]]["assigned_cold_caller"] == \
            item["assigned_cold_caller"]


# ---------------------------------------------------------------------------
# 13–16: evidence, source URLs, titles, visible signals
# ---------------------------------------------------------------------------

def test_builds_evidence_snippets_from_evidence_sheet(tmp_path):
    evidence = [
        {"source_index": 1, "signal_name": "international_profile",
         "query_used": "acme international",
         "source_url": "https://www.example.com/acme",
         "source_title": "Acme goes global",
         "source_snippet": "Acme opened offices in 12 countries.",
         "source_type": "organic", "parser_source": "organic_1",
         "confidence": "High", "notes": "n1"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row()], evidence)

    snippets = detail_for(out_dir, "Acme Brasil")["evidence_snippets"]
    assert len(snippets) == 1
    snip = snippets[0]
    assert snip["title"] == "Acme goes global"
    assert snip["source_domain"] == "example.com"
    assert snip["query_type"] == "international_profile"
    assert snip["text"] == "Acme opened offices in 12 countries."
    assert snip["snippet"] == snip["text"]
    assert snip["url"] == "https://www.example.com/acme"
    assert snip["source"] == "organic"
    assert snip["confidence"] == "High"


def test_builds_source_urls_from_all_sources(tmp_path):
    enriched = [enriched_row(
        key_source_links_app=(
            "International profile — Title: https://links.example.com/a\n"
            "https://bare.example.com/page"
        ),
    )]
    evidence = [
        {"source_index": 1, "signal_name": "x",
         "source_url": "https://evidence.example.com/1",
         "source_snippet": "s"},
        {"source_index": 1, "signal_name": "x",
         "source_url": "https://evidence.example.com/1",  # duplicate
         "source_snippet": "s2"},
    ]
    signals = [
        {"source_index": 1, "signal_name": "rapid_growth", "signal_score": 2,
         "evidence_url": "https://signals.example.com/growth"},
    ]
    _, out_dir = run_export(tmp_path, enriched, evidence, signals)

    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["source_urls"] == [
        "https://evidence.example.com/1",
        "https://links.example.com/a",
        "https://bare.example.com/page",
        "https://signals.example.com/growth",
    ]
    # key_source_links_app must be {label, url} objects, not plain strings.
    assert detail["key_source_links_app"][0] == {
        "label": "International profile — Title",
        "url": "https://links.example.com/a",
    }
    assert detail["key_source_links_app"][1]["label"] == "bare.example.com"


def test_builds_serper_result_titles(tmp_path):
    evidence = [
        {"source_index": 1, "signal_name": "a", "source_title": "Title one",
         "source_url": "https://e.com/1", "source_snippet": "s1"},
        {"source_index": 1, "signal_name": "b", "source_title": "Title one",
         "source_url": "https://e.com/2", "source_snippet": "s2"},
        {"source_index": 1, "signal_name": "c", "source_title": "Title two",
         "source_url": "https://e.com/3", "source_snippet": "s3"},
        {"source_index": 1, "signal_name": "d", "source_title": None,
         "source_url": "https://e.com/4", "source_snippet": "s4"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row()], evidence)

    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["serper_result_titles"] == ["Title one", "Title two"]


def test_builds_visible_icp_signal_scores_with_foreign_hq_row(tmp_path):
    enriched = [enriched_row(
        c5_adjudication="foreign_parent_confirmed",
        c5_parent_company="Nissan Motor Corporation",
        c5_parent_hq_country="Japan",
    )]
    signals = [
        {"source_index": 1, "signal_name": "international_profile",
         "signal_score": 2, "signal_reason": "Operates in three regions."},
        {"source_index": 1, "signal_name": "unmapped_custom_signal",
         "signal_score": 1, "evidence_quote": "Some quote."},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    by_label = {row["label"]: row for row in visible}

    hq_row = by_label[FOREIGN_HQ_SIGNAL_LABEL]
    assert hq_row["score"] == 3
    assert "Nissan Motor Corporation" in hq_row["evidence"]
    assert "Japan" in hq_row["evidence"]
    # Internal C5 adjudication token must not leak into caller-facing text.
    assert "foreign_parent_confirmed" not in hq_row["evidence"]
    assert "C5" not in hq_row["evidence"]

    intl = by_label["International business context"]
    assert intl["score"] == 2
    assert intl["evidence"] == "Operates in three regions."

    # No raw technical tokens as labels.
    assert not any(label.startswith("sig_") for label in by_label)
    assert "unmapped_custom_signal" not in by_label
    assert by_label["Unmapped custom signal"]["evidence"] == "Some quote."


def test_visible_icp_signal_prefers_evidence_quote_over_technical_reason(tmp_path):
    enriched = [enriched_row()]
    signals = [
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 2,
         "signal_reason": "3 distinct keyword match(es) in evidence: training, "
                           "learning, development",
         "evidence_quote": "The company runs a dedicated L&D academy for new hires."},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    by_label = {row["label"]: row for row in visible}

    row = by_label["Explicit learning and development signal"]
    assert row["evidence"] == "The company runs a dedicated L&D academy for new hires."


def test_visible_icp_signal_hides_technical_reason_without_quote(tmp_path):
    enriched = [enriched_row()]
    signals = [
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 2,
         "signal_reason": "3 distinct keyword match(es) in evidence: training, "
                           "learning, development"},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    by_label = {row["label"]: row for row in visible}

    row = by_label["Explicit learning and development signal"]
    assert row["evidence"] != signals[0]["signal_reason"]
    assert "distinct keyword match" not in (row["evidence"] or "")


def test_visible_icp_signal_keeps_non_technical_reason_without_quote(tmp_path):
    enriched = [enriched_row()]
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "signal_reason": "Operates offices in three countries."},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    by_label = {row["label"]: row for row in visible}

    row = by_label["International business context"]
    assert row["evidence"] == "Operates offices in three countries."


@pytest.mark.parametrize("reason", [
    "3 distinct keyword match(es) in evidence: training, learning, development",
    "2 keyword match(es) found",
    "C5 confirmed foreign parent",
    "c5_adjudication: foreign_parent_confirmed",
    "sig_foreign_hq_score_for_next_scoring set to 3",
    "parser_source: knowledge_graph",
    "Adjudication result: confirmed",
    "raw AI classification: foreign_parent",
])
def test_is_technical_reason_detects_internal_text(reason):
    assert is_technical_reason(reason) is True


@pytest.mark.parametrize("reason", [
    "Operates offices in three countries.",
    "The company recently announced international expansion.",
    None,
    "",
])
def test_is_technical_reason_allows_user_facing_text(reason):
    assert is_technical_reason(reason) is False


def test_foreign_hq_visible_evidence_strips_c5_fragment(tmp_path):
    enriched = [enriched_row(
        c5_adjudication="foreign_parent_confirmed",
        c5_parent_company="Prudential Financial",
        c5_parent_hq_country="United States",
        c5_parent_hq_city="Newark",
    )]
    _, out_dir = run_export(tmp_path, enriched)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    hq_row = {row["label"]: row for row in visible}[FOREIGN_HQ_SIGNAL_LABEL]

    assert hq_row["evidence"] == (
        "Confirmed foreign parent: Prudential Financial, "
        "HQ United States (Newark)."
    )
    assert "C5" not in hq_row["evidence"]
    assert "foreign_parent_confirmed" not in hq_row["evidence"]


@pytest.mark.parametrize("adjudication", [
    "foreign_parent_confirmed",
    "domestic_confirmed",
    "unclear",
])
def test_sanitize_caller_facing_evidence_strips_c5_fragment_forms(adjudication):
    text_with_period = (
        f"Confirmed foreign parent: Acme Corp, HQ Germany (Berlin). "
        f"C5: {adjudication}."
    )
    text_without_period = (
        f"Foreign headquarters detected: Germany C5: {adjudication}."
    )
    assert sanitize_caller_facing_evidence(text_with_period) == (
        "Confirmed foreign parent: Acme Corp, HQ Germany (Berlin)."
    )
    assert sanitize_caller_facing_evidence(text_without_period) == (
        "Foreign headquarters detected: Germany"
    )


def test_sanitize_caller_facing_evidence_never_blanks_text():
    # A fragment preceded by real text is fully stripped, leaving the useful part.
    with_prefix = "Detected foreign HQ. C5: unclear."
    assert sanitize_caller_facing_evidence(with_prefix) == "Detected foreign HQ."
    assert sanitize_caller_facing_evidence(None) is None
    assert sanitize_caller_facing_evidence("") is None


def test_non_hq_evidence_quote_unaffected_by_c5_sanitization(tmp_path):
    enriched = [enriched_row()]
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "Runs regional offices across three countries. "
                            "C5 was never mentioned here."},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    by_label = {row["label"]: row for row in visible}

    row = by_label["International business context"]
    assert row["evidence"] == (
        "Runs regional offices across three countries. "
        "C5 was never mentioned here."
    )


def test_employer_branding_signal_maps_to_lovable_label(tmp_path):
    enriched = [enriched_row()]
    signals = [
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
         "evidence_quote": "Recognized as a great place to work by employees."},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    by_label = {row["label"]: row for row in visible}

    row = by_label["Employer branding or employee satisfaction"]
    assert row["score"] == 2.0
    assert row["evidence"] == "Recognized as a great place to work by employees."

    detail = detail_for(out_dir, "Acme Brasil")
    signal_names = {s["signal_name"] for s in detail["evidence_audit"]["signal_evidence"]}
    assert "employer_branding" in signal_names


# ---------------------------------------------------------------------------
# Sector / industry resolution
# ---------------------------------------------------------------------------

def test_existing_industry_not_overwritten_by_detected(tmp_path):
    enriched = [enriched_row(detected_industry="Retail")]
    _, out_dir = run_export(tmp_path, enriched)

    item = load_list(out_dir)[0]
    assert item["industry"] == "Manufacturing"  # input value preserved


def test_blank_industry_filled_from_detected(tmp_path):
    enriched = [enriched_row(industry="", detected_industry="Retail")]
    _, out_dir = run_export(tmp_path, enriched)

    item = load_list(out_dir)[0]
    assert item["industry"] == "Retail"


def test_unknown_industry_filled_from_detected(tmp_path):
    enriched = [enriched_row(industry="Unknown", detected_industry="Consumer goods")]
    _, out_dir = run_export(tmp_path, enriched)

    item = load_list(out_dir)[0]
    assert item["industry"] == "Consumer goods"


def test_industry_falls_back_to_lusha_then_unknown(tmp_path):
    enriched = [
        enriched_row(source_index=1, industry="", lusha_industry="Insurance"),
        enriched_row(source_index=2, company_name="Beta Ltd", domain="beta.com",
                     industry="", lusha_industry=""),
    ]
    _, out_dir = run_export(tmp_path, enriched)

    by_name = {item["company_name"]: item for item in load_list(out_dir)}
    assert by_name["Acme Brasil"]["industry"] == "Insurance"
    assert by_name["Beta Ltd"]["industry"] == "Unknown"


def test_sector_alias_column_is_used(tmp_path):
    enriched = [enriched_row(industry="", **{"Sector": "Retail"})]
    _, out_dir = run_export(tmp_path, enriched)

    item = load_list(out_dir)[0]
    assert item["industry"] == "Retail"


def test_lusha_industry_alias_with_space_and_casing_is_used(tmp_path):
    enriched = [enriched_row(industry="", **{"Lusha Industry": "Insurance"})]
    _, out_dir = run_export(tmp_path, enriched)

    item = load_list(out_dir)[0]
    assert item["industry"] == "Insurance"


@pytest.mark.parametrize("placeholder", ["Unknown", "unknown", "N/A", "n/a",
                                         "None", "none", "nan", "", "   "])
def test_placeholder_industry_values_are_skipped(tmp_path, placeholder):
    enriched = [enriched_row(industry=placeholder, detected_industry="Consumer goods")]
    _, out_dir = run_export(tmp_path, enriched)

    item = load_list(out_dir)[0]
    assert item["industry"] == "Consumer goods"


def test_fallback_stays_unknown_when_nothing_usable(tmp_path):
    enriched = [enriched_row(industry="N/A", detected_industry="none",
                             lusha_industry="", main_industry="Unknown")]
    _, out_dir = run_export(tmp_path, enriched)

    item = load_list(out_dir)[0]
    assert item["industry"] == "Unknown"


def test_industry_resolution_summary_in_manifest(tmp_path):
    enriched = [
        enriched_row(source_index=1, industry="Manufacturing"),
        enriched_row(source_index=2, company_name="Beta Ltd", domain="beta.com",
                     industry="", **{"Sector": "Retail"}),
        enriched_row(source_index=3, company_name="Gamma Ltd", domain="gamma.com",
                     industry="", detected_industry=""),
    ]
    manifest, _ = run_export(tmp_path, enriched)

    summary = manifest["industry_resolution_summary"]
    assert summary["known_count"] == 2
    assert summary["unknown_count"] == 1
    assert summary["source_counts"] == {"industry": 1, "Sector": 1}


def test_detail_exposes_detected_sector_fields(tmp_path):
    enriched = [enriched_row(
        industry="",
        detected_industry="Consumer goods",
        detected_sub_industry="Consumer electronics",
        detected_company_type="Subsidiary",
        sector_confidence="High",
        sector_reason="Matched sector keyword(s): consumer electronics.",
        sector_evidence_url="https://acme.com/about",
        sector_evidence_quote="Acme is a consumer electronics company.",
        sector_source_title="About Acme",
    )]
    _, out_dir = run_export(tmp_path, enriched)

    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["industry"] == "Consumer goods"
    assert detail["detected_industry"] == "Consumer goods"
    assert detail["detected_sub_industry"] == "Consumer electronics"
    assert detail["detected_company_type"] == "Subsidiary"
    assert detail["sector_confidence"] == "High"
    assert detail["sector_reason"].startswith("Matched sector")
    assert detail["sector_evidence_url"] == "https://acme.com/about"
    assert detail["sector_evidence_quote"] == "Acme is a consumer electronics company."
    assert detail["sector_source_title"] == "About Acme"


def test_sector_industry_not_a_visible_commercial_driver(tmp_path):
    enriched = [enriched_row(detected_industry="Retail")]
    evidence = [
        {"source_index": 1, "signal_name": "sector_industry",
         "source_url": "https://acme.com/about", "source_title": "About",
         "source_snippet": "Acme is a retail company."},
    ]
    _, out_dir = run_export(tmp_path, enriched, evidence)

    visible = detail_for(out_dir, "Acme Brasil")["visible_icp_signal_scores"]
    labels = {row["label"].lower() for row in visible}
    assert not any("sector" in label for label in labels)


def test_c5_audit_and_debug_fields_not_removed(tmp_path):
    enriched = [enriched_row(
        c5_adjudication="foreign_parent_confirmed",
        c5_parent_company="Prudential Financial",
        c5_parent_hq_country="United States",
        c5_parent_hq_city="Newark",
    )]
    _, out_dir = run_export(tmp_path, enriched)

    detail = detail_for(out_dir, "Acme Brasil")

    assert detail["evidence_audit"]["c5_audit"]["c5_adjudication"] == \
        "foreign_parent_confirmed"
    assert detail["evidence_audit"]["c5_audit"]["c5_parent_company"] == \
        "Prudential Financial"
    assert detail["debug"]["lead_prioritizer_row"]["c5_adjudication"] == \
        "foreign_parent_confirmed"


# ---------------------------------------------------------------------------
# 17: debug preservation
# ---------------------------------------------------------------------------

def test_preserves_extra_fields_under_debug(tmp_path):
    enriched = [enriched_row(
        hq_reason="Knowledge graph HQ hit",
        some_future_column="future value",
    )]
    evidence = [{"source_index": 1, "signal_name": "a",
                 "source_url": "https://e.com/1", "source_snippet": "s"}]
    signals = [{"source_index": 1, "signal_name": "rapid_growth",
                "signal_score": 2}]
    _, out_dir = run_export(tmp_path, enriched, evidence, signals)

    detail = detail_for(out_dir, "Acme Brasil")
    debug_row = detail["debug"]["lead_prioritizer_row"]
    assert debug_row["hq_reason"] == "Knowledge graph HQ hit"
    assert debug_row["some_future_column"] == "future value"
    assert detail["debug"]["evidence_rows_count"] == 1
    assert detail["debug"]["signals_rows_count"] == 1
    # hq_* fields also land in the evidence audit.
    assert detail["evidence_audit"]["hq_audit"]["hq_reason"] == \
        "Knowledge graph HQ hit"


# ---------------------------------------------------------------------------
# 18–19: manifest contents
# ---------------------------------------------------------------------------

def test_manifest_contains_caller_distribution_and_counts(tmp_path):
    enriched = [
        enriched_row(source_index=i, company_name=f"Co{i}", domain=f"co{i}.com")
        for i in range(1, 4)
    ]
    manifest, out_dir = run_export(
        tmp_path, enriched, cold_callers=["Jantje", "Pietje"])

    assert manifest["total_rows_read"] == 3
    assert manifest["rows_exported"] == 3
    assert manifest["skipped_rows_excluded"] == 0
    assert manifest["bucket_count"] == 1
    assert manifest["caller_distribution"] == {"Jantje": 2, "Pietje": 1}
    assert manifest["export_country"] == "Brazil"
    assert manifest["cold_callers"] == ["Jantje", "Pietje"]
    assert "Enriched Leads" in manifest["source_sheets_found"]
    assert manifest["validation_summary"]["status"] == "ok"
    on_disk = json.loads(
        (out_dir / "export_manifest.json").read_text(encoding="utf-8"))
    assert on_disk["caller_distribution"] == manifest["caller_distribution"]


def test_manifest_contains_foreign_hq_fields(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Foreign",
                     sig_foreign_hq_score_for_next_scoring=3),
        enriched_row(source_index=2, company_name="Domestic",
                     sig_foreign_hq_score_for_next_scoring=0),
    ]
    manifest, _ = run_export(tmp_path, enriched, foreign_hq_only=True)

    assert manifest["foreign_hq_only"] is True
    assert manifest["foreign_hq_rows_exported"] == 1
    assert manifest["non_foreign_hq_rows_excluded"] == 1


# ---------------------------------------------------------------------------
# 20–21: missing sheets
# ---------------------------------------------------------------------------

def test_missing_evidence_and_signals_sheets_warn_but_succeed(tmp_path):
    manifest, out_dir = run_export(tmp_path, [enriched_row()])

    assert manifest["rows_exported"] == 1
    warning_text = " ".join(manifest["warnings"])
    assert "'Evidence'" in warning_text
    assert "'Signals'" in warning_text
    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["evidence_snippets"] == []
    assert detail["source_urls"] == []
    assert isinstance(detail["visible_icp_signal_scores"], list)


def test_missing_enriched_leads_sheet_fails_clearly(tmp_path):
    xlsx = tmp_path / "workbook.xlsx"
    write_workbook(
        xlsx, enriched=None, skip_enriched=True,
        evidence=[{"source_index": 1, "signal_name": "a",
                   "source_url": "https://e.com/1"}],
    )
    with pytest.raises(LovableExportError, match="Enriched Leads"):
        export_workbook_to_lovable_json(
            xlsx, tmp_path / "out", export_country="Brazil",
            cold_callers=["Jantje"])


# ---------------------------------------------------------------------------
# Extra coverage: country authority, ui_payload, link parsing
# ---------------------------------------------------------------------------

def test_export_country_is_authoritative_and_original_preserved(tmp_path):
    enriched = [enriched_row(input_country="Brasil (BR)")]
    _, out_dir = run_export(tmp_path, enriched, export_country="Brazil")

    item = load_list(out_dir)[0]
    assert item["country"] == "Brazil"
    assert item["input_country"] == "Brazil"
    assert item["display_country_app"] == "Brazil"
    assert item["export_country"] == "Brazil"
    assert item["original_input_country"] == "Brasil (BR)"


def test_netherlands_export_produces_standard_structure(tmp_path):
    # The exporter is country-agnostic; Netherlands must behave exactly like
    # the existing countries: same standard files, country fields verbatim.
    enriched = [enriched_row(input_country="Nederland (NL)")]
    manifest, out_dir = run_export(
        tmp_path, enriched, export_country="Netherlands")

    assert (out_dir / "companies.list.json").exists()
    assert (out_dir / "company-details-000.json").exists()
    assert (out_dir / "export_manifest.json").exists()
    assert manifest["export_country"] == "Netherlands"

    item = load_list(out_dir)[0]
    assert item["country"] == "Netherlands"
    assert item["input_country"] == "Netherlands"
    assert item["display_country_app"] == "Netherlands"
    assert item["export_country"] == "Netherlands"

    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["export_country"] == "Netherlands"


def test_ui_payload_and_array_fields(tmp_path):
    enriched = [enriched_row(
        why_relevant_app="Strong foreign parent and L&D hiring.",
        what_is_hot_app="Foreign parent confirmed\nHiring L&D manager",
        what_is_not_app="No competitor signal",
        caller_angle_app="Lead with onboarding angle.",
        call_starter_app="I saw you are part of a Japanese group...",
        cold_caller_summary_app="Concrete reason to explore cross-border alignment.",
        parent_hq_summary_app="The enrichment data identifies Acme Group as the parent company.",
        evidence_summary_app="Two strong sources.",
        buyer_route_app="HR Director | L&D Manager",
        likely_training_interest_app="Business English; Onboarding English",
    )]
    evidence = [{"source_index": 1, "signal_name": "a",
                 "source_url": "https://e.com/1", "source_snippet": "s"}]
    _, out_dir = run_export(tmp_path, enriched, evidence)

    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["what_is_hot_app"] == [
        "Foreign parent confirmed", "Hiring L&D manager"]
    assert detail["what_is_not_app"] == ["No competitor signal"]
    assert detail["buyer_route_app"] == ["HR Director", "L&D Manager"]
    assert detail["likely_training_interest_app"] == [
        "Business English", "Onboarding English"]
    assert detail["cold_caller_summary_app"] == "Concrete reason to explore cross-border alignment."
    assert detail["parent_hq_summary_app"] == \
        "The enrichment data identifies Acme Group as the parent company."
    assert detail["ui_payload"] == {
        "why_relevant": "Strong foreign parent and L&D hiring.",
        "what_is_hot": ["Foreign parent confirmed", "Hiring L&D manager"],
        "what_is_not": ["No competitor signal"],
        "caller_angle": "Lead with onboarding angle.",
        "call_starter": "I saw you are part of a Japanese group...",
        "cold_caller_summary": "Concrete reason to explore cross-border alignment.",
        "parent_hq_summary": "The enrichment data identifies Acme Group as the parent company.",
        "evidence_summary": "Two strong sources.",
        "source_urls": ["https://e.com/1"],
    }
    # Lovable list-compatibility fallbacks.
    item = load_list(out_dir)[0]
    assert item["commercial_fit_score"] == 80  # copied from _app field
    assert item["commercial_tier"] == "A"


def test_parse_key_source_links_variants():
    parsed = parse_key_source_links(
        "International profile — Title: https://example.com/a\n"
        "https://www.example.com/b\n"
        "Two urls: https://x.com/1 https://x.com/2\n"
        "No url on this line"
    )
    assert parsed[0] == {"label": "International profile — Title",
                         "url": "https://example.com/a"}
    assert parsed[1] == {"label": "example.com",
                         "url": "https://www.example.com/b"}
    assert parsed[2]["url"] == "https://x.com/1"
    assert parsed[3]["url"] == "https://x.com/2"
    assert parsed[2]["label"] == parsed[3]["label"] == "Two urls"
    assert len(parsed) == 4


def test_stable_company_ids_avoid_collisions(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="Same", domain="same.com"),
        enriched_row(source_index=2, company_name="Same Two", domain="same.com"),
        enriched_row(source_index=3, company_name="No Domain", domain=None),
    ]
    _, out_dir = run_export(tmp_path, enriched)

    ids = [item["company_id"] for item in load_list(out_dir)]
    assert len(set(ids)) == 3
    assert "same-com" in ids
    assert any(cid.startswith("no-domain") for cid in ids)


# ---------------------------------------------------------------------------
# export_batch_output_tables_to_lovable_json — in-memory DataFrames -> JSON
# (integrates the Streamlit batch app's output_tables without a manual
# save-Excel-then-reupload step; delegates straight to
# export_workbook_to_lovable_json via a temporary workbook)
# ---------------------------------------------------------------------------

class TestExportBatchOutputTablesToLovableJson:
    def _output_tables(self):
        enriched = pd.DataFrame([
            enriched_row(source_index=1, company_name="Acme Brasil", domain="acme.com.br"),
            enriched_row(source_index=2, company_name="Beta Brasil", domain="beta.com.br",
                        sig_foreign_hq_score_for_next_scoring=0,
                        enrichment_skipped=True, enrichment_skip_reason="Not confirmed foreign HQ"),
        ])
        evidence = pd.DataFrame([
            {"source_index": 1, "signal_name": "international_profile",
             "source_url": "https://acme.com.br/about", "source_title": "About",
             "source_snippet": "Global footprint."},
        ])
        signals = pd.DataFrame([
            {"source_index": 1, "signal_name": "international_profile",
             "signal_score": 2, "signal_value": "yes"},
        ])
        run_summary = pd.DataFrame([{"run_mode": "full_foreign_hq_only"}])
        return {
            "enriched_leads": enriched, "evidence": evidence,
            "signals": signals, "run_summary": run_summary,
        }

    def test_generates_expected_json_files(self, tmp_path):
        out_dir = tmp_path / "lovable_export"
        manifest = export_batch_output_tables_to_lovable_json(
            self._output_tables(), out_dir, export_country="Brazil",
            cold_callers=["Jantje", "Pietje"],
        )
        assert (out_dir / "companies.list.json").exists()
        assert (out_dir / "company-details-000.json").exists()
        assert (out_dir / "export_manifest.json").exists()
        assert manifest["rows_exported"] == 1  # Beta Brasil skipped (not confirmed)
        assert manifest["validation_summary"]["status"] == "ok"

    def test_does_not_leak_a_temp_workbook_on_disk(self, tmp_path):
        out_dir = tmp_path / "lovable_export"
        before = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
        export_batch_output_tables_to_lovable_json(
            self._output_tables(), out_dir, export_country="Brazil",
            cold_callers=["Jantje"],
        )
        after = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
        assert after == before  # the temp workbook is always cleaned up

    def test_matches_export_workbook_to_lovable_json_output(self, tmp_path):
        tables = self._output_tables()
        manifest_a = export_batch_output_tables_to_lovable_json(
            tables, tmp_path / "from_tables", export_country="Brazil",
            cold_callers=["Jantje", "Pietje"],
        )

        xlsx = tmp_path / "workbook.xlsx"
        write_workbook(xlsx, tables["enriched_leads"].to_dict("records"),
                       tables["evidence"].to_dict("records"),
                       tables["signals"].to_dict("records"),
                       tables["run_summary"].to_dict("records"))
        manifest_b = export_workbook_to_lovable_json(
            xlsx, tmp_path / "from_workbook", export_country="Brazil",
            cold_callers=["Jantje", "Pietje"],
        )
        assert manifest_a["rows_exported"] == manifest_b["rows_exported"]
        assert manifest_a["caller_distribution"] == manifest_b["caller_distribution"]
        assert manifest_a["validation_summary"] == manifest_b["validation_summary"]
