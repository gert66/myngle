"""Streaming, resumable, immutable raw extraction.

Writes one JSONL file per object under ``<output_dir>/raw/``, appends
pagination metadata to a checkpoint file so a re-run can resume instead of
re-fetching from scratch, and never holds more than one page of records in
memory at a time (resource-awareness requirement: the target VM is small).
"""
from __future__ import annotations

import json
import os
from typing import Iterable

from .hubspot_client import ACTIVITY_TYPES, HubSpotReadClient, OBJECT_TYPES
from .models import now_iso

CHECKPOINT_NAME = "_checkpoint.json"


def _checkpoint_path(raw_dir: str) -> str:
    return os.path.join(raw_dir, CHECKPOINT_NAME)


def _load_checkpoint(raw_dir: str) -> dict:
    path = _checkpoint_path(raw_dir)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _save_checkpoint(raw_dir: str, checkpoint: dict) -> None:
    path = _checkpoint_path(raw_dir)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(checkpoint, fh, indent=2)
    os.replace(tmp_path, path)


def extract_object(
    client: HubSpotReadClient,
    object_type: str,
    raw_dir: str,
    page_size: int,
    is_activity: bool = False,
    max_pages: int | None = None,
) -> dict:
    """Extract one object type page-by-page, streaming to JSONL.

    Returns a small summary dict (record_count, pages, resumed, errors) for
    ``run_status.json`` / logs -- never the records themselves.
    """
    os.makedirs(raw_dir, exist_ok=True)
    checkpoint = _load_checkpoint(raw_dir)
    obj_checkpoint = checkpoint.get(object_type, {})
    cursor = obj_checkpoint.get("next_cursor")
    resumed = cursor is not None
    record_count = obj_checkpoint.get("record_count", 0)
    pages = obj_checkpoint.get("pages", 0)
    errors = []

    out_path = os.path.join(raw_dir, f"{object_type}.jsonl")
    mode = "a" if resumed and os.path.exists(out_path) else "w"

    fetch_page = client.get_activity_page if is_activity else client.get_page

    with open(out_path, mode, encoding="utf-8") as out_fh:
        while True:
            if max_pages is not None and pages >= max_pages:
                break
            try:
                page = fetch_page(object_type, cursor, page_size)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
                break
            for record in page.records:
                envelope = {
                    "record": record,
                    "extracted_at": page.fetched_at,
                    "page_index": pages,
                }
                out_fh.write(json.dumps(envelope) + "\n")
            record_count += len(page.records)
            pages += 1
            cursor = page.next_cursor
            checkpoint[object_type] = {
                "next_cursor": cursor,
                "record_count": record_count,
                "pages": pages,
                "updated_at": now_iso(),
                "complete": cursor is None,
            }
            _save_checkpoint(raw_dir, checkpoint)
            if cursor is None:
                break

    return {
        "object_type": object_type,
        "record_count": record_count,
        "pages": pages,
        "resumed": resumed,
        "complete": cursor is None,
        "errors": errors,
    }


def extract_all(client: HubSpotReadClient, raw_dir: str, page_size: int, capability_matrix: dict) -> dict:
    """Extract every object whose capability is available/partially_available."""
    summaries = {}
    for obj in OBJECT_TYPES:
        cap = capability_matrix.get(obj)
        if cap is not None and cap.status.value in ("endpoint_unavailable", "error", "scope_missing"):
            summaries[obj] = {"object_type": obj, "skipped": True, "reason": cap.detail}
            continue
        summaries[obj] = extract_object(client, obj, raw_dir, page_size, is_activity=False)
    for obj in ACTIVITY_TYPES:
        cap = capability_matrix.get(obj)
        if cap is not None and cap.status.value in ("endpoint_unavailable", "error", "scope_missing"):
            summaries[obj] = {"object_type": obj, "skipped": True, "reason": cap.detail}
            continue
        summaries[obj] = extract_object(client, obj, raw_dir, page_size, is_activity=True)

    owners = client.get_owners()
    owners_path = os.path.join(raw_dir, "owners.jsonl")
    with open(owners_path, "w", encoding="utf-8") as fh:
        for owner in owners:
            fh.write(json.dumps({"record": owner, "extracted_at": now_iso()}) + "\n")
    summaries["owners"] = {"object_type": "owners", "record_count": len(owners)}

    for obj in OBJECT_TYPES:
        cap_key = f"properties_{obj}"
        cap = capability_matrix.get(cap_key)
        props_path = os.path.join(raw_dir, f"properties_{obj}.json")
        if cap is not None and cap.status.value in ("endpoint_unavailable", "error", "scope_missing"):
            with open(props_path, "w", encoding="utf-8") as fh:
                json.dump({"properties": [], "extracted_at": now_iso(), "skipped": True, "reason": cap.detail}, fh, indent=2)
            summaries[cap_key] = {"object_type": cap_key, "record_count": 0, "skipped": True, "reason": cap.detail}
            continue
        try:
            props = client.get_properties(obj)
            with open(props_path, "w", encoding="utf-8") as fh:
                json.dump({"properties": props, "extracted_at": now_iso()}, fh, indent=2)
            summaries[cap_key] = {"object_type": cap_key, "record_count": len(props)}
        except Exception as exc:
            with open(props_path, "w", encoding="utf-8") as fh:
                json.dump({"properties": [], "extracted_at": now_iso(), "skipped": True, "reason": f"{type(exc).__name__}: {exc}"}, fh, indent=2)
            summaries[cap_key] = {"object_type": cap_key, "record_count": 0, "skipped": True, "reason": f"{type(exc).__name__}: {exc}"}

    return summaries


def read_jsonl(path: str) -> Iterable[dict]:
    """Stream a JSONL file one line at a time -- never load it fully into memory."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
