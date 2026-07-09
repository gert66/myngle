"""Interactive cold-caller reallocation UI for reallocate_callers_from_gcs.py.

Local Streamlit UI on top of reallocate_callers_from_gcs.py: pick a country
folder in the Lovable GCS bucket, load its current run, edit the cold-caller
pool, and see the resulting per-caller workload and the list of companies
that change caller update live, entirely in-memory, before anything is
written back to GCS.

Nothing is uploaded until you explicitly click "Upload naar GCS" for a named
run folder — current/ and every existing run stay untouched (a bad
reallocation always has a fallback, exactly like the re-score explorer).

The ``import streamlit``/``plotly`` calls are deliberately lazy (inside
``main``) so the pure helper functions below can be imported and
unit-tested without Streamlit or Plotly installed.

Run with:
    streamlit run reallocate_callers_streamlit_app.py
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from caller_range_assignment import (
    CallerRange,
    RANGE_MODES,
    assign_callers_by_ranges,
    caller_ranges_coverage,
    even_count_ranges,
    resolve_range_bounds,
)
from reallocate_callers_from_gcs import (
    assign_callers,
    build_reallocated_run_from_assignment,
    caller_distribution,
    default_reallocate_run_folder,
    download_current_run,
    existing_cold_callers,
    list_country_folders,
    normalize_cold_callers,
    reallocation_movers,
)
from rescore_from_gcs import DEFAULT_GCS_BUCKET, promote_run_to_current


# =============================================================================
# Pure helpers — no Streamlit/Plotly import required
# =============================================================================


def parse_caller_input(text: str) -> list[str]:
    """Split a free-form caller box (comma- or newline-separated) into a
    clean, de-duplicated, order-preserving list — same normalization the
    reallocation core applies, so the UI preview matches the written run."""
    raw = (text or "").replace("\n", ",").split(",")
    return normalize_cold_callers(raw)


def validate_callers(callers: list[str]) -> "Optional[str]":
    """User-facing error when the caller pool is empty, else ``None``.
    Mirrors ``rescore_streamlit_app.validate_tier_thresholds``'s style."""
    if not callers:
        return (
            "Geef minstens één cold caller op — anders blijft elk bedrijf "
            "zonder toegewezen beller (de export-validatie weigert dat)."
        )
    return None


def caller_distribution_dataframe(
    original_list_items: list[dict], new_list_items: list[dict],
) -> pd.DataFrame:
    """Long-form ``(caller, when, count)`` table for a before/after workload
    bar chart. Every caller appearing on either side gets a row on both
    sides (0 where absent), so a caller who is dropped or newly added is
    still visible; blank/None callers show as "— (geen)"."""
    before = caller_distribution(original_list_items)
    after = caller_distribution(new_list_items)
    callers = list(dict.fromkeys([*before, *after]))
    rows = []
    for caller in callers:
        label = caller if caller else "— (geen)"
        rows.append({"caller": label, "when": "Huidig", "count": before.get(caller, 0)})
        rows.append({"caller": label, "when": "Nieuw", "count": after.get(caller, 0)})
    return pd.DataFrame(rows)


def movers_dataframe(
    original_list_items: list[dict], assignment: dict,
) -> pd.DataFrame:
    """Table of companies whose caller changed, for display."""
    return pd.DataFrame(reallocation_movers(original_list_items, assignment))


def default_range_settings(callers: list[str], total: int) -> dict:
    """Seed the range-mode editor with equal, contiguous ``"count"`` blocks
    (via ``even_count_ranges``) — a sane starting point an admin then adjusts
    per caller. Keyed by caller name; each value is the flat dict shape the
    Streamlit widgets read/write (``mode``, ``start``, ``end``,
    ``cohort_size``)."""
    settings: dict = {}
    for cr in even_count_ranges(callers, total):
        settings[cr.caller] = {
            "mode": "count", "start": cr.start, "end": cr.end, "cohort_size": 100,
        }
    return settings


def caller_ranges_from_settings(
    callers: list[str], settings: dict,
) -> list[CallerRange]:
    """Build the ordered ``CallerRange`` list the assignment/coverage
    functions expect, from the per-caller settings dict the Streamlit
    widgets maintain. Callers missing from ``settings`` (e.g. just added to
    the pool) are skipped rather than raising — they show up as gaps in the
    coverage check until the admin configures them."""
    ranges = []
    for caller in callers:
        cfg = settings.get(caller)
        if not cfg:
            continue
        ranges.append(CallerRange(
            caller=caller,
            mode=cfg["mode"],
            start=cfg["start"],
            end=cfg["end"],
            cohort_size=cfg.get("cohort_size") if cfg["mode"] == "cohort" else None,
        ))
    return ranges


