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
    main as export_cli_main,
    parse_key_source_links,
    sanitize_caller_facing_evidence,
    normalize_content_language,
    should_localize_content,
    localize_detail_record_for_dutch,
    localize_detail_record_for_italian,
)
from lovable_content_localization import (
    translate_known_label,
    localize_why_relevant_app,
    localize_caller_angle_app,
    localize_call_starter_app,
    localize_caution_app,
    localize_cold_caller_summary_app,
    localize_parent_hq_summary_app,
    localize_evidence_summary_app,
    localize_foreign_hq_evidence_text,
    localize_what_is_hot_item,
    localize_what_is_not_item,
    translate_known_label_it,
    localize_why_relevant_app_it,
    localize_caller_angle_app_it,
    localize_call_starter_app_it,
    localize_caution_app_it,
    localize_cold_caller_summary_app_it,
    localize_parent_hq_summary_app_it,
    localize_evidence_summary_app_it,
    localize_foreign_hq_evidence_text_it,
    localize_what_is_hot_item_it,
    localize_what_is_not_item_it,
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
                   run_summary=None, skip_enriched=False, deep_dive=None):
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
        if deep_dive is not None:
            pd.DataFrame(deep_dive).to_excel(
                writer, sheet_name="Deep Dive", index=False)


def run_export(tmp_path, enriched, evidence=None, signals=None,
               run_summary=None, deep_dive=None, **kwargs):
    xlsx = tmp_path / "workbook.xlsx"
    write_workbook(xlsx, enriched, evidence, signals, run_summary, deep_dive=deep_dive)
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


# ---------------------------------------------------------------------------
# "Test" bucket export-override (Onderdeel B): export_country is purely a
# display-label override at export time. The country actually used for
# enrichment/localization during the run (input_country on each row) is
# preserved verbatim in original_input_country and never touched.
# ---------------------------------------------------------------------------

def test_country_test_override_only_changes_display_fields(tmp_path):
    enriched = [enriched_row(input_country="Italy", commercial_fit_score_app=77,
                             sig_foreign_hq_score_for_next_scoring=3)]
    italy_src = tmp_path / "italy"
    test_src = tmp_path / "test"
    italy_src.mkdir()
    test_src.mkdir()
    _, out_dir_italy = run_export(italy_src, enriched, export_country="Italy")
    _, out_dir_test = run_export(test_src, enriched, export_country="Test")

    item_italy = load_list(out_dir_italy)[0]
    item_test = load_list(out_dir_test)[0]

    assert item_test["country"] == "Test"
    assert item_test["export_country"] == "Test"
    assert item_test["display_country_app"] == "Test"
    # The real enrichment-time country is preserved regardless of the label.
    assert item_test["original_input_country"] == "Italy"
    assert item_italy["original_input_country"] == "Italy"

    # Everything else (score, signals-derived fields) is untouched by the
    # bucket-label override.
    for key in ("commercial_fit_score", "commercial_tier",
                "foreign_hq_detected_for_export", "company_name", "domain"):
        assert item_test[key] == item_italy[key]


def test_country_mismatch_warning_present_for_test_bucket(tmp_path):
    enriched = [enriched_row(input_country="Italy")]
    manifest, _ = run_export(tmp_path, enriched, export_country="Test")
    warning_text = " ".join(manifest["warnings"])
    assert "LET OP" in warning_text
    assert "'Test'" in warning_text
    assert "Italy" in warning_text


def test_country_mismatch_warning_absent_when_matching(tmp_path):
    enriched = [enriched_row(input_country="Italy")]
    manifest, _ = run_export(tmp_path, enriched, export_country="Italy")
    warning_text = " ".join(manifest["warnings"])
    assert "LET OP" not in warning_text


def test_country_mismatch_warning_lists_every_distinct_source_country(tmp_path):
    enriched = [
        enriched_row(source_index=1, company_name="A", input_country="Italy"),
        enriched_row(source_index=2, company_name="B", input_country="Netherlands"),
    ]
    manifest, _ = run_export(tmp_path, enriched, export_country="Test")
    warning_text = " ".join(manifest["warnings"])
    assert "Italy" in warning_text
    assert "Netherlands" in warning_text


def test_cli_prints_mismatch_warning_for_test_bucket(tmp_path, capsys):
    xlsx = tmp_path / "workbook.xlsx"
    write_workbook(xlsx, [enriched_row(input_country="Italy")])
    out_dir = tmp_path / "out"
    export_cli_main([
        "--input-xlsx", str(xlsx), "--output-dir", str(out_dir),
        "--country", "Test", "--cold-callers", "Jantje",
        "--no-foreign-hq-only",
    ])
    captured = capsys.readouterr()
    assert "WARNING: LET OP" in captured.out
    assert "Italy" in captured.out


def test_cli_prints_no_mismatch_warning_for_matching_country(tmp_path, capsys):
    xlsx = tmp_path / "workbook.xlsx"
    write_workbook(xlsx, [enriched_row(input_country="Italy")])
    out_dir = tmp_path / "out"
    export_cli_main([
        "--input-xlsx", str(xlsx), "--output-dir", str(out_dir),
        "--country", "Italy", "--cold-callers", "Jantje",
        "--no-foreign-hq-only",
    ])
    captured = capsys.readouterr()
    assert "LET OP" not in captured.out


def test_test_bucket_output_path_never_collides_with_existing_country_export(tmp_path):
    # Simulate the Streamlit app's per-country output-folder convention
    # (lovable_json_exports/<country>/...) and prove a "Test" export can
    # never land in, or overwrite, an existing country's folder.
    from lead_prioritizer_batch_app import default_auto_lovable_base_folder

    assert Path(default_auto_lovable_base_folder("Italy")) == Path("lovable_json_exports/Italy")
    assert Path(default_auto_lovable_base_folder("Test")) == Path("lovable_json_exports/Test")

    base = tmp_path / "lovable_json_exports"
    italy_src = base / "Italy"
    test_src = base / "Test"
    italy_src.mkdir(parents=True)
    test_src.mkdir(parents=True)

    enriched = [enriched_row(input_country="Italy")]
    _, italy_out_dir = run_export(italy_src, enriched, export_country="Italy")
    assert italy_out_dir != test_src  # sanity: distinct base directories
    italy_manifest_before = (italy_out_dir / "export_manifest.json").read_text(encoding="utf-8")

    _, test_out_dir = run_export(test_src, enriched, export_country="Test")

    assert italy_out_dir != test_out_dir
    # The pre-existing Italy export is completely untouched by the Test export.
    assert (italy_out_dir / "export_manifest.json").read_text(encoding="utf-8") == \
        italy_manifest_before
    assert (test_out_dir / "export_manifest.json").exists()
    assert load_list(test_out_dir)[0]["country"] == "Test"
    assert load_list(italy_out_dir)[0]["country"] == "Italy"


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
    ui_payload = detail["ui_payload"]
    # These still mirror the *_app fields verbatim — unchanged, Italy-compatible
    # behavior kept for every export, not just Italian ones.
    assert ui_payload["what_is_not"] == ["No competitor signal"]
    assert ui_payload["caller_angle"] == "Lead with onboarding angle."
    assert ui_payload["call_starter"] == "I saw you are part of a Japanese group..."
    # parent_hq_summary is now rebuilt fresh for non-Italy (never a straight
    # mirror of parent_hq_summary_app) — no c5_parent_company/hq_country in
    # this fixture, so there's nothing safe to build it from.
    assert ui_payload["parent_hq_summary"] is None
    assert ui_payload["evidence_summary"] == "Two strong sources."
    # These are independently built (non-Italy curated layer), richer,
    # company-specific content — not a mirror of why_relevant_app /
    # what_is_hot_app / cold_caller_summary_app.
    assert ui_payload["why_relevant"] == (
        "Acme Brasil is a Brazil-based company in Manufacturing. It has a "
        "confirmed foreign parent or HQ context. The current evidence is "
        "not strong enough to confirm a specific training trigger, so "
        "treat this as a light discovery lead and first validate whether "
        "international communication, onboarding, or team-development "
        "needs exist.")
    assert ui_payload["what_is_hot"] == [
        "Foreign ownership or group structure confirmed.",
        "Industry: Manufacturing.",
        "Company size: 201-500 employees.",
    ]
    assert len(ui_payload["what_is_hot"]) <= 5
    # commercial_fit_drivers always lists all six fixed dimensions now — the
    # other five have no signal data at all in this fixture, so they're
    # "Not evidenced" rather than omitted.
    drivers_by_label = {d["label"]: d for d in ui_payload["commercial_fit_drivers"]}
    assert len(ui_payload["commercial_fit_drivers"]) == 6
    assert drivers_by_label["Foreign ownership or group structure"] == {
        "id": "foreign_ownership_or_group_structure",
        "label": "Foreign ownership or group structure",
        "strength": "Strong",
        "evidence": "Foreign headquarters or group structure detected.",
        "note": "",
    }
    for label in (
        "International business context", "Explicit learning and development",
        "Learning and development or onboarding needs", "Possible onboarding need",
        "Employer branding or employee satisfaction",
    ):
        assert drivers_by_label[label]["strength"] == "Not evidenced"
        assert drivers_by_label[label]["evidence"] == ""
        assert drivers_by_label[label]["note"]
    assert ui_payload["cold_caller_summary"] == (
        "This company has a confirmed foreign parent or HQ context, which "
        "is a concrete reason to explore cross-border communication and "
        "team alignment.")
    assert ui_payload["caution"] == []
    # "e.com" is unrelated to the lead's own domain (acme.com.br) and its
    # snippet ("s") never mentions the company, so it's excluded rather than
    # promoted as a visible source link.
    assert ui_payload["source_urls"] == []
    # Lovable list-compatibility fallbacks.
    item = load_list(out_dir)[0]
    assert item["commercial_fit_score"] == 80  # copied from _app field
    assert item["commercial_tier"] == "A"


# ---------------------------------------------------------------------------
# ui_payload richer content builders (Brazil-style, controlled) — item 7 of
# the Lovable JSON export content upgrade: what_is_hot capped at 5 clean
# bullets, no technical tokens, human-readable caution, deduplicated labeled
# source URLs, and weak/generic/suspicious evidence never promoted.
# ---------------------------------------------------------------------------

def test_brazil_style_record_has_clean_what_is_hot_max_five_bullets(tmp_path):
    enriched = [enriched_row(
        c5_parent_company="Shandong Heavy Industry Group",
        c5_parent_hq_country="China",
        needs_manual_review=True,
    )]
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "Operates offices across five countries in Latin America."},
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": "Actively hiring an L&D manager for new-hire onboarding."},
        {"source_index": 1, "signal_name": "company_size_complexity", "signal_score": 1,
         "evidence_quote": "Runs a multi-site manufacturing operation."},
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 2,
         "signal_reason": "3 distinct keyword match(es) in evidence: training, learning, development"},
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 1,
         "evidence_quote": "Recognized as a great place to work."},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    detail = detail_for(out_dir, "Acme Brasil")
    what_is_hot = detail["ui_payload"]["what_is_hot"]

    assert len(what_is_hot) <= 5
    assert what_is_hot[0] == "Foreign ownership or group structure: headquartered in China."
    assert what_is_hot[1] == (
        "International business context: Operates offices across five "
        "countries in Latin America.")
    joined = " ".join(what_is_hot)
    assert "distinct keyword match" not in joined
    assert "Industry: Unknown" not in joined


def test_ui_payload_visible_text_has_no_technical_tokens(tmp_path):
    enriched = [enriched_row(
        c5_adjudication="foreign_parent_confirmed",
        c5_parent_company="Foreign Group",
        c5_parent_hq_country="Germany",
    )]
    signals = [
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 2,
         "signal_reason": "3 distinct keyword match(es) in evidence: training, learning, development"},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    detail = detail_for(out_dir, "Acme Brasil")
    ui = detail["ui_payload"]
    visible_text = " ".join([
        ui["why_relevant"] or "",
        " ".join(ui["what_is_hot"]),
        " ".join(str(d) for d in ui["commercial_fit_drivers"]),
    ])
    for token in ("sig_", "ti_", "c4_", "c5_", "foreign_parent_confirmed"):
        assert token not in visible_text
    assert "Positive signals:" not in visible_text
    assert "Buying signals:" not in visible_text


def test_caution_never_exposes_raw_quality_flag_names(tmp_path):
    enriched = [enriched_row(
        needs_manual_review=True,
        hq_evidence_domain_mismatch_warning="Yes",
        hq_positive_score_suppressed_for_review="Yes",
    )]
    _, out_dir = run_export(tmp_path, enriched)

    detail = detail_for(out_dir, "Acme Brasil")
    caution = detail["ui_payload"]["caution"]

    assert caution, "expected human-readable caution entries"
    joined = " ".join(caution)
    for raw_flag in (
        "hq_evidence_domain_mismatch_warning",
        "needs_manual_review",
        "hq_positive_score_suppressed_for_review",
        "parser_source",
        "raw_",
    ):
        assert raw_flag not in joined
    assert "Manual review recommended before outreach." in caution
    assert any("domain" in c.lower() for c in caution)


def test_ui_payload_source_urls_are_deduplicated_with_labels(tmp_path):
    enriched = [enriched_row(
        website_url="https://acme.com.br",
        careers_url="https://acme.com.br/careers",
        linkedin_url="https://www.linkedin.com/company/acme-brasil",
    )]
    evidence = [
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://acme.com.br", "source_snippet": "dup of website"},
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://www.linkedin.com/company/acme-brasil",
         "source_snippet": "dup of linkedin"},
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://otherdirectory.example.com/acme",
         "source_snippet": "Acme Brasil company profile on a third-party directory"},
    ]
    _, out_dir = run_export(tmp_path, enriched, evidence)

    detail = detail_for(out_dir, "Acme Brasil")
    source_urls = detail["ui_payload"]["source_urls"]

    urls = [item["url"] for item in source_urls]
    assert len(urls) == len(set(urls))  # deduplicated
    by_url = {item["url"]: item["label"] for item in source_urls}
    assert by_url["https://acme.com.br"] == "Official website"
    assert by_url["https://acme.com.br/careers"] == "Careers page"
    assert by_url["https://www.linkedin.com/company/acme-brasil"] == "LinkedIn"
    # Genuinely third-party (its snippet actually mentions the company) —
    # kept and labeled, unlike a truly unrelated domain (see below).
    assert by_url["https://otherdirectory.example.com/acme"] == "Third-party company profile"


def test_weak_generic_evidence_not_promoted_into_caller_facing_text(tmp_path):
    enriched = [enriched_row(employee_range="10001+")]
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "Signals point to international alignment."},
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 1,
         "evidence_quote": "The company has 5 employees according to this snippet."},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals)

    detail = detail_for(out_dir, "Acme Brasil")
    ui = detail["ui_payload"]

    joined_hot = " ".join(ui["what_is_hot"])
    assert "Signals point to" not in joined_hot
    assert "5 employees" not in joined_hot

    # commercial_fit_drivers always lists all six fixed dimensions; with no
    # curated evidence behind either signal, they show as "Rejected" (not
    # positively claimed) rather than a weak/evidence-less "Strong" row —
    # that's what made the old BauWatch-style output inconsistent with
    # what_is_hot, and they must never be silently omitted either.
    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    assert len(ui["commercial_fit_drivers"]) == 6
    assert drivers_by_label["International business context"]["strength"] == "Rejected"
    assert drivers_by_label["International business context"]["evidence"] == ""
    assert drivers_by_label["Learning and development or onboarding needs"]["strength"] == "Rejected"
    assert drivers_by_label["Learning and development or onboarding needs"]["evidence"] == ""


# ---------------------------------------------------------------------------
# Netherlands / DORC-style content-quality regression tests — stricter
# ui_payload filters: raw location dumps, unrelated-domain evidence, caution
# fragmentation, source-url dedup/labeling, what_is_hot <-> commercial_fit_
# drivers consistency, and "l&d"/raw-token wording.
# ---------------------------------------------------------------------------

