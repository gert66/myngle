"""Tests for the C5 Sonnet HQ adjudication probe (no live API calls).

The Anthropic client is mocked at the module level; the adjudicator and its
recommendation logic are otherwise pure.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import lead_hq_sonnet_adjudicator as c5
from lead_hq_sonnet_adjudicator import (
    adjudicate_hq_with_sonnet,
    build_adjudication_prompt,
    build_c5_recommendation,
    extract_anthropic_text,
    SonnetHQAdjudicationResult,
    DEFAULT_SONNET_ADJUDICATION_MODEL,
)


class _Block:
    """Minimal stand-in for an Anthropic content block object."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ThinkingBlock:
    """A block with NO .text attribute (like a real ThinkingBlock)."""
    def __init__(self, thinking=""):
        self.thinking = thinking
        self.type = "thinking"


class _Resp:
    def __init__(self, content):
        self.content = content


def _mock_anthropic(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("lead_hq_sonnet_adjudicator._anthropic_lib", lib)


def _adj(text, **kw):
    base = dict(company_name="FIAP", domain="fiap.com.br", input_country="Brazil",
                anthropic_api_key="fake")
    base.update(kw)
    with _mock_anthropic(text):
        return adjudicate_hq_with_sonnet(**base)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_clean_json_foreign_parent(self):
        text = ('{"adjudication":"foreign_parent_confirmed","confidence":"High",'
                '"target_company_match":"yes","parent_company":"Acme AG",'
                '"parent_hq_country":"Germany","parent_hq_city":"Munich","reason":"r"}')
        r = _adj(text)
        assert r.adjudication == "foreign_parent_confirmed"
        assert r.confidence == "High"
        assert r.target_company_match == "yes"
        assert r.parent_hq_country == "Germany"
        assert r.call_success is True and r.call_attempted is True

    def test_fenced_json(self):
        text = ('```json\n{"adjudication":"domestic_confirmed","confidence":"High",'
                '"target_company_match":"yes","parent_hq_country":"Brasil"}\n```')
        r = _adj(text)
        assert r.adjudication == "domestic_confirmed"
        assert r.parent_hq_country == "Brasil"

    def test_prose_around_json(self):
        text = ('Here is my answer:\n{"adjudication":"unclear","confidence":"Low",'
                '"target_company_match":"no"}\nHope this helps.')
        r = _adj(text)
        assert r.adjudication == "unclear"
        assert r.target_company_match == "no"

    def test_regex_fallback_on_truncated_json(self):
        # Unterminated reason string → json.loads fails → regex fallback.
        text = ('{"adjudication":"foreign_parent_confirmed","confidence":"Medium",'
                '"target_company_match":"yes","parent_hq_country":"Japan",'
                '"reason":"controlled by a Japanese parent but the text is cut')
        r = _adj(text)
        assert r.adjudication == "foreign_parent_confirmed"
        assert r.confidence == "Medium"
        assert r.target_company_match == "yes"
        assert r.parent_hq_country == "Japan"

    def test_no_api_key_returns_unclear_not_attempted(self):
        r = adjudicate_hq_with_sonnet(
            company_name="FIAP", domain="fiap.com.br", input_country="Brazil",
            anthropic_api_key="")
        assert r.adjudication == "unclear"
        assert r.call_attempted is False
        assert r.call_success is False
        assert r.error == "no_anthropic_api_key"

    def test_parse_failure_returns_manual_safe(self):
        r = _adj("I cannot determine this from the given information.")
        assert r.adjudication == "unclear"
        assert r.call_attempted is True
        assert r.call_success is False
        assert r.error == "sonnet_parse_failed"


# ---------------------------------------------------------------------------
# extract_anthropic_text — robust content-block handling (Sonnet 5 thinking)
# ---------------------------------------------------------------------------

class TestExtractAnthropicText:
    def test_thinking_then_text_objects(self):
        resp = _Resp([_ThinkingBlock("reasoning..."),
                      _Block(type="text", text='{"adjudication":"unclear"}')])
        assert extract_anthropic_text(resp) == '{"adjudication":"unclear"}'

    def test_dict_thinking_then_dict_text(self):
        resp = _Resp([
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": '{"adjudication":"domestic_confirmed"}'},
        ])
        assert extract_anthropic_text(resp) == '{"adjudication":"domestic_confirmed"}'

    def test_plain_string_content(self):
        assert extract_anthropic_text(_Resp('{"adjudication":"unclear"}')) \
            == '{"adjudication":"unclear"}'

    def test_no_text_block_returns_empty(self):
        # Only a thinking block, no text → safe empty string (no crash).
        assert extract_anthropic_text(_Resp([_ThinkingBlock("only thinking")])) == ""
        assert extract_anthropic_text(_Resp([{"type": "thinking", "thinking": "x"}])) == ""

    def test_multiple_text_blocks_concatenated(self):
        resp = _Resp([_Block(type="text", text='{"a":1'),
                      _Block(type="text", text=',"b":2}')])
        assert extract_anthropic_text(resp) == '{"a":1,"b":2}'

    def test_none_content_returns_empty(self):
        assert extract_anthropic_text(_Resp(None)) == ""

    def test_leading_thinking_does_not_crash_adjudicate(self):
        # Full adjudicate path: client returns thinking block first, then text.
        payload = ('{"adjudication":"foreign_parent_confirmed","confidence":"High",'
                   '"target_company_match":"yes","parent_hq_country":"Japan"}')
        resp = _Resp([_ThinkingBlock("let me reason"),
                      _Block(type="text", text=payload)])
        client = MagicMock()
        client.messages.create.return_value = resp
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("lead_hq_sonnet_adjudicator._anthropic_lib", lib):
            r = adjudicate_hq_with_sonnet(
                company_name="X", domain="x.com", input_country="Brazil",
                anthropic_api_key="fake")
        assert r.call_success is True
        assert r.adjudication == "foreign_parent_confirmed"
        assert r.parent_hq_country == "Japan"

    def test_only_thinking_block_yields_parse_failure(self):
        # No text block at all → empty raw → parse fails → manual-safe unclear.
        resp = _Resp([_ThinkingBlock("only thinking, no answer")])
        client = MagicMock()
        client.messages.create.return_value = resp
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("lead_hq_sonnet_adjudicator._anthropic_lib", lib):
            r = adjudicate_hq_with_sonnet(
                company_name="X", domain="x.com", input_country="Brazil",
                anthropic_api_key="fake")
        assert r.call_success is False
        assert r.error == "sonnet_parse_failed"
        assert r.adjudication == "unclear"


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------

class TestRecommendation:
    def test_foreign_high_target_yes_scores_3(self):
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="foreign_parent_confirmed", confidence="High",
            target_company_match="yes"))
        assert rec["c5_recommended_hq_score"] == 3.0
        assert rec["c5_recommended_manual_review"] is False

    def test_foreign_medium_target_yes_scores_3(self):
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="foreign_parent_confirmed", confidence="Medium",
            target_company_match="yes"))
        assert rec["c5_recommended_hq_score"] == 3.0

    def test_domestic_scores_0_no_review(self):
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="domestic_confirmed", confidence="High",
            target_company_match="yes"))
        assert rec["c5_recommended_hq_score"] == 0.0
        assert rec["c5_recommended_manual_review"] is False

    def test_unclear_scores_0_review(self):
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="unclear", confidence="Low", target_company_match="unclear"))
        assert rec["c5_recommended_hq_score"] == 0.0
        assert rec["c5_recommended_manual_review"] is True

    def test_target_no_scores_0_review(self):
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="foreign_parent_confirmed", confidence="High",
            target_company_match="no"))
        assert rec["c5_recommended_hq_score"] == 0.0
        assert rec["c5_recommended_manual_review"] is True

    def test_foreign_low_confidence_scores_0_review(self):
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="foreign_parent_confirmed", confidence="Low",
            target_company_match="yes"))
        assert rec["c5_recommended_hq_score"] == 0.0
        assert rec["c5_recommended_manual_review"] is True


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

