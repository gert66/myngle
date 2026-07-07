"""Tests for lead_caller_content_composer.py (Step 3 — no live API calls).

The Anthropic client is mocked at the module level, same pattern as
test_lead_hq_sonnet_adjudicator.py.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from lead_caller_content_composer import (
    DEFAULT_CALLER_CONTENT_MODEL,
    DRIVER_SIGNAL_NAMES,
    build_curated_signals_from_result,
    compose_caller_content,
)


def _mock_anthropic(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("lead_caller_content_composer._anthropic_lib", lib)


_BASE_KWARGS = dict(
    company_name="Nordic Gear AB",
    country="Netherlands",
    industry="Manufacturing",
    employee_range="501-1000",
    foreign_hq_detected=True,
    parent_company="Nordic Gear Group",
    parent_hq_country="Sweden",
    parent_hq_city="Stockholm",
    hq_adjudication="foreign_parent_confirmed",
    curated_signals=[{
        "signal_name": "international_profile",
        "label": "International business context",
        "evidence": "Operates sales offices across five European countries.",
        "source_url": "https://nordicgear.se/about",
    }],
    driver_ids=list(DRIVER_SIGNAL_NAMES),
    quality_flags=[],
    anthropic_api_key="fake",
)


# ---------------------------------------------------------------------------
# 1. Mocked successful API response — fields correctly mapped.
# ---------------------------------------------------------------------------

class TestSuccessfulComposition:
    def test_fields_mapped_from_clean_json(self):
        payload = json.dumps({
            "why_relevant": "Nordic Gear AB is part of a Swedish parent group with a growing European footprint.",
            "what_is_hot": ["Foreign HQ in Sweden", "Sales offices in five countries"],
            "cold_caller_summary": "A practical briefing paragraph for the caller.",
            "caller_angle": "Open with the cross-border alignment angle.",
            "call_starter": "Hi, I noticed Nordic Gear AB operates across Europe.",
            "driver_evidence": {
                "foreign_hq": "Confirmed Swedish parent, Nordic Gear Group.",
                "international_profile": "Sales offices across five European countries.",
                "icp_keyword_match": "",
                "onboarding_training_need": "",
                "company_size_complexity": "",
                "employer_branding": "",
            },
        })
        with _mock_anthropic(payload):
            result = compose_caller_content(**_BASE_KWARGS)

        assert result.call_attempted is True
        assert result.call_success is True
        assert result.error == ""
        assert result.why_relevant == (
            "Nordic Gear AB is part of a Swedish parent group with a growing European footprint."
        )
        assert result.what_is_hot == ["Foreign HQ in Sweden", "Sales offices in five countries"]
        assert result.cold_caller_summary == "A practical briefing paragraph for the caller."
        assert result.caller_angle == "Open with the cross-border alignment angle."
        assert result.call_starter == "Hi, I noticed Nordic Gear AB operates across Europe."
        assert result.driver_evidence == {
            "foreign_hq": "Confirmed Swedish parent, Nordic Gear Group.",
            "international_profile": "Sales offices across five European countries.",
        }
        assert result.model == DEFAULT_CALLER_CONTENT_MODEL

    def test_fenced_json_is_stripped(self):
        payload = "```json\n" + json.dumps({
            "why_relevant": "Fenced response text.",
            "what_is_hot": [],
            "cold_caller_summary": "Fenced summary.",
            "caller_angle": "Fenced angle.",
            "call_starter": "Fenced starter.",
            "driver_evidence": {},
        }) + "\n```"
        with _mock_anthropic(payload):
            result = compose_caller_content(**_BASE_KWARGS)
        assert result.call_success is True
        assert result.why_relevant == "Fenced response text."

    def test_what_is_hot_capped_at_five_and_blank_items_dropped(self):
        payload = json.dumps({
            "why_relevant": "x",
            "what_is_hot": ["a", "", "b", "c", "d", "e", "f"],
            "cold_caller_summary": "x",
            "caller_angle": "x",
            "call_starter": "x",
            "driver_evidence": {},
        })
        with _mock_anthropic(payload):
            result = compose_caller_content(**_BASE_KWARGS)
        assert result.what_is_hot == ["a", "b", "c", "d", "e"]

    def test_driver_evidence_only_keeps_known_driver_ids(self):
        payload = json.dumps({
            "why_relevant": "x", "what_is_hot": [], "cold_caller_summary": "x",
            "caller_angle": "x", "call_starter": "x",
            "driver_evidence": {"foreign_hq": "ok", "not_a_real_driver": "ignored"},
        })
        with _mock_anthropic(payload):
            result = compose_caller_content(**_BASE_KWARGS)
        assert result.driver_evidence == {"foreign_hq": "ok"}


# ---------------------------------------------------------------------------
# 2. Mocked API error — silent fallback, audit note (error) present.
# ---------------------------------------------------------------------------

class TestApiErrorFallback:
    def test_client_exception_yields_silent_fallback_with_error(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("lead_caller_content_composer._anthropic_lib", lib):
            result = compose_caller_content(**_BASE_KWARGS)

        assert result.call_attempted is True
        assert result.call_success is False
        assert result.error.startswith("caller_content_call_failed")
        assert "connection reset" in result.error
        # Never raises, and no content is fabricated.
        assert result.why_relevant is None
        assert result.what_is_hot == []
        assert result.cold_caller_summary is None
        assert result.driver_evidence == {}

    def test_missing_anthropic_package_yields_silent_fallback(self):
        with patch("lead_caller_content_composer._anthropic_lib", None):
            result = compose_caller_content(**_BASE_KWARGS)
        assert result.call_attempted is True
        assert result.call_success is False
        assert result.error.startswith("caller_content_call_failed")

    def test_no_api_key_never_attempts_call(self):
        kwargs = dict(_BASE_KWARGS)
        kwargs["anthropic_api_key"] = ""
        result = compose_caller_content(**kwargs)
        assert result.call_attempted is False
        assert result.call_success is False
        assert result.error == "no_anthropic_api_key"


# ---------------------------------------------------------------------------
# 3. Mocked unparseable response — silent fallback, audit note present.
# ---------------------------------------------------------------------------

class TestUnparseableResponseFallback:
    def test_prose_response_yields_silent_fallback_with_error(self):
        text = "I'm sorry, I cannot produce a JSON object for this request."
        with _mock_anthropic(text):
            result = compose_caller_content(**_BASE_KWARGS)

        assert result.call_attempted is True
        assert result.call_success is False
        assert result.error == "caller_content_parse_failed"
        assert result.raw_json == text
        assert result.why_relevant is None
        assert result.what_is_hot == []
        assert result.driver_evidence == {}

    def test_empty_response_yields_silent_fallback_with_error(self):
        with _mock_anthropic(""):
            result = compose_caller_content(**_BASE_KWARGS)
        assert result.call_success is False
        assert result.error == "caller_content_parse_failed"

    def test_non_dict_json_yields_silent_fallback_with_error(self):
        with _mock_anthropic("[1, 2, 3]"):
            result = compose_caller_content(**_BASE_KWARGS)
        assert result.call_success is False
        assert result.error == "caller_content_parse_failed"


# ---------------------------------------------------------------------------
# build_curated_signals_from_result — curated-input builder used by the core
# wiring (lead_prioritizer_core.py) to feed compose_caller_content().
# ---------------------------------------------------------------------------

class _FakeSignal:
    def __init__(self, signal_name, signal_score, evidence_quote=None,
                 signal_reason=None, evidence_url=None):
        self.signal_name = signal_name
        self.signal_score = signal_score
        self.evidence_quote = evidence_quote
        self.signal_reason = signal_reason
        self.evidence_url = evidence_url


class _FakeResult:
    def __init__(self, signals):
        self.signals = signals


class TestBuildCuratedSignalsFromResult:
    def test_only_positive_scored_signals_with_evidence_are_included(self):
        result = _FakeResult([
            _FakeSignal("international_profile", 2.0, evidence_quote="Sales offices in five countries."),
            _FakeSignal("onboarding_training_need", 0.0, evidence_quote="No positive keywords matched."),
            _FakeSignal("employer_branding", 2.0, evidence_quote=""),  # blank evidence -> excluded
            _FakeSignal("icp_keyword_match", None, evidence_quote="Should be excluded (no score)."),
        ])
        curated = build_curated_signals_from_result(result)
        assert len(curated) == 1
        assert curated[0]["signal_name"] == "international_profile"
        assert curated[0]["evidence"] == "Sales offices in five countries."

    def test_falls_back_to_signal_reason_when_no_evidence_quote(self):
        result = _FakeResult([
            _FakeSignal("company_size_complexity", 1.0, evidence_quote=None,
                        signal_reason="1 distinct keyword match(es) in evidence: onboarding"),
        ])
        curated = build_curated_signals_from_result(result)
        assert curated[0]["evidence"] == "1 distinct keyword match(es) in evidence: onboarding"

    def test_no_signals_yields_empty_list(self):
        assert build_curated_signals_from_result(_FakeResult([])) == []
        assert build_curated_signals_from_result(_FakeResult(None)) == []
