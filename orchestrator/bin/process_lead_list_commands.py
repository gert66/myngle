#!/usr/bin/env python3
"""Process Sales Cockpit lead-list intake commands on the trusted VM.

The Control Center keeps the original upload in private storage. Its public,
token-protected worker route returns a short-lived signed download URL plus
list metadata. This worker performs deterministic intake and protected preflight. A cockpit
import command is the explicit live intent that may run enrichment and protected
publication. HubSpot is outside this flow.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib import error, request

ORCH_ROOT = Path(__file__).resolve().parents[1]
# In the repository, orchestrator/ sits under the repo root. On the live VM it
# is installed as /home/myngle/orchestrator beside /home/myngle/myngle.
_default_repo = ORCH_ROOT.parent
if not (_default_repo / "lead_list_intake.py").is_file():
    sibling_repo = ORCH_ROOT.parent / "myngle"
    if sibling_repo.is_dir():
        _default_repo = sibling_repo
REPO_ROOT = Path(os.environ.get("MYNGLE_REPO_ROOT", str(_default_repo)))
sys.path.insert(0, str(REPO_ROOT))

from lead_list_intake import analyze_lead_list  # noqa: E402
from lead_list_pipeline import build_dry_run_report, persist_intake_artifacts  # noqa: E402

TOKEN_FILE = ORCH_ROOT / "secrets" / "orchestrator_ingest_token"
PUSH_URL_FILE = ORCH_ROOT / "config" / "ops_push_url.txt"
ARTIFACTS_DIR = ORCH_ROOT / "lead_lists"


def commands_url(path=PUSH_URL_FILE):
    base = Path(path).read_text(encoding="utf-8").strip()
    suffix = "/api/public/orchestrator-snapshot"
    if not base.endswith(suffix):
        raise ValueError("ops_push_url.txt has unexpected format")
    return base[:-len(suffix)] + "/api/public/lead-list-commands"


def _call(url, token, *, method="GET", payload=None, timeout=30):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Sales-Cockpit-Lead-List-Worker/1.0",
        "x-orchestrator-token": token,
    })
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_commands(*, url=None, token=None):
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _call(url, token).get("commands", [])


def ack(list_id, status, *, status_note="", intake_report=None, url=None, token=None):
    """Report the intake result using the Control Center's lead-list contract."""
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    payload = {
        "list_id": list_id,
        "status": status,
        "status_note": status_note or None,
        "intake_report": intake_report,
    }
    return _call(url, token, method="POST", payload=payload)


