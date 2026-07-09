"""Local checkpoint persistence for a Cloud Run Jobs task's in-progress
batch shard.

Each Cloud Run Jobs task processes its own contiguous row shard (typically
~50-100 rows for a large input) entirely in memory, writing its output file
only once at the very end (see ``lead_prioritizer_batch_core.py``'s Phase 3
loops and ``lead_prioritizer_batch_cli.py``'s single final
``output_path.write_bytes(...)``). If the task is OOM-killed or crashes
partway through, every row it had already processed is lost -- not just the
row that triggered the crash -- because nothing was ever written before
that point.

This module lets a batch loop write its progress-so-far to a local file
periodically (every N rows), so ``cloud_job_runner.py`` can upload it to GCS
alongside the task's status files while the task is still running. On a
crash, the last-uploaded checkpoint is still recoverable from GCS even
though the task's own part-output file was never produced.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional


def write_checkpoint_file(
    path, enriched_rows: list, evidence_rows: list, signal_rows: list,
    *, processed: int, selected_rows: int,
) -> None:
    """Atomically write the batch's progress-so-far to ``path`` as JSON.

    Never raises -- a checkpoint failure must not break the actual
    enrichment run (same philosophy as ``enrichment_cache.py``'s save-
    failure handling). Writes to a temp file in the same directory then
    ``os.replace()``s it into place, so a crash mid-write never leaves a
    truncated/corrupt checkpoint for a later reader to choke on.
    """
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "processed": processed,
            "selected_rows": selected_rows,
            "enriched_rows": enriched_rows,
            "evidence_rows": evidence_rows,
            "signal_rows": signal_rows,
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".checkpoint_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, default=str)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        pass  # a broken checkpoint must never break the batch run


def read_checkpoint_file(path) -> Optional[dict]:
    """Read back a checkpoint written by ``write_checkpoint_file``.

    Returns ``None`` -- never raises -- on any missing/corrupt/unreadable
    file, so a caller can always safely treat "no usable checkpoint" the
    same as "no checkpoint at all".
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def make_checkpoint_callback(path, *, get_selected_rows):
    """Build a ``(enriched_rows, evidence_rows, signal_rows) -> None``
    callback for ``lead_prioritizer_batch_core.py``'s ``checkpoint_callback``
    parameter, closing over ``path`` and a zero-arg ``get_selected_rows``
    (deferred so the caller doesn't need the row count known up front).
    """
    def _callback(enriched_rows: list, evidence_rows: list, signal_rows: list) -> None:
        write_checkpoint_file(
            path, enriched_rows, evidence_rows, signal_rows,
            processed=len(enriched_rows), selected_rows=get_selected_rows(),
        )
    return _callback
