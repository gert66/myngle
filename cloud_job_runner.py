"""Cloud Run Jobs worker entrypoint for the mYngle Lead Prioritizer.

Reads (or downloads) one input Excel, slices out this task's contiguous row
block, and calls the existing ``lead_prioritizer_batch_cli.py`` (Lead
Prioritizer v2 — Serper + Anthropic + Firecrawl) via subprocess to do the
actual enrichment/scoring — nothing here re-implements that logic. The
resulting part output and a status JSON are written to GCS (``gs://...``
paths) or to local paths, so the same script also runs as a local smoke test.

Company/domain/(optional) input-country columns are resolved the same way
the Streamlit batch app resolves them (see
``lead_prioritizer_batch_app.resolve_default_column``) unless overridden via
COMPANY_COLUMN/DOMAIN_COLUMN/INPUT_COUNTRY_COLUMN (env or
--company-column/--domain-column/--input-country-column), so a dataset that
already works there needs no extra cloud-specific config.

Cloud usage (inside the container, values injected by Cloud Run Jobs):
    python cloud_job_runner.py
    (INPUT_GCS_URI, OUTPUT_GCS_DIR, RUN_ID, ANTHROPIC_API_KEY, SERPER_API_KEY,
     FIRECRAWL_API_KEY, CLOUD_RUN_TASK_INDEX, CLOUD_RUN_TASK_COUNT come from env)

Local smoke test:
    python cloud_job_runner.py --input in.xlsx --output-dir .\\out \\
        --task-index 0 --task-count 10 --max-rows 5
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

ROW_INDEX_COL = "_cloud_original_row_index"

REPO_ROOT = Path(__file__).resolve().parent
BATCH_CLI_SCRIPT = REPO_ROOT / "lead_prioritizer_batch_cli.py"


# ── gs:// / local path helpers ────────────────────────────────────────────

def is_gcs_uri(path: str) -> bool:
    return str(path or "").startswith("gs://")


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    without_scheme = uri[len("gs://"):]
    bucket, _, blob = without_scheme.partition("/")
    if not bucket or not blob:
        raise ValueError(f"Invalid gs:// URI: {uri}")
    return bucket, blob


def join_path(base: str, *parts: str) -> str:
    """Join a gs:// or local dir with sub-path parts using '/'.

    Works for both URI-style and local paths; local writers still go
    through Path(...) before touching disk, so this is safe on Windows too.
    """
    base_norm = str(base or "").rstrip("/")
    return "/".join([base_norm, *parts])


def _gcs_client():
    # Imported lazily so purely-local runs don't need the dependency installed.
    from google.cloud import storage
    return storage.Client()


def download_gcs_file(uri: str, local_path: Path) -> None:
    bucket_name, blob_name = parse_gcs_uri(uri)
    blob = _gcs_client().bucket(bucket_name).blob(blob_name)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(local_path))


def upload_gcs_file(local_path: Path, uri: str) -> None:
    bucket_name, blob_name = parse_gcs_uri(uri)
    blob = _gcs_client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_filename(str(local_path))


def gcs_blob_exists(uri: str) -> bool:
    bucket_name, blob_name = parse_gcs_uri(uri)
    return _gcs_client().bucket(bucket_name).blob(blob_name).exists()


def list_gcs_uris(prefix_uri: str, suffix: str = "") -> list[str]:
    """List gs:// URIs of blobs under prefix_uri, optionally filtered by suffix."""
    without_scheme = prefix_uri[len("gs://"):]
    bucket_name, _, prefix = without_scheme.partition("/")
    client = _gcs_client()
    blobs = client.bucket(bucket_name).list_blobs(prefix=prefix)
    return [
        f"gs://{bucket_name}/{blob.name}"
        for blob in blobs
        if not suffix or blob.name.endswith(suffix)
    ]


def path_exists(uri: str) -> bool:
    if is_gcs_uri(uri):
        return gcs_blob_exists(uri)
    return Path(uri).exists()


