"""country_visibility_app.py — Streamlit toggle for which countries the
Lovable Company Hub shows.

Reads/writes ``gs://<bucket>/countries.index.json`` directly — the same
file ``generate_lovable_countries_index.py`` produces from the hardcoded
``DISABLED_COUNTRY_LABELS`` set — but this app lets an operator flip a
country's ``enabled`` flag from a browser, without a code change or
redeploy. The Lovable Company Hub reads this manifest to decide which
countries to offer in its country picker; it never scans the bucket
itself, so a country with ``enabled: false`` here simply never appears
there, regardless of whether its GCS folder has data.

Usage:
    streamlit run country_visibility_app.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

from generate_lovable_countries_index import MANIFEST_COUNTRY_LABELS, _manifest_id
from lovable_gcs_upload import (
    CURRENT_CACHE_CONTROL,
    DEFAULT_GCS_BUCKET,
    check_gcloud_available,
    country_folder_slug,
    describe_gcloud_environment,
    fetch_gcs_text,
    gcs_manifest_path,
    public_manifest_url,
    resolve_gcs_upload_tool,
    upload_file,
)

# =============================================================================
# Pure helpers — no Streamlit import required, so these are unit-testable
# =============================================================================


def default_entry(label: str, bucket: str) -> dict:
    """A fresh, enabled-by-default manifest entry for ``label`` — used both
    as the fallback when no manifest is published yet and to backfill a
    country that's in the code's country list but missing from a live
    manifest published by an older version of the app."""
    return {
        "id": _manifest_id(label),
        "label": label,
        "enabled": True,
        "baseUrl": f"https://storage.googleapis.com/{bucket}/{country_folder_slug(label)}/current",
    }


def parse_manifest_json(raw: Optional[str]) -> Optional[dict]:
    """Parse a manifest JSON string, or ``None`` on missing/invalid input —
    never raises, so a corrupted or absent remote file falls back cleanly
    to a freshly-built manifest instead of crashing the app."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("countries"), list):
        return None
    return parsed


def merge_with_known_labels(manifest: Optional[dict], bucket: str) -> list[dict]:
    """Build the checkbox list: one entry per label in
    ``MANIFEST_COUNTRY_LABELS`` (the same source-of-truth list
    ``generate_lovable_countries_index.py`` uses), preserving each
    country's live ``enabled``/``baseUrl`` from ``manifest`` when present,
    and defaulting a country that's in the code but missing from the live
    manifest (e.g. one added since the manifest was last uploaded) to
    enabled. Countries in the live manifest but no longer in
    ``MANIFEST_COUNTRY_LABELS`` are dropped -- the code list is
    authoritative for *which* countries exist; this app only controls
    *visibility*.
    """
    by_id = {c.get("id"): c for c in (manifest or {}).get("countries", []) if isinstance(c, dict)}
    merged = []
    for label in MANIFEST_COUNTRY_LABELS:
        cid = _manifest_id(label)
        existing = by_id.get(cid)
        if existing and "enabled" in existing:
            entry = default_entry(label, bucket)
            entry["enabled"] = bool(existing["enabled"])
            entry["baseUrl"] = existing.get("baseUrl") or entry["baseUrl"]
            merged.append(entry)
        else:
            merged.append(default_entry(label, bucket))
    return merged


def load_current_countries(bucket: str) -> list[dict]:
    """Live GCS state merged with the known-labels list -- the single
    entry point both the app and its tests use to get the checkbox rows."""
    fetch_result = fetch_gcs_text(gcs_manifest_path(bucket))
    manifest = parse_manifest_json(fetch_result.get("text")) if fetch_result.get("exists") else None
    return merge_with_known_labels(manifest, bucket)


# =============================================================================
# Streamlit UI
# =============================================================================


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import streamlit as st

    st.set_page_config(page_title="Landen zichtbaarheid", page_icon="🌍", layout="centered")
    st.title("🌍 Landen zichtbaarheid — Company Hub")
    st.caption(
        "Bepaalt welke landen zichtbaar zijn in de Lovable Company Hub-app "
        "(landenkeuze linksboven). Opslaan schrijft direct naar "
        "`gs://<bucket>/countries.index.json` — geen code-wijziging of "
        "redeploy nodig. Uitzetten verwijdert geen data uit de bucket, "
        "alleen de zichtbaarheid in de app-picker verandert."
    )

    bucket = st.text_input("Bucket", value=DEFAULT_GCS_BUCKET, key="cv_bucket")

    if "cv_countries" not in st.session_state or st.session_state.get("cv_bucket_loaded") != bucket:
        st.session_state["cv_countries"] = load_current_countries(bucket)
        st.session_state["cv_bucket_loaded"] = bucket

    if st.button("🔄 Opnieuw laden vanuit GCS", help="Verwerpt onopgeslagen wijzigingen hieronder."):
        st.session_state["cv_countries"] = load_current_countries(bucket)
        st.rerun()

    st.divider()

    countries = st.session_state["cv_countries"]
    for entry in countries:
        entry["enabled"] = st.checkbox(
            entry["label"], value=bool(entry.get("enabled")), key=f"cv_check_{entry['id']}",
        )

    st.divider()
    enabled_labels = [c["label"] for c in countries if c["enabled"]]
    disabled_labels = [c["label"] for c in countries if not c["enabled"]]
    st.write(f"**Zichtbaar in de app ({len(enabled_labels)}):** {', '.join(enabled_labels) or '—'}")
    st.write(f"**Verborgen ({len(disabled_labels)}):** {', '.join(disabled_labels) or '—'}")

    if st.button("💾 Opslaan & uploaden naar GCS", type="primary"):
        tool_info = check_gcloud_available()
        if not tool_info["available"]:
            st.error("Geen gcloud/gsutil gevonden op PATH — kan niet uploaden.")
            return
        env = describe_gcloud_environment()
        st.caption(f"gcloud account: {env['account'] or '(geen actief)'} · project: {env['project'] or '(geen)'}")

        manifest = {"countries": countries}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "countries.index.json"
            path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tool_cmd = resolve_gcs_upload_tool()
            destination = gcs_manifest_path(bucket)
            result = upload_file(tool_cmd, str(path), destination, cache_control=CURRENT_CACHE_CONTROL)

        if result["success"]:
            st.success(f"Geüpload naar {destination}")
            st.caption(f"Publieke URL: {public_manifest_url(bucket)}")
        else:
            st.error(f"Upload mislukt: {result.get('error') or result.get('stderr')}")


if __name__ == "__main__":
    main()
