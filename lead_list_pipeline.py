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
        "stages": stages,
        "blockers": blockers,
        "next_action": (
            "Review this dry run, then explicitly enable the real pipeline."
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
    return target