def upload_output_file(local_path: Path, dest: str) -> None:
    if is_gcs_uri(dest):
        upload_gcs_file(local_path, dest)
    else:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest_path)


def write_status_json(dest: str, payload: dict) -> None:
    text = json.dumps(payload, indent=2, default=str)
    if is_gcs_uri(dest):
        bucket_name, blob_name = parse_gcs_uri(dest)
        blob = _gcs_client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_string(text, content_type="application/json")
    else:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(text, encoding="utf-8")


_CHECKPOINT_UPLOAD_INTERVAL_SECONDS = 20


def _checkpoint_uploader_loop(
    local_path: Path, dest_uri: str, stop_event: threading.Event, state: dict,
) -> None:
    """Background loop: while the batch subprocess runs, periodically upload
    the local checkpoint file (written by ``lead_prioritizer_batch_cli.py``
    via ``--checkpoint-path``, see ``batch_checkpoint.py``) to GCS if it
    changed since the last upload -- so an OOM kill or crash mid-shard still
    leaves *something* recoverable in GCS, instead of losing every row this
    task had already processed.

    Runs in a daemon thread; any error is swallowed (checkpoint uploads are
    best-effort crash protection, never allowed to affect the actual task).
    Polls ``stop_event`` instead of sleeping the full interval so shutdown
    (once the subprocess finishes, success or failure) is prompt. Mutates
    ``state`` (``{"uploaded": bool, "uri": str | None}``) so the caller can
    report whether anything was actually salvageable after a failure.
    """
    last_mtime = None
    while not stop_event.is_set():
        try:
            if local_path.exists():
                mtime = local_path.stat().st_mtime
                if mtime != last_mtime:
                    upload_output_file(local_path, dest_uri)
                    last_mtime = mtime
                    state["uploaded"] = True
                    state["uri"] = dest_uri
        except Exception:
            pass
        stop_event.wait(_CHECKPOINT_UPLOAD_INTERVAL_SECONDS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


# ── Pure sharding logic (unit-tested) ─────────────────────────────────────

def compute_row_range(total_rows: int, task_index: int, task_count: int) -> tuple[int, int]:
    """Contiguous [start, end) row block for one task. task_count is clamped to >= 1."""
    task_count = max(int(task_count), 1)
    if total_rows <= 0:
        return (0, 0)
    chunk_size = math.ceil(total_rows / task_count)
    start = task_index * chunk_size
    end = min(start + chunk_size, total_rows)
    return (max(start, 0), max(end, 0))


def add_row_index_column(df: pd.DataFrame) -> pd.DataFrame:
    """Add ROW_INDEX_COL (0-based original row position) if not already present."""
    if ROW_INDEX_COL not in df.columns:
        df = df.copy()
        df[ROW_INDEX_COL] = range(len(df))
    return df


def resolve_columns(cfg: "RunConfig", columns) -> tuple[str, str, Optional[str]]:
    """Resolve company/domain/(optional) input-country column names.

    Explicit ``cfg.company_column``/``cfg.domain_column``/
    ``cfg.input_country_column`` (set via env or CLI flag) win; otherwise
    falls back to the same auto-detection the Streamlit batch app's column-
    mapping step uses, so a dataset that already works there needs no extra
    cloud-specific config.
    """
    from lead_prioritizer_batch_app import (
        COMPANY_CANDIDATES,
        COUNTRY_CANDIDATES,
        DOMAIN_CANDIDATES,
        resolve_default_column,
    )

    cols = list(columns)
    company_col = cfg.company_column or resolve_default_column(cols, COMPANY_CANDIDATES)
    domain_col = cfg.domain_column or resolve_default_column(cols, DOMAIN_CANDIDATES)
    if not company_col or not domain_col:
        raise ValueError(
            "Could not resolve company/domain columns "
            f"(company={company_col!r}, domain={domain_col!r}); "
            "set COMPANY_COLUMN/DOMAIN_COLUMN explicitly."
        )
    country_col = cfg.input_country_column or resolve_default_column(cols, COUNTRY_CANDIDATES)
    return company_col, domain_col, country_col


# ── Config ─────────────────────────────────────────────────────────────────

@dataclass
class RunConfig:
    input_uri: str
    output_dir: str
    run_id: str
    task_index: int
    task_count: int
    anthropic_key: str
    serper_key: str
    firecrawl_key: str
    max_rows: Optional[int]
    force_rerun: bool
    mode: str
    company_column: Optional[str] = None
    domain_column: Optional[str] = None
    input_country_column: Optional[str] = None
    default_country: Optional[str] = None
    compose_caller_content: bool = False
    deep_dive: bool = False
    deep_dive_min_score: float = 8.0
    deep_dive_on_foreign_hq: bool = True
    rich_icp_context: bool = False
    ai_signal_scoring: bool = False
    use_enrichment_cache: bool = False
    enrichment_cache_bucket: str = ""
    c5_enabled: bool = False
    total_row_limit: Optional[int] = None
    gate_full_enrichment_on_foreign_hq: bool = False
    checkpoint_every_rows: int = 5


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="mYngle Lead Prioritizer — Cloud Run Job worker (one row-shard task)",
    )
    parser.add_argument("--input", default=None, help="gs://... URI or local file path (overrides INPUT_GCS_URI)")
    parser.add_argument("--output-dir", default=None, help="gs://... dir or local dir (overrides OUTPUT_GCS_DIR)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--task-index", type=int, default=None)
    parser.add_argument("--task-count", type=int, default=None)
    parser.add_argument("--anthropic-key", default=None)
    parser.add_argument("--serper-key", default=None)
    parser.add_argument("--firecrawl-key", default=None)
    parser.add_argument("--max-rows", type=int, default=None,
                         help="Cap rows WITHIN this task's already-computed shard "
                              "(local smoke-test knob; env MAX_ROWS). For a total-"
                              "file row limit applied BEFORE sharding, use "
                              "--total-row-limit instead.")
    parser.add_argument("--total-row-limit", type=int, default=None,
                         help="Truncate the input file to its first N rows BEFORE "
                              "computing shards, so N is distributed proportionally "
                              "across all tasks (env TOTAL_ROW_LIMIT).")
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--mode", default=None,
                         help="Lead Prioritizer v2 run mode (default: full).")
    parser.add_argument("--company-column", default=None,
                         help="Company name column (default: auto-detected).")
    parser.add_argument("--domain-column", default=None,
                         help="Domain column (default: auto-detected).")
    parser.add_argument("--input-country-column", default=None,
                         help="Optional per-row input country column (default: auto-detected).")
    parser.add_argument("--default-country", default=None,
                         help="Fallback input_country when no per-row country column can be "
                              "resolved from the source file (env DEFAULT_COUNTRY). Unlike "
                              "lead_prioritizer_batch_cli.py's own --default-country, this has "
                              "NO 'Italy' fallback: when unset and no country column resolves, "
                              "the task fails instead of silently defaulting to Italy.")
    parser.add_argument("--compose-caller-content", action="store_true",
                         help="Opt-in Step 3 (default: off; env COMPOSE_CALLER_CONTENT).")
    parser.add_argument("--deep-dive", action="store_true",
                         help="Opt-in Step B (default: off; env DEEP_DIVE).")
    parser.add_argument("--deep-dive-min-score", type=float, default=None,
                         help="Deep Dive score trigger threshold (default: 8.0; env DEEP_DIVE_MIN_SCORE).")
    parser.add_argument("--no-deep-dive-on-foreign-hq", action="store_true",
                         help="Disable the confirmed-foreign-HQ Deep Dive trigger "
                              "(default: trigger stays on; env DEEP_DIVE_ON_FOREIGN_HQ=false).")
    parser.add_argument("--rich-icp-context", action="store_true",
                         help="Opt-in rich ICP context (default: off; env RICH_ICP_CONTEXT).")
    parser.add_argument("--ai-signal-scoring", action="store_true",
                         help="Opt-in AI signal scoring (default: off; env AI_SIGNAL_SCORING).")
    parser.add_argument("--use-enrichment-cache", action="store_true",
                         help="Opt-in shared GCS enrichment cache (default: off; env USE_ENRICHMENT_CACHE).")
    parser.add_argument("--enrichment-cache-bucket", default=None,
                         help="GCS bucket for --use-enrichment-cache (env ENRICHMENT_CACHE_BUCKET).")
    parser.add_argument("--c5-enabled", action="store_true",
                         help="Opt-in C5 Sonnet HQ adjudication (default: off; env C5_ENABLED).")
    parser.add_argument("--gate-full-enrichment-on-foreign-hq", action="store_true",
                         help="Opt-in cost gate: cheap HQ-only screening for every "
                              "row, full non-HQ enrichment only for confirmed "
                              "foreign-HQ rows (default: off; env "
                              "GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ). See "
                              "lead_prioritizer_batch_cli.py --help for the C5 "
                              "interaction caveat.")
    parser.add_argument("--checkpoint-every-rows", type=int, default=None,
                         help="Crash-protection: periodically upload this "
                              "task's progress-so-far to GCS (status/"
                              "<task>_checkpoint.json) while it's still "
                              "running, so an OOM kill or crash loses at "
                              "most this many rows instead of the whole "
                              "shard's work (default: 5; env "
                              "CHECKPOINT_EVERY_ROWS; 0 disables it).")
    return parser