def _dorc_row(**overrides) -> dict:
    row = dict(
        source_index=1,
        company_name="DORC Dutch Ophthalmic Research Center (International)",
        domain="dorcglobal.com",
        website_url="https://www.dorcglobal.com",
        careers_url="https://www.dorcglobal.com/careers",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=60,
        commercial_tier_app="B",
        industry="Medical devices",
        employee_range="201-500",
    )
    row.update(overrides)
    return row


def test_raw_multiline_location_dump_not_promoted_to_what_is_hot(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": (
             "DORC locations\n___THE NETHERLANDS\n___Austria\n___China\n"
             "___United States\n___Brazil"
         )},
    ]
    _, out_dir = run_export(tmp_path, [_dorc_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "DORC Dutch Ophthalmic Research Center (International)")
    joined_hot = " ".join(detail["ui_payload"]["what_is_hot"])
    assert "___" not in joined_hot
    assert "DORC locations" not in joined_hot
    # No curated evidence survives the raw-dump filter, so the driver shows
    # as Rejected (with an explanatory note) rather than a positive claim —
    # but it's still present, not silently omitted.
    drivers_by_label = {d["label"]: d for d in detail["ui_payload"]["commercial_fit_drivers"]}
    assert drivers_by_label["International business context"]["strength"] == "Rejected"
    assert drivers_by_label["International business context"]["evidence"] == ""


def test_unrelated_domain_excluded_from_source_urls_and_evidence(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": "Back by popular demand...",
         "evidence_url": "https://careers.accor.com/jobs/123"},
    ]
    evidence = [
        {"source_index": 1, "signal_name": "onboarding_training_need",
         "source_url": "https://careers.accor.com/jobs/123",
         "source_title": "Accor Careers", "source_snippet": "Back by popular demand..."},
    ]
    _, out_dir = run_export(tmp_path, [_dorc_row()], evidence, signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "DORC Dutch Ophthalmic Research Center (International)")
    ui = detail["ui_payload"]

    urls = [item["url"] for item in ui["source_urls"]]
    assert not any("accor.com" in url for url in urls)
    joined_hot = " ".join(ui["what_is_hot"])
    assert "Back by popular demand" not in joined_hot
    assert "accor" not in joined_hot.lower()
    # No curated evidence survives (event fragment + unrelated domain), so
    # the bucketed driver is omitted entirely, not shown as an empty row.
    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    assert "learning and development or onboarding needs" not in drivers_by_label


def test_caution_warning_deduplicated_into_one_sentence(tmp_path):
    enriched = [_dorc_row(hq_evidence_domain_mismatch_warning="Yes")]
    _, out_dir = run_export(tmp_path, enriched, export_country="Netherlands",
                            foreign_hq_only=False)

    detail = detail_for(out_dir, "DORC Dutch Ophthalmic Research Center (International)")
    caution = detail["ui_payload"]["caution"]

    domain_warnings = [c for c in caution if "domain" in c.lower()]
    assert len(domain_warnings) == 1
    assert domain_warnings[0] == (
        "The HQ evidence source does not clearly match the lead's own "
        "domain; verify the HQ signal before relying on it."
    )


def test_own_domain_labeled_official_website_no_duplicates(tmp_path):
    # Same homepage as website_url ("https://www.dorcglobal.com"), just a
    # different scheme/www form — must dedupe to a single entry, not a
    # second "dorcglobal.com" row mislabeled as third-party.
    evidence = [
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "http://dorcglobal.com", "source_snippet": "DORC home"},
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://dorcglobal.com/", "source_snippet": "DORC home"},
    ]
    _, out_dir = run_export(tmp_path, [_dorc_row()], evidence,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "DORC Dutch Ophthalmic Research Center (International)")
    source_urls = detail["ui_payload"]["source_urls"]

    dorc_home_entries = [
        item for item in source_urls
        if item["url"].rstrip("/").endswith("dorcglobal.com")
    ]
    assert len(dorc_home_entries) == 1
    assert dorc_home_entries[0]["label"] == "Official website"


def test_commercial_fit_drivers_not_inconsistent_with_what_is_hot(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "Runs subsidiaries in Germany, France, and Japan."},
        {"source_index": 1, "signal_name": "company_size_complexity", "signal_score": 2,
         "evidence_quote": "Possible onboarding need due to multi-site structure."},
    ]
    _, out_dir = run_export(tmp_path, [_dorc_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "DORC Dutch Ophthalmic Research Center (International)")
    ui = detail["ui_payload"]
    summary_line = ui["what_is_hot"][0]

    # onboarding_training_need itself is absent (no such signal row above);
    # only company_size_complexity is positive, so the summary line must not
    # falsely claim a learning-and-development signal off the shared
    # "onboarding" word in company_size_complexity's own display label.
    assert "Learning and development" not in summary_line
    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    assert "learning and development or onboarding needs" not in drivers_by_label


def test_no_visible_ui_payload_field_contains_shorthand_or_raw_tokens(tmp_path):
    enriched = [_dorc_row(
        c5_parent_company="Some Foreign Group", c5_parent_hq_country="Germany",
        sig_foreign_hq_score_for_next_scoring=3,
    )]
    signals = [
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": "Runs a structured onboarding and training program for new hires."},
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 2,
         "signal_reason": "3 distinct keyword match(es) in evidence: training, learning, development"},
    ]
    _, out_dir = run_export(tmp_path, enriched, signals=signals,
                            export_country="Netherlands")

    detail = detail_for(out_dir, "DORC Dutch Ophthalmic Research Center (International)")
    ui = detail["ui_payload"]

    visible_text = " ".join([
        ui["why_relevant"] or "",
        " ".join(ui["what_is_hot"]),
        " ".join(str(d) for d in ui["commercial_fit_drivers"]),
        " ".join(ui["caution"]),
    ])
    assert "l&d" not in visible_text.lower()
    assert "___" not in visible_text
    for token in ("sig_", "ti_", "c4_", "c5_"):
        assert token not in visible_text


# ---------------------------------------------------------------------------
# BauWatch-style regression tests — topical-relevance gate. Being domain-safe
# and not-generic-filler is not enough: generic homepage/product sales copy
# must not be promoted as international/L&D evidence just because it was
# tagged under that signal_name, and what_is_hot must never claim a topic
# commercial_fit_drivers can't back up with real evidence (and vice versa).
# ---------------------------------------------------------------------------

_BAUWATCH_HOMEPAGE_COPY = (
    "Protect your site with construction site monitoring, live cameras and "
    "24/7 alerts for theft and vandalism prevention."
)


def _bauwatch_row(**overrides) -> dict:
    row = dict(
        source_index=1,
        company_name="BauWatch",
        domain="bauwatch.com",
        website_url="https://www.bauwatch.com",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="BauWatch Holding",
        c5_parent_hq_country="Germany",
        commercial_fit_score_app=55,
        commercial_tier_app="B",
        industry="Security services",
        employee_range="201-500",
    )
    row.update(overrides)
    return row


def test_generic_homepage_copy_not_accepted_as_ld_evidence(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": _BAUWATCH_HOMEPAGE_COPY},
    ]
    _, out_dir = run_export(tmp_path, [_bauwatch_row()], signals=signals,
                            export_country="Netherlands")

    detail = detail_for(out_dir, "BauWatch")
    ui = detail["ui_payload"]

    joined_hot = " ".join(ui["what_is_hot"])
    assert "construction site monitoring" not in joined_hot
    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    assert drivers_by_label["Learning and development or onboarding needs"]["strength"] == "Rejected"
    assert drivers_by_label["Learning and development or onboarding needs"]["evidence"] == ""


def test_generic_homepage_copy_not_accepted_as_international_evidence(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _BAUWATCH_HOMEPAGE_COPY},
    ]
    _, out_dir = run_export(tmp_path, [_bauwatch_row()], signals=signals,
                            export_country="Netherlands")

    detail = detail_for(out_dir, "BauWatch")
    ui = detail["ui_payload"]

    joined_hot = " ".join(ui["what_is_hot"])
    assert "construction site monitoring" not in joined_hot
    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    assert drivers_by_label["International business context"]["strength"] == "Rejected"
    assert drivers_by_label["International business context"]["evidence"] == ""


def test_unsupported_ld_signal_creates_no_bare_bullet(tmp_path):
    # Same generic copy reused for both signals, as in the real BauWatch case.
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _BAUWATCH_HOMEPAGE_COPY},
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": _BAUWATCH_HOMEPAGE_COPY},
    ]
    _, out_dir = run_export(tmp_path, [_bauwatch_row()], signals=signals,
                            export_country="Netherlands")

    detail = detail_for(out_dir, "BauWatch")
    what_is_hot = detail["ui_payload"]["what_is_hot"]

    assert "Learning and development." not in what_is_hot
    assert "International business context." not in what_is_hot
    assert not any(b.startswith("Learning and development") for b in what_is_hot)
    assert not any(b.startswith("International business context") for b in what_is_hot)


def test_what_is_hot_summary_omits_ld_without_curated_evidence(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": _BAUWATCH_HOMEPAGE_COPY},
    ]
    _, out_dir = run_export(tmp_path, [_bauwatch_row()], signals=signals,
                            export_country="Netherlands")

    detail = detail_for(out_dir, "BauWatch")
    summary_line = detail["ui_payload"]["what_is_hot"][0]
    assert "Learning and development" not in summary_line


def test_what_is_hot_and_commercial_fit_drivers_stay_consistent_for_bauwatch(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _BAUWATCH_HOMEPAGE_COPY},
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": _BAUWATCH_HOMEPAGE_COPY},
    ]
    _, out_dir = run_export(tmp_path, [_bauwatch_row()], signals=signals,
                            export_country="Netherlands")

    detail = detail_for(out_dir, "BauWatch")
    ui = detail["ui_payload"]
    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}

    # Neither field claims international/L&D positively — fully consistent:
    # what_is_hot has no bullet for either topic, and their drivers are
    # Rejected (present, but not a positive claim) rather than Strong/Moderate.
    for topic_label in ("International business context",
                        "Learning and development or onboarding needs"):
        claimed_in_hot = any(
            b.lower().startswith(topic_label.lower()) for b in ui["what_is_hot"])
        driver_is_positive = drivers_by_label[topic_label]["strength"] in (
            "Strong", "Moderate", "Weak")
        assert claimed_in_hot == driver_is_positive
        assert not claimed_in_hot
        assert not driver_is_positive


def test_aldi_style_rich_evidence_is_still_promoted(tmp_path):
    aldi_row = dict(
        source_index=1,
        company_name="ALDI S.R.L.",
        domain="aldi-sued.com",
        website_url="https://www.aldi-sued.com",
        input_country="Italy",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="ALDI SUD",
        c5_parent_hq_country="Germany",
        commercial_fit_score_app=85,
        commercial_tier_app="A",
        industry="Retail",
        employee_range="10001+",
    )
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": (
             "ALDI SUD group operates across 11 countries with 7,300+ stores globally."
         )},
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": (
             "Company website explicitly details a formal training approach "
             "with a Learning Management System and mandatory training courses."
         )},
        {"source_index": 1, "signal_name": "company_size_complexity", "signal_score": 2,
         "evidence_quote": "Store management roles across distributed locations support onboarding."},
    ]
    _, out_dir = run_export(tmp_path, [aldi_row], signals=signals,
                            export_country="Italy", foreign_hq_only=False)

    detail = detail_for(out_dir, "ALDI S.R.L.")
    ui = detail["ui_payload"]

    joined_hot = " ".join(ui["what_is_hot"])
    assert "11 countries" in joined_hot
    assert "Learning Management System" in joined_hot
    assert any(b.startswith("International business context:") for b in ui["what_is_hot"])
    assert any(b.startswith("Learning and development or onboarding needs:")
               for b in ui["what_is_hot"])

    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    assert len(ui["commercial_fit_drivers"]) == 6
    assert drivers_by_label["International business context"]["strength"] == "Strong"
    assert "11 countries" in drivers_by_label["International business context"]["evidence"]
    assert drivers_by_label["Learning and development or onboarding needs"]["strength"] == "Strong"
    assert "Learning Management System" in \
        drivers_by_label["Learning and development or onboarding needs"]["evidence"]


# ---------------------------------------------------------------------------
# IGM Resins-style regression tests — the non-Italy curated display-signal
# layer (why_relevant, what_is_hot, commercial_fit_drivers, and
# cold_caller_summary all built from the same curated signals, per item B
# of the ui_payload content-quality fix).
# ---------------------------------------------------------------------------

_IGM_HOMEPAGE_COPY = (
    "IGM Resins delivers high-performance UV-curable resins and coatings "
    "solutions to customers around the world."
)


def _igm_row(**overrides) -> dict:
    row = dict(
        source_index=1,
        company_name="IGM Resins",
        domain="igmresins.com",
        website_url="https://www.igmresins.com",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=40,
        commercial_tier_app="C",
        industry="Chemicals",
        employee_range="1001-5000",
    )
    row.update(overrides)
    return row


def test_weak_employer_branding_not_shown_as_strong_driver(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
         "evidence_quote": _IGM_HOMEPAGE_COPY},
    ]
    _, out_dir = run_export(tmp_path, [_igm_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "IGM Resins")
    ui = detail["ui_payload"]

    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    driver = drivers_by_label["Employer branding or employee satisfaction"]
    assert driver["strength"] not in ("Strong", "Moderate", "Weak")
    assert driver["evidence"] == ""
    assert "IGM Resins delivers high-performance" not in " ".join(ui["what_is_hot"])


def test_sparse_record_gets_cautious_why_relevant_and_cold_caller_summary(tmp_path):
    _, out_dir = run_export(tmp_path, [_igm_row()],
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "IGM Resins")
    ui = detail["ui_payload"]

    assert ui["why_relevant"] != "IGM Resins is a Netherlands-based company."
    assert "light discovery" in ui["why_relevant"].lower()
    assert "Chemicals" in ui["why_relevant"]

    # No curated signals, no foreign HQ -> only safe factual bullets (or none).
    for bullet in ui["what_is_hot"]:
        assert bullet.startswith("Industry:") or bullet.startswith("Company size:")

    # All six fixed dimensions are still present, all "Not evidenced" —
    # never silently omitted just because the record is sparse.
    assert len(ui["commercial_fit_drivers"]) == 6
    for driver in ui["commercial_fit_drivers"]:
        assert driver["strength"] == "Not evidenced"
        assert driver["evidence"] == ""

    assert ui["cold_caller_summary"] == (
        "Use this as a light discovery lead. The current evidence does not "
        "yet show a clear training trigger, so start by validating whether "
        "international communication, onboarding, or team-development "
        "needs exist.")
    assert ui["cold_caller_summary"] != detail["cold_caller_summary_app"]


def test_legacy_generic_phrases_never_appear_in_ui_payload(tmp_path):
    row = _igm_row(
        cold_caller_summary_app=(
            "Signals point to international operations and onboarding or "
            "training needs. Company size or complexity suggests structured "
            "training coordination may be relevant. Keyword evidence signals "
            "alignment with the target profile for language or training "
            "support."
        ),
    )
    _, out_dir = run_export(tmp_path, [row], export_country="Netherlands",
                            foreign_hq_only=False)

    detail = detail_for(out_dir, "IGM Resins")
    ui = detail["ui_payload"]

    visible_text = " ".join([
        ui["why_relevant"] or "",
        " ".join(ui["what_is_hot"]),
        ui["cold_caller_summary"] or "",
        " ".join(str(d) for d in ui["commercial_fit_drivers"]),
    ])
    assert "Signals point to" not in visible_text
    assert "Keyword evidence signals" not in visible_text
    assert "Company size or complexity suggests" not in visible_text


def test_generic_homepage_copy_rejected_for_all_signal_types(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _IGM_HOMEPAGE_COPY},
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": _IGM_HOMEPAGE_COPY},
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
         "evidence_quote": _IGM_HOMEPAGE_COPY},
    ]
    _, out_dir = run_export(tmp_path, [_igm_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "IGM Resins")
    ui = detail["ui_payload"]

    # All six fixed dimensions are still present — none omitted — but none
    # is a positive claim, since the only evidence offered was generic copy.
    assert len(ui["commercial_fit_drivers"]) == 6
    for driver in ui["commercial_fit_drivers"]:
        assert driver["strength"] not in ("Strong", "Moderate", "Weak")
        assert driver["evidence"] == ""
    joined_hot = " ".join(ui["what_is_hot"])
    assert "IGM Resins delivers high-performance" not in joined_hot
    assert "UV-curable" not in joined_hot


def test_duplicate_official_website_urls_removed(tmp_path):
    evidence = [
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://www.igmresins.com/about-us",
         "source_snippet": "About IGM Resins"},
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://igmresins.com/products",
         "source_snippet": "IGM Resins products"},
    ]
    _, out_dir = run_export(tmp_path, [_igm_row()], evidence,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "IGM Resins")
    source_urls = detail["ui_payload"]["source_urls"]

    official = [item for item in source_urls if item["label"] == "Official website"]
    assert len(official) == 1
    assert official[0]["url"] == "https://www.igmresins.com"  # explicit field wins


