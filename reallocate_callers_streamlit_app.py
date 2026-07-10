"""Caller Workload Manager — reallocate cold callers & set focus limits.

Management-facing Streamlit UI on top of reallocate_callers_from_gcs.py.
One page, one story, top to bottom:

1. Load a country's current run from the Lovable GCS bucket.
2. FOCUS — decide which companies are in play at all: everything, only the
   top N of the score ranking, or only companies inside a score range. Live
   feedback shows exactly how many companies match ("512 of 5,288 fall in
   score 6.0–10.0") plus a histogram with the selected band highlighted.
   Companies outside the focus get NO caller — they stay out of the calling
   lists (that is the "visibility limit" managers set here).
3. TEAM — edit the caller pool in a simple table.
4. DIVIDE — split the in-focus companies over the callers: contiguous
   blocks of the ranking (editable table with live company counts and score
   ranges per block, plus an allocation map), or a round-robin mix.
5. RESULT — per-caller summary (companies, score range, tier counts),
   before/after workload chart, and the list of companies changing caller.
6. PUBLISH — save as a draft run (never touches current/), then make it
   live for the calling team as a separate, deliberate step.

Nothing is uploaded until the explicit publish step; current/ and every
existing run stay untouched, so a bad reallocation always has a fallback.

The ``import streamlit``/``plotly`` calls are deliberately lazy (inside
``main``) so the pure helper functions below can be imported and
unit-tested without Streamlit or Plotly installed.

Run with:
    streamlit run reallocate_callers_streamlit_app.py
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from caller_range_assignment import even_count_ranges
from reallocate_callers_from_gcs import (
    build_reallocated_run_from_assignment,
    caller_distribution,
    compute_caller_ranks,
    default_reallocate_run_folder,
    download_current_run,
    existing_cold_callers,
    list_country_folders,
    normalize_cold_callers,
    reallocation_movers,
)
from rescore_from_gcs import DEFAULT_GCS_BUCKET, promote_run_to_current

UNASSIGNED_LABEL = "— (none)"
OUT_OF_FOCUS_LABEL = "out of focus"

FOCUS_MODES = ("all", "top_n", "score_band")

# Fixed colors for the before/after workload chart so "Current" always reads
# as the muted/reference bar and "New" as the highlighted one.
CHART_COLOR_MAP = {"Current": "#94a3b8", "New": "#0284c7"}

# Allocation-map colors: callers cycle through a qualitative palette;
# uncovered positions and the out-of-focus tail get fixed, muted colors so
# problems (gaps) and the visibility limit are instantly recognisable.
CALLER_COLOR_SEQUENCE = [
    "#0284c7", "#059669", "#d97706", "#7c3aed", "#db2777",
    "#0d9488", "#b45309", "#4f46e5", "#be185d", "#15803d",
]
GAP_COLOR = "#ef4444"
OUT_OF_FOCUS_COLOR = "#e2e8f0"


# =============================================================================
# Pure helpers — no Streamlit/Plotly import required
# =============================================================================


def validate_callers(callers: list[str]) -> "Optional[str]":
    """User-facing error when the caller pool is empty, else ``None``."""
    if not callers:
        return (
            "Add at least one caller — otherwise every company is left "
            "without an assigned caller."
        )
    return None


def score_of(item: dict) -> "float | None":
    """``commercial_fit_score`` as a float, or ``None`` when absent/invalid."""
    value = item.get("commercial_fit_score")
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def ranked_items(list_items: list[dict], *, rerank_by_score: bool = True) -> list[dict]:
    """List items ordered by rank (rank 1 first), using the exact same rank
    derivation the reallocation core writes back
    (``compute_caller_ranks``), so what the manager sees as "position 1" is
    what the Company Hub will show as rank 1."""
    ranks = compute_caller_ranks(list_items, rerank_by_score=rerank_by_score)
    return sorted(list_items, key=lambda it: ranks[str(it.get("company_id"))])


def tier_counts(items: list[dict]) -> dict:
    """``commercial_tier -> count`` over a set of items, blank tiers under ''."""
    counts: dict = {}
    for item in items:
        tier = str(item.get("commercial_tier") or "").strip()
        counts[tier] = counts.get(tier, 0) + 1
    return counts


def focus_selection(ranked: list[dict], focus: dict) -> tuple[list[str], dict]:
    """Apply the manager's focus limit to the ranked company list.

    ``focus`` is ``{"mode": "all"|"top_n"|"score_band", "top_n": int,
    "min_score": float, "max_score": float}`` (irrelevant keys ignored per
    mode). Returns ``(in_focus_ids, summary)`` where ``in_focus_ids`` keeps
    rank order and ``summary`` carries the live feedback the UI shows:
    ``n_total``, ``n_focus``, ``n_out``, ``best_score``, ``worst_score``
    (of the in-focus companies; ``None`` when no scores) and
    ``tier_counts``.

    ``top_n`` takes the first N of the ranking; ``score_band`` filters on
    the score VALUE (inclusive bounds), so it stays correct even when
    stored ranks and scores disagree (e.g. an un-reranked run after a
    re-score).
    """
    mode = focus.get("mode", "all")
    if mode not in FOCUS_MODES:
        raise ValueError(f"Unknown focus mode {mode!r}; must be one of {FOCUS_MODES}.")

    if mode == "all":
        selected = list(ranked)
    elif mode == "top_n":
        top_n = max(0, int(focus.get("top_n") or 0))
        selected = ranked[:top_n]
    else:  # score_band
        lo = float(focus.get("min_score", 0.0))
        hi = float(focus.get("max_score", 10.0))
        selected = [
            item for item in ranked
            if (score := score_of(item)) is not None and lo <= score <= hi
        ]

    scores = [s for item in selected if (s := score_of(item)) is not None]
    summary = {
        "n_total": len(ranked),
        "n_focus": len(selected),
        "n_out": len(ranked) - len(selected),
        "best_score": max(scores) if scores else None,
        "worst_score": min(scores) if scores else None,
        "tier_counts": tier_counts(selected),
    }
    return [str(item.get("company_id")) for item in selected], summary


def even_blocks(callers: list[str], n_focus: int) -> list[dict]:
    """Equal, contiguous blocks over focus positions ``1..n_focus`` in caller
    order — the seed for the editable blocks table. First callers get the
    best-scoring blocks (and any remainder)."""
    return [
        {"caller": cr.caller, "start": int(cr.start), "end": int(cr.end)}
        for cr in even_count_ranges(callers, n_focus)
    ]


def _compress_ranges(positions: list[int]) -> list[tuple[int, int]]:
    """Sorted ints -> inclusive ``(start, end)`` runs, e.g. [1,2,3,7] ->
    [(1,3), (7,7)]."""
    runs: list[tuple[int, int]] = []
    for pos in sorted(positions):
        if runs and pos == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], pos)
        else:
            runs.append((pos, pos))
    return runs


def format_position_ranges(ranges: list[tuple[int, int]], limit: int = 4) -> str:
    """Human-readable "1–250, 300–310, …" for coverage messages."""
    parts = [
        (f"{a}–{b}" if a != b else str(a)) for a, b in ranges[:limit]
    ]
    if len(ranges) > limit:
        parts.append("…")
    return ", ".join(parts)


def blocks_coverage(blocks: list[dict], n_focus: int) -> dict:
    """Coverage diagnostic over focus positions ``1..n_focus``: which
    positions no block covers (``gap_ranges``) and which are claimed by more
    than one block (``overlap_ranges``), both as compressed inclusive runs
    plus total counts. Positions outside ``1..n_focus`` in a block are
    ignored (blocks are clamped at evaluation time)."""
    claimed: dict[int, int] = {}
    for block in blocks:
        start = max(1, int(block["start"]))
        end = min(n_focus, int(block["end"]))
        for pos in range(start, end + 1):
            claimed[pos] = claimed.get(pos, 0) + 1
    gaps = [pos for pos in range(1, n_focus + 1) if pos not in claimed]
    overlaps = [pos for pos, count in claimed.items() if count > 1]
    return {
        "gap_ranges": _compress_ranges(gaps),
        "n_gaps": len(gaps),
        "overlap_ranges": _compress_ranges(overlaps),
        "n_overlaps": len(overlaps),
    }


def assignment_from_blocks(
    ranked: list[dict], in_focus_ids: list[str], blocks: list[dict],
    *, unassigned_label: str = UNASSIGNED_LABEL,
) -> dict:
    """``company_id -> (caller, rank)`` from contiguous blocks over the
    in-focus sequence (position 1 = best in-focus company). Out-of-focus
    companies and uncovered positions get ``unassigned_label``. When blocks
    overlap, the LAST block in the list wins — same override semantics as
    the range core. Ranks are the company's position in the full ranking,
    so the Company Hub ordering stays intact."""
    focus_pos = {cid: pos for pos, cid in enumerate(in_focus_ids, start=1)}
    resolved = [
        (str(block["caller"]), int(block["start"]), int(block["end"]))
        for block in blocks
    ]
    assignment: dict = {}
    for rank, item in enumerate(ranked, start=1):
        cid = str(item.get("company_id"))
        caller = unassigned_label
        pos = focus_pos.get(cid)
        if pos is not None:
            for block_caller, start, end in resolved:
                if start <= pos <= end:
                    caller = block_caller
        assignment[cid] = (caller, rank)
    return assignment


def assignment_interleaved(
    ranked: list[dict], in_focus_ids: list[str], callers: list[str],
    *, unassigned_label: str = UNASSIGNED_LABEL,
) -> dict:
    """``company_id -> (caller, rank)`` alternating the in-focus companies
    over the callers in rank order (position 1 -> first caller, position 2
    -> second, …) so every caller gets a comparable mix of high and low
    scores. Out-of-focus companies get ``unassigned_label``. With the focus
    set to "all companies" this reproduces the export pipeline's own
    round-robin formula exactly."""
    if not callers:
        raise ValueError("At least one caller is required.")
    focus_pos = {cid: pos for pos, cid in enumerate(in_focus_ids, start=1)}
    assignment: dict = {}
    for rank, item in enumerate(ranked, start=1):
        cid = str(item.get("company_id"))
        pos = focus_pos.get(cid)
        caller = callers[(pos - 1) % len(callers)] if pos is not None else unassigned_label
        assignment[cid] = (caller, rank)
    return assignment


def blocks_with_feedback(
    blocks: list[dict], in_focus_items: list[dict], n_focus: int,
) -> list[dict]:
    """The blocks enriched with the live feedback columns the editor shows:
    ``companies`` (count actually covered after clamping to the focus),
    ``best_score`` and ``worst_score`` of the covered positions. Overlaps
    are not subtracted here — this answers "what does THIS block span?";
    overlap resolution is reported separately by ``blocks_coverage``."""
    enriched = []
    for block in blocks:
        start = max(1, int(block["start"]))
        end = min(n_focus, int(block["end"]))
        covered = in_focus_items[start - 1:end] if start <= end else []
        scores = [s for item in covered if (s := score_of(item)) is not None]
        enriched.append({
            **block,
            "companies": len(covered),
            "best_score": max(scores) if scores else None,
            "worst_score": min(scores) if scores else None,
        })
    return enriched


def allocation_map_segments(
    blocks: list[dict], n_focus: int, n_total: int,
    *, unassigned_label: str = UNASSIGNED_LABEL,
) -> list[dict]:
    """Run-length segments for the horizontal allocation map: consecutive
    focus positions with the same owner (last block wins), gaps labelled
    ``unassigned_label``, plus one trailing ``OUT_OF_FOCUS_LABEL`` segment
    for the companies the focus limit excludes. Returns
    ``[{"label", "count"}, ...]`` in ranking order."""
    owner_by_pos: dict[int, str] = {}
    for block in blocks:
        start = max(1, int(block["start"]))
        end = min(n_focus, int(block["end"]))
        for pos in range(start, end + 1):
            owner_by_pos[pos] = str(block["caller"])

    segments: list[dict] = []
    for pos in range(1, n_focus + 1):
        label = owner_by_pos.get(pos, unassigned_label)
        if segments and segments[-1]["label"] == label:
            segments[-1]["count"] += 1
        else:
            segments.append({"label": label, "count": 1})
    if n_total > n_focus:
        segments.append({"label": OUT_OF_FOCUS_LABEL, "count": n_total - n_focus})
    return segments


def per_caller_summary_dataframe(
    ranked: list[dict], assignment: dict,
    *, unassigned_label: str = UNASSIGNED_LABEL,
) -> pd.DataFrame:
    """One row per caller with the numbers a manager checks before
    publishing: companies, best/lowest score, and per-tier counts. Callers
    sorted by their best score (best block first); the unassigned bucket —
    the companies the focus limit hides — is always the last row."""
    by_caller: dict[str, list[dict]] = {}
    for item in ranked:
        entry = assignment.get(str(item.get("company_id")))
        if entry is None:
            continue
        by_caller.setdefault(entry[0], []).append(item)

    rows = []
    for caller, items in by_caller.items():
        scores = [s for item in items if (s := score_of(item)) is not None]
        tiers = tier_counts(items)
        row = {
            "caller": caller,
            "companies": len(items),
            "best_score": max(scores) if scores else None,
            "worst_score": min(scores) if scores else None,
        }
        for tier, count in sorted(tiers.items()):
            if tier:
                row[tier] = count
        rows.append(row)
    rows.sort(key=lambda r: (
        r["caller"] == unassigned_label,
        -(r["best_score"] if r["best_score"] is not None else float("-inf")),
        r["caller"],
    ))
    df = pd.DataFrame(rows)
    if not df.empty:
        tier_cols = [c for c in df.columns if c not in
                     ("caller", "companies", "best_score", "worst_score")]
        df[tier_cols] = df[tier_cols].fillna(0).astype(int)
    return df


def caller_distribution_dataframe(
    original_list_items: list[dict], new_list_items: list[dict],
) -> pd.DataFrame:
    """Long-form ``(caller, period, count)`` table for the before/after
    workload bar chart. Every caller appearing on either side gets a row on
    both sides (0 where absent); blank/None callers show as
    ``UNASSIGNED_LABEL``. Ordered by new workload descending."""
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


# =============================================================================
# Streamlit UI — lazy imports so the helpers above stay testable without them
# =============================================================================


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import shutil
    import tempfile
    import time

    import plotly.graph_objects as go
    import plotly.express as px
    import streamlit as st

    st.set_page_config(
        page_title="Caller Workload Manager", page_icon="📞", layout="wide")
    st.title("📞 Caller Workload Manager")
    st.caption(
        "Decide **which companies are in play** (the focus limit) and **who "
        "calls what**. Scores and tiers never change here. Nothing goes "
        "live until you explicitly publish in step 6 — drafts never touch "
        "the live data."
    )

    # ---------------------------------------------------------------------
    # Sidebar — load a country
    # ---------------------------------------------------------------------
    with st.sidebar:
        st.header("1 · Country")
        with st.expander("Advanced"):
            bucket = st.text_input("Bucket", value=DEFAULT_GCS_BUCKET, key="bucket_input")
            rerank = st.checkbox(
                "Rank by current score (recommended)",
                value=True, key="_rerank",
                help="Re-derives the ranking from each company's current "
                     "commercial_fit_score, so position 1 is always today's "
                     "best-scoring company — correct even after a re-score. "
                     "Turn off only to keep the original export-time order.",
            )

        if st.button("🔍 Fetch countries"):
            with st.spinner("Scanning bucket…"):
                st.session_state["_available_countries"] = list_country_folders(bucket)
            if not st.session_state.get("_available_countries"):
                st.warning(
                    "No country folders found. Is gcloud/gsutil installed "
                    "and authenticated (`gcloud auth login`)?"
                )

        countries = st.session_state.get("_available_countries", [])
        if countries:
            country_folder = st.selectbox("Country", options=countries, key="country_select")
        else:
            country_folder = st.text_input(
                "Country folder (e.g. brazil)", value="brazil", key="country_text")

        if st.button("📥 Load country", type="primary"):
            old_dir = st.session_state.get("_work_dir")
            if old_dir:
                shutil.rmtree(old_dir, ignore_errors=True)
            work_dir = tempfile.mkdtemp(prefix="reallocate_streamlit_")
            st.session_state["_work_dir"] = work_dir
            try:
                _t0 = time.monotonic()
                with st.spinner(f"Downloading {country_folder}…"):
                    current = download_current_run(bucket, country_folder, work_dir)
                st.session_state["_current"] = current
                st.session_state["_current_country"] = country_folder
                st.session_state["_current_bucket"] = bucket
                st.success(
                    f"{len(current['list_items']):,} companies loaded "
                    f"({time.monotonic() - _t0:.1f}s)."
                )
            except Exception as exc:
                st.error(f"Load failed: {exc}")

    current = st.session_state.get("_current")
    if not current:
        st.info(
            "**Start by loading a country from the sidebar.** Then this page "
            "walks you through three decisions:\n\n"
            "1. **Focus** — which companies are in play (all of them, the "
            "top N, or a score range). Everything outside the focus stays "
            "out of the calling lists.\n"
            "2. **Team & split** — who is calling, and which slice of the "
            "ranking each caller owns.\n"
            "3. **Publish** — save a draft, check the numbers, then make it "
            "live in one deliberate click."
        )
        return

    country_folder = st.session_state["_current_country"]
    bucket = st.session_state["_current_bucket"]
    original_list_items = current["list_items"]

    ranked = ranked_items(original_list_items, rerank_by_score=rerank)
    items_by_id = {str(item.get("company_id")): item for item in ranked}
    n_total = len(ranked)
    all_scores = [s for item in ranked if (s := score_of(item)) is not None]

    # ---------------------------------------------------------------------
    # 2 · Focus — which companies are in play?
    # ---------------------------------------------------------------------
    st.subheader("2 · Focus — which companies are in play?")
    focus_mode = st.radio(
        "Focus",
        options=list(FOCUS_MODES),
        format_func=lambda v: {
            "all": "All companies",
            "top_n": "Only the top N",
            "score_band": "Only a score range",
        }[v],
        horizontal=True, label_visibility="collapsed", key="_focus_mode",
    )

    focus: dict = {"mode": focus_mode}
    if focus_mode == "top_n":
        focus["top_n"] = int(st.number_input(
            "How many companies (from the top of the ranking)?",
            min_value=1, max_value=max(1, n_total),
            value=min(500, n_total), step=50, key="_focus_top_n",
        ))
    elif focus_mode == "score_band":
        lo_bound = float(min(all_scores)) if all_scores else 0.0
        hi_bound = float(max(all_scores)) if all_scores else 10.0
        band = st.slider(
            "Only companies with a score between…",
            min_value=0.0, max_value=10.0,
            value=(max(0.0, round(lo_bound, 1)), min(10.0, round(hi_bound, 1))),
            step=0.1, key="_focus_band",
        )
        focus["min_score"], focus["max_score"] = band

    in_focus_ids, focus_summary = focus_selection(ranked, focus)
    n_focus = focus_summary["n_focus"]
    in_focus_items = [items_by_id[cid] for cid in in_focus_ids]

    f1, f2, f3 = st.columns(3)
    f1.metric("In play", f"{n_focus:,} of {n_total:,}")
    f2.metric(
        "Score range in play",
        (f"{focus_summary['best_score']:.2f} – {focus_summary['worst_score']:.2f}"
         if focus_summary["best_score"] is not None else "—"),
    )
    f3.metric("Hidden (no caller)", f"{focus_summary['n_out']:,}")
    _tier_parts = [
        f"{count} {tier}" for tier, count in sorted(
            focus_summary["tier_counts"].items(), reverse=True) if tier
    ]
    st.caption(
        ("Tiers in play: " + " · ".join(_tier_parts) + ". " if _tier_parts else "")
        + "Companies outside the focus keep all their data but get no "
          "caller — they disappear from the calling lists until you widen "
          "the focus again."
    )

    if all_scores:
        hist = px.histogram(
            pd.DataFrame({"score": all_scores}), x="score", nbins=40,
            labels={"score": "commercial_fit_score"}, height=230,
        )
        hist.update_traces(marker_color="#94a3b8")
        if focus_mode == "score_band":
            hist.add_vrect(
                x0=focus["min_score"], x1=focus["max_score"],
                fillcolor="#0284c7", opacity=0.15, line_width=0,
            )
        elif focus_mode == "top_n" and focus_summary["worst_score"] is not None:
            hist.add_vline(
                x=focus_summary["worst_score"], line_color="#0284c7",
                line_dash="dash",
                annotation_text=f"top {focus['top_n']} cutoff",
            )
        hist.update_layout(
            margin=dict(l=10, r=10, t=10, b=10), yaxis_title="companies")
        st.plotly_chart(hist, use_container_width=True, key="focus_histogram")

    # ---------------------------------------------------------------------
    # 3 · Team — who is calling?
    # ---------------------------------------------------------------------
    st.subheader("3 · Team — who is calling?")
    pool_key = f"_pool_seed::{country_folder}"
    if pool_key not in st.session_state:
        st.session_state[pool_key] = existing_cold_callers(original_list_items)
    seed_callers = st.session_state[pool_key]

    pool_df = st.data_editor(
        pd.DataFrame({"caller": seed_callers or [""]}),
        num_rows="dynamic", use_container_width=False, width=420,
        column_config={"caller": st.column_config.TextColumn(
            "Caller", help="One row per caller. Add or delete rows freely.")},
        key=f"_pool_editor::{country_folder}", hide_index=True,
    )
    new_callers = normalize_cold_callers(list(pool_df["caller"].fillna(""))) \
        if not pool_df.empty else []
    st.caption(
        "Order matters: the **first caller gets the best-scoring block** "
        "(blocks) or the first pick of each round (mixed)."
    )
    error = validate_callers(new_callers)
    if error:
        st.error(error)
        return

    # ---------------------------------------------------------------------
    # 4 · Divide the work
    # ---------------------------------------------------------------------
    st.subheader("4 · Divide the work")
    split_mode = st.radio(
        "Split",
        options=["blocks", "mixed"],
        format_func=lambda v: (
            "Blocks — each caller owns a contiguous slice of the ranking"
            if v == "blocks"
            else "Mixed — alternate companies so every caller gets a similar "
                 "mix of high and low scores"
        ),
        horizontal=True, label_visibility="collapsed", key="_split_mode",
    )

    if split_mode == "mixed":
        assignment = assignment_interleaved(ranked, in_focus_ids, new_callers)
        per_head = [
            n_focus // len(new_callers) + (1 if i < n_focus % len(new_callers) else 0)
            for i in range(len(new_callers))
        ]
        st.caption(
            f"Each of the {len(new_callers)} callers gets "
            f"{min(per_head)}–{max(per_head)} companies, spread evenly over "
            "the whole score range in play."
        )
        blocks = None
    else:
        blocks_state_key = f"_blocks::{country_folder}"
        blocks_sig_key = f"_blocks_sig::{country_folder}"
        blocks_ver_key = f"_blocks_ver::{country_folder}"
        signature = (tuple(new_callers), n_focus)
        if st.session_state.get(blocks_sig_key) != signature:
            st.session_state[blocks_state_key] = even_blocks(new_callers, n_focus)
            st.session_state[blocks_sig_key] = signature
            st.session_state[blocks_ver_key] = st.session_state.get(blocks_ver_key, 0) + 1

        top_row = st.columns([1, 3])
        if top_row[0].button("↔️ Split evenly", key="_split_evenly_btn",
                             help="Reset the table to equal blocks."):
            st.session_state[blocks_state_key] = even_blocks(new_callers, n_focus)
            st.session_state[blocks_ver_key] = st.session_state.get(blocks_ver_key, 0) + 1
            st.rerun()
        top_row[1].caption(
            "Edit **From / To** (positions in the in-play ranking, 1 = best "
            "company). The other columns update live. Changing the team or "
            "the focus re-seeds the table to an even split."
        )

        seeded = blocks_with_feedback(
            st.session_state[blocks_state_key], in_focus_items, n_focus)
        editor_df = st.data_editor(
            pd.DataFrame(seeded, columns=[
                "caller", "start", "end", "companies", "best_score", "worst_score"]),
            column_config={
                "caller": st.column_config.TextColumn("Caller", disabled=True),
                "start": st.column_config.NumberColumn(
                    "From (position)", min_value=1, max_value=max(1, n_focus), step=1),
                "end": st.column_config.NumberColumn(
                    "To (position)", min_value=1, max_value=max(1, n_focus), step=1),
                "companies": st.column_config.NumberColumn("Companies", disabled=True),
                "best_score": st.column_config.NumberColumn(
                    "Best score", disabled=True, format="%.2f"),
                "worst_score": st.column_config.NumberColumn(
                    "Lowest score", disabled=True, format="%.2f"),
            },
            hide_index=True, use_container_width=True,
            key=f"_blocks_editor::{country_folder}::{st.session_state[blocks_ver_key]}",
        )
        blocks = [
            {"caller": row["caller"],
             "start": int(row["start"] if pd.notna(row["start"]) else 1),
             "end": int(row["end"] if pd.notna(row["end"]) else n_focus)}
            for _, row in editor_df.iterrows()
        ]
        st.session_state[blocks_state_key] = blocks

        coverage = blocks_coverage(blocks, n_focus)
        if coverage["n_gaps"]:
            st.warning(
                f"**{coverage['n_gaps']:,} companies in play have no caller "
                f"yet** (positions {format_position_ranges(coverage['gap_ranges'])}). "
                "They stay out of the calling lists unless a block covers them."
            )
        if coverage["n_overlaps"]:
            st.info(
                f"{coverage['n_overlaps']:,} positions are claimed by more "
                f"than one block ({format_position_ranges(coverage['overlap_ranges'])}) "
                "— the block lowest in the table wins."
            )

        assignment = assignment_from_blocks(ranked, in_focus_ids, blocks)

        # Allocation map — the whole ranking as one bar, split by owner.
        segments = allocation_map_segments(blocks, n_focus, n_total)
        color_by_caller = {
            caller: CALLER_COLOR_SEQUENCE[i % len(CALLER_COLOR_SEQUENCE)]
            for i, caller in enumerate(new_callers)
        }
        fig_map = go.Figure()
        seen_labels: set = set()
        for segment in segments:
            label = segment["label"]
            color = color_by_caller.get(
                label,
                OUT_OF_FOCUS_COLOR if label == OUT_OF_FOCUS_LABEL else GAP_COLOR,
            )
            fig_map.add_trace(go.Bar(
                x=[segment["count"]], y=["ranking"], orientation="h",
                name=label, marker_color=color,
                showlegend=label not in seen_labels,
                text=label if segment["count"] >= n_total * 0.06 else None,
                textposition="inside", insidetextanchor="middle",
            ))
            seen_labels.add(label)
        fig_map.update_layout(
            barmode="stack", height=110,
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title=f"ranking position (1 = best of {n_total:,})",
            yaxis_visible=False, legend_orientation="h",
        )
        st.plotly_chart(fig_map, use_container_width=True, key="allocation_map")

    # ---------------------------------------------------------------------
    # 5 · Check the result
    # ---------------------------------------------------------------------
    st.subheader("5 · Check the result")
    new_list_items = [
        {**item, **_assigned(assignment, item)} for item in original_list_items
    ]
    movers_df = movers_dataframe(original_list_items, assignment)

    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Companies", f"{n_total:,}")
    r2.metric("With a caller", f"{sum(1 for c, _ in assignment.values() if c != UNASSIGNED_LABEL):,}")
    r3.metric("Without (hidden)", f"{sum(1 for c, _ in assignment.values() if c == UNASSIGNED_LABEL):,}")
    r4.metric("Changing caller", f"{len(movers_df):,}")

    summary_df = per_caller_summary_dataframe(ranked, assignment)
    if not summary_df.empty:
        st.dataframe(
            summary_df, use_container_width=True, hide_index=True,
            column_config={
                "caller": "Caller",
                "companies": "Companies",
                "best_score": st.column_config.NumberColumn("Best score", format="%.2f"),
                "worst_score": st.column_config.NumberColumn("Lowest score", format="%.2f"),
            },
        )

    dist_df = caller_distribution_dataframe(original_list_items, new_list_items)
    fig = px.bar(
        dist_df, x="caller", y="count", color="period", barmode="group",
        text="count", color_discrete_map=CHART_COLOR_MAP,
        category_orders={
            "caller": list(dict.fromkeys(dist_df["caller"])),
            "period": ["Current", "New"],
        },
        labels={"caller": "Caller", "count": "Companies", "period": ""},
        height=320,
    )
    fig.update_traces(textposition="outside", cliponaxis=False)
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True, key="workload_chart")

    with st.expander(f"Companies changing caller ({len(movers_df):,})"):
        if movers_df.empty:
            st.info("No company changes caller with these settings.")
        else:
            st.dataframe(movers_df, use_container_width=True, hide_index=True)

    # ---------------------------------------------------------------------
    # 6 · Publish
    # ---------------------------------------------------------------------
    st.divider()
    st.subheader("6 · Publish")
    st.caption(
        "Two deliberate steps. **A** saves a draft in the cloud — the "
        "calling team sees nothing yet. **B** makes a draft live. The "
        "previous live version always stays available as a fallback."
    )
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**A · Save draft**")
        run_folder = st.text_input(
            "Draft name", value=default_reallocate_run_folder(), key="_run_folder")
        confirmed = st.checkbox(
            "Save this allocation as a draft (nothing goes live).",
            key="_upload_confirmed",
        )
        if st.button("💾 Save draft", type="primary", disabled=not confirmed):
            from reallocate_callers_from_gcs import (
                upload_reallocated_run,
                write_reallocated_run,
            )
            try:
                now_iso = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
                reallocated_run = build_reallocated_run_from_assignment(
                    current, assignment, new_callers,
                    country_folder=country_folder, run_folder=run_folder,
                    now_iso=now_iso, rerank_by_score=rerank,
                )
                out_dir = write_reallocated_run(
                    reallocated_run, st.session_state["_work_dir"] + "/out")
                with st.spinner("Saving draft…"):
                    results = upload_reallocated_run(
                        out_dir, bucket, country_folder, run_folder)
                n_failed = sum(1 for r in results if not r["success"])
                if n_failed:
                    st.error(f"{n_failed} of {len(results)} files failed to save.")
                else:
                    st.success(f"Draft **{run_folder}** saved ({len(results)} files).")
                    st.session_state["_last_uploaded_run_folder"] = run_folder
            except Exception as exc:
                st.error(f"Saving failed: {exc}")

    with col_b:
        st.markdown("**B · Make live**")
        promote_run_folder = st.text_input(
            "Draft to make live",
            value=st.session_state.get("_last_uploaded_run_folder", ""),
            key="_promote_run_folder",
        )
        promote_confirmed = st.checkbox(
            f"Make draft '{promote_run_folder or '…'}' live for the calling "
            f"team in {country_folder} — they see it immediately.",
            key="_promote_confirmed", disabled=not promote_run_folder,
        )
        if st.button(
            "🚀 Make live", type="primary",
            disabled=not (promote_confirmed and promote_run_folder),
        ):
            try:
                with st.spinner(f"Publishing {promote_run_folder}…"):
                    promote_result = promote_run_to_current(
                        bucket, country_folder, promote_run_folder)
                promote_results = promote_result["results"]
                n_failed = sum(1 for r in promote_results if not r["success"])
                if n_failed:
                    st.error(f"{n_failed} of {len(promote_results)} files not published.")
                else:
                    st.success(
                        f"**{promote_run_folder}** is live — the calling team "
                        f"now sees this allocation for {country_folder}."
                    )
            except Exception as exc:
                st.error(f"Publishing failed: {exc}")


def _assigned(assignment: dict, item: dict) -> dict:
    """Overlay dict applying an assignment entry to a list item for the live
    preview (kept tiny so ``main`` reads cleanly)."""
    entry = assignment.get(str(item.get("company_id")))
    if entry is None:
        return {}
    caller, rank = entry
    return {"assigned_cold_caller": caller, "assigned_cold_caller_rank": rank}


if __name__ == "__main__":  # pragma: no cover
    main()
