"""Manual test app for Lead Prioritizer v2 HQ detection.

Run with:
    streamlit run lead_prioritizer_test_app.py

API keys are read from the environment:
    SERPER_API_KEY
    ANTHROPIC_API_KEY

This is a thin manual harness around ``prioritize_single_lead`` — it does not
contain any HQ logic of its own.
"""

from __future__ import annotations

import os
from dataclasses import asdict

import streamlit as st

from lead_output_schema import LeadInput
from lead_prioritizer_core import prioritize_single_lead

st.set_page_config(page_title="Lead Prioritizer v2 — HQ test", layout="centered")
st.title("Lead Prioritizer v2 — HQ test")

def _get_key(name: str) -> str:
    """Read a key from Streamlit secrets first, then environment variables."""
    try:
        if name in st.secrets:
            return str(st.secrets[name] or "")
    except Exception:
        # st.secrets raises if no secrets.toml exists — fall back to env.
        pass
    return os.environ.get(name, "")


_serper_key = _get_key("SERPER_API_KEY")
_anthropic_key = _get_key("ANTHROPIC_API_KEY")

with st.sidebar:
    st.header("API keys (secrets or environment)")
    st.write("SERPER_API_KEY:", "✅ set" if _serper_key else "❌ missing")
    st.write("ANTHROPIC_API_KEY:", "✅ set" if _anthropic_key else "❌ missing")
    st.caption(
        "Keys are read from `.streamlit/secrets.toml` first, then environment "
        "variables. Either of:\n\n"
        "secrets.toml:\n\n"
        "`SERPER_API_KEY = \"...\"`\n\n"
        "`ANTHROPIC_API_KEY = \"...\"`\n\n"
        "or environment:\n\n"
        "`export SERPER_API_KEY=...`\n\n"
        "`export ANTHROPIC_API_KEY=...`"
    )

st.subheader("Lead")
company_name = st.text_input("Company name", "")
domain = st.text_input("Domain", "")
input_country = st.text_input("Input country", "Italy")
run_full_pipeline = st.checkbox("Run full v2 single-lead pipeline", value=False)
st.caption(
    "Full pipeline runs HQ, non-HQ evidence, signal extraction, app summaries, "
    "scoring, and caller/app fields."
)
collect_non_hq = st.checkbox("Collect non-HQ enrichment evidence", value=False)
extract_non_hq = st.checkbox("Extract non-HQ signals from evidence", value=False)
build_app_summary = st.checkbox("Build app/evidence summary fields", value=False)
calculate_score = st.checkbox("Calculate commercial fit score", value=False)
build_caller_fields = st.checkbox("Build caller/app fields", value=False)

run = st.button("Run HQ detection", type="primary")

