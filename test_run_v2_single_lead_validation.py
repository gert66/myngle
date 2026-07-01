"""Non-live tests for the v2 single-lead validation runner.

No live API keys and no network calls: ``prioritize_single_lead`` is mocked
where needed. Covers key loading, compact output conversion, the six-URL
evidence cap, and the guarantee that key values never leak into output.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from lead_output_schema import LeadEvidence, LeadPrioritizationResult, LeadSignal

import run_v2_single_lead_validation as runner

SERPER = runner.SERPER_KEY_NAME
ANTHROPIC = runner.ANTHROPIC_KEY_NAME

_FAKE_SERPER = "fake-serper-key-DO-NOT-LEAK"
_FAKE_ANTHROPIC = "fake-anthropic-key-DO-NOT-LEAK"


def _result_with_evidence(n_urls: int) -> LeadPrioritizationResult:
    evidence = [
        LeadEvidence(source_url=f"https://example.com/e{i}", signal_name="s")
        for i in range(n_urls)
    ]
    return LeadPrioritizationResult(
        company_name="Acme",
        domain="acme.com",
        input_country="Italy",
        v2_pipeline_mode="full_v2_single_lead",
        hq_detected_country="Germany",
        foreign_hq_simple=True,
        sig_foreign_hq_score_for_next_scoring=3.0,
        ai_hq_raw_json='{"secret_model_output": "should never appear"}',
        final_commercial_fit_score=42.0,
        commercial_tier="B",
        evidence_items=evidence,
        signals=[LeadSignal(signal_name="international_profile", signal_score=1.0)],
    )


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

class TestKeyLoading:
    def test_env_only(self):
        env = {SERPER: "s-env", ANTHROPIC: "a-env"}
        keys = runner.load_api_keys(env=env)
        assert keys[SERPER] == "s-env"
        assert keys[ANTHROPIC] == "a-env"

    def test_missing_both_returns_empty(self):
        keys = runner.load_api_keys(env={})
        assert keys[SERPER] == ""
        assert keys[ANTHROPIC] == ""

    def test_env_is_stripped(self):
        keys = runner.load_api_keys(env={SERPER: "  s  ", ANTHROPIC: "\ta\n"})
        assert keys[SERPER] == "s"
        assert keys[ANTHROPIC] == "a"

    def test_secrets_file_fallback_when_env_missing(self, tmp_path):
        secrets = tmp_path / "secrets.toml"
        secrets.write_text(
            f'{SERPER} = "s-file"\n{ANTHROPIC} = "a-file"\n', encoding="utf-8"
        )
        keys = runner.load_api_keys(env={}, secrets_file=secrets)
        assert keys[SERPER] == "s-file"
        assert keys[ANTHROPIC] == "a-file"

    def test_env_takes_precedence_over_secrets_file(self, tmp_path):
        secrets = tmp_path / "secrets.toml"
        secrets.write_text(
            f'{SERPER} = "s-file"\n{ANTHROPIC} = "a-file"\n', encoding="utf-8"
        )
        # SERPER in env, ANTHROPIC only in file -> mixed resolution.
        keys = runner.load_api_keys(env={SERPER: "s-env"}, secrets_file=secrets)
        assert keys[SERPER] == "s-env"
        assert keys[ANTHROPIC] == "a-file"

    def test_missing_secrets_file_is_safe(self, tmp_path):
        keys = runner.load_api_keys(
            env={}, secrets_file=tmp_path / "does_not_exist.toml"
        )
        assert keys[SERPER] == ""
        assert keys[ANTHROPIC] == ""


# ---------------------------------------------------------------------------
# Compact output conversion
# ---------------------------------------------------------------------------

class TestCompactOutput:
    def test_includes_expected_compact_fields(self):
        result = _result_with_evidence(2)
        rec = runner.to_compact_output(result, run_success=True)
        for field in runner.COMPACT_SCALAR_FIELDS:
            assert field in rec
        assert rec["company_name"] == "Acme"
        assert rec["final_commercial_fit_score"] == 42.0
        assert rec["evidence_count"] == 2
        assert rec["signal_count"] == 1
        assert rec["run_success"] is True
        assert rec["run_error"] is None

    def test_excludes_raw_ai_json_field(self):
        rec = runner.to_compact_output(_result_with_evidence(1), run_success=True)
        assert "ai_hq_raw_json" not in rec

    def test_failure_record_marks_run_error(self):
        case = runner.ValidationCase("Foo", "foo.it")
        rec = runner._failed_case_record(case, "RuntimeError: boom")
        assert rec["run_success"] is False
        assert rec["run_error"] == "RuntimeError: boom"
        assert rec["company_name"] == "Foo"
        assert rec["input_country"] == runner.DEFAULT_INPUT_COUNTRY


# ---------------------------------------------------------------------------
# Evidence URL cap (max 6)
# ---------------------------------------------------------------------------

class TestEvidenceUrlCap:
    def test_caps_at_six(self):
        rec = runner.to_compact_output(_result_with_evidence(10), run_success=True)
        assert len(rec["evidence_urls"]) == runner.MAX_EVIDENCE_URLS == 6

    def test_under_cap_kept(self):
        rec = runner.to_compact_output(_result_with_evidence(3), run_success=True)
        assert len(rec["evidence_urls"]) == 3

    def test_dedupes_and_skips_blank_urls(self):
        result = LeadPrioritizationResult(
            company_name="Acme",
            evidence_items=[
                LeadEvidence(source_url="https://x.com/a"),
                LeadEvidence(source_url="https://x.com/a"),  # dupe
                LeadEvidence(source_url="   "),               # blank
                LeadEvidence(source_url=None),                # none
                LeadEvidence(source_url="https://x.com/b"),
            ],
        )
        urls = runner._evidence_urls(result)
        assert urls == ["https://x.com/a", "https://x.com/b"]


# ---------------------------------------------------------------------------
# No key leakage
# ---------------------------------------------------------------------------

class TestNoKeyLeakage:
    def test_keys_absent_from_compact_output(self):
        # A result carrying raw AI JSON must not leak it, and the compact record
        # (serialized) must not contain either fake key value.
        result = _result_with_evidence(2)
        rec = runner.to_compact_output(result, run_success=True)
        blob = json.dumps(rec)
        assert _FAKE_SERPER not in blob
        assert _FAKE_ANTHROPIC not in blob
        assert "secret_model_output" not in blob  # raw AI JSON excluded

    def test_keys_absent_after_full_run_case(self):
        # run_case receives the real keys but must never place them in output.
        result = _result_with_evidence(1)
        with patch.object(runner, "prioritize_single_lead", return_value=result):
            rec = runner.run_case(
                runner.ValidationCase("Acme", "acme.com"),
                serper_api_key=_FAKE_SERPER,
                anthropic_api_key=_FAKE_ANTHROPIC,
            )
        blob = json.dumps(rec)
        assert _FAKE_SERPER not in blob
        assert _FAKE_ANTHROPIC not in blob
        assert rec["run_success"] is True

    def test_run_case_captures_exception_without_leaking_keys(self):
        def _boom(*_a, **_k):
            raise RuntimeError("network down")

        with patch.object(runner, "prioritize_single_lead", side_effect=_boom):
            rec = runner.run_case(
                runner.ValidationCase("Acme", "acme.com"),
                serper_api_key=_FAKE_SERPER,
                anthropic_api_key=_FAKE_ANTHROPIC,
            )
        assert rec["run_success"] is False
        assert "network down" in rec["run_error"]
        blob = json.dumps(rec)
        assert _FAKE_SERPER not in blob
        assert _FAKE_ANTHROPIC not in blob


# ---------------------------------------------------------------------------
# Validation cases match the documented six
# ---------------------------------------------------------------------------

class TestValidationCases:
    def test_six_documented_cases(self):
        assert len(runner.VALIDATION_CASES) == 6
        domains = {c.domain for c in runner.VALIDATION_CASES}
        assert {"bmw.it", "knorr-bremse.com", "danfoss.com",
                "ricoh.it", "cannonbono.com", "iet.it"} == domains
