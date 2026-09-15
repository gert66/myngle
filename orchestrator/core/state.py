"""Persistent job state: atomic JSON save/load plus an append-only event log.

Canonical state lives in one JSON file (schema_version 1, see
schemas/state.schema.json). Every save goes through a temp file in the same
directory, flush + fsync, os.replace, then fsync of the parent directory, so a
crash at any point leaves either the old or the new state on disk, never a
partial file. Loading never repairs or guesses: unreadable, unparsable or
invalid state raises StateError.

Phase transitions are recorded in events.jsonl, one JSON object per line with
ts / from / to / reason / step_id, flushed and fsynced on every append.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.contracts import PHASES, SCHEMA_VERSION, ContractError, validate_state

__all__ = [
    "PHASES", "SCHEMA_VERSION", "StateError",
    "utc_now", "new_state", "load_state", "save_state",
    "save_json", "load_json",
    "append_event", "read_events",
]


class StateError(Exception):
    """Raised when canonical state cannot be read, parsed, validated or written."""


def utc_now():
    """Current UTC time as an ISO 8601 string with a Z suffix, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_state(job_id, phase="QUEUED"):
    """Build a fresh, validated state dict for a job."""
    now = utc_now()
    state = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "phase": phase,
        "step_id": None,
        "attempt": 0,
        "created_at": now,
        "updated_at": now,
        "last_error": None,
    }
    try:
        return validate_state(state)
    except ContractError as exc:
        raise StateError(f"cannot create state: {exc}") from exc


def load_state(path):
    """Read and validate canonical state. Raises StateError; never recovers silently."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StateError(f"state file not found: {path}") from None
    except OSError as exc:
        raise StateError(f"cannot read state file {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"corrupt state file {path}: not valid JSON ({exc})") from exc
    try:
        return validate_state(data)
    except ContractError as exc:
        raise StateError(f"invalid state file {path}: {exc}") from exc


def save_state(path, state):
    """Validate and atomically persist state. Returns the validated copy."""
    try:
        validated = validate_state(state)
    except ContractError as exc:
        raise StateError(f"refusing to save invalid state: {exc}") from exc
    text = json.dumps(validated, indent=2, sort_keys=True) + "\n"
    try:
        _atomic_write_text(Path(path), text)
    except OSError as exc:
        raise StateError(f"cannot write state file {path}: {exc}") from exc
    return validated


def save_json(path, obj):
    """Atomically persist any JSON-serialisable object (same durability as save_state)."""
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    try:
        _atomic_write_text(Path(path), text)
    except OSError as exc:
        raise StateError(f"cannot write {path}: {exc}") from exc


def load_json(path):
    """Read a JSON file written by save_json. Missing or corrupt files raise StateError."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StateError(f"file not found: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read {path}: {exc}") from exc


def _atomic_write_text(path, text):
    """Write text to path via temp file + fsync + os.replace; the old file survives any failure."""
    directory = path.parent
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _fsync_dir(directory):
    """Make the directory entry durable. Best effort: not every platform allows it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def append_event(path, from_phase, to_phase, reason, step_id=None, ts=None):
    """Append one transition record to events.jsonl with a durable flush. Returns the record."""
    if from_phase is not None and from_phase not in PHASES:
        raise StateError(f"event: from phase {from_phase!r} not in {list(PHASES)}")
    if to_phase not in PHASES:
        raise StateError(f"event: to phase {to_phase!r} not in {list(PHASES)}")
    if not isinstance(reason, str) or not reason.strip():
        raise StateError("event: reason must be a non-empty string")
    if step_id is not None and not isinstance(step_id, str):
        raise StateError("event: step_id must be a string or null")
    record = {
        "ts": ts or utc_now(),
        "from": from_phase,
        "to": to_phase,
        "reason": reason,
        "step_id": step_id,
    }
    line = json.dumps(record, sort_keys=True) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise StateError(f"cannot append to event log {path}: {exc}") from exc
    return record


def read_events(path):
    """Read all records from events.jsonl. A missing file yields []; a corrupt line raises."""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise StateError(f"corrupt event log {path} at line {lineno}: {exc}") from exc
    return records