def resolve_config(argv=None) -> RunConfig:
    args = build_arg_parser().parse_args(argv)

    input_uri = args.input or os.environ.get("INPUT_GCS_URI", "")
    output_dir = args.output_dir or os.environ.get("OUTPUT_GCS_DIR", "")
    run_id = args.run_id or os.environ.get("RUN_ID", "") or datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")

    cloud_task_index = os.environ.get("CLOUD_RUN_TASK_INDEX")
    cloud_task_count = os.environ.get("CLOUD_RUN_TASK_COUNT")
    if cloud_task_index is not None and cloud_task_count is not None:
        task_index = int(cloud_task_index)
        task_count = int(cloud_task_count)
    else:
        task_index = args.task_index if args.task_index is not None else 0
        task_count = (
            args.task_count if args.task_count is not None
            else int(os.environ.get("TASK_COUNT", "1"))
        )

    max_rows = args.max_rows if args.max_rows is not None else (
        int(os.environ["MAX_ROWS"]) if os.environ.get("MAX_ROWS") else None
    )
    total_row_limit = args.total_row_limit if args.total_row_limit is not None else (
        int(os.environ["TOTAL_ROW_LIMIT"]) if os.environ.get("TOTAL_ROW_LIMIT") else None
    )

    return RunConfig(
        input_uri=input_uri,
        output_dir=output_dir,
        run_id=run_id,
        task_index=task_index,
        task_count=task_count,
        anthropic_key=args.anthropic_key or os.environ.get("ANTHROPIC_API_KEY", ""),
        serper_key=args.serper_key or os.environ.get("SERPER_API_KEY", ""),
        firecrawl_key=args.firecrawl_key or os.environ.get("FIRECRAWL_API_KEY", ""),
        max_rows=max_rows,
        force_rerun=args.force_rerun or _env_bool("FORCE_RERUN"),
        mode=args.mode or os.environ.get("MODE", "") or "full",
        company_column=args.company_column or os.environ.get("COMPANY_COLUMN") or None,
        domain_column=args.domain_column or os.environ.get("DOMAIN_COLUMN") or None,
        input_country_column=(
            args.input_country_column or os.environ.get("INPUT_COUNTRY_COLUMN") or None
        ),
        default_country=(
            args.default_country or os.environ.get("DEFAULT_COUNTRY") or None
        ),
        compose_caller_content=args.compose_caller_content or _env_bool("COMPOSE_CALLER_CONTENT"),
        deep_dive=args.deep_dive or _env_bool("DEEP_DIVE"),
        deep_dive_min_score=(
            args.deep_dive_min_score if args.deep_dive_min_score is not None
            else float(os.environ.get("DEEP_DIVE_MIN_SCORE", "8.0"))
        ),
        deep_dive_on_foreign_hq=(
            not args.no_deep_dive_on_foreign_hq
            and _env_bool("DEEP_DIVE_ON_FOREIGN_HQ", default=True)
        ),
        rich_icp_context=args.rich_icp_context or _env_bool("RICH_ICP_CONTEXT"),
        ai_signal_scoring=args.ai_signal_scoring or _env_bool("AI_SIGNAL_SCORING"),
        use_enrichment_cache=args.use_enrichment_cache or _env_bool("USE_ENRICHMENT_CACHE"),
        enrichment_cache_bucket=(
            args.enrichment_cache_bucket or os.environ.get("ENRICHMENT_CACHE_BUCKET") or ""
        ),
        c5_enabled=args.c5_enabled or _env_bool("C5_ENABLED"),
        total_row_limit=total_row_limit,
        gate_full_enrichment_on_foreign_hq=(
            args.gate_full_enrichment_on_foreign_hq
            or _env_bool("GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ")
        ),
        checkpoint_every_rows=(
            args.checkpoint_every_rows if args.checkpoint_every_rows is not None
            else int(os.environ.get("CHECKPOINT_EVERY_ROWS", "5"))
        ),
    )


