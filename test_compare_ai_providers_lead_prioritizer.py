"""Tests for compare_ai_providers_lead_prioritizer.

All comparison runs use an injected fake prioritize function or --dry-run —
no Serper, Anthropic, or OpenAI calls anywhere.
"""

from __future__ import annotations

import pandas as pd
import pytest

from lead_output_schema import LeadPrioritizationResult
from compare_ai_providers_lead_prioritizer import (
    COMPARISON_COLUMNS,
    TRIPLE_COMPARISON_COLUMNS,
    TWO_WAY_COST_PROVIDERS,
    TRIPLE_COST_PROVIDERS,
    build_comparison_row,
    build_cost_totals,
    build_provider_cost_rows,
    build_triple_comparison_row,
    main,
    read_input_rows,
    run_comparison,
    run_triple_comparison,
    select_rows,
)


def _result(provider, **kw) -> LeadPrioritizationResult:
    base = dict(
        company_name="Acme", domain="acme.com", input_country="Brazil",
        ai_hq_provider=provider,
        ai_hq_model="claude-haiku-4-5-20251001" if provider == "anthropic"
        else "gpt-5.4-nano",
        ai_hq_classification="foreign_parent",
        ai_parent_hq_country="Germany",
        sig_foreign_hq_score_for_next_scoring=3.0,
        final_commercial_fit_score=7.5,
        commercial_tier="A",
    )
    base.update(kw)
    return LeadPrioritizationResult(**base)


class TestBuildComparisonRow:
    _input = {"company_name": "Acme", "domain": "acme.com",
              "input_country": "Brazil"}

    def test_matching_results(self):
        row = build_comparison_row(
            self._input, _result("anthropic"), _result("openai"))
        assert row["company_name"] == "Acme"
        assert row["classification_match"] is True
        assert row["score_delta"] == 0.0
        assert row["tier_delta"] == "same"
        assert row["anthropic_foreign_hq_score"] == 3.0
        assert row["openai_foreign_hq_score"] == 3.0

    def test_diverging_results(self):
        openai = _result(
            "openai", ai_hq_classification="domestic",
            ai_parent_hq_country="Brazil",
            sig_foreign_hq_score_for_next_scoring=0.0,
            final_commercial_fit_score=4.25, commercial_tier="C",
        )
        row = build_comparison_row(self._input, _result("anthropic"), openai)
        assert row["classification_match"] is False
        assert row["score_delta"] == -3.25
        assert row["tier_delta"] == "A -> C"
        assert row["openai_parent_hq_country"] == "Brazil"

    def test_missing_scores_leave_delta_blank(self):
        openai = _result("openai", final_commercial_fit_score=None,
                         commercial_tier=None)
        row = build_comparison_row(self._input, _result("anthropic"), openai)
        assert row["score_delta"] is None
        assert row["tier_delta"] == "A -> ?"

    def test_usage_and_cost_fields_pass_through(self):
        anthropic = _result(
            "anthropic", ai_hq_input_tokens=1000, ai_hq_output_tokens=100,
            ai_hq_total_tokens=1100, ai_hq_estimated_cost_usd=0.0015)
        row = build_comparison_row(self._input, anthropic, _result("openai"))
        assert row["anthropic_input_tokens"] == 1000
        assert row["anthropic_estimated_cost_usd"] == 0.0015
        # OpenAI usage unknown → blank, never guessed.
        assert row["openai_input_tokens"] is None
        assert row["openai_estimated_cost_usd"] is None

    def test_blank_classifications_never_match(self):
        a = _result("anthropic", ai_hq_classification=None)
        o = _result("openai", ai_hq_classification=None)
        row = build_comparison_row(self._input, a, o)
        assert row["classification_match"] is False


class TestRunComparison:
    def test_three_row_mocked_comparison(self):
        # Tiny 3-row mocked comparison — exercises the full loop without any
        # network calls.
        df = pd.DataFrame({
            "company_name": ["Alpha", "Beta", "Gamma"],
            "domain": ["alpha.com", "beta.com", "gamma.com"],
            "country": ["Brazil", "Brazil", "Uruguay"],
        })
        calls = []

        def _fake(lead, **kwargs):
            calls.append((lead.company_name, kwargs["ai_provider"],
                          kwargs["ai_model"]))
            return _result(kwargs["ai_provider"],
                           company_name=lead.company_name,
                           domain=lead.domain,
                           input_country=lead.input_country)

        out = run_comparison(
            df, company_column="company_name", domain_column="domain",
            country_column="country", anthropic_model="claude-haiku-4-5-20251001",
            openai_model="gpt-5.4-nano", prioritize_fn=_fake,
        )
        assert list(out.columns) == COMPARISON_COLUMNS
        assert len(out) == 3
        assert out["company_name"].tolist() == ["Alpha", "Beta", "Gamma"]
        # Each row runs once per provider with the right model.
        assert calls.count(("Alpha", "anthropic", "claude-haiku-4-5-20251001")) == 1
        assert calls.count(("Alpha", "openai", "gpt-5.4-nano")) == 1
        assert len(calls) == 6
        assert out["classification_match"].all()

    def test_scoring_flag_requested_but_no_enrichment_flags(self):
        df = pd.DataFrame({"company_name": ["Alpha"], "domain": ["alpha.com"]})
        captured = {}

        def _fake(lead, **kwargs):
            captured.update(kwargs)
            return _result(kwargs["ai_provider"])

        run_comparison(df, company_column="company_name",
                       domain_column="domain", prioritize_fn=_fake)
        assert captured["calculate_commercial_score_flag"] is True
        assert "run_full_v2_pipeline" not in captured
        assert "collect_non_hq_evidence" not in captured

    def test_default_openai_model_is_mini_not_nano(self):
        # OpenAI nano returned too many unclear cases in real-company
        # testing; the two-way compare now defaults to OpenAI mini.
        df = pd.DataFrame({"company_name": ["Alpha"], "domain": ["alpha.com"]})
        captured = {}

        def _fake(lead, **kwargs):
            if kwargs["ai_provider"] == "openai":
                captured.update(kwargs)
            return _result(kwargs["ai_provider"])

        run_comparison(df, company_column="company_name",
                       domain_column="domain", prioritize_fn=_fake)
        assert captured["ai_model"] == "gpt-5.4-mini"


