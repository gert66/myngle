"""Interactive scoring-parameter explorer for rescore_from_gcs.py.

Local Streamlit UI on top of rescore_from_gcs.py / commercial_fit_scoring.py:
pick a country folder in the Lovable GCS bucket, load its current run, and
tweak the logistic-regression coefficients, the sigmoid steepness (K), the
ICP-vs-company-size blend weights, and the tier thresholds with sliders —
seeing the resulting score distribution/tier mix update live, entirely
in-memory, before anything is written back to GCS.

Nothing is uploaded until you explicitly click "Upload naar GCS" for a named
run folder — current/ and every existing run stay untouched (see
rescore_from_gcs.rescore_country's docstring).

The ``import streamlit``/``plotly`` calls are deliberately lazy (inside
``main``) so the pure helper functions below can be imported and
unit-tested without Streamlit or Plotly installed.

Run with:
    streamlit run rescore_streamlit_app.py
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from commercial_fit_scoring import (
    COMPANY_SIZE_WEIGHT,
    ICP_SIMILARITY_WEIGHT,
    INTERCEPT,
    LEAN_COEFFICIENTS,
    SIGMOID_K,
    SIZE_BAND_LOOKUP,
    TIER_THRESHOLDS,
    _FIELD_TO_COMPONENT,
    _SIGMOID_P_HI,
    _SIGMOID_P_LO,
    score_company,
)
from rescore_from_gcs import (
    DEFAULT_GCS_BUCKET,
    build_rescored_run,
    default_rescore_run_folder,
    download_current_run,
    list_country_folders,
    rescore_details_bucket,
    tier_distribution,
    upload_rescored_run,
    write_rescored_run,
)

# Business-meaning captions for each coefficient — straight from the
# Results(8).xlsx "Lean Coefficients" reference sheet — shown next to each
# slider so tweaking the model doesn't require reading commercial_fit_scoring.py.
COEFFICIENT_LABELS: dict[str, str] = {
    "sig_foreign_hq_score":
        "Foreign HQ detected — strongest predictor. International HQ implies "
        "a cross-border workforce.",
    "sig_explicit_lnd_score":
        "Explicit L&D content — the company visibly invests in learning & "
        "development.",
    "sig_intl_footprint_score":
        "International offices/operations spread across multiple countries.",
    "sig_employer_branding_score":
        "Employer branding signal. Note: this is NOT sig_merger_acq_score — "
        "see commercial_fit_scoring.LEAN_COEFFICIENTS for why.",
    "sig_lnd_onboarding_score":
        "Combined L&D + onboarding signal.",
    "ti_onboarding_score":
        "Topic intent: onboarding content engagement.",
    "sig_rapid_growth_score":
        "Rapid growth / startup signal — active disqualifier (negative "
        "coefficient: higher growth score pulls the fit score DOWN).",
}

TIER_LABELS: list[str] = ["🥇 Hot", "🥈 Warm", "🥉 Cool", "❄️ Pass"]

# Consistent order + colours for the huidig/nieuw comparison across every
# chart. Distinct hues (not two shades of one colour) so that even when the
# current and re-scored distributions land on the same bar they stay
# tellable apart — the earlier overlay/two-blues styling made "Huidig"
# disappear behind "Nieuw".
WHEN_ORDER: list[str] = ["Huidig", "Nieuw"]
WHEN_COLORS: dict[str, str] = {"Huidig": "#8c8c8c", "Nieuw": "#1f77b4"}


# =============================================================================
# Pure helpers — no Streamlit/Plotly import required
# =============================================================================


def default_params() -> dict:
    """Baseline params dict mirroring commercial_fit_scoring's module-level
    defaults (``SCORING_PROFILES['default']``) — the UI's starting point
    before any slider is touched."""
    return {
        "intercept": INTERCEPT,
        "coefficients": dict(LEAN_COEFFICIENTS),
        "model_weight": ICP_SIMILARITY_WEIGHT,
        "size_weight": COMPANY_SIZE_WEIGHT,
        "sigmoid_k": SIGMOID_K,
        "tier_thresholds": [list(t) for t in TIER_THRESHOLDS],
    }


def sigmoid_curve_dataframe(k: float, n: int = 200) -> pd.DataFrame:
    """``probability -> (sigmoid_raw_s, icp_similarity_score)`` for the given
    sigmoid steepness — the curve rendered under "Sigmoid & blend" so a user
    can see what turning K up/down actually does to the 1–10 spread, using
    the exact same reference probabilities/formula as
    ``commercial_fit_scoring.score_company``."""
    s_min = 1.0 / (1.0 + math.exp(-k * (_SIGMOID_P_LO - 0.5)))
    s_max = 1.0 / (1.0 + math.exp(-k * (_SIGMOID_P_HI - 0.5)))
    denom = s_max - s_min if abs(s_max - s_min) > 1e-9 else 1.0

    rows = []
    for i in range(n + 1):
        p = i / n
        s = 1.0 / (1.0 + math.exp(-k * (p - 0.5)))
        icp = max(1.0, min(10.0, 1.0 + 9.0 * (s - s_min) / denom))
        rows.append({"probability": p, "sigmoid_raw_s": s, "icp_similarity_score": icp})
    return pd.DataFrame(rows)


def validate_tier_thresholds(hot: float, warm: float, cool: float) -> "Optional[str]":
    """User-facing error when tier cutoffs aren't strictly descending, else
    ``None``. Mirrors ``lovable_gcs_upload.validate_gcs_bucket``'s style."""
    if not (hot > warm > cool >= 0):
        return (
            "Tier-drempels moeten aflopend zijn: Hot > Warm > Cool ≥ 0 "
            f"(nu: {hot} / {warm} / {cool})."
        )
    return None


