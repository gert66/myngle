"""Protected VM live route for Control Center Lead Lists.

Default behavior is read-only preflight. External writes require an explicit
batch confirmation and a green safety report. HubSpot is never called here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib import request as urllib_request
from typing import Any

from lead_list_safety import (
    apply_reviewed_match_overrides,
    build_enrichment_plan,
    prematch_companies,
    write_before_snapshot,
)
from lovable_gcs_upload import DEFAULT_GCS_BUCKET

DEFAULT_GCLOUD = Path("/home/myngle/google-cloud-sdk/bin/gcloud")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _gcloud() -> str:
    found = shutil.which("gcloud")
    if found:
        return found
    if DEFAULT_GCLOUD.is_file():
        return str(DEFAULT_GCLOUD)
    raise RuntimeError("gcloud is unavailable on the VM")


def _cp_from_gcs(source: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([_gcloud(), "storage", "cp", source, str(target)], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "GCS read failed")[-1200:])


def download_current_readonly(bucket: str, prefix: str, target_dir: Path) -> None:
    """Refresh a local read-only copy of list + detail buckets."""
    target_dir.mkdir(parents=True, exist_ok=True)
    _cp_from_gcs(f"gs://{bucket}/{prefix}/companies.list.json", target_dir / "companies.list.json")
    proc = subprocess.run(
        [_gcloud(), "storage", "ls", f"gs://{bucket}/{prefix}/company-details-*.json"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "GCS detail listing failed")[-1200:])
    for uri in [line.strip() for line in proc.stdout.splitlines() if line.strip()]:
        _cp_from_gcs(uri, target_dir / uri.rsplit("/", 1)[-1])


def load_current_export(current_dir: Path) -> tuple[list[dict], dict[str, dict]]:
    items = json.loads((current_dir / "companies.list.json").read_text(encoding="utf-8"))
    details: dict[str, dict] = {}
    for path in sorted(current_dir.glob("company-details-*.json")):
        details.update(json.loads(path.read_text(encoding="utf-8")))
    return items, details


def build_live_preflight(
    list_dir: str | Path,
    *,
    country_slug: str,
    batch_id: str,
    caller: str,
    bucket: str = DEFAULT_GCS_BUCKET,
    refresh_current: bool = True,
) -> dict[str, Any]:
    base = Path(list_dir)
    country_dir = base / "countries" / country_slug
    preflight_dir = base / "preflight"
    current_dir = preflight_dir / "current-readonly"
    target_prefix = f"{country_slug}/current"
    if refresh_current:
        download_current_readonly(bucket, target_prefix, current_dir)
    existing_items, existing_details = load_current_export(current_dir)
    companies = _read_jsonl(country_dir / "companies.jsonl")
    rows = _read_jsonl(country_dir / "normalized_rows.jsonl")
    prematch = prematch_companies(
        companies, existing_items,
        normalized_rows=rows,
        existing_details=existing_details,
    )
    automatic_prematch = prematch
    overrides_path = base / "reviewed_matches.json"
    if overrides_path.is_file():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        if not isinstance(overrides, list):
            raise ValueError("reviewed_matches.json must contain a JSON list")
        prematch = apply_reviewed_match_overrides(prematch, overrides)
    enrichment = build_enrichment_plan(prematch)
    summary = prematch["summary"]
    safety_dir = preflight_dir / "safety" / batch_id
    before_dir = write_before_snapshot(
        safety_dir / "before",
        existing_items,
        existing_details,
        batch_id=batch_id,
        country_key=country_slug,
    )
    green = int(summary.get("ambiguous") or 0) == 0
    report = {
        "mode": "protected_live_preflight",
        "batch_id": batch_id,
        "country": country_slug,
        "target": target_prefix,
        "caller": caller,
        "hubspot_sync": False,
        "supplier_calls": 0,
        "external_writes": 0,
        "existing_matched": int(summary.get("matched_existing") or 0),
        "new": int(summary.get("new") or 0),
        "ambiguous": int(summary.get("ambiguous") or 0),
        "reviewed_matches": int(summary.get("reviewed_matches") or 0),
        "enrichment_full": int(enrichment["summary"].get("full_enrichment") or 0),
        "enrichment_gap_fill": int(enrichment["summary"].get("gap_fill_existing") or 0),
        "expected_gcs_creates": int(summary.get("new") or 0),
        "expected_gcs_updates": int(summary.get("matched_existing") or 0),
        "snapshot_location": str(before_dir),
        "planned_snapshot_gcs": f"gs://{bucket}/{country_slug}/imports/{batch_id}/before/",
        "rollback_readiness": "READY" if green else "BLOCKED_BY_AMBIGUOUS",
        "safety_preflight": "GREEN" if green else "BLOCKED",
        "zyte_provider": "zyte",
    }
    preflight_dir.mkdir(parents=True, exist_ok=True)
    (preflight_dir / "prematch.auto.json").write_text(
        json.dumps({"batch_id": batch_id, **automatic_prematch}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (preflight_dir / "prematch.json").write_text(
        json.dumps({"batch_id": batch_id, **prematch}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (preflight_dir / "enrichment_plan.json").write_text(
        json.dumps(enrichment, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (preflight_dir / "live_preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _require_live_gate(preflight: dict, confirm_batch_id: str) -> None:
    if preflight.get("safety_preflight") != "GREEN":
        raise RuntimeError("live write blocked: safety preflight is not GREEN")
    if not confirm_batch_id or confirm_batch_id != preflight.get("batch_id"):
        raise RuntimeError("live write blocked: explicit batch confirmation missing or mismatched")
    if preflight.get("hubspot_sync") is not False:
        raise RuntimeError("live write blocked: HubSpot must remain disabled")


def _guarded_upload(local_path: Path, destination: str, *, preflight: dict, confirm_batch_id: str) -> None:
    _require_live_gate(preflight, confirm_batch_id)
    if not local_path.is_file():
        raise RuntimeError(f"live write blocked: local artifact missing: {local_path}")
    proc = subprocess.run(
        [_gcloud(), "storage", "cp", str(local_path), destination],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "GCS write failed")[-1200:])


def _load_export_dir(export_dir: Path) -> tuple[list[dict], dict[str, dict]]:
    items = json.loads((export_dir / "companies.list.json").read_text(encoding="utf-8"))
    details: dict[str, dict] = {}
    for path in sorted(export_dir.glob("company-details-*.json")):
        details.update(json.loads(path.read_text(encoding="utf-8")))
    return items, details


def protected_publish_export(
    export_dir: str | Path,
    list_dir: str | Path,
    *,
    preflight: dict,
    caller: str,
    confirm_batch_id: str,
    bucket: str = DEFAULT_GCS_BUCKET,
    bucket_size: int = 500,
) -> dict[str, Any]:
    """Protected merge of an already enriched/exported Lead List into current/."""
    _require_live_gate(preflight, confirm_batch_id)
    batch_id = str(preflight["batch_id"])
    country_slug = str(preflight["country"])
    base = Path(list_dir)
    current_dir = base / "preflight" / "current-readonly"
    existing_items, existing_details = load_current_export(current_dir)
    new_items, new_details = _load_export_dir(Path(export_dir))
    from lead_list_safety import (
        annotate_import_provenance,
        build_change_ledger,
        prematch_export_records,
        reconcile_export_ids,
    )
    import lovable_gcs_upload as gcs

    export_match = prematch_export_records(
        new_items, existing_items,
        new_details=new_details,
        existing_details=existing_details,
    )
    if int(export_match["summary"].get("ambiguous") or 0):
        raise RuntimeError("live write blocked: enriched export introduced ambiguous matches")
    reconciled_items, reconciled_details = reconcile_export_ids(
        new_items, new_details, export_match
    )
    existing_ids = {str(x.get("company_id") or "") for x in existing_items}
    reconciled_items, reconciled_details = apply_existing_gap_fill(
        reconciled_items, reconciled_details, existing_items, existing_details
    )
    for item in reconciled_items:
        if str(item.get("company_id") or "") not in existing_ids:
            item["assigned_cold_caller"] = caller
    for cid, detail in reconciled_details.items():
        if str(cid) not in existing_ids:
            detail["assigned_cold_caller"] = caller
    reconciled_items, reconciled_details = annotate_import_provenance(
        reconciled_items, reconciled_details,
        batch_id=batch_id,
        source_list_id=base.name,
    )
    ledger = build_change_ledger(
        existing_items, reconciled_items, export_match, batch_id=batch_id
    )
    safety_dir = base / "live" / "safety" / batch_id
    before_dir = write_before_snapshot(
        safety_dir / "before", existing_items, existing_details,
        batch_id=batch_id, country_key=country_slug,
    )
    ledger_path = safety_dir / "ledger.json"
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    prematch_path = safety_dir / "prematch-export.json"
    prematch_path.write_text(json.dumps(export_match, ensure_ascii=False, indent=2), encoding="utf-8")

    safety_prefix = f"gs://{bucket}/{country_slug}/imports/{batch_id}"
    for path in [prematch_path, ledger_path, *sorted(before_dir.glob("*.json"))]:
        subdir = "before/" if path.parent == before_dir else ""
        _guarded_upload(
            path, f"{safety_prefix}/{subdir}{path.name}",
            preflight=preflight, confirm_batch_id=confirm_batch_id,
        )
    merged_items, merged_details = gcs.merge_company_records(
        existing_items, existing_details, reconciled_items, reconciled_details
    )
    merged_items, buckets = gcs.rebucket_company_details(
        merged_items, merged_details, bucket_size
    )
    merged_dir = base / "live" / "merged" / batch_id
    merged_dir.mkdir(parents=True, exist_ok=True)
    (merged_dir / "companies.list.json").write_text(
        json.dumps(merged_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for filename, payload in buckets.items():
        (merged_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    current_prefix = f"gs://{bucket}/{country_slug}/current"
    for path in [merged_dir / "companies.list.json", *sorted(merged_dir.glob("company-details-*.json"))]:
        _guarded_upload(
            path, f"{current_prefix}/{path.name}",
            preflight=preflight, confirm_batch_id=confirm_batch_id,
        )

    imported_ids = [str(item.get("company_id") or "") for item in reconciled_items if item.get("company_id")]
    try:
        membership = publish_prospect_membership(
            imported_ids, preflight=preflight, confirm_batch_id=confirm_batch_id,
        )
        membership_status = "complete"
        publish_status = "merged"
    except Exception as exc:
        membership = {"status": "error", "message": str(exc)}
        membership_status = "failed_after_merge"
        publish_status = "merged_membership_failed"

    result = {
        "status": publish_status,
        "batch_id": batch_id,
        "created": ledger["summary"]["created"],
        "updated_existing": ledger["summary"]["updated_existing"],
        "companies_total_after": len(merged_items),
        "snapshot": f"{safety_prefix}/before/",
        "ledger": f"{safety_prefix}/ledger.json",
        "hubspot_sync": False,
        "prospect_membership": membership_status,
        "prospect_membership_result": membership,
    }
    result_path = base / "live" / f"{batch_id}.publish-result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def gap_fill_mapping(existing: dict, incoming: dict) -> dict:
    """Preserve existing values; add only fields that are genuinely empty."""
    out = dict(existing)
    for key, value in incoming.items():
        if key not in out or _is_blank(out.get(key)):
            if not _is_blank(value):
                out[key] = value
        elif isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = gap_fill_mapping(out[key], value)
    return out


def apply_existing_gap_fill(
    items: list[dict], details: dict[str, dict],
    existing_items: list[dict], existing_details: dict[str, dict],
) -> tuple[list[dict], dict[str, dict]]:
    old_items = {str(x.get("company_id")): x for x in existing_items}
    safe_items: list[dict] = []
    safe_details = dict(details)
    for item in items:
        cid = str(item.get("company_id") or "")
        old = old_items.get(cid)
        if old is None:
            safe_items.append(item)
            continue
        safe_items.append(gap_fill_mapping(old, item))
        if cid in existing_details and cid in details:
            safe_details[cid] = gap_fill_mapping(existing_details[cid], details[cid])
    return safe_items, safe_details


def prepare_zyte_worksets(list_dir: str | Path, preflight: dict) -> dict[str, str]:
    """Create local full/gap-fill XLSX inputs. No supplier calls occur here."""
    if preflight.get("safety_preflight") != "GREEN":
        raise RuntimeError("Zyte worksets blocked until ambiguous matches are resolved")
    import pandas as pd
    base = Path(list_dir)
    country_slug = str(preflight["country"])
    country_dir = base / "countries" / country_slug
    companies = {c["company_key"]: c for c in _read_jsonl(country_dir / "companies.jsonl")}
    pm = json.loads((base / "preflight" / "prematch.json").read_text(encoding="utf-8"))
    work_dir = base / "live" / "zyte-worksets" / str(preflight["batch_id"])
    work_dir.mkdir(parents=True, exist_ok=True)
    groups = {"full": [], "gap_fill": []}
    for entry in pm["entries"]:
        company = companies[entry["source_company_key"]]
        row = {
            "Company": company.get("company_name"),
            "Domain": company.get("domain") or company.get("email_domain_hint") or "",
            "Input Country": "South Korea",
            "source_company_key": entry["source_company_key"],
            "existing_company_id": entry.get("existing_company_id") or "",
        }
        groups["gap_fill" if entry["action"] == "matched_existing" else "full"].append(row)
    outputs = {}
    for key, rows in groups.items():
        path = work_dir / f"{key}.xlsx"
        pd.DataFrame(rows).to_excel(path, index=False)
        outputs[key] = str(path)
    return outputs


def _hermes_env_secret(name: str) -> str:
    """Read one existing Hermes secret without exposing it in logs."""
    path = Path("/home/myngle/.config/hermes/secrets.env")
    if not path.is_file():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("\"' ")
    return ""


def _enriched_workbook_complete(output: Path, input_path: str) -> bool:
    """Reuse only a same-batch workbook with every selected row successful."""
    if not output.is_file():
        return False
    try:
        import pandas as pd
        expected = len(pd.read_excel(input_path))
        rows = pd.read_excel(output, sheet_name="Enriched Leads")
        if len(rows) != expected or "run_success" not in rows.columns:
            return False
        return bool(rows["run_success"].fillna(False).astype(bool).all())
    except Exception:
        return False


def run_zyte_enrichment(
    list_dir: str | Path, preflight: dict, *, allow_supplier_calls: bool = False,
    python_bin: str = "/home/myngle/myngle/.venv/bin/python",
) -> dict[str, str]:
    """Run existing batch CLI with Zyte only after a green protected preflight."""
    if preflight.get("safety_preflight") != "GREEN":
        raise RuntimeError("supplier calls blocked: safety preflight is not GREEN")
    if not allow_supplier_calls:
        raise RuntimeError("supplier calls blocked: explicit allow_supplier_calls is required")
    base = Path(list_dir)
    worksets = prepare_zyte_worksets(base, preflight)
    repo = Path(__file__).resolve().parent
    outputs: dict[str, str] = {}
    secrets = Path("/home/myngle/Myngle/secrets.toml")
    for mode_key, input_path in worksets.items():
        output = Path(input_path).with_suffix(".enriched.xlsx")
        if _enriched_workbook_complete(output, input_path):
            outputs[mode_key] = str(output)
            continue
        checkpoint = Path(input_path).with_suffix(".checkpoint.json")
        cmd = [
            python_bin, str(repo / "lead_prioritizer_batch_cli.py"),
            "--input", input_path,
            "--company-column", "Company", "--domain-column", "Domain",
            "--input-country-column", "Input Country", "--default-country", "South Korea",
            "--mode", "full" if mode_key == "full" else "hq_only",
            "--row-limit", "0", "--output", str(output),
            "--hq-crawl-provider", "zyte", "--yes",
            "--checkpoint-path", str(checkpoint), "--checkpoint-every-rows", "1",
        ]
        if secrets.is_file():
            cmd += ["--secrets-file", str(secrets)]
        env = os.environ.copy()
        if not env.get("ZYTE_API_KEY"):
            zyte = _hermes_env_secret("ZYTE_API_KEY")
            if zyte:
                env["ZYTE_API_KEY"] = zyte
        if not env.get("ZYTE_API_KEY"):
            raise RuntimeError("supplier calls blocked: ZYTE_API_KEY unavailable")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=21600, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"Zyte enrichment failed for {mode_key}: {(proc.stderr or proc.stdout)[-2000:]}")
        outputs[mode_key] = str(output)
    return outputs


def combine_enrichment_exports(
    enriched_outputs: dict[str, str], list_dir: str | Path, preflight: dict, caller: str
) -> str:
    """Reuse the existing exporter and combine full + gap-fill outputs locally."""
    from export_lead_prioritizer_to_lovable_json import export_workbook_to_lovable_json
    base = Path(list_dir)
    batch_id = str(preflight["batch_id"])
    combined_dir = base / "live" / "combined-export" / batch_id
    combined_dir.mkdir(parents=True, exist_ok=True)
    all_items: list[dict] = []
    all_details: dict[str, dict] = {}
    used_ids: set[str] = set()
    for group, workbook in enriched_outputs.items():
        export_dir = base / "live" / "exports" / batch_id / group
        export_workbook_to_lovable_json(
            input_xlsx=workbook, output_dir=export_dir,
            export_country="South Korea", cold_callers=[caller],
            include_skipped=True, foreign_hq_only=False, bucket_size=500,
        )
        items, details = _load_export_dir(export_dir)
        for item in items:
            original = str(item.get("company_id") or "company")
            cid = original
            suffix = 2
            while cid in used_ids:
                cid = f"{original}-ll{suffix}"
                suffix += 1
            used_ids.add(cid)
            detail = dict(details.get(original, {}))
            item = dict(item)
            item["company_id"] = cid
            item["detail_bucket"] = "company-details-000.json"
            detail["company_id"] = cid
            detail["detail_bucket"] = "company-details-000.json"
            all_items.append(item)
            all_details[cid] = detail
    (combined_dir / "companies.list.json").write_text(
        json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (combined_dir / "company-details-000.json").write_text(
        json.dumps(all_details, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(combined_dir)


def build_membership_payload(
    merged_company_ids: list[str], *, preflight: dict, list_name: str = "Carla Korea"
) -> dict[str, Any]:
    """Idempotent membership payload for the Company Hub write route."""
    ids = list(dict.fromkeys(str(x) for x in merged_company_ids if x))
    return {
        "import_batch": preflight["batch_id"],
        "country_key": preflight["country"],
        "list_key": "carla-korea",
        "list_label": list_name,
        "members": [
            {"company_id": cid, "assigned_caller": preflight["caller"]}
            for cid in ids
        ],
        "source_system": "control-center-lead-list",
        "added_by": "sales-cockpit-lead-list-worker",
    }


def publish_prospect_membership(
    merged_company_ids: list[str], *, preflight: dict, confirm_batch_id: str,
    list_name: str = "Carla Korea", url: str | None = None, token: str | None = None,
) -> dict[str, Any]:
    """Write Prospect List membership only after the same protected live gate."""
    _require_live_gate(preflight, confirm_batch_id)
    endpoint = (url or os.environ.get("PROSPECT_LIST_MEMBERSHIP_URL") or
                "https://myngle.whofirst.nl/api/public/prospect-list-membership").strip()
    secret = (token or os.environ.get("FEEDBACK_AUTOPILOT_TOKEN") or "").strip()
    if not secret:
        token_file = Path("/home/myngle/orchestrator/secrets/feedback_autopilot_token")
        if token_file.is_file():
            secret = token_file.read_text(encoding="utf-8").strip()
    if not secret:
        raise RuntimeError("Prospect List write blocked: feedback autopilot token unavailable")
    payload = build_membership_payload(merged_company_ids, preflight=preflight, list_name=list_name)
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-feedback-autopilot-token": secret,
        "User-Agent": "Sales-Cockpit-Lead-List-Worker/1.0",
    })
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Prospect List membership write failed: {exc}") from exc
    if result.get("status") != "ok":
        raise RuntimeError(f"Prospect List membership rejected: {result}")
    return result


def prepare_batch_rollback(list_dir: str | Path, preflight: dict) -> dict[str, Any]:
    """Prepare, never apply, selective rollback from current + snapshot + ledger."""
    from lead_list_rollback import prepare_rollback
    base = Path(list_dir)
    batch_id = str(preflight["batch_id"])
    safety_dir = base / "live" / "safety" / batch_id
    output_dir = base / "live" / "rollback" / batch_id
    current_dir = base / "preflight" / "current-readonly"
    return prepare_rollback(current_dir, safety_dir, output_dir)
