# Orchestrator Git home

Canonical source lives here in `gert66/myngle` on branch `work`.

The active VM runtime remains `/home/myngle/orchestrator` until a separately reviewed cutover.
This directory intentionally excludes credentials, mutable quota/state, locks, logs, jobs,
intake receipts, job inputs, backups, and VM-specific account configuration.

Safety policy:
- `gert66/myngle` canonical development base: `work`.
- `main` remains stable and is not a direct development target.
- Runtime changes must preserve repo/remote/branch/clean-tree/freshness gates.