def _status_payload(
    cfg: RunConfig, part_output_uri: str,
    row_start: Optional[int], row_end: Optional[int],
    rows_requested: int, rows_processed: int,
    status: str, started_at: str, finished_at: Optional[str], error: Optional[str],
    usage: Optional[dict] = None,
    checkpoint_uri: Optional[str] = None,
) -> dict:
    return {
        "run_id": cfg.run_id,
        "task_index": cfg.task_index,
        "task_count": cfg.task_count,
        "input_uri": cfg.input_uri,
        "output_part_uri": part_output_uri,
        "row_start": row_start,
        "row_end": row_end,
        "rows_requested": rows_requested,
        "rows_processed": rows_processed,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "error": error,
        # usage_tracker.snapshot() dict from this task's lead_prioritizer_batch_cli.py
        # subprocess (see --usage-output), or None when unavailable -- e.g. an old
        # deployed image that doesn't support the flag yet. The Cloud Run
        # orchestrator (cloud_run_streamlit_app.py) sums these across every task's
        # _done.json via usage_tracker.merge_snapshots() for one combined report.
        "usage": usage,
        # Set only on a "failed" status when a periodic checkpoint managed to
        # upload at least once before the crash -- see the checkpoint-upload
        # thread in main(). None elsewhere (nothing to salvage: either the
        # task is fine, or checkpointing was off/never got a chance to run).
        "checkpoint_uri": checkpoint_uri,
    }


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    cfg = resolve_config(argv)
    started_at = _now_iso()

    if not cfg.input_uri:
        print("[cloud_job_runner] ERROR: no input specified (INPUT_GCS_URI env var or --input)", file=sys.stderr)
        return 1
    if not cfg.output_dir:
        print("[cloud_job_runner] ERROR: no output dir specified (OUTPUT_GCS_DIR env var or --output-dir)", file=sys.stderr)
        return 1

    from lead_prioritizer_batch_core import SUPPORTED_RUN_MODES
    if cfg.mode not in SUPPORTED_RUN_MODES:
        print(
            f"[cloud_job_runner] ERROR: unknown mode {cfg.mode!r}; "
            f"expected one of {sorted(SUPPORTED_RUN_MODES)}",
            file=sys.stderr,
        )
        return 1

    task_label = f"part_{cfg.task_index:04d}"
    part_output_uri = join_path(cfg.output_dir, "parts", f"{task_label}.xlsx")
    status_running_uri = join_path(cfg.output_dir, "status", f"{task_label}_running.json")
    status_done_uri = join_path(cfg.output_dir, "status", f"{task_label}_done.json")
    status_failed_uri = join_path(cfg.output_dir, "status", f"{task_label}_failed.json")

    print(f"[cloud_job_runner] run_id={cfg.run_id} task_index={cfg.task_index} task_count={cfg.task_count} mode={cfg.mode}", flush=True)
    print(f"[cloud_job_runner] input={cfg.input_uri}", flush=True)
    print(f"[cloud_job_runner] output_dir={cfg.output_dir}", flush=True)
    print(
        f"[cloud_job_runner] anthropic_key={'set' if cfg.anthropic_key else 'missing'} "
        f"serper_key={'set' if cfg.serper_key else 'missing'} "
        f"firecrawl_key={'set' if cfg.firecrawl_key else 'missing'}",
        flush=True,
    )

    if not cfg.force_rerun and path_exists(part_output_uri):
        print(f"[cloud_job_runner] Part output already exists, skipping (FORCE_RERUN not set): {part_output_uri}", flush=True)
        finished_at = _now_iso()
        write_status_json(
            status_done_uri,
            _status_payload(cfg, part_output_uri, None, None, 0, 0, "skipped", started_at, finished_at, None),
        )
        return 0

    write_status_json(
        status_running_uri,
        _status_payload(cfg, part_output_uri, None, None, 0, 0, "running", started_at, None, None),
    )

    tmp_dir = Path(tempfile.gettempdir()) / "cloud_job_runner" / f"{cfg.run_id}_{task_label}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    row_start: Optional[int] = None
    row_end: Optional[int] = None
    rows_in_part = 0
    checkpoint_state: dict = {"uploaded": False, "uri": None}

    try:
        if is_gcs_uri(cfg.input_uri):
            local_input = tmp_dir / "input.xlsx"
            print("[cloud_job_runner] Downloading input from GCS...", flush=True)
            download_gcs_file(cfg.input_uri, local_input)
        else:
            local_input = Path(cfg.input_uri)
            if not local_input.exists():
                raise FileNotFoundError(f"Local input not found: {local_input}")

        fname = local_input.name.lower()
        df_in = pd.read_csv(local_input) if fname.endswith(".csv") else pd.read_excel(local_input)
        if cfg.total_row_limit:
            # Every task independently reads the same original file and
            # applies this same deterministic head-truncation BEFORE sharding,
            # so all tasks agree on the same (smaller) total_rows and end up
            # with proportionally smaller shards -- e.g. TOTAL_ROW_LIMIT=100
            # with TASK_COUNT=50 gives every task ~2 rows, not ~10 (the full
            # file's share). This is distinct from MAX_ROWS/--max-rows below,
            # which instead caps rows WITHIN one task's already-computed shard.
            df_in = df_in.iloc[: int(cfg.total_row_limit)].copy()
        total_rows = len(df_in)
        df_in = add_row_index_column(df_in)

        row_start, row_end = compute_row_range(total_rows, cfg.task_index, cfg.task_count)
        print(f"[cloud_job_runner] total_rows={total_rows} row_start={row_start} row_end={row_end}", flush=True)

        if row_start >= total_rows:
            print("[cloud_job_runner] No rows assigned to this task; writing empty done status.", flush=True)
            finished_at = _now_iso()
            write_status_json(
                status_done_uri,
                _status_payload(cfg, part_output_uri, row_start, row_start, 0, 0, "done", started_at, finished_at, None),
            )
            return 0

        df_part = df_in.iloc[row_start:row_end].copy()
        rows_in_part = len(df_part)

        company_col, domain_col, country_col = resolve_columns(cfg, df_part.columns)
        if not country_col and not cfg.default_country:
            # No per-row country column in the source file (e.g. a raw Lusha
            # export, which nests country inside a "location" dict rather
            # than a flat input_country/country column) AND no explicit
            # fallback was configured. Refuse to proceed: silently falling
            # through to lead_prioritizer_batch_cli.py's own "Italy" default
            # here previously mis-enriched an entire non-Italy country batch
            # (every row scored/adjudicated against the wrong home country)
            # without any error, log line, or way to detect it until a human
            # spotted the wrong result downstream. Fail fast instead -- this
            # is before any AI/Firecrawl/Serper call is made, so it costs
            # nothing -- and point at the fix: a sidecar 'default_country'
            # (or 'lovable_export.country') key, or a proper country column.
            raise ValueError(
                "Could not resolve an input-country column from the source "
                "file, and no --default-country/DEFAULT_COUNTRY fallback was "
                "provided. Refusing to silently default to \"Italy\". Set a "
                "sidecar 'default_country' (or 'lovable_export.country') key "
                "for this incoming/ upload, or add an input_country/country "
                "column to the source file."
            )

        part_input_path = tmp_dir / f"input_part_{cfg.task_index:04d}.xlsx"
        df_part.to_excel(part_input_path, index=False)

        task_output_dir = tmp_dir / "output"
        task_output_dir.mkdir(parents=True, exist_ok=True)
        local_output_path = task_output_dir / f"{task_label}.xlsx"
        usage_output_path = task_output_dir / f"{task_label}_usage.json"
        checkpoint_local_path = task_output_dir / f"{task_label}_checkpoint.json"
        checkpoint_status_uri = join_path(cfg.output_dir, "status", f"{task_label}_checkpoint.json")

        cmd = [
            sys.executable, str(BATCH_CLI_SCRIPT),
            "--input", str(part_input_path),
            "--company-column", company_col,
            "--domain-column", domain_col,
            "--mode", cfg.mode,
            # Each task already got its own contiguous row block above; process
            # all of it (0 = no limit), except in a --max-rows smoke test.
            "--row-limit", str(cfg.max_rows) if cfg.max_rows else "0",
            "--output", str(local_output_path),
            "--usage-output", str(usage_output_path),
            "--yes",
        ]
        if cfg.checkpoint_every_rows > 0:
            cmd += ["--checkpoint-path", str(checkpoint_local_path),
                    "--checkpoint-every-rows", str(cfg.checkpoint_every_rows)]
        if country_col:
            cmd += ["--input-country-column", country_col]
        if cfg.default_country:
            cmd += ["--default-country", cfg.default_country]
        if cfg.compose_caller_content:
            cmd += ["--compose-caller-content"]
        if cfg.deep_dive:
            cmd += ["--deep-dive", "--deep-dive-min-score", str(cfg.deep_dive_min_score)]
            if not cfg.deep_dive_on_foreign_hq:
                cmd += ["--no-deep-dive-on-foreign-hq"]
        if cfg.rich_icp_context:
            cmd += ["--rich-icp-context"]
        if cfg.ai_signal_scoring:
            cmd += ["--ai-signal-scoring"]
        if cfg.use_enrichment_cache:
            cmd += ["--use-enrichment-cache", "--enrichment-cache-bucket", cfg.enrichment_cache_bucket]
        if cfg.c5_enabled:
            cmd += ["--c5-enabled"]
        if cfg.gate_full_enrichment_on_foreign_hq:
            cmd += ["--gate-full-enrichment-on-foreign-hq"]

        env = os.environ.copy()
        if cfg.anthropic_key:
            env["ANTHROPIC_API_KEY"] = cfg.anthropic_key
        if cfg.serper_key:
            env["SERPER_API_KEY"] = cfg.serper_key
        if cfg.firecrawl_key:
            env["FIRECRAWL_API_KEY"] = cfg.firecrawl_key

        print(
            f"[cloud_job_runner] Invoking lead_prioritizer_batch_cli.py for {rows_in_part} rows "
            f"(company_column={company_col!r} domain_column={domain_col!r})...",
            flush=True,
        )

        checkpoint_stop_event = threading.Event()
        checkpoint_thread = None
        if cfg.checkpoint_every_rows > 0:
            checkpoint_thread = threading.Thread(
                target=_checkpoint_uploader_loop,
                args=(checkpoint_local_path, checkpoint_status_uri,
                      checkpoint_stop_event, checkpoint_state),
                daemon=True,
            )
            checkpoint_thread.start()
        try:
            proc = subprocess.run(cmd, env=env)
        finally:
            # Always stop the uploader once the subprocess is done (success,
            # non-zero exit, or an exception from subprocess.run itself) --
            # never leave the daemon thread polling after this function returns.
            checkpoint_stop_event.set()
            if checkpoint_thread is not None:
                checkpoint_thread.join(timeout=5)
            # One last unconditional catch-up upload: the background thread
            # only polls every _CHECKPOINT_UPLOAD_INTERVAL_SECONDS, so without
            # this, a checkpoint written in the final few seconds before the
            # subprocess exited (success OR failure) could be missed entirely.
            if cfg.checkpoint_every_rows > 0 and checkpoint_local_path.exists():
                try:
                    upload_output_file(checkpoint_local_path, checkpoint_status_uri)
                    checkpoint_state["uploaded"] = True
                    checkpoint_state["uri"] = checkpoint_status_uri
                except Exception:
                    pass

        if proc.returncode != 0:
            raise RuntimeError(f"lead_prioritizer_batch_cli.py exited with code {proc.returncode}")

        if not local_output_path.exists():
            raise FileNotFoundError(f"No output written to {local_output_path}")

        print(f"[cloud_job_runner] Uploading part output -> {part_output_uri}", flush=True)
        upload_output_file(local_output_path, part_output_uri)

        # lead_prioritizer_batch_cli.py applies --row-limit itself (head-of-shard
        # truncation), so the actually-processed count can be lower than the shard size.
        rows_processed = min(rows_in_part, cfg.max_rows) if cfg.max_rows else rows_in_part

        usage = None
        try:
            if usage_output_path.exists():
                usage = json.loads(usage_output_path.read_text(encoding="utf-8"))
        except Exception as exc:
            # Never let a usage-reporting hiccup fail an otherwise-successful task.
            print(f"[cloud_job_runner] WARNING: could not read usage output "
                  f"({type(exc).__name__}: {exc})", flush=True)

        finished_at = _now_iso()
        write_status_json(
            status_done_uri,
            _status_payload(cfg, part_output_uri, row_start, row_end, rows_in_part, rows_processed, "done", started_at, finished_at, None, usage=usage),
        )
        print(f"[cloud_job_runner] Task {cfg.task_index} done. rows_processed={rows_processed}", flush=True)
        return 0

    except Exception as exc:
        finished_at = _now_iso()
        err_msg = f"{type(exc).__name__}: {exc}"
        print(f"[cloud_job_runner] ERROR: {err_msg}", file=sys.stderr, flush=True)
        if checkpoint_state["uploaded"]:
            print(f"[cloud_job_runner] A partial checkpoint was uploaded to "
                  f"{checkpoint_state['uri']} before the crash.", flush=True)
        try:
            write_status_json(
                status_failed_uri,
                _status_payload(
                    cfg, part_output_uri, row_start, row_end, rows_in_part, 0, "failed",
                    started_at, finished_at, err_msg,
                    checkpoint_uri=checkpoint_state["uri"] if checkpoint_state["uploaded"] else None,
                ),
            )
        except Exception as write_exc:
            print(f"[cloud_job_runner] ERROR: could not write failed status: {write_exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
