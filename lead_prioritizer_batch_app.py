"""Local Streamlit batch app for Lead Prioritizer v2.

Upload an Excel file, map columns, pick a run mode, run the shared batch core,
and download an enriched workbook.  Intended for synchronous local runs (small /
medium batches, HQ-only checks, full v2 for manageable sizes) — NOT the future
async Anthropic Message Batch workflow, which will be designed separately.

This app adds no enrichment logic and does not duplicate batch logic: it uses
``BatchRunConfig`` / ``run_batch_dataframe`` / ``build_excel_workbook_bytes``
from ``lead_prioritizer_batch_core.py``.  It does not import or modify any legacy
app, ``enrich_clients_claude.py``, or ``commercial_fit_scoring.py``.

The ``import streamlit`` is deliberately lazy (inside ``main``) so the pure
helper functions below can be imported and unit-tested without Streamlit
installed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from lead_prioritizer_batch_core import (
    BatchRunConfig,
    build_excel_workbook_bytes,
    run_batch_dataframe,
    run_batch_foreign_hq_only,
    run_batch_non_english_foreign_hq_only,
    run_batch_dataframe_parallel,
    select_batch_rows,
    FOREIGN_HQ_ONLY_MODE,
    NON_ENGLISH_FOREIGN_HQ_ONLY_MODE,
    MAX_PARALLEL_WORKERS,
    apply_c5_adjudication,
    add_c5_summary_fields,
    row_selected_for_c5,
    C5_SCORING_BEHAVIORS,
    C5_SCOPES,
)
from lead_hq_ai_interpreter import (
    DEFAULT_OPENAI_MODEL,
    SUPPORTED_OPENAI_MODELS,
)
from compare_ai_providers_lead_prioritizer import (
    run_comparison,
    run_triple_comparison,
    build_provider_cost_rows,
    build_cost_totals,
    TWO_WAY_COST_PROVIDERS,
    TRIPLE_COST_PROVIDERS,
    DEFAULT_OPENAI_NANO_MODEL,
    DEFAULT_OPENAI_MINI_MODEL,
)

from lead_hq_sonnet_adjudicator import (
    DEFAULT_SONNET_ADJUDICATION_MODEL,
    C5_MODEL_TIER_CHOICES,
)
from run_hq_sonnet_adjudication_probe import (
    resolve_c5_model,
    check_opus_guardrail,
    _OPUS_WARNING,
)
from export_lead_prioritizer_to_lovable_json import (
    export_batch_output_tables_to_lovable_json,
)
from lovable_gcs_upload import (
    DEFAULT_GCS_BUCKET,
    check_gcloud_available,
    country_folder_slug,
    default_gcs_run_folder,
    describe_gcloud_environment,
    normalize_gcs_prefix,
    public_url as gcs_public_url,
    build_upload_plan,
    run_upload_plan,
)

CONFIRM_THRESHOLD = 50
_C5_OPUS_ROW_CAP = 10

# Run-mode radio labels → core modes (order defines UI order; first is default).
MODE_LABELS: list[str] = [
    "Full v2 enrichment",
    "HQ only",
    "Evidence only",
    "Signals, no score",
    "Full, no score",
    "Full enrichment, confirmed foreign-HQ only",
    "Full enrichment, confirmed non-English foreign-HQ only",
]
_LABEL_TO_MODE: dict[str, str] = {
    "Full v2 enrichment": "full",
    "HQ only": "hq_only",
    "Evidence only": "evidence_only",
    "Signals, no score": "signals_no_score",
    "Full, no score": "full_no_score",
    "Full enrichment, confirmed foreign-HQ only": FOREIGN_HQ_ONLY_MODE,
    "Full enrichment, confirmed non-English foreign-HQ only": NON_ENGLISH_FOREIGN_HQ_ONLY_MODE,
}

FOREIGN_HQ_ONLY_HELP_TEXT = (
    "This first runs HQ detection and optional C5 adjudication, then performs "
    "full enrichment only for leads with confirmed foreign-HQ score 3. "
    "Non-confirmed rows are kept in the output and marked as skipped."
)

NON_ENGLISH_FOREIGN_HQ_ONLY_HELP_TEXT = (
    "Australia-specific: this first runs HQ detection and optional C5 "
    "adjudication (same as \"confirmed foreign-HQ only\"), then performs full "
    "enrichment only for leads whose input country is Australia, whose "
    "foreign HQ is confirmed, AND whose parent HQ is in a non-English-speaking "
    "market (e.g. Japan, Germany, Brazil). English-speaking parents (US, UK, "
    "Canada, New Zealand, Ireland) and nuanced/review markets (Singapore, "
    "India, South Africa, UAE, ...) are kept in the output but not fully "
    "enriched."
)

# Likely column names for preselection.
COMPANY_CANDIDATES = ["company_name", "Company Name", "name", "legal_name"]
DOMAIN_CANDIDATES = [
    "domain", "validated_domain", "input_domain", "website_domain",
    "Website", "website",
]
COUNTRY_CANDIDATES = ["input_country", "country", "Country"]

# Central list of default-input-country choices. Add new countries here only —
# no other code path hardcodes a country name. Kept alphabetically sorted.
SUPPORTED_DEFAULT_INPUT_COUNTRIES = ["Australia", "Brazil", "Italy", "Netherlands", "New Zealand", "Uruguay"]
DEFAULT_COUNTRY_PLACEHOLDER = "Select country..."
DEFAULT_COUNTRY_REQUIRED_MESSAGE = "Please select a default input country before running."

_SERPER_KEY_NAME = "SERPER_API_KEY"
_ANTHROPIC_KEY_NAME = "ANTHROPIC_API_KEY"
_OPENAI_KEY_NAME = "OPENAI_API_KEY"
_GCS_BUCKET_NAME_KEY = "GCS_BUCKET_NAME"
_GCS_BASE_PREFIX_KEY = "GCS_BASE_PREFIX"

# Experimental AI-provider selection for HQ interpretation. The first label is
# the default and preserves the existing Anthropic-only behavior exactly.
AI_PROVIDER_LABELS: list[str] = [
    "Anthropic only (default)",
    "OpenAI only (experimental)",
    "Compare Anthropic vs OpenAI (experimental)",
    "Compare Anthropic vs OpenAI nano vs OpenAI mini (experimental)",
]
_AI_PROVIDER_LABEL_TO_MODE: dict[str, str] = {
    "Anthropic only (default)": "anthropic",
    "OpenAI only (experimental)": "openai",
    "Compare Anthropic vs OpenAI (experimental)": "compare",
    "Compare Anthropic vs OpenAI nano vs OpenAI mini (experimental)": "compare_triple",
}

COMPARE_MODE_WARNING_TEXT = (
    "EXPERIMENTAL: compare mode runs every selected row TWICE — once with "
    "Anthropic and once with OpenAI — doubling AI calls and cost. "
    "Recommended row limit: 5-10 rows."
)

COMPARE_TRIPLE_MODE_WARNING_TEXT = (
    "EXPERIMENTAL: this mode runs every selected row THREE times — Anthropic, "
    "OpenAI nano, and OpenAI mini — tripling AI calls and cost. "
    "Recommended row limit: 5-10 rows."
)

_EXPERIMENTAL_PROVIDER_MODE_BLOCK_TEXT = (
    "The experimental OpenAI/compare provider options are only available for "
    "the standard batch modes for now. Foreign-HQ-only modes stay "
    "Anthropic-only; switch the run mode or select \"Anthropic only\"."
)
_EXPERIMENTAL_PROVIDER_C5_BLOCK_TEXT = (
    "The experimental OpenAI/compare provider options are not available "
    "together with C5 Sonnet adjudication yet. Disable C5 or select "
    "\"Anthropic only\"."
)
_OPENAI_KEY_MISSING_TEXT = (
    "OPENAI_API_KEY is missing. Set it in .streamlit/secrets.toml or the "
    "environment to use the experimental OpenAI provider options."
)


def ai_provider_label_to_mode(label: str) -> str:
    """Map an AI-provider UI label to "anthropic" | "openai" | "compare"."""
    try:
        return _AI_PROVIDER_LABEL_TO_MODE[label]
    except KeyError:
        raise ValueError(f"Unknown AI provider label: {label!r}")


def validate_ai_provider_run(
    provider_mode: str,
    run_mode: str,
    c5_enabled: bool,
    openai_api_key: str,
) -> Optional[str]:
    """Validate an experimental provider selection against the run settings.

    Returns a user-facing error message when the run must be blocked, or
    ``None`` when the run may proceed. "anthropic" is always allowed — the
    existing SERPER/ANTHROPIC key handling stays authoritative for it.
    """
    if provider_mode == "anthropic":
        return None
    if provider_mode not in ("openai", "compare", "compare_triple"):
        return f"Unknown AI provider mode: {provider_mode!r}"
    if run_mode in (FOREIGN_HQ_ONLY_MODE, NON_ENGLISH_FOREIGN_HQ_ONLY_MODE):
        return _EXPERIMENTAL_PROVIDER_MODE_BLOCK_TEXT
    if c5_enabled:
        return _EXPERIMENTAL_PROVIDER_C5_BLOCK_TEXT
    if not openai_api_key:
        return _OPENAI_KEY_MISSING_TEXT
    return None


def build_provider_comparison_workbook_bytes(comparison_df, cost_summary_df=None) -> bytes:
    """Write the provider-comparison DataFrame to xlsx bytes.

    ``cost_summary_df`` is optional (backward compatible); when given, it is
    written to an additional "Cost Summary" sheet.
    """
    import io

    import pandas as _pd

    buf = io.BytesIO()
    with _pd.ExcelWriter(buf, engine="openpyxl") as writer:
        comparison_df.to_excel(writer, sheet_name="Provider Comparison", index=False)
        if cost_summary_df is not None:
            cost_summary_df.to_excel(writer, sheet_name="Cost Summary", index=False)
    buf.seek(0)
    return buf.getvalue()


def build_cost_summary_dataframe(comparison_df, providers) -> "pd.DataFrame":
    """Per-provider/model cost rows as a DataFrame, for display and export.

    ``providers`` is the same ``(label, column_prefix)`` list accepted by
    ``build_provider_cost_rows`` (e.g. ``TWO_WAY_COST_PROVIDERS`` or
    ``TRIPLE_COST_PROVIDERS``).
    """
    import pandas as _pd

    cost_rows = build_provider_cost_rows(comparison_df, providers)
    return _pd.DataFrame(cost_rows)


def build_comparison_download_filename(timestamp) -> str:
    """Download name for a provider-comparison workbook."""
    return (
        "lead_prioritizer_provider_comparison_"
        f"{timestamp.strftime('%Y%m%d_%H%M%S')}.xlsx"
    )


# ---------------------------------------------------------------------------
# Pure helpers (no Streamlit import required)
# ---------------------------------------------------------------------------

def _load_streamlit_secrets():
    """Return ``st.secrets`` if available, else None.  Never raises."""
    try:
        import streamlit as st  # lazy
        return st.secrets
    except Exception:
        return None


def get_secret_or_env(
    key: str,
    secrets=None,
    env: Optional[dict] = None,
) -> str:
    """Resolve a key from Streamlit secrets first, then environment variables.

    Returns "" when absent.  Never raises and never surfaces the value beyond
    returning it to the caller.
    """
    env = os.environ if env is None else env
    if secrets is None:
        secrets = _load_streamlit_secrets()
    try:
        if secrets is not None and key in secrets:
            val = secrets[key]
            if val:
                return str(val).strip()
    except Exception:
        pass
    return (env.get(key) or "").strip()


def resolve_default_input_country(selected: str) -> tuple[Optional[str], Optional[str]]:
    """Validate the selected default input country.

    Returns ``(country, error)`` — exactly one of the two is not ``None``.
    ``country`` is the selected value verbatim (e.g. "Italy", "Brazil",
    "Uruguay"); ``error`` is the user-facing message when the placeholder is
    still selected (or an unknown value is passed in).
    """
    if selected in SUPPORTED_DEFAULT_INPUT_COUNTRIES:
        return selected, None
    return None, DEFAULT_COUNTRY_REQUIRED_MESSAGE


def resolve_default_column(columns, candidates) -> Optional[str]:
    """Pick the first candidate present in ``columns`` (exact, then case-insensitive)."""
    cols = list(columns)
    for cand in candidates:
        if cand in cols:
            return cand
    lower_map = {str(c).lower(): c for c in cols}
    for cand in candidates:
        hit = lower_map.get(str(cand).lower())
        if hit is not None:
            return hit
    return None


def count_selected_rows(total_rows: int, start_row: int, row_limit: int) -> int:
    """Mirror ``select_batch_rows``: start offset + row_limit (0 = all remaining)."""
    remaining = max(0, int(total_rows) - max(0, int(start_row)))
    if row_limit and int(row_limit) > 0:
        return min(remaining, int(row_limit))
    return remaining


def mode_label_to_core_mode(label: str) -> str:
    """Map a UI radio label to a core run mode."""
    try:
        return _LABEL_TO_MODE[label]
    except KeyError:
        raise ValueError(f"Unknown run-mode label: {label!r}")


def build_download_filename(mode: str) -> str:
    return f"lead_prioritizer_v2_{mode}_enriched.xlsx"


DEFAULT_AUTOSAVE_DIR = "batch_outputs"
AUTOSAVE_HELP_TEXT = (
    "When enabled, the completed Excel output will be saved automatically on "
    "this machine in the selected folder. The download button will still be shown."
)

PARALLEL_WORKER_CHOICES = [1, 2, 3, 4]
PARALLEL_HELP_TEXT = (
    "Splits selected rows into equal chunks and processes chunks in parallel. "
    "This may use more API calls at the same time and can hit rate limits. "
    "Recommended: 2–4 workers for local overnight runs."
)
PARALLEL_WARNING_TEXT = (
    "Parallel mode increases concurrent Serper and Anthropic calls. Use with care."
)


def sanitize_filename_part(value, fallback: str = "value") -> str:
    """Reduce a string to a Windows-safe filename/folder component.

    Whitespace becomes ``_``; anything outside ``[A-Za-z0-9_-]`` is stripped.
    Returns ``fallback`` when the result would otherwise be empty (blank,
    None, or entirely unsafe characters).
    """
    safe = re.sub(r"\s+", "_", str(value or "").strip())
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", safe).strip("_")
    return safe or fallback


def sanitize_run_mode_for_filename(run_mode: str) -> str:
    """Reduce a run mode to a filesystem-safe token (alnum, ``_``, ``-``)."""
    return sanitize_filename_part(run_mode, fallback="run")


def clean_user_path(value) -> Optional[Path]:
    """Turn user-entered path text into a ``Path``, or ``None`` when blank.

    Strips surrounding whitespace and a single pair of matching quotes (users
    often paste Windows paths wrapped in quotes), then expands ``~``. Never
    raises — an unparseable value is treated as blank.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except Exception:
        return None