class TestPrompt:
    def test_prompt_contains_identity_and_guard(self):
        p = build_adjudication_prompt(
            company_name="FIAP", domain="fiap.com.br", input_country="Brazil")
        assert "FIAP" in p
        assert "fiap.com.br" in p
        assert "Brazil" in p
        # target-identity guard against same-name companies
        assert "same-name" in p.lower() or "different, same-name" in p.lower() \
            or "same name" in p.lower()
        assert "do not guess" in p.lower()

    def test_prompt_contains_control_vs_investment_guard(self):
        p = build_adjudication_prompt(
            company_name="Sólides", domain="solides.com.br", input_country="Brazil")
        # Normalize whitespace so wrapped phrases still match.
        low = " ".join(p.lower().split())
        # investment/funding/minority-stake alone is NOT a foreign parent
        assert "venture capital" in low
        assert "private equity" in low
        assert "minority stake" in low
        assert "funding round" in low
        assert "control, not investment" in low or "not investment" in low
        # confirmation requires control / majority ownership / subsidiary
        assert "controlled by" in low
        assert "majority-owned" in low or "majority owned" in low
        assert "subsidiary of" in low


class TestControlVsInvestment:
    """Investment/backing alone must not yield a positive HQ recommendation."""

    def test_investment_only_response_is_unclear_not_score_3(self):
        # The model, following the refined prompt, returns unclear for an
        # investment-only (Warburg Pincus) situation → score 0 + manual review.
        text = ('{"adjudication":"unclear","confidence":"Low",'
                '"target_company_match":"yes","parent_company":"Warburg Pincus",'
                '"parent_hq_country":"United States",'
                '"reason":"Warburg Pincus invested in the company but control '
                'is not established"}')
        r = _adj(text, company_name="Sólides", domain="solides.com.br")
        assert r.adjudication == "unclear"
        rec = build_c5_recommendation(r)
        assert rec["c5_recommended_hq_score"] == 0.0
        assert rec["c5_recommended_manual_review"] is True

    def test_unclear_investment_recommendation_is_manual_review(self):
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="unclear", confidence="Low", target_company_match="yes",
            parent_company="Some PE Fund", reason="minority stake only"))
        assert rec["c5_recommended_hq_score"] == 0.0
        assert rec["c5_recommended_manual_review"] is True

    def test_explicit_confirm_still_scores_3(self):
        # Score 3 requires the JSON to explicitly say foreign_parent_confirmed +
        # High/Medium + target yes (recommendation logic unchanged).
        rec = build_c5_recommendation(SonnetHQAdjudicationResult(
            adjudication="foreign_parent_confirmed", confidence="High",
            target_company_match="yes", parent_company="Nissan Motor Co."))
        assert rec["c5_recommended_hq_score"] == 3.0
        assert rec["c5_recommended_manual_review"] is False

    def test_parse_failure_stays_manual_safe(self):
        r = _adj("The ownership situation is complex and I cannot give JSON.")
        assert r.call_success is False
        assert r.error == "sonnet_parse_failed"
        assert r.adjudication == "unclear"
        assert r.confidence == "Low"
        assert r.target_company_match == "unclear"
        rec = build_c5_recommendation(r)
        assert rec["c5_recommended_hq_score"] == 0.0
        assert rec["c5_recommended_manual_review"] is True


