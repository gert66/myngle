"""Safety layer for publishing imported lead lists into country datasets.

Pure helpers only: conservative pre-match, batch provenance, immutable before
snapshots, change ledgers, and selective rollback. No network or GCS writes.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

_LEGAL_SUFFIXES = {
    "ag", "bv", "co", "company", "corp", "corporation", "gmbh", "inc",
    "incorporated", "limited", "llc", "ltd", "nv", "oy", "plc", "pty",
    "sa", "sas", "spa", "srl",
}


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split(":", 1)[0].strip(".")
    return text[4:] if text.startswith("www.") else text


def normalize_company_name(value: Any) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value or "").lower())
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _phones_from_mapping(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if any(tag in str(key).lower() for tag in ("phone", "mobile", "telephone", "tel")):
                if isinstance(child, (str, int, float)):
                    phone = normalize_phone(child)
                    if len(phone) >= 7:
                        found.add(phone)
            found |= _phones_from_mapping(child)
    elif isinstance(value, list):
        for child in value:
            found |= _phones_from_mapping(child)
    return found


def _incoming_phone_map(normalized_rows: Iterable[Mapping[str, Any]] | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in normalized_rows or []:
        key = str(row.get("company_key") or "")
        if not key:
            continue
        phones = result.setdefault(key, set())
        for field in ("direct_phone", "mobile", "mobile_2"):
            phone = normalize_phone(row.get(field))
            if len(phone) >= 7:
                phones.add(phone)
    return result


def _existing_indexes(existing_items: list[dict], existing_details: Mapping[str, dict] | None = None) -> dict[str, Any]:
    by_domain: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    phones: dict[str, set[str]] = {}
    details = existing_details or {}
    for item in existing_items:
        cid = str(item.get("company_id") or "")
        domain = normalize_domain(item.get("domain") or item.get("website_url"))
        name = normalize_company_name(item.get("company_name"))
        if domain:
            by_domain.setdefault(domain, []).append(item)
        if name:
            by_name.setdefault(name, []).append(item)
        phones[cid] = _phones_from_mapping(details.get(cid, {}))
    return {"by_domain": by_domain, "by_name": by_name, "phones": phones}


def prematch_companies(
    companies: Iterable[Mapping[str, Any]],
    existing_items: list[dict],
    *,
    normalized_rows: Iterable[Mapping[str, Any]] | None = None,
    existing_details: Mapping[str, dict] | None = None,
) -> dict[str, Any]:
    """Conservative pre-match. Name-only matches are review, never automatic."""
    idx = _existing_indexes(existing_items, existing_details)
    incoming_phones = _incoming_phone_map(normalized_rows)
    entries: list[dict[str, Any]] = []
    counts = {"matched_existing": 0, "new": 0, "ambiguous": 0}

    for company in companies:
        source_key = str(company.get("company_key") or company.get("company_id") or "")
        name = str(company.get("company_name") or "")
        domain = normalize_domain(company.get("domain"))
        hint = normalize_domain(company.get("email_domain_hint"))
        name_key = normalize_company_name(name)
        decision = "new"
        match_basis = "none"
        confidence = "none"
        matched_id = None
        candidates: list[str] = []

        domain_candidates: list[dict] = []
        for candidate_domain in (domain, hint):
            if candidate_domain:
                domain_candidates = idx["by_domain"].get(candidate_domain, [])
                if domain_candidates:
                    match_basis = "exact_domain" if candidate_domain == domain else "email_domain_hint"
                    break
        if len(domain_candidates) == 1:
            decision, confidence = "matched_existing", "high"
            matched_id = str(domain_candidates[0].get("company_id") or "")
        elif len(domain_candidates) > 1:
            decision, confidence = "ambiguous", "review"
            candidates = [str(x.get("company_id") or "") for x in domain_candidates]
        else:
            name_candidates = idx["by_name"].get(name_key, []) if name_key else []
            source_phones = incoming_phones.get(source_key, set())
            phone_matches = [
                item for item in name_candidates
                if source_phones and source_phones & idx["phones"].get(str(item.get("company_id") or ""), set())
            ]
            if len(phone_matches) == 1:
                decision, match_basis, confidence = "matched_existing", "exact_name_and_phone", "high"
                matched_id = str(phone_matches[0].get("company_id") or "")
            elif len(phone_matches) > 1:
                decision, match_basis, confidence = "ambiguous", "exact_name_and_phone", "review"
                candidates = [str(x.get("company_id") or "") for x in phone_matches]
            elif name_candidates:
                decision, match_basis, confidence = "ambiguous", "exact_name_only", "review"
                candidates = [str(x.get("company_id") or "") for x in name_candidates]

        counts[decision] += 1
        entries.append({
            "source_company_key": source_key,
            "company_name": name,
            "incoming_domain": domain or hint or None,
            "action": decision,
            "existing_company_id": matched_id,
            "match_basis": match_basis,
            "confidence": confidence,
            "candidate_company_ids": candidates,
        })

    return {"summary": {**counts, "total": len(entries)}, "entries": entries}


def prematch_export_records(
    new_items: list[dict], existing_items: list[dict], *,
    new_details: Mapping[str, dict] | None = None,
    existing_details: Mapping[str, dict] | None = None,
) -> dict[str, Any]:
    companies = []
    rows = []
    for item in new_items:
        cid = str(item.get("company_id") or "")
        companies.append({
            "company_key": cid,
            "company_name": item.get("company_name"),
            "domain": item.get("domain") or item.get("website_url"),
            "email_domain_hint": None,
        })
        detail = (new_details or {}).get(cid, {})
        for phone in _phones_from_mapping(detail):
            rows.append({"company_key": cid, "direct_phone": phone})
    return prematch_companies(
        companies, existing_items, normalized_rows=rows, existing_details=existing_details,
    )


def reconcile_export_ids(
    new_items: list[dict], new_details: Mapping[str, dict], prematch: Mapping[str, Any],
) -> tuple[list[dict], dict[str, dict]]:
    """Reuse an existing company_id only for high-confidence matches."""
    match_by_source = {
        str(e.get("source_company_key")): str(e.get("existing_company_id"))
        for e in prematch.get("entries", [])
        if e.get("action") == "matched_existing" and e.get("existing_company_id")
    }
    claimed: set[str] = set()
    out_items: list[dict] = []
    out_details: dict[str, dict] = {}
    for item in new_items:
        source_id = str(item.get("company_id") or "")
        final_id = match_by_source.get(source_id, source_id)
        if final_id in claimed:
            raise ValueError(f"Safety match collision: multiple incoming records map to {final_id}")
        claimed.add(final_id)
        copy_item = dict(item)
        copy_item["company_id"] = final_id
        out_items.append(copy_item)
        detail = dict(new_details.get(source_id, {}))
        if detail:
            detail["company_id"] = final_id
            out_details[final_id] = detail
    return out_items, out_details


def annotate_import_provenance(
    items: list[dict], details: Mapping[str, dict], *, batch_id: str, source_list_id: str | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    def annotate(record: dict) -> dict:
        out = dict(record)
        batches = [str(x) for x in (out.get("import_batches") or []) if x]
        if batch_id not in batches:
            batches.append(batch_id)
        out["import_batches"] = batches
        out["last_import_batch"] = batch_id
        if source_list_id:
            out["last_import_list_id"] = source_list_id
        return out
    return [annotate(x) for x in items], {cid: annotate(d) for cid, d in details.items()}


def build_change_ledger(
    existing_items: list[dict], reconciled_new_items: list[dict], prematch: Mapping[str, Any], *, batch_id: str,
) -> dict[str, Any]:
    existing_ids = {str(x.get("company_id") or "") for x in existing_items}
    matched_ids = {
        str(e.get("existing_company_id")): e for e in prematch.get("entries", [])
        if e.get("action") == "matched_existing" and e.get("existing_company_id")
    }
    entries = []
    counts = {"created": 0, "updated_existing": 0}
    for item in reconciled_new_items:
        cid = str(item.get("company_id") or "")
        action = "updated_existing" if cid in existing_ids else "created"
        counts[action] += 1
        match = matched_ids.get(cid, {})
        entries.append({
            "batch_id": batch_id,
            "company_id": cid,
            "company_name": item.get("company_name"),
            "action": action,
            "match_basis": match.get("match_basis") or ("new_company" if action == "created" else "existing_id"),
            "confidence": match.get("confidence") or ("high" if action == "created" else "high"),
        })
    return {"batch_id": batch_id, "summary": {**counts, "total": len(entries)}, "entries": entries}


def write_before_snapshot(
    target_dir: str | Path, existing_items: list[dict], existing_details: Mapping[str, dict], *,
    batch_id: str, country_key: str,
) -> Path:
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    list_text = json.dumps(existing_items, ensure_ascii=False, indent=2, default=str)
    details_text = json.dumps(dict(existing_details), ensure_ascii=False, indent=2, default=str)
    digest = hashlib.sha256((list_text + "\n" + details_text).encode("utf-8")).hexdigest()
    (target / "companies.list.json").write_text(list_text + "\n", encoding="utf-8")
    (target / "details.by-id.json").write_text(details_text + "\n", encoding="utf-8")
    (target / "snapshot_manifest.json").write_text(json.dumps({
        "batch_id": batch_id,
        "country_key": country_key,
        "company_count": len(existing_items),
        "sha256": digest,
    }, indent=2) + "\n", encoding="utf-8")
    return target


def selective_rollback(
    current_items: list[dict], current_details: Mapping[str, dict],
    before_items: list[dict], before_details: Mapping[str, dict], ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Undo this batch only. Later touched records are reported as conflicts."""
    batch_id = str(ledger.get("batch_id") or "")
    current_by_id = {str(x.get("company_id") or ""): dict(x) for x in current_items}
    before_by_id = {str(x.get("company_id") or ""): dict(x) for x in before_items}
    details = {str(k): dict(v) for k, v in current_details.items()}
    conflicts: list[str] = []
    removed = restored = 0

    for entry in ledger.get("entries", []):
        cid = str(entry.get("company_id") or "")
        action = str(entry.get("action") or "")
        current = current_by_id.get(cid)
        if not cid or current is None:
            continue
        if current.get("last_import_batch") not in (None, "", batch_id):
            conflicts.append(cid)
            continue
        if action == "created":
            current_by_id.pop(cid, None)
            details.pop(cid, None)
            removed += 1
        elif action == "updated_existing" and cid in before_by_id:
            current_by_id[cid] = deepcopy(before_by_id[cid])
            if cid in before_details:
                details[cid] = deepcopy(before_details[cid])
            else:
                details.pop(cid, None)
            restored += 1

    original_order = [str(x.get("company_id") or "") for x in current_items]
    out_items = [current_by_id[cid] for cid in original_order if cid in current_by_id]
    missing_restored = [cid for cid in before_by_id if cid in current_by_id and cid not in original_order]
    out_items.extend(current_by_id[cid] for cid in missing_restored)
    return {
        "items": out_items,
        "details": details,
        "summary": {"removed_created": removed, "restored_existing": restored, "conflicts": len(conflicts)},
        "conflict_company_ids": conflicts,
        "safe_to_apply": not conflicts,
    }


def build_enrichment_plan(prematch: Mapping[str, Any]) -> dict[str, Any]:
    """Route companies before Zyte: new=full, matched=gap-fill, ambiguous=hold."""
    full: list[str] = []
    gap_fill: list[str] = []
    hold: list[str] = []
    for entry in prematch.get("entries", []):
        key = str(entry.get("source_company_key") or "")
        action = entry.get("action")
        if action == "new":
            full.append(key)
        elif action == "matched_existing":
            gap_fill.append(key)
        else:
            hold.append(key)
    return {
        "full_enrichment": full,
        "gap_fill_existing": gap_fill,
        "held_for_review": hold,
        "summary": {
            "full_enrichment": len(full),
            "gap_fill_existing": len(gap_fill),
            "held_for_review": len(hold),
        },
        "safe_to_enrich": not hold,
    }