def resolve_batch_output_dir(source_folder) -> Path:
    """Resolve the default batch autosave/output directory.

    If ``source_folder`` is a usable path, outputs default to
    ``<source_folder>/lead_prioritizer_outputs`` — organized next to the
    country input file instead of Downloads or a random temp folder.
    Otherwise falls back to the safe relative ``batch_outputs`` folder. This
    is only the *default value* shown to the user; the "Autosave directory"
    field remains editable, and ``resolve_autosave_directory`` performs the
    final ``~``-expansion / cwd-anchoring when the run actually saves.
    """
    base = clean_user_path(source_folder)
    if base is not None:
        return base / "lead_prioritizer_outputs"
    return Path(DEFAULT_AUTOSAVE_DIR)


def make_batch_output_filename(country: str, run_mode: str, timestamp) -> str:
    """Build the completed-workbook filename.

    ``lead_prioritizer_v2_<Country>_<run_mode>_enriched_<YYYYMMDD_HHMMSS>.xlsx``
    e.g. ``lead_prioritizer_v2_Brazil_full_foreign_hq_only_enriched_
    20260702_231500.xlsx``. Country and run mode are sanitized independently.
    """
    country_part = sanitize_filename_part(country, fallback="Country")
    mode_part = sanitize_run_mode_for_filename(run_mode)
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    return f"lead_prioritizer_v2_{country_part}_{mode_part}_enriched_{stamp}.xlsx"