def test_italy_legacy_ui_payload_behavior_untouched(tmp_path):
    aldi_row = dict(
        source_index=1,
        company_name="ALDI S.R.L.",
        domain="aldi-sued.com",
        website_url="https://www.aldi-sued.com",
        input_country="Italy",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="ALDI SUD",
        c5_parent_hq_country="Germany",
        commercial_fit_score_app=85,
        commercial_tier_app="A",
        industry="Retail",
        employee_range="10001+",
        cold_caller_summary_app="Legacy Italian cold caller summary text.",
        parent_hq_summary_app="Legacy Italian parent HQ summary text.",
    )
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "Signals point to international alignment."},
    ]
    _, out_dir = run_export(tmp_path, [aldi_row], signals=signals,
                            export_country="Italy", foreign_hq_only=False,
                            content_language="Italian")

    detail = detail_for(out_dir, "ALDI S.R.L.")
    ui = detail["ui_payload"]

    # Italy keeps mirroring cold_caller_summary_app / parent_hq_summary_app
    # verbatim — never rebuilt by the new non-Italy curated layer.
    assert ui["cold_caller_summary"] == "Legacy Italian cold caller summary text."
    assert ui["parent_hq_summary"] == "Legacy Italian parent HQ summary text."
    # Italy keeps the original (pre-curated-layer) commercial_fit_drivers
    # behavior: a positively-scored non-bucketed... here the bucketed
    # international_profile signal has weak evidence ("Signals point to")
    # and is correctly still filtered by the original, unmodified
    # build_commercial_fit_drivers logic (unchanged from before this task).
    driver_labels = {d["label"] for d in ui["commercial_fit_drivers"]}
    assert "International business context" not in driver_labels
    # Italy's commercial_fit_drivers is still the old variable-length shape
    # (no fixed six dimensions, no id/note fields) — only the foreign-HQ
    # driver exists here, since it's the only signal with real evidence.
    assert len(ui["commercial_fit_drivers"]) == 1
    assert "id" not in ui["commercial_fit_drivers"][0]
    assert "note" not in ui["commercial_fit_drivers"][0]


# ---------------------------------------------------------------------------
# AI-composed caller content (Step 3, opt-in — non-Italy only). When
# prioritize_single_lead(..., compose_caller_content_flag=True) succeeded for
# a row, the Enriched Leads sheet carries composed_* fields alongside the
# deterministic *_app templates; the exporter must prefer them wherever
# present and leave the curated-layer value otherwise. Italy stays untouched
# even when composed_* columns happen to be present on the row.
# ---------------------------------------------------------------------------

def test_composed_caller_content_preferred_over_curated_layer_non_italy(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        website_url="https://www.nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="Nordic Gear Group",
        c5_parent_hq_country="Sweden",
        commercial_fit_score_app=70,
        commercial_tier_app="B",
        industry="Manufacturing",
        employee_range="501-1000",
        composed_why_relevant="AI-composed why-relevant text mentioning Nordic Gear AB.",
        composed_what_is_hot="AI bullet one\nAI bullet two",
        composed_cold_caller_summary="AI-composed cold caller summary.",
        composed_caller_angle="AI-composed caller angle.",
        composed_call_starter="Hi, this is an AI-composed call starter.",
        composed_driver_evidence_json=json.dumps(
            {"foreign_hq": "AI evidence sentence for foreign HQ."}),
        composed_by_ai=True,
        composed_content_note="AI-composed caller content used.",
    )
    _, out_dir = run_export(tmp_path, [row],
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Nordic Gear AB")
    ui = detail["ui_payload"]

    assert ui["why_relevant"] == "AI-composed why-relevant text mentioning Nordic Gear AB."
    assert ui["what_is_hot"] == ["AI bullet one", "AI bullet two"]
    assert ui["cold_caller_summary"] == "AI-composed cold caller summary."
    assert ui["caller_angle"] == "AI-composed caller angle."
    assert ui["call_starter"] == "Hi, this is an AI-composed call starter."

    hq_driver = next(
        d for d in ui["commercial_fit_drivers"]
        if d["id"] == "foreign_ownership_or_group_structure")
    assert hq_driver["strength"] == "Strong"
    assert hq_driver["evidence"] == "AI evidence sentence for foreign HQ."

    assert detail["composed_by_ai"] is True
    assert detail["composed_content_note"] == "AI-composed caller content used."


def test_composed_caller_content_partial_fields_keep_curated_layer_for_rest(tmp_path):
    # Only composed_why_relevant is present -> every other field must keep
    # exactly the curated-layer value it would have had without composition.
    base_row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        website_url="https://www.nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="Nordic Gear Group",
        c5_parent_hq_country="Sweden",
        commercial_fit_score_app=70,
        commercial_tier_app="B",
        industry="Manufacturing",
        employee_range="501-1000",
    )
    baseline_dir = tmp_path / "baseline"
    composed_dir = tmp_path / "composed"
    baseline_dir.mkdir()
    composed_dir.mkdir()

    _, baseline_out_dir = run_export(baseline_dir, [base_row],
                                     export_country="Netherlands", foreign_hq_only=False)
    baseline_ui = detail_for(baseline_out_dir, "Nordic Gear AB")["ui_payload"]

    composed_row = dict(base_row, composed_why_relevant="Only this field is AI-composed.")
    _, out_dir = run_export(composed_dir, [composed_row],
                            export_country="Netherlands", foreign_hq_only=False)
    ui = detail_for(out_dir, "Nordic Gear AB")["ui_payload"]

    assert ui["why_relevant"] == "Only this field is AI-composed."
    assert ui["what_is_hot"] == baseline_ui["what_is_hot"]
    assert ui["cold_caller_summary"] == baseline_ui["cold_caller_summary"]
    assert ui["caller_angle"] == baseline_ui["caller_angle"]
    assert ui["call_starter"] == baseline_ui["call_starter"]
    assert ui["commercial_fit_drivers"] == baseline_ui["commercial_fit_drivers"]


def test_composed_caller_content_ignored_for_italian_content_language(tmp_path):
    row = dict(
        source_index=1,
        company_name="ALDI S.R.L.",
        domain="aldi-sued.com",
        website_url="https://www.aldi-sued.com",
        input_country="Italy",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="ALDI SUD",
        c5_parent_hq_country="Germany",
        commercial_fit_score_app=85,
        commercial_tier_app="A",
        industry="Retail",
        employee_range="10001+",
        cold_caller_summary_app="Legacy Italian cold caller summary text.",
        parent_hq_summary_app="Legacy Italian parent HQ summary text.",
        caller_angle_app="Legacy Italian caller angle.",
        call_starter_app="Legacy Italian call starter.",
        # Composed fields present but must never leak into the frozen Italy path.
        composed_why_relevant="Should never appear for Italy.",
        composed_what_is_hot="Should not appear\nEither",
        composed_cold_caller_summary="Should not appear for Italy.",
        composed_caller_angle="Should not appear for Italy.",
        composed_call_starter="Should not appear for Italy.",
        composed_driver_evidence_json=json.dumps(
            {"foreign_hq": "Should not appear for Italy."}),
        composed_by_ai=True,
        composed_content_note="AI-composed caller content used.",
    )
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "Signals point to international alignment."},
    ]
    _, out_dir = run_export(tmp_path, [row], signals=signals,
                            export_country="Italy", foreign_hq_only=False,
                            content_language="Italian")

    detail = detail_for(out_dir, "ALDI S.R.L.")
    ui = detail["ui_payload"]

    # Byte-for-byte the same as the pre-existing Italy legacy behavior.
    assert ui["cold_caller_summary"] == "Legacy Italian cold caller summary text."
    assert ui["parent_hq_summary"] == "Legacy Italian parent HQ summary text."
    assert ui["caller_angle"] == "Legacy Italian caller angle."
    assert ui["call_starter"] == "Legacy Italian call starter."

    visible_text = " ".join([
        ui["why_relevant"] or "", " ".join(ui["what_is_hot"]),
        ui["cold_caller_summary"] or "", ui["caller_angle"] or "",
        ui["call_starter"] or "",
    ])
    assert "Should not appear" not in visible_text
    assert not any("Should not appear" in (d.get("evidence") or "")
                   for d in ui["commercial_fit_drivers"])

    # composed_by_ai / composed_content_note are still copied through as
    # audit metadata even though the Italy ui_payload ignores them.
    assert detail["composed_by_ai"] is True
    assert detail["composed_content_note"] == "AI-composed caller content used."


# ---------------------------------------------------------------------------
# Rich ICP context (opt-in, independent of AI-composed caller content) —
# added as a nested icp_context object only when actually present on the
# row, never touching ui_payload or scoring.
# ---------------------------------------------------------------------------