def score_distribution_dataframe(original_by_id: dict, rescored_by_id: dict) -> pd.DataFrame:
    """Long-form ``(company_id, when, commercial_fit_score)`` table for an
    overlaid before/after histogram."""
    rows = []
    for cid, detail in original_by_id.items():
        rows.append({
            "company_id": cid, "when": "Huidig",
            "commercial_fit_score": detail.get("commercial_fit_score"),
        })
    for cid, detail in rescored_by_id.items():
        rows.append({
            "company_id": cid, "when": "Nieuw",
            "commercial_fit_score": detail.get("commercial_fit_score"),
        })
    return pd.DataFrame(rows)


def tier_distribution_dataframe(original_by_id: dict, rescored_by_id: dict) -> pd.DataFrame:
    """Long-form ``(tier, when, count)`` table for a before/after tier bar
    chart — known tiers first in Hot→Pass order, any unrecognised label
    last rather than dropped."""
    before = tier_distribution(original_by_id)
    after = tier_distribution(rescored_by_id)
    tiers = sorted(
        set(before) | set(after),
        key=lambda t: TIER_LABELS.index(t) if t in TIER_LABELS else len(TIER_LABELS),
    )
    rows = []
    for tier in tiers:
        rows.append({"tier": tier, "when": "Huidig", "count": before.get(tier, 0)})
        rows.append({"tier": tier, "when": "Nieuw", "count": after.get(tier, 0)})
    return pd.DataFrame(rows)