# ---------------------------------------------------------------------------
# CLI row filtering (small, optional)
# ---------------------------------------------------------------------------

class TestModelConfig:
    def test_default_constant_is_sonnet_5(self):
        assert DEFAULT_SONNET_ADJUDICATION_MODEL == "claude-sonnet-5"

    def test_no_older_ids_as_default(self):
        assert "4-5" not in DEFAULT_SONNET_ADJUDICATION_MODEL
        assert "3-5" not in DEFAULT_SONNET_ADJUDICATION_MODEL
        assert "3-7" not in DEFAULT_SONNET_ADJUDICATION_MODEL

    def test_result_default_model_uses_constant(self):
        assert SonnetHQAdjudicationResult().model == DEFAULT_SONNET_ADJUDICATION_MODEL

    def test_cli_default_tier_is_sonnet_and_resolves_to_constant(self):
        from run_hq_sonnet_adjudication_probe import build_arg_parser, resolve_c5_model
        args = build_arg_parser().parse_args(["--input", "x.xlsx", "--output", "y.xlsx"])
        assert args.model_tier == "sonnet"
        assert args.model is None
        model_used, err = resolve_c5_model(args.model_tier, args.model)
        assert err is None
        assert model_used == DEFAULT_SONNET_ADJUDICATION_MODEL

    def test_adjudicate_records_model_used(self):
        text = ('{"adjudication":"unclear","confidence":"Low",'
                '"target_company_match":"unclear"}')
        with _mock_anthropic(text):
            r = adjudicate_hq_with_sonnet(
                company_name="X", domain="x.com", input_country="Brazil",
                anthropic_api_key="fake")
        assert r.model == DEFAULT_SONNET_ADJUDICATION_MODEL


