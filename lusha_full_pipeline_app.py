"""
lusha_full_pipeline_app.py
---------------------------
End-to-end pipeline, one Streamlit app: Lusha Prospecting -> Lusha Enrich ->
Input Cleaner · Lusha Edition (with optional Haiku prescreen) -> Cloud Run
Job -> (automatic) merge + Lovable export/upload into current/ -> country
visibility in the Lovable Company Hub.

This module does NOT reimplement any of those stages -- it imports and
chains the existing, already-tested building blocks:
  - lusha_prospecting_app: Prospecting search + pagination.
  - (this file) enrich_companies / lusha_enrich_records_to_dataframe: the
    one genuinely new piece -- Prospecting alone returns only id/name/domain,
    which is not enough for the Input Cleaner's industry-exclusion rules or
    the Haiku prescreen (both need Main Industry / Description). Enrich
    fills that gap, at Enrich's own (higher) per-result credit cost.
  - input_cleaner_lusha_edition: dedupe -> industry exclusion -> hot list ->
    optional Haiku prescreen -> sort -> the same 4-sheet workbook a human
    would produce with that standalone app.
  - cloud_run_streamlit_app: job submission (upload + execute, exactly the
    same gcloud-subprocess commands the manual dashboard uses) and the
    existing Autopilot machinery (finish_run_merge -> Lovable export ->
    current/) -- triggered here by writing the same manifest.json shape
    the dashboard's own "Autopilot" checkbox produces, then polling GCS
    status the same way the dashboard's status panel does.
  - country_visibility_app / generate_lovable_countries_index: registers a
    brand-new country in the hardcoded MANIFEST_COUNTRY_LABELS list (a
    source-code change this app makes and shows you, but does not commit)
    and flips it visible in the live countries.index.json manifest.

Because a Cloud Run Job run can take a long time, "Start volledige
pipeline" blocks and polls in-place, live-updating the page, all the way
through to the country becoming visible -- keep the browser tab open for
the run's duration. If the tab is closed partway through, the already-
submitted job still finishes and (if Autopilot's own Eventarc trigger is
wired up) still merges/exports on its own -- only the final "make the
country visible" step would then need a manual re-run of this app once the
run has actually finished.

The `import streamlit`/`pandas` calls are deliberately lazy (inside `main`)
so the pure helper functions below can be imported and unit-tested without
Streamlit installed.

Run with:
    streamlit run lusha_full_pipeline_app.py
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from lusha_prospecting_app import (
    DEFAULT_EXCLUDED_INDUSTRY_IDS,
    SIZE_BANDS,
    _BASE_URL,
    _headers,
    fetch_all_companies,
    find_locations,
    get_account_usage,
    resolve_industry_labels,
    size_band_label,
)

_ENRICH_ENDPOINT = "/v3/companies/enrich"
_ENRICH_BATCH_SIZE = 100  # Lusha's documented max ids per /companies/enrich call
_ENRICH_REQUEST_TIMEOUT = 30

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


# =============================================================================
# Stage 1.5 -- Enrich: Prospecting alone has no Main Industry/Description,
# so the Input Cleaner's rules and Haiku prescreen have nothing to work
# with. Enrich fills that gap for the whole prospected set.
# =============================================================================

def enrich_companies(key: str, ids: list[str], *, reveal: "list[str] | None" = None) -> list[dict]:
    """Full firmographic records for ``ids`` (Prospecting result ids), in
    batches of ``_ENRICH_BATCH_SIZE`` (Lusha's own per-call cap). Order of
    the input ``ids`` is not preserved across batch boundaries -- callers
    that need to correlate back to a prospecting row should key off the
    returned records' own ``id`` field."""
    results: list[dict] = []
    for i in range(0, len(ids), _ENRICH_BATCH_SIZE):
        batch = ids[i:i + _ENRICH_BATCH_SIZE]
        body: dict = {"ids": batch}
        if reveal:
            body["reveal"] = list(reveal)
        resp = requests.post(
            _BASE_URL + _ENRICH_ENDPOINT, headers=_headers(key), json=body,
            timeout=_ENRICH_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results.extend(resp.json().get("results") or [])
    return results


def lusha_enrich_records_to_dataframe(records: list[dict]):
    """Enrich records -> a DataFrame with Lusha "full export" (UI-CSV-style)
    column names, so ``input_cleaner_lusha_edition.detect_lusha_columns``
    recognises them exactly as it would a manually-exported CSV -- no
    separate/duplicate column-detection logic needed here."""
    import pandas as pd

    rows = []
    for r in records:
        employee_count = r.get("employeeCount") or {}
        location = r.get("location") or {}
        social = r.get("socialLinks") or {}
        rows.append({
            "Company Name": r.get("name") or "",
            "Company Domain": r.get("domain") or "",
            "Company Description": r.get("description") or "",
            "Company Number of Employees": employee_count.get("exact")
                or employee_count.get("max") or employee_count.get("min") or "",
            "Company Main Industry": r.get("industry") or "",
            "Company Sub Industry": r.get("subIndustry") or "",
            "Company Country": location.get("country") or "",
            "Company linkedin URL": social.get("linkedin") or "",
            "Company Intent Topics": "",  # not present on Enrich responses
            "_lusha_id": r.get("id") or "",
        })
    return pd.DataFrame(rows)


# =============================================================================
# Stage 2 -- Input Cleaner · Lusha Edition, called as pure functions (no
# Streamlit widgets) in the exact sequence input_cleaner_lusha_edition.main()
# uses interactively.
# =============================================================================

def run_input_cleaning(
    df, *, run_prescreen: bool, anthropic_key: str = "",
    model: str = DEFAULT_ANTHROPIC_MODEL, progress_callback=None,
) -> dict:
    """Dedupe -> industry exclusion/hot-list -> optional Haiku prescreen ->
    sort -> batch-app-compatible columns -> the same 4-sheet workbook bytes
    ``input_cleaner_lusha_edition``'s own "Download cleaned Excel" produces.

    Government/Community are NOT re-excluded here -- Lusha's own
    ``exclude.mainIndustriesIds`` filter already kept them out of the
    Prospecting results, before Enrich ever ran. Other industry-exclusion
    rules (Education, care-delivery, ...) still apply here as normal.

    Returns ``{"excel_bytes", "selected_df", "hot_df", "excluded_df",
    "funnel", "prescreen_ran"}``.
    """
    from input_cleaner_lusha_edition import (
        IndustryExclusionConfig,
        add_batch_app_compatible_columns,
        build_excel,
        classify_rows,
        dedupe_by_domain,
        detect_lusha_columns,
        eligible_for_prescreen,
        estimate_prescreen_cost,
        missing_required_lusha_columns,
        prescreen_rows_with_ai,
        sort_selected_rows,
    )

    mapping = detect_lusha_columns(df)
    missing = missing_required_lusha_columns(mapping)
    if missing:
        raise ValueError(f"Ontbrekende verplichte kolom(men): {', '.join(missing)}")

    total_rows = len(df)
    # Government/Community are already excluded server-side by Lusha's own
    # Prospecting filter -- keep the other rule-based exclusions active.
    config = IndustryExclusionConfig(exclude_government=False, exclude_nonprofit=False)

    deduped_df, removed_dupes = dedupe_by_domain(df, mapping["domain"])
    classified_df = classify_rows(deduped_df, mapping, config)
    excluded_df = classified_df[classified_df["excluded"]].copy()
    selected_df = classified_df[~classified_df["excluded"]].copy()
    override_count = int(classified_df["intent_override_warning"].sum())

    prescreen_ran = False
    eligible_mask = eligible_for_prescreen(selected_df, mapping)
    cost_estimate = estimate_prescreen_cost(selected_df, mapping, eligible_mask, model=model)
    if run_prescreen and cost_estimate["eligible_rows"] > 0 and anthropic_key:
        selected_df = prescreen_rows_with_ai(
            selected_df, mapping, eligible_mask, anthropic_key, model=model,
            progress_cb=progress_callback,
        )
        prescreen_ran = True

    selected_df = sort_selected_rows(selected_df, mapping)
    selected_df = add_batch_app_compatible_columns(selected_df, mapping)
    hot_df = (selected_df[selected_df["hot_list"]].copy()
              if "hot_list" in selected_df.columns else selected_df.iloc[0:0].copy())

    funnel = {
        "Rijen in bronbestand": total_rows,
        "Duplicaten verwijderd": removed_dupes,
        "Na ontdubbeling": len(deduped_df),
        "Uitgesloten op industrie": len(excluded_df),
        "Behouden ondanks uitsluitregel (intent)": override_count,
        "Geselecteerd (totaal)": len(selected_df),
        "Waarvan hot list (intent topics)": len(hot_df),
    }
    if prescreen_ran and "icp_prescreen" in selected_df.columns:
        funnel["Prescreen: likely_fit"] = int((selected_df["icp_prescreen"] == "likely_fit").sum())
        funnel["Prescreen: unclear"] = int((selected_df["icp_prescreen"] == "unclear").sum())
        funnel["Prescreen: unlikely_fit"] = int((selected_df["icp_prescreen"] == "unlikely_fit").sum())

    excel_bytes = build_excel(selected_df, hot_df, excluded_df, funnel)
    return {
        "excel_bytes": excel_bytes, "selected_df": selected_df, "hot_df": hot_df,
        "excluded_df": excluded_df, "funnel": funnel, "prescreen_ran": prescreen_ran,
        "prescreen_cost_estimate": cost_estimate,
    }


# =============================================================================
# Stage 3 -- Cloud Run Job submission (same commands as the manual dashboard)
# =============================================================================

def build_pipeline_run_manifest(
    *, run_id: str, input_uri: str, output_dir: str, task_count: int,
    execution_name: "str | None", export_country: str, cold_callers: list[str],
    foreign_hq_only: bool, gcs_bucket: str,
) -> dict:
    """Same manifest.json shape cloud_run_streamlit_app's "Start Cloud Run"
    button writes (see its config.autopilot / config.lovable_export block)
    -- writing this shape is what lets the existing Autopilot machinery
    (Eventarc-triggered merge+export, and this app's own polling fallback,
    see run_autopilot_step) pick the run up with no further input."""
    from lead_prioritizer_batch_app import default_gcs_country_prefix

    return {
        "run_id": run_id, "input_uri": input_uri, "output_dir": output_dir,
        "task_count": int(task_count), "mode": "full",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "lusha_full_pipeline_app",
        "execution_name": execution_name,
        "config": {
            "gate_full_enrichment_on_foreign_hq": bool(foreign_hq_only),
            "autopilot": True,
            "lovable_export": {
                "enabled": True,
                "country": export_country,
                "cold_callers": cold_callers,
                "foreign_hq_only": bool(foreign_hq_only),
                "bucket_size": 500,
                "gcs_bucket": gcs_bucket,
                "gcs_prefix": default_gcs_country_prefix(export_country),
                "gcs_run_folder": run_id,
                "merge_current": True,
            },
        },
    }


def submit_cloud_run_job(
    *, local_input: Path, project: str, region: str, job_name: str,
    run_bucket: str, task_count: int, foreign_hq_only: bool,
    export_country: str, cold_callers: list[str], export_gcs_bucket: str,
    work_dir: Path,
) -> dict:
    """Upload the cleaned Excel and start the Cloud Run Job execution, then
    write manifest.json with Autopilot configured -- the same three-step
    sequence (upload -> execute -> write+upload manifest) the dashboard's
    "Start Cloud Run" button runs, reusing its exact pure command-builders
    and subprocess runner so this is guaranteed to behave identically.

    Returns ``{"ok", "run_id", "input_uri", "output_dir", "execution_name",
    "error"}``. Never raises -- a failure at any of the three steps comes
    back as ``{"ok": False, "error": ...}``.
    """
    from cloud_dispatcher import build_run_id
    from cloud_run_streamlit_app import (
        build_execute_command,
        build_upload_command,
        extract_execution_name,
        gcs_incoming_uri,
        gcs_output_dir,
        run_capture,
    )

    run_id = build_run_id(local_input.name)
    input_uri = gcs_incoming_uri(run_bucket, local_input.name)
    output_dir = gcs_output_dir(run_bucket, run_id)

    rc, output = run_capture(build_upload_command(str(local_input), input_uri, project))
    if rc != 0:
        return {"ok": False, "error": f"Upload mislukt: {output}"}

    extra_env = {
        "COMPOSE_CALLER_CONTENT": "true",
        "RICH_ICP_CONTEXT": "true",
        "AI_SIGNAL_SCORING": "true",
        "USE_ENRICHMENT_CACHE": "true",
        "ENRICHMENT_CACHE_BUCKET": run_bucket,
        "DEEP_DIVE": "true",
        "DEEP_DIVE_MIN_SCORE": "8.0",
        "DEEP_DIVE_ON_FOREIGN_HQ": "true",
        "C5_ENABLED": "true",
        "GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ": str(bool(foreign_hq_only)).lower(),
    }
    rc, output = run_capture(build_execute_command(
        job_name, project, region, input_uri, output_dir, run_id,
        int(task_count), "full", extra_env=extra_env, wait=False,
    ))
    if rc != 0:
        return {"ok": False, "error": f"Cloud Run Job starten mislukt: {output}"}
    execution_name = extract_execution_name(output)

    manifest = build_pipeline_run_manifest(
        run_id=run_id, input_uri=input_uri, output_dir=output_dir,
        task_count=task_count, execution_name=execution_name,
        export_country=export_country, cold_callers=cold_callers,
        foreign_hq_only=foreign_hq_only, gcs_bucket=export_gcs_bucket,
    )
    manifest_local = work_dir / "manifest.json"
    manifest_local.write_text(json.dumps(manifest), encoding="utf-8")
    from cloud_job_runner import join_path
    run_capture(build_upload_command(
        str(manifest_local), join_path(output_dir, "manifest.json"), project))

    return {
        "ok": True, "run_id": run_id, "input_uri": input_uri,
        "output_dir": output_dir, "execution_name": execution_name,
        "manifest": manifest,
    }


# =============================================================================
# Stage 4 -- Poll GCS for task completion (same status derivation the
# dashboard's status panel uses)
# =============================================================================

def poll_run_stage(*, bucket: str, project: str, run_id: str, task_count: int) -> dict:
    """One status check -- mirrors the dashboard's status panel exactly:
    list status/*.json, count them, read final/manifest_done.json if
    present, classify the stage. Returns
    ``{"stage", "counts", "merge_manifest"}``."""
    from cloud_job_runner import join_path
    from cloud_run_streamlit_app import (
        build_list_command,
        count_task_statuses,
        determine_run_stage,
        gcs_output_dir,
        read_gcs_json,
        run_capture,
    )

    output_dir = gcs_output_dir(bucket, run_id)
    rc_ls, listing = run_capture(build_list_command(
        join_path(output_dir, "status", "*.json"), project))
    counts = count_task_statuses(listing) if rc_ls == 0 else {"running": 0, "done": 0, "failed": 0}
    merge_manifest = read_gcs_json(join_path(output_dir, "final", "manifest_done.json"), project)
    stage = determine_run_stage(counts, task_count, merge_manifest)
    return {"stage": stage, "counts": counts, "merge_manifest": merge_manifest}


def run_autopilot_step(*, bucket: str, project: str, run_id: str, task_count: int, work_dir: Path) -> dict:
    """Once a run is ``ready_to_merge``, chain merge -> Lovable export ->
    current/ -- the same call the dashboard's own Autopilot panel makes
    when a human happens to revisit the status page. Reading arguments back
    out of the manifest (via ``lovable_export_args_from_manifest``) means
    this app doesn't need to re-thread every export setting through the
    polling loop itself."""
    from cloud_job_runner import join_path
    from cloud_run_streamlit_app import (
        gcs_output_dir,
        lovable_export_args_from_manifest,
        read_gcs_json,
        run_autopilot_merge_and_export,
    )

    output_dir = gcs_output_dir(bucket, run_id)
    manifest = read_gcs_json(join_path(output_dir, "manifest.json"), project)
    args = lovable_export_args_from_manifest(run_id, manifest)
    return run_autopilot_merge_and_export(
        bucket=bucket, project=project, run_id=run_id, task_count=task_count,
        work_dir=work_dir, export_country=args["country"],
        cold_callers_raw=args["cold_callers_raw"],
        foreign_hq_only_export=args["foreign_hq_only"], bucket_size=500,
        content_language="English", export_gcs_bucket=args["gcs_bucket"],
        export_gcs_prefix=args["gcs_prefix"], export_gcs_run_folder=args["gcs_run_folder"],
        merge_current=args["merge_current"],
    )


# =============================================================================
# Stage 5 -- Country visibility
# =============================================================================

def country_needs_registration(label: str) -> bool:
    from generate_lovable_countries_index import MANIFEST_COUNTRY_LABELS
    return label not in MANIFEST_COUNTRY_LABELS


def register_country_in_source(label: str, *, path: "Path | None" = None) -> "str | None":
    """Adds ``label`` to ``MANIFEST_COUNTRY_LABELS`` in
    generate_lovable_countries_index.py (alphabetically, matching the
    existing list's own ordering) and writes the file back. Returns the
    unified diff-style summary of the change, or ``None`` if the label was
    already present (no-op). This edits a git-tracked source file -- the
    caller is responsible for showing the change and letting a human
    review/commit/push it; this function never touches git itself.

    ``path`` defaults to the real generate_lovable_countries_index.py next
    to this file; tests inject a temp-directory copy instead."""
    if not country_needs_registration(label):
        return None

    path = path or (Path(__file__).parent / "generate_lovable_countries_index.py")
    text = path.read_text(encoding="utf-8")
    match = re.search(r"MANIFEST_COUNTRY_LABELS\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not match:
        raise RuntimeError(
            "Kon MANIFEST_COUNTRY_LABELS niet vinden in generate_lovable_countries_index.py")

    from generate_lovable_countries_index import MANIFEST_COUNTRY_LABELS
    new_labels = sorted(set(MANIFEST_COUNTRY_LABELS) | {label})
    new_block = ", ".join(f'"{lbl}"' for lbl in new_labels)
    # Match the file's own wrapping style (one label per short line) is
    # unnecessary for correctness -- a single well-formed list literal is
    # all Python needs, and this keeps the diff minimal to reason about.
    new_text = text[:match.start()] + f"MANIFEST_COUNTRY_LABELS = [{new_block}]" + text[match.end():]
    path.write_text(new_text, encoding="utf-8")
    return (
        f"+ \"{label}\" toegevoegd aan MANIFEST_COUNTRY_LABELS in "
        f"generate_lovable_countries_index.py -- controleer en commit deze wijziging."
    )


def make_country_visible(bucket: str, label: str) -> dict:
    """Ensures ``label`` is present (enabled) in the live
    ``countries.index.json`` manifest -- assumes it's already in
    MANIFEST_COUNTRY_LABELS (call register_country_in_source first if not).
    Returns the upload result dict from lovable_gcs_upload.upload_file."""
    from country_visibility_app import load_current_countries
    from lovable_gcs_upload import (
        CURRENT_CACHE_CONTROL,
        check_gcloud_available,
        gcs_manifest_path,
        resolve_gcs_upload_tool,
        upload_file,
    )

    tool_info = check_gcloud_available()
    if not tool_info["available"]:
        return {"success": False, "error": "Geen gcloud/gsutil gevonden op PATH."}

    countries = load_current_countries(bucket)
    found = False
    for entry in countries:
        if entry["label"] == label:
            entry["enabled"] = True
            found = True
    if not found:
        # Reflects a manifest already regenerated (offline) with the new
        # label present but not yet reflected in the live GCS file.
        countries = load_current_countries(bucket)

    manifest = {"countries": countries}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "countries.index.json"
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result = upload_file(
            resolve_gcs_upload_tool(), str(path), gcs_manifest_path(bucket),
            cache_control=CURRENT_CACHE_CONTROL,
        )
    return result


# =============================================================================
# Streamlit UI
# =============================================================================

def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import pandas as pd
    import streamlit as st

    from cloud_run_streamlit_app import DEFAULT_JOB_NAME, DEFAULT_PROJECT, DEFAULT_REGION, suggest_task_count
    from lead_prioritizer_batch_app import DEFAULT_COLD_CALLERS_TEXT, parse_cold_callers
    from lovable_gcs_upload import DEFAULT_GCS_BUCKET

    st.set_page_config(page_title="Lusha -> Live pipeline", page_icon="\U0001f6e4️", layout="wide")
    st.title("\U0001f6e4️ Lusha → Live: volledige pijplijn")
    st.caption(
        "Van Lusha-prospecting tot en met een zichtbaar land in de Lovable "
        "Company Hub, in één doorlopende run. Blijft dit tabblad open tot de "
        "melding 'Klaar' verschijnt — de run zelf loopt op Cloud Run en stopt "
        "niet als je even wegklikt, maar de laatste stap (land zichtbaar "
        "maken) gebeurt alleen als deze pagina blijft pollen."
    )

    with st.sidebar:
        st.header("API-keys")
        lusha_key = st.text_input(
            "Lusha API-key", value=os.environ.get("LUSHA_API_KEY", ""), type="password")
        anthropic_key = st.text_input(
            "Anthropic API-key (voor Haiku-prescreen)",
            value=os.environ.get("ANTHROPIC_API_KEY", ""), type="password")

        st.divider()
        st.header("1. Land")
        country_query = st.text_input("Land", value="Uruguay", key="pl_country_query")
        if st.button("\U0001f50d Land opzoeken", disabled=not (lusha_key and country_query)):
            try:
                st.session_state["_pl_location_matches"] = find_locations(lusha_key, country_query)
            except Exception as exc:
                st.error(f"Opzoeken mislukt: {exc}")
        matches = st.session_state.get("_pl_location_matches") or []
        location = None
        if matches:
            labels = [
                f"{m.get('country', '?')} — {m.get('continent', '?')} / {m.get('countryGrouping', '?')}"
                for m in matches
            ]
            idx = st.selectbox(
                "Exacte match", options=list(range(len(matches))),
                format_func=lambda i: labels[i], key="pl_location_select")
            location = matches[idx]

        st.divider()
        st.header("2. Bedrijfsgrootte")
        chosen_bands = []
        for i, band in enumerate(SIZE_BANDS):
            if st.checkbox(size_band_label(band), value=True, key=f"pl_size_band_{i}"):
                chosen_bands.append(band)

        st.divider()
        st.header("3. Reikwijdte")
        foreign_hq_only = st.checkbox(
            "Alleen bevestigd buitenlandse HQ volledig verrijken", value=True,
            key="pl_foreign_hq_only",
            help="Aan: alleen bedrijven met bevestigd buitenlands hoofdkantoor "
                 "worden volledig verrijkt en geëxporteerd (de gebruikelijke "
                 "instelling). Uit: de hele geselecteerde lijst wordt volledig "
                 "verwerkt.",
        )

        st.divider()
        st.header("4. Curatie")
        run_prescreen = st.checkbox(
            "Haiku-prescreen (extra Enrich-kosten voor Description/Industrie)",
            value=True, key="pl_run_prescreen",
        )

        st.divider()
        st.header("5. Lovable-export")
        cold_callers_raw = st.text_input("Cold callers", value=DEFAULT_COLD_CALLERS_TEXT)

        st.divider()
        with st.expander("Geavanceerd (project/regio/job)"):
            project = st.text_input("GCP-project", value=DEFAULT_PROJECT)
            region = st.text_input("Regio", value=DEFAULT_REGION)
            job_name = st.text_input("Cloud Run Job", value=DEFAULT_JOB_NAME)
            run_bucket = st.text_input("Bucket voor de Cloud Run-job (incoming/runs)", value=DEFAULT_GCS_BUCKET)
            export_gcs_bucket = st.text_input("Bucket voor Lovable-export/current/", value=DEFAULT_GCS_BUCKET)
            poll_interval = st.number_input("Poll-interval (seconden)", min_value=15, value=30, step=15)

        if lusha_key and st.button("\U0001f4b3 Account & prijzen testen"):
            try:
                st.session_state["_pl_account_usage"] = get_account_usage(lusha_key)
            except Exception as exc:
                st.error(f"Kon account-info niet ophalen: {exc}")
        usage = st.session_state.get("_pl_account_usage")
        if usage:
            credits = usage.get("credits", {})
            st.caption(f"Lusha credits over: {credits.get('remaining', '?')} / {credits.get('total', '?')}")

    ready = bool(lusha_key and location and chosen_bands and country_query)
    if not ready:
        st.info("Vul links een Lusha API-key in, zoek een land op, en kies minstens één grootteband.")
        return

    if not st.button("\U0001f680 Start volledige pipeline", type="primary"):
        return

    work_dir = Path(tempfile.mkdtemp(prefix="lusha_full_pipeline_"))
    cold_callers = parse_cold_callers(cold_callers_raw)
    if not cold_callers:
        st.error("Minimaal één cold caller is verplicht.")
        return

    # ── Stage 1: Prospecting ─────────────────────────────────────────────
    st.subheader("1/5 — Lusha Prospecting")
    prog1 = st.progress(0.0)
    status1 = st.empty()

    def _prospect_progress(page, collected, total):
        prog1.progress(min(1.0, collected / total) if total else 0.0)
        status1.text(f"Pagina {page + 1} — {collected} van {total or '?'} bedrijven…")

    companies, prospect_stats = fetch_all_companies(
        lusha_key, location=location, size_bands=chosen_bands,
        excluded_industry_ids=DEFAULT_EXCLUDED_INDUSTRY_IDS,
        progress_callback=_prospect_progress,
    )
    st.success(
        f"{prospect_stats['companies_collected']} bedrijven opgehaald "
        f"({prospect_stats['credits_charged']} credits)."
    )
    if not companies:
        st.warning("Geen bedrijven gevonden voor deze filters — pipeline gestopt.")
        return

    # ── Stage 1.5: Enrich ─────────────────────────────────────────────────
    st.subheader("2/5 — Lusha Enrich (Industrie/Description voor curatie)")
    ids = [c["id"] for c in companies if c.get("id")]
    with st.spinner(f"{len(ids)} bedrijven verrijken…"):
        enriched = enrich_companies(lusha_key, ids)
    df = lusha_enrich_records_to_dataframe(enriched)
    st.success(f"{len(df)} bedrijven verrijkt met firmografie.")

    # ── Stage 2: Input Cleaner · Lusha Edition ───────────────────────────
    st.subheader("3/5 — Curatie (Input Cleaner · Lusha Edition)")
    prog2 = st.progress(0.0)
    status2 = st.empty()

    def _prescreen_progress(i, n):
        if n:
            prog2.progress(i / n)
        status2.text(f"Haiku-prescreen rij {i} van {n}…")

    try:
        cleaning = run_input_cleaning(
            df, run_prescreen=run_prescreen, anthropic_key=anthropic_key,
            progress_callback=_prescreen_progress,
        )
    except Exception as exc:
        st.error(f"Curatie mislukt: {exc}")
        return
    st.dataframe(pd.DataFrame([cleaning["funnel"]]).T.rename(columns={0: "Aantal"}),
                 use_container_width=True)
    selected_df = cleaning["selected_df"]
    if selected_df.empty:
        st.warning("Geen bedrijven over na curatie — pipeline gestopt.")
        return

    local_input = work_dir / f"{country_query.strip().replace(' ', '_')}_cleaned.xlsx"
    local_input.write_bytes(cleaning["excel_bytes"])

    # ── Stage 3: Cloud Run Job ───────────────────────────────────────────
    st.subheader("4/5 — Cloud Run Job")
    task_count = suggest_task_count(len(selected_df))
    with st.spinner("Uploaden en Cloud Run Job starten…"):
        submission = submit_cloud_run_job(
            local_input=local_input, project=project, region=region, job_name=job_name,
            run_bucket=run_bucket, task_count=task_count, foreign_hq_only=foreign_hq_only,
            export_country=country_query.strip(), cold_callers=cold_callers,
            export_gcs_bucket=export_gcs_bucket, work_dir=work_dir,
        )
    if not submission["ok"]:
        st.error(submission["error"])
        return
    run_id = submission["run_id"]
    st.success(f"Job gestart — run-ID `{run_id}` ({task_count} taken).")

    # ── Stage 4: poll until merged ────────────────────────────────────────
    status3 = st.empty()
    autopilot_done = False
    while True:
        poll = poll_run_stage(bucket=run_bucket, project=project, run_id=run_id, task_count=task_count)
        stage = poll["stage"]
        status3.text(f"Status: {stage} ({poll['counts']})")
        if stage == "merged":
            autopilot_done = True
            break
        if stage in ("has_failed_tasks", "merge_failed"):
            st.error(f"Run gestopt met status '{stage}' — controleer de dashboard-app voor details.")
            return
        if stage == "ready_to_merge":
            with st.spinner("Mergen + Lovable-export…"):
                autopilot_result = run_autopilot_step(
                    bucket=run_bucket, project=project, run_id=run_id,
                    task_count=task_count, work_dir=work_dir,
                )
            if not autopilot_result.get("ok"):
                st.error(f"Mergen/export mislukt: {autopilot_result}")
                return
            autopilot_done = True
            break
        time.sleep(int(poll_interval))

    st.success("Merge + Lovable-export + current/-upload voltooid.")

    # ── Stage 5: country visibility ──────────────────────────────────────
    st.subheader("5/5 — Land zichtbaar maken")
    diff = register_country_in_source(country_query.strip())
    if diff:
        st.warning(diff)
    vis_result = make_country_visible(export_gcs_bucket, country_query.strip())
    if vis_result.get("success"):
        st.success(f"'{country_query.strip()}' is nu zichtbaar in de Company Hub.")
    else:
        st.error(f"Zichtbaar maken mislukt: {vis_result.get('error')}")

    st.balloons()
    st.success(f"Klaar. Run-ID: `{run_id}`.")


if __name__ == "__main__":  # pragma: no cover
    main()
