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


def domains_related(left: Any, right: Any) -> bool:
    """True for exact domains or a strict parent/subdomain relationship."""
    a = normalize_domain(left)
    b = normalize_domain(right)
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _email_domains_from_mapping(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for child in value.values():
            found |= _email_domains_from_mapping(child)
    elif isinstance(value, list):
        for child in value:
            found |= _email_domains_from_mapping(child)
    elif isinstance(value, str):
        for match in re.findall(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", value):
            domain = normalize_domain(match)
            if domain:
                found.add(domain)
    return found


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


def _incoming_email_domain_map(normalized_rows: Iterable[Mapping[str, Any]] | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in normalized_rows or []:
        key = str(row.get("company_key") or "")
        if not key:
            continue
        domains = result.setdefault(key, set())
        hint = normalize_domain(row.get("email_domain_hint"))
        if hint:
            domains.add(hint)
        domains |= _email_domains_from_mapping(row.get("work_email"))
    return result


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


def _explicit_existing_domains(item: Mapping[str, Any], detail: Mapping[str, Any]) -> set[str]:
    values = [item.get("domain"), item.get("website_url")]
    debug_row = ((detail.get("debug") or {}).get("lead_prioritizer_row") or {}) if isinstance(detail, Mapping) else {}
    values.extend([debug_row.get("Company Domain"), debug_row.get("Company Website")])
    return {d for d in (normalize_domain(v) for v in values) if d}


def _existing_indexes(existing_items: list[dict], existing_details: Mapping[str, dict] | None = None) -> dict[str, Any]:
    by_domain: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    phones: dict[str, set[str]] = {}
    email_domains: dict[str, set[str]] = {}
    explicit_domains: dict[str, set[str]] = {}
    details = existing_details or {}
    for item in existing_items:
        cid = str(item.get("company_id") or "")
        domain = normalize_domain(item.get("domain") or item.get("website_url"))
        name = normalize_company_name(item.get("company_name"))
        if domain:
            by_domain.setdefault(domain, []).append(item)
        if name:
            by_name.setdefault(name, []).append(item)
        detail = details.get(cid, {})
        phones[cid] = _phones_from_mapping(detail)
        email_domains[cid] = _email_domains_from_mapping(detail)
        explicit_domains[cid] = _explicit_existing_domains(item, detail)
    return {
        "by_domain": by_domain, "by_name": by_name, "phones": phones,
        "email_domains": email_domains, "explicit_domains": explicit_domains,
    }


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
    incoming_email_domains = _incoming_email_domain_map(normalized_rows)
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

        name_candidates = idx["by_name"].get(name_key, []) if name_key else []
        name_ids = {str(x.get("company_id") or "") for x in name_candidates}

        domain_candidates = idx["by_domain"].get(domain, []) if domain else []
        hint_candidates = idx["by_domain"].get(hint, []) if hint else []

        if domain_candidates:
            domain_ids = [str(x.get("company_id") or "") for x in domain_candidates]
            if len(domain_candidates) == 1:
                target_id = domain_ids[0]
                if name_ids and target_id not in name_ids:
                    decision, match_basis, confidence = "ambiguous", "name_domain_conflict", "review"
                    candidates = sorted(name_ids | {target_id})
                else:
                    decision, match_basis, confidence = "matched_existing", "exact_domain", "high"
                    matched_id = target_id
            else:
                decision, match_basis, confidence = "ambiguous", "exact_domain", "review"
                candidates = domain_ids
        elif hint_candidates:
            hint_ids = {str(x.get("company_id") or "") for x in hint_candidates}
            overlap = sorted(name_ids & hint_ids)
            if len(overlap) == 1:
                decision, match_basis, confidence = (
                    "matched_existing", "exact_name_and_email_domain_hint", "high"
                )
                matched_id = overlap[0]
            elif name_ids:
                decision, match_basis, confidence = "ambiguous", "name_hint_conflict", "review"
                candidates = sorted(name_ids | hint_ids)
            else:
                # Email-domain hints can be group/shared domains. They are useful
                # review evidence but are never enough by themselves to reuse an
                # existing company identity.
                decision, match_basis, confidence = "ambiguous", "email_domain_hint_only", "review"
                candidates = sorted(hint_ids)
        else:
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
            else:
                source_domains = {d for d in (domain, hint) if d}
                source_domains |= incoming_email_domains.get(source_key, set())
                hierarchy_matches = [
                    item for item in name_candidates
                    if any(
                        domains_related(source_domain, existing_domain)
                        for source_domain in source_domains
                        for existing_domain in idx["explicit_domains"].get(
                            str(item.get("company_id") or ""), set()
                        )
                    )
                ]
                if len(hierarchy_matches) == 1:
                    decision, match_basis, confidence = (
                        "matched_existing", "exact_name_and_strong_domain", "high"
                    )
                    matched_id = str(hierarchy_matches[0].get("company_id") or "")
                elif len(hierarchy_matches) > 1:
                    decision, match_basis, confidence = (
                        "ambiguous", "exact_name_and_strong_domain", "review"
                    )
                    candidates = [str(x.get("company_id") or "") for x in hierarchy_matches]
                else:
                    strong_email_domain = domain or hint
                    email_matches = [
                        item for item in name_candidates
                        if strong_email_domain and strong_email_domain in idx["email_domains"].get(
                            str(item.get("company_id") or ""), set()
                        )
                    ]
                    if len(email_matches) == 1:
                        decision, match_basis, confidence = (
                            "matched_existing", "exact_name_and_contact_email_domain", "high"
                        )
                        matched_id = str(email_matches[0].get("company_id") or "")
                    elif len(email_matches) > 1:
                        decision, match_basis, confidence = (
                            "ambiguous", "exact_name_and_contact_email_domain", "review"
                        )
                        candidates = [str(x.get("company_id") or "") for x in email_matches]
                    elif name_candidates:
                        decision, match_basis, confidence = "ambiguous", "exact_name_only", "review"
                        candidates = [str(x.get("company_id") or "") for x in name_candidates]

        counts[decision] += 1
        entries.append({
            "source_company_key": source_key,
            "company_name": name,
            "incoming_domain": domain or hint or None,
            "incoming_company_domain": domain or None,
            "email_domain_hint": hint or None,
            "action": decision,
            "existing_company_id": matched_id,
            "match_basis": match_basis,
            "confidence": confidence,
            "candidate_company_ids": candidates,
        })

    # A protected import is one source company -> at most one existing target.
    # If multiple distinct source companies claim the same target, downgrade all
    # of them to review instead of silently collapsing entities.
    by_target: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry.get("action") == "matched_existing" and entry.get("existing_company_id"):
            by_target.setdefault(str(entry["existing_company_id"]), []).append(entry)
    collisions = {target: group for target, group in by_target.items() if len(group) > 1}
    if collisions:
        for target, group in collisions.items():
            for entry in group:
                prior_basis = str(entry.get("match_basis") or "")
                entry["action"] = "ambiguous"
                entry["candidate_company_ids"] = sorted(
                    set(entry.get("candidate_company_ids") or []) | {target}
                )
                entry["existing_company_id"] = None
                entry["match_basis"] = f"target_collision:{prior_basis}"
                entry["confidence"] = "review"
        counts = {"matched_existing": 0, "new": 0, "ambiguous": 0}
        for entry in entries:
            counts[str(entry.get("action"))] += 1

    return {
        "summary": {
            **counts,
            "total": len(entries),
            "target_collision_groups": len(collisions),
        },
        "entries": entries,
    }


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


def apply_reviewed_match_overrides(
    prematch: Mapping[str, Any], overrides: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Resolve reviewed ambiguities without weakening automatic rules.

    Backward compatible existing-match override:
      source_company_key + existing_company_id + evidence

    Explicit create-new override:
      source_company_key + action="new" + evidence
    """
    result = deepcopy(dict(prematch))
    entries = [dict(e) for e in result.get("entries", [])]
    by_key = {str(e.get("source_company_key") or ""): e for e in entries}
    reviewed = 0
    reviewed_new = 0
    for raw in overrides or []:
        key = str(raw.get("source_company_key") or "").strip()
        target = str(raw.get("existing_company_id") or "").strip()
        requested_action = str(raw.get("action") or "").strip()
        if not requested_action:
            requested_action = "matched_existing" if target else ""
        evidence = [str(x).strip() for x in (raw.get("evidence") or []) if str(x).strip()]
        if not key or not requested_action or not evidence:
            raise ValueError(
                "reviewed override requires source_company_key, action/target and evidence"
            )
        entry = by_key.get(key)
        if not entry or entry.get("action") != "ambiguous":
            raise ValueError(f"reviewed override is not a current ambiguous entry: {key}")

        if requested_action == "matched_existing":
            if not target:
                raise ValueError(f"reviewed existing match requires existing_company_id: {key}")
            candidates = {str(x) for x in entry.get("candidate_company_ids") or []}
            if target not in candidates:
                raise ValueError(f"reviewed match target is not a prematch candidate: {key} -> {target}")
            entry["action"] = "matched_existing"
            entry["existing_company_id"] = target
            entry["match_basis"] = "reviewed_identity_match"
        elif requested_action == "new":
            entry["action"] = "new"
            entry["existing_company_id"] = None
            entry["match_basis"] = "reviewed_create_new"
            reviewed_new += 1
        else:
            raise ValueError(f"unsupported reviewed override action: {requested_action}")

        entry["confidence"] = "high"
        entry["review_evidence"] = evidence
        entry["reviewer"] = str(raw.get("reviewer") or "protected-live-review")
        entry["review_note"] = str(raw.get("note") or "").strip() or None
        reviewed += 1

    # A review must not create a many-to-one collision either.
    by_target: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry.get("action") == "matched_existing" and entry.get("existing_company_id"):
            by_target.setdefault(str(entry["existing_company_id"]), []).append(entry)
    collisions = {target: group for target, group in by_target.items() if len(group) > 1}
    if collisions:
        targets = ", ".join(sorted(collisions)[:5])
        raise ValueError(f"reviewed overrides create duplicate existing targets: {targets}")

    counts = {"matched_existing": 0, "new": 0, "ambiguous": 0}
    for entry in entries:
        action = str(entry.get("action") or "")
        if action in counts:
            counts[action] += 1
    result["entries"] = entries
    result["summary"] = {
        **counts,
        "total": len(entries),
        "reviewed_matches": reviewed,
        "reviewed_create_new": reviewed_new,
        "target_collision_groups": 0,
    }
    return result


def _planned_new_company_id(
    source_key: str, company_name: Any, occupied: set[str],
) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_company_name(company_name)).strip("-")
    slug = slug[:48] or "company"
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:10]
    base = f"{slug}-{digest}"
    candidate = base
    suffix = 2
    while candidate in occupied:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def build_import_plan(
    prematch: Mapping[str, Any], *, batch_id: str, country: str, caller: str,
    existing_company_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Freeze a GREEN pre-match into an immutable execution contract.

    Both existing and new records receive a final company_id here. Enrichment
    may add data, but it may never re-decide identity.
    """
    ambiguous = int((prematch.get("summary") or {}).get("ambiguous") or 0)
    if ambiguous:
        raise ValueError("cannot freeze import plan while ambiguous matches remain")

    occupied = {str(x) for x in (existing_company_ids or []) if str(x)}
    entries: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()

    for raw in prematch.get("entries", []):
        source_key = str(raw.get("source_company_key") or "").strip()
        action = str(raw.get("action") or "")
        if not source_key or source_key in seen_sources:
            raise ValueError(f"invalid or duplicate source_company_key in import plan: {source_key!r}")
        if action not in {"new", "matched_existing"}:
            raise ValueError(f"unsupported import-plan action for {source_key}: {action}")

        if action == "matched_existing":
            target = str(raw.get("existing_company_id") or "").strip()
            if not target:
                raise ValueError(f"matched_existing import-plan entry lacks target company_id: {source_key}")
            if target in seen_targets:
                raise ValueError(f"multiple import-plan entries target existing company_id: {target}")
        else:
            target = _planned_new_company_id(source_key, raw.get("company_name"), occupied | seen_targets)

        seen_sources.add(source_key)
        seen_targets.add(target)
        entries.append({
            "source_company_key": source_key,
            "company_name": raw.get("company_name"),
            "action": action,
            "target_company_id": target,
            "match_basis": raw.get("match_basis"),
            "confidence": raw.get("confidence"),
            "review_evidence": raw.get("review_evidence") or [],
            "reviewer": raw.get("reviewer"),
            "review_note": raw.get("review_note"),
        })

    payload = {
        "schema_version": 1,
        "batch_id": str(batch_id),
        "country": str(country),
        "caller": str(caller),
        "locked": True,
        "entries": entries,
        "summary": {
            "matched_existing": sum(e["action"] == "matched_existing" for e in entries),
            "new": sum(e["action"] == "new" for e in entries),
            "total": len(entries),
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def validate_import_plan(
    plan: Mapping[str, Any], prematch: Mapping[str, Any], *,
    batch_id: str, country: str, caller: str,
    existing_company_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Compare a refreshed pre-match with the frozen plan without rewriting it."""
    drift: list[dict[str, Any]] = []
    for field, expected in (("batch_id", batch_id), ("country", country), ("caller", caller)):
        actual = str(plan.get(field) or "")
        if actual != str(expected):
            drift.append({"type": "metadata_changed", "field": field, "planned": actual, "current": str(expected)})

    planned = {str(e.get("source_company_key") or ""): e for e in plan.get("entries", [])}
    current = {str(e.get("source_company_key") or ""): e for e in prematch.get("entries", [])}
    existing_now = {str(x) for x in (existing_company_ids or []) if str(x)}
    for entry in planned.values():
        if entry.get("action") == "new":
            target = str(entry.get("target_company_id") or "")
            if not target:
                drift.append({
                    "type": "planned_new_target_missing",
                    "source_company_key": entry.get("source_company_key"),
                })
            elif target in existing_now:
                drift.append({
                    "type": "planned_new_target_now_exists",
                    "source_company_key": entry.get("source_company_key"),
                    "target_company_id": target,
                })
    for key in sorted(set(planned) | set(current)):
        p = planned.get(key)
        c = current.get(key)
        if p is None:
            drift.append({"type": "unexpected_source", "source_company_key": key})
            continue
        if c is None:
            drift.append({"type": "missing_source", "source_company_key": key})
            continue
        planned_action = str(p.get("action") or "")
        current_action = str(c.get("action") or "")
        planned_target = str(p.get("target_company_id") or "")
        current_target = str(c.get("existing_company_id") or "")
        if planned_action != current_action or (
            planned_action == "matched_existing" and planned_target != current_target
        ):
            drift.append({
                "type": "identity_decision_changed",
                "source_company_key": key,
                "planned_action": planned_action,
                "current_action": current_action,
                "planned_target_company_id": planned_target or None,
                "current_target_company_id": current_target or None,
            })
    return {"valid": not drift, "drift_count": len(drift), "drift": drift}


def _export_source_key(item: Mapping[str, Any], details: Mapping[str, dict]) -> str:
    source_id = str(item.get("company_id") or "")
    detail = details.get(source_id, {}) if isinstance(details, Mapping) else {}
    debug = detail.get("debug") or {} if isinstance(detail, Mapping) else {}
    row = debug.get("lead_prioritizer_row") or {} if isinstance(debug, Mapping) else {}
    return str(row.get("source_company_key") or item.get("source_company_key") or "").strip()


def reconcile_export_with_import_plan(
    new_items: list[dict], new_details: Mapping[str, dict], plan: Mapping[str, Any],
) -> tuple[list[dict], dict[str, dict], dict[str, Any]]:
    """Apply frozen identity decisions to enriched output using source lineage."""
    planned = {str(e.get("source_company_key") or ""): e for e in plan.get("entries", [])}
    if not planned:
        raise ValueError("import plan has no entries")
    seen_sources: set[str] = set()
    claimed_ids: set[str] = set()
    out_items: list[dict] = []
    out_details: dict[str, dict] = {}
    lineage: list[dict[str, Any]] = []

    for item in new_items:
        source_id = str(item.get("company_id") or "")
        source_key = _export_source_key(item, new_details)
        if not source_key or source_key not in planned:
            raise ValueError(f"enriched export is not covered by import plan: {source_id or source_key}")
        if source_key in seen_sources:
            raise ValueError(f"enriched export contains duplicate source lineage: {source_key}")
        seen_sources.add(source_key)
        decision = planned[source_key]
        final_id = str(decision.get("target_company_id") or "")
        if not final_id:
            raise ValueError(f"import plan target missing for {source_key}")
        if not final_id or final_id in claimed_ids:
            raise ValueError(f"import-plan company_id collision: {final_id!r}")
        claimed_ids.add(final_id)

        copy_item = dict(item)
        copy_item["company_id"] = final_id
        out_items.append(copy_item)
        detail = dict(new_details.get(source_id, {}))
        if detail:
            detail["company_id"] = final_id
            out_details[final_id] = detail
        lineage.append({
            "source_company_key": source_key,
            "export_company_id": source_id,
            "final_company_id": final_id,
            "action": decision.get("action"),
        })

    missing = sorted(set(planned) - seen_sources)
    if missing:
        raise ValueError(f"enriched export is missing {len(missing)} import-plan entries: {missing[:5]}")

    match_report = {
        "summary": {
            "matched_existing": sum(e.get("action") == "matched_existing" for e in plan.get("entries", [])),
            "new": sum(e.get("action") == "new" for e in plan.get("entries", [])),
            "ambiguous": 0,
            "total": len(plan.get("entries", [])),
        },
        "entries": [
            {
                "source_company_key": e.get("source_company_key"),
                "company_name": e.get("company_name"),
                "action": e.get("action"),
                "existing_company_id": e.get("target_company_id"),
                "match_basis": e.get("match_basis"),
                "confidence": e.get("confidence"),
            }
            for e in plan.get("entries", [])
        ],
        "lineage": lineage,
        "import_plan_sha256": plan.get("plan_sha256"),
    }
    return out_items, out_details, match_report


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
