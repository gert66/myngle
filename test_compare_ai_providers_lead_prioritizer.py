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
    build_comparison_row,
    main,
    read_input_rows,
    run_comparison,
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
        assert "gpt-5.4-nano" in text

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
