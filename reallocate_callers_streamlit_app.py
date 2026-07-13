"""Interactive cold-caller reallocation UI for reallocate_callers_from_gcs.py.

Local Streamlit UI on top of reallocate_callers_from_gcs.py: pick a country
folder in the Lovable GCS bucket, load its current run, edit the cold-caller
pool, and see the resulting per-caller workload and the list of companies
that change caller update live, entirely in-memory, before anything is
written back to GCS.

Nothing is uploaded until you explicitly click "Upload to GCS" for a named
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
    assign_callers_round_robin_by_cohort_window,
    caller_ranges_coverage,
    even_count_ranges,
    resolve_cohort_window,
    resolve_range_bounds,
)
import gcs_python_backend
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
    resolve_gcs_tool,
)
from rescore_from_gcs import DEFAULT_GCS_BUCKET, promote_run_to_current

UNASSIGNED_LABEL = "— (none)"

# Fixed colors for the before/after workload chart so "Current" always reads
# as the muted/reference bar and "New" always reads as the highlighted one —
# a shared color per period, distinguished by axis position (caller), not by
# a same-hue split that's hard to tell apart at a glance.
CHART_COLOR_MAP = {"Current": "#94a3b8", "New": "#0284c7"}


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
            "Enter at least one cold caller — otherwise every company is "
            "left without an assigned caller (the export validator rejects "
            "that)."
        )
    return None


def caller_distribution_dataframe(
    original_list_items: list[dict], new_list_items: list[dict],
) -> pd.DataFrame:
    """Long-form ``(caller, period, count)`` table for a before/after
    workload bar chart. Every caller appearing on either side gets a row on
    both sides (0 where absent), so a caller who is dropped or newly added
    is still visible; blank/None callers show as ``UNASSIGNED_LABEL``.
    Callers are ordered by their new (post-reallocation) workload
    descending, so the chart reads left-to-right from busiest to quietest."""
    before = caller_distribution(original_list_items)
    after = caller_distribution(new_list_items)
    callers = list(dict.fromkeys([*before, *after]))
    callers.sort(key=lambda c: after.get(c, 0), reverse=True)
    rows = []
    for caller in callers:
        label = caller if caller else UNASSIGNED_LABEL
        rows.append({"caller": label, "period": "Current", "count": before.get(caller, 0)})
        rows.append({"caller": label, "period": "New", "count": after.get(caller, 0)})
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
        page_title="Caller reallocation", page_icon="📞", layout="wide")
    st.title("📞 Cold-caller reallocation")
    st.caption(
        "Redistribute the cold callers across one country's companies — "
        "scores and tiers are left untouched. Nothing is written to GCS "
        "until you explicitly upload; current/ and existing runs stay "
        "untouched."
    )

    # ---------------------------------------------------------------------
    # Sidebar — GCS data source
    # ---------------------------------------------------------------------
    with st.sidebar:
        st.header("1. GCS source")
        bucket = st.text_input("Bucket", value=DEFAULT_GCS_BUCKET, key="bucket_input")

        if st.button("🔍 Fetch countries"):
            with st.spinner("Scanning bucket…"):
                st.session_state["_available_countries"] = list_country_folders(bucket)
            if not st.session_state.get("_available_countries"):
                st.warning(
                    "No country folders found. Locally: is gcloud/gsutil "
                    "installed and authenticated (`gcloud auth login`)? On "
                    "Streamlit Cloud: add a `[gcp_service_account]` "
                    "service-account key to the app's Secrets — see "
                    "`.streamlit/secrets.toml.example`. The connection "
                    "status below shows what's missing."
                )

        with st.expander("🩺 GCS connection status"):
            _render_gcs_status(st)

        countries = st.session_state.get("_available_countries", [])
        if countries:
            country_folder = st.selectbox("Country folder", options=countries, key="country_select")
        else:
            country_folder = st.text_input(
                "Country folder (e.g. brazil)", value="brazil", key="country_text")

        if st.button("📥 Load current run", type="primary"):
            old_dir = st.session_state.get("_work_dir")
            if old_dir:
                shutil.rmtree(old_dir, ignore_errors=True)
            work_dir = tempfile.mkdtemp(prefix="reallocate_streamlit_")
            st.session_state["_work_dir"] = work_dir
            try:
                with st.spinner(f"Downloading {country_folder}/current/…"):
                    current = download_current_run(bucket, country_folder, work_dir)
                st.session_state["_current"] = current
                st.session_state["_current_country"] = country_folder
                st.session_state["_current_bucket"] = bucket
                n_companies = len(current["list_items"])
                st.success(f"Loaded {n_companies} companies from {country_folder}/current/.")
                # Seed the caller box with the run's existing pool.
                st.session_state["_caller_box"] = ", ".join(
                    existing_cold_callers(current["list_items"]))
            except Exception as exc:
                st.error(f"Load failed: {exc}")

    current = st.session_state.get("_current")
    if not current:
        st.info("Load a country folder from the sidebar to get started.")
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
        f"Current pool in {country_folder}/current/: "
        f"**{', '.join(current_callers) or UNASSIGNED_LABEL}** "
        f"({len(original_list_items)} companies)."
    )
    caller_text = st.text_area(
        "New caller pool (comma- or newline-separated)",
        value=st.session_state.get("_caller_box", ", ".join(current_callers)),
        key="_caller_box",
        help="The order determines the round-robin assignment by score "
             "rank: rank 1 → first caller, rank 2 → second, etc. Duplicate "
             "names are ignored.",
    )
    new_callers = parse_caller_input(caller_text)
    rerank = st.checkbox(
        "Re-rank by current commercial_fit_score",
        value=False, key="_rerank",
        help="By default the export-time rank is preserved. Enable this to "
             "re-derive the ranking from the current score (e.g. after a "
             "re-score), the same way the export itself sorts.",
    )

    error = validate_callers(new_callers)
    if error:
        st.error(error)
        return

    st.write("New pool:", " · ".join(f"`{c}`" for c in new_callers))

    # ---------------------------------------------------------------------
    # Assignment method: round-robin, or explicit ranges per caller
    # ---------------------------------------------------------------------
    st.subheader("3. Assignment method")
    mode = st.radio(
        "How are companies assigned to callers?",
        options=["round_robin", "ranges"],
        format_func=lambda v: (
            "Round-robin (evenly split across the score ranking)" if v == "round_robin"
            else "Ranges per caller (count / percentile / cohort)"
        ),
        key="_assignment_mode",
        horizontal=True,
    )

    total_companies = len(original_list_items)
    now_iso = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if mode == "round_robin":
        use_cohort_window = st.checkbox(
            "Restrict to a cohort window (release only part of the ranking)",
            value=False, key="_rr_use_cohort",
            help="Leave unchecked for a plain round-robin across every "
                 "company. When checked, only companies inside the chosen "
                 "cohort window are round-robin split across the caller "
                 "pool — the rest stay unassigned until a later window "
                 "covers them. A company's caller never changes as you "
                 "advance the window, since the round-robin formula still "
                 "keys off its global rank.",
        )
        if use_cohort_window:
            size_col, range_col = st.columns([1, 3])
            cohort_size = size_col.number_input(
                "Cohort size", min_value=1,
                value=int(st.session_state.get("_rr_cohort_size", 100)),
                key="_rr_cohort_size",
            )
            max_cohort = max(1, math.ceil(total_companies / cohort_size))
            prev_start, prev_end = st.session_state.get(
                "_rr_cohort_range", (1, max_cohort))
            cohort_start, cohort_end = range_col.slider(
                "Cohort range", min_value=1, max_value=max_cohort,
                value=(
                    min(int(prev_start), max_cohort),
                    min(int(prev_end), max_cohort),
                ),
                key="_rr_cohort_range",
            )
            start_rank, end_rank = resolve_cohort_window(
                cohort_size, cohort_start, cohort_end, total_companies)
            n_in_window = max(0, end_rank - start_rank + 1)
            st.caption(
                f"→ rank {start_rank}–{end_rank} ({n_in_window} of "
                f"{total_companies} companies released; the rest stay "
                "unassigned)."
            )
            assignment = assign_callers_round_robin_by_cohort_window(
                original_list_items, new_callers,
                cohort_size=cohort_size, cohort_start=cohort_start,
                cohort_end=cohort_end, rerank_by_score=rerank,
                unassigned_label=UNASSIGNED_LABEL,
            )
        else:
            assignment = assign_callers(
                original_list_items, new_callers, rerank_by_score=rerank)
    else:
        settings_key = f"_range_settings::{country_folder}"
        settings = st.session_state.get(settings_key)
        if not settings or set(settings) - set(new_callers):
            # Reseed when the caller pool changed (new/removed caller) so
            # every caller in the pool always has a starting range.
            settings = default_range_settings(new_callers, total_companies)
            st.session_state[settings_key] = settings

        st.caption(
            "Each caller gets an explicit range on the score ranking "
            "(rank 1 = highest score). Ranges can be specified per caller "
            "in a different unit; they all operate on the same underlying "
            "ranking. On overlap, the last caller in the list above wins."
        )

        top_n_key = f"_top_n::{country_folder}"
        col_topn, col_autosplit = st.columns([3, 1])
        top_n = col_topn.number_input(
            "Quick setup — top N to distribute (the rest is left unassigned "
            "and hidden from every caller)",
            min_value=1, max_value=total_companies,
            value=min(int(st.session_state.get(top_n_key, total_companies)), total_companies),
            key=top_n_key,
            help="E.g. with 3 callers and top N = 300, 'Auto-split evenly' "
                 "below gives each caller a contiguous block of 100 "
                 "(rank 1-100, 101-200, 201-300). Anyone ranked below 300 "
                 "falls outside every range and stays hidden from all "
                 "callers.",
        )
        col_autosplit.write("")  # vertical spacer to align the button with the input
        col_autosplit.write("")
        if col_autosplit.button(
            "🔀 Auto-split evenly",
            help="Overwrites every caller's range below with equal, "
                 "contiguous blocks over rank 1–{}.".format(top_n),
        ):
            settings = default_range_settings(new_callers, top_n)
            st.session_state[settings_key] = settings
            # The per-caller widgets below hold their own session_state under
            # these keys, which otherwise win over `settings` on rerun (a
            # widget's `value=`/`index=` is only honored on its first render)
            # — clear them so the sliders actually pick up the new split.
            for caller in new_callers:
                for widget_key in (
                    f"_range_mode::{country_folder}::{caller}",
                    f"_range_count::{country_folder}::{caller}",
                    f"_range_pct::{country_folder}::{caller}",
                    f"_range_cohort_size::{country_folder}::{caller}",
                    f"_range_cohort_idx::{country_folder}::{caller}",
                ):
                    st.session_state.pop(widget_key, None)
            st.rerun()

        for caller in new_callers:
            cfg = settings.setdefault(
                caller, {"mode": "count", "start": 1, "end": total_companies, "cohort_size": 100})
            with st.expander(f"Range for **{caller}**", expanded=True):
                col_mode, col_range = st.columns([1, 3])
                range_mode = col_mode.selectbox(
                    "Unit", options=list(RANGE_MODES),
                    index=list(RANGE_MODES).index(cfg["mode"]),
                    format_func=lambda v: {
                        "count": "Count (rank number)",
                        "percentile": "Percentile (%)",
                        "cohort": "Cohort",
                    }[v],
                    key=f"_range_mode::{country_folder}::{caller}",
                )
                cfg["mode"] = range_mode
                if range_mode == "count":
                    start, end = col_range.slider(
                        "Rank range", min_value=1, max_value=max(1, total_companies),
                        value=(int(cfg["start"]), int(min(cfg["end"], total_companies))),
                        key=f"_range_count::{country_folder}::{caller}",
                    )
                    cfg["start"], cfg["end"] = start, end
                elif range_mode == "percentile":
                    start, end = col_range.slider(
                        "Percentile range (%)", min_value=0.0, max_value=100.0,
                        value=(float(cfg["start"]), float(cfg["end"])), step=1.0,
                        key=f"_range_pct::{country_folder}::{caller}",
                    )
                    cfg["start"], cfg["end"] = start, end
                else:  # cohort
                    size_col, range_col = col_range.columns([1, 2])
                    cohort_size = size_col.number_input(
                        "Cohort size", min_value=1, value=int(cfg.get("cohort_size") or 100),
                        key=f"_range_cohort_size::{country_folder}::{caller}",
                    )
                    cfg["cohort_size"] = cohort_size
                    max_cohort = max(1, math.ceil(total_companies / cohort_size))
                    start, end = range_col.slider(
                        "Cohort range", min_value=1, max_value=max_cohort,
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
                st.caption(f"→ rank {start_rank}–{end_rank} ({n_in_range} companies).")

        ranges = caller_ranges_from_settings(new_callers, settings)
        coverage = caller_ranges_coverage(ranges, total_companies)
        if coverage["gaps"]:
            st.warning(
                f"{len(coverage['gaps'])} companies fall outside every range "
                "(rank " + ", ".join(str(r) for r in coverage["gaps"][:10]) +
                (", …" if len(coverage["gaps"]) > 10 else "") +
                ") and stay unmanaged until a range covers them."
            )
        if coverage["overlaps"]:
            st.info(
                f"{len(coverage['overlaps'])} companies fall inside more than "
                "one range — the last caller listed above wins per company."
            )
        assignment = assign_callers_by_ranges(
            original_list_items, ranges, rerank_by_score=rerank,
            unassigned_label=UNASSIGNED_LABEL,
        )

    # ---------------------------------------------------------------------
    # Live preview
    # ---------------------------------------------------------------------
    new_list_items = [
        {**it, **_moved(assignment, it)} for it in original_list_items
    ]

    movers_df = movers_dataframe(original_list_items, assignment)

    m1, m2, m3 = st.columns(3)
    m1.metric("Total companies", len(original_list_items))
    m2.metric("Changing caller", len(movers_df))
    m3.metric("Callers in pool", len(new_callers))

    st.subheader("Workload: current vs. new")
    dist_df = caller_distribution_dataframe(original_list_items, new_list_items)
    fig = px.bar(
        dist_df, x="caller", y="count", color="period", barmode="group",
        text="count",
        color_discrete_map=CHART_COLOR_MAP,
        category_orders={"caller": list(dict.fromkeys(dist_df["caller"])), "period": ["Current", "New"]},
        labels={"caller": "Caller", "count": "Companies", "period": "Period"},
    )
    fig.update_traces(textposition="outside", cliponaxis=False)
    fig.update_layout(legend_title_text="Period", yaxis_title="Companies", xaxis_title=None)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Companies changing caller")
    if movers_df.empty:
        st.info("No company changes caller with this pool.")
    else:
        st.dataframe(movers_df, use_container_width=True, hide_index=True)

    # ---------------------------------------------------------------------
    # Apply & upload
    # ---------------------------------------------------------------------
    st.divider()
    st.subheader("4. Upload to GCS")
    st.caption(
        "Writes to a NEW run folder — current/ and existing runs stay "
        "unchanged. The live Company Hub only sees this assignment after a "
        "separate, explicit 'current' promotion."
    )
    run_folder = st.text_input(
        "Run folder", value=default_reallocate_run_folder(), key="_run_folder")
    confirmed = st.checkbox(
        f"I understand this writes to gs://{bucket}/{country_folder}/runs/"
        f"{run_folder}/ (current/ stays untouched).",
        key="_upload_confirmed",
    )
    if st.button("📤 Upload to GCS", type="primary", disabled=not confirmed):
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
            with st.spinner("Uploading…"):
                results = upload_reallocated_run(
                    out_dir, bucket, country_folder, run_folder)
            n_failed = sum(1 for r in results if not r["success"])
            if n_failed:
                st.error(f"{n_failed} of {len(results)} uploads failed.")
            else:
                st.success(
                    f"{len(results)} files uploaded to "
                    f"gs://{bucket}/{country_folder}/runs/{run_folder}/"
                )
                st.session_state["_last_uploaded_run_folder"] = run_folder
            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f"Upload failed: {exc}")

    # ---------------------------------------------------------------------
    # Promote to current — the deliberate, separate step that makes a run
    # live in the Company Hub.
    # ---------------------------------------------------------------------
    st.divider()
    st.subheader("5. Promote to current")
    st.caption(
        "The live Company Hub only reads from current/. Until you "
        "explicitly promote here, the run you just uploaded stays invisible "
        "to the Hub — exactly as stated above."
    )
    promote_run_folder = st.text_input(
        "Run folder to promote",
        value=st.session_state.get("_last_uploaded_run_folder", run_folder),
        key="_promote_run_folder",
    )
    promote_confirmed = st.checkbox(
        f"I understand this overwrites gs://{bucket}/{country_folder}/runs/"
        f"{promote_run_folder}/ onto gs://{bucket}/{country_folder}/"
        f"current/ (the live Company Hub will show this immediately).",
        key="_promote_confirmed",
    )
    if st.button(
        "🚀 Promote to current", type="primary", disabled=not promote_confirmed,
    ):
        try:
            with st.spinner(f"Promoting {promote_run_folder} to current/…"):
                promote_result = promote_run_to_current(
                    bucket, country_folder, promote_run_folder)
            promote_results = promote_result["results"]
            n_failed = sum(1 for r in promote_results if not r["success"])
            if n_failed:
                st.error(f"{n_failed} of {len(promote_results)} files not promoted.")
            else:
                st.success(
                    f"{len(promote_results)} files promoted to "
                    f"gs://{bucket}/{country_folder}/current/ — the Company "
                    "Hub now shows this assignment immediately."
                )
            st.dataframe(pd.DataFrame(promote_results), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f"Promotion failed: {exc}")


def _render_gcs_status(st) -> None:  # pragma: no cover - pure Streamlit UI
    """Sidebar panel showing exactly which GCS access route is active and,
    when nothing works, which link in the chain is broken — a hosted app
    swallows listing errors into an empty country list, which is
    undebuggable without this."""
    tool = resolve_gcs_tool()
    if tool is not None:
        st.success(f"gcloud/gsutil CLI found (`{tool[0]}`) — the CLI handles "
                   "all GCS access; the checks below don't apply.")
        return
    st.info("No gcloud/gsutil CLI on PATH — the google-cloud-storage "
            "fallback is used (normal on Streamlit Cloud).")
    diag = gcs_python_backend.diagnostics()
    check = lambda ok: "✅" if ok else "❌"  # noqa: E731
    st.markdown(
        f"{check(diag['library_installed'])} `google-cloud-storage` library "
        "installed\n\n"
        f"{check(diag['secret_present'])} `[gcp_service_account]` present in "
        "Streamlit secrets\n\n"
        f"{check(diag['env_var_present'])} `GCP_SERVICE_ACCOUNT_JSON` "
        "environment variable set\n\n"
        f"{check(diag['client_ready'])} GCS client ready"
        + (f" as `{diag['service_account_email']}`"
           if diag["client_ready"] and diag["service_account_email"] else "")
    )
    if diag["last_error"]:
        st.caption("Most recent error:")
        st.code(diag["last_error"], language=None)
    elif not diag["client_ready"]:
        st.caption(
            "No credentials found yet: add the `[gcp_service_account]` block "
            "to the app's Secrets (see `.streamlit/secrets.toml.example`) "
            "and rerun."
        )


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
