"""Tests for the Lead Prioritizer v2 batch CLI (non-live parts only).

`run_batch_dataframe` / `build_excel_workbook_bytes` / key loading are mocked;
no live APIs and no real keys.
"""

from __future__ import annotations

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
        assert args.default_country == "Italy"
        assert args.sheet is None
        assert args.include_raw_ai_json is False
        assert args.stop_on_error is False
        assert args.yes is False
        assert args.compose_caller_content is False

    def test_compose_caller_content_flag_parses(self):
        args = build_arg_parser().parse_args(
            ["--input", "x.xlsx", "--company-column", "c", "--domain-column", "d",
             "--compose-caller-content"])
        assert args.compose_caller_content is True
        cfg = config_from_args(args)
        assert cfg.compose_caller_content is True

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
        assert str(out.parent) == "/data"

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

        def _fake_run(df, config, serper, anthropic):
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
