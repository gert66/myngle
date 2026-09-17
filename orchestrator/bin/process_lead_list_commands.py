#!/usr/bin/env python3
"""Process Sales Cockpit lead-list intake commands on the trusted VM.

The Control Center keeps the original upload in private storage. Its public,
token-protected worker route returns a short-lived signed download URL plus
list metadata. This worker performs deterministic intake plus a downstream dry run. It persists
private derived worksets and reports the protection/enrichment/publication plan.
No enrichment supplier calls, HubSpot writes, or Sales Cockpit writes happen.
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


def process_command(command):
    list_id = str(command.get("list_id") or "")
    download_url = str(command.get("file_url") or "")
    filename = str(command.get("original_filename") or "lead-list.xlsx")
    country = str(command.get("country") or "").strip()
    assigned_caller = str(command.get("cold_caller") or "").strip()
    list_name = str(command.get("name") or Path(filename).stem).strip()
    import_plan = command.get("import_plan") if isinstance(command.get("import_plan"), dict) else {}
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
            default_country=country,
            assigned_caller=assigned_caller,
            list_name=list_name,
            import_plan=import_plan,
        )

    pipeline = build_dry_run_report(result, import_plan)
    persist_intake_artifacts(list_id, result, pipeline, ARTIFACTS_DIR)
    report = dict(result.report)
    report.update({
        "source_sheet": result.source_sheet,
        "header_row": result.header_row,
        "file_sha256": result.file_sha256,
        # UI compatibility aliases.
        "companies_needing_domain": report.get("companies_needing_domain_resolution", 0),
        "duplicate_company_rows": report.get("company_rows_collapsed", 0),
        "pipeline": pipeline,
        "artifacts_persisted": True,
    })
    decision = report.get("decision")
    list_status = "ready" if pipeline.get("status") == "complete" else "review_required"
    note = (
        f"Dry run {'complete' if list_status == 'ready' else 'blocked'}: "
        f"{report['source_rows']} rows -> {report['unique_companies']} companies; "
        f"quality {report.get('quality_status', 'UNKNOWN')} / {decision}. "
        "No enrichment supplier calls or external writes were performed."
    )
    return {
        "list_id": list_id,
        "status": list_status,
        "status_note": note,
        "intake_report": report,
    }


def process_once(*, url=None, token=None):
    commands = fetch_commands(url=url, token=token)
    results = []
    for command in commands:
        list_id = str(command.get("list_id") or "")
        try:
            outcome = process_command(command)
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
