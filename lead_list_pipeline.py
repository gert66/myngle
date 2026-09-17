"""Safe downstream planner for Control Center lead-list imports.

The first production use is deliberately dry-run only. It proves the wiring,
persists deterministic intake artifacts, and reports exactly what later stages
would do. It never calls enrichment suppliers, writes Sales Cockpit, or writes
HubSpot.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from lead_list_intake import IntakeResult
from lead_list_config_registry import country_folder_slug


def _options(import_plan: Mapping[str, Any] | None) -> dict[str, bool]:
    raw = (import_plan or {}).get("options") if isinstance(import_plan, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "enrich": bool(raw.get("enrich", True)),
        "merge": bool(raw.get("merge", True)),
        "update": bool(raw.get("update", True)),
        "publish": bool(raw.get("publish", False)),
        "hubspot_sync": bool(raw.get("hubspot_sync", raw.get("hubspot", False))),
        "split_multi_value": bool(raw.get("splitMultiValue", False)),
    }


def _country_worksets(result: IntakeResult) -> list[dict[str, Any]]:
    """Partition normalized rows by their effective country for later GCS publication."""
    rows = result.normalized_rows
    companies = result.companies
    if rows is None or rows.empty or "country" not in rows.columns:
        return []

    worksets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows["country"].fillna("").astype(str).tolist():
        label = raw.strip()
        if not label:
            continue
        slug = country_folder_slug(label)
        if slug in seen:
            continue
        seen.add(slug)
        row_mask = rows["country"].fillna("").astype(str).str.strip().map(country_folder_slug) == slug
        source_rows = int(row_mask.sum())
        company_count = 0
        if companies is not None and not companies.empty and "country" in companies.columns:
            company_mask = companies["country"].fillna("").astype(str).str.strip().map(country_folder_slug) == slug
            company_count = int(company_mask.sum())
        worksets.append({
            "label": label,
            "slug": slug,
            "source_rows": source_rows,
            "companies": company_count,
            "target_prefix": f"{slug}/current",
        })
    return worksets


def build_dry_run_report(
    result: IntakeResult,
    import_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the full downstream plan without performing external writes."""
    report = result.report
    options = _options(import_plan)
    companies = result.companies
    company_count = int(len(companies))
    missing_domain = int(report.get("companies_needing_domain_resolution") or 0)
    email_hints = int(report.get("companies_with_email_domain_hint") or 0)
    decision = str(report.get("decision") or "UNKNOWN")
    intake_ok = decision == "READY"
    country_worksets = _country_worksets(result)

    stages: list[dict[str, Any]] = [{
        "key": "intake",
        "label": "Intake & validation",
        "status": "complete" if intake_ok else "blocked",
        "count": int(report.get("source_rows") or 0),
        "summary": (
            f"{report.get('source_rows', 0)} rows -> {company_count} unique companies. "
            f"Quality {report.get('quality_status', 'UNKNOWN')} / {decision}."
        ),
    }]

    stages.append({
        "key": "country_partition",
        "label": "Country worksets",
        "status": "simulated" if intake_ok else "blocked",
        "count": len(country_worksets) if intake_ok else 0,
        "summary": (
            "; ".join(
                f"{w['label']}: {w['source_rows']} rows -> {w['target_prefix']}"
                for w in country_worksets
            )
            + ". Target only; no GCS write in dry run."
            if intake_ok and country_worksets
            else ("No country workset could be formed." if intake_ok else "Held until intake is approved.")
        ),
    })

    stages.append({
        "key": "protection",
        "label": "Customer protection",
        "status": "simulated" if intake_ok else "blocked",
        "count": company_count if intake_ok else 0,
        "summary": (
            f"Would check {company_count} companies against HubSpot, Account Management, "
            "and existing Sales Cockpit companies before any enrichment."
            if intake_ok else "Held until intake is approved."
        ),
    })

    enrich_candidates = company_count if intake_ok and options["enrich"] else 0
    stages.append({
        "key": "enrichment",
        "label": "Enrichment",
        "status": "simulated" if enrich_candidates else ("skipped" if intake_ok else "blocked"),
        "count": enrich_candidates,
        "summary": (
            f"Would enrich up to {enrich_candidates} eligible companies after protection. "
            f"{missing_domain} currently need a domain; {email_hints} have a usable work-email domain hint."
            if enrich_candidates else
            ("Disabled by import options." if intake_ok else "Held until intake is approved.")
        ),
    })

    publish_candidates = company_count if intake_ok and options["publish"] else 0
    stages.append({
        "key": "cockpit_publish",
        "label": "Sales Cockpit publication",
        "status": "simulated" if publish_candidates else ("skipped" if intake_ok else "blocked"),
        "count": publish_candidates,
        "summary": (
            f"Would publish up to {publish_candidates} approved companies to the selected caller's Prospect List."
            if publish_candidates else
            ("Publication is switched off for this import." if intake_ok else "Held until intake is approved.")
        ),
    })

    hubspot_candidates = company_count if intake_ok and options["hubspot_sync"] else 0
    stages.append({
        "key": "hubspot_sync",
        "label": "HubSpot sync",
        "status": "simulated" if hubspot_candidates else ("skipped" if intake_ok else "blocked"),
        "count": hubspot_candidates,
        "summary": (
            f"Would sync up to {hubspot_candidates} approved companies to HubSpot after validation."
            if hubspot_candidates else
            ("HubSpot sync is switched off for this import." if intake_ok else "Held until intake is approved.")
        ),
    })

    blockers = [
        *[str(x) for x in report.get("hard_stops", [])],
        *[str(x) for x in report.get("review_reasons", [])],
    ]
    return {
        "mode": "dry_run",
        "status": "complete" if intake_ok else "blocked",
        "writes_performed": 0,
        "external_write_calls": 0,
        "supplier_calls": 0,
        "options": options,
        "country_worksets": country_worksets,
        "stages": stages,
        "blockers": blockers,
        "live_guardrails": {
            "prematch_current_country": "required",
            "ambiguous_matches_allowed": 0,
            "before_snapshot": "required_before_first_write",
            "batch_ledger": "required",
            "selective_rollback": "required",
            "hubspot_bulk_sync": "blocked_for_initial_live_import",
        },
        "next_action": (
            "Review this dry run, then run the protected live preflight before enrichment or publication."
            if intake_ok else "Resolve the intake blockers before any downstream processing."
        ),
    }