def make_parallel_run_folder_name(country: str, run_mode: str, timestamp) -> str:
    """Folder name for a parallel autosave run: ``run_<Country>_<run_mode>_<stamp>``."""
    country_part = sanitize_filename_part(country, fallback="Country")
    mode_part = sanitize_run_mode_for_filename(run_mode)
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    return f"run_{country_part}_{mode_part}_{stamp}"


def resolve_autosave_directory(output_dir: str) -> Path:
    """Resolve the autosave directory: expand ``~``, anchor relative paths to cwd.

    Never fails just because ``output_dir`` is blank — falls back to
    ``DEFAULT_AUTOSAVE_DIR`` (``batch_outputs``).
    """
    directory = Path(output_dir or DEFAULT_AUTOSAVE_DIR).expanduser()
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    return directory


def write_parallel_run_manifest(run_dir, manifest: dict) -> Path:
    """Write ``run_manifest.json`` into the parallel run directory."""
    path = Path(run_dir) / "run_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return path


def autosave_output_workbook(output_bytes: bytes, output_dir: str, run_mode: str,
                             country: str = "", now=None) -> Path:
    """Write the completed workbook bytes to ``output_dir`` and return the path.

    - Creates the directory (and parents) when missing.
    - Relative paths resolve against the current working directory; ``~`` is
      expanded. Windows-safe via ``pathlib``.
    - Filename: ``make_batch_output_filename(country, run_mode, timestamp)``,
      e.g. ``lead_prioritizer_v2_Brazil_full_foreign_hq_only_enriched_
      20260702_231500.xlsx``. Never overwrites: an existing name gets a
      ``_2``, ``_3``, … suffix.
    - Writes only the already-built workbook bytes — no keys, no secrets.
    - Raises on failure (unwritable path etc.); the caller decides how to
      surface the error.
    """
    from datetime import datetime as _dt

    timestamp = now or _dt.now()
    full_name = make_batch_output_filename(country, run_mode, timestamp)
    stem = full_name[:-5] if full_name.endswith(".xlsx") else full_name

    directory = resolve_autosave_directory(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / f"{stem}.xlsx"
    counter = 2
    while target.exists():
        target = directory / f"{stem}_{counter}.xlsx"
        counter += 1

    target.write_bytes(output_bytes)
    return target.resolve()


def format_duration(seconds) -> str:
    """Format a duration in seconds as HH:MM:SS.  Negative/None → 00:00:00."""
    try:
        total = int(max(0, round(float(seconds))))
    except (TypeError, ValueError):
        return "00:00:00"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_progress_status_text(payload: dict, started_at: float, now: Optional[float] = None) -> str:
    """Build a calm one-line status string with elapsed time and ETA.

    ``started_at`` / ``now`` are wall-clock epoch seconds (``time.time()``).
    ETA is based on average time per processed row, so it is unknown until at
    least one row has been processed and grows more reliable with more rows.
    Contains no secrets.
    """
    import time as _time
    from datetime import datetime as _dt

    if now is None:
        now = _time.time()

    processed = int(payload.get("processed_rows", 0) or 0)
    selected = int(payload.get("selected_rows", 0) or 0)
    success = int(payload.get("success_count", 0) or 0)
    errors = int(payload.get("error_count", 0) or 0)
    current = str(payload.get("current_company_name") or "?")

    elapsed = max(0.0, now - started_at)
    parts = [
        f"Processed {processed}/{selected}",
        f"Success {success}",
        f"Errors {errors}",
        f"Current: {current}",
        f"Elapsed {format_duration(elapsed)}",
    ]

    if processed > 0 and selected > 0:
        avg = elapsed / processed
        remaining = avg * max(0, selected - processed)
        finish = _dt.fromtimestamp(now + remaining)
        parts.append(f"ETA {format_duration(remaining)}")
        parts.append(f"Finish around {finish.strftime('%H:%M')}")
    else:
        parts.append("ETA unknown")

    return " | ".join(parts)


def build_phase_progress_status_text(
    payload: dict, started_at: float, now: Optional[float] = None,
) -> str:
    """Status line for phased runs (the foreign-HQ-only mode).

    Renders the ``phase`` / ``phase_label`` / ``phase_processed`` /
    ``phase_total`` keys emitted by ``run_batch_foreign_hq_only``, with
    success/error counts when the phase provides them.  Payloads without phase
    info fall back to ``build_progress_status_text``, so this is safe as a
    single renderer for any progress payload.  Contains no secrets.
    """
    if "phase" not in payload:
        return build_progress_status_text(payload, started_at, now)

    import time as _time

    if now is None:
        now = _time.time()

    phase = int(payload.get("phase", 0) or 0)
    phase_count = int(payload.get("phase_count", 3) or 3)
    label = str(payload.get("phase_label") or "")
    done = int(payload.get("phase_processed", 0) or 0)
    total = int(payload.get("phase_total", 0) or 0)
    current = str(payload.get("current_company_name") or "?")
    elapsed = max(0.0, now - started_at)

    parts = [
        f"Phase {phase}/{phase_count}: {label}",
        f"Processed {done}/{total}",
    ]
    if "success_count" in payload or "error_count" in payload:
        parts.append(f"Success {int(payload.get('success_count', 0) or 0)}")
        parts.append(f"Errors {int(payload.get('error_count', 0) or 0)}")
    parts.append(f"Current: {current}")
    parts.append(f"Elapsed {format_duration(elapsed)}")
    return " | ".join(parts)


def format_local_time(ts: Optional[float] = None) -> str:
    """Local wall-clock time as HH:MM:SS — used for a 'last update' display."""
    import time as _time

    t = ts if ts is not None else _time.time()
    return _time.strftime("%H:%M:%S", _time.localtime(t))


PARALLEL_PROGRESS_NOTE_TEXT = (
    "Parallel progress updates when a row/chunk completes. Long rows may "
    "take several minutes."
)

RUN_BUTTON_NOTE_TEXT = (
    "For large full enrichment runs, progress may update slowly while "
    "external API calls are in flight."
)


def build_parallel_progress_status_text(
    payload: dict, started_at: float, now: Optional[float] = None,
) -> str:
    """Rich status line for parallel/chunk-based batch runs.

    Renders the payload shape emitted by ``run_batch_dataframe_parallel``:
    chunk counts, worker count, aggregated processed/success/error counts,
    the current phase/company from whichever chunk is still running, elapsed
    time, and a local-time "last update" stamp. Distinguishes a heartbeat
    wake-up (``payload["heartbeat"]`` — timeout elapsed with nothing finished
    yet) from a chunk-completion event, satisfying the "still running" /
    "chunk N completed" UI requirement without needing Streamlit itself.
    Contains no secrets — safe to call from the main Streamlit thread only
    (the payload itself is produced by a thread-safe aggregation in
    ``lead_prioritizer_batch_core``).
    """
    import time as _time

    if now is None:
        now = _time.time()

    chunks_total = int(payload.get("parallel_chunks_total", 0) or 0)
    chunks_done = int(payload.get("parallel_chunks_completed", 0) or 0)
    workers = int(payload.get("parallel_workers", 0) or 0)
    processed = int(payload.get("processed_rows", 0) or 0)
    selected = int(payload.get("selected_rows", 0) or 0)
    success = int(payload.get("success_count", 0) or 0)
    errors = int(payload.get("error_count", 0) or 0)
    current = str(payload.get("current_company_name") or "?")
    active_chunks = payload.get("active_chunks") or []
    elapsed = max(0.0, now - started_at)

    if payload.get("heartbeat"):
        headline = "Still running; waiting for worker results..."
    elif payload.get("chunk_index") is not None:
        outcome = "ok" if payload.get("chunk_success") else "FAILED"
        headline = (
            f"Chunk {payload.get('chunk_index')} "
            f"({payload.get('chunk_row_count')} rows) {outcome}"
        )
    else:
        headline = "Parallel batch running..."

    phase_label = next(
        (c.get("phase_label") for c in active_chunks if c.get("phase_label")), None)

    parts = [
        headline,
        f"Chunks {chunks_done}/{chunks_total}",
        f"Workers {workers}",
        f"Processed {processed}/{selected}",
        f"Success {success}",
        f"Errors {errors}",
    ]
    if phase_label:
        parts.append(f"Phase: {phase_label}")
    parts.append(f"Current: {current}")
    parts.append(f"Elapsed {format_duration(elapsed)}")
    parts.append(f"Last update {format_local_time(now)}")
    return " | ".join(parts)


def build_chunk_detail_line(chunk: dict) -> str:
    """One caption line for a still-running parallel chunk.

    Phase-based modes (the foreign-HQ-only family) report per-phase progress
    via ``phase_label`` / ``phase_processed`` / ``phase_total``; rendering
    those instead of the chunk's overall processed/selected counts avoids the
    confusing "168/168 rows — C5 adjudication" display where a chunk looked
    finished while C5 adjudication was still running. Chunks without phase
    info keep the original "processed/selected rows" format.
    """
    idx = chunk.get("chunk_index")
    label = str(chunk.get("phase_label") or "")
    phase_done = int(chunk.get("phase_processed") or 0)
    phase_total = int(chunk.get("phase_total") or 0)

    if label and phase_total > 0:
        line = f"Chunk {idx}: {label} {phase_done}/{phase_total}"
    else:
        line = (
            f"Chunk {idx}: {int(chunk.get('processed') or 0)}/"
            f"{int(chunk.get('selected') or 0)} rows"
        )
        if label:
            line += f" — {label}"
    company = chunk.get("current_company_name") or ""
    if company:
        line += f" — current: {company}"
    return line


# ---------------------------------------------------------------------------
# Lovable JSON export section (pure helpers — UI wiring lives in main())
# ---------------------------------------------------------------------------
#
# Integrates the existing standalone exporter (export_lead_prioritizer_to_
# lovable_json.py) and the GCS upload helper (lovable_gcs_upload.py) into this
# app so a run's output can go straight to Lovable JSON (and, optionally, GCS)
# without the manual "download Excel, open the separate exporter app, export,
# manually upload" workflow. No enrichment/scoring/HQ/C4/C5 logic lives here.

DEFAULT_COLD_CALLERS_TEXT = "Vanessa, Francesca, Lorenzo, Matteo"

# Run modes whose whole point is "only confirmed foreign-HQ rows get full
# enrichment" — the Lovable export's foreign-HQ-only toggle defaults to True
# for these, and False for every other run mode.
_FOREIGN_HQ_ONLY_EXPORT_DEFAULT_MODES = (
    FOREIGN_HQ_ONLY_MODE,
    NON_ENGLISH_FOREIGN_HQ_ONLY_MODE,
)


def parse_cold_callers(text) -> list[str]:
    """Parse a comma-separated cold-caller string into a clean name list.

    Blank entries (double commas, leading/trailing commas, whitespace-only
    names) are dropped; surrounding whitespace on each name is stripped.
    """
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def default_foreign_hq_only_export(run_mode: str) -> bool:
    """Default state for the "Foreign-HQ-only export" toggle.

    True for the two confirmed-foreign-HQ-only batch run modes, False for
    every other mode; always user-overridable in the UI.
    """
    return run_mode in _FOREIGN_HQ_ONLY_EXPORT_DEFAULT_MODES


def default_lovable_output_folder(export_country: str, timestamp) -> str:
    """Default local output folder: ``lovable_json_exports/<country>/<stamp>/``.

    Lives next to the Excel autosave output, not in a temp directory, so a
    user can find the generated JSON files afterwards.
    """
    country_part = sanitize_filename_part(export_country, fallback="export")
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    return str(Path("lovable_json_exports") / country_part / stamp)


def zip_directory_bytes(directory, filenames: list[str]) -> bytes:
    """Zip the given filenames from ``directory`` into an in-memory archive.

    Silently skips any filename that doesn't exist (defensive — the caller
    always passes filenames the exporter just wrote, but a partial export
    should still produce a downloadable zip of whatever *did* get written).
    """
    import io
    import zipfile

    directory = Path(directory)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in filenames:
            path = directory / filename
            if path.exists():
                zf.write(path, arcname=filename)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import io
    from datetime import datetime as _dt

    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Lead Prioritizer v2 Batch", layout="wide")
    st.title("Lead Prioritizer v2 Batch Excel App")
    st.markdown(
        "- Upload an Excel file and choose **HQ only** or **full** enrichment.\n"
        "- Download an enriched workbook (Enriched Leads, Evidence, Signals, "
        "Run Summary).\n"
        "- Intended for **synchronous local runs**.\n"
        "- Large async Anthropic Message Batch processing will be handled "
        "separately later."
    )

    # ── API keys ──────────────────────────────────────────────────────────────
    serper = get_secret_or_env(_SERPER_KEY_NAME)
    anthropic = get_secret_or_env(_ANTHROPIC_KEY_NAME)
    openai_key = get_secret_or_env(_OPENAI_KEY_NAME)
    with st.sidebar:
        st.header("API keys (secrets or environment)")
        st.write(f"{_SERPER_KEY_NAME}:", "✅ set" if serper else "❌ missing")
        st.write(f"{_ANTHROPIC_KEY_NAME}:", "✅ set" if anthropic else "❌ missing")
        st.write(f"{_OPENAI_KEY_NAME}:",
                 "✅ set" if openai_key else "➖ not set (only needed for the "
                 "experimental OpenAI provider)")
        st.caption(
            "Local secrets in `.streamlit/secrets.toml`, or environment "
            "variables. Key values are never shown or written to output."
        )
    keys_ok = bool(serper and anthropic)
    if not keys_ok:
        st.error(
            "Missing API key(s). Set SERPER_API_KEY and ANTHROPIC_API_KEY in "
            ".streamlit/secrets.toml or the environment. The run button is "
            "disabled until both are present."
        )

    # ── Upload ────────────────────────────────────────────────────────────────
    uploaded = st.file_uploader("Upload an .xlsx file", type=["xlsx"])
    if not uploaded:
        st.info("Upload an Excel workbook to begin.")
        return

    try:
        xls = pd.ExcelFile(uploaded)
    except Exception as exc:
        st.error(f"Could not read workbook: {exc}")
        return

    sheet_names = list(xls.sheet_names)
    sheet = sheet_names[0] if len(sheet_names) == 1 else st.selectbox(
        "Sheet", sheet_names)

    try:
        df = xls.parse(sheet)
    except Exception as exc:
        st.error(f"Could not read sheet {sheet!r}: {exc}")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Sheet", sheet)
    c2.metric("Rows", len(df))
    c3.metric("Columns", len(df.columns))
    st.dataframe(df.head(5), use_container_width=True)

    # ── Column mapping ────────────────────────────────────────────────────────
    st.subheader("Column mapping")
    cols = list(df.columns)

    def _index_of(default):
        return cols.index(default) if default in cols else 0

    company_col = st.selectbox(
        "Company name column", cols,
        index=_index_of(resolve_default_column(cols, COMPANY_CANDIDATES)))
    domain_col = st.selectbox(
        "Domain column", cols,
        index=_index_of(resolve_default_column(cols, DOMAIN_CANDIDATES)))

    country_default = resolve_default_column(cols, COUNTRY_CANDIDATES)
    country_options = ["(None)"] + cols
    country_choice = st.selectbox(
        "Input country column (optional)", country_options,
        index=(country_options.index(country_default) if country_default in country_options else 0))
    input_country_column = None if country_choice == "(None)" else country_choice

    default_country_choice = st.selectbox(
        "Default input country",
        [DEFAULT_COUNTRY_PLACEHOLDER] + SUPPORTED_DEFAULT_INPUT_COUNTRIES,
        index=0,
        help="Used as the fallback input_country when the row's own "
             "input_country column is blank. Must be chosen explicitly.")
    default_country, default_country_error = resolve_default_input_country(default_country_choice)
    if default_country_error:
        st.error(default_country_error)

    # ── Run mode ──────────────────────────────────────────────────────────────
    st.subheader("Run mode")
    mode_label = st.radio("Mode", MODE_LABELS, index=0)
    run_mode = mode_label_to_core_mode(mode_label)
    if run_mode == FOREIGN_HQ_ONLY_MODE:
        st.caption(FOREIGN_HQ_ONLY_HELP_TEXT)
    elif run_mode == NON_ENGLISH_FOREIGN_HQ_ONLY_MODE:
        st.caption(NON_ENGLISH_FOREIGN_HQ_ONLY_HELP_TEXT)

    # ── AI provider (experimental) ────────────────────────────────────────────
    st.subheader("AI provider")
    provider_label = st.selectbox(
        "AI provider for HQ interpretation", AI_PROVIDER_LABELS, index=0,
        help="Anthropic only is the production default. The OpenAI and "
             "compare options (including the nano-vs-mini triple compare) "
             "are experimental and only available for the standard batch "
             "modes without C5.")
    ai_provider_mode = ai_provider_label_to_mode(provider_label)
    openai_model = DEFAULT_OPENAI_MODEL
    if ai_provider_mode in ("openai", "compare"):
        openai_model = st.selectbox(
            "OpenAI model", list(SUPPORTED_OPENAI_MODELS),
            index=list(SUPPORTED_OPENAI_MODELS).index(DEFAULT_OPENAI_MODEL))
    if ai_provider_mode == "compare":
        st.warning(COMPARE_MODE_WARNING_TEXT)
    if ai_provider_mode == "compare_triple":
        st.caption(
            f"Fixed models for this mode: OpenAI nano = **{DEFAULT_OPENAI_NANO_MODEL}**, "
            f"OpenAI mini = **{DEFAULT_OPENAI_MINI_MODEL}**.")
        st.warning(COMPARE_TRIPLE_MODE_WARNING_TEXT)

    # ── Row controls ──────────────────────────────────────────────────────────
    st.subheader("Rows")
    rc1, rc2 = st.columns(2)
    start_row = rc1.number_input("Start row", min_value=0, value=0, step=1)
    row_limit = rc2.number_input("Row limit (0 = all remaining)", min_value=0, value=0, step=1)
    stop_on_error = st.checkbox("Stop on first row error", value=False)
    include_raw_ai_json = st.checkbox("Include raw AI JSON", value=False)

    # ── Autosave (output workbook to disk when the run completes) ─────────────
    autosave_enabled = st.checkbox(
        "Autosave output workbook when run completes", value=False)
    autosave_dir = DEFAULT_AUTOSAVE_DIR
    if autosave_enabled:
        source_folder_text = st.text_input(
            "Input/source folder for outputs", value="",
            help="Because browsers do not expose the uploaded file path, paste "
                 "the folder where this country input file lives. Outputs "
                 "will default to this folder.")
        autosave_dir = st.text_input(
            "Autosave directory", value=str(resolve_batch_output_dir(source_folder_text)))
        if not source_folder_text.strip():
            st.caption(
                "Streamlit cannot infer the original upload folder "
                "automatically; paste the source folder if you want outputs "
                "saved next to your input files."
            )
        st.caption(AUTOSAVE_HELP_TEXT)

    # ── Parallel processing ───────────────────────────────────────────────────
    st.subheader("Parallel processing")
    parallel_enabled = st.checkbox("Enable parallel chunk processing", value=True)
    parallel_workers = 1
    if parallel_enabled:
        parallel_workers = int(st.selectbox(
            "Parallel workers", PARALLEL_WORKER_CHOICES,
            index=PARALLEL_WORKER_CHOICES.index(MAX_PARALLEL_WORKERS)))
        st.caption(PARALLEL_HELP_TEXT)
        if parallel_workers > 1:
            st.warning(PARALLEL_WARNING_TEXT)
        st.caption(PARALLEL_PROGRESS_NOTE_TEXT)

    selected_count = count_selected_rows(len(df), int(start_row), int(row_limit))
    st.caption(f"Selected rows: **{selected_count}**")

    big_run_ok = True
    if selected_count > CONFIRM_THRESHOLD:
        st.warning(
            f"{selected_count} selected rows exceeds {CONFIRM_THRESHOLD}. Full "
            "mode makes multiple Serper + Anthropic calls per row (cost and time)."
        )
        big_run_ok = st.checkbox(
            "I understand this may use many API calls and want to run this batch",
            value=True)

    # ── C5 Sonnet HQ adjudication (optional, country-agnostic) ────────────────
    st.subheader("C5 Sonnet HQ adjudication")
    c5_enabled = st.checkbox("Use C5 Sonnet adjudication", value=True)
    c5_scoring_behavior = "conservative_adjustment"
    c5_scope = "score_3_or_manual_review"
    c5_model_tier = "sonnet"
    c5_model_override = ""
    c5_model_used = ""
    c5_model_error = None
    c5_opus_confirm = False
    c5_block_reason = ""
    if c5_enabled:
        c5_scoring_behavior = st.selectbox(
            "C5 scoring behavior", list(C5_SCORING_BEHAVIORS),
            index=list(C5_SCORING_BEHAVIORS).index("conservative_adjustment"),
            help="append_only: add C5 fields only. conservative_adjustment: may "
                 "confirm/downgrade existing score-3 positives; never auto-upgrades "
                 "score-0 rows.")
        c5_scope = st.selectbox(
            "Rows to send to C5", list(C5_SCOPES),
            index=list(C5_SCOPES).index("score_3_or_manual_review"))
        c5_model_tier = st.selectbox("C5 model tier", list(C5_MODEL_TIER_CHOICES), index=0)
        if c5_model_tier == "sonnet":
            st.caption(f"Sonnet default model: **{DEFAULT_SONNET_ADJUDICATION_MODEL}**")
        c5_model_override = st.text_input(
            "C5 explicit model override (optional)", value="",
            help="Overrides the tier. Required for the opus tier.")
        c5_model_used, c5_model_error = resolve_c5_model(c5_model_tier, c5_model_override)
        if c5_model_tier == "opus":
            st.warning(_OPUS_WARNING)
            rl = int(row_limit)
            if rl == 0 or rl > _C5_OPUS_ROW_CAP:
                c5_opus_confirm = st.checkbox(
                    "I understand Opus is expensive and want to continue", value=False)
        if c5_model_error:
            c5_block_reason = c5_model_error
        elif c5_model_tier == "opus":
            _guard = check_opus_guardrail(c5_model_tier, int(row_limit), c5_opus_confirm)
            if _guard:
                c5_block_reason = _guard
        if c5_block_reason:
            st.error(c5_block_reason)

    ai_provider_error = validate_ai_provider_run(
        ai_provider_mode, run_mode, c5_enabled, openai_key)
    if ai_provider_error:
        st.error(ai_provider_error)

    run_disabled = (not keys_ok) or (not big_run_ok) or selected_count == 0 \
        or bool(c5_block_reason) or bool(default_country_error) \
        or bool(ai_provider_error)

    # ── Run ─────────────────────────────────────────────────────────────────
    st.caption(RUN_BUTTON_NOTE_TEXT)
    if st.button("Run batch enrichment", type="primary", disabled=run_disabled):
        if default_country_error:
            st.error(default_country_error)
            return
        config = BatchRunConfig(
            company_name_column=company_col,
            domain_column=domain_col,
            input_country_column=input_country_column,
            default_input_country=default_country,
            run_mode=run_mode,
            start_row=int(start_row),
            row_limit=int(row_limit),
            continue_on_error=not stop_on_error,
            include_raw_ai_json=include_raw_ai_json,
            # "compare" / "compare_triple" run their own dedicated path below;
            # the config itself stays anthropic unless OpenAI-only was
            # explicitly selected.
            ai_provider="openai" if ai_provider_mode == "openai" else "anthropic",
            ai_model=openai_model if ai_provider_mode == "openai" else "",
        )
        import time as _time

        # ── Experimental provider comparison (dedicated small-run path) ───────
        if ai_provider_mode in ("compare", "compare_triple"):
            selected_rows_df = select_batch_rows(df, config)
            run_timestamp = _dt.now()
            is_triple = ai_provider_mode == "compare_triple"
            run_times = 3 if is_triple else 2
            cost_providers = TRIPLE_COST_PROVIDERS if is_triple else TWO_WAY_COST_PROVIDERS
            with st.spinner(
                f"Comparing providers on {len(selected_rows_df)} row(s) — "
                f"each row runs {run_times} times..."
            ):
                if is_triple:
                    comparison_df = run_triple_comparison(
                        selected_rows_df,
                        company_column=company_col,
                        domain_column=domain_col,
                        country_column=input_country_column or "",
                        default_input_country=default_country,
                        openai_nano_model=DEFAULT_OPENAI_NANO_MODEL,
                        openai_mini_model=DEFAULT_OPENAI_MINI_MODEL,
                        serper_api_key=serper,
                        anthropic_api_key=anthropic,
                        openai_api_key=openai_key,
                    )
                else:
                    comparison_df = run_comparison(
                        selected_rows_df,
                        company_column=company_col,
                        domain_column=domain_col,
                        country_column=input_country_column or "",
                        default_input_country=default_country,
                        openai_model=openai_model,
                        serper_api_key=serper,
                        anthropic_api_key=anthropic,
                        openai_api_key=openai_key,
                    )
            if is_triple:
                st.success(f"Compared {len(comparison_df)} row(s) across three providers/models.")
            else:
                matches = int(comparison_df["classification_match"].sum())
                st.success(
                    f"Compared {len(comparison_df)} row(s) across both providers. "
                    f"Classification matches: {matches}/{len(comparison_df)}."
                )
            st.dataframe(comparison_df, use_container_width=True)

            # ── Cost summary (audit only — never affects scoring) ─────────────
            cost_summary_df = build_cost_summary_dataframe(comparison_df, cost_providers)
            cost_totals = build_cost_totals(
                build_provider_cost_rows(comparison_df, cost_providers), len(comparison_df))
            st.subheader("Cost summary")
            st.dataframe(cost_summary_df, use_container_width=True)
            totals_cols = st.columns(len(cost_providers))
            for col, (label, _prefix) in zip(totals_cols, cost_providers):
                total = cost_totals.get(f"total_{label}_cost_usd")
                col.metric(f"Total {label} cost",
                          f"${total:.4f}" if total is not None else "n/a")
            st.caption(
                "Estimated cost per 100 / 1,000 / 10,000 companies (combined "
                "across providers shown above, based on this run's average "
                "cost per company): "
                f"${cost_totals['estimated_cost_per_100_companies_usd']:.2f} / "
                f"${cost_totals['estimated_cost_per_1000_companies_usd']:.2f} / "
                f"${cost_totals['estimated_cost_per_10000_companies_usd']:.2f}"
                if cost_totals.get("estimated_cost_per_100_companies_usd") is not None
                else "Estimated cost per 100 / 1,000 / 10,000 companies: "
                     "unavailable (no priced provider in this run)."
            )

            st.download_button(
                "Download provider comparison workbook",
                data=build_provider_comparison_workbook_bytes(comparison_df, cost_summary_df),
                file_name=build_comparison_download_filename(run_timestamp),
                mime=("application/vnd.openxmlformats-officedocument"
                      ".spreadsheetml.sheet"),
            )
            return

        progress_bar = st.progress(0.0)
        status = st.empty()
        started_at = _time.time()

        def _on_progress(payload: dict) -> None:
            selected = int(payload.get("selected_rows", 0) or 0)
            processed = int(payload.get("processed_rows", 0) or 0)
            frac = (processed / selected) if selected else 0.0
            progress_bar.progress(min(1.0, max(0.0, frac)))
            status.info(build_progress_status_text(payload, started_at))

        use_parallel = parallel_enabled and parallel_workers > 1
        run_timestamp = _dt.now()
        run_stamp = run_timestamp.strftime("%Y%m%d_%H%M%S")
        run_dir = None
        chunk_files: dict = {}
        chunk_reports: list = []

        if use_parallel:
            # Chunked parallel run: each chunk goes through the exact same code
            # path as a sequential smaller batch (incl. FHO mode and C5 config).
            if autosave_enabled:
                try:
                    run_folder_name = make_parallel_run_folder_name(
                        default_country, run_mode, run_timestamp)
                    run_dir = resolve_autosave_directory(autosave_dir) / run_folder_name
                    run_dir.mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    st.error(
                        f"Could not create autosave run directory: {exc}. "
                        "Chunk checkpoints are disabled for this run."
                    )
                    run_dir = None

            def _save_chunk(report: dict, chunk_tables: dict) -> None:
                if run_dir is None:
                    return
                try:
                    fname = f"chunk_{int(report['chunk_index']):03d}_output.xlsx"
                    (run_dir / fname).write_bytes(build_excel_workbook_bytes(chunk_tables))
                    chunk_files[int(report["chunk_index"])] = fname
                except Exception as exc:
                    st.warning(f"Chunk checkpoint save failed: {exc}")

            chunk_detail = st.empty()

            def _on_chunk_progress(payload: dict) -> None:
                # Runs on the main Streamlit thread only — run_batch_dataframe_parallel
                # collects progress from worker threads into a lock-protected shared
                # snapshot and invokes this callback itself from the main thread
                # (both on heartbeat wake-ups and chunk completions), so it's always
                # safe to call st.* here.
                selected = int(payload.get("selected_rows", 0) or 0)
                processed = int(payload.get("processed_rows", 0) or 0)
                frac = (processed / selected) if selected else 0.0
                progress_bar.progress(min(1.0, max(0.0, frac)))
                status.info(build_parallel_progress_status_text(payload, started_at))

                if payload.get("chunk_index") is not None and payload.get("chunk_success") is False:
                    st.warning(
                        f"Chunk {payload.get('chunk_index')} failed: "
                        f"{payload.get('chunk_error') or 'unknown error'}"
                    )

                active = payload.get("active_chunks") or []
                if active:
                    chunk_detail.caption(
                        "  \n".join(build_chunk_detail_line(c) for c in active))
                else:
                    chunk_detail.empty()

            with st.spinner(f"Running parallel batch ({parallel_workers} workers)..."):
                tables = run_batch_dataframe_parallel(
                    df, config, serper, anthropic,
                    workers=parallel_workers,
                    c5_enabled=c5_enabled,
                    c5_scoring_behavior=c5_scoring_behavior,
                    c5_scope=c5_scope,
                    c5_model_used=c5_model_used,
                    c5_model_tier=c5_model_tier,
                    openai_api_key=openai_key,
                    progress_callback=_on_chunk_progress,
                    chunk_result_callback=_save_chunk if run_dir is not None else None,
                )
            progress_bar.progress(1.0)

            chunk_reports = tables.get("chunk_reports") or []
            for report in chunk_reports:
                if report.get("success") is False:
                    st.error(
                        f"Chunk {report['chunk_index']} failed: {report['error']} — "
                        "its rows were added as error rows; successful chunks are "
                        "included in the combined output."
                    )
        elif run_mode in (FOREIGN_HQ_ONLY_MODE, NON_ENGLISH_FOREIGN_HQ_ONLY_MODE):
            # HQ+C4+optional-C5 screening and the confirmed-only full-enrichment
            # pass both happen inside the mode function; C5 must not be
            # re-applied afterward here (it already ran as part of the decision).
            def _on_phase_progress(payload: dict) -> None:
                phase = int(payload.get("phase", 1) or 1)
                phase_count = int(payload.get("phase_count", 3) or 3)
                total = int(payload.get("phase_total", 0) or 0)
                done = int(payload.get("phase_processed", 0) or 0)
                within = min(1.0, done / total) if total else 0.0
                overall = ((phase - 1) + within) / phase_count
                progress_bar.progress(min(1.0, max(0.0, overall)))
                status.info(build_phase_progress_status_text(payload, started_at))

            _mode_fn = (
                run_batch_non_english_foreign_hq_only
                if run_mode == NON_ENGLISH_FOREIGN_HQ_ONLY_MODE
                else run_batch_foreign_hq_only
            )
            with st.spinner(
                "Running HQ screening, optional C5 adjudication, and "
                "confirmed-only full enrichment..."
            ):
                tables = _mode_fn(
                    df, config, serper, anthropic,
                    c5_enabled=c5_enabled,
                    c5_scoring_behavior=c5_scoring_behavior,
                    c5_scope=c5_scope,
                    c5_model_used=c5_model_used,
                    c5_model_tier=c5_model_tier,
                    progress_callback=_on_phase_progress,
                )
            progress_bar.progress(1.0)
        else:
            with st.spinner("Running batch enrichment..."):
                tables = run_batch_dataframe(
                    df, config, serper, anthropic,
                    progress_callback=_on_progress,
                    openai_api_key=openai_key)

            progress_bar.progress(1.0)

            # ── Optional C5 adjudication (after normal batch processing) ──────────
            c5_counts = {}
            if c5_enabled:
                c5_bar = st.progress(0.0)
                c5_status = st.empty()

                def _on_c5_progress(payload: dict) -> None:
                    sel = int(payload.get("c5_selected", 0) or 0)
                    dn = int(payload.get("c5_processed", 0) or 0)
                    c5_bar.progress(min(1.0, dn / sel) if sel else 1.0)
                    c5_status.info(
                        f"C5 {dn}/{sel}: {payload.get('current_company_name', '')}")

                with st.spinner("Running C5 Sonnet adjudication..."):
                    c5_rows, c5_counts = apply_c5_adjudication(
                        tables["enriched_leads"],
                        anthropic_api_key=anthropic,
                        model_used=c5_model_used,
                        model_tier=c5_model_tier,
                        scoring_behavior=c5_scoring_behavior,
                        scope=c5_scope,
                        include_raw=include_raw_ai_json,
                        progress_callback=_on_c5_progress,
                    )
                c5_bar.progress(1.0)
                tables["enriched_leads"] = pd.DataFrame(c5_rows)

            # Extend Run Summary with C5 settings/counts (always records enabled flag).
            tables["run_summary"] = add_c5_summary_fields(
                tables["run_summary"],
                c5_enabled=c5_enabled,
                c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
                c5_scope=c5_scope if c5_enabled else "",
                c5_model_tier=c5_model_tier if c5_enabled else "",
                c5_model_used=c5_model_used if c5_enabled else "",
                counts=c5_counts,
            )

        data = build_excel_workbook_bytes(tables)

        _total_elapsed = format_duration(_time.time() - started_at)
        _summary = tables["run_summary"].iloc[0].to_dict() if len(tables["run_summary"]) else {}
        status.success(
            f"Completed {_summary.get('processed_rows', 0)} rows in {_total_elapsed} "
            f"(success {_summary.get('success_count', 0)}, errors {_summary.get('error_count', 0)})."
        )
        st.session_state["v2_batch_output_bytes"] = data
        st.session_state["v2_batch_tables"] = tables
        st.session_state["v2_batch_mode"] = run_mode
        st.session_state["v2_batch_country"] = default_country
        st.session_state["v2_batch_run_timestamp"] = run_timestamp

        # ── Optional autosave (same bytes as the download button) ─────────────
        if autosave_enabled:
            if use_parallel and run_dir is not None:
                # Parallel run: combined workbook + manifest in the run dir,
                # next to the chunk checkpoint workbooks saved during the run.
                try:
                    combined_name = make_batch_output_filename(
                        default_country, run_mode, run_timestamp)
                    (run_dir / combined_name).write_bytes(data)
                    _summary_row = (tables["run_summary"].iloc[0].to_dict()
                                    if len(tables["run_summary"]) else {})
                    write_parallel_run_manifest(run_dir, {
                        "run_mode": run_mode,
                        "selected_rows": int(_summary_row.get("selected_rows", 0) or 0),
                        "workers": int(parallel_workers),
                        "chunk_count": len(chunk_reports),
                        "chunks": [
                            {**report,
                             "output_file": chunk_files.get(report["chunk_index"], "")}
                            for report in chunk_reports
                        ],
                        "combined_output_file": combined_name,
                    })
                    st.success(
                        "Autosaved combined workbook, chunk checkpoints and "
                        f"run_manifest.json to: {run_dir.resolve()}"
                    )
                except Exception as exc:
                    st.error(
                        f"Autosave failed: {exc}. "
                        "You can still use the download button below."
                    )
            else:
                try:
                    saved_path = autosave_output_workbook(
                        data, autosave_dir, run_mode,
                        country=default_country, now=run_timestamp)
                    st.success(f"Autosaved output workbook to: {saved_path}")
                except Exception as exc:
                    st.error(
                        f"Autosave failed: {exc}. "
                        "You can still use the download button below."
                    )

    # ── Output ────────────────────────────────────────────────────────────────
    tables = st.session_state.get("v2_batch_tables")
    data = st.session_state.get("v2_batch_output_bytes")
    if tables is not None and data is not None:
        st.subheader("Results")
        summary = tables["run_summary"].iloc[0].to_dict() if len(tables["run_summary"]) else {}
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Processed rows", summary.get("processed_rows", 0))
        m2.metric("Success", summary.get("success_count", 0))
        m3.metric("Errors", summary.get("error_count", 0))
        m4.metric("Run mode", summary.get("run_mode", st.session_state.get("v2_batch_mode", "")))

        enriched = tables["enriched_leads"]
        st.markdown("**Enriched Leads (preview)**")
        st.dataframe(enriched.head(20), use_container_width=True)

        if "run_success" in enriched.columns:
            errors = enriched[enriched["run_success"] == False]  # noqa: E712
            if len(errors):
                st.markdown("**Rows with errors**")
                _wanted = ["source_index", "company_name", "domain", "run_error"]
                _err_cols = [c for c in _wanted if c in errors.columns]
                st.dataframe(errors[_err_cols] if _err_cols else errors,
                             use_container_width=True)

        _dl_country = st.session_state.get("v2_batch_country") or default_country
        _dl_mode = st.session_state.get("v2_batch_mode", run_mode)
        _dl_timestamp = st.session_state.get("v2_batch_run_timestamp") or _dt.now()
        st.download_button(
            "⬇️ Download enriched workbook",
            data=data,
            file_name=make_batch_output_filename(_dl_country, _dl_mode, _dl_timestamp),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        # ── Lovable JSON export ─────────────────────────────────────────────
        st.subheader("Lovable JSON export")
        st.caption(
            "Optional: export this run straight to Lovable Company Hub JSON, "
            "download it as a zip, and (only if you choose to) upload it to "
            "Google Cloud Storage. Local Excel download above always works "
            "regardless of anything below — nothing here runs automatically."
        )

        lc1, lc2 = st.columns(2)
        lovable_export_country = lc1.text_input(
            "Export country", value=_dl_country, key="lovable_export_country")
        lovable_cold_callers_raw = lc2.text_input(
            "Cold callers (comma-separated)", value=DEFAULT_COLD_CALLERS_TEXT,
            key="lovable_cold_callers")

        lc3, lc4 = st.columns(2)
        lovable_foreign_hq_only = lc3.checkbox(
            "Foreign-HQ-only export",
            value=default_foreign_hq_only_export(_dl_mode),
            key="lovable_foreign_hq_only",
            help="Only export rows with a detected/confirmed foreign HQ.")
        lovable_bucket_size = lc4.number_input(
            "Bucket size", min_value=1, value=500, step=50, key="lovable_bucket_size")

        lovable_output_dir = st.text_input(
            "Local output folder",
            value=default_lovable_output_folder(lovable_export_country, _dl_timestamp),
            key="lovable_output_dir")

        if st.button("Export to Lovable JSON", key="lovable_export_button"):
            cold_callers = parse_cold_callers(lovable_cold_callers_raw)
            if not (lovable_export_country or "").strip():
                st.error("Export country is required.")
            elif not cold_callers:
                st.error("At least one cold caller is required.")
            else:
                try:
                    with st.spinner("Exporting to Lovable JSON..."):
                        manifest = export_batch_output_tables_to_lovable_json(
                            tables, lovable_output_dir, lovable_export_country,
                            cold_callers,
                            foreign_hq_only=lovable_foreign_hq_only,
                            bucket_size=int(lovable_bucket_size),
                        )
                    st.session_state["lovable_manifest"] = manifest
                    st.session_state["lovable_manifest_output_dir"] = lovable_output_dir
                except Exception as exc:
                    st.error(f"Lovable JSON export failed: {exc}")

        manifest = st.session_state.get("lovable_manifest")
        manifest_output_dir = st.session_state.get("lovable_manifest_output_dir")
        if manifest is not None and manifest_output_dir:
            validation = manifest.get("validation_summary", {}) or {}
            validation_ok = (
                validation.get("status") == "ok"
                and int(validation.get("structural_errors", 0) or 0) == 0
            )

            st.markdown("**Export result**")
            v1, v2, v3, v4 = st.columns(4)
            v1.metric("Rows exported", manifest.get("rows_exported", 0))
            v2.metric("Skipped rows excluded", manifest.get("skipped_rows_excluded", 0))
            v3.metric("Foreign-HQ rows exported", manifest.get("foreign_hq_rows_exported", 0))
            v4.metric("Bucket count", manifest.get("bucket_count", 0))
            st.write("Foreign-HQ-only export:", manifest.get("foreign_hq_only"))
            st.write("Caller distribution:", manifest.get("caller_distribution", {}))
            if validation_ok:
                st.success(f"Validation status: {validation.get('status')}")
            else:
                st.error(
                    f"Validation status: {validation.get('status')} — "
                    f"structural_errors: {validation.get('structural_errors', 0)}"
                )
            warnings = manifest.get("warnings") or []
            if warnings:
                with st.expander(f"Warnings ({len(warnings)})"):
                    for warning in warnings:
                        st.write("-", warning)

            output_filenames = sorted({Path(p).name for p in manifest.get("output_files", [])})
            zip_bytes = zip_directory_bytes(manifest_output_dir, output_filenames)
            st.download_button(
                "⬇️ Download Lovable JSON (zip)",
                data=zip_bytes,
                file_name=f"lovable_json_{sanitize_filename_part(lovable_export_country)}.zip",
                mime="application/zip",
                key="lovable_zip_download",
            )

            st.markdown("---")
            st.markdown("**Optional Google Cloud Storage upload**")
            st.caption(
                "Experimental / manual only: uses your local gcloud CLI. "
                "Nothing uploads until you explicitly check the box and "
                "click the upload button below."
            )
            lovable_gcs_enabled = st.checkbox(
                "Upload generated Lovable JSON to Google Cloud Storage",
                value=False, key="lovable_gcs_enabled")
            if lovable_gcs_enabled:
                gcloud_info = check_gcloud_available()
                if not gcloud_info["available"]:
                    st.warning(
                        "Neither gcloud nor gsutil was found on PATH. Install "
                        "or authenticate the Google Cloud SDK before uploading."
                    )
                else:
                    env_info = describe_gcloud_environment()
                    st.caption(
                        f"Detected CLI: `{gcloud_info['tool']}`"
                        + (f" ({gcloud_info['version']})" if gcloud_info["version"] else "")
                        + (f" — account: {env_info['account']}" if env_info["account"] else "")
                        + (f" — project: {env_info['project']}" if env_info["project"] else "")
                    )

                _default_bucket = get_secret_or_env(_GCS_BUCKET_NAME_KEY) or DEFAULT_GCS_BUCKET
                _base_prefix = get_secret_or_env(_GCS_BASE_PREFIX_KEY)
                _default_country_folder = country_folder_slug(lovable_export_country)
                if _base_prefix:
                    _default_country_folder = normalize_gcs_prefix(
                        f"{_base_prefix}/{_default_country_folder}")

                gc1, gc2 = st.columns(2)
                gcs_bucket = gc1.text_input(
                    "GCS bucket", value=_default_bucket, key="lovable_gcs_bucket")
                gcs_country_folder = gc2.text_input(
                    "GCS prefix/path (e.g. <country>)",
                    value=_default_country_folder,
                    key="lovable_gcs_country_folder")
                gcs_run_folder = st.text_input(
                    "GCS run folder",
                    value=default_gcs_run_folder(_dl_mode, _dl_timestamp),
                    key="lovable_gcs_run_folder")

                gu1, gu2 = st.columns(2)
                upload_current = gu1.checkbox(
                    "Overwrite <prefix>/current/", value=True,
                    key="lovable_gcs_upload_current")
                upload_archive = gu2.checkbox(
                    "Archive to <prefix>/runs/<run_folder>/", value=True,
                    key="lovable_gcs_upload_archive")

                override_invalid = False
                if not validation_ok:
                    st.error(
                        "Export validation did not pass — upload is blocked "
                        "by default."
                    )
                    override_invalid = st.checkbox(
                        "Override and upload anyway (not recommended)",
                        value=False, key="lovable_gcs_override")

                gcs_bucket_norm = gcs_bucket.strip()
                gcs_country_folder_norm = normalize_gcs_prefix(gcs_country_folder)
                gcs_run_folder_norm = normalize_gcs_prefix(gcs_run_folder)
                missing_fields = not gcs_bucket_norm or not gcs_country_folder_norm
                if missing_fields:
                    st.error("GCS bucket and prefix/path are both required.")

                upload_disabled = not (upload_current or upload_archive) or (
                    not validation_ok and not override_invalid) or missing_fields
                if st.button("Upload JSON to GCS",
                            key="lovable_gcs_upload_button", disabled=upload_disabled):
                    jobs = build_upload_plan(
                        manifest_output_dir, output_filenames, gcs_bucket_norm,
                        gcs_country_folder_norm, gcs_run_folder_norm,
                        upload_current=upload_current, upload_archive=upload_archive,
                    )
                    with st.spinner("Uploading to Google Cloud Storage..."):
                        results = run_upload_plan(jobs)
                    failures = [r for r in results if not r["success"]]
                    if failures:
                        st.error(f"{len(failures)} of {len(results)} uploads failed.")
                        for r in failures:
                            st.code(f"{r['destination']}: {r.get('error') or r.get('stderr') or ''}")
                    else:
                        st.success(f"Uploaded {len(results)} file(s) to Google Cloud Storage.")
                    if upload_current:
                        st.markdown("**Public URLs**")
                        for filename in output_filenames:
                            st.write(gcs_public_url(
                                gcs_bucket_norm, gcs_country_folder_norm, filename))
            else:
                st.caption(
                    "GCS upload is off by default. Check the box above to "
                    "upload this export's JSON files."
                )


if __name__ == "__main__":
    main()