def biggest_movers_dataframe(
    original_by_id: dict, rescored_by_id: dict, top_n: int = 20,
) -> pd.DataFrame:
    """Companies whose ``commercial_fit_score`` moved the most between the
    original and re-scored run, sorted by absolute delta descending.
    Companies that were skipped (no ``scoring_inputs``, unchanged score) or
    missing from either side are excluded — nothing to show a delta for."""
    rows = []
    for cid, new_detail in rescored_by_id.items():
        old_detail = original_by_id.get(cid)
        if old_detail is None:
            continue
        old_score = old_detail.get("commercial_fit_score")
        new_score = new_detail.get("commercial_fit_score")
        if old_score is None or new_score is None:
            continue
        rows.append({
            "company_id": cid,
            "company_name": new_detail.get("company_name", ""),
            "tier_before": old_detail.get("commercial_tier"),
            "tier_after": new_detail.get("commercial_tier"),
            "score_before": old_score,
            "score_after": new_score,
            "delta": round(new_score - old_score, 3),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.reindex(df["delta"].abs().sort_values(ascending=False).index).head(top_n)


def _signal_raw_value(detail: dict, signal_field: str) -> "float | None":
    """Persisted raw value of one signal from a company-details record's
    ``scoring_inputs.signals`` block, or ``None`` if never enriched /
    unparseable. Mirrors the same block ``rehydrate_scoring_row`` reads."""
    signals = (detail.get("scoring_inputs") or {}).get("signals") or {}
    raw = signals.get(signal_field)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def signal_has_presence(detail: dict, signal_field: str) -> bool:
    """True when ``signal_field`` carries a genuine, non-zero value.

    ``score_company`` folds both a missing signal and an explicit 0 into the
    same 0.0 contribution to the LR formula (see its ``_is_missing`` check),
    so "present" here means strictly greater than zero — the same condition
    under which the signal actually pulls the score in its coefficient's
    direction."""
    value = _signal_raw_value(detail, signal_field)
    return value is not None and value > 0


def signal_split_score_dataframe(
    original_by_id: dict, rescored_by_id: dict, signal_field: str,
) -> pd.DataFrame:
    """Long-form ``(company_id, when, group, commercial_fit_score)`` table,
    split into "Met <signal>" / "Zonder <signal>" groups by the ORIGINAL
    record's raw signal value (rescoring never changes which companies had
    the signal — only the score), so a company sits in the same group for
    both its "Huidig" and "Nieuw" row. Powers the signal-analysis tab's
    faceted before/after histogram."""
    met_label = f"Met {signal_field}"
    zonder_label = f"Zonder {signal_field}"
    rows = []
    for cid, detail in original_by_id.items():
        group = met_label if signal_has_presence(detail, signal_field) else zonder_label
        rows.append({
            "company_id": cid, "when": "Huidig", "group": group,
            "commercial_fit_score": detail.get("commercial_fit_score"),
        })
    for cid, detail in rescored_by_id.items():
        source = original_by_id.get(cid, detail)
        group = met_label if signal_has_presence(source, signal_field) else zonder_label
        rows.append({
            "company_id": cid, "when": "Nieuw", "group": group,
            "commercial_fit_score": detail.get("commercial_fit_score"),
        })
    return pd.DataFrame(rows)


def signal_split_summary(split_df: pd.DataFrame) -> pd.DataFrame:
    """``(group, when) -> n, mediaan`` summary table under the signal-split
    histogram — the actual numbers behind the visual spread."""
    if split_df.empty:
        return split_df
    return (
        split_df.groupby(["group", "when"])["commercial_fit_score"]
        .agg(n="count", mediaan="median")
        .reset_index()
        .sort_values(["group", "when"])
    )


def build_multi_country_preview(
    current_by_country: dict[str, dict], params: dict, *, now_iso: str,
) -> dict:
    """Re-score every already-loaded country's companies against the same
    ``params``, purely in memory — no download, no upload. Returns
    ``{"original_by_id", "rescored_by_id"}`` with ids prefixed
    ``"<country_folder>:<company_id>"`` so companies from different
    countries never collide in the aggregated preview.

    Upload always stays per-country (via ``build_rescored_run`` +
    ``upload_rescored_run`` on one country's own unprefixed ids) — this
    function only feeds the "Alle landen" preview tab's aggregate charts."""
    original_by_id: dict = {}
    rescored_by_id: dict = {}
    for country, current in current_by_country.items():
        rescored_by_file = {
            filename: rescore_details_bucket(bucket_dict, params, now_iso=now_iso)
            for filename, bucket_dict in current["detail_files"].items()
        }
        for bucket_dict in current["detail_files"].values():
            for cid, detail in bucket_dict.items():
                original_by_id[f"{country}:{cid}"] = detail
        for bucket_dict in rescored_by_file.values():
            for cid, detail in bucket_dict.items():
                rescored_by_id[f"{country}:{cid}"] = detail
    return {"original_by_id": original_by_id, "rescored_by_id": rescored_by_id}


def build_multi_country_rescored_runs(
    current_by_country: dict[str, dict], params: dict, *, run_folder: str, now_iso: str,
) -> dict[str, dict]:
    """Re-score every already-loaded country against the same ``params`` and
    target ``run_folder``, purely in memory. Unlike ``build_multi_country_preview``
    (which prefixes ids for the aggregate charts), each country's result here
    keeps its own unprefixed company ids — exactly the shape
    ``write_rescored_run``/``upload_rescored_run`` expect for a single
    country — so the bulk-upload button can write+upload one country's
    result at a time straight from this dict, same as the single-country
    "Toepassen & uploaden" flow does for one already-loaded country.

    Returns ``{country_folder: rescored_run}`` where each ``rescored_run``
    is a ``build_rescored_run`` result (``{"list_items", "detail_files",
    "manifest"}``)."""
    return {
        country: build_rescored_run(
            current, params, country_folder=country, run_folder=run_folder, now_iso=now_iso,
        )
        for country, current in current_by_country.items()
    }


def multi_country_summary_dataframe(original_by_id: dict, rescored_by_id: dict) -> pd.DataFrame:
    """``(country_folder, n_bedrijven, n_tier_gewijzigd)`` per country, from
    the country-prefixed ids ``build_multi_country_preview`` produces — the
    per-country breakdown table under the aggregate preview charts."""
    rows: dict = {}
    for cid, new_detail in rescored_by_id.items():
        country = cid.split(":", 1)[0]
        row = rows.setdefault(
            country, {"country_folder": country, "n_bedrijven": 0, "n_tier_gewijzigd": 0})
        row["n_bedrijven"] += 1
        old_detail = original_by_id.get(cid)
        if old_detail is not None and old_detail.get("commercial_tier") != new_detail.get("commercial_tier"):
            row["n_tier_gewijzigd"] += 1
    return pd.DataFrame(sorted(rows.values(), key=lambda r: r["country_folder"]))


def score_component_breakdown(row: dict, params: dict) -> dict:
    """Run ``score_company()`` on one synthetic/rehydrated row and shape its
    per-signal LR components into a waterfall: intercept -> each signal's
    +/- contribution -> lr_z_score, alongside the resulting probability,
    icp_similarity_score, company_size_score, final_commercial_fit_score and
    commercial_tier. Powers the "single company" calculator tab.

    Returns ``{"result": <score_company output>, "waterfall_steps": [(label,
    delta), ...]}`` — steps sum to ``result["lr_z_score"]``.
    """
    result = score_company(row, params=params)
    coeffs = {**LEAN_COEFFICIENTS, **(params.get("coefficients") or {})}

    steps = [("Intercept", result["lr_intercept_component"])]
    for field in coeffs:
        component_key = _FIELD_TO_COMPONENT[field]
        label = COEFFICIENT_LABELS.get(field, field).split(" — ")[0].split(".")[0]
        steps.append((label, result[component_key]))

    return {"result": result, "waterfall_steps": steps}


def employee_range_options() -> list[str]:
    """Selectable employee-range strings for the single-company calculator,
    plus a "missing / unknown" option — mirrors
    ``commercial_fit_scoring.SIZE_BAND_LOOKUP``."""
    return list(SIZE_BAND_LOOKUP) + ["missing / unknown"]


# =============================================================================
# Streamlit UI — lazy imports so the helpers above stay testable without them
# =============================================================================


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import shutil
    import tempfile

    import plotly.express as px
    import plotly.graph_objects as go
    import streamlit as st

    def render_before_after(original_by_id: dict, rescored_by_id: dict) -> None:
        """Score-distribution histogram + tier bar chart for a huidig/nieuw
        pair. Shared by the "Alle landen" and "Toepassen & uploaden" tabs.
        Shows an info message instead of crashing when there is no company
        with a score to plot (e.g. a country folder whose current/ run has
        no company-details records)."""
        dist_df = score_distribution_dataframe(original_by_id, rescored_by_id)
        if not dist_df.empty:
            dist_df = dist_df.dropna(subset=["commercial_fit_score"])
        st.subheader("Scoreverdeling: huidig vs. nieuw")
        if dist_df.empty:
            st.info("Geen bedrijven met een score om te tonen.")
        else:
            st.plotly_chart(
                px.histogram(
                    dist_df, x="commercial_fit_score", color="when",
                    barmode="group", nbins=20,
                    category_orders={"when": WHEN_ORDER},
                    color_discrete_map=WHEN_COLORS,
                    labels={"commercial_fit_score": "commercial_fit_score", "when": ""},
                ),
                use_container_width=True,
            )

        tier_df = tier_distribution_dataframe(original_by_id, rescored_by_id)
        st.subheader("Tier-verdeling: huidig vs. nieuw")
        if tier_df.empty:
            st.info("Geen tier-data om te tonen.")
        else:
            st.plotly_chart(
                px.bar(
                    tier_df, x="tier", y="count", color="when", barmode="group",
                    category_orders={"when": WHEN_ORDER},
                    color_discrete_map=WHEN_COLORS,
                    labels={"when": ""},
                ),
                use_container_width=True,
            )

    st.set_page_config(page_title="Re-score Explorer", page_icon="🎛️", layout="wide")
    st.title("🎛️ Commercial Fit Re-score Explorer")
    st.caption(
        "Tweak the scoring model — LR coefficients, sigmoid K, ICP/size blend, "
        "tier thresholds — and see the effect live. Nothing is written to GCS "
        "until you explicitly upload; current/ and existing runs are never touched."
    )

    if "rescore_params" not in st.session_state:
        st.session_state["rescore_params"] = default_params()
    params = st.session_state["rescore_params"]

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
            work_dir = tempfile.mkdtemp(prefix="rescore_streamlit_")
            st.session_state["_work_dir"] = work_dir
            try:
                with st.spinner(f"{country_folder}/current/ downloaden…"):
                    current = download_current_run(bucket, country_folder, work_dir)
                st.session_state["_current"] = current
                st.session_state["_current_country"] = country_folder
                st.session_state["_current_bucket"] = bucket
                n_companies = sum(len(b) for b in current["detail_files"].values())
                st.success(f"{n_companies} bedrijven geladen uit {country_folder}/current/.")
            except Exception as exc:
                st.error(f"Laden mislukt: {exc}")

        st.divider()
        st.header("2. Alle landen (preview & bulk-upload)")
        st.caption(
            "Laadt meerdere land-folders tegelijk voor de tab '🌍 Alle "
            "landen', waar je ze ook in bulk kunt uploaden. Wil je maar één "
            "land? Gebruik dan '📥 Huidige run laden' hierboven en "
            "'🚀 Toepassen & uploaden'."
        )
        preview_countries = st.multiselect(
            "Landen om te previewen", options=countries, default=countries,
            key="preview_countries_select",
        )
        if st.button("🌍 Alle landen preview laden", disabled=not preview_countries):
            old_all_dir = st.session_state.get("_all_countries_work_dir")
            if old_all_dir:
                shutil.rmtree(old_all_dir, ignore_errors=True)
            all_dir = tempfile.mkdtemp(prefix="rescore_streamlit_all_")
            st.session_state["_all_countries_work_dir"] = all_dir
            loaded: dict = {}
            progress = st.progress(0.0)
            for i, c in enumerate(preview_countries):
                try:
                    loaded[c] = download_current_run(bucket, c, f"{all_dir}/{c}")
                except Exception as exc:
                    st.warning(f"{c}: laden mislukt ({exc})")
                progress.progress((i + 1) / len(preview_countries))
            st.session_state["_all_countries_current"] = loaded
            n_total = sum(len(b) for cur in loaded.values() for b in cur["detail_files"].values())
            st.success(f"{len(loaded)} landen geladen, {n_total} bedrijven totaal.")

        st.divider()
        if st.button("↺ Reset naar standaardparameters"):
            st.session_state["rescore_params"] = default_params()
            st.rerun()

    current = st.session_state.get("_current")

    # ---------------------------------------------------------------------
    # Live re-score of the currently loaded single country — shared by the
    # "Signaal-analyse" and "Toepassen & uploaden" tabs so it's computed once
    # per rerun, not twice.
    # ---------------------------------------------------------------------
    rescored_run = None
    original_by_id: dict = {}
    rescored_by_id: dict = {}
    if current:
        _now_iso = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        rescored_run = build_rescored_run(
            current, params,
            country_folder=st.session_state.get("_current_country", ""),
            run_folder=st.session_state.get("_run_folder_preview") or default_rescore_run_folder(),
            now_iso=_now_iso,
        )
        original_by_id = {
            cid: d for b in current["detail_files"].values() for cid, d in b.items()}
        rescored_by_id = {
            cid: d for b in rescored_run["detail_files"].values() for cid, d in b.items()}

    # ---------------------------------------------------------------------
    # Parameter tabs
    # ---------------------------------------------------------------------
    tab_coef, tab_sigmoid, tab_tiers, tab_calc, tab_signal, tab_all, tab_apply = st.tabs([
        "⚖️ Coëfficiënten", "📈 Sigmoid & blend", "🎯 Tier-drempels",
        "🧮 Eén bedrijf", "🔬 Signaal-analyse", "🌍 Alle landen",
        "🚀 Toepassen & uploaden",
    ])

    # ── Coefficients ───────────────────────────────────────────────────────
    with tab_coef:
        st.subheader("Logistic-regression coëfficiënten")
        st.caption(
            "Elke coëfficiënt weegt hoe zwaar dat signaal (0–3, genormaliseerd "
            "naar 0–1) meetelt in de log-odds. Positief = verhoogt de kans op "
            "ICP-fit; negatief = verlaagt de kans."
        )
        params["intercept"] = st.slider(
            "Intercept", min_value=-3.0, max_value=1.0,
            value=float(params["intercept"]), step=0.01, key="intercept_slider",
            help="Basiswaarde van de log-odds vóór enig signaal wordt meegeteld.",
        )
        new_coeffs = {}
        for field, default_val in LEAN_COEFFICIENTS.items():
            current_val = params["coefficients"].get(field, default_val)
            new_coeffs[field] = st.slider(
                field, min_value=-1.0, max_value=1.5,
                value=float(current_val), step=0.005, key=f"coef_{field}",
            )
            st.caption(COEFFICIENT_LABELS.get(field, ""))
        params["coefficients"] = new_coeffs

    # ── Sigmoid & blend ──────────────────────────────────────────────────────
    with tab_sigmoid:
        st.subheader("Sigmoid steilheid (K) & ICP/grootte-blend")
        col_k, col_w = st.columns(2)
        with col_k:
            params["sigmoid_k"] = st.slider(
                "Sigmoid K", min_value=1.0, max_value=25.0,
                value=float(params["sigmoid_k"]), step=0.5, key="k_slider",
                help="Hoger = scherpere spreiding tussen 1–10 rond de "
                     "kansdrempel van 0.5; verandert de ranking-volgorde niet, "
                     "wel de spreiding.",
            )
        with col_w:
            params["model_weight"] = st.slider(
                "ICP-gewicht (model_weight)", min_value=0.0, max_value=1.0,
                value=float(params["model_weight"]), step=0.05, key="model_weight_slider",
            )
            params["size_weight"] = round(1.0 - params["model_weight"], 2)
            st.metric(
                "Grootte-gewicht (size_weight, uit Lucia/Lusha-data)",
                params["size_weight"],
            )
        st.caption(
            "model_weight + size_weight telt altijd op tot 1 — het "
            "grootte-gewicht past zich automatisch aan (zoals in de "
            "referentie-spreadsheet's 'Scoring Parameters'-tab)."
        )

        curve_df = sigmoid_curve_dataframe(params["sigmoid_k"])
        fig = px.line(
            curve_df, x="probability", y=["sigmoid_raw_s", "icp_similarity_score"],
            labels={"probability": "model_probability (p)", "value": "waarde", "variable": ""},
            title=f"Sigmoid-curve bij K={params['sigmoid_k']}",
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "sigmoid_raw_s (links, 0–1) is de gestretchte kans; "
            "icp_similarity_score (rechts, 1–10) is die kans herschaald over "
            "de trainingspopulatie's min/max."
        )

    # ── Tier thresholds ──────────────────────────────────────────────────────
    with tab_tiers:
        st.subheader("Tier-drempels (final_commercial_fit_score)")
        thresholds_by_label = {label: score for score, label in params["tier_thresholds"]}
        hot = st.number_input(
            "🥇 Hot vanaf", value=float(thresholds_by_label.get("🥇 Hot", 8.86)),
            step=0.01, key="tier_hot")
        warm = st.number_input(
            "🥈 Warm vanaf", value=float(thresholds_by_label.get("🥈 Warm", 7.32)),
            step=0.01, key="tier_warm")
        cool = st.number_input(
            "🥉 Cool vanaf", value=float(thresholds_by_label.get("🥉 Cool", 5.04)),
            step=0.01, key="tier_cool")
        error = validate_tier_thresholds(hot, warm, cool)
        if error:
            st.error(error)
        else:
            params["tier_thresholds"] = [
                [hot, "🥇 Hot"], [warm, "🥈 Warm"], [cool, "🥉 Cool"], [0.0, "❄️ Pass"],
            ]
        st.caption("Onder Cool valt een bedrijf automatisch in ❄️ Pass.")

    # ── Single company calculator ────────────────────────────────────────────
    with tab_calc:
        st.subheader("Eén bedrijf — live doorrekenen")
        st.caption(
            "Zet signalen op 'missing' om te zien hoe score_company's eigen "
            "'op basis van onvolledige data'-notitie werkt."
        )
        row: dict = {}
        cols = st.columns(2)
        for i, field in enumerate(LEAN_COEFFICIENTS):
            with cols[i % 2]:
                missing = st.checkbox(f"{field}: missing", key=f"calc_missing_{field}")
                if missing:
                    row[field] = None
                    st.caption("→ None (nooit enriched)")
                else:
                    row[field] = st.slider(
                        field, min_value=0, max_value=3, value=0,
                        key=f"calc_signal_{field}")
        emp_range = st.selectbox(
            "employee_range", options=employee_range_options(), key="calc_emp_range")
        row["employee_range"] = None if emp_range == "missing / unknown" else emp_range

        breakdown = score_component_breakdown(row, params)
        result = breakdown["result"]

        waterfall = go.Figure(go.Waterfall(
            orientation="v",
            measure=["relative"] * len(breakdown["waterfall_steps"]),
            x=[label for label, _ in breakdown["waterfall_steps"]],
            y=[delta for _, delta in breakdown["waterfall_steps"]],
        ))
        waterfall.update_layout(
            title=f"Opbouw van lr_z_score = {result['lr_z_score']}", showlegend=False)
        st.plotly_chart(waterfall, use_container_width=True)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("lr_z_score", result["lr_z_score"])
        m2.metric("model_probability", result["lean_model_prob"])
        m3.metric("icp_similarity_score", result["icp_similarity_score"])
        m4.metric("company_size_score", result["company_size_score"])
        m5.metric("final_commercial_fit_score", result["final_commercial_fit_score"])
        st.subheader(f"Tier: {result['commercial_tier']}")
        if result.get("missing_scoring_fields"):
            st.info(result["scoring_notes"])

    # ── Signal analysis ──────────────────────────────────────────────────────
    with tab_signal:
        st.subheader("Signaal-analyse — doorwerking van één signaal")
        st.caption(
            "Bedrijven MET het signaal (score > 0) tegenover bedrijven ZONDER "
            "(score 0 of nooit enriched — score_company behandelt die twee "
            "hetzelfde), huidig vs. nieuw naast elkaar. Zo zie je bijvoorbeeld "
            "hoe zwaar sig_foreign_hq_score — de sterkste voorspeller — de "
            "score echt optrekt."
        )
        if not current:
            st.info(
                "Laad eerst een land-folder via de zijbalk om de doorwerking "
                "op echte data te zien."
            )
        else:
            signal_options = list(LEAN_COEFFICIENTS)
            default_idx = (
                signal_options.index("sig_foreign_hq_score")
                if "sig_foreign_hq_score" in signal_options else 0
            )
            signal_field = st.selectbox(
                "Signaal", options=signal_options, index=default_idx,
                key="signal_analysis_field",
            )
            st.caption(COEFFICIENT_LABELS.get(signal_field, ""))

            split_df = signal_split_score_dataframe(original_by_id, rescored_by_id, signal_field)
            if split_df.empty:
                st.info("Geen data om te tonen.")
            else:
                met_label = f"Met {signal_field}"
                zonder_label = f"Zonder {signal_field}"
                fig = px.histogram(
                    split_df, x="commercial_fit_score", color="when",
                    facet_col="group", barmode="group", nbins=20,
                    color_discrete_map=WHEN_COLORS,
                    category_orders={
                        "when": WHEN_ORDER,
                        "group": [met_label, zonder_label],
                    },
                    labels={"when": ""},
                )
                st.plotly_chart(fig, use_container_width=True)

                st.dataframe(
                    signal_split_summary(split_df), use_container_width=True, hide_index=True)

    # ── All countries — preview + bulk upload ───────────────────────────────
    with tab_all:
        st.subheader("Alle landen — preview & bulk-upload")
        st.caption(
            "Geaggregeerd over alle geladen land-folders. Er wordt pas "
            "geschreven naar GCS als je onderaan expliciet op 'Upload naar "
            "GCS — alle landen' klikt; tot dan is dit alleen een preview."
        )
        all_countries_current = st.session_state.get("_all_countries_current")
        if not all_countries_current:
            st.info(
                "Nog geen landen geladen. Gebruik '🌍 Alle landen preview "
                "laden' in de zijbalk."
            )
        else:
            _now_iso_all = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            preview = build_multi_country_preview(all_countries_current, params, now_iso=_now_iso_all)
            p_original = preview["original_by_id"]
            p_rescored = preview["rescored_by_id"]
            summary_df = multi_country_summary_dataframe(p_original, p_rescored)

            m1, m2, m3 = st.columns(3)
            m1.metric("Landen geladen", len(all_countries_current))
            m2.metric("Bedrijven totaal", len(p_original))
            m3.metric(
                "Tier gewijzigd",
                int(summary_df["n_tier_gewijzigd"].sum()) if not summary_df.empty else 0,
            )

            st.subheader("Per land")
            if summary_df.empty:
                st.info(
                    "Geen bedrijven in de geladen landen — hebben deze "
                    "land-folders een current/ run met company-details?"
                )
            else:
                st.dataframe(summary_df, use_container_width=True, hide_index=True)

            render_before_after(p_original, p_rescored)

            st.divider()
            st.subheader("Uploaden naar GCS — alle landen")
            st.caption(
                "Schrijft, per geladen land, naar een NIEUWE run-folder met "
                "dezelfde naam — current/ en bestaande runs blijven overal "
                "ongewijzigd. Eén land dat mislukt houdt de andere landen "
                "niet tegen; het resultaat per land staat in de tabel "
                "hieronder."
            )
            all_run_folder = st.text_input(
                "Run-folder (voor alle landen)", value=default_rescore_run_folder(),
                key="_all_run_folder_preview")
            all_confirmed = st.checkbox(
                f"Ik begrijp dat dit naar gs://{bucket}/<land>/runs/"
                f"{all_run_folder}/ schrijft voor elk van de "
                f"{len(all_countries_current)} geladen landen "
                "(current/ blijft onaangeroerd).",
                key="_all_upload_confirmed",
            )
            if st.button(
                "📤 Upload naar GCS — alle landen", type="primary",
                disabled=not all_confirmed,
            ):
                all_dir = st.session_state["_all_countries_work_dir"]
                _now_iso_upload = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                per_country_runs = build_multi_country_rescored_runs(
                    all_countries_current, params,
                    run_folder=all_run_folder, now_iso=_now_iso_upload,
                )
                progress = st.progress(0.0)
                result_rows = []
                for i, (country, run) in enumerate(per_country_runs.items()):
                    try:
                        out_dir = write_rescored_run(run, f"{all_dir}/{country}/out")
                        upload_results = upload_rescored_run(
                            out_dir, bucket, country, all_run_folder)
                        n_failed = sum(1 for r in upload_results if not r["success"])
                        result_rows.append({
                            "country_folder": country,
                            "bedrijven_herscoord": run["manifest"]["companies_rescored"],
                            "bestanden_geupload": len(upload_results),
                            "mislukt": n_failed,
                            "status": "✅ OK" if n_failed == 0 else f"⚠️ {n_failed} upload(s) mislukt",
                        })
                    except Exception as exc:
                        result_rows.append({
                            "country_folder": country,
                            "bedrijven_herscoord": 0,
                            "bestanden_geupload": 0,
                            "mislukt": None,
                            "status": f"❌ {exc}",
                        })
                    progress.progress((i + 1) / len(per_country_runs))
                result_df = pd.DataFrame(result_rows)
                n_ok = (result_df["status"] == "✅ OK").sum() if not result_df.empty else 0
                if n_ok == len(result_df) and not result_df.empty:
                    st.success(
                        f"Alle {len(result_df)} landen succesvol geüpload naar "
                        f"gs://{bucket}/<land>/runs/{all_run_folder}/."
                    )
                else:
                    st.error(
                        f"{len(result_df) - n_ok} van {len(result_df)} landen hadden "
                        "problemen — zie de tabel hieronder."
                    )
                st.dataframe(result_df, use_container_width=True, hide_index=True)

    # ── Apply & upload ────────────────────────────────────────────────────────
    with tab_apply:
        if not current:
            st.info(
                "Laad eerst een land-folder via de zijbalk om de effect op "
                "echte data te zien."
            )
        else:
            country_folder = st.session_state["_current_country"]
            bucket = st.session_state["_current_bucket"]
            manifest = rescored_run["manifest"]

            m1, m2, m3 = st.columns(3)
            m1.metric("Bedrijven her-scoord", manifest["companies_rescored"])
            m2.metric("Overgeslagen (geen scoring_inputs)", manifest["companies_skipped"])
            m3.metric("Land-folder", country_folder)
            if manifest["skipped_company_ids"]:
                with st.expander("Overgeslagen bedrijven"):
                    st.write(", ".join(manifest["skipped_company_ids"]))

            render_before_after(original_by_id, rescored_by_id)

            st.subheader("Grootste verschuivingen")
            movers_df = biggest_movers_dataframe(original_by_id, rescored_by_id)
            if movers_df.empty:
                st.info("Geen scoreverschillen om te tonen.")
            else:
                st.dataframe(movers_df, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("Uploaden naar GCS")
            st.caption(
                "Schrijft naar een NIEUWE run-folder — current/ en bestaande "
                "runs blijven ongewijzigd. De live Company Hub ziet deze "
                "cijfers pas na een aparte, expliciete 'current'-promotie."
            )
            run_folder = st.text_input(
                "Run-folder", value=default_rescore_run_folder(), key="_run_folder_preview")
            confirmed = st.checkbox(
                f"Ik begrijp dat dit naar gs://{bucket}/{country_folder}/runs/"
                f"{run_folder}/ schrijft (current/ blijft onaangeroerd).",
                key="_upload_confirmed",
            )
            if st.button("📤 Upload naar GCS", type="primary", disabled=not confirmed):
                try:
                    out_dir = write_rescored_run(
                        rescored_run, st.session_state["_work_dir"] + "/out")
                    with st.spinner("Uploaden…"):
                        results = upload_rescored_run(out_dir, bucket, country_folder, run_folder)
                    n_failed = sum(1 for r in results if not r["success"])
                    if n_failed:
                        st.error(f"{n_failed} van {len(results)} uploads mislukt.")
                    else:
                        st.success(
                            f"{len(results)} bestanden geüpload naar "
                            f"gs://{bucket}/{country_folder}/runs/{run_folder}/"
                        )
                    st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
                except Exception as exc:
                    st.error(f"Upload mislukt: {exc}")

    st.session_state["rescore_params"] = params


if __name__ == "__main__":  # pragma: no cover
    main()
