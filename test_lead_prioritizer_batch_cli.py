"""Tests for the Lead Prioritizer v2 batch CLI (non-live parts only).

`run_batch_dataframe` / `build_excel_workbook_bytes` / key loading are mocked;
no live APIs and no real keys.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

import lead_prioritizer_batch_cli as cli
from lead_prioritizer_batch_cli import (
    build_arg_parser,
    generate_output_path,
    resolve_sheet,
    check_required_columns,
    config_from_args,
    load_firecrawl_key,
    SheetResolutionError,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _write_xlsx(path: Path, sheets: dict) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name, index=False)


_LEADS = pd.DataFrame({
    "company_name": [f"Co{i}" for i in range(5)],
    "domain": [f"co{i}.com" for i in range(5)],
})

_KEYS_OK = {"SERPER_API_KEY": "SK", "ANTHROPIC_API_KEY": "AK"}
_KEYS_MISSING = {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""}


def _fake_tables():
    return {
        "enriched_leads": pd.DataFrame([{"company_name": "Co0", "run_success": True}]),
        "evidence": pd.DataFrame(),
        "signals": pd.DataFrame(),
        "run_summary": pd.DataFrame([{
            "processed_rows": 1, "success_count": 1, "error_count": 0,
        }]),
    }


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_defaults(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d"])
        assert args.mode == "full"
        assert args.start_row == 0
        assert args.row_limit == 10
        assert args.default_country == ""
        assert args.sheet is None
        assert args.include_raw_ai_json is False
        assert args.stop_on_error is False
        assert args.yes is False
        assert args.compose_caller_content is False
        assert args.rich_icp_context is False
        assert args.ai_signal_scoring is False
        assert args.legacy_enrichment_mode is False
        assert args.deep_dive is False
        assert args.deep_dive_min_score == 8.0
        assert args.deep_dive_max_pages == 6
        assert args.no_verify_quotes is False
        assert args.no_auto_correct_quotes is False
        assert args.no_deep_dive_on_foreign_hq is False
        assert args.use_enrichment_cache is False
        assert args.enrichment_cache_bucket == ""
        assert args.c5_enabled is False
        cfg = config_from_args(args)
        assert cfg.verify_quotes is True
        assert cfg.auto_correct_quotes is True
        assert cfg.ai_signal_scoring is False
        assert cfg.legacy_enrichment_mode is False
        assert cfg.deep_dive_on_foreign_hq is True
        assert cfg.use_enrichment_cache is False
        assert cfg.enrichment_cache_bucket == ""

    def test_compose_caller_content_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--compose-caller-content"])
        assert args.compose_caller_content is True
        cfg = config_from_args(args)
        assert cfg.compose_caller_content is True

    def test_rich_icp_context_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--rich-icp-context"])
        assert args.rich_icp_context is True
        cfg = config_from_args(args)
        assert cfg.rich_icp_context is True

    def test_ai_signal_scoring_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--ai-signal-scoring"])
        assert args.ai_signal_scoring is True
        cfg = config_from_args(args)
        assert cfg.ai_signal_scoring is True

    @pytest.mark.parametrize("cli_flags,expect_caller,expect_icp", [
        ([], False, False),
        (["--compose-caller-content"], True, False),
        (["--rich-icp-context"], False, True),
        (["--compose-caller-content", "--rich-icp-context"], True, True),
    ])
    def test_flag_combinations_are_independent(self, cli_flags, expect_caller, expect_icp):
        # Onderdeel A (--rich-icp-context) and Step 3 (--compose-caller-content)
        # must parse and combine into BatchRunConfig with no cross-dependency:
        # every one of the four on/off combinations must work standalone.
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             *cli_flags])
        cfg = config_from_args(args)
        assert cfg.compose_caller_content is expect_caller
        assert cfg.rich_icp_context is expect_icp

    def test_ai_signal_scoring_independent_of_other_ai_flags(self):
        # Onderdeel 2 (--ai-signal-scoring) must parse and combine with no
        # cross-dependency on --compose-caller-content / --rich-icp-context.
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--ai-signal-scoring"])
        cfg = config_from_args(args)
        assert cfg.ai_signal_scoring is True
        assert cfg.compose_caller_content is False
        assert cfg.rich_icp_context is False

    def test_legacy_enrichment_mode_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--legacy-enrichment-mode"])
        assert args.legacy_enrichment_mode is True
        cfg = config_from_args(args)
        assert cfg.legacy_enrichment_mode is True

    def test_legacy_enrichment_mode_independent_of_other_flags(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--legacy-enrichment-mode"])
        cfg = config_from_args(args)
        assert cfg.legacy_enrichment_mode is True
        assert cfg.compose_caller_content is False
        assert cfg.rich_icp_context is False
        assert cfg.ai_signal_scoring is False

    def test_deep_dive_flags_parse(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--deep-dive", "--deep-dive-min-score", "6.5", "--deep-dive-max-pages", "4"])
        assert args.deep_dive is True
        assert args.deep_dive_min_score == 6.5
        assert args.deep_dive_max_pages == 4
        cfg = config_from_args(args)
        assert cfg.deep_dive is True
        assert cfg.deep_dive_min_score == 6.5
        assert cfg.deep_dive_max_pages == 4

    def test_no_deep_dive_on_foreign_hq_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--deep-dive", "--no-deep-dive-on-foreign-hq"])
        assert args.no_deep_dive_on_foreign_hq is True
        cfg = config_from_args(args)
        assert cfg.deep_dive_on_foreign_hq is False
        assert cfg.deep_dive is True

    def test_use_enrichment_cache_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--use-enrichment-cache", "--enrichment-cache-bucket", "my-bucket"])
        assert args.use_enrichment_cache is True
        assert args.enrichment_cache_bucket == "my-bucket"
        cfg = config_from_args(args)
        assert cfg.use_enrichment_cache is True
        assert cfg.enrichment_cache_bucket == "my-bucket"

    def test_c5_enabled_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--c5-enabled"])
        assert args.c5_enabled is True

    def test_gate_full_enrichment_on_foreign_hq_flag_parses_and_defaults_off(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d"])
        assert args.gate_full_enrichment_on_foreign_hq is False
        cfg = config_from_args(args)
        assert cfg.gate_full_enrichment_on_foreign_hq is False

        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--gate-full-enrichment-on-foreign-hq"])
        assert args.gate_full_enrichment_on_foreign_hq is True
        cfg = config_from_args(args)
        assert cfg.gate_full_enrichment_on_foreign_hq is True


    def test_checkpoint_flags_default_off(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d"])
        assert args.checkpoint_path is None
        assert args.checkpoint_every_rows == 0

    def test_checkpoint_flags_parse(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--checkpoint-path", "/tmp/checkpoint.json", "--checkpoint-every-rows", "5"])
        assert args.checkpoint_path == "/tmp/checkpoint.json"
        assert args.checkpoint_every_rows == 5

    def test_no_verify_quotes_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--deep-dive", "--no-verify-quotes"])
        assert args.no_verify_quotes is True
        cfg = config_from_args(args)
        assert cfg.verify_quotes is False
        # auto_correct_quotes stays True at the config level -- run_deep_dive
        # itself only applies auto-correction when verify_quotes is on.
        assert cfg.auto_correct_quotes is True

    def test_no_auto_correct_quotes_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--deep-dive", "--no-auto-correct-quotes"])
        assert args.no_auto_correct_quotes is True
        cfg = config_from_args(args)
        assert cfg.auto_correct_quotes is False
        assert cfg.verify_quotes is True

    def test_both_no_verify_and_no_auto_correct_together(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--deep-dive", "--no-verify-quotes", "--no-auto-correct-quotes"])
        cfg = config_from_args(args)
        assert cfg.verify_quotes is False
        assert cfg.auto_correct_quotes is False

    @pytest.mark.parametrize("cli_flags,expect_icp,expect_deep_dive", [
        ([], False, False),
        (["--rich-icp-context"], True, False),
        (["--deep-dive"], False, True),
        (["--rich-icp-context", "--deep-dive"], True, True),
    ])
    def test_rich_icp_context_and_deep_dive_are_independent(
        self, cli_flags, expect_icp, expect_deep_dive,
    ):
        # Onderdeel A (--rich-icp-context) x Onderdeel B (--deep-dive):
        # every one of the four on/off combinations must parse and combine
        # with no cross-dependency between the two opt-ins.
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             *cli_flags])
        cfg = config_from_args(args)
        assert cfg.rich_icp_context is expect_icp
        assert cfg.deep_dive is expect_deep_dive

    def test_config_from_args_maps_stop_on_error(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--stop-on-error", "--mode", "hq_only"])
        cfg = config_from_args(args)
        assert cfg.continue_on_error is False
        assert cfg.run_mode == "hq_only"
        assert cfg.company_name_column == "c"

    def test_invalid_mode_rejected(self):
        with pytest.raises(SystemExit):
            build_arg_parser().parse_args(
                ["--input", "x.xlsx", "--company-column", "c",
                 "--domain-column", "d", "--mode", "bogus"])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_output_path_generation(self):
        when = datetime(2026, 7, 1, 9, 30, 15)
        out = generate_output_path(Path("/data/Italy_500.xlsx"), "full", when)
        assert out.name == "Italy_500_lead_prioritizer_v2_full_20260701_093015.xlsx"
        assert out.parent == Path("/data")

    def test_resolve_single_sheet(self):
        assert resolve_sheet(["OnlySheet"], None) == "OnlySheet"

    def test_resolve_named_sheet(self):
        assert resolve_sheet(["A", "B"], "B") == "B"

    def test_resolve_missing_named_sheet_raises(self):
        with pytest.raises(SheetResolutionError):
            resolve_sheet(["A", "B"], "C")

    def test_resolve_multi_sheet_without_arg_raises(self):
        with pytest.raises(SheetResolutionError):
            resolve_sheet(["A", "B"], None)

    def test_check_columns_ok(self):
        check_required_columns(["company_name", "domain", "country"],
                               "company_name", "domain", "country")

    def test_check_columns_missing_raises(self):
        with pytest.raises(ValueError):
            check_required_columns(["company_name"], "company_name", "domain")


class TestLoadFirecrawlKey:
    def test_env_var_used(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-from-env")
        assert load_firecrawl_key(None) == "fc-from-env"

    def test_missing_is_not_an_error(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        assert load_firecrawl_key(None) == ""

    def test_secrets_file_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        secrets = tmp_path / "secrets.toml"
        secrets.write_text('FIRECRAWL_API_KEY = "fc-from-file"\n')
        assert load_firecrawl_key(str(secrets)) == "fc-from-file"

    def test_env_takes_precedence_over_secrets_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-from-env")
        secrets = tmp_path / "secrets.toml"
        secrets.write_text('FIRECRAWL_API_KEY = "fc-from-file"\n')
        assert load_firecrawl_key(str(secrets)) == "fc-from-env"

    def test_missing_secrets_file_is_safe(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        assert load_firecrawl_key("/no/such/file.toml") == ""


# ---------------------------------------------------------------------------
# main() — mocked, no live APIs
# ---------------------------------------------------------------------------

class TestMain:
    def _base_argv(self, path: Path, **extra):
        argv = ["--input", str(path), "--company-column", "company_name",
                "--domain-column", "domain"]
        for k, v in extra.items():
            argv.append(k)
            if v is not None:
                argv.append(str(v))
        return argv

    def test_missing_keys_exits_nonzero(self, tmp_path, capsys):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_MISSING):
            rc = main(self._base_argv(p))
        assert rc == 2
        out = capsys.readouterr()
        assert "SERPER_API_KEY: missing" in out.out
        # never leaks values
        assert "SK" not in out.out and "AK" not in out.out

    def test_multi_sheet_without_sheet_exits(self, tmp_path):
        p = tmp_path / "multi.xlsx"
        _write_xlsx(p, {"A": _LEADS, "B": _LEADS})
        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK):
            rc = main(self._base_argv(p))
        assert rc == 2

    def test_missing_column_exits(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": pd.DataFrame({"company_name": ["X"]})})  # no domain
        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK):
            rc = main(self._base_argv(p))
        assert rc == 2

    def test_over_50_requires_yes(self, tmp_path, capsys):
        big = pd.DataFrame({"company_name": [f"C{i}" for i in range(60)],
                            "domain": [f"c{i}.com" for i in range(60)]})
        p = tmp_path / "big.xlsx"
        _write_xlsx(p, {"Sheet1": big})
        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe") as m_run:
            rc = main(self._base_argv(p, **{"--row-limit": 0}))  # 0 = all 60
        assert rc == 3
        m_run.assert_not_called()
        assert "Re-run with --yes" in capsys.readouterr().err

    def test_over_50_with_yes_runs(self, tmp_path):
        big = pd.DataFrame({"company_name": [f"C{i}" for i in range(60)],
                            "domain": [f"c{i}.com" for i in range(60)]})
        p = tmp_path / "big.xlsx"
        _write_xlsx(p, {"Sheet1": big})
        out_path = tmp_path / "out.xlsx"
        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", return_value=_fake_tables()) as m_run, \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"XLSXBYTES") as m_build:
            argv = self._base_argv(p, **{"--row-limit": 0, "--output": str(out_path)})
            argv.append("--yes")
            rc = main(argv)
        assert rc == 0
        m_run.assert_called_once()
        m_build.assert_called_once()
        assert out_path.read_bytes() == b"XLSXBYTES"

    def test_calls_core_with_expected_config_and_writes_output(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["config"] = config
            captured["serper"] = serper
            captured["anthropic"] = anthropic
            return _fake_tables()

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{"--mode": "hq_only", "--row-limit": 3,
                                         "--default-country": "Italy",
                                         "--output": str(out_path)})
            argv.append("--compose-caller-content")
            rc = main(argv)

        assert rc == 0
        cfg = captured["config"]
        assert cfg.run_mode == "hq_only"
        assert cfg.row_limit == 3
        assert cfg.company_name_column == "company_name"
        assert cfg.domain_column == "domain"
        assert cfg.default_input_country == "Italy"
        assert cfg.compose_caller_content is True
        # keys passed through to core but never written to disk output
        assert captured["serper"] == "SK" and captured["anthropic"] == "AK"
        assert out_path.read_bytes() == b"BYTES"

    def test_checkpoint_kwargs_wired_when_flags_set(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        checkpoint_path = tmp_path / "checkpoint.json"
        captured = {}

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["kwargs"] = kwargs
            return _fake_tables()

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{
                "--output": str(out_path),
                "--checkpoint-path": str(checkpoint_path),
                "--checkpoint-every-rows": 1,
            })
            rc = main(argv)

        assert rc == 0
        assert "checkpoint_callback" in captured["kwargs"]
        assert captured["kwargs"]["checkpoint_every_rows"] == 1
        # the wired callback actually writes to the requested path
        captured["kwargs"]["checkpoint_callback"]([{"a": 1}], [], [])
        assert checkpoint_path.exists()

    def test_checkpoint_kwargs_absent_by_default(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["kwargs"] = kwargs
            return _fake_tables()

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            rc = main(self._base_argv(p, **{"--output": str(out_path)}))

        assert rc == 0
        assert "checkpoint_callback" not in captured["kwargs"]
        assert "checkpoint_every_rows" not in captured["kwargs"]

    def test_usage_output_writes_snapshot_json(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        usage_path = tmp_path / "usage.json"

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", return_value=_fake_tables()), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{
                "--output": str(out_path), "--usage-output": str(usage_path),
            })
            rc = main(argv)

        assert rc == 0
        assert usage_path.exists()
        snapshot = json.loads(usage_path.read_text(encoding="utf-8"))
        assert "serper_total" in snapshot and "anthropic_calls" in snapshot

    def test_no_usage_output_flag_writes_no_file(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", return_value=_fake_tables()), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            rc = main(self._base_argv(p, **{"--output": str(out_path)}))

        assert rc == 0
        assert not (tmp_path / "usage.json").exists()

    def test_c5_enabled_runs_adjudication_and_records_summary(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_c5(enriched_rows, **kwargs):
            captured["c5_kwargs"] = kwargs
            return [{"company_name": "Co0", "run_success": True, "c5_touched": True}], {
                "c5_rows_attempted": 1, "c5_success_count": 1,
            }

        def _fake_summary(run_summary, **kwargs):
            captured["summary_kwargs"] = kwargs
            return run_summary

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", return_value=_fake_tables()), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES") as m_build, \
             patch("lead_prioritizer_batch_core.apply_c5_adjudication", side_effect=_fake_c5), \
             patch("lead_prioritizer_batch_core.add_c5_summary_fields", side_effect=_fake_summary):
            argv = self._base_argv(p, **{"--output": str(out_path)})
            argv.append("--c5-enabled")
            rc = main(argv)

        assert rc == 0
        assert captured["c5_kwargs"]["scoring_behavior"] == "conservative_adjustment"
        assert captured["c5_kwargs"]["scope"] == "score_3_or_manual_review"
        assert captured["c5_kwargs"]["model_tier"] == "sonnet"
        assert captured["summary_kwargs"]["c5_enabled"] is True
        assert captured["summary_kwargs"]["c5_scoring_behavior"] == "conservative_adjustment"
        # the C5-adjudicated rows replace enriched_leads before the workbook is built
        tables_written = m_build.call_args[0][0]
        assert bool(tables_written["enriched_leads"].iloc[0]["c5_touched"]) is True

    def test_c5_disabled_still_records_summary_flag(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_summary(run_summary, **kwargs):
            captured["summary_kwargs"] = kwargs
            return run_summary

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", return_value=_fake_tables()), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"), \
             patch("lead_prioritizer_batch_core.add_c5_summary_fields", side_effect=_fake_summary):
            rc = main(self._base_argv(p, **{"--output": str(out_path)}))

        assert rc == 0
        assert captured["summary_kwargs"]["c5_enabled"] is False
        assert captured["summary_kwargs"]["c5_scoring_behavior"] == ""

    def test_gate_plus_c5_passes_c5_into_run_batch_dataframe_and_skips_post_step(self, tmp_path):
        # When both --gate-full-enrichment-on-foreign-hq and --c5-enabled are
        # set, C5 must run INSIDE run_batch_dataframe's gated path (passed as
        # kwargs), not as a second, separate apply_c5_adjudication call --
        # run_summary already carries the C5 columns from that inner call.
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        gated_tables = _fake_tables()
        gated_tables["run_summary"] = pd.DataFrame([{
            "processed_rows": 1, "success_count": 1, "error_count": 0,
            "gated_full_enrichment_attempted_count": 1,
            "gated_full_enrichment_skipped_count": 0,
            "gated_estimated_serper_calls_saved": 0,
            "c5_enabled": True,
        }])

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["run_batch_dataframe_kwargs"] = kwargs
            return gated_tables

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"), \
             patch("run_hq_sonnet_adjudication_probe.resolve_c5_model",
                   return_value=("claude-fake-model", None)), \
             patch("lead_prioritizer_batch_core.apply_c5_adjudication") as m_c5_post, \
             patch("lead_prioritizer_batch_core.add_c5_summary_fields") as m_summary_post:
            argv = self._base_argv(p, **{"--output": str(out_path)})
            argv += ["--gate-full-enrichment-on-foreign-hq", "--c5-enabled"]
            rc = main(argv)

        assert rc == 0
        kwargs = captured["run_batch_dataframe_kwargs"]
        assert kwargs["c5_enabled"] is True
        assert kwargs["c5_scoring_behavior"] == "conservative_adjustment"
        assert kwargs["c5_scope"] == "score_3_or_manual_review"
        assert kwargs["c5_model_tier"] == "sonnet"
        assert kwargs["c5_model_used"] == "claude-fake-model"
        # The CLI's own post-step must NOT run a second C5 pass or overwrite
        # the run_summary the gated path already produced.
        m_c5_post.assert_not_called()
        m_summary_post.assert_not_called()

    def test_gate_without_c5_does_not_pass_c5_kwargs(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["run_batch_dataframe_kwargs"] = kwargs
            return _fake_tables()

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{"--output": str(out_path)})
            argv.append("--gate-full-enrichment-on-foreign-hq")
            rc = main(argv)

        assert rc == 0
        assert "c5_enabled" not in captured["run_batch_dataframe_kwargs"]

    def test_rich_icp_context_passthrough_independent_of_compose_caller_content(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["config"] = config
            return _fake_tables()

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{"--output": str(out_path)})
            argv.append("--rich-icp-context")
            rc = main(argv)

        assert rc == 0
        cfg = captured["config"]
        assert cfg.rich_icp_context is True
        # --compose-caller-content was never passed: stays independently off.
        assert cfg.compose_caller_content is False

    def test_deep_dive_flags_and_firecrawl_key_passthrough(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "FC")
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["config"] = config
            captured["firecrawl_api_key"] = kwargs.get("firecrawl_api_key")
            return _fake_tables()

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{"--output": str(out_path),
                                         "--deep-dive-min-score": "6.0",
                                         "--deep-dive-max-pages": "3"})
            argv.append("--deep-dive")
            rc = main(argv)

        assert rc == 0
        cfg = captured["config"]
        assert cfg.deep_dive is True
        assert cfg.deep_dive_min_score == 6.0
        assert cfg.deep_dive_max_pages == 3
        assert captured["firecrawl_api_key"] == "FC"
        assert "FIRECRAWL_API_KEY: set" in capsys.readouterr().out

    def test_no_verify_and_no_auto_correct_quotes_passthrough(self, tmp_path):
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"
        captured = {}

        def _fake_run(df, config, serper, anthropic, **kwargs):
            captured["config"] = config
            return _fake_tables()

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", side_effect=_fake_run), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{"--output": str(out_path)})
            argv += ["--deep-dive", "--no-verify-quotes", "--no-auto-correct-quotes"]
            rc = main(argv)

        assert rc == 0
        cfg = captured["config"]
        assert cfg.verify_quotes is False
        assert cfg.auto_correct_quotes is False

    def test_missing_firecrawl_key_is_not_fatal(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "result.xlsx"

        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe",
                   return_value=_fake_tables()) as m_run, \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"BYTES"):
            argv = self._base_argv(p, **{"--output": str(out_path)})
            argv.append("--deep-dive")
            rc = main(argv)

        assert rc == 0  # missing Firecrawl key never blocks the run
        m_run.assert_called_once()
        assert m_run.call_args.kwargs["firecrawl_api_key"] == ""
        assert "not set (Deep Dive fallback mode)" in capsys.readouterr().out

    def test_output_bytes_contain_no_keys(self, tmp_path):
        # Guard: the CLI writes exactly the core's bytes, which never embed keys.
        p = tmp_path / "in.xlsx"
        _write_xlsx(p, {"Sheet1": _LEADS})
        out_path = tmp_path / "o.xlsx"
        with patch("lead_prioritizer_batch_cli.load_api_keys", return_value=_KEYS_OK), \
             patch("lead_prioritizer_batch_cli.run_batch_dataframe", return_value=_fake_tables()), \
             patch("lead_prioritizer_batch_cli.build_excel_workbook_bytes", return_value=b"clean-bytes"):
            rc = main(self._base_argv(p, **{"--output": str(out_path)}))
        assert rc == 0
        blob = out_path.read_bytes()
        assert b"SK" not in blob and b"AK" not in blob