def test_icp_context_present_when_fields_are_on_the_row(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=50,
        commercial_tier_app="B",
        industry="Manufacturing",
        employee_range="501-1000",
        icp_buying_signals="Active onboarding academy suggests near-term L&D investment.",
        icp_likely_training_interest="Onboarding and new-hire ramp-up.",
        icp_potential_buyer_function="HR / Talent Development",
    )
    _, out_dir = run_export(tmp_path, [row],
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Nordic Gear AB")
    assert detail["icp_context"] == {
        "buying_signals": "Active onboarding academy suggests near-term L&D investment.",
        "likely_training_interest": "Onboarding and new-hire ramp-up.",
        "potential_buyer_function": "HR / Talent Development",
    }
    # Never leaks into the caller-facing ui_payload.
    ui = detail["ui_payload"]
    visible_text = " ".join([ui["why_relevant"] or "", " ".join(ui["what_is_hot"]),
                             ui["cold_caller_summary"] or ""])
    assert "onboarding academy" not in visible_text.lower()


def test_icp_context_absent_when_no_fields_on_the_row(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=50,
        commercial_tier_app="B",
        industry="Manufacturing",
        employee_range="501-1000",
    )
    _, out_dir = run_export(tmp_path, [row],
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Nordic Gear AB")
    assert "icp_context" not in detail


def test_icp_context_partial_fields_only_include_present_ones(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=50,
        commercial_tier_app="B",
        industry="Manufacturing",
        employee_range="501-1000",
        icp_potential_buyer_function="HR / Talent Development",
    )
    _, out_dir = run_export(tmp_path, [row],
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Nordic Gear AB")
    assert detail["icp_context"] == {"potential_buyer_function": "HR / Talent Development"}


def test_icp_context_ignored_for_italian_content_language(tmp_path):
    row = dict(
        source_index=1,
        company_name="ALDI S.R.L.",
        domain="aldi-sued.com",
        input_country="Italy",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="ALDI SUD",
        c5_parent_hq_country="Germany",
        commercial_fit_score_app=85,
        commercial_tier_app="A",
        industry="Retail",
        employee_range="10001+",
        cold_caller_summary_app="Legacy Italian cold caller summary text.",
        icp_buying_signals="Should still be exposed as icp_context for Italy too.",
    )
    _, out_dir = run_export(tmp_path, [row],
                            export_country="Italy", foreign_hq_only=False,
                            content_language="Italian")

    detail = detail_for(out_dir, "ALDI S.R.L.")
    # icp_context is built independently of the Italy/non-Italy ui_payload
    # branch, so it is present regardless of content_language — but it never
    # touches the frozen Italy ui_payload fields themselves.
    assert detail["icp_context"] == {
        "buying_signals": "Should still be exposed as icp_context for Italy too.",
    }
    assert detail["ui_payload"]["cold_caller_summary"] == "Legacy Italian cold caller summary text."


# ---------------------------------------------------------------------------
# Deep Dive (Step B, opt-in, independent of AI-composed caller content and
# rich ICP context) — a nested deep_dive object built from the optional
# "Deep Dive" sheet, present only when it has at least one claim row for the
# company, never touching ui_payload/icp_context/scoring.
# ---------------------------------------------------------------------------

def test_deep_dive_present_when_sheet_has_claims(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=90,
        commercial_tier_app="A",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    deep_dive_rows = [
        {"source_index": 1, "company_name": "Nordic Gear AB",
         "trigger_reason": "score_threshold", "category": "hq_structure",
         "statement": "Nordic Gear AB is headquartered in Stockholm.",
         "quote": "headquartered in Stockholm", "source_url": "https://nordicgear.se/about",
         "source_kind": "own_domain", "domain_verified": True,
         "retrieval_method": "firecrawl", "error": ""},
        {"source_index": 1, "company_name": "Nordic Gear AB",
         "trigger_reason": "score_threshold", "category": "workforce",
         "statement": "Nordic Gear AB employs over 1000 people.",
         "quote": "over 1000 employees", "source_url": "https://nordicgear.se/careers",
         "source_kind": "own_domain", "domain_verified": True,
         "retrieval_method": "firecrawl", "error": ""},
    ]
    _, out_dir = run_export(tmp_path, [row], deep_dive=deep_dive_rows,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Nordic Gear AB")
    assert detail["deep_dive"]["trigger_reason"] == "score_threshold"
    assert len(detail["deep_dive"]["claims"]) == 2
    categories = {c["category"] for c in detail["deep_dive"]["claims"]}
    assert categories == {"hq_structure", "workforce"}
    for claim in detail["deep_dive"]["claims"]:
        assert claim["domain_verified"] is True
        assert claim["source_kind"] == "own_domain"

    # Never leaks into the caller-facing ui_payload or icp_context.
    ui = detail["ui_payload"]
    visible_text = " ".join([ui["why_relevant"] or "", " ".join(ui["what_is_hot"]),
                             ui["cold_caller_summary"] or ""])
    assert "Stockholm" not in visible_text
    assert "icp_context" not in detail


def test_deep_dive_claim_quote_verification_fields_and_badge(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=90,
        commercial_tier_app="A",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    deep_dive_rows = [
        {"source_index": 1, "company_name": "Nordic Gear AB",
         "trigger_reason": "score_threshold", "category": "hq_structure",
         "statement": "s1", "quote": "the real page text",
         "source_url": "https://nordicgear.se/about", "source_kind": "own_domain",
         "domain_verified": True, "retrieval_method": "firecrawl", "error": "",
         "quote_verified": True, "quote_verification_status": "verified_corrected",
         "quote_match_score": 0.91, "original_quote": "the AI's paraphrase"},
        {"source_index": 1, "company_name": "Nordic Gear AB",
         "trigger_reason": "score_threshold", "category": "workforce",
         "statement": "s2", "quote": "not actually on the page",
         "source_url": "https://nordicgear.se/careers", "source_kind": "own_domain",
         "domain_verified": True, "retrieval_method": "firecrawl", "error": "",
         "quote_verified": False, "quote_verification_status": "not_found",
         "quote_match_score": 0.31, "original_quote": ""},
    ]
    _, out_dir = run_export(tmp_path, [row], deep_dive=deep_dive_rows,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Nordic Gear AB")
    claims_by_category = {c["category"]: c for c in detail["deep_dive"]["claims"]}

    corrected = claims_by_category["hq_structure"]
    assert corrected["quote_verified"] is True
    assert corrected["quote_verification_status"] == "verified_corrected"
    assert corrected["quote_match_score"] == pytest.approx(0.91)
    assert corrected["original_quote"] == "the AI's paraphrase"
    assert corrected["badge"] == "confirmed"

    not_found = claims_by_category["workforce"]
    assert not_found["quote_verified"] is False
    assert not_found["quote_verification_status"] == "not_found"
    assert not_found["original_quote"] is None
    assert not_found["badge"] == "unconfirmed"
    # not_found claims are shown, never dropped -- a trust signal, not a filter.
    assert len(detail["deep_dive"]["claims"]) == 2


def test_deep_dive_claim_missing_quote_verification_fields_default_safely(tmp_path):
    # A claim row written before these fields existed (or with verify_quotes
    # off, leaving them at schema defaults) must still export safely.
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=90,
        commercial_tier_app="A",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    deep_dive_rows = [
        {"source_index": 1, "company_name": "Nordic Gear AB",
         "trigger_reason": "score_threshold", "category": "hq_structure",
         "statement": "s1", "quote": "some quote", "source_url": "https://nordicgear.se/about",
         "source_kind": "own_domain", "domain_verified": True,
         "retrieval_method": "firecrawl", "error": ""},
    ]
    _, out_dir = run_export(tmp_path, [row], deep_dive=deep_dive_rows,
                            export_country="Netherlands", foreign_hq_only=False)
    detail = detail_for(out_dir, "Nordic Gear AB")
    claim = detail["deep_dive"]["claims"][0]
    assert claim["quote_verified"] is False
    assert claim["quote_verification_status"] == "not_checked"
    assert claim["quote_match_score"] == 0.0
    assert claim["original_quote"] is None
    assert claim["badge"] == "unconfirmed"


def test_deep_dive_absent_when_sheet_missing(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=90,
        commercial_tier_app="A",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    _, out_dir = run_export(tmp_path, [row],
                            export_country="Netherlands", foreign_hq_only=False)
    detail = detail_for(out_dir, "Nordic Gear AB")
    assert "deep_dive" not in detail


def test_deep_dive_absent_when_sheet_present_but_empty_or_unrelated_row(tmp_path):
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=90,
        commercial_tier_app="A",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    # A claim row for a DIFFERENT source_index must never attach to this company.
    deep_dive_rows = [
        {"source_index": 99, "company_name": "Someone Else",
         "trigger_reason": "score_threshold", "category": "hq_structure",
         "statement": "s", "quote": "q", "source_url": "https://else.com",
         "source_kind": "own_domain", "domain_verified": True,
         "retrieval_method": "firecrawl", "error": ""},
    ]
    _, out_dir = run_export(tmp_path, [row], deep_dive=deep_dive_rows,
                            export_country="Netherlands", foreign_hq_only=False)
    detail = detail_for(out_dir, "Nordic Gear AB")
    assert "deep_dive" not in detail


def test_deep_dive_error_only_row_produces_no_claims_and_no_field(tmp_path):
    # A row with error set but no actual claim columns filled (company-level
    # failure with zero claims) must not fabricate a deep_dive object.
    row = dict(
        source_index=1,
        company_name="Nordic Gear AB",
        domain="nordicgear.se",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=90,
        commercial_tier_app="A",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    _, out_dir = run_export(tmp_path, [row], deep_dive=[],
                            export_country="Netherlands", foreign_hq_only=False)
    detail = detail_for(out_dir, "Nordic Gear AB")
    assert "deep_dive" not in detail


def test_deep_dive_ignored_for_italian_content_language_but_still_present(tmp_path):
    row = dict(
        source_index=1,
        company_name="ALDI S.R.L.",
        domain="aldi-sued.com",
        input_country="Italy",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="ALDI SUD",
        c5_parent_hq_country="Germany",
        commercial_fit_score_app=85,
        commercial_tier_app="A",
        industry="Retail",
        employee_range="10001+",
        cold_caller_summary_app="Legacy Italian cold caller summary text.",
    )
    deep_dive_rows = [
        {"source_index": 1, "company_name": "ALDI S.R.L.",
         "trigger_reason": "foreign_hq", "category": "hq_structure",
         "statement": "s", "quote": "q", "source_url": "https://aldi-sued.com/about",
         "source_kind": "own_domain", "domain_verified": True,
         "retrieval_method": "firecrawl", "error": ""},
    ]
    _, out_dir = run_export(tmp_path, [row], deep_dive=deep_dive_rows,
                            export_country="Italy", foreign_hq_only=False,
                            content_language="Italian")
    detail = detail_for(out_dir, "ALDI S.R.L.")
    # deep_dive is built independently of the Italy/non-Italy ui_payload
    # branch, so it is present regardless of content_language — but the
    # frozen Italy ui_payload fields stay untouched.
    assert detail["deep_dive"]["trigger_reason"] == "foreign_hq"
    assert len(detail["deep_dive"]["claims"]) == 1
    assert detail["ui_payload"]["cold_caller_summary"] == "Legacy Italian cold caller summary text."


# ---------------------------------------------------------------------------
# Six fixed commercial_fit_drivers dimensions (non-Italy) — always present,
# in this exact order, never silently omitted.
# ---------------------------------------------------------------------------

_FIXED_DRIVER_LABELS_IN_ORDER = (
    "Foreign ownership or group structure",
    "International business context",
    "Explicit learning and development",
    "Learning and development or onboarding needs",
    "Possible onboarding need",
    "Employer branding or employee satisfaction",
)


def test_commercial_fit_drivers_always_six_fixed_dimensions_in_order(tmp_path):
    _, out_dir = run_export(tmp_path, [_igm_row()],
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "IGM Resins")
    drivers = detail["ui_payload"]["commercial_fit_drivers"]

    assert [d["label"] for d in drivers] == list(_FIXED_DRIVER_LABELS_IN_ORDER)
    assert [d["id"] for d in drivers] == [
        "foreign_ownership_or_group_structure",
        "international_business_context",
        "explicit_learning_and_development",
        "learning_and_development_or_onboarding_needs",
        "possible_onboarding_need",
        "employer_branding_or_employee_satisfaction",
    ]
    for d in drivers:
        assert d["strength"] == "Not evidenced"
        assert d["evidence"] == ""
        assert d["note"] == "No reliable company-specific evidence found in the current sources."


# ---------------------------------------------------------------------------
# Shimano-style regression test — Workday-hosted careers domain must never
# be treated as official website, parent company, or sector evidence, and a
# generic Glassdoor list-page snippet must not become a positive employer-
# branding claim.
# ---------------------------------------------------------------------------

def test_shimano_workday_hosted_case(tmp_path):
    shimano_row = dict(
        source_index=1,
        company_name="Shimano Europe Group",
        domain="shimano.wd3.myworkdayjobs.com",
        website_url="https://shimano.wd3.myworkdayjobs.com/en-US/Shimano_Careers",
        careers_url="https://shimano.wd3.myworkdayjobs.com/en-US/Shimano_Careers",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="Shimano Inc.",
        c5_parent_hq_country="Japan",
        # Simulates the old contaminated legacy field the new non-Italy
        # parent_hq_summary must NOT copy.
        parent_hq_summary_app=(
            "The enrichment data identifies Workday as the parent company, "
            "with HQ context in United States / Santa Clara."
        ),
        commercial_fit_score_app=60,
        commercial_tier_app="B",
        industry="",
        detected_industry="Financial services",
        sector_evidence_url="https://shimano.wd3.myworkdayjobs.com/some-posting",
        employee_range="10001+",
    )
    signals = [
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
         "evidence_quote": "Best Places to Work 2026 is now live. Discover top-rated workplaces this year.",
         "evidence_url": "https://www.glassdoor.com/Best-Places-to-Work-LST_KQ0,25.htm"},
    ]
    evidence = [
        {"source_index": 1, "signal_name": "employer_branding",
         "source_url": "https://www.glassdoor.com/Best-Places-to-Work-LST_KQ0,25.htm",
         "source_title": "Best Places to Work 2026",
         "source_snippet": "Best Places to Work 2026 is now live."},
    ]
    _, out_dir = run_export(tmp_path, [shimano_row], evidence, signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Shimano Europe Group")
    ui = detail["ui_payload"]

    # Workday is never the official website / careers page label.
    source_urls = ui["source_urls"]
    assert not any(item["label"] == "Official website" for item in source_urls)
    workday_entries = [item for item in source_urls if "myworkdayjobs.com" in item["url"]]
    assert workday_entries
    assert all(item["label"] == "Careers platform" for item in workday_entries)

    # parent_hq_summary uses the clean Shimano/Japan fields, never Workday.
    assert ui["parent_hq_summary"] is not None
    assert "Workday" not in ui["parent_hq_summary"]
    assert "United States" not in ui["parent_hq_summary"]
    assert "Shimano Inc." in ui["parent_hq_summary"]
    assert "Japan" in ui["parent_hq_summary"]

    # Generic Glassdoor list-page text is not positive employer-branding
    # evidence, but is no longer hidden: it shows as a Weak driver with its
    # external source link (account manager judges) — never as clean evidence
    # and never promoted into positive caller-facing text (asserted below).
    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    eb_driver = drivers_by_label["Employer branding or employee satisfaction"]
    assert eb_driver["strength"] == "Weak"
    assert eb_driver["evidence"] == ""
    assert eb_driver["note"]
    assert eb_driver["source_scope"] == "external"
    assert eb_driver["evidence_source_url"] == \
        "https://www.glassdoor.com/Best-Places-to-Work-LST_KQ0,25.htm"

    visible_text = " ".join([
        ui["why_relevant"] or "", " ".join(ui["what_is_hot"]),
        ui["cold_caller_summary"] or "",
    ])
    assert "Best Places to Work" not in visible_text
    assert "Workday" not in visible_text

    # Industry is not confidently set from vendor/Workday-sourced sector evidence.
    assert "Financial services" not in (ui["why_relevant"] or "")
    assert not any("Financial services" in b for b in ui["what_is_hot"])


# ---------------------------------------------------------------------------
# Samsung-style regression test — external installer/product/partner
# training must not be accepted as internal employee L&D/onboarding evidence.
# ---------------------------------------------------------------------------

def test_samsung_external_installer_training_case(tmp_path):
    samsung_row = dict(
        source_index=1,
        company_name="Samsung Electronics Air Conditioner Europe",
        domain="samsung.com",
        website_url="https://www.samsung.com",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=45,
        commercial_tier_app="C",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    signals = [
        {"source_index": 1, "signal_name": "onboarding_training_need", "signal_score": 2,
         "evidence_quote": (
             "Find out how to become a Samsung heat pump and air "
             "conditioning installer in the UK. Get the training you need "
             "to become a climate solutions partner."
         )},
    ]
    _, out_dir = run_export(tmp_path, [samsung_row], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Samsung Electronics Air Conditioner Europe")
    ui = detail["ui_payload"]

    drivers_by_label = {d["label"]: d for d in ui["commercial_fit_drivers"]}
    driver = drivers_by_label["Learning and development or onboarding needs"]
    assert driver["strength"] == "Rejected"
    assert driver["evidence"] == ""
    assert "installer" in driver["note"].lower() or "partner" in driver["note"].lower()

    visible_text = " ".join([
        ui["why_relevant"] or "", " ".join(ui["what_is_hot"]),
        ui["cold_caller_summary"] or "",
    ])
    assert "installer" not in visible_text.lower()
    assert "climate solutions partner" not in visible_text.lower()


# ---------------------------------------------------------------------------
# Generic third-party directory text (Glassdoor/PitchBook/ZoomInfo-style)
# must mention the lead company to be accepted.
# ---------------------------------------------------------------------------

def test_generic_third_party_directory_text_requires_company_mention(tmp_path):
    (tmp_path / "a").mkdir()
    signals_without_mention = [
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
         "evidence_quote": "Explore workplace culture rankings and employer branding trends for 2026.",
         "evidence_url": "https://www.pitchbook.com/profiles/company/best-workplaces-2026"},
    ]
    _, out_a = run_export(tmp_path / "a", [_igm_row()], signals=signals_without_mention,
                          export_country="Netherlands", foreign_hq_only=False)
    detail_a = detail_for(out_a, "IGM Resins")
    drivers_a = {d["label"]: d for d in detail_a["ui_payload"]["commercial_fit_drivers"]}
    # Generic directory text that does not mention the company is no longer
    # hidden: it shows as a Weak driver (external source) for manual review,
    # but is never promoted into positive caller-facing text.
    eb_a = drivers_a["Employer branding or employee satisfaction"]
    assert eb_a["strength"] == "Weak"
    assert eb_a["source_scope"] == "external"
    joined_a = " ".join([
        detail_a["ui_payload"]["why_relevant"] or "",
        " ".join(detail_a["ui_payload"]["what_is_hot"]),
    ])
    assert "workplace culture rankings" not in joined_a

    signals_with_mention = [
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
         "evidence_quote": "IGM Resins employer branding and workplace culture are highly rated.",
         "evidence_url": "https://www.pitchbook.com/profiles/company/igm-resins"},
    ]
    (tmp_path / "b").mkdir()
    _, out_b = run_export(tmp_path / "b", [_igm_row()], signals=signals_with_mention,
                          export_country="Netherlands", foreign_hq_only=False)
    detail_b = detail_for(out_b, "IGM Resins")
    drivers_b = {d["label"]: d for d in detail_b["ui_payload"]["commercial_fit_drivers"]}
    # Company-mentioning directory evidence is still promotable to a positive claim.
    assert drivers_b["Employer branding or employee satisfaction"]["strength"] in (
        "Strong", "Moderate")


# ---------------------------------------------------------------------------
# Source URL dedupe / labeling — duplicate official website and duplicate
# Glassdoor entries removed, hosted job platform never "Official website",
# parent company source labeled distinctly.
# ---------------------------------------------------------------------------

def test_source_urls_dedupe_and_parent_company_label(tmp_path):
    row = dict(
        source_index=1,
        company_name="Samsung Electronics Air Conditioner Europe",
        domain="samsung.com",
        website_url="https://www.samsung.com",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="Samsung Electronics",
        c5_parent_hq_country="South Korea",
        commercial_fit_score_app=50,
        commercial_tier_app="C",
        industry="Manufacturing",
        employee_range="1001-5000",
    )
    evidence = [
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "http://samsung.com", "source_snippet": "dup of website"},
        {"source_index": 1, "signal_name": "employer_branding",
         "source_url": "https://www.glassdoor.com/Overview/Working-at-Samsung.htm",
         "source_title": "Samsung Glassdoor profile",
         "source_snippet": "Samsung employee reviews and ratings."},
        {"source_index": 1, "signal_name": "employer_branding",
         "source_url": "https://glassdoor.com/Overview/Working-at-Samsung.htm",
         "source_title": "Samsung Glassdoor profile (dup)",
         "source_snippet": "Samsung employee reviews and ratings."},
        {"source_index": 1, "signal_name": "international_profile",
         "source_url": "https://www.samsung.com/global/about-us/",
         "source_snippet": "About the Samsung group, our parent HQ."},
    ]
    _, out_dir = run_export(tmp_path, [row], evidence,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Samsung Electronics Air Conditioner Europe")
    source_urls = detail["ui_payload"]["source_urls"]

    official = [item for item in source_urls if item["label"] == "Official website"]
    assert len(official) == 1

    glassdoor_entries = [item for item in source_urls if "glassdoor.com" in item["url"]]
    assert len(glassdoor_entries) == 1


def test_hosted_job_platform_not_labeled_official_website(tmp_path):
    row = dict(
        source_index=1,
        company_name="Shimano Europe Group",
        domain="shimano.wd3.myworkdayjobs.com",
        website_url="https://shimano.wd3.myworkdayjobs.com/en-US/Shimano_Careers",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=40,
        commercial_tier_app="C",
        industry="Manufacturing",
        employee_range="10001+",
    )
    _, out_dir = run_export(tmp_path, [row],
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Shimano Europe Group")
    source_urls = detail["ui_payload"]["source_urls"]

    assert not any(item["label"] == "Official website" for item in source_urls)
    assert any(item["label"] == "Careers platform" for item in source_urls)


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
# Onderdeel 1: multiple clickable evidence_urls -> evidence_sources[] on
# commercial_fit_drivers and what_is_hot_items, and score/backward-compat
# invariance. evidence_urls arrives on the Signals sheet as the same
# semicolon-joined string format used everywhere else in this exporter
# (mirroring flatten_signals_for_excel's "evidence_urls" column).
# ---------------------------------------------------------------------------

_ACME_INTL_QUOTE = "Acme Global operates across 11 countries with 40+ offices worldwide."


def _acme_row(**overrides) -> dict:
    row = dict(
        source_index=1,
        company_name="Acme Global BV",
        domain="acmeglobal.com",
        website_url="https://www.acmeglobal.com",
        input_country="Netherlands",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=0,
        commercial_fit_score_app=50,
        commercial_tier_app="C",
        industry="Manufacturing",
        employee_range="201-500",
    )
    row.update(overrides)
    return row


def test_commercial_fit_driver_evidence_sources_ordered_deduped_and_own_domain_first(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _ACME_INTL_QUOTE,
         "evidence_url": "https://www.acmeglobal.com/about",
         "evidence_urls": (
             "https://www.acmeglobal.com/about; "
             "https://en.wikipedia.org/wiki/Acme_Global; "
             "https://www.acmeglobal.com/about"
         )},
    ]
    _, out_dir = run_export(tmp_path, [_acme_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Acme Global BV")
    drivers = {d["id"]: d for d in detail["ui_payload"]["commercial_fit_drivers"]}
    driver = drivers["international_business_context"]

    sources = driver["evidence_sources"]
    urls = [s["url"] for s in sources]
    assert urls == [
        "https://www.acmeglobal.com/about",
        "https://en.wikipedia.org/wiki/Acme_Global",
    ]
    assert sources[0]["domain"] == "acmeglobal.com"
    assert sources[1]["domain"] == "wikipedia.org"


def test_commercial_fit_driver_evidence_source_singular_equals_first_of_array(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _ACME_INTL_QUOTE,
         "evidence_url": "https://www.acmeglobal.com/about",
         "evidence_urls": (
             "https://www.acmeglobal.com/about; https://en.wikipedia.org/wiki/Acme_Global"
         )},
    ]
    _, out_dir = run_export(tmp_path, [_acme_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Acme Global BV")
    drivers = {d["id"]: d for d in detail["ui_payload"]["commercial_fit_drivers"]}
    driver = drivers["international_business_context"]

    assert driver["evidence_source_url"] == driver["evidence_sources"][0]["url"]
    assert driver["evidence_source_domain"] == driver["evidence_sources"][0]["domain"]


def test_commercial_fit_driver_evidence_sources_excludes_hosted_platform_and_caps_at_five(tmp_path):
    other_domains = "; ".join(
        f"https://example{i}.com/page" for i in range(1, 6)
    )
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _ACME_INTL_QUOTE,
         "evidence_url": "https://www.acmeglobal.com/about",
         "evidence_urls": (
             "https://www.acmeglobal.com/about; "
             "https://acmeglobal.wd3.myworkdayjobs.com/en-US/jobs; "
             f"{other_domains}"
         )},
    ]
    _, out_dir = run_export(tmp_path, [_acme_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Acme Global BV")
    drivers = {d["id"]: d for d in detail["ui_payload"]["commercial_fit_drivers"]}
    sources = drivers["international_business_context"]["evidence_sources"]

    assert not any("myworkdayjobs.com" in s["url"] for s in sources)
    assert len(sources) == 5


def test_commercial_fit_driver_no_evidence_sources_key_when_all_urls_filtered_out(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _ACME_INTL_QUOTE,
         "evidence_url": "",
         "evidence_urls": "https://acmeglobal.wd3.myworkdayjobs.com/en-US/jobs"},
    ]
    _, out_dir = run_export(tmp_path, [_acme_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Acme Global BV")
    drivers = {d["id"]: d for d in detail["ui_payload"]["commercial_fit_drivers"]}
    driver = drivers["international_business_context"]

    assert "evidence_sources" not in driver
    assert "evidence_source_url" not in driver
    # The driver itself is untouched: still Strong with its evidence text.
    assert driver["strength"] == "Strong"
    assert "11 countries" in driver["evidence"]


def test_what_is_hot_items_parallel_to_what_is_hot_with_evidence_sources(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": _ACME_INTL_QUOTE,
         "evidence_url": "https://www.acmeglobal.com/about",
         "evidence_urls": (
             "https://www.acmeglobal.com/about; https://en.wikipedia.org/wiki/Acme_Global"
         )},
    ]
    _, out_dir = run_export(tmp_path, [_acme_row()], signals=signals,
                            export_country="Netherlands", foreign_hq_only=False)

    detail = detail_for(out_dir, "Acme Global BV")
    ui = detail["ui_payload"]

    assert [item["text"] for item in ui["what_is_hot_items"]] == ui["what_is_hot"]
    matching = [item for item in ui["what_is_hot_items"]
                if item["text"].startswith("International business context:")]
    assert len(matching) == 1
    sources = matching[0]["evidence_sources"]
    assert [s["url"] for s in sources] == [
        "https://www.acmeglobal.com/about",
        "https://en.wikipedia.org/wiki/Acme_Global",
    ]


def test_what_is_hot_items_absent_for_italy(tmp_path):
    aldi_row = dict(
        source_index=1,
        company_name="ALDI S.R.L.",
        domain="aldi-sued.com",
        website_url="https://www.aldi-sued.com",
        input_country="Italy",
        enrichment_skipped=False,
        sig_foreign_hq_score_for_next_scoring=3,
        c5_parent_company="ALDI SUD",
        c5_parent_hq_country="Germany",
        commercial_fit_score_app=85,
        commercial_tier_app="A",
        industry="Retail",
        employee_range="10001+",
    )
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "ALDI SUD group operates across 11 countries with 7,300+ stores."},
    ]
    _, out_dir = run_export(tmp_path, [aldi_row], signals=signals,
                            export_country="Italy", foreign_hq_only=False,
                            content_language="Italian")

    detail = detail_for(out_dir, "ALDI S.R.L.")
    assert "what_is_hot_items" not in detail["ui_payload"]


def test_evidence_urls_field_does_not_change_score_or_existing_fields(tmp_path):
    row = _acme_row()
    signal_base = {"source_index": 1, "signal_name": "international_profile",
                   "signal_score": 2, "evidence_quote": _ACME_INTL_QUOTE,
                   "evidence_url": "https://www.acmeglobal.com/about"}

    without_dir = tmp_path / "without"
    with_dir = tmp_path / "with"
    without_dir.mkdir()
    with_dir.mkdir()

    _, out_dir_without = run_export(without_dir, [row], signals=[dict(signal_base)],
                                    export_country="Netherlands", foreign_hq_only=False)
    signal_with_urls = dict(signal_base)
    signal_with_urls["evidence_urls"] = (
        "https://www.acmeglobal.com/about; https://en.wikipedia.org/wiki/Acme_Global"
    )
    _, out_dir_with = run_export(with_dir, [row], signals=[signal_with_urls],
                                 export_country="Netherlands", foreign_hq_only=False)

    list_without = load_list(out_dir_without)[0]
    list_with = load_list(out_dir_with)[0]
    assert list_without["commercial_fit_score_app"] == list_with["commercial_fit_score_app"]
    assert list_without["commercial_tier_app"] == list_with["commercial_tier_app"]

    detail_without = detail_for(out_dir_without, "Acme Global BV")
    detail_with = detail_for(out_dir_with, "Acme Global BV")
    drivers_without = {d["id"]: d for d in detail_without["ui_payload"]["commercial_fit_drivers"]}
    drivers_with = {d["id"]: d for d in detail_with["ui_payload"]["commercial_fit_drivers"]}
    for driver_id in drivers_without:
        d_without = drivers_without[driver_id]
        d_with = drivers_with[driver_id]
        assert d_without["strength"] == d_with["strength"]
        assert d_without["evidence"] == d_with["evidence"]
        assert d_without["note"] == d_with["note"]
    assert detail_without["ui_payload"]["what_is_hot"] == detail_with["ui_payload"]["what_is_hot"]


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


# ---------------------------------------------------------------------------
# Optional Dutch content localization (demo only)
# ---------------------------------------------------------------------------

class TestNormalizeContentLanguage:
    def test_recognizes_english_dutch_and_italian_case_insensitively(self):
        assert normalize_content_language("English") == "English"
        assert normalize_content_language("english") == "English"
        assert normalize_content_language("Dutch") == "Dutch"
        assert normalize_content_language("dutch") == "Dutch"
        assert normalize_content_language("  Dutch  ") == "Dutch"
        assert normalize_content_language("Italian") == "Italian"
        assert normalize_content_language("italian") == "Italian"
        assert normalize_content_language("  Italian  ") == "Italian"

    def test_unknown_or_blank_falls_back_to_english(self):
        assert normalize_content_language("") == "English"
        assert normalize_content_language(None) == "English"
        assert normalize_content_language("French") == "English"


class TestShouldLocalizeContent:
    def test_true_for_dutch_and_italian(self):
        assert should_localize_content("Dutch") is True
        assert should_localize_content("dutch") is True
        assert should_localize_content("Italian") is True
        assert should_localize_content("italian") is True
        assert should_localize_content("English") is False
        assert should_localize_content("") is False
        assert should_localize_content(None) is False


class TestTranslateKnownLabel:
    def test_known_label_translated(self):
        assert translate_known_label(
            "Employer branding or employee satisfaction") == (
            "Employer branding of medewerkerstevredenheid")
        assert translate_known_label(FOREIGN_HQ_SIGNAL_LABEL) == (
            "Buitenlands hoofdkantoor of groepsstructuur")

    def test_unknown_label_left_unchanged(self):
        assert translate_known_label("Some custom label") == "Some custom label"

    def test_blank_passes_through(self):
        assert translate_known_label("") == ""
        assert translate_known_label(None) is None


class TestTranslateKnownLabelIt:
    def test_known_label_translated(self):
        assert translate_known_label_it(
            "Employer branding or employee satisfaction") == (
            "Employer branding o soddisfazione dei dipendenti")
        assert translate_known_label_it(FOREIGN_HQ_SIGNAL_LABEL) == (
            "Sede centrale estera o struttura di gruppo")

    def test_unknown_label_left_unchanged(self):
        assert translate_known_label_it("Some custom label") == "Some custom label"

    def test_blank_passes_through(self):
        assert translate_known_label_it("") == ""
        assert translate_known_label_it(None) is None


# ---------------------------------------------------------------------------
# Whole-template rebuilders (lovable_content_localization) — each function
# matches a *complete* known English sentence built by
# lead_caller_app_fields_builder.py / lead_app_summary_builder.py and rebuilds
# it as a complete Dutch sentence; nothing is patched fragment-by-fragment,
# so there is no risk of the old bug's mixed-language output (e.g. "The
# hoofdkantoor evidence source...").
# ---------------------------------------------------------------------------

class TestLocalizeWhyRelevantApp:
    def test_foreign_hq_with_signal_rebuilt_fully_in_dutch(self):
        text = (
            "Acme Brasil is relevant because it combines a foreign-parent or "
            "international group signal with evidence of international "
            "operations, onboarding, training, or company complexity. That "
            "makes it a practical target for a first conversation about "
            "language, communication, or training support for Brazil-based "
            "teams.")
        result = localize_why_relevant_app(text)
        assert result == (
            "Acme Brasil is relevant omdat het een signaal van een "
            "buitenlandse moeder- of hoofdkantoororganisatie combineert met "
            "bewijs van internationale activiteiten, onboarding, training of "
            "bedrijfscomplexiteit. Dat maakt het een praktisch "
            "aanknopingspunt voor een eerste gesprek over taal, communicatie "
            "of trainingsondersteuning voor in Brazil gevestigde teams.")
        assert "is relevant because" not in result
        assert "The hoofdkantoor" not in result

    def test_unmatched_custom_text_left_in_english(self):
        text = "Some custom analyst note that no template produced."
        assert localize_why_relevant_app(text) == text


class TestLocalizeWhatIsHotApp:
    def test_known_item_rebuilt_fully_in_dutch(self):
        item = (
            "Foreign-parent context gives a clear reason to discuss "
            "cross-border communication and team alignment.")
        result = localize_what_is_hot_item(item)
        assert "Foreign-parent context" not in result
        assert result == (
            "Buitenlandse moedercontext geeft een duidelijke reden om "
            "grensoverschrijdende communicatie en teamafstemming te "
            "bespreken.")

    def test_unknown_item_left_in_english(self):
        assert localize_what_is_hot_item("A custom hot item.") == "A custom hot item."


class TestLocalizeCallerAngleApp:
    def test_foreign_hq_variant_rebuilt_fully_in_dutch(self):
        text = (
            "Open around how the Brazil team stays aligned with "
            "international business expectations, especially in "
            "customer-facing, sales, service, onboarding, or internal "
            "communication roles.")
        result = localize_caller_angle_app(text)
        assert "Open around" not in result
        assert result == (
            "Open het gesprek met hoe het Brazil-team aansluiting houdt bij "
            "internationale zakelijke verwachtingen, vooral in klantgerichte "
            "functies, sales, service, onboarding of interne communicatie.")

    def test_fixed_fallback_variant_translated(self):
        text = (
            "Use a light discovery angle: ask a few open questions to "
            "validate whether international training or communication "
            "needs exist before proposing anything specific.")
        assert "Open around" not in localize_caller_angle_app(text)


class TestLocalizeCallStarterApp:
    def test_foreign_hq_variant_rebuilt_fully_in_dutch(self):
        text = (
            "I saw that Acme Brasil appears to operate in Brazil within a "
            "wider international group context. I was wondering how you "
            "currently support teams that need to work across local "
            "priorities and international expectations.")
        result = localize_call_starter_app(text)
        assert "I saw that" not in result
        assert result == (
            "Ik zag dat Acme Brasil in Brazil lijkt te opereren binnen een "
            "bredere internationale groepscontext. Ik vroeg me af hoe jullie "
            "momenteel teams ondersteunen die moeten schakelen tussen lokale "
            "prioriteiten en internationale verwachtingen.")


class TestLocalizeCautionApp:
    def test_domain_mismatch_item_rebuilt_fully_in_dutch(self):
        text = (
            "Manual review recommended before outreach.; The HQ evidence "
            "source does not clearly match the lead's own domain; verify "
            "the HQ signal before relying on it.")
        result = localize_caution_app(text)
        assert "The HQ evidence source" not in result
        assert "The hoofdkantoor" not in result
        assert result == (
            "Handmatige controle aanbevolen vóór contactopname.; De bron van "
            "het hoofdkantoorbewijs komt niet duidelijk overeen met het "
            "domein van de lead zelf; controleer het signaal voordat je erop "
            "vertrouwt.")


class TestLocalizeParentHqSummaryApp:
    def test_parent_and_location_rebuilt_fully_in_dutch(self):
        text = (
            "The enrichment data identifies Foreign Group as the parent "
            "company, with HQ context in Germany / Munich.")
        result = localize_parent_hq_summary_app(text)
        assert result == (
            "De verrijkte data identificeert Foreign Group als het "
            "moederbedrijf, met hoofdkantoorcontext in Germany / Munich.")
        assert "The enrichment data" not in result


class TestLocalizeColdCallerSummaryApp:
    def test_foreign_hq_composite_rebuilt_fully_in_dutch(self):
        text = (
            "The company appears to be a Brazil-based operation connected "
            "to a foreign parent or HQ context in Germany. This creates a "
            "concrete reason to explore cross-border communication, "
            "onboarding, and alignment with international group "
            "expectations. Open around how the Brazil team stays aligned "
            "with international business expectations, especially in "
            "customer-facing, sales, service, onboarding, or internal "
            "communication roles.")
        result = localize_cold_caller_summary_app(text)
        assert "The company appears to be" not in result
        assert "Open around" not in result
        assert result == (
            "Het bedrijf lijkt een in Brazil gevestigde activiteit te zijn "
            "die verbonden is met een buitenlandse moeder- of "
            "hoofdkantoorcontext in Germany. Dit vormt een concrete reden om "
            "grensoverschrijdende communicatie, onboarding en afstemming met "
            "internationale groepsverwachtingen te verkennen. Open het "
            "gesprek met hoe het Brazil-team aansluiting houdt bij "
            "internationale zakelijke verwachtingen, vooral in klantgerichte "
            "functies, sales, service, onboarding of interne communicatie.")


class TestLocalizeForeignHqEvidenceText:
    def test_parent_country_and_city_rebuilt_fully_in_dutch(self):
        text = "Confirmed foreign parent: Prudential Financial, HQ United States (Newark)."
        result = localize_foreign_hq_evidence_text(text)
        assert result == (
            "Bevestigd buitenlands moederbedrijf: Prudential Financial, "
            "hoofdkantoor United States (Newark).")
        assert "Confirmed foreign parent" not in result


class TestLocalizeEvidenceSummaryApp:
    def test_translates_label_score_and_confidence_and_drops_reason(self):
        text = (
            "International profile: score 2, High confidence. Operates "
            "offices in three countries.")
        result = localize_evidence_summary_app(text)
        assert result == "Internationaal profiel: score 2, Hoge betrouwbaarheid."
        assert "Operates offices" not in result

    def test_technical_reason_never_surfaces(self):
        text = (
            "ICP keyword match: score 2, High confidence. 3 distinct keyword "
            "match(es) in evidence: training, learning, development")
        result = localize_evidence_summary_app(text)
        assert "distinct keyword match" not in result
        assert result == "ICP-trefwoordovereenkomst: score 2, Hoge betrouwbaarheid."


# ---------------------------------------------------------------------------
# Italian (IT) whole-template rebuilders — same architecture, same English
# source templates, Italian output. Guards against mixed-language output
# (both stray English anchor phrases and stray Dutch fragments).
# ---------------------------------------------------------------------------

class TestLocalizeWhyRelevantAppIt:
    def test_foreign_hq_with_signal_rebuilt_fully_in_italian(self):
        text = (
            "Acme Brasil is relevant because it combines a foreign-parent or "
            "international group signal with evidence of international "
            "operations, onboarding, training, or company complexity. That "
            "makes it a practical target for a first conversation about "
            "language, communication, or training support for Brazil-based "
            "teams.")
        result = localize_why_relevant_app_it(text)
        assert result == (
            "Acme Brasil è rilevante perché unisce un segnale di società "
            "madre estera o gruppo internazionale a prove di attività "
            "internazionali, onboarding, formazione o complessità aziendale. "
            "Questo lo rende un obiettivo pratico per una prima conversazione "
            "su lingua, comunicazione o supporto formativo per i team con "
            "sede in Brazil.")
        assert "is relevant because" not in result
        assert "hoofdkantoor" not in result

    def test_unmatched_custom_text_left_in_english(self):
        text = "Some custom analyst note that no template produced."
        assert localize_why_relevant_app_it(text) == text


class TestLocalizeWhatIsHotAppIt:
    def test_known_item_rebuilt_fully_in_italian(self):
        item = (
            "Foreign-parent context gives a clear reason to discuss "
            "cross-border communication and team alignment.")
        result = localize_what_is_hot_item_it(item)
        assert "Foreign-parent context" not in result
        assert result == (
            "Il contesto di società madre estera offre un motivo chiaro per "
            "discutere di comunicazione internazionale e allineamento del "
            "team.")

    def test_unknown_item_left_in_english(self):
        assert localize_what_is_hot_item_it("A custom hot item.") == "A custom hot item."


class TestLocalizeCallerAngleAppIt:
    def test_foreign_hq_variant_rebuilt_fully_in_italian(self):
        text = (
            "Open around how the Brazil team stays aligned with "
            "international business expectations, especially in "
            "customer-facing, sales, service, onboarding, or internal "
            "communication roles.")
        result = localize_caller_angle_app_it(text)
        assert "Open around" not in result
        assert result == (
            "Apri la conversazione su come il team in Brazil rimane "
            "allineato alle aspettative aziendali internazionali, "
            "soprattutto nei ruoli a contatto con i clienti, vendite, "
            "assistenza, onboarding o comunicazione interna.")

    def test_fixed_fallback_variant_translated(self):
        text = (
            "Use a light discovery angle: ask a few open questions to "
            "validate whether international training or communication "
            "needs exist before proposing anything specific.")
        assert "Open around" not in localize_caller_angle_app_it(text)


class TestLocalizeCallStarterAppIt:
    def test_foreign_hq_variant_rebuilt_fully_in_italian(self):
        text = (
            "I saw that Acme Brasil appears to operate in Brazil within a "
            "wider international group context. I was wondering how you "
            "currently support teams that need to work across local "
            "priorities and international expectations.")
        result = localize_call_starter_app_it(text)
        assert "I saw that" not in result
        assert result == (
            "Ho notato che Acme Brasil sembra operare in Brazil all'interno "
            "di un contesto di gruppo internazionale più ampio. Mi chiedevo "
            "come supportate attualmente i team che devono conciliare "
            "priorità locali e aspettative internazionali.")


class TestLocalizeCautionAppIt:
    def test_domain_mismatch_item_rebuilt_fully_in_italian(self):
        text = (
            "Manual review recommended before outreach.; The HQ evidence "
            "source does not clearly match the lead's own domain; verify "
            "the HQ signal before relying on it.")
        result = localize_caution_app_it(text)
        assert "The HQ evidence source" not in result
        assert "hoofdkantoor" not in result
        assert result == (
            "Si consiglia una revisione manuale prima del contatto.; La "
            "fonte delle prove sulla sede centrale non corrisponde "
            "chiaramente al dominio del lead; verifica il segnale prima di "
            "affidarti ad esso.")


class TestLocalizeParentHqSummaryAppIt:
    def test_parent_and_location_rebuilt_fully_in_italian(self):
        text = (
            "The enrichment data identifies Foreign Group as the parent "
            "company, with HQ context in Germany / Munich.")
        result = localize_parent_hq_summary_app_it(text)
        assert result == (
            "I dati di arricchimento identificano Foreign Group come "
            "società madre, con sede centrale in Germany / Munich.")
        assert "The enrichment data" not in result


class TestLocalizeColdCallerSummaryAppIt:
    def test_foreign_hq_composite_rebuilt_fully_in_italian(self):
        text = (
            "The company appears to be a Brazil-based operation connected "
            "to a foreign parent or HQ context in Germany. This creates a "
            "concrete reason to explore cross-border communication, "
            "onboarding, and alignment with international group "
            "expectations. Open around how the Brazil team stays aligned "
            "with international business expectations, especially in "
            "customer-facing, sales, service, onboarding, or internal "
            "communication roles.")
        result = localize_cold_caller_summary_app_it(text)
        assert "The company appears to be" not in result
        assert "Open around" not in result
        assert result == (
            "L'azienda sembra essere un'attività con sede in Brazil "
            "collegata a un contesto di società madre estera o sede "
            "centrale in Germany. Questo crea un motivo concreto per "
            "esplorare la comunicazione internazionale, l'onboarding e "
            "l'allineamento con le aspettative del gruppo internazionale. "
            "Apri la conversazione su come il team in Brazil rimane "
            "allineato alle aspettative aziendali internazionali, "
            "soprattutto nei ruoli a contatto con i clienti, vendite, "
            "assistenza, onboarding o comunicazione interna.")


class TestLocalizeForeignHqEvidenceTextIt:
    def test_parent_country_and_city_rebuilt_fully_in_italian(self):
        text = "Confirmed foreign parent: Prudential Financial, HQ United States (Newark)."
        result = localize_foreign_hq_evidence_text_it(text)
        assert result == (
            "Società madre estera confermata: Prudential Financial, sede "
            "centrale United States (Newark).")
        assert "Confirmed foreign parent" not in result


class TestLocalizeEvidenceSummaryAppIt:
    def test_translates_label_score_and_confidence_and_drops_reason(self):
        text = (
            "International profile: score 2, High confidence. Operates "
            "offices in three countries.")
        result = localize_evidence_summary_app_it(text)
        assert result == "Profilo internazionale: punteggio 2, affidabilità Alta."
        assert "Operates offices" not in result

    def test_technical_reason_never_surfaces(self):
        text = (
            "ICP keyword match: score 2, High confidence. 3 distinct keyword "
            "match(es) in evidence: training, learning, development")
        result = localize_evidence_summary_app_it(text)
        assert "distinct keyword match" not in result
        assert result == "Corrispondenza parole chiave ICP: punteggio 2, affidabilità Alta."


class TestLocalizeDetailRecordForDutch:
    def _sample_detail(self) -> dict:
        why_relevant = (
            "Acme Brasil is relevant because it combines a foreign-parent or "
            "international group signal with evidence of international "
            "operations, onboarding, training, or company complexity. That "
            "makes it a practical target for a first conversation about "
            "language, communication, or training support for Brazil-based "
            "teams.")
        caller_angle = (
            "Open around how the Brazil team stays aligned with "
            "international business expectations, especially in "
            "customer-facing, sales, service, onboarding, or internal "
            "communication roles.")
        call_starter = (
            "I saw that Acme Brasil appears to operate in Brazil within a "
            "wider international group context. I was wondering how you "
            "currently support teams that need to work across local "
            "priorities and international expectations.")
        cold_caller_summary = (
            "The company appears to be a Brazil-based operation connected "
            "to a foreign parent or HQ context in Germany. This creates a "
            "concrete reason to explore cross-border communication, "
            "onboarding, and alignment with international group "
            "expectations. " + caller_angle)
        parent_hq_summary = (
            "The enrichment data identifies Foreign Group as the parent "
            "company, with HQ context in Germany / Munich.")
        evidence_summary = (
            "International profile: score 2, High confidence. Operates "
            "offices in three countries.")
        caution = (
            "Manual review recommended before outreach.; The HQ evidence "
            "source does not clearly match the lead's own domain; verify "
            "the HQ signal before relying on it.")
        what_is_hot = [
            "Foreign-parent context gives a clear reason to discuss "
            "cross-border communication and team alignment.",
            "A custom hot item no template covers.",
        ]
        what_is_not = ["Source evidence should be checked before outreach."]

        return {
            "company_id": "abc123",
            "company_name": "Acme Brasil",
            "domain": "acme.com.br",
            "commercial_fit_score": 80,
            "commercial_tier": "A",
            "why_relevant_app": why_relevant,
            "what_is_hot_app": list(what_is_hot),
            "what_is_not_app": list(what_is_not),
            "caller_angle_app": caller_angle,
            "call_starter_app": call_starter,
            "caution_app": caution,
            "cold_caller_summary_app": cold_caller_summary,
            "parent_hq_summary_app": parent_hq_summary,
            "evidence_summary_app": evidence_summary,
            "advanced_notes_app": "Non-HQ evidence items: 1. Extracted signals: 1.",
            "source_urls": ["https://acme.com/about"],
            "evidence_audit": {"raw_google_evidence_count": 1},
            "debug": {"lead_prioritizer_row": {"foo": "bar"}},
            "ui_payload": {
                "why_relevant": why_relevant,
                "what_is_hot": list(what_is_hot),
                "what_is_not": list(what_is_not),
                "caller_angle": caller_angle,
                "call_starter": call_starter,
                "cold_caller_summary": cold_caller_summary,
                "parent_hq_summary": parent_hq_summary,
                "evidence_summary": evidence_summary,
                "source_urls": ["https://acme.com/about"],
            },
            "visible_icp_signal_scores": [
                {"label": FOREIGN_HQ_SIGNAL_LABEL,
                 "evidence": "Confirmed foreign parent: Foreign Group, HQ Germany (Munich)."},
                {"label": "Some custom label",
                 "evidence": "Custom text quoted directly from a source."},
            ],
        }

    def test_translates_flat_and_nested_fields(self):
        detail = self._sample_detail()
        localized, localized_n, unchanged_n = localize_detail_record_for_dutch(detail)

        assert localized["why_relevant_app"] == (
            "Acme Brasil is relevant omdat het een signaal van een "
            "buitenlandse moeder- of hoofdkantoororganisatie combineert met "
            "bewijs van internationale activiteiten, onboarding, training of "
            "bedrijfscomplexiteit. Dat maakt het een praktisch "
            "aanknopingspunt voor een eerste gesprek over taal, communicatie "
            "of trainingsondersteuning voor in Brazil gevestigde teams.")
        assert localized["what_is_hot_app"][0] == (
            "Buitenlandse moedercontext geeft een duidelijke reden om "
            "grensoverschrijdende communicatie en teamafstemming te "
            "bespreken.")
        assert localized["what_is_hot_app"][1] == "A custom hot item no template covers."
        assert localized["what_is_not_app"] == [
            "Controleer de brondata voordat je contact opneemt."]
        assert "Open around" not in localized["caller_angle_app"]
        assert "I saw that" not in localized["call_starter_app"]
        assert "The HQ evidence source" not in localized["caution_app"]
        assert localized["parent_hq_summary_app"].startswith("De verrijkte data")
        assert localized["evidence_summary_app"] == (
            "Internationaal profiel: score 2, Hoge betrouwbaarheid.")
        # Not translated — no known template for this field.
        assert localized["advanced_notes_app"] == detail["advanced_notes_app"]

        for field in ("why_relevant_app", "caller_angle_app", "call_starter_app",
                      "caution_app", "cold_caller_summary_app",
                      "parent_hq_summary_app", "evidence_summary_app"):
            assert "The hoofdkantoor" not in localized[field]

        ui = localized["ui_payload"]
        assert ui["why_relevant"] == localized["why_relevant_app"]
        assert ui["caller_angle"] == localized["caller_angle_app"]
        assert ui["call_starter"] == localized["call_starter_app"]
        assert ui["cold_caller_summary"] == localized["cold_caller_summary_app"]
        assert ui["parent_hq_summary"] == localized["parent_hq_summary_app"]
        assert ui["evidence_summary"] == localized["evidence_summary_app"]
        assert ui["what_is_hot"] == localized["what_is_hot_app"]
        assert ui["what_is_not"] == localized["what_is_not_app"]

        scores = localized["visible_icp_signal_scores"]
        assert scores[0]["label"] == "Buitenlands hoofdkantoor of groepsstructuur"
        assert scores[0]["evidence"] == (
            "Bevestigd buitenlands moederbedrijf: Foreign Group, "
            "hoofdkantoor Germany (Munich).")
        assert scores[1]["label"] == "Some custom label"  # unknown label untouched
        # Non-foreign-HQ evidence is never touched — it may be an external quote.
        assert scores[1]["evidence"] == "Custom text quoted directly from a source."

        assert localized_n > 0
        assert unchanged_n > 0  # the untranslatable entries above

    def test_ids_domain_scores_and_debug_fields_untouched(self):
        detail = self._sample_detail()
        localized, _, _ = localize_detail_record_for_dutch(detail)

        assert localized["company_id"] == detail["company_id"]
        assert localized["company_name"] == detail["company_name"]
        assert localized["domain"] == detail["domain"]
        assert localized["commercial_fit_score"] == detail["commercial_fit_score"]
        assert localized["commercial_tier"] == detail["commercial_tier"]
        assert localized["source_urls"] == detail["source_urls"]
        assert localized["evidence_audit"] == detail["evidence_audit"]
        assert localized["debug"] == detail["debug"]

    def test_does_not_mutate_input_detail(self):
        detail = self._sample_detail()
        original_why_relevant = detail["why_relevant_app"]
        localize_detail_record_for_dutch(detail)
        assert detail["why_relevant_app"] == original_why_relevant


class TestLocalizeDetailRecordForItalian:
    def _sample_detail(self) -> dict:
        # Same sample shape as TestLocalizeDetailRecordForDutch, kept as a
        # separate copy so each language's test is independently readable.
        why_relevant = (
            "Acme Brasil is relevant because it combines a foreign-parent or "
            "international group signal with evidence of international "
            "operations, onboarding, training, or company complexity. That "
            "makes it a practical target for a first conversation about "
            "language, communication, or training support for Brazil-based "
            "teams.")
        caller_angle = (
            "Open around how the Brazil team stays aligned with "
            "international business expectations, especially in "
            "customer-facing, sales, service, onboarding, or internal "
            "communication roles.")
        call_starter = (
            "I saw that Acme Brasil appears to operate in Brazil within a "
            "wider international group context. I was wondering how you "
            "currently support teams that need to work across local "
            "priorities and international expectations.")
        cold_caller_summary = (
            "The company appears to be a Brazil-based operation connected "
            "to a foreign parent or HQ context in Germany. This creates a "
            "concrete reason to explore cross-border communication, "
            "onboarding, and alignment with international group "
            "expectations. " + caller_angle)
        parent_hq_summary = (
            "The enrichment data identifies Foreign Group as the parent "
            "company, with HQ context in Germany / Munich.")
        evidence_summary = (
            "International profile: score 2, High confidence. Operates "
            "offices in three countries.")
        caution = (
            "Manual review recommended before outreach.; The HQ evidence "
            "source does not clearly match the lead's own domain; verify "
            "the HQ signal before relying on it.")
        what_is_hot = [
            "Foreign-parent context gives a clear reason to discuss "
            "cross-border communication and team alignment.",
            "A custom hot item no template covers.",
        ]
        what_is_not = ["Source evidence should be checked before outreach."]

        return {
            "company_id": "abc123",
            "company_name": "Acme Brasil",
            "domain": "acme.com.br",
            "commercial_fit_score": 80,
            "commercial_tier": "A",
            "why_relevant_app": why_relevant,
            "what_is_hot_app": list(what_is_hot),
            "what_is_not_app": list(what_is_not),
            "caller_angle_app": caller_angle,
            "call_starter_app": call_starter,
            "caution_app": caution,
            "cold_caller_summary_app": cold_caller_summary,
            "parent_hq_summary_app": parent_hq_summary,
            "evidence_summary_app": evidence_summary,
            "advanced_notes_app": "Non-HQ evidence items: 1. Extracted signals: 1.",
            "source_urls": ["https://acme.com/about"],
            "evidence_audit": {"raw_google_evidence_count": 1},
            "debug": {"lead_prioritizer_row": {"foo": "bar"}},
            "ui_payload": {
                "why_relevant": why_relevant,
                "what_is_hot": list(what_is_hot),
                "what_is_not": list(what_is_not),
                "caller_angle": caller_angle,
                "call_starter": call_starter,
                "cold_caller_summary": cold_caller_summary,
                "parent_hq_summary": parent_hq_summary,
                "evidence_summary": evidence_summary,
                "source_urls": ["https://acme.com/about"],
            },
            "visible_icp_signal_scores": [
                {"label": FOREIGN_HQ_SIGNAL_LABEL,
                 "evidence": "Confirmed foreign parent: Foreign Group, HQ Germany (Munich)."},
                {"label": "Some custom label",
                 "evidence": "Custom text quoted directly from a source."},
            ],
        }

    def test_translates_flat_and_nested_fields(self):
        detail = self._sample_detail()
        localized, localized_n, unchanged_n = localize_detail_record_for_italian(detail)

        assert localized["why_relevant_app"] == (
            "Acme Brasil è rilevante perché unisce un segnale di società "
            "madre estera o gruppo internazionale a prove di attività "
            "internazionali, onboarding, formazione o complessità aziendale. "
            "Questo lo rende un obiettivo pratico per una prima conversazione "
            "su lingua, comunicazione o supporto formativo per i team con "
            "sede in Brazil.")
        assert localized["what_is_hot_app"][0] == (
            "Il contesto di società madre estera offre un motivo chiaro per "
            "discutere di comunicazione internazionale e allineamento del "
            "team.")
        assert localized["what_is_hot_app"][1] == "A custom hot item no template covers."
        assert localized["what_is_not_app"] == [
            "Controlla le fonti principali prima dell'outreach."]
        assert "Open around" not in localized["caller_angle_app"]
        assert "I saw that" not in localized["call_starter_app"]
        assert "The HQ evidence source" not in localized["caution_app"]
        assert localized["parent_hq_summary_app"].startswith("I dati di arricchimento")
        assert localized["evidence_summary_app"] == (
            "Profilo internazionale: punteggio 2, affidabilità Alta.")
        # Not translated — no known template for this field.
        assert localized["advanced_notes_app"] == detail["advanced_notes_app"]

        for field in ("why_relevant_app", "caller_angle_app", "call_starter_app",
                      "caution_app", "cold_caller_summary_app",
                      "parent_hq_summary_app", "evidence_summary_app"):
            # No mixed-language leakage, English anchors or Dutch fragments.
            assert "hoofdkantoor" not in localized[field]
            assert "is relevant because" not in localized[field]

        ui = localized["ui_payload"]
        assert ui["why_relevant"] == localized["why_relevant_app"]
        assert ui["caller_angle"] == localized["caller_angle_app"]
        assert ui["call_starter"] == localized["call_starter_app"]
        assert ui["cold_caller_summary"] == localized["cold_caller_summary_app"]
        assert ui["parent_hq_summary"] == localized["parent_hq_summary_app"]
        assert ui["evidence_summary"] == localized["evidence_summary_app"]
        assert ui["what_is_hot"] == localized["what_is_hot_app"]
        assert ui["what_is_not"] == localized["what_is_not_app"]

        scores = localized["visible_icp_signal_scores"]
        assert scores[0]["label"] == "Sede centrale estera o struttura di gruppo"
        assert scores[0]["evidence"] == (
            "Società madre estera confermata: Foreign Group, sede centrale "
            "Germany (Munich).")
        assert scores[1]["label"] == "Some custom label"  # unknown label untouched
        # Non-foreign-HQ evidence is never touched — it may be an external quote.
        assert scores[1]["evidence"] == "Custom text quoted directly from a source."

        assert localized_n > 0
        assert unchanged_n > 0  # the untranslatable entries above

    def test_ids_domain_scores_and_debug_fields_untouched(self):
        detail = self._sample_detail()
        localized, _, _ = localize_detail_record_for_italian(detail)

        assert localized["company_id"] == detail["company_id"]
        assert localized["company_name"] == detail["company_name"]
        assert localized["domain"] == detail["domain"]
        assert localized["commercial_fit_score"] == detail["commercial_fit_score"]
        assert localized["commercial_tier"] == detail["commercial_tier"]
        assert localized["source_urls"] == detail["source_urls"]
        assert localized["evidence_audit"] == detail["evidence_audit"]
        assert localized["debug"] == detail["debug"]

    def test_does_not_mutate_input_detail(self):
        detail = self._sample_detail()
        original_why_relevant = detail["why_relevant_app"]
        localize_detail_record_for_italian(detail)
        assert detail["why_relevant_app"] == original_why_relevant


# ---------------------------------------------------------------------------
# End-to-end: content_language on export_workbook_to_lovable_json
# ---------------------------------------------------------------------------

_FOREIGN_HQ_WHY_RELEVANT_EN = (
    "Acme Brasil is relevant because it shows a foreign-parent or HQ context "
    "outside Brazil. That alone is a practical reason to open a conversation "
    "about how the local team stays aligned with the wider group.")
_FOREIGN_HQ_WHY_RELEVANT_NL = (
    "Acme Brasil is relevant omdat het een buitenlandse moeder- of "
    "hoofdkantoorcontext buiten Brazil laat zien. Dat alleen al is een "
    "praktische reden om een gesprek te openen over hoe het lokale team "
    "afgestemd blijft met de bredere groep.")


def _foreign_hq_row(**overrides) -> dict:
    row = dict(
        why_relevant_app=_FOREIGN_HQ_WHY_RELEVANT_EN,
        c5_parent_company="Foreign Group",
        c5_parent_hq_country="Germany",
    )
    row.update(overrides)
    return enriched_row(**row)


class TestContentLanguageEnglishUnchanged:
    def test_english_is_the_default_and_leaves_text_untouched(self, tmp_path):
        enriched = [_foreign_hq_row()]
        manifest, out_dir = run_export(tmp_path, enriched)

        assert manifest["content_language"] == "English"
        assert manifest["localization"] == {"enabled": False}

        detail = detail_for(out_dir, "Acme Brasil")
        assert detail["why_relevant_app"] == _FOREIGN_HQ_WHY_RELEVANT_EN
        foreign_row = next(
            s for s in detail["visible_icp_signal_scores"]
            if s["label"] == FOREIGN_HQ_SIGNAL_LABEL)
        assert foreign_row["evidence"] == (
            "Confirmed foreign parent: Foreign Group, HQ Germany.")

    def test_explicit_english_matches_omitted_default(self, tmp_path):
        enriched = [_foreign_hq_row()]
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        manifest_default, out_default = run_export(tmp_path / "a", enriched)
        manifest_explicit, out_explicit = run_export(
            tmp_path / "b", enriched, content_language="English")

        assert detail_for(out_default, "Acme Brasil") == detail_for(
            out_explicit, "Acme Brasil")
        assert manifest_default["content_language"] == manifest_explicit["content_language"]


class TestContentLanguageDutch:
    def test_localizes_known_visible_icp_signal_scores_label(self, tmp_path):
        enriched = [enriched_row()]
        signals = [
            {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
             "evidence_quote": "Recognized as a great place to work by employees."},
        ]
        _, out_dir = run_export(tmp_path, enriched, signals=signals,
                                content_language="Dutch")

        detail = detail_for(out_dir, "Acme Brasil")
        labels = {s["label"] for s in detail["visible_icp_signal_scores"]}
        assert "Employer branding of medewerkerstevredenheid" in labels
        assert "Employer branding or employee satisfaction" not in labels

    def test_localizes_foreign_hq_evidence_text(self, tmp_path):
        enriched = [_foreign_hq_row()]
        _, out_dir = run_export(tmp_path, enriched, content_language="Dutch")

        detail = detail_for(out_dir, "Acme Brasil")
        foreign_row = next(
            s for s in detail["visible_icp_signal_scores"]
            if s["label"] == "Buitenlands hoofdkantoor of groepsstructuur")
        assert foreign_row["evidence"] == (
            "Bevestigd buitenlands moederbedrijf: Foreign Group, hoofdkantoor Germany.")

    def test_localizes_nested_ui_payload_fields(self, tmp_path):
        enriched = [_foreign_hq_row()]
        _, out_dir = run_export(tmp_path, enriched, content_language="Dutch")

        detail = detail_for(out_dir, "Acme Brasil")
        # The legacy Italy-compatible *_app field is still fully localized.
        assert detail["why_relevant_app"] == _FOREIGN_HQ_WHY_RELEVANT_NL
        # ui_payload.why_relevant is now built independently by the non-Italy
        # curated layer (richer, company-specific, English-only for now) —
        # it is no longer a mirror of why_relevant_app.
        assert detail["ui_payload"]["why_relevant"] == (
            "Acme Brasil is a Brazil-based company in Manufacturing. It "
            "operates as part of Foreign Group, headquartered in Germany. "
            "The current evidence is not strong enough to confirm a "
            "specific training trigger, so treat this as a light discovery "
            "lead and first validate whether international communication, "
            "onboarding, or team-development needs exist.")
        assert "is relevant because" not in detail["why_relevant_app"]

    def test_ids_domain_scores_tiers_and_debug_unchanged(self, tmp_path):
        enriched = [_foreign_hq_row()]
        (tmp_path / "en").mkdir()
        (tmp_path / "nl").mkdir()
        _, out_en = run_export(tmp_path / "en", enriched, content_language="English")
        _, out_nl = run_export(tmp_path / "nl", enriched, content_language="Dutch")

        item_en = load_list(out_en)[0]
        item_nl = load_list(out_nl)[0]
        assert item_en["company_id"] == item_nl["company_id"]
        assert item_en["domain"] == item_nl["domain"]
        assert item_en["commercial_fit_score"] == item_nl["commercial_fit_score"]
        assert item_en["commercial_tier"] == item_nl["commercial_tier"]

        detail_en = detail_for(out_en, "Acme Brasil")
        detail_nl = detail_for(out_nl, "Acme Brasil")
        assert detail_en["source_urls"] == detail_nl["source_urls"]
        assert detail_en["evidence_audit"] == detail_nl["evidence_audit"]
        assert detail_en["debug"] == detail_nl["debug"]

    def test_manifest_reports_content_language_and_localization_summary(self, tmp_path):
        enriched = [_foreign_hq_row()]
        manifest, _ = run_export(tmp_path, enriched, content_language="Dutch")

        assert manifest["content_language"] == "Dutch"
        localization = manifest["localization"]
        assert localization["enabled"] is True
        assert localization["mode"] == "deterministic_demo"
        assert localization["localized_field_count"] > 0
        assert "unchanged_field_count" in localization

    def test_unrecognized_language_falls_back_to_english_behavior(self, tmp_path):
        enriched = [_foreign_hq_row()]
        manifest, out_dir = run_export(tmp_path, enriched, content_language="French")

        assert manifest["content_language"] == "English"
        assert manifest["localization"] == {"enabled": False}
        detail = detail_for(out_dir, "Acme Brasil")
        assert detail["why_relevant_app"] == _FOREIGN_HQ_WHY_RELEVANT_EN

    def test_dutch_output_is_unaffected_by_italian_support(self, tmp_path):
        # Regression guard: adding Italian must not change a single byte of
        # Dutch output.
        enriched = [_foreign_hq_row()]
        _, out_dir = run_export(tmp_path, enriched, content_language="Dutch")

        detail = detail_for(out_dir, "Acme Brasil")
        assert detail["why_relevant_app"] == _FOREIGN_HQ_WHY_RELEVANT_NL
        foreign_row = next(
            s for s in detail["visible_icp_signal_scores"]
            if s["label"] == "Buitenlands hoofdkantoor of groepsstructuur")
        assert foreign_row["evidence"] == (
            "Bevestigd buitenlands moederbedrijf: Foreign Group, hoofdkantoor Germany.")


_FOREIGN_HQ_WHY_RELEVANT_IT = (
    "Acme Brasil è rilevante perché mostra un contesto di società madre "
    "estera o sede centrale al di fuori di Brazil. Questo da solo è un "
    "motivo pratico per aprire una conversazione su come il team locale "
    "rimane allineato con il gruppo più ampio.")


class TestContentLanguageItalian:
    def test_localizes_known_visible_icp_signal_scores_label(self, tmp_path):
        enriched = [enriched_row()]
        signals = [
            {"source_index": 1, "signal_name": "employer_branding", "signal_score": 2,
             "evidence_quote": "Recognized as a great place to work by employees."},
        ]
        _, out_dir = run_export(tmp_path, enriched, signals=signals,
                                content_language="Italian")

        detail = detail_for(out_dir, "Acme Brasil")
        labels = {s["label"] for s in detail["visible_icp_signal_scores"]}
        assert "Employer branding o soddisfazione dei dipendenti" in labels
        assert "Employer branding or employee satisfaction" not in labels

    def test_localizes_foreign_hq_evidence_text(self, tmp_path):
        enriched = [_foreign_hq_row()]
        _, out_dir = run_export(tmp_path, enriched, content_language="Italian")

        detail = detail_for(out_dir, "Acme Brasil")
        foreign_row = next(
            s for s in detail["visible_icp_signal_scores"]
            if s["label"] == "Sede centrale estera o struttura di gruppo")
        assert foreign_row["evidence"] == (
            "Società madre estera confermata: Foreign Group, sede centrale Germany.")

    def test_localizes_nested_ui_payload_fields(self, tmp_path):
        enriched = [_foreign_hq_row()]
        _, out_dir = run_export(tmp_path, enriched, content_language="Italian")

        detail = detail_for(out_dir, "Acme Brasil")
        # The legacy Italy-compatible *_app field is still fully localized.
        assert detail["why_relevant_app"] == _FOREIGN_HQ_WHY_RELEVANT_IT
        assert "is relevant because" not in detail["why_relevant_app"]
        assert "hoofdkantoor" not in detail["why_relevant_app"]
        # ui_payload.why_relevant is now built independently (richer,
        # company-specific) and has no matching Italian template to rebuild
        # from, so it stays in the English it was generated in — it is no
        # longer a mirror of why_relevant_app.
        assert detail["ui_payload"]["why_relevant"] == (
            "Acme Brasil is a Brazil-based manufacturing company operating "
            "as part of Foreign Group, headquartered in Germany.")

    def test_ids_domain_scores_tiers_and_debug_unchanged(self, tmp_path):
        enriched = [_foreign_hq_row()]
        (tmp_path / "en").mkdir()
        (tmp_path / "it").mkdir()
        _, out_en = run_export(tmp_path / "en", enriched, content_language="English")
        _, out_it = run_export(tmp_path / "it", enriched, content_language="Italian")

        item_en = load_list(out_en)[0]
        item_it = load_list(out_it)[0]
        assert item_en["company_id"] == item_it["company_id"]
        assert item_en["domain"] == item_it["domain"]
        assert item_en["commercial_fit_score"] == item_it["commercial_fit_score"]
        assert item_en["commercial_tier"] == item_it["commercial_tier"]

        detail_en = detail_for(out_en, "Acme Brasil")
        detail_it = detail_for(out_it, "Acme Brasil")
        assert detail_en["source_urls"] == detail_it["source_urls"]
        assert detail_en["evidence_audit"] == detail_it["evidence_audit"]
        assert detail_en["debug"] == detail_it["debug"]

    def test_manifest_reports_content_language_and_localization_summary(self, tmp_path):
        enriched = [_foreign_hq_row()]
        manifest, _ = run_export(tmp_path, enriched, content_language="Italian")

        assert manifest["content_language"] == "Italian"
        localization = manifest["localization"]
        assert localization["enabled"] is True
        assert localization["mode"] == "deterministic_demo"
        assert localization["localized_field_count"] > 0
        assert "unchanged_field_count" in localization

    def test_unrecognized_language_falls_back_to_english_behavior(self, tmp_path):
        enriched = [_foreign_hq_row()]
        manifest, out_dir = run_export(tmp_path, enriched, content_language="French")

        assert manifest["content_language"] == "English"
        assert manifest["localization"] == {"enabled": False}
        detail = detail_for(out_dir, "Acme Brasil")
        assert detail["why_relevant_app"] == _FOREIGN_HQ_WHY_RELEVANT_EN

    def test_english_output_is_unaffected_by_italian_support(self, tmp_path):
        # Regression guard: adding Italian must not change a single byte of
        # English output.
        enriched = [_foreign_hq_row()]
        manifest, out_dir = run_export(tmp_path, enriched)

        assert manifest["content_language"] == "English"
        assert manifest["localization"] == {"enabled": False}
        detail = detail_for(out_dir, "Acme Brasil")
        assert detail["why_relevant_app"] == _FOREIGN_HQ_WHY_RELEVANT_EN
        foreign_row = next(
            s for s in detail["visible_icp_signal_scores"]
            if s["label"] == FOREIGN_HQ_SIGNAL_LABEL)
        assert foreign_row["evidence"] == (
            "Confirmed foreign parent: Foreign Group, HQ Germany.")


# ---------------------------------------------------------------------------
# Three-tier driver visibility: Strong / Moderate / Weak + hidden, plus the
# independent own_domain vs external source axis — see VISIBILITY_TIERS_NOTES.md.
# ---------------------------------------------------------------------------

_RAW_DUMP_EVIDENCE = "___THE NETHERLANDS\n___Austria\n___China\n___Brazil"


def _drivers_by_label(out_dir, company="Acme Brasil"):
    return {d["label"]: d
            for d in detail_for(out_dir, company)["ui_payload"]["commercial_fit_drivers"]}


def test_weak_tier_shown_with_own_domain_link(tmp_path):
    # score 1 whose evidence text fails the cleanliness check, but the URL is
    # on the company's own domain -> Weak card with link and own_domain scope
    # (previously "Rejected"/omitted).
    signals = [
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 1,
         "evidence_quote": _RAW_DUMP_EVIDENCE,
         "evidence_url": "https://acme.com/careers"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row(domain="acme.com")], signals=signals,
                            export_country="Brazil", foreign_hq_only=False)
    card = _drivers_by_label(out_dir)["Explicit learning and development"]
    assert card["strength"] == "Weak"
    assert card["source_scope"] == "own_domain"
    assert card["evidence_source_url"] == "https://acme.com/careers"
    assert card["evidence"] == ""
    assert card["note"]


def test_weak_tier_external_source_badge(tmp_path):
    # employer_branding is included (no exception); a hosted Glassdoor link
    # still shows as Weak, flagged external.
    signals = [
        {"source_index": 1, "signal_name": "employer_branding", "signal_score": 1,
         "evidence_quote": _RAW_DUMP_EVIDENCE,
         "evidence_url": "https://www.glassdoor.com/Reviews/Acme.htm"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row(domain="acme.com")], signals=signals,
                            export_country="Brazil", foreign_hq_only=False)
    card = _drivers_by_label(out_dir)["Employer branding or employee satisfaction"]
    assert card["strength"] == "Weak"
    assert card["source_scope"] == "external"
    assert card["evidence_source_url"] == "https://www.glassdoor.com/Reviews/Acme.htm"


def test_score_zero_stays_hidden_not_weak(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 0,
         "evidence_quote": "Some unrelated text.", "evidence_url": "https://acme.com/x"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row(domain="acme.com")], signals=signals,
                            export_country="Brazil", foreign_hq_only=False)
    assert _drivers_by_label(out_dir)["Explicit learning and development"]["strength"] \
        == "Not evidenced"


def test_weak_needs_a_url_else_not_promoted(tmp_path):
    # score 1, rejected evidence, but NO evidence_url -> nothing to link ->
    # stays Rejected/Not evidenced, never a Weak card out of thin air.
    signals = [
        {"source_index": 1, "signal_name": "icp_keyword_match", "signal_score": 1,
         "evidence_quote": _RAW_DUMP_EVIDENCE},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row(domain="acme.com")], signals=signals,
                            export_country="Brazil", foreign_hq_only=False)
    assert _drivers_by_label(out_dir)["Explicit learning and development"]["strength"] \
        in ("Rejected", "Not evidenced")


def test_confidence_and_source_axes_are_independent(tmp_path):
    # Strong (clean own-domain evidence) still reports source_scope separately.
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 2,
         "evidence_quote": "Acme Brasil operates internationally across many countries and export markets.",
         "evidence_url": "https://acme.com/about"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row(domain="acme.com")], signals=signals,
                            export_country="Brazil", foreign_hq_only=False)
    card = _drivers_by_label(out_dir)["International business context"]
    assert card["strength"] == "Strong"
    assert card["source_scope"] == "own_domain"


def test_italy_weak_signal_now_visible_with_link(tmp_path):
    # Italy path previously omitted every sub-threshold bucketed signal; it now
    # shows as Weak with its link.
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 1,
         "evidence_quote": _RAW_DUMP_EVIDENCE,
         "evidence_url": "https://acme.com/about"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row(domain="acme.com")], signals=signals,
                            export_country="Italy", content_language="Italian",
                            foreign_hq_only=False)
    drivers = detail_for(out_dir, "Acme Brasil")["ui_payload"]["commercial_fit_drivers"]
    intl = [d for d in drivers if "nternational" in d["label"]]
    assert intl, "international driver should be present, not omitted"
    assert intl[0]["strength"] == "Weak"
    assert intl[0]["evidence_source_url"] == "https://acme.com/about"


def test_italy_score_zero_still_omitted(tmp_path):
    signals = [
        {"source_index": 1, "signal_name": "international_profile", "signal_score": 0,
         "evidence_quote": "x", "evidence_url": "https://acme.com/about"},
    ]
    _, out_dir = run_export(tmp_path, [enriched_row(domain="acme.com")], signals=signals,
                            export_country="Italy", content_language="Italian",
                            foreign_hq_only=False)
    drivers = detail_for(out_dir, "Acme Brasil")["ui_payload"]["commercial_fit_drivers"]
    assert not any("nternational business" in d["label"] for d in drivers)


# ---------------------------------------------------------------------------
# hq_location_summary — always-shown structured HQ location line.
# ---------------------------------------------------------------------------

def test_hq_location_summary_flows_into_detail_record(tmp_path):
    enriched = [enriched_row(
        hq_location_summary="Parent company headquarters: Tokyo, Japan")]
    _, out_dir = run_export(tmp_path, enriched)
    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["hq_location_summary"] == "Parent company headquarters: Tokyo, Japan"


def test_hq_location_summary_absent_when_blank(tmp_path):
    enriched = [enriched_row()]  # no hq_location_summary key at all
    _, out_dir = run_export(tmp_path, enriched)
    detail = detail_for(out_dir, "Acme Brasil")
    assert detail["hq_location_summary"] is None


def test_hq_location_summary_localized_for_dutch(tmp_path):
    from lovable_content_localization import localize_hq_location_summary
    detail = {"hq_location_summary": "Parent company headquarters: Tokyo, Japan"}
    localized, _, _ = localize_detail_record_for_dutch(detail)
    assert localized["hq_location_summary"] == "Hoofdkantoor moederbedrijf: Tokio, Japan"
    # sanity: the direct localizer agrees.
    assert localize_hq_location_summary(detail["hq_location_summary"]) == (
        "Hoofdkantoor moederbedrijf: Tokio, Japan")


# ---------------------------------------------------------------------------
# "Confirmed domestic" driver badge — detect_confirmed_domestic_hq_for_export
# and its wiring into build_fixed_commercial_fit_drivers.
# ---------------------------------------------------------------------------

from export_lead_prioritizer_to_lovable_json import detect_confirmed_domestic_hq_for_export


def _domestic_row(**overrides) -> dict:
    row = enriched_row(
        sig_foreign_hq_score_for_next_scoring=0,
        hq_structure_type="domestic",
        hq_confidence="High",
        hq_detected_country="Netherlands",
        hq_detected_city="Amsterdam",
        hq_evidence_url="https://coolblue.nl/about",
        hq_location_summary="Headquarters: Amsterdam, Netherlands",
    )
    row.update(overrides)
    return row


class TestDetectConfirmedDomesticHqForExportUnit:
    def test_fires_for_high_confidence_domestic_with_location(self):
        row = _domestic_row()
        confirmed, location = detect_confirmed_domestic_hq_for_export(row, False)
        assert confirmed is True
        assert location == "Amsterdam, Netherlands"

    def test_fires_for_medium_confidence(self):
        row = _domestic_row(hq_confidence="Medium")
        confirmed, _ = detect_confirmed_domestic_hq_for_export(row, False)
        assert confirmed is True

    def test_never_fires_when_foreign_hq_detected_true(self):
        # Mutual exclusivity guard, even if hq_structure_type is inconsistent.
        row = _domestic_row()
        confirmed, location = detect_confirmed_domestic_hq_for_export(row, True)
        assert confirmed is False
        assert location is None

    def test_low_confidence_does_not_fire(self):
        row = _domestic_row(hq_confidence="Low")
        confirmed, location = detect_confirmed_domestic_hq_for_export(row, False)
        assert confirmed is False
        assert location is None

    def test_non_domestic_structure_type_does_not_fire(self):
        row = _domestic_row(hq_structure_type="regional_branch_only")
        confirmed, location = detect_confirmed_domestic_hq_for_export(row, False)
        assert confirmed is False

    def test_no_resolvable_location_does_not_fire(self):
        row = _domestic_row(hq_detected_country="", hq_detected_city="",
                            ai_parent_hq_country="", c5_parent_hq_country="")
        confirmed, location = detect_confirmed_domestic_hq_for_export(row, False)
        assert confirmed is False
        assert location is None

    def test_blank_row_does_not_fire(self):
        row = enriched_row(sig_foreign_hq_score_for_next_scoring=0)
        confirmed, location = detect_confirmed_domestic_hq_for_export(row, False)
        assert confirmed is False
        assert location is None

    def test_location_matches_hq_location_summary_chain(self):
        # C5 > AI > detected priority — same chain hq_location_summary uses.
        row = _domestic_row(
            hq_detected_country="Belgium", hq_detected_city="Brussels")
        confirmed, location = detect_confirmed_domestic_hq_for_export(row, False)
        assert confirmed is True
        assert location == "Brussels, Belgium"


class TestConfirmedDomesticDriverCard(object):
    def test_driver_card_shows_confirmed_domestic(self, tmp_path):
        enriched = [_domestic_row()]
        _, out_dir = run_export(tmp_path, enriched, foreign_hq_only=False)
        detail = detail_for(out_dir, "Acme Brasil")
        drivers_by_label = {
            d["label"]: d for d in detail["ui_payload"]["commercial_fit_drivers"]
        }
        driver = drivers_by_label["Foreign ownership or group structure"]
        assert driver["strength"] == "Confirmed domestic"
        assert driver["evidence"] == (
            "Amsterdam, Netherlands is the confirmed local headquarters; "
            "no foreign parent identified.")
        assert driver["evidence_source_url"] == "https://coolblue.nl/about"
        assert driver.get("evidence_sources")

    def test_driver_location_text_agrees_with_hq_location_summary(self, tmp_path):
        enriched = [_domestic_row(
            hq_detected_country="Belgium", hq_detected_city="Brussels",
            hq_location_summary="Headquarters: Brussels, Belgium")]
        _, out_dir = run_export(tmp_path, enriched, foreign_hq_only=False)
        detail = detail_for(out_dir, "Acme Brasil")
        driver = next(
            d for d in detail["ui_payload"]["commercial_fit_drivers"]
            if d["id"] == "foreign_ownership_or_group_structure")
        location_from_summary = detail["hq_location_summary"].removeprefix("Headquarters: ")
        assert location_from_summary in driver["evidence"]

    def test_genuinely_blank_row_still_not_evidenced(self, tmp_path):
        enriched = [enriched_row(sig_foreign_hq_score_for_next_scoring=0)]
        _, out_dir = run_export(tmp_path, enriched, foreign_hq_only=False)
        detail = detail_for(out_dir, "Acme Brasil")
        driver = next(
            d for d in detail["ui_payload"]["commercial_fit_drivers"]
            if d["id"] == "foreign_ownership_or_group_structure")
        assert driver["strength"] == "Not evidenced"
        assert detail.get("hq_location_summary") is None

    def test_low_confidence_domestic_stays_not_evidenced_in_driver(self, tmp_path):
        # Stricter bar for the driver card than hq_location_summary itself:
        # a Low-confidence domestic call never earns "Confirmed domestic".
        enriched = [_domestic_row(hq_confidence="Low")]
        _, out_dir = run_export(tmp_path, enriched, foreign_hq_only=False)
        detail = detail_for(out_dir, "Acme Brasil")
        driver = next(
            d for d in detail["ui_payload"]["commercial_fit_drivers"]
            if d["id"] == "foreign_ownership_or_group_structure")
        assert driver["strength"] == "Not evidenced"

    def test_confirmed_domestic_never_positive_in_why_relevant_or_what_is_hot(self, tmp_path):
        enriched = [_domestic_row()]
        _, out_dir = run_export(tmp_path, enriched, foreign_hq_only=False)
        detail = detail_for(out_dir, "Acme Brasil")
        why_relevant = detail["ui_payload"]["why_relevant"]
        what_is_hot = detail["ui_payload"]["what_is_hot"]
        assert "foreign" not in why_relevant.lower()
        assert not any("foreign" in bullet.lower() for bullet in what_is_hot)
        assert not any(
            "confirmed local headquarters" in bullet.lower() for bullet in what_is_hot)

    def test_confirmed_domestic_not_in_positive_driver_strengths(self):
        from export_lead_prioritizer_to_lovable_json import _POSITIVE_DRIVER_STRENGTHS
        assert "Confirmed domestic" not in _POSITIVE_DRIVER_STRENGTHS

    def test_foreign_and_confirmed_domestic_are_mutually_exclusive_in_export(self, tmp_path):
        # A row with BOTH a foreign score-3 AND domestic-looking fields (should
        # never happen upstream, but the export-side guard must still hold):
        # foreign wins, "Confirmed domestic" never appears.
        enriched = [_domestic_row(
            sig_foreign_hq_score_for_next_scoring=3,
            ai_parent_hq_country="Japan", ai_parent_company="Acme KK",
        )]
        _, out_dir = run_export(tmp_path, enriched)
        detail = detail_for(out_dir, "Acme Brasil")
        driver = next(
            d for d in detail["ui_payload"]["commercial_fit_drivers"]
            if d["id"] == "foreign_ownership_or_group_structure")
        assert driver["strength"] == "Strong"
