"""Deterministic app/evidence summary builder for Lead Prioritizer v2 (Step 4).

Turns existing non-HQ ``LeadSignal`` and ``LeadEvidence`` into the app/audit
text fields (`evidence_summary_app`, `key_source_links_app`,
`advanced_notes_app`).  Everything is deterministic and traceable:

- no live Serper calls, no AI,
- no competitor content (only the four supported non-HQ signals are used),
- rapid growth is never presented as a positive driver,
- nothing (quotes / URLs / facts) is invented — only existing field values are
  reused.
"""

from __future__ import annotations

from typing import Optional

from lead_output_schema import LeadEvidence, LeadSignal
from lead_non_hq_signal_extractor import SUPPORTED_SIGNALS


# Readable labels for the supported non-HQ signals (canonical order).
_SIGNAL_LABELS: dict[str, str] = {
    "international_profile": "International profile",
    "onboarding_training_need": "Onboarding / training need",
    "company_size_complexity": "Company size / complexity",
    "icp_keyword_match": "ICP keyword match",
}


def _fmt_score(score: Optional[float]) -> str:
    if score is None:
        return ""
    return f"{score:g}"


def build_evidence_summary_app(
    signals: list[LeadSignal],
    evidence_items: list[LeadEvidence],
) -> Optional[str]:
    """One compact line per present supported signal, or None when nothing to say."""
    if not signals and not evidence_items:
        return None

    by_name = {s.signal_name: s for s in (signals or [])}
    lines: list[str] = []
    for name in SUPPORTED_SIGNALS:
        sig = by_name.get(name)
        if sig is None:
            continue
        label = _SIGNAL_LABELS[name]
        parts: list[str] = []
        score_str = _fmt_score(sig.signal_score)
        if score_str:
            parts.append(f"score {score_str}")
        if sig.signal_confidence:
            parts.append(f"{sig.signal_confidence} confidence")
        head = f"{label}: " + ", ".join(parts) if parts else f"{label}:"
        if sig.signal_reason:
            head = f"{head}. {sig.signal_reason}" if parts else f"{head} {sig.signal_reason}"
        lines.append(head.rstrip())

    return "\n".join(lines) if lines else None


def build_key_source_links_app(
    signals: list[LeadSignal],
    evidence_items: list[LeadEvidence],
    max_links: int = 6,
) -> Optional[str]:
    """Deduplicated source links, signal-attached URLs first, then evidence URLs.

    Only supported non-HQ signals contribute; anything else (e.g. a
    competitor-tagged item) is ignored.
    """
    seen: set[str] = set()
    lines: list[str] = []

    def _add(signal_name: Optional[str], title: Optional[str], url: Optional[str]) -> None:
        if len(lines) >= max_links:
            return
        if signal_name not in SUPPORTED_SIGNALS:
            return
        url = (url or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        label = _SIGNAL_LABELS[signal_name]
        title = (title or "").strip()
        lines.append(f"{label} — {title}: {url}" if title else f"{label}: {url}")

    # 1. URLs attached to signals first.
    for s in (signals or []):
        _add(s.signal_name, s.evidence_title, s.evidence_url)

    # 2. Remaining evidence URLs.
    for e in (evidence_items or []):
        _add(e.signal_name, e.source_title, e.source_url)

    return "\n".join(lines) if lines else None


def build_advanced_notes_app(
    signals: list[LeadSignal],
    evidence_items: list[LeadEvidence],
) -> Optional[str]:
    """Concise audit notes — counts and flags only, never a sales pitch."""
    supported_evidence = [
        e for e in (evidence_items or []) if e.signal_name in SUPPORTED_SIGNALS
    ]
    supported_signals = [
        s for s in (signals or []) if s.signal_name in SUPPORTED_SIGNALS
    ]

    if not supported_evidence and not supported_signals:
        return None

    notes: list[str] = [
        f"Non-HQ evidence items: {len(supported_evidence)}.",
        f"Extracted signals: {len(supported_signals)}.",
    ]

    if supported_signals:
        names = [s.signal_name for s in supported_signals]
        notes.append("Signal names: " + ", ".join(names) + ".")

        weak = [
            s.signal_name for s in supported_signals
            if s.signal_confidence == "Low" or (s.signal_score is not None and s.signal_score == 0.0)
        ]
        if weak:
            notes.append("Low-confidence or zero-score signals: " + ", ".join(weak) + ".")

        review = [s.signal_name for s in supported_signals if s.needs_manual_review]
        if review:
            notes.append("Manual review flagged: " + ", ".join(review) + ".")

    return " ".join(notes)


def build_app_summary_fields(
    signals: list[LeadSignal],
    evidence_items: list[LeadEvidence],
) -> dict:
    """Build the three app/audit text fields as a dict of result field names."""
    return {
        "evidence_summary_app": build_evidence_summary_app(signals, evidence_items),
        "key_source_links_app": build_key_source_links_app(signals, evidence_items),
        "advanced_notes_app": build_advanced_notes_app(signals, evidence_items),
    }
