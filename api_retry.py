"""Shared 429 retry/backoff helper for external API calls (Serper, Firecrawl).

Both providers are hit from many concurrent threads/Cloud Run tasks at once,
so a 429 ("rate limited, try again shortly") is expected under load and is
NOT the same kind of failure as a bad API key or an outage — it should be
retried a few times with backoff rather than immediately giving up on the
whole lead. Every other failure (network error, other 4xx, 5xx) is left
completely unchanged by this module; only 429 handling is new behavior.
"""

from __future__ import annotations

DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 8.0


def backoff_seconds(
    attempt: int,
    retry_after: float | None = None,
    *,
    base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
) -> float:
    """Seconds to sleep before retry ``attempt`` (0-based).

    Honors a server-supplied ``Retry-After`` value when present (capped, so a
    misbehaving server can't stall a run for minutes); otherwise exponential:
    ``base * 2**attempt``, capped at ``cap``.
    """
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, cap)
    return min(base * (2 ** attempt), cap)


def parse_retry_after(raw: str | None) -> float | None:
    """Parse an HTTP ``Retry-After`` header value (seconds form only).

    Returns ``None`` for a missing/non-numeric header (the HTTP-date form is
    not used by Serper/Firecrawl in practice) so the caller falls back to
    plain exponential backoff.
    """
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