class TestInputHandling:
    def test_reads_xlsx_and_csv(self, tmp_path):
        df = pd.DataFrame({"company_name": ["A"], "domain": ["a.com"]})
        xlsx = tmp_path / "in.xlsx"
        csv = tmp_path / "in.csv"
        df.to_excel(xlsx, index=False)
        df.to_csv(csv, index=False)
        assert read_input_rows(xlsx)["company_name"].tolist() == ["A"]
        assert read_input_rows(csv)["company_name"].tolist() == ["A"]

    def test_unsupported_extension_raises(self, tmp_path):
        path = tmp_path / "in.txt"
        path.write_text("x")
        with pytest.raises(ValueError):
            read_input_rows(path)

    def test_select_rows_start_and_limit(self):
        df = pd.DataFrame({"c": list(range(10))})
        assert select_rows(df, 2, 3)["c"].tolist() == [2, 3, 4]
        assert select_rows(df, 8, 0)["c"].tolist() == [8, 9]


class TestMainDryRun:
    def test_dry_run_makes_no_calls_and_writes_nothing(self, tmp_path, capsys):
        df = pd.DataFrame({
            "company_name": ["Alpha", "Beta"],
            "domain": ["alpha.com", "beta.com"],
        })
        xlsx = tmp_path / "in.xlsx"
        df.to_excel(xlsx, index=False)
        out = tmp_path / "out.xlsx"

        rc = main(["--input", str(xlsx), "--output-xlsx", str(out),
                   "--dry-run", "--row-limit", "5"])

        assert rc == 0
        assert not out.exists()
        text = capsys.readouterr().out
        assert "DRY RUN" in text
        assert "Alpha" in text and "Beta" in text
        assert "gpt-5.4-mini" in text

    def test_missing_env_keys_fail_cleanly(self, tmp_path, monkeypatch, capsys):
        for key in ("SERPER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        df = pd.DataFrame({"company_name": ["Alpha"], "domain": ["alpha.com"]})
        xlsx = tmp_path / "in.xlsx"
        df.to_excel(xlsx, index=False)

        rc = main(["--input", str(xlsx),
                   "--output-xlsx", str(tmp_path / "out.xlsx")])

        assert rc == 1
        assert "SERPER_API_KEY" in capsys.readouterr().err

    def test_missing_company_column_fails_cleanly(self, tmp_path, capsys):
        df = pd.DataFrame({"bedrijf": ["Alpha"]})
        xlsx = tmp_path / "in.xlsx"
        df.to_excel(xlsx, index=False)
        rc = main(["--input", str(xlsx),
                   "--output-xlsx", str(tmp_path / "out.xlsx"), "--dry-run"])
        assert rc == 1


class TestTripleComparisonColumns:
    def test_nano_columns_removed_deepseek_flash_columns_present(self):
        joined = " ".join(TRIPLE_COMPARISON_COLUMNS)
        assert "nano" not in joined
        assert any(col.startswith("deepseek_flash_") for col in TRIPLE_COMPARISON_COLUMNS)
        assert any(col.startswith("openai_mini_") for col in TRIPLE_COMPARISON_COLUMNS)
        assert any(col.startswith("anthropic_") for col in TRIPLE_COMPARISON_COLUMNS)


class TestBuildTripleComparisonRow:
    _input = {"company_name": "Acme", "domain": "acme.com", "input_country": "Brazil"}

    def test_three_way_columns_and_values(self):
        anthropic = _result("anthropic", ai_hq_model="claude-haiku-4-5-20251001")
        openai_mini = _result(
            "openai", ai_hq_model="gpt-5.4-mini",
            ai_hq_classification="domestic", ai_parent_hq_country="Brazil",
            final_commercial_fit_score=4.0, commercial_tier="C")
        deepseek_flash = _result("deepseek", ai_hq_model="deepseek-v4-flash")
        row = build_triple_comparison_row(
            self._input, anthropic, openai_mini, deepseek_flash)
        assert set(row.keys()) == set(TRIPLE_COMPARISON_COLUMNS)
        assert "openai_nano" not in " ".join(row.keys())
        assert row["anthropic_model"] == "claude-haiku-4-5-20251001"
        assert row["openai_mini_model"] == "gpt-5.4-mini"
        assert row["deepseek_flash_model"] == "deepseek-v4-flash"
        assert row["openai_mini_hq_classification"] == "domestic"
        assert row["openai_mini_parent_hq_country"] == "Brazil"
        assert row["deepseek_flash_hq_classification"] == "foreign_parent"


class TestRunTripleComparison:
    def test_three_calls_per_row(self):
        df = pd.DataFrame({
            "company_name": ["Alpha", "Beta"],
            "domain": ["alpha.com", "beta.com"],
        })
        calls = []

        def _fake(lead, **kwargs):
            calls.append((lead.company_name, kwargs["ai_provider"], kwargs["ai_model"]))
            return _result(kwargs["ai_provider"], ai_hq_model=kwargs["ai_model"],
                           company_name=lead.company_name, domain=lead.domain,
                           input_country=lead.input_country)

        out = run_triple_comparison(
            df, company_column="company_name", domain_column="domain",
            anthropic_model="claude-haiku-4-5-20251001",
            openai_mini_model="gpt-5.4-mini", deepseek_model="deepseek-v4-flash",
            prioritize_fn=_fake,
        )
        assert list(out.columns) == TRIPLE_COMPARISON_COLUMNS
        assert len(out) == 2
        assert calls.count(("Alpha", "anthropic", "claude-haiku-4-5-20251001")) == 1
        assert calls.count(("Alpha", "openai", "gpt-5.4-mini")) == 1
        assert calls.count(("Alpha", "deepseek", "deepseek-v4-flash")) == 1
        assert len(calls) == 6

    def test_passes_deepseek_api_key_through_to_prioritize_fn(self):
        df = pd.DataFrame({"company_name": ["Alpha"], "domain": ["alpha.com"]})
        captured = {}

        def _fake(lead, **kwargs):
            if kwargs["ai_provider"] == "deepseek":
                captured.update(kwargs)
            return _result(kwargs["ai_provider"], ai_hq_model=kwargs["ai_model"])

        run_triple_comparison(
            df, company_column="company_name", domain_column="domain",
            deepseek_api_key="fake-deepseek-key", prioritize_fn=_fake,
        )
        assert captured["deepseek_api_key"] == "fake-deepseek-key"


class TestCostSummary:
    def test_provider_cost_rows_and_totals(self):
        df = pd.DataFrame([{
            "anthropic_model": "claude-haiku-4-5-20251001",
            "anthropic_input_tokens": 1000, "anthropic_output_tokens": 100,
            "anthropic_total_tokens": 1100, "anthropic_estimated_cost_usd": 0.0015,
            "openai_mini_model": "gpt-5.4-mini",
            "openai_mini_input_tokens": 800, "openai_mini_output_tokens": 120,
            "openai_mini_total_tokens": 920, "openai_mini_estimated_cost_usd": 0.00114,
            "deepseek_flash_model": "deepseek-v4-flash",
            "deepseek_flash_input_tokens": 800, "deepseek_flash_output_tokens": 120,
            "deepseek_flash_total_tokens": 920, "deepseek_flash_estimated_cost_usd": 0.00015,
        }])
        rows = build_provider_cost_rows(df, TRIPLE_COST_PROVIDERS)
        by_provider = {r["provider"]: r for r in rows}
        assert by_provider["anthropic"]["estimated_cost_usd"] == 0.0015
        assert by_provider["anthropic"]["cost_per_company_usd"] == 0.0015
        assert by_provider["openai_mini"]["model"] == "gpt-5.4-mini"
        assert by_provider["deepseek_flash"]["model"] == "deepseek-v4-flash"

        totals = build_cost_totals(rows, len(df))
        assert totals["total_anthropic_cost_usd"] == 0.0015
        assert totals["total_openai_mini_cost_usd"] == 0.00114
        assert totals["total_deepseek_flash_cost_usd"] == 0.00015
        combined = 0.0015 + 0.00114 + 0.00015
        assert totals["estimated_cost_per_100_companies_usd"] == round(combined * 100, 2)
        assert totals["estimated_cost_per_1000_companies_usd"] == round(combined * 1000, 2)
        assert totals["estimated_cost_per_10000_companies_usd"] == round(combined * 10000, 2)

    def test_unpriced_or_missing_provider_never_crashes_and_stays_blank(self):
        df = pd.DataFrame([{"anthropic_model": "claude-haiku-4-5-20251001"}])
        rows = build_provider_cost_rows(df, TWO_WAY_COST_PROVIDERS)
        by_provider = {r["provider"]: r for r in rows}
        assert by_provider["openai"]["estimated_cost_usd"] is None
        assert by_provider["openai"]["input_tokens"] == 0
        totals = build_cost_totals(rows, len(df))
        assert totals["total_openai_cost_usd"] is None
        assert totals["estimated_cost_per_100_companies_usd"] is None
