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
    deep_dive: bool = False
    rich_icp_context: bool = False
    ai_signal_scoring: bool = False


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
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--mode", default=None,
                         help="Lead Prioritizer v2 run mode (default: full).")
    parser.add_argument("--company-column", default=None,
                         help="Company name column (default: auto-detected).")
    parser.add_argument("--domain-column", default=None,
                         help="Domain column (default: auto-detected).")
    parser.add_argument("--input-country-column", default=None,
                         help="Optional per-row input country column (default: auto-detected).")
    parser.add_argument("--deep-dive", action="store_true",
                         help="Opt-in Step B (default: off; env DEEP_DIVE).")
    parser.add_argument("--rich-icp-context", action="store_true",
                         help="Opt-in rich ICP context (default: off; env RICH_ICP_CONTEXT).")
    parser.add_argument("--ai-signal-scoring", action="store_true",
                         help="Opt-in AI signal scoring (default: off; env AI_SIGNAL_SCORING).")
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
        deep_dive=args.deep_dive or _env_bool("DEEP_DIVE"),
        rich_icp_context=args.rich_icp_context or _env_bool("RICH_ICP_CONTEXT"),
        ai_signal_scoring=args.ai_signal_scoring or _env_bool("AI_SIGNAL_SCORING"),
    )


def _status_payload(
    cfg: RunConfig, part_output_uri: str,
    row_start: Optional[int], row_end: Optional[int],
    rows_requested: int, rows_processed: int,
    status: str, started_at: str, finished_at: Optional[str], error: Optional[str],
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

        part_input_path = tmp_dir / f"input_part_{cfg.task_index:04d}.xlsx"
        df_part.to_excel(part_input_path, index=False)

        task_output_dir = tmp_dir / "output"
        task_output_dir.mkdir(parents=True, exist_ok=True)
        local_output_path = task_output_dir / f"{task_label}.xlsx"

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
            "--yes",
        ]
        if country_col:
            cmd += ["--input-country-column", country_col]
        if cfg.deep_dive:
            cmd += ["--deep-dive"]
        if cfg.rich_icp_context:
            cmd += ["--rich-icp-context"]
        if cfg.ai_signal_scoring:
            cmd += ["--ai-signal-scoring"]

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
        proc = subprocess.run(cmd, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"lead_prioritizer_batch_cli.py exited with code {proc.returncode}")

        if not local_output_path.exists():
            raise FileNotFoundError(f"No output written to {local_output_path}")

        print(f"[cloud_job_runner] Uploading part output -> {part_output_uri}", flush=True)
        upload_output_file(local_output_path, part_output_uri)

        # lead_prioritizer_batch_cli.py applies --row-limit itself (head-of-shard
        # truncation), so the actually-processed count can be lower than the shard size.
        rows_processed = min(rows_in_part, cfg.max_rows) if cfg.max_rows else rows_in_part

        finished_at = _now_iso()
        write_status_json(
            status_done_uri,
            _status_payload(cfg, part_output_uri, row_start, row_end, rows_in_part, rows_processed, "done", started_at, finished_at, None),
        )
        print(f"[cloud_job_runner] Task {cfg.task_index} done. rows_processed={rows_processed}", flush=True)
        return 0

    except Exception as exc:
        finished_at = _now_iso()
        err_msg = f"{type(exc).__name__}: {exc}"
        print(f"[cloud_job_runner] ERROR: {err_msg}", file=sys.stderr, flush=True)
        try:
            write_status_json(
                status_failed_uri,
                _status_payload(cfg, part_output_uri, row_start, row_end, rows_in_part, 0, "failed", started_at, finished_at, err_msg),
            )
        except Exception as write_exc:
            print(f"[cloud_job_runner] ERROR: could not write failed status: {write_exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