def persist_intake_artifacts(
    list_id: str,
    result: IntakeResult,
    pipeline_report: Mapping[str, Any],
    root: str | Path,
) -> Path:
    """Persist derived worksets for later stages with private file permissions."""
    target = Path(root) / list_id
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass

    files = {
        "normalized_rows.jsonl": result.normalized_rows.to_json(orient="records", lines=True),
        "companies.jsonl": result.companies.to_json(orient="records", lines=True),
        "intake_report.json": json.dumps(result.report, indent=2, ensure_ascii=False),
        "pipeline_report.json": json.dumps(dict(pipeline_report), indent=2, ensure_ascii=False),
    }
    for name, content in files.items():
        path = target / name
        path.write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    countries_root = target / "countries"
    for workset in pipeline_report.get("country_worksets", []):
        slug = str(workset.get("slug") or "").strip()
        if not slug:
            continue
        country_dir = countries_root / slug
        country_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(country_dir, 0o700)
        except OSError:
            pass
        row_mask = result.normalized_rows["country"].fillna("").astype(str).str.strip().map(country_folder_slug) == slug
        company_mask = result.companies["country"].fillna("").astype(str).str.strip().map(country_folder_slug) == slug if not result.companies.empty else []
        country_files = {
            "normalized_rows.jsonl": result.normalized_rows[row_mask].to_json(orient="records", lines=True),
            "companies.jsonl": result.companies[company_mask].to_json(orient="records", lines=True) if not result.companies.empty else "",
        }
        for name, content in country_files.items():
            path = country_dir / name
            path.write_text(content + ("\n" if content and not content.endswith("\n") else ""), encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    return target