def _download(url: str, target: Path, timeout: int = 60) -> None:
    req = request.Request(url, headers={"User-Agent": "Sales-Cockpit-Lead-List-Worker/1.0"})
    with request.urlopen(req, timeout=timeout) as resp, target.open("wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def _records(df):
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records"))




def _live_identity(list_id: str, list_name: str, import_plan: dict) -> tuple[str, str, str]:
    batch_id = str(import_plan.get("import_batch") or f"lead-list-{list_id.replace('-', '')[:12]}").strip()
    list_key = str(import_plan.get("prospect_list_key") or f"lead-list-{list_id.replace('-', '')[:12]}").strip()
    list_label = str(list_name or "Prospect List").strip()
    return batch_id, list_key, list_label


def _requested_action(command: dict, import_plan: dict) -> str:
    action = str(command.get("action") or "").strip().lower()
    if not action:
        control = import_plan.get("control") if isinstance(import_plan.get("control"), dict) else {}
        action = str(control.get("action") or "").strip().lower()
    return action or "dry_run"


def _apply_review_overrides(target: Path, command: dict, import_plan: dict) -> None:
    explicit = "review_overrides" in command or "review_overrides" in import_plan
    if not explicit:
        return
    raw = command.get("review_overrides", import_plan.get("review_overrides", []))
    if not isinstance(raw, list):
        raise ValueError("review_overrides must be a list")
    path = target / "reviewed_matches.json"
    if raw:
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    elif path.exists():
        path.unlink()


def _single_country(pipeline: dict) -> tuple[str, str] | None:
    worksets = pipeline.get("country_worksets") or []
    if len(worksets) != 1:
        return None
    workset = worksets[0]
    slug = str(workset.get("slug") or "").strip()
    label = str(workset.get("label") or slug).strip()
    return (slug, label) if slug else None


def _live_report_fields(preflight: dict) -> dict:
    return {
        "matched_existing": int(preflight.get("existing_matched") or 0),
        "new": int(preflight.get("new") or 0),
        "review_required": int(preflight.get("review_required_count") or 0),
        "blocked_rows": 0 if preflight.get("safety_preflight") == "GREEN" else int(preflight.get("review_required_count") or 0),
        "review_blockers": preflight.get("review_required") or [],
        "hubspot_sync": False,
        "import_plan_locked": bool(preflight.get("import_plan_locked")),
        "import_plan_sha256": preflight.get("import_plan_sha256"),
        "import_plan_drift": int(preflight.get("import_plan_drift") or 0),
        "safety_preflight": preflight.get("safety_preflight"),
    }


def process_command(command, progress=None):
    list_id = str(command.get("list_id") or "")
    download_url = str(command.get("file_url") or "")
    filename = str(command.get("original_filename") or "lead-list.xlsx")
    country = str(command.get("country") or "").strip()
    assigned_caller = str(command.get("cold_caller") or "").strip()
    list_name = str(command.get("name") or Path(filename).stem).strip()
    import_plan = command.get("import_plan") if isinstance(command.get("import_plan"), dict) else {}
    # HubSpot is intentionally impossible in this flow, even if stale UI metadata says otherwise.
    import_plan = json.loads(json.dumps(import_plan))
    options = import_plan.setdefault("options", {})
    if isinstance(options, dict):
        options["hubspot_sync"] = False
        options["hubspot"] = False
    fallback_country = str(import_plan.get("country_fallback") or "").strip()
    if not fallback_country and country not in {"", "Unknown", "Multi-country"}:
        fallback_country = country
    if not list_id or not download_url:
        raise ValueError("command is missing list_id/file_url")

    suffix = Path(filename).suffix.lower()
    if suffix not in {".xlsx", ".csv"}:
        raise ValueError(f"unsupported uploaded file type: {suffix or '(none)'}")

    with tempfile.TemporaryDirectory(prefix="lead-list-intake-") as td:
        local_path = Path(td) / ("input" + suffix)
        _download(download_url, local_path)
        result = analyze_lead_list(
            local_path,
            default_country=fallback_country,
            assigned_caller=assigned_caller,
            list_name=list_name,
            import_plan=import_plan,
        )

    pipeline = build_dry_run_report(result, import_plan)
    target = persist_intake_artifacts(list_id, result, pipeline, ARTIFACTS_DIR)
    report = dict(result.report)
    report.update({
        "source_sheet": result.source_sheet,
        "header_row": result.header_row,
        "file_sha256": result.file_sha256,
        "companies_needing_domain": report.get("companies_needing_domain_resolution", 0),
        "duplicate_company_rows": report.get("company_rows_collapsed", 0),
        "pipeline": pipeline,
        "artifacts_persisted": True,
        "hubspot_sync": False,
    })

    action = _requested_action(command, import_plan)
    if action == "dry_run":
        list_status = "ready" if pipeline.get("status") == "complete" else "review_required"
        note = (
            f"Dry run {'complete' if list_status == 'ready' else 'blocked'}: "
            f"{report['source_rows']} rows -> {report['unique_companies']} companies; "
            f"quality {report.get('quality_status', 'UNKNOWN')} / {report.get('decision')}. "
            "No enrichment supplier calls or external writes were performed."
        )
        return {"list_id": list_id, "status": list_status, "status_note": note, "intake_report": report}

    if pipeline.get("status") != "complete":
        return {
            "list_id": list_id,
            "status": "review_required",
            "status_note": "Intake blockers must be resolved before protected preflight.",
            "intake_report": report,
        }

    country_info = _single_country(pipeline)
    if not country_info:
        report["safety_preflight"] = "BLOCKED"
        report["review_blockers"] = [{
            "company_name": None,
            "source_company_key": None,
            "incoming_domain": None,
            "email_domain_hint": None,
            "reason": "Live self-service import currently requires exactly one country per upload.",
            "match_basis": "multi_country_upload",
            "candidate_company_ids": [],
            "review_actions": [],
        }]
        report["blocked_rows"] = int(report.get("source_rows") or 0)
        return {
            "list_id": list_id,
            "status": "review_required",
            "status_note": "Choose or upload exactly one country before live import.",
            "intake_report": report,
        }

    country_slug, _country_label = country_info
    batch_id, list_key, list_label = _live_identity(list_id, list_name, import_plan)
    report["prospect_list"] = {"list_key": list_key, "display_name": list_label, "caller": assigned_caller}
    _apply_review_overrides(target, command, import_plan)

    from lead_list_live import (
        build_live_preflight,
        combine_enrichment_exports,
        protected_publish_export,
        run_zyte_enrichment,
    )

    preflight = build_live_preflight(
        target, country_slug=country_slug, batch_id=batch_id, caller=assigned_caller,
        refresh_current=True,
    )
    report["live_preflight"] = preflight
    report.update(_live_report_fields(preflight))

    if preflight.get("safety_preflight") != "GREEN":
        return {
            "list_id": list_id,
            "status": "review_required",
            "status_note": (
                f"Protected preflight BLOCKED: {preflight.get('review_required_count', 0)} "
                "company match(es) require review."
            ),
            "intake_report": report,
        }

    if action != "import":
        return {
            "list_id": list_id,
            "status": "ready",
            "status_note": (
                f"READY TO IMPORT / GREEN: {report.get('unique_companies', 0)} companies; "
                f"{preflight.get('existing_matched', 0)} existing, {preflight.get('new', 0)} new. HubSpot OFF."
            ),
            "intake_report": report,
        }

    if progress:
        progress("enriching", "Import started. Enriching companies on the trusted VM.", report)
    outputs = run_zyte_enrichment(target, preflight, allow_supplier_calls=True)
    combined = combine_enrichment_exports(outputs, target, preflight, assigned_caller)

    if progress:
        progress("enriching", "Enrichment complete. Rechecking immutable Import Plan against live current.", report)

    final_preflight = build_live_preflight(
        target, country_slug=country_slug, batch_id=batch_id, caller=assigned_caller,
        refresh_current=True,
    )
    report["live_preflight"] = final_preflight
    report.update(_live_report_fields(final_preflight))
    if final_preflight.get("safety_preflight") != "GREEN":
        return {
            "list_id": list_id,
            "status": "review_required",
            "status_note": "Import stopped after enrichment because the immutable Import Plan drift check is no longer GREEN.",
            "intake_report": report,
        }

    if progress:
        progress("enriching", "Safety recheck GREEN. Publishing protected snapshot, ledger, GCS current and Prospect List.", report)

    live_result = protected_publish_export(
        combined,
        target,
        preflight=final_preflight,
        caller=assigned_caller,
        confirm_batch_id=batch_id,
        list_key=list_key,
        list_name=list_label,
    )
    report["live_result"] = live_result
    report["hubspot_sync"] = False
    if live_result.get("prospect_membership") != "complete":
        return {
            "list_id": list_id,
            "status": "failed",
            "status_note": "GCS publish completed but Prospect List membership failed. HubSpot remained OFF.",
            "intake_report": report,
        }
    return {
        "list_id": list_id,
        "status": "published",
        "status_note": (
            f"COMPLETE: {report.get('unique_companies', 0)} companies published to "
            f"{list_label}; HubSpot OFF."
        ),
        "intake_report": report,
    }

def process_once(*, url=None, token=None):
    commands = fetch_commands(url=url, token=token)
    results = []
    for command in commands:
        list_id = str(command.get("list_id") or "")
        try:
            def progress(status, note, report):
                ack(list_id, status, status_note=note, intake_report=report, url=url, token=token)
            outcome = process_command(command, progress=progress)
            ack(
                list_id,
                outcome["status"],
                status_note=outcome["status_note"],
                intake_report=outcome["intake_report"],
                url=url,
                token=token,
            )
            results.append((list_id, "executed"))
        except ValueError as exc:
            if list_id:
                ack(list_id, "review_required", status_note=str(exc), url=url, token=token)
            results.append((list_id, "stale"))
        except Exception as exc:
            try:
                if list_id:
                    ack(list_id, "failed", status_note=str(exc), url=url, token=token)
            except Exception:
                pass
            results.append((list_id, "error"))
    return results


def main():
    try:
        results = process_once()
    except (OSError, error.URLError, json.JSONDecodeError) as exc:
        print(f"lead-list command poll failed: {exc}", file=sys.stderr)
        return 1
    for list_id, status in results:
        print(f"{list_id} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