if run:
    if not company_name.strip() and not domain.strip():
        st.error("Enter at least a company name or a domain.")
        st.stop()
    if not _serper_key or not _anthropic_key:
        st.warning(
            "One or both API keys are missing — the run may return an AI error / "
            "manual review result. Set SERPER_API_KEY and ANTHROPIC_API_KEY."
        )

    with st.spinner("Running one Serper search + AI HQ interpretation…"):
        result = prioritize_single_lead(
            LeadInput(
                company_name=company_name.strip(),
                domain=domain.strip() or None,
                input_country=input_country.strip() or None,
            ),
            serper_api_key=_serper_key,
            anthropic_api_key=_anthropic_key,
            default_input_country="Italy",
            collect_non_hq_evidence=collect_non_hq,
            extract_non_hq_signals_flag=extract_non_hq,
            build_app_summary_fields_flag=build_app_summary,
            calculate_commercial_score_flag=calculate_score,
            build_caller_app_fields_flag=build_caller_fields,
            run_full_v2_pipeline=run_full_pipeline,
        )

    # ── Headline ────────────────────────────────────────────────────────────
    score = result.sig_foreign_hq_score_for_next_scoring
    c1, c2, c3 = st.columns(3)
    c1.metric("HQ score (next scoring)", "—" if score is None else f"{score:g}")
    c2.metric("Foreign HQ", str(result.foreign_hq_simple))
    c3.metric("Manual review", "Yes" if result.needs_manual_review else "No")
    st.caption(f"Pipeline mode: **{result.v2_pipeline_mode}**")

    # ── Important output fields ───────────────────────────────────────────────
    st.subheader("Output fields")
    _display_keys = [
        "domain_root",
        "query_used",
        "ai_hq_classification",
        "ai_hq_confidence",
        "ai_parent_company",
        "ai_parent_hq_country",
        "ai_parent_hq_city",
        "hq_detected_country",
        "hq_detected_city",
        "foreign_hq_simple",
        "sig_foreign_hq_score_for_next_scoring",
        "needs_manual_review",
        "hq_reason",
        "hq_evidence_url",
        "hq_evidence_quote",
        "ai_hq_error",
    ]
    rd = asdict(result)
    st.table([{"field": k, "value": rd.get(k)} for k in _display_keys])

    st.subheader("Raw AI JSON (ai_hq_raw_json)")
    st.code(rd.get("ai_hq_raw_json") or "(none)", language="json")

    st.subheader("Non-HQ enrichment placeholders")
    st.caption("Not implemented yet — these stay empty until non-HQ enrichment is added.")
    _placeholder_keys = [
        "sig_international_profile_score",
        "sig_onboarding_training_need_score",
        "sig_company_size_complexity_score",
        "sig_icp_keyword_match_score",
    ]
    st.table([{"field": k, "value": rd.get(k)} for k in _placeholder_keys])

    st.subheader("Non-HQ enrichment evidence")
    _evidence = rd.get("evidence_items") or []
    if not _evidence:
        st.caption(
            "No evidence collected. Tick 'Collect non-HQ enrichment evidence' "
            "and provide a Serper key to gather evidence."
        )
    else:
        st.table([
            {
                "signal_name":    e.get("signal_name"),
                "source_type":    e.get("source_type"),
                "source_title":   e.get("source_title"),
                "source_url":     e.get("source_url"),
                "source_snippet": e.get("source_snippet"),
                "query_used":     e.get("query_used"),
            }
            for e in _evidence
        ])

    st.subheader("Non-HQ extracted signals")
    _signals = rd.get("signals") or []
    if not _signals:
        st.caption(
            "No signals extracted. Tick 'Extract non-HQ signals from evidence' "
            "(evidence must be collected first)."
        )
    else:
        st.table([
            {
                "signal_name":       s.get("signal_name"),
                "signal_score":      s.get("signal_score"),
                "signal_confidence": s.get("signal_confidence"),
                "signal_value":      s.get("signal_value"),
                "signal_reason":     s.get("signal_reason"),
                "evidence_url":      s.get("evidence_url"),
                "evidence_quote":    s.get("evidence_quote"),
            }
            for s in _signals
        ])

    st.subheader("App/evidence summary fields")
    if not any(rd.get(k) for k in
               ("evidence_summary_app", "key_source_links_app", "advanced_notes_app")):
        st.caption(
            "No summary built. Tick 'Build app/evidence summary fields' "
            "(signals/evidence must be present)."
        )
    else:
        st.markdown("**evidence_summary_app**")
        st.text(rd.get("evidence_summary_app") or "(none)")
        st.markdown("**key_source_links_app**")
        st.text(rd.get("key_source_links_app") or "(none)")
        st.markdown("**advanced_notes_app**")
        st.text(rd.get("advanced_notes_app") or "(none)")

    st.subheader("Commercial fit score")
    if rd.get("final_commercial_fit_score") is None and rd.get("scoring_profile") is None:
        st.caption("No score calculated. Tick 'Calculate commercial fit score'.")
    else:
        s1, s2, s3 = st.columns(3)
        _fcs = rd.get("final_commercial_fit_score")
        s1.metric("Final commercial fit score", "—" if _fcs is None else f"{_fcs:g}")
        s2.metric("Commercial tier", rd.get("commercial_tier") or "—")
        _prob = rd.get("lean_model_prob")
        s3.metric("Lean model prob", "—" if _prob is None else f"{_prob:g}")
        _score_keys = [
            "final_commercial_fit_score", "commercial_tier", "icp_similarity_score",
            "lean_model_prob", "lr_z_score", "scoring_profile", "scoring_notes",
            "missing_scoring_fields",
            "score_input_foreign_hq", "score_input_intl_footprint",
            "score_input_explicit_lnd", "score_input_lnd_onboarding",
            "score_input_rapid_growth", "v2_score_input_mapping_note",
        ]
        st.table([{"field": k, "value": rd.get(k)} for k in _score_keys])

    st.subheader("Caller/app fields")
    _caller_keys = [
        "commercial_fit_score_app", "commercial_tier_app",
        "what_is_hot_app", "what_is_not_app", "why_relevant_app",
        "caller_angle_app", "call_starter_app", "caution_app",
        "foreign_hq_signal_used_in_app", "foreign_hq_country_app", "foreign_hq_city_app",
    ]
    if not any(rd.get(k) is not None for k in _caller_keys):
        st.caption("No caller/app fields built. Tick 'Build caller/app fields'.")
    else:
        st.table([{"field": k, "value": rd.get(k)} for k in _caller_keys])

    with st.expander("Full result (all fields)"):
        st.json(rd)
