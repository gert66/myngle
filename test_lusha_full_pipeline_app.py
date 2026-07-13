"""Tests for lusha_full_pipeline_app.py's pure orchestration logic. No real
Lusha/Cloud Run/GCS calls -- requests and the underlying subprocess-driving
helpers are monkeypatched or exercised purely in-memory."""

from pathlib import Path

import pandas as pd
import pytest

import lusha_full_pipeline_app as app


# ---------------------------------------------------------------------------
# enrich_companies -- batching
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_enrich_companies_batches_by_100(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(list(json["ids"]))
        return _FakeResponse({"results": [{"id": i} for i in json["ids"]]})

    monkeypatch.setattr(app.requests, "post", fake_post)

    ids = [str(i) for i in range(150)]
    results = app.enrich_companies("key", ids)

    assert len(calls) == 2
    assert len(calls[0]) == 100
    assert len(calls[1]) == 50
    assert [r["id"] for r in results] == ids


def test_enrich_companies_passes_reveal(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(json)
        return _FakeResponse({"results": []})

    monkeypatch.setattr(app.requests, "post", fake_post)
    app.enrich_companies("key", ["1"], reveal=["competitors"])
    assert captured["reveal"] == ["competitors"]


# ---------------------------------------------------------------------------
# lusha_enrich_records_to_dataframe
# ---------------------------------------------------------------------------

def test_lusha_enrich_records_to_dataframe_maps_fields():
    records = [{
        "id": "1", "name": "ACME", "domain": "acme.com",
        "description": "A widget maker.",
        "employeeCount": {"exact": 250, "min": 201, "max": 500},
        "industry": "Manufacturing", "subIndustry": "Widgets",
        "location": {"country": "Uruguay"},
        "socialLinks": {"linkedin": "https://linkedin.com/company/acme"},
    }]
    df = app.lusha_enrich_records_to_dataframe(records)
    row = df.iloc[0]
    assert row["Company Name"] == "ACME"
    assert row["Company Domain"] == "acme.com"
    assert row["Company Description"] == "A widget maker."
    assert row["Company Number of Employees"] == 250
    assert row["Company Main Industry"] == "Manufacturing"
    assert row["Company Sub Industry"] == "Widgets"
    assert row["Company Country"] == "Uruguay"
    assert row["Company linkedin URL"] == "https://linkedin.com/company/acme"


def test_lusha_enrich_records_to_dataframe_falls_back_to_max_when_no_exact():
    records = [{"id": "1", "name": "X", "domain": "x.com", "employeeCount": {"min": 51, "max": 200}}]
    df = app.lusha_enrich_records_to_dataframe(records)
    assert df.iloc[0]["Company Number of Employees"] == 200


def test_lusha_enrich_records_to_dataframe_handles_missing_optional_fields():
    records = [{"id": "1", "name": "X", "domain": "x.com"}]
    df = app.lusha_enrich_records_to_dataframe(records)
    row = df.iloc[0]
    assert row["Company Description"] == ""
    assert row["Company Main Industry"] == ""
    assert row["Company Number of Employees"] == ""


# ---------------------------------------------------------------------------
# run_input_cleaning
# ---------------------------------------------------------------------------

def _records(*rows):
    return [dict(r) for r in rows]


def test_run_input_cleaning_dedupes_excludes_and_sorts():
    records = _records(
        {"id": "1", "name": "ACME BV", "domain": "acme.com", "description": "Software.",
         "employeeCount": {"exact": 250}, "industry": "Technology, Information & Media",
         "subIndustry": "Software Development", "location": {"country": "Uruguay"}},
        {"id": "2", "name": "ACME BV (dup)", "domain": "acme.com", "description": "",
         "employeeCount": {"exact": 10}, "industry": "", "subIndustry": "",
         "location": {"country": "Uruguay"}},
        {"id": "3", "name": "Escuela Publica", "domain": "escuela.edu.uy",
         "description": "A public school.", "employeeCount": {"exact": 60},
         "industry": "Education", "subIndustry": "Primary/Secondary Education",
         "location": {"country": "Uruguay"}},
    )
    df = app.lusha_enrich_records_to_dataframe(records)
    result = app.run_input_cleaning(df, run_prescreen=False)

    assert result["funnel"]["Duplicaten verwijderd"] == 1
    assert result["funnel"]["Uitgesloten op industrie"] == 1
    assert result["selected_df"]["domain"].tolist() == ["acme.com"]
    assert len(result["excel_bytes"]) > 0
    assert result["prescreen_ran"] is False


def test_run_input_cleaning_does_not_reexclude_government_or_nonprofit():
    # Government/Community are already filtered out server-side by Lusha's
    # own prospecting exclude filter -- run_input_cleaning must not exclude
    # them again even if one somehow slips through.
    records = _records(
        {"id": "1", "name": "City Hall", "domain": "cityhall.gov.uy", "description": "",
         "employeeCount": {"exact": 300}, "industry": "Government", "subIndustry": "",
         "location": {"country": "Uruguay"}},
    )
    df = app.lusha_enrich_records_to_dataframe(records)
    result = app.run_input_cleaning(df, run_prescreen=False)
    assert result["selected_df"]["domain"].tolist() == ["cityhall.gov.uy"]


def test_run_input_cleaning_works_on_thin_sector_loop_records():
    # fetch_companies_by_sector records only ever carry id/name/domain/
    # main_industry -- no Description, no employees. Main Industry is
    # already known (no blank-industry rows), so nothing is eligible for
    # Haiku regardless of run_prescreen.
    records = [
        {"id": "1", "name": "ACME BV", "domain": "acme.com", "main_industry": "Finance"},
        {"id": "2", "name": "Escuela Publica", "domain": "escuela.edu.uy", "main_industry": "Education"},
    ]
    df = app.lusha_sector_records_to_dataframe(records)
    result = app.run_input_cleaning(df, run_prescreen=True, anthropic_key="unused")

    assert result["prescreen_ran"] is False  # nothing eligible -- every row has a Main Industry
    assert result["selected_df"]["domain"].tolist() == ["acme.com"]
    assert result["funnel"]["Uitgesloten op industrie"] == 1


def test_run_input_cleaning_raises_on_missing_required_columns():
    df = pd.DataFrame([{"foo": "bar"}])
    with pytest.raises(ValueError):
        app.run_input_cleaning(df, run_prescreen=False)


# ---------------------------------------------------------------------------
# build_pipeline_run_manifest
# ---------------------------------------------------------------------------

def test_build_pipeline_run_manifest_shape():
    manifest = app.build_pipeline_run_manifest(
        run_id="run1", input_uri="gs://b/incoming/x.xlsx", output_dir="gs://b/runs/run1",
        task_count=10, execution_name="exec-1", export_country="Uruguay",
        cold_callers=["Vanessa"], foreign_hq_only=True, gcs_bucket="b",
    )
    assert manifest["config"]["autopilot"] is True
    assert manifest["config"]["gate_full_enrichment_on_foreign_hq"] is True
    export_cfg = manifest["config"]["lovable_export"]
    assert export_cfg["country"] == "Uruguay"
    assert export_cfg["cold_callers"] == ["Vanessa"]
    assert export_cfg["foreign_hq_only"] is True
    assert export_cfg["merge_current"] is True
    assert export_cfg["gcs_prefix"] == "uruguay"


# ---------------------------------------------------------------------------
# Country registration
# ---------------------------------------------------------------------------

def test_country_needs_registration_true_for_unknown_country():
    assert app.country_needs_registration("Wakanda") is True


def test_country_needs_registration_false_for_known_country():
    assert app.country_needs_registration("Uruguay") is False


def test_register_country_in_source_is_noop_for_known_country(tmp_path):
    assert app.register_country_in_source("Uruguay", path=tmp_path / "unused.py") is None


def test_register_country_in_source_adds_new_label(tmp_path):
    src = Path(__file__).parent / "generate_lovable_countries_index.py"
    copy = tmp_path / "generate_lovable_countries_index.py"
    copy.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    summary = app.register_country_in_source("Wakanda", path=copy)

    assert summary is not None
    assert "Wakanda" in summary
    new_text = copy.read_text(encoding="utf-8")
    assert '"Wakanda"' in new_text
    assert '"Uruguay"' in new_text  # existing labels preserved
    compile(new_text, str(copy), "exec")  # still valid Python
