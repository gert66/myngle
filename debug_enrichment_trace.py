"""
debug_enrichment_trace.py — Step-by-step enrichment debug tracer.

Runs the full enrichment pipeline for a single company and saves a
detailed JSON trace of every intermediate step so the Python pipeline
can be compared with the Lovable TypeScript port.

Normal batch behaviour is NOT changed: this script only wraps existing
pipeline functions; it does not modify scoring, prompts, or enrichment
logic.

Usage (Italy register / Lovable comparison):
    python debug_enrichment_trace.py --company "IET" --domain "iet.it" --country "Italy" --profile italy_register_icp_only
    python debug_enrichment_trace.py --company "IET" --domain "iet.it" --profile italy_register_icp_only --out debug_traces/IET_iet_it_italy_profile.json

Required environment variables (same as normal enrichment):
    ANTHROPIC_API_KEY
    SERPER_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Import pipeline functions (no new dependencies) ──────────────────────────

import enrich_clients_claude as _enc
import commercial_fit_scoring as _cfs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(v):
    """Convert value to JSON-serialisable form."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _safe_row(row: dict) -> dict:
    return {k: _safe(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Main trace runner
# ---------------------------------------------------------------------------

def run_trace(
    company_name: str,
    domain: str,
    country: str = "Italy",
    out_path: "Path | None" = None,
    anthropic_key: str = "",
    serper_key: str = "",
    scoring_profile: str = "italy_register_icp_only",
) -> dict:

    ts = datetime.now(timezone.utc).isoformat()
    trace: dict = {"_trace_version": 2, "timestamp": ts}

    # ── 0. API key warnings (written into JSON so the trace is self-documenting) ──
    api_warnings: list[str] = []
    if not serper_key:
        api_warnings.append("SERPER_API_KEY is not set — Serper queries will fail (no web evidence)")
    if not anthropic_key:
        api_warnings.append("ANTHROPIC_API_KEY is not set — model signal extraction will be skipped (all signals default to 0)")
    trace["api_warnings"] = api_warnings

    # ── 1. input ─────────────────────────────────────────────────────────────
    trace["input"] = {
        "company_name": company_name,
        "domain": domain,
        "country": country,
        "timestamp": ts,
        "scoring_profile": scoring_profile,
    }

    # ── 2. domain_resolution ─────────────────────────────────────────────────
    print("[trace] Step 0: domain validation …", flush=True)
    dv = _enc.validate_company_domain(
        company_name,
        domain,
        serper_key=serper_key,
        dry_run=False,
    )
    trace["domain_resolution"] = {
        "input_domain": domain,
        "canonical_domain": _enc.clean_domain(domain),
        "validated_domain": dv.get("validated_domain", ""),
        "domain_match_confidence": dv.get("domain_match_confidence", ""),
        "domain_used_for_enrichment": dv.get("domain_used_for_enrichment", ""),
        "suggested_domain": dv.get("suggested_domain", ""),
        "domain_validation_notes": dv.get("match_notes", dv.get("domain_validation_notes", "")),
        "all_dv_fields": _safe_row(dv),
    }

    # Determine effective URL (same logic as enrich_one_row)
    effective_url = domain
    if dv.get("domain_used_for_enrichment") == "suggested_domain" and dv.get("suggested_domain"):
        effective_url = _enc.normalize_url(dv["suggested_domain"])

    # ── 3+4. serper_queries + raw_serper_results ──────────────────────────────
    print("[trace] Step 2a: building Serper queries …", flush=True)
    target = _enc.normalize_url(effective_url) if effective_url else company_name
    queries = _enc._build_serper_queries(company_name, target)
    labels = _enc._SERPER_QUERY_LABELS

    trace["serper_queries"] = [
        {"index": i + 1, "label": lbl, "query": q}
        for i, (q, lbl) in enumerate(zip(queries, labels))
    ]

    print("[trace] Step 2b: executing Serper queries …", flush=True)
    query_groups: list = []
    raw_results_per_query: list[dict] = []
    serper_errors: list[str] = []

    for i, (q, label) in enumerate(zip(queries, labels), 1):
        hits, http_status, raw_json, err_str = _enc._call_serper(q, serper_key)
        query_groups.append((label, hits))
        if err_str:
            serper_errors.append(f"[{label}] http={http_status}: {err_str}")
        raw_results_per_query.append({
            "query_index": i,
            "query_label": label,
            "query": q,
            "http_status": http_status,
            "hit_count": len(hits),
            "error": err_str,
            "results": [
                {
                    "title": h.get("title", ""),
                    "url": h.get("link", ""),
                    "snippet": h.get("snippet", ""),
                    "source_domain": _enc.clean_domain(h.get("link", "")),
                    "source_type": _enc._classify_serper_source(
                        h.get("link", ""), h.get("title", "")
                    ),
                }
                for h in (hits or [])
            ],
        })

    # Detect Serper key rejection (403 on first call = key invalid or not set)
    _first_status = raw_results_per_query[0]["http_status"] if raw_results_per_query else None
    _serper_ok = not serper_errors and sum(r["hit_count"] for r in raw_results_per_query) > 0
    if _first_status == 403 and serper_key:
        api_warnings.append("SERPER_API_KEY was rejected (HTTP 403) — no web evidence in this trace")
    elif serper_errors and serper_key:
        api_warnings.append(f"Serper returned errors for {len(serper_errors)} of {len(queries)} queries")

    trace["raw_serper_results"] = raw_results_per_query
    trace["serper_status"] = {
        "key_present": bool(serper_key),
        "succeeded": _serper_ok,
        "total_hits": sum(r["hit_count"] for r in raw_results_per_query),
        "errors": serper_errors,
    }

    # ── 5. filtered_evidence ─────────────────────────────────────────────────
    results_text = _enc._format_serper_results(query_groups)
    total_hits = sum(len(h) for _, h in query_groups)

    evidence_kept: list[dict] = []
    evidence_ignored: list[dict] = []
    canonical = _enc.clean_domain(effective_url)

    for qr in raw_results_per_query:
        for r in qr["results"]:
            src_domain = r["source_domain"]
            is_domain_matched = bool(canonical and src_domain and (
                src_domain == canonical or src_domain.endswith("." + canonical)
            ))
            entry = {
                "query_label": qr["query_label"],
                "title": r["title"],
                "url": r["url"],
                "snippet": r["snippet"],
                "source_type": r["source_type"],
                "source_domain": src_domain,
                "domain_matched": is_domain_matched,
            }
            if r["snippet"] or r["title"]:
                entry["reason"] = "included in formatted evidence"
                evidence_kept.append(entry)
            else:
                entry["reason"] = "no title or snippet"
                evidence_ignored.append(entry)

    trace["filtered_evidence"] = {
        "total_raw_hits": total_hits,
        "kept_count": len(evidence_kept),
        "ignored_count": len(evidence_ignored),
        "evidence_kept": evidence_kept,
        "evidence_ignored": evidence_ignored,
    }

    # ── 6. evidence_packet_for_model ─────────────────────────────────────────
    trace["evidence_packet_for_model"] = {
        "text": results_text,
        "character_count": len(results_text),
        "note": "Formatted Serper evidence passed verbatim to Claude in Step 2 prompt",
    }

    # ── 7. model_signal_prompt ───────────────────────────────────────────────
    search_instruction = (
        f"Now analyze this company based on the web search results provided below.\n\n"
        f"Company: {target}\n\n"
        f"Web search results (retrieved via Serper Google Search, grouped by signal type):\n"
        f"{results_text}\n\n"
        "Evidence quality rules:\n"
        "- Only mark a buying signal as present when a result contains company-specific, "
        "contextual evidence — not just a keyword in a URL or a generic snippet.\n"
        "- A competitor signal requires the provider name to appear in a meaningful context "
        "(HR case study, employee benefit page, vendor review) not just a search result title.\n"
        "- Set lead_score to High only when two or more clearly distinct strong signals appear.\n"
        "- Base your analysis ONLY on the search results above. Do not invent or infer evidence."
    )
    step2_full_prompt = _enc.STEP2_STATIC_PREFIX + f"\n\n{search_instruction}"

    trace["model_signal_prompt"] = {
        "step2_static_prefix_chars": len(_enc.STEP2_STATIC_PREFIX),
        "step2_full_prompt_chars": len(step2_full_prompt),
        "step2_full_prompt": step2_full_prompt,
        "note": "API key values are never included in this trace",
    }

    # ── Run enrich_one_row WITHOUT model signal extraction ────────────────────
    # We run Step 2 (Serper ICP) here. Model signals are extracted separately
    # below so we can attach _debug_capture and record the raw Anthropic response.
    print("[trace] Step 2c: running enrich_one_row (extract_model_signals=False) …", flush=True)
    t_start = time.time()
    enriched_row, _debug_rec = _enc.enrich_one_row(
        company_name=company_name,
        raw_url=effective_url,
        api_key=anthropic_key,
        delay=0.5,
        use_playwright=False,
        search_provider=_enc.STEP2_PROVIDER_SERPER,
        serper_key=serper_key,
        dry_run=False,
        extract_model_signals=False,   # We call run_model_signal_extraction below
        include_signal_evidence=True,
        run_step1_enrichment=False,    # Step 1 needs Jina/Lusha; skip for trace
        run_step2_enrichment=True,
        scoring_profile=scoring_profile,
    )

    # ── Step 3: model signal extraction with raw response capture ────────────
    print("[trace] Step 3: model signal extraction (capturing raw response) …", flush=True)
    _debug_capture: dict = {}
    ms_fields = _enc.run_model_signal_extraction(
        company_name=company_name,
        raw_url=effective_url,
        enrichment_row=enriched_row,
        api_key=anthropic_key,
        model_id=_enc.MODEL_STEP2,
        include_evidence=True,
        search_provider=_enc.STEP2_PROVIDER_SERPER,
        _debug_capture=_debug_capture,
    )
    enriched_row.update(ms_fields)
    t_elapsed = time.time() - t_start

    # ── 8. model_signal_raw_response ─────────────────────────────────────────
    _anthropic_ok = bool(_debug_capture.get("raw_text"))
    if not anthropic_key:
        pass  # warning already added before the run
    elif not _anthropic_ok:
        _ms_reason = enriched_row.get("model_signal_manual_review_reason", "")
        api_warnings.append(f"Anthropic call failed or returned no text — {_ms_reason}")

    trace["model_signal_raw_response"] = {
        "raw_text": _debug_capture.get("raw_text", None),
        "retry_was_needed": _debug_capture.get("retry", False),
        "raw_text_attempt1_if_retry": _debug_capture.get("raw_text_attempt1", None),
        "model_id": _debug_capture.get("model_id", _enc.MODEL_STEP2),
        "anthropic_called": _anthropic_ok,
        "api_key_present": bool(anthropic_key),
        "model_signal_search_quality": enriched_row.get("model_signal_search_quality", ""),
        "model_signal_needs_manual_review": enriched_row.get("model_signal_needs_manual_review", ""),
        "model_signal_manual_review_reason": enriched_row.get("model_signal_manual_review_reason", ""),
        "enrichment_elapsed_seconds": round(t_elapsed, 2),
    }

    # ── 9. model_signal_parsed ───────────────────────────────────────────────
    _SIGNAL_FIELDS = [
        "sig_intl_footprint_score",
        "sig_foreign_hq_score",
        "sig_explicit_lnd_score",
        "sig_multicultural_score",
        "sig_employer_branding_score",
        "sig_rapid_growth_score",
        "sig_lnd_onboarding_score",
        "ti_language_english_score",
        "ti_onboarding_score",
        "ti_leadership_score",
        "ti_broader_professional_score",
        "ti_team_collab_score",
        "ti_intercultural_score",
        "ti_negotiation_sales_score",
        "model_signal_overall_confidence_score",
        "model_signal_needs_manual_review",
    ]
    _EVIDENCE_FIELDS = [f for f in enriched_row if "evidence" in f or "rationale" in f or "reason" in f]

    trace["model_signal_parsed"] = {
        f: _safe(enriched_row.get(f, "")) for f in _SIGNAL_FIELDS
    }
    trace["model_signal_parsed"]["evidence_fields"] = {
        f: _safe(enriched_row.get(f, "")) for f in _EVIDENCE_FIELDS[:20]
    }

    # ── 10. model_signal_validated ───────────────────────────────────────────
    _coerced = {}
    _defaults_used = {}
    for f in _SIGNAL_FIELDS:
        raw_val = enriched_row.get(f, "")
        _coerced[f] = _safe(raw_val)
        if raw_val == "" or raw_val is None:
            _defaults_used[f] = "defaulted to 0 (missing)"
        elif isinstance(raw_val, (int, float)):
            if f.endswith("_score") and f != "model_signal_overall_confidence_score":
                clamped = max(0.0, min(3.0, float(raw_val)))
                if clamped != float(raw_val):
                    _defaults_used[f] = f"clamped from {raw_val} to {clamped}"

    trace["model_signal_validated"] = {
        "signals": _coerced,
        "defaults_applied": _defaults_used,
        "note": "Values after _coerce_model_signals inside run_model_signal_extraction",
    }

    # ── 11. score_inputs ─────────────────────────────────────────────────────
    _SCORE_INPUT_MAP = {
        "sig_foreign_hq_score":        "score_input_foreign_hq",
        "sig_explicit_lnd_score":      "score_input_explicit_lnd",
        "sig_intl_footprint_score":    "score_input_intl_footprint",
        "sig_employer_branding_score": "score_input_employer_branding",
        "sig_lnd_onboarding_score":    "score_input_lnd_onboarding",
        "ti_onboarding_score":         "score_input_ti_onboarding",
        "sig_rapid_growth_score":      "score_input_rapid_growth",
    }
    trace["score_inputs"] = {
        "scoring_profile": scoring_profile,
        **{alias: _safe(enriched_row.get(field, "")) for field, alias in _SCORE_INPUT_MAP.items()},
        "employee_range": _safe(
            enriched_row.get("lusha_api_employee_range")
            or enriched_row.get("lusha_employee_range")
            or enriched_row.get("employee_range")
            or enriched_row.get("company_size")
            or ""
        ),
        "company_size_score": _safe(enriched_row.get("company_size_score", "")),
        "company_size_missing": _safe(enriched_row.get("company_size_missing", "")),
    }

    # ── 12. score_components ─────────────────────────────────────────────────
    print("[trace] Scoring …", flush=True)
    score_out = _cfs.score_company(enriched_row, params={"scoring_profile": scoring_profile})

    profile_cfg = _cfs.SCORING_PROFILES.get(scoring_profile, _cfs.SCORING_PROFILES["default"])
    _model_w = profile_cfg["model_weight"]
    _size_w  = profile_cfg["size_weight"]
    _sig_k   = profile_cfg["sigmoid_k"]

    per_signal = {}
    for field, coeff in _cfs.LEAN_COEFFICIENTS.items():
        raw_val = float(enriched_row.get(field) or 0)
        norm = max(0.0, min(raw_val, 3.0)) / 3.0
        per_signal[field] = {
            "raw_value": raw_val,
            "normalized": round(norm, 6),
            "coefficient": coeff,
            "contribution": round(coeff * norm, 6),
        }

    trace["score_components"] = {
        "scoring_profile": scoring_profile,
        "coefficients": dict(_cfs.LEAN_COEFFICIENTS),
        "intercept": _cfs.INTERCEPT,
        "sigmoid_k": _sig_k,
        "model_weight": _model_w,
        "size_weight": _size_w,
        "per_signal": per_signal,
        "lr_z_score": _safe(score_out.get("lr_z_score")),
        "lean_model_prob": _safe(score_out.get("lean_model_prob")),
        "icp_similarity_score": _safe(score_out.get("icp_similarity_score")),
        "company_size_score": _safe(score_out.get("company_size_score")),
        "weighted_model_component": _safe(score_out.get("weighted_model_component")),
        "weighted_size_component": _safe(score_out.get("weighted_size_component")),
        "final_commercial_fit_score": _safe(score_out.get("final_commercial_fit_score")),
        "commercial_tier": _safe(score_out.get("commercial_tier")),
        "outreach_readiness_status": _safe(
            enriched_row.get("outreach_readiness_status", score_out.get("outreach_readiness_status", ""))
        ),
        "top_score_drivers": _safe(score_out.get("top_score_drivers")),
        "weak_score_drivers": _safe(score_out.get("weak_score_drivers")),
        "data_quality_flag": _safe(score_out.get("data_quality_flag")),
        "missing_scoring_fields": score_out.get("missing_scoring_fields", []),
        "all_score_output": _safe_row(score_out),
    }

    # ── 13. final_output_row ─────────────────────────────────────────────────
    final_row = dict(enriched_row)
    final_row.update(score_out)
    trace["final_output_row"] = _safe_row(final_row)

    # Write api_warnings back (may have grown during run)
    trace["api_warnings"] = api_warnings

    # ── Write output ──────────────────────────────────────────────────────────
    if out_path is None:
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in company_name)
        safe_dom  = "".join(c if c.isalnum() or c in "-_" else "_" for c in domain)
        out_path = Path("debug_traces") / f"{safe_name}_{safe_dom}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Safety: do not write if the trace contains anything that looks like an API key.
    raw_json_str = json.dumps(trace, indent=2, ensure_ascii=False)
    for _secret_hint in [anthropic_key, serper_key]:
        if _secret_hint and len(_secret_hint) > 8 and _secret_hint in raw_json_str:
            print(
                f"[trace] SAFETY ABORT: API key found in trace output. "
                f"File not written. Please report this bug.",
                file=sys.stderr,
            )
            sys.exit(1)

    out_path.write_text(raw_json_str, encoding="utf-8")
    return trace


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run enrichment for one company and save a full step-by-step JSON trace."
    )
    parser.add_argument("--company", required=True, help="Company name, e.g. 'IET'")
    parser.add_argument("--domain",  required=True, help="Domain, e.g. 'iet.it'")
    parser.add_argument("--country", default="Italy", help="Country (default: Italy)")
    parser.add_argument("--out",     default=None,   help="Output JSON path")
    parser.add_argument(
        "--profile",
        default="italy_register_icp_only",
        help="Scoring profile (default: italy_register_icp_only for Lovable comparison)",
    )
    args = parser.parse_args()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    serper_key    = os.environ.get("SERPER_API_KEY", "")

    if not serper_key:
        print("WARNING: SERPER_API_KEY not set — Serper queries will fail (no web evidence).", file=sys.stderr)
    if not anthropic_key:
        print("WARNING: ANTHROPIC_API_KEY not set — model signals will default to 0.", file=sys.stderr)

    out_path = Path(args.out) if args.out else None

    print(f"\n[trace] Company : {args.company}")
    print(f"[trace] Domain  : {args.domain}")
    print(f"[trace] Country : {args.country}")
    print(f"[trace] Profile : {args.profile}\n")

    trace = run_trace(
        company_name=args.company,
        domain=args.domain,
        country=args.country,
        out_path=out_path,
        anthropic_key=anthropic_key,
        serper_key=serper_key,
        scoring_profile=args.profile,
    )

    if out_path is None:
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.company)
        safe_dom  = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.domain)
        out_path = Path("debug_traces") / f"{safe_name}_{safe_dom}.json"

    sc  = trace.get("score_components", {})
    msr = trace.get("model_signal_raw_response", {})
    ss  = trace.get("serper_status", {})

    print(f"\n[trace] Done.")
    print(f"[trace] Output saved to    : {out_path}")
    print(f"[trace] Serper succeeded   : {ss.get('succeeded')}  (total hits: {ss.get('total_hits', 0)})")
    print(f"[trace] Anthropic called   : {msr.get('anthropic_called')}  (raw text captured: {bool(msr.get('raw_text'))})")
    print(f"[trace] Final score        : {sc.get('final_commercial_fit_score', 'n/a')}")
    print(f"[trace] Tier               : {sc.get('commercial_tier', 'n/a')}")
    print(f"[trace] ICP similarity     : {sc.get('icp_similarity_score', 'n/a')}")
    if trace.get("api_warnings"):
        print(f"\n[trace] Warnings:")
        for w in trace["api_warnings"]:
            print(f"   ! {w}")
    print(f"\n[trace] Pipeline functions reused (unchanged):")
    print(f"   enrich_clients_claude.validate_company_domain()")
    print(f"   enrich_clients_claude._build_serper_queries()")
    print(f"   enrich_clients_claude._call_serper()")
    print(f"   enrich_clients_claude._format_serper_results()")
    print(f"   enrich_clients_claude._classify_serper_source()")
    print(f"   enrich_clients_claude.enrich_one_row()  (extract_model_signals=False, run_step1_enrichment=False)")
    print(f"   enrich_clients_claude.run_model_signal_extraction()  (_debug_capture=...)")
    print(f"   commercial_fit_scoring.score_company()")
    print(f"\n[trace] Normal batch behaviour is unaffected by this script.")


if __name__ == "__main__":
    main()