class TestModelTierResolution:
    def test_explicit_model_overrides_tier(self):
        from run_hq_sonnet_adjudication_probe import resolve_c5_model
        model_used, err = resolve_c5_model("sonnet", "my-custom-model")
        assert err is None and model_used == "my-custom-model"
        # also overrides opus tier
        model_used2, err2 = resolve_c5_model("opus", "claude-opus-4-8")
        assert err2 is None and model_used2 == "claude-opus-4-8"

    def test_sonnet_tier_no_model_uses_constant(self):
        from run_hq_sonnet_adjudication_probe import resolve_c5_model
        model_used, err = resolve_c5_model("sonnet", None)
        assert err is None and model_used == DEFAULT_SONNET_ADJUDICATION_MODEL

    def test_opus_tier_without_model_rejected(self):
        from run_hq_sonnet_adjudication_probe import resolve_c5_model
        model_used, err = resolve_c5_model("opus", None)
        assert model_used is None
        assert err and "explicit --model" in err

    def test_opus_guardrail_small_limit_ok(self):
        from run_hq_sonnet_adjudication_probe import check_opus_guardrail
        assert check_opus_guardrail("opus", 10, False) is None
        assert check_opus_guardrail("opus", 5, False) is None

    def test_opus_guardrail_large_limit_requires_confirm(self):
        from run_hq_sonnet_adjudication_probe import check_opus_guardrail
        assert check_opus_guardrail("opus", 50, False) is not None
        assert check_opus_guardrail("opus", 0, False) is not None   # 0 = all rows
        assert check_opus_guardrail("opus", 50, True) is None
        assert check_opus_guardrail("opus", 0, True) is None

    def test_opus_guardrail_ignored_for_sonnet(self):
        from run_hq_sonnet_adjudication_probe import check_opus_guardrail
        assert check_opus_guardrail("sonnet", 0, False) is None
        assert check_opus_guardrail("sonnet", 500, False) is None

    def test_main_opus_without_model_returns_error(self, tmp_path):
        import pandas as pd
        import run_hq_sonnet_adjudication_probe as probe
        inp = tmp_path / "in.xlsx"
        pd.DataFrame({"company_name": ["A"], "domain": ["a.com"],
                      "input_country": ["Brazil"]}).to_excel(
            inp, sheet_name="Enriched Leads", index=False)
        rc = probe.main(["--input", str(inp), "--output", str(tmp_path / "o.xlsx"),
                         "--model-tier", "opus", "--row-limit", "5"])
        assert rc == 2

    def test_main_opus_large_limit_without_confirm_returns_error(self, tmp_path):
        import pandas as pd
        import run_hq_sonnet_adjudication_probe as probe
        inp = tmp_path / "in.xlsx"
        pd.DataFrame({"company_name": ["A"], "domain": ["a.com"],
                      "input_country": ["Brazil"]}).to_excel(
            inp, sheet_name="Enriched Leads", index=False)
        rc = probe.main(["--input", str(inp), "--output", str(tmp_path / "o.xlsx"),
                         "--model-tier", "opus", "--model", "claude-opus-4-8",
                         "--row-limit", "50"])
        assert rc == 2

    def test_main_opus_small_limit_with_model_runs(self, tmp_path):
        import pandas as pd
        import run_hq_sonnet_adjudication_probe as probe
        inp = tmp_path / "in.xlsx"
        pd.DataFrame({"company_name": ["A"], "domain": ["a.com"],
                      "input_country": ["Brazil"], "needs_manual_review": [True]}).to_excel(
            inp, sheet_name="Enriched Leads", index=False)
        out = tmp_path / "o.xlsx"
        fake = SonnetHQAdjudicationResult(
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            model="claude-opus-4-8", call_attempted=True, call_success=True)
        with patch("run_hq_sonnet_adjudication_probe.load_api_keys",
                   return_value={"ANTHROPIC_API_KEY": "fake"}), \
             patch("run_hq_sonnet_adjudication_probe.adjudicate_hq_with_sonnet",
                   return_value=fake):
            rc = probe.main(["--input", str(inp), "--output", str(out),
                             "--model-tier", "opus", "--model", "claude-opus-4-8",
                             "--row-limit", "10"])
        assert rc == 0
        adj = pd.read_excel(out, sheet_name="C5 Adjudication")
        assert adj.iloc[0]["c5_model_used"] == "claude-opus-4-8"
        assert adj.iloc[0]["c5_model_tier"] == "opus"

    def test_main_opus_large_limit_with_confirm_runs(self, tmp_path):
        import pandas as pd
        import run_hq_sonnet_adjudication_probe as probe
        inp = tmp_path / "in.xlsx"
        pd.DataFrame({"company_name": [f"C{i}" for i in range(3)],
                      "domain": [f"c{i}.com" for i in range(3)],
                      "input_country": ["Brazil"] * 3}).to_excel(
            inp, sheet_name="Enriched Leads", index=False)
        out = tmp_path / "o.xlsx"
        fake = SonnetHQAdjudicationResult(
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            model="claude-opus-4-8", call_attempted=True, call_success=True)
        with patch("run_hq_sonnet_adjudication_probe.load_api_keys",
                   return_value={"ANTHROPIC_API_KEY": "fake"}), \
             patch("run_hq_sonnet_adjudication_probe.adjudicate_hq_with_sonnet",
                   return_value=fake):
            rc = probe.main(["--input", str(inp), "--output", str(out),
                             "--model-tier", "opus", "--model", "claude-opus-4-8",
                             "--row-limit", "0", "--confirm-expensive-opus"])
        assert rc == 0


