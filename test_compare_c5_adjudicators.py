"""Tests for compare_c5_adjudicators (C5 comparison-only probe).

All provider calls are mocked or injected — no live Anthropic/OpenAI/DeepSeek
calls anywhere. Confirms:
- Sonnet, OpenAI mini, and DeepSeek Pro results all normalize into the same
  SonnetHQAdjudicationResult / build_c5_recommendation shape.
- production adjudicate_hq_with_sonnet behavior is exercised unchanged
  (mocked _anthropic_lib, same as test_lead_hq_sonnet_adjudicator.py).
- dry-run makes no API calls; row-limit is respected.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from lead_hq_sonnet_adjudicator import (
    DEFAULT_SONNET_ADJUDICATION_MODEL,
    SonnetHQAdjudicationResult,
    adjudicate_hq_with_sonnet,
    build_c5_recommendation,
)
from compare_c5_adjudicators import (
    BASE_COLUMNS,
    C5_COMPARISON_COLUMNS,
    COST_SUMMARY_COLUMNS,
    DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_PRO_MODEL,
    DEFAULT_OPENAI_MINI_MODEL,
    DEFAULT_ROW_LIMIT,
    PROVIDER_PREFIXES,
    adjudicate_with_openai_compatible,
    build_c5_comparison_row,
    build_c5_cost_summary,
    main,
    read_c5_input_workbook,
    resolve_c5_input_row,
    run_c5_comparison,
    select_rows,
)


def _fake_result(**kw) -> SonnetHQAdjudicationResult:
    base = dict(
        adjudication="foreign_parent_confirmed", confidence="High",
        target_company_match="yes", parent_company="Acme Group",
        parent_hq_country="Germany", parent_hq_city="Munich",
        reason="fake reason", model="fake-model",
        call_attempted=True, call_success=True,
    )
    base.update(kw)
    return SonnetHQAdjudicationResult(**base)


def _fake_sonnet_fn(**kw):
    return _fake_result(model=kw.get("model", DEFAULT_SONNET_ADJUDICATION_MODEL))


def _fake_openai_compatible_fn(**kw):
    result = _fake_result(model=kw.get("model", ""))
    usage = {"input_tokens": 500, "output_tokens": 80, "total_tokens": 580,
             "estimated_cost_usd": 0.001}
    return result, usage


# ---------------------------------------------------------------------------
# resolve_c5_input_row
# ---------------------------------------------------------------------------

class TestResolveC5InputRow:
    def test_normal_enriched_leads_row(self):
        row = {
            "company_name": "Acme Brasil", "domain": "acme.com.br",
            "input_country": "Brazil", "hq_detected_country": "Germany",
            "hq_detected_city": "Munich", "ai_parent_company": "Acme Group",
            "ai_parent_hq_country": "Germany", "ai_parent_hq_city": "Munich",
            "hq_evidence_url": "https://acme.com/about",
            "hq_evidence_quote": "part of Acme Group",
            "hq_reason": "foreign_parent (High): ...",
        }
        base = resolve_c5_input_row(row)
        assert base["company_name"] == "Acme Brasil"
        assert base["domain"] == "acme.com.br"
        assert base["ai_parent_hq_country"] == "Germany"
        assert base["hq_reason"] == "foreign_parent (High): ..."

    def test_provider_comparison_workbook_equivalents(self):
        row = {
            "company_name": "Acme Brasil", "domain": "acme.com.br",
            "input_country": "Brazil",
            "anthropic_parent_hq_country": "Germany",
            "anthropic_hq_classification": "foreign_parent",
            "anthropic_ai_error": "",
        }
        base = resolve_c5_input_row(row)
        assert base["ai_parent_hq_country"] == "Germany"
        assert "foreign_parent" in base["hq_reason"]

    def test_missing_fields_become_blank_not_crash(self):
        base = resolve_c5_input_row({"company_name": "X", "domain": "x.com"})
        assert base["hq_detected_country"] == ""
        assert base["ai_parent_hq_country"] == ""
        assert base["hq_reason"] == ""

    def test_custom_column_names(self):
        row = {"Company": "Acme", "Domain": "acme.com", "Country": "Italy"}
        base = resolve_c5_input_row(
            row, company_column="Company", domain_column="Domain",
            country_column="Country")
        assert base["company_name"] == "Acme"
        assert base["domain"] == "acme.com"
        assert base["input_country"] == "Italy"

    def test_nan_like_values_treated_as_blank(self):
        row = {"company_name": "Acme", "domain": "acme.com",
              "hq_detected_country": float("nan")}
        base = resolve_c5_input_row(row)
        assert base["hq_detected_country"] == ""


# ---------------------------------------------------------------------------
# adjudicate_with_openai_compatible (OpenAI mini / DeepSeek Pro)
# ---------------------------------------------------------------------------

def _mock_openai_lib(content: str, prompt_tokens=500, completion_tokens=80):
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=content))]
    mock_resp.usage = MagicMock(prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=prompt_tokens + completion_tokens)
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp
    mock_lib = MagicMock()
    mock_lib.OpenAI.return_value = mock_client
    return patch("compare_c5_adjudicators._openai_lib", mock_lib), mock_client


class TestAdjudicateWithOpenAICompatible:
    _ai_json = json.dumps({
        "adjudication": "foreign_parent_confirmed", "confidence": "High",
        "target_company_match": "yes", "parent_company": "Acme Group",
        "parent_hq_country": "Germany", "parent_hq_city": "Munich",
        "reason": "Confirmed via mocked call.",
    })

    def test_openai_mini_parses_into_sonnet_result_shape(self):
        patcher, _ = _mock_openai_lib(self._ai_json)
        with patcher:
            result, usage = adjudicate_with_openai_compatible(
                company_name="Acme Brasil", domain="acme.com.br",
                input_country="Brazil", api_key="fake-openai",
                model=DEFAULT_OPENAI_MINI_MODEL,
            )
        assert isinstance(result, SonnetHQAdjudicationResult)
        assert result.adjudication == "foreign_parent_confirmed"
        assert result.confidence == "High"
        assert result.target_company_match == "yes"
        assert result.parent_hq_country == "Germany"
        assert result.call_success is True
        assert usage["input_tokens"] == 500
        assert usage["output_tokens"] == 80
        assert usage["total_tokens"] == 580

    def test_deepseek_pro_parses_into_same_shape(self):
        patcher, mock_client = _mock_openai_lib(self._ai_json)
        with patcher:
            result, usage = adjudicate_with_openai_compatible(
                company_name="Acme Brasil", domain="acme.com.br",
                input_country="Brazil", api_key="fake-deepseek",
                model=DEFAULT_DEEPSEEK_PRO_MODEL, base_url=DEEPSEEK_BASE_URL,
                max_tokens_kwarg="max_tokens",
            )
        assert isinstance(result, SonnetHQAdjudicationResult)
        assert result.adjudication == "foreign_parent_confirmed"
        assert result.model == DEFAULT_DEEPSEEK_PRO_MODEL
        assert usage["total_tokens"] == 580

    def test_deepseek_uses_base_url_and_max_tokens_kwarg(self):
        patcher, mock_client = _mock_openai_lib(self._ai_json)
        mock_lib = patcher
        with patch("compare_c5_adjudicators._openai_lib") as lib:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock(message=MagicMock(content=self._ai_json))]
            mock_resp.usage = None
            mock_client2 = MagicMock()
            mock_client2.chat.completions.create.return_value = mock_resp
            lib.OpenAI.return_value = mock_client2

            adjudicate_with_openai_compatible(
                company_name="Acme", domain="acme.com", input_country="Brazil",
                api_key="fake", model=DEFAULT_DEEPSEEK_PRO_MODEL,
                base_url=DEEPSEEK_BASE_URL, max_tokens_kwarg="max_tokens",
            )
            lib.OpenAI.assert_called_once_with(
                api_key="fake", base_url=DEEPSEEK_BASE_URL)
            _, call_kwargs = mock_client2.chat.completions.create.call_args
            assert "max_tokens" in call_kwargs
            assert "max_completion_tokens" not in call_kwargs

    def test_missing_api_key_no_call_attempted(self):
        result, usage = adjudicate_with_openai_compatible(
            company_name="Acme", domain="acme.com", input_country="Brazil",
            api_key="", model=DEFAULT_OPENAI_MINI_MODEL,
        )
        assert result.call_attempted is False
        assert result.error == "no_api_key"
        assert usage["input_tokens"] is None
        assert usage["estimated_cost_usd"] is None

    def test_call_failure_isolated(self):
        with patch("compare_c5_adjudicators._openai_lib") as lib:
            lib.OpenAI.side_effect = RuntimeError("boom")
            result, usage = adjudicate_with_openai_compatible(
                company_name="Acme", domain="acme.com", input_country="Brazil",
                api_key="fake", model=DEFAULT_OPENAI_MINI_MODEL,
            )
        assert result.call_attempted is True
        assert result.call_success is False
        assert "call_failed" in result.error

    def test_unparseable_response(self):
        patcher, _ = _mock_openai_lib("not json at all")
        with patcher:
            result, usage = adjudicate_with_openai_compatible(
                company_name="Acme", domain="acme.com", input_country="Brazil",
                api_key="fake", model=DEFAULT_OPENAI_MINI_MODEL,
            )
        assert result.call_success is False
        assert result.error == "parse_failed"

    def test_cost_estimated_for_priced_openai_mini(self):
        patcher, _ = _mock_openai_lib(self._ai_json, prompt_tokens=1000,
                                       completion_tokens=100)
        with patcher:
            _, usage = adjudicate_with_openai_compatible(
                company_name="Acme", domain="acme.com", input_country="Brazil",
                api_key="fake", model=DEFAULT_OPENAI_MINI_MODEL,
            )
        # gpt-5.4-mini is priced in MODEL_PRICING_USD_PER_MTOK.
        assert usage["estimated_cost_usd"] is not None

    def test_cost_estimated_for_priced_deepseek_pro(self):
        patcher, _ = _mock_openai_lib(self._ai_json)
        with patcher:
            _, usage = adjudicate_with_openai_compatible(
                company_name="Acme", domain="acme.com", input_country="Brazil",
                api_key="fake", model=DEFAULT_DEEPSEEK_PRO_MODEL,
                base_url=DEEPSEEK_BASE_URL,
            )
        # deepseek-v4-pro is priced in MODEL_PRICING_USD_PER_MTOK.
        assert usage["estimated_cost_usd"] is not None

    def test_cost_blank_for_unpriced_model(self):
        patcher, _ = _mock_openai_lib(self._ai_json)
        with patcher:
            _, usage = adjudicate_with_openai_compatible(
                company_name="Acme", domain="acme.com", input_country="Brazil",
                api_key="fake", model="some-unpriced-model",
            )
        # A model absent from MODEL_PRICING_USD_PER_MTOK → blank, never guessed.
        assert usage["estimated_cost_usd"] is None
        assert usage["input_tokens"] is not None  # tokens still recorded
        assert usage["input_tokens"] is not None  # tokens still recorded


# ---------------------------------------------------------------------------
# Production Sonnet adjudicator — must remain unchanged (mocked _anthropic_lib)
# ---------------------------------------------------------------------------

class TestProductionSonnetUnchanged:
    def _mock_anthropic(self, text):
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        client = MagicMock()
        client.messages.create.return_value = msg
        lib = MagicMock()
        lib.Anthropic.return_value = client
        return patch("lead_hq_sonnet_adjudicator._anthropic_lib", lib)

    def test_adjudicate_hq_with_sonnet_still_works_as_before(self):
        ai_json = json.dumps({
            "adjudication": "foreign_parent_confirmed", "confidence": "High",
            "target_company_match": "yes", "parent_company": "Acme Group",
            "parent_hq_country": "Germany", "parent_hq_city": "Munich",
            "reason": "Confirmed.",
        })
        with self._mock_anthropic(ai_json):
            result = adjudicate_hq_with_sonnet(
                company_name="Acme Brasil", domain="acme.com.br",
                input_country="Brazil", anthropic_api_key="fake",
            )
        assert result.adjudication == "foreign_parent_confirmed"
        assert result.model == DEFAULT_SONNET_ADJUDICATION_MODEL
        rec = build_c5_recommendation(result)
        assert rec["c5_recommended_hq_score"] == 3.0

    def test_no_usage_fields_added_to_sonnet_result(self):
        # SonnetHQAdjudicationResult must not have gained new fields as a
        # side effect of this comparison script.
        result = SonnetHQAdjudicationResult()
        assert not hasattr(result, "input_tokens")
        assert not hasattr(result, "estimated_cost_usd")


# ---------------------------------------------------------------------------
# build_c5_comparison_row / run_c5_comparison
# ---------------------------------------------------------------------------

class TestBuildC5ComparisonRow:
    def test_all_three_provider_prefixes_present(self):
        sonnet = _fake_result(model="claude-sonnet-5")
        mini = _fake_result(model="gpt-5.4-mini")
        deepseek = _fake_result(model="deepseek-v4-pro")
        rec = build_c5_recommendation(sonnet)
        row = build_c5_comparison_row(
            0, {"company_name": "Acme", "domain": "acme.com"},
            sonnet, rec, {"input_tokens": None, "output_tokens": None,
                         "total_tokens": None, "estimated_cost_usd": None},
            mini, rec, {"input_tokens": 1, "output_tokens": 2,
                       "total_tokens": 3, "estimated_cost_usd": 0.01},
            deepseek, rec, {"input_tokens": 1, "output_tokens": 2,
                           "total_tokens": 3, "estimated_cost_usd": None},
        )
        for prefix in PROVIDER_PREFIXES:
            assert f"{prefix}_adjudication" in row
            assert f"{prefix}_model" in row
            assert f"{prefix}_estimated_cost_usd" in row

    def test_agreement_flags_when_all_match(self):
        r = _fake_result()
        rec = build_c5_recommendation(r)
        usage = {"input_tokens": None, "output_tokens": None,
                "total_tokens": None, "estimated_cost_usd": None}
        row = build_c5_comparison_row(
            0, {"company_name": "Acme", "domain": "acme.com"},
            r, rec, usage, r, rec, usage, r, rec, usage,
        )
        assert row["any_adjudication_disagreement"] is False
        assert row["any_score_disagreement"] is False
        assert row["any_parent_country_disagreement"] is False
        assert row["sonnet_openai_mini_match"] is True

    def test_disagreement_flags_when_providers_diverge(self):
        sonnet = _fake_result(adjudication="foreign_parent_confirmed",
                              parent_hq_country="Germany")
        mini = _fake_result(adjudication="domestic_confirmed",
                            parent_hq_country="Brazil")
        deepseek = _fake_result(adjudication="unclear", parent_hq_country="")
        usage = {"input_tokens": None, "output_tokens": None,
                "total_tokens": None, "estimated_cost_usd": None}
        row = build_c5_comparison_row(
            0, {"company_name": "Acme", "domain": "acme.com"},
            sonnet, build_c5_recommendation(sonnet), usage,
            mini, build_c5_recommendation(mini), usage,
            deepseek, build_c5_recommendation(deepseek), usage,
        )
        assert row["any_adjudication_disagreement"] is True
        assert row["any_score_disagreement"] is True
        assert row["any_parent_country_disagreement"] is True
        assert row["sonnet_openai_mini_match"] is False


class TestRunC5Comparison:
    def test_output_columns_include_all_provider_prefixes(self):
        df = pd.DataFrame([{"company_name": "Acme", "domain": "acme.com",
                            "input_country": "Brazil"}])
        out = run_c5_comparison(
            df, sonnet_fn=_fake_sonnet_fn,
            openai_compatible_fn=_fake_openai_compatible_fn,
        )
        assert list(out.columns) == C5_COMPARISON_COLUMNS
        for prefix in PROVIDER_PREFIXES:
            assert f"{prefix}_adjudication" in out.columns
            assert f"{prefix}_input_tokens" in out.columns
            assert f"{prefix}_estimated_cost_usd" in out.columns

    def test_sonnet_cost_columns_always_blank(self):
        df = pd.DataFrame([{"company_name": "Acme", "domain": "acme.com"}])
        out = run_c5_comparison(
            df, sonnet_fn=_fake_sonnet_fn,
            openai_compatible_fn=_fake_openai_compatible_fn,
        )
        assert out.loc[0, "sonnet_input_tokens"] is None
        assert out.loc[0, "sonnet_estimated_cost_usd"] is None
        # Other providers DO have tokens from the fake fn.
        assert out.loc[0, "openai_mini_input_tokens"] == 500
        assert out.loc[0, "deepseek_pro_input_tokens"] == 500

    def test_row_count_matches_input(self):
        df = pd.DataFrame([
            {"company_name": "A", "domain": "a.com"},
            {"company_name": "B", "domain": "b.com"},
            {"company_name": "C", "domain": "c.com"},
        ])
        out = run_c5_comparison(
            df, sonnet_fn=_fake_sonnet_fn,
            openai_compatible_fn=_fake_openai_compatible_fn,
        )
        assert len(out) == 3
        assert out["company_name"].tolist() == ["A", "B", "C"]

    def test_each_provider_called_once_per_row(self):
        calls = {"sonnet": 0, "openai_compatible": 0}

        def _sonnet(**kw):
            calls["sonnet"] += 1
            return _fake_result()

        def _oc(**kw):
            calls["openai_compatible"] += 1
            return _fake_openai_compatible_fn(**kw)

        df = pd.DataFrame([{"company_name": "A", "domain": "a.com"},
                           {"company_name": "B", "domain": "b.com"}])
        run_c5_comparison(df, sonnet_fn=_sonnet, openai_compatible_fn=_oc)
        assert calls["sonnet"] == 2
        assert calls["openai_compatible"] == 4  # 2 rows x (mini + deepseek)


# ---------------------------------------------------------------------------
# Cost summary
# ---------------------------------------------------------------------------

class TestBuildC5CostSummary:
    def test_columns_and_providers(self):
        df = pd.DataFrame([{"company_name": "Acme", "domain": "acme.com",
                            "input_country": "Brazil"}])
        comparison = run_c5_comparison(
            df, sonnet_fn=_fake_sonnet_fn,
            openai_compatible_fn=_fake_openai_compatible_fn,
        )
        summary = build_c5_cost_summary(comparison)
        assert list(summary.columns) == COST_SUMMARY_COLUMNS
        assert set(summary["provider"]) == set(PROVIDER_PREFIXES)
        assert len(summary) == 3

    def test_sonnet_cost_blank_others_priced_for_openai_mini(self):
        df = pd.DataFrame([{"company_name": "Acme", "domain": "acme.com"}])
        comparison = run_c5_comparison(
            df, sonnet_fn=_fake_sonnet_fn,
            openai_compatible_fn=_fake_openai_compatible_fn,
        )
        summary = build_c5_cost_summary(comparison).set_index("provider")
        assert pd.isna(summary.loc["sonnet", "estimated_cost_usd"])
        assert summary.loc["openai_mini", "estimated_cost_usd"] == 0.001
        assert summary.loc["deepseek_pro", "estimated_cost_usd"] == 0.001

    def test_projection_columns_computed_when_cost_known(self):
        df = pd.DataFrame([{"company_name": "Acme", "domain": "acme.com"}])
        comparison = run_c5_comparison(
            df, sonnet_fn=_fake_sonnet_fn,
            openai_compatible_fn=_fake_openai_compatible_fn,
        )
        summary = build_c5_cost_summary(comparison).set_index("provider")
        row = summary.loc["openai_mini"]
        assert row["estimated_cost_100_companies_usd"] == round(0.001 * 100, 2)
        assert row["estimated_cost_1000_companies_usd"] == round(0.001 * 1000, 2)
        assert row["estimated_cost_10000_companies_usd"] == round(0.001 * 10000, 2)

    def test_rows_compared_matches_input_length(self):
        df = pd.DataFrame([
            {"company_name": "A", "domain": "a.com"},
            {"company_name": "B", "domain": "b.com"},
        ])
        comparison = run_c5_comparison(
            df, sonnet_fn=_fake_sonnet_fn,
            openai_compatible_fn=_fake_openai_compatible_fn,
        )
        summary = build_c5_cost_summary(comparison)
        assert (summary["rows_compared"] == 2).all()


# ---------------------------------------------------------------------------
# Input handling / row selection
# ---------------------------------------------------------------------------

class TestInputHandling:
    def test_reads_enriched_leads_sheet_when_present(self, tmp_path):
        path = tmp_path / "in.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame([{"company_name": "A", "domain": "a.com"}]).to_excel(
                writer, sheet_name="Enriched Leads", index=False)
            pd.DataFrame([{"x": 1}]).to_excel(
                writer, sheet_name="Other", index=False)
        df, sheet_used = read_c5_input_workbook(path)
        assert sheet_used == "Enriched Leads"
        assert df["company_name"].tolist() == ["A"]

    def test_falls_back_to_first_sheet_without_enriched_leads(self, tmp_path):
        path = tmp_path / "in.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame([{"company_name": "A", "domain": "a.com"}]).to_excel(
                writer, sheet_name="Comparison", index=False)
        df, sheet_used = read_c5_input_workbook(path)
        assert sheet_used == "Comparison"

    def test_explicit_sheet_argument_used_when_present(self, tmp_path):
        path = tmp_path / "in.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame([{"company_name": "A"}]).to_excel(
                writer, sheet_name="Enriched Leads", index=False)
            pd.DataFrame([{"company_name": "B"}]).to_excel(
                writer, sheet_name="Custom", index=False)
        df, sheet_used = read_c5_input_workbook(path, sheet="Custom")
        assert sheet_used == "Custom"
        assert df["company_name"].tolist() == ["B"]

    def test_select_rows_respects_start_and_limit(self):
        df = pd.DataFrame({"c": list(range(10))})
        assert select_rows(df, 2, 3)["c"].tolist() == [2, 3, 4]
        assert select_rows(df, 0, DEFAULT_ROW_LIMIT)["c"].tolist() == [0, 1, 2, 3, 4]
        assert select_rows(df, 8, 0)["c"].tolist() == [8, 9]


# ---------------------------------------------------------------------------
# CLI: dry-run and row-limit enforcement
# ---------------------------------------------------------------------------

class TestMainDryRun:
    def _write_workbook(self, tmp_path, n_rows=8):
        path = tmp_path / "in.xlsx"
        df = pd.DataFrame([
            {"company_name": f"Co{i}", "domain": f"co{i}.com",
             "input_country": "Brazil"}
            for i in range(n_rows)
        ])
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Enriched Leads", index=False)
        return path

    def test_dry_run_makes_no_api_calls_and_writes_nothing(self, tmp_path, capsys):
        input_path = self._write_workbook(tmp_path)
        out_path = tmp_path / "out.xlsx"

        with patch("compare_c5_adjudicators._openai_lib") as lib, \
             patch("lead_hq_sonnet_adjudicator._anthropic_lib") as alib:
            rc = main(["--input", str(input_path), "--output-xlsx", str(out_path),
                      "--dry-run", "--row-limit", "3"])
            lib.OpenAI.assert_not_called()
            alib.Anthropic.assert_not_called()

        assert rc == 0
        assert not out_path.exists()
        text = capsys.readouterr().out
        assert "DRY RUN" in text
        assert "sonnet_model" in text
        assert "deepseek-v4-pro" in text
        assert text.count("Co") == 3  # only 3 rows printed (row-limit)

    def test_dry_run_default_row_limit_is_five(self, tmp_path, capsys):
        input_path = self._write_workbook(tmp_path, n_rows=8)
        out_path = tmp_path / "out.xlsx"
        rc = main(["--input", str(input_path), "--output-xlsx", str(out_path),
                  "--dry-run"])
        assert rc == 0
        text = capsys.readouterr().out
        assert "rows_selected: 5" in text

    def test_missing_env_keys_fail_cleanly(self, tmp_path, monkeypatch, capsys):
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        input_path = self._write_workbook(tmp_path)
        rc = main(["--input", str(input_path),
                  "--output-xlsx", str(tmp_path / "out.xlsx")])
        assert rc == 1
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err

    def test_row_limit_respected_in_real_run(self, tmp_path, monkeypatch):
        input_path = self._write_workbook(tmp_path, n_rows=8)
        out_path = tmp_path / "out.xlsx"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        monkeypatch.setenv("OPENAI_API_KEY", "fake")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")

        with patch("compare_c5_adjudicators.run_c5_comparison") as fake_run:
            fake_run.return_value = pd.DataFrame(
                [{"any_adjudication_disagreement": False}])
            main(["--input", str(input_path), "--output-xlsx", str(out_path),
                 "--row-limit", "2"])
            called_df = fake_run.call_args[0][0]
            assert len(called_df) == 2

    def test_output_workbook_has_both_sheets(self, tmp_path, monkeypatch):
        input_path = self._write_workbook(tmp_path, n_rows=1)
        out_path = tmp_path / "out.xlsx"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        monkeypatch.setenv("OPENAI_API_KEY", "fake")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")

        with patch("compare_c5_adjudicators.run_c5_comparison") as fake_run:
            fake_run.return_value = run_c5_comparison(
                pd.DataFrame([{"company_name": "A", "domain": "a.com"}]),
                sonnet_fn=_fake_sonnet_fn,
                openai_compatible_fn=_fake_openai_compatible_fn,
            )
            rc = main(["--input", str(input_path), "--output-xlsx", str(out_path)])

        assert rc == 0
        xls = pd.ExcelFile(out_path)
        assert "C5 Comparison" in xls.sheet_names
        assert "Cost Summary" in xls.sheet_names
