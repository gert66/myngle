"""Atomic progress.json writer, designed to be polled by an external VM
Monitor while the audit runs.

Schema (``schema_version: 1``)::

    {
      "schema_version": 1,
      "job": "hubspot_population_audit",
      "phase": "<overall phase>",
      "phases": [
        {"name": "snapshot_load", "status": "running|completed|not_yet_implemented|...", "updated_at": "..."},
        ...
      ],
      "counters": {"records_loaded.companies": 123, "lookups_done": 0, ...},
      "started_at": "...",
      "updated_at": "...",
      "last_error": null
    }

Every write goes to a temp file in the same directory followed by
``os.replace`` so a reader never observes a partially written file.
"""
from __future__ import annotations

import json
import os
import time

SCHEMA_VERSION = 1


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ProgressWriter:
    def __init__(self, path: str, job: str = "hubspot_population_audit"):
        self.path = path
        started = now_iso()
        self._phases: dict = {}
        self.state = {
            "schema_version": SCHEMA_VERSION,
            "job": job,
            "phase": "starting",
            "phases": [],
            "counters": {},
            "started_at": started,
            "updated_at": started,
            "last_error": None,
        }
        self._flush()

    def set_phase(self, phase: str) -> None:
        self.state["phase"] = phase
        self._touch_and_flush()

    def set_subprocess_status(self, name: str, status: str, **extra) -> None:
        entry = self._phases.setdefault(name, {"name": name})
        entry["status"] = status
        entry["updated_at"] = now_iso()
        entry.update(extra)
        self.state["phases"] = list(self._phases.values())
        self._touch_and_flush()

    def set_counter(self, name: str, value) -> None:
        self.state["counters"][name] = value
        self._touch_and_flush()

    def increment_counter(self, name: str, by: int = 1) -> None:
        self.state["counters"][name] = self.state["counters"].get(name, 0) + by
        self._touch_and_flush()

    def set_error(self, message: str) -> None:
        self.state["last_error"] = message
        self._touch_and_flush()

    def _touch_and_flush(self) -> None:
        self.state["updated_at"] = now_iso()
        self._flush()

    def _flush(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp_path, self.path)