# =============================================================================
# Streamlit UI — lazy imports so the helpers above stay testable without them
# =============================================================================


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import shutil
    import tempfile

    import plotly.express as px
    import streamlit as st

    st.set_page_config(
        page_title="Caller-reallocatie", page_icon="📞", layout="wide")
    st.title("📞 Cold-caller reallocatie")
    st.caption(
        "Herverdeel de cold callers over de bedrijven van één land — scores en "
        "tiers blijven ongemoeid. Er wordt niets naar GCS geschreven tot je "
        "expliciet uploadt; current/ en bestaande runs blijven onaangeroerd."
    )

    # ---------------------------------------------------------------------
    # Sidebar — GCS data source
    # ---------------------------------------------------------------------
    with st.sidebar:
        st.header("1. GCS-bron")
        bucket = st.text_input("Bucket", value=DEFAULT_GCS_BUCKET, key="bucket_input")

        if st.button("🔍 Landen ophalen"):
            with st.spinner("Bucket doorzoeken…"):
                st.session_state["_available_countries"] = list_country_folders(bucket)
            if not st.session_state.get("_available_countries"):
                st.warning(
                    "Geen land-folders gevonden. Is gcloud/gsutil geïnstalleerd "
                    "en ingelogd (`gcloud auth login`)?"
                )

        countries = st.session_state.get("_available_countries", [])
        if countries:
            country_folder = st.selectbox("Land-folder", options=countries, key="country_select")
        else:
            country_folder = st.text_input(
                "Land-folder (bv. brazil)", value="brazil", key="country_text")

        if st.button("📥 Huidige run laden", type="primary"):
            old_dir = st.session_state.get("_work_dir")
            if old_dir:
                shutil.rmtree(old_dir, ignore_errors=True)
            work_dir = tempfile.mkdtemp(prefix="reallocate_streamlit_")
            st.session_state["_work_dir"] = work_dir
            try:
                with st.spinner(f"{country_folder}/current/ downloaden…"):
                    current = download_current_run(bucket, country_folder, work_dir)
                st.session_state["_current"] = current
                st.session_state["_current_country"] = country_folder
                st.session_state["_current_bucket"] = bucket
                n_companies = len(current["list_items"])
                st.success(f"{n_companies} bedrijven geladen uit {country_folder}/current/.")
                # Seed the caller box with the run's existing pool.
                st.session_state["_caller_box"] = ", ".join(
                    existing_cold_callers(current["list_items"]))
            except Exception as exc:
                st.error(f"Laden mislukt: {exc}")

    current = st.session_state.get("_current")
    if not current:
        st.info("Laad eerst een land-folder via de zijbalk om te beginnen.")
        return

    country_folder = st.session_state["_current_country"]
    bucket = st.session_state["_current_bucket"]
    original_list_items = current["list_items"]

    # ---------------------------------------------------------------------
    # Caller pool editor
    # ---------------------------------------------------------------------
    st.subheader("2. Cold callers")
    current_callers = existing_cold_callers(original_list_items)
    st.caption(
        f"Huidige pool in {country_folder}/current/: "
        f"**{', '.join(current_callers) or '— (geen)'}** "
        f"({len(original_list_items)} bedrijven)."
    )
    caller_text = st.text_area(
        "Nieuwe caller-pool (komma- of regel-gescheiden)",
        value=st.session_state.get("_caller_box", ", ".join(current_callers)),
        key="_caller_box",
        help="De volgorde bepaalt de round-robin toewijzing op scorerang: "
             "rang 1 → eerste caller, rang 2 → tweede, enz. Dubbele namen "
             "worden genegeerd.",
    )
    new_callers = parse_caller_input(caller_text)
    rerank = st.checkbox(
        "Herrangschik op huidige commercial_fit_score",
        value=False, key="_rerank",
        help="Standaard blijft de export-tijd rang behouden. Zet dit aan om de "
             "rangorde opnieuw af te leiden uit de huidige score (bv. na een "
             "re-score), net zoals de export zelf sorteert.",
    )

    error = validate_callers(new_callers)
    if error:
        st.error(error)
        return

    st.write("Nieuwe pool:", " · ".join(f"`{c}`" for c in new_callers))

    # ---------------------------------------------------------------------
    # Toewijzingsmethode: round-robin, of expliciete ranges per caller
    # ---------------------------------------------------------------------
    st.subheader("3. Toewijzingsmethode")
    mode = st.radio(
        "Hoe worden bedrijven aan callers toegewezen?",
        options=["round_robin", "ranges"],
        format_func=lambda v: (
            "Round-robin (gelijk verdeeld over de scorerangorde)" if v == "round_robin"
            else "Ranges per caller (aantal / percentiel / cohort)"
        ),
        key="_assignment_mode",
        horizontal=True,
    )

    total_companies = len(original_list_items)
    now_iso = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if mode == "round_robin":
        assignment = assign_callers(original_list_items, new_callers, rerank_by_score=rerank)
    else:
        settings_key = f"_range_settings::{country_folder}"
        settings = st.session_state.get(settings_key)
        if not settings or set(settings) - set(new_callers):
            # Reseed when the caller pool changed (new/removed caller) so
            # every caller in the pool always has a starting range.
            settings = default_range_settings(new_callers, total_companies)
            st.session_state[settings_key] = settings

        st.caption(
            "Elke caller krijgt een expliciete range op de scorerangorde "
            "(rang 1 = hoogste score). Ranges kunnen per caller in een "
            "andere eenheid worden opgegeven; ze werken samen op dezelfde "
            "onderliggende rangorde. Bij overlap wint de laatste caller in "
            "de lijst hierboven."
        )
        for caller in new_callers:
            cfg = settings.setdefault(
                caller, {"mode": "count", "start": 1, "end": total_companies, "cohort_size": 100})
            with st.expander(f"Range voor **{caller}**", expanded=True):
                col_mode, col_range = st.columns([1, 3])
                range_mode = col_mode.selectbox(
                    "Eenheid", options=list(RANGE_MODES),
                    index=list(RANGE_MODES).index(cfg["mode"]),
                    format_func=lambda v: {
                        "count": "Aantal (rangnummer)",
                        "percentile": "Percentiel (%)",
                        "cohort": "Cohort",
                    }[v],
                    key=f"_range_mode::{country_folder}::{caller}",
                )
                cfg["mode"] = range_mode
                if range_mode == "count":
                    start, end = col_range.slider(
                        "Rangbereik", min_value=1, max_value=max(1, total_companies),
                        value=(int(cfg["start"]), int(min(cfg["end"], total_companies))),
                        key=f"_range_count::{country_folder}::{caller}",
                    )
                    cfg["start"], cfg["end"] = start, end
                elif range_mode == "percentile":
                    start, end = col_range.slider(
                        "Percentielbereik (%)", min_value=0.0, max_value=100.0,
                        value=(float(cfg["start"]), float(cfg["end"])), step=1.0,
                        key=f"_range_pct::{country_folder}::{caller}",
                    )
                    cfg["start"], cfg["end"] = start, end
                else:  # cohort
                    size_col, range_col = col_range.columns([1, 2])
                    cohort_size = size_col.number_input(
                        "Cohortgrootte", min_value=1, value=int(cfg.get("cohort_size") or 100),
                        key=f"_range_cohort_size::{country_folder}::{caller}",
                    )
                    cfg["cohort_size"] = cohort_size
                    max_cohort = max(1, math.ceil(total_companies / cohort_size))
                    start, end = range_col.slider(
                        "Cohortbereik", min_value=1, max_value=max_cohort,
                        value=(
                            min(int(cfg["start"]), max_cohort),
                            min(int(cfg["end"]), max_cohort),
                        ),
                        key=f"_range_cohort_idx::{country_folder}::{caller}",
                    )
                    cfg["start"], cfg["end"] = start, end
                bounds_preview = CallerRange(
                    caller=caller, mode=cfg["mode"], start=cfg["start"], end=cfg["end"],
                    cohort_size=cfg.get("cohort_size") if cfg["mode"] == "cohort" else None,
                )
                start_rank, end_rank = resolve_range_bounds(bounds_preview, total_companies)
                n_in_range = max(0, end_rank - start_rank + 1)
                st.caption(f"→ rang {start_rank}–{end_rank} ({n_in_range} bedrijven).")

        ranges = caller_ranges_from_settings(new_callers, settings)
        coverage = caller_ranges_coverage(ranges, total_companies)
        if coverage["gaps"]:
            st.warning(
                f"{len(coverage['gaps'])} bedrijven vallen buiten elke range "
                "(rang " + ", ".join(str(r) for r in coverage["gaps"][:10]) +
                (", …" if len(coverage["gaps"]) > 10 else "") +
                ") en blijven onbeheerd totdat een range ze dekt."
            )
        if coverage["overlaps"]:
            st.info(
                f"{len(coverage['overlaps'])} bedrijven vallen in meerdere ranges — "
                "de laatst genoemde caller hierboven wint per bedrijf."
            )
        assignment = assign_callers_by_ranges(
            original_list_items, ranges, rerank_by_score=rerank)

    # ---------------------------------------------------------------------
    # Live preview
    # ---------------------------------------------------------------------
    new_list_items = [
        {**it, **_moved(assignment, it)} for it in original_list_items
    ]

    movers_df = movers_dataframe(original_list_items, assignment)

    m1, m2, m3 = st.columns(3)
    m1.metric("Bedrijven totaal", len(original_list_items))
    m2.metric("Wisselen van caller", len(movers_df))
    m3.metric("Callers in pool", len(new_callers))

    st.subheader("Werkverdeling: huidig vs. nieuw")
    dist_df = caller_distribution_dataframe(original_list_items, new_list_items)
    st.plotly_chart(
        px.bar(dist_df, x="caller", y="count", color="when", barmode="group"),
        use_container_width=True,
    )

    st.subheader("Bedrijven die van caller wisselen")
    if movers_df.empty:
        st.info("Geen enkel bedrijf wisselt van caller met deze pool.")
    else:
        st.dataframe(movers_df, use_container_width=True, hide_index=True)

    # ---------------------------------------------------------------------
    # Apply & upload
    # ---------------------------------------------------------------------
    st.divider()
    st.subheader("4. Uploaden naar GCS")
    st.caption(
        "Schrijft naar een NIEUWE run-folder — current/ en bestaande runs "
        "blijven ongewijzigd. De live Company Hub ziet deze toewijzing pas na "
        "een aparte, expliciete 'current'-promotie."
    )
    run_folder = st.text_input(
        "Run-folder", value=default_reallocate_run_folder(), key="_run_folder")
    confirmed = st.checkbox(
        f"Ik begrijp dat dit naar gs://{bucket}/{country_folder}/runs/"
        f"{run_folder}/ schrijft (current/ blijft onaangeroerd).",
        key="_upload_confirmed",
    )
    if st.button("📤 Upload naar GCS", type="primary", disabled=not confirmed):
        from reallocate_callers_from_gcs import (
            upload_reallocated_run,
            write_reallocated_run,
        )
        try:
            reallocated_run = build_reallocated_run_from_assignment(
                current, assignment, new_callers, country_folder=country_folder,
                run_folder=run_folder, now_iso=now_iso, rerank_by_score=rerank,
            )
            out_dir = write_reallocated_run(
                reallocated_run, st.session_state["_work_dir"] + "/out")
            with st.spinner("Uploaden…"):
                results = upload_reallocated_run(
                    out_dir, bucket, country_folder, run_folder)
            n_failed = sum(1 for r in results if not r["success"])
            if n_failed:
                st.error(f"{n_failed} van {len(results)} uploads mislukt.")
            else:
                st.success(
                    f"{len(results)} bestanden geüpload naar "
                    f"gs://{bucket}/{country_folder}/runs/{run_folder}/"
                )
                st.session_state["_last_uploaded_run_folder"] = run_folder
            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f"Upload mislukt: {exc}")

    # ---------------------------------------------------------------------
    # Promote to current — the deliberate, separate step that makes a run
    # live in the Company Hub.
    # ---------------------------------------------------------------------
    st.divider()
    st.subheader("5. Promoveer naar current")
    st.caption(
        "De live Company Hub leest alleen uit current/. Zolang je hier niet "
        "expliciet promoveert, blijft de zojuist geüploade run onzichtbaar "
        "voor de Hub — precies zoals hierboven staat."
    )
    promote_run_folder = st.text_input(
        "Te promoveren run-folder",
        value=st.session_state.get("_last_uploaded_run_folder", run_folder),
        key="_promote_run_folder",
    )
    promote_confirmed = st.checkbox(
        f"Ik begrijp dat dit gs://{bucket}/{country_folder}/runs/"
        f"{promote_run_folder}/ overschrijft naar gs://{bucket}/{country_folder}/"
        f"current/ (de live Company Hub gaat dit direct tonen).",
        key="_promote_confirmed",
    )
    if st.button(
        "🚀 Promoveer naar current", type="primary", disabled=not promote_confirmed,
    ):
        try:
            with st.spinner(f"Promoveren van {promote_run_folder} naar current/…"):
                promote_result = promote_run_to_current(
                    bucket, country_folder, promote_run_folder)
            promote_results = promote_result["results"]
            n_failed = sum(1 for r in promote_results if not r["success"])
            if n_failed:
                st.error(f"{n_failed} van {len(promote_results)} bestanden niet gepromoveerd.")
            else:
                st.success(
                    f"{len(promote_results)} bestanden gepromoveerd naar "
                    f"gs://{bucket}/{country_folder}/current/ — de Company Hub "
                    "toont deze toewijzing nu direct."
                )
            st.dataframe(pd.DataFrame(promote_results), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f"Promoveren mislukt: {exc}")


def _moved(assignment: dict, item: dict) -> dict:
    """Overlay dict applying an assignment entry to a list item for the live
    preview (kept tiny so ``main`` reads cleanly)."""
    entry = assignment.get(str(item.get("company_id")))
    if entry is None:
        return {}
    caller, rank = entry
    return {"assigned_cold_caller": caller, "assigned_cold_caller_rank": rank}


if __name__ == "__main__":  # pragma: no cover
    main()
