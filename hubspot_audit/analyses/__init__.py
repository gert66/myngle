"""Deterministic, streaming CRM quality checks.

Every analysis module here takes an iterator over normalized JSONL records
(never a fully-materialized list of full records) and returns a list of
plain-dict "signals": candidate findings with counts and safe sample IDs,
not yet promoted to a Finding until the runner passes them through the
evidence ledger. Grouping keys carry only the handful of fields each check
needs (id, domain/email, dates, owner) -- never the full ``source_properties``
blob -- to keep memory bounded on the target VM.
"""
