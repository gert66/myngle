"""Read-only reconciliation of former mYngle clients against live Company Hub GCS data.

Reads Marina's New Business handover workbook, scans every country folder's
``current/companies.list.json`` in the Lovable GCS bucket, and classifies each
former client as:

- already_in_cockpit
- new_company_existing_country
- new_company_new_country
- manual_review

This script never uploads, promotes, deletes, or changes any GCS object.
It writes only a local Excel report.

Example:
    python reconcile_former_clients_against_gcs.py \
      --input-xlsx "crossreference_removed_FINAL_TO PASS .to New Biz.xlsx" \
      --output-xlsx former_client_reactivation_live_reconciliation.xlsx
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

import gcs_python_backend
from lovable_gcs_upload import DEFAULT_GCS_BUCKET
from rescore_from_gcs import list_country_folders, resolve_gcs_tool, download_file

HANDOVER_SHEET = "Handover List"
LIST_FILENAME = "companies.list.json"


def _text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def normalize_name(value) -> str:
    text = unicodedata.normalize("NFKD", _text(value)).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    legal = {
        "ag", "aps", "as", "bv", "bvba", "co", "company", "corp", "corporation",
        "gmbh", "inc", "kg", "limited", "ltd", "mbh", "nv", "oy", "sa", "sarL".lower(),
        "spa", "srl", "srL".lower(), "plc", "llc", "pte", "pty", "sl", "sas",
    }
    parts = [p for p in text.split() if p not in legal]
    return " ".join(parts)


def normalize_domain(value) -> str:
    text = _text(value).lower()
    if not text:
        return ""
    try:
        host = urlparse(text if "://" in text else f"https://{text}").netloc
    except ValueError:
        host = text
    host = host.lower().split(":")[0]
    return host.removeprefix("www.").strip("/")


def normalize_vat(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", _text(value).upper())


def normalize_country(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).lower())


def _find_header_row(path: Path, sheet_name: str, required: set[str], scan_rows: int = 10) -> int:
    """Scans the first ``scan_rows`` rows of ``sheet_name`` with no header assumed
    and returns the index of the first row whose cell values are a superset of
    ``required`` — the real header row.

    Some exports of this workbook carry a title/banner sentence (and a blank
    row) above the actual header, which a bare ``header=0`` read would
    otherwise silently misparse: the banner text becomes one mangled column
    name, and the real header row is read as ordinary data.
    """
    preview = pd.read_excel(path, sheet_name=sheet_name, header=None, nrows=scan_rows)
    for i in range(len(preview)):
        row_values = {_text(v) for v in preview.iloc[i].tolist()}
        if required.issubset(row_values):
            return i
    raise ValueError(
        f"Could not find a header row containing all of {sorted(required)} "
        f"in the first {scan_rows} rows of sheet '{sheet_name}'."
    )


def load_handover(path: Path) -> pd.DataFrame:
    required = {"Company", "Country", "ASSIGNED CLOSER"}
    header_row = _find_header_row(path, HANDOVER_SHEET, required)
    df = pd.read_excel(path, sheet_name=HANDOVER_SHEET, header=header_row)
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Missing required Handover List columns: {', '.join(missing)}")
    df = df[df["Company"].notna()].copy()
    df = df[_text_series(df["Company"]) != ""]
    return df.reset_index(drop=True)


def _text_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def _download_current_list(bucket: str, country_folder: str, target: Path) -> list[dict]:
    blob_name = f"{country_folder}/current/{LIST_FILENAME}"
    tool = resolve_gcs_tool()
    if tool is None:
        result = gcs_python_backend.download_file(bucket, blob_name, str(target))
    else:
        result = download_file(tool, f"gs://{bucket}/{blob_name}", str(target))
    if not result.get("success"):
        raise RuntimeError(
            f"Could not read gs://{bucket}/{blob_name}: "
            f"{result.get('error') or result.get('stderr') or 'unknown error'}"
        )
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{blob_name} is not a JSON array")
    return raw


def build_live_index(bucket: str) -> tuple[list[str], list[dict]]:
    folders = sorted(list_country_folders(bucket))
    if not folders:
        diag = gcs_python_backend.diagnostics() if resolve_gcs_tool() is None else {}
        raise RuntimeError(
            "No GCS country folders could be listed. Check Google Cloud credentials. "
            f"Backend diagnostics: {diag}"
        )

    records: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="former-client-reconcile-") as tmp:
        tmpdir = Path(tmp)
        for folder in folders:
            try:
                items = _download_current_list(bucket, folder, tmpdir / f"{folder}.json")
            except RuntimeError as exc:
                # Top-level bucket folders can contain non-country material. Keep a visible
                # audit row rather than failing the entire reconciliation on those.
                records.append({"_folder": folder, "_read_error": str(exc)})
                continue
            for item in items:
                if isinstance(item, dict):
                    rec = dict(item)
                    rec["_folder"] = folder
                    records.append(rec)
    return folders, records


def _candidate_match(row: pd.Series, live: list[dict]) -> tuple[str, list[dict], str]:
    source_name = normalize_name(row.get("Company"))
    source_vat = normalize_vat(row.get("VAT Number"))
    source_domain = normalize_domain(row.get("Domain")) if "Domain" in row.index else ""

    vat_matches = []
    domain_matches = []
    exact_name_matches = []

    for item in live:
        if item.get("_read_error"):
            continue
        item_vat = normalize_vat(
            item.get("vat_number") or item.get("vat") or item.get("company_vat")
        )
        item_domain = normalize_domain(item.get("domain") or item.get("website"))
        item_name = normalize_name(item.get("company_name") or item.get("name"))
        if source_vat and item_vat and source_vat == item_vat:
            vat_matches.append(item)
        if source_domain and item_domain and source_domain == item_domain:
            domain_matches.append(item)
        if source_name and item_name and source_name == item_name:
            exact_name_matches.append(item)

    if len(vat_matches) == 1:
        return "already_in_cockpit", vat_matches, "exact VAT"
    if len(domain_matches) == 1:
        return "already_in_cockpit", domain_matches, "exact domain"
    if len(exact_name_matches) == 1:
        return "already_in_cockpit", exact_name_matches, "normalized exact company name"

    combined = {str(m.get("company_id") or id(m)): m for m in vat_matches + domain_matches + exact_name_matches}
    if combined:
        return "manual_review", list(combined.values()), "multiple/ambiguous exact candidates"
    return "", [], "no exact live match"


def reconcile(source: pd.DataFrame, folders: list[str], live: list[dict]) -> pd.DataFrame:
    folder_norms = {normalize_country(f): f for f in folders}
    rows = []
    for _, row in source.iterrows():
        status, matches, reason = _candidate_match(row, live)
        country = _text(row.get("Country"))
        country_folder = folder_norms.get(normalize_country(country), "")
        if not status:
            status = "new_company_existing_country" if country_folder else "new_company_new_country"

        match = matches[0] if len(matches) == 1 else {}
        out = row.to_dict()
        out.update({
            "reconciliation_status": status,
            "match_reason": reason,
            "live_match_count": len(matches),
            "live_country_folder": match.get("_folder") or country_folder,
            "live_company_id": match.get("company_id", ""),
            "live_company_name": match.get("company_name") or match.get("name") or "",
            "live_domain": match.get("domain") or match.get("website") or "",
            "proposed_cohort": "former_client_reactivation",
            "preserve_assigned_closer": _text(row.get("ASSIGNED CLOSER")),
        })
        rows.append(out)
    return pd.DataFrame(rows)


def write_report(result: pd.DataFrame, folders: list[str], live: list[dict], output: Path) -> None:
    summary = (
        result["reconciliation_status"].value_counts(dropna=False)
        .rename_axis("status").reset_index(name="count")
    )
    by_country = (
        result.groupby(["Country", "reconciliation_status"], dropna=False)
        .size().reset_index(name="count")
        .sort_values(["Country", "reconciliation_status"])
    )
    read_errors = pd.DataFrame([
        {"folder": r.get("_folder"), "error": r.get("_read_error")}
        for r in live if r.get("_read_error")
    ])
    metadata = pd.DataFrame([
        {"metric": "source_accounts", "value": len(result)},
        {"metric": "gcs_top_level_folders_seen", "value": len(folders)},
        {"metric": "live_company_records_read", "value": sum(1 for r in live if not r.get("_read_error"))},
        {"metric": "gcs_write_operations", "value": 0},
    ])

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="Reconciliation", index=False)
        summary.to_excel(writer, sheet_name="Summary", index=False)
        by_country.to_excel(writer, sheet_name="By Country", index=False)
        metadata.to_excel(writer, sheet_name="Run Metadata", index=False)
        if not read_errors.empty:
            read_errors.to_excel(writer, sheet_name="GCS Read Errors", index=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-xlsx", required=True)
    p.add_argument("--output-xlsx", default="former_client_reactivation_live_reconciliation.xlsx")
    p.add_argument("--bucket", default=DEFAULT_GCS_BUCKET)
    return p


def main() -> int:
    args = build_parser().parse_args()
    source = load_handover(Path(args.input_xlsx))
    folders, live = build_live_index(args.bucket)
    result = reconcile(source, folders, live)
    output = Path(args.output_xlsx)
    write_report(result, folders, live, output)

    print(f"Source accounts: {len(result)}")
    print(result["reconciliation_status"].value_counts().to_string())
    print(f"Report: {output.resolve()}")
    print("GCS write operations: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