class TestProbeOutputModelColumn:
    def test_output_rows_include_c5_sonnet_model(self, tmp_path):
        import pandas as pd
        import run_hq_sonnet_adjudication_probe as probe

        inp = tmp_path / "hq_only.xlsx"
        pd.DataFrame({
            "company_name": ["Acme"],
            "domain": ["acme.com.br"],
            "input_country": ["Brazil"],
            "needs_manual_review": [True],
        }).to_excel(inp, sheet_name="Enriched Leads", index=False)
        out = tmp_path / "c5.xlsx"

        fake = SonnetHQAdjudicationResult(
            adjudication="domestic_confirmed", confidence="High",
            target_company_match="yes", model=DEFAULT_SONNET_ADJUDICATION_MODEL,
            call_attempted=True, call_success=True)

        with patch("run_hq_sonnet_adjudication_probe.load_api_keys",
                   return_value={"ANTHROPIC_API_KEY": "fake"}), \
             patch("run_hq_sonnet_adjudication_probe.adjudicate_hq_with_sonnet",
                   return_value=fake):
            rc = probe.main(["--input", str(inp), "--output", str(out)])
        assert rc == 0

        adj = pd.read_excel(out, sheet_name="C5 Adjudication")
        assert "c5_sonnet_model" in adj.columns
        assert adj.iloc[0]["c5_sonnet_model"] == DEFAULT_SONNET_ADJUDICATION_MODEL
        # preferred general columns
        assert "c5_model_used" in adj.columns
        assert adj.iloc[0]["c5_model_used"] == DEFAULT_SONNET_ADJUDICATION_MODEL
        assert adj.iloc[0]["c5_model_tier"] == "sonnet"
        summ = pd.read_excel(out, sheet_name="C5 Summary")
        assert "sonnet_model" in summ.columns
        assert summ.iloc[0]["sonnet_model"] == DEFAULT_SONNET_ADJUDICATION_MODEL
        assert "c5_model_used" in summ.columns
        assert summ.iloc[0]["c5_model_used"] == DEFAULT_SONNET_ADJUDICATION_MODEL
        assert "c5_model_tier" in summ.columns
        assert "confirm_expensive_opus" in summ.columns


class TestProbeRowFiltering:
    def test_filters_and_limits(self):
        import pandas as pd
        from run_hq_sonnet_adjudication_probe import filter_probe_rows

        df = pd.DataFrame({
            "company_name": [f"C{i}" for i in range(6)],
            "needs_manual_review": [True, False, True, True, False, True],
            "hq_positive_score_suppressed_for_review": ["Yes", "No", "No", "Yes", "No", "Yes"],
        })
        # only manual review → indices 0,2,3,5
        mr = filter_probe_rows(df, only_manual_review=True, only_suppressed=False,
                               start_row=0, row_limit=0)
        assert list(mr.index) == [0, 2, 3, 5]
        # only suppressed → indices 0,3,5
        sup = filter_probe_rows(df, only_manual_review=False, only_suppressed=True,
                                start_row=0, row_limit=0)
        assert list(sup.index) == [0, 3, 5]
        # both filters + limit → 0,3,5 intersect, first 2
        both = filter_probe_rows(df, only_manual_review=True, only_suppressed=True,
                                 start_row=0, row_limit=2)
        assert list(both.index) == [0, 3]
