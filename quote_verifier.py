"""Mechanical quote verification for Deep Dive claims (Lead Prioritizer v2).

A ``DeepDiveClaim`` carries a ``quote`` the AI distillation step (see
``deep_dive_runner.py``) claims is a literal excerpt of ``source_url``. This
module never trusts that claim: it re-checks the quote against the actual
page text with a layered, deterministic matcher, so an AI hallucination (an
invented or subtly reworded quote) is caught automatically rather than
presented to a caller as fact.

Pure matching only — no network calls, no AI calls, no mutation of
scoring-related state. ``verify_claims`` is the only function that mutates
``DeepDiveClaim`` objects, and only their ``quote_verified`` /
``quote_verification_status`` / ``quote_match_score`` /
``quote_matched_snippet`` fields; it never touches ``quote`` or
``original_quote`` themselves — the quote-correction ("self-healing") logic
that does that lives in ``deep_dive_runner.py``, which calls
``verify_quote_on_page`` again to mechanically re-check any AI-proposed
correction before ever accepting it.

Matching layers, in order (stops at the first hit):
  1. Normalized exact substring match (lowercase, Unicode NFKC, collapsed
     whitespace, typographic quotes/dashes folded to ASCII) -> "verified",
     score 1.0.
  2. Sliding-window fuzzy match via ``difflib.SequenceMatcher`` (stdlib —
     no new dependency) over windows sized at ~80%/100%/120% of the quote's
     normalized length -> "fuzzy_match" when the best ratio is >= 0.85,
     otherwise "not_found" with that best ratio recorded as the score.
  3. Quotes shorter than ``_MIN_FUZZY_QUOTE_LEN`` skip the fuzzy layer
     entirely (fuzzy matching on very short strings produces far too many
     false positives) — no exact hit means "not_found".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Optional

# Page text is normalized once and capped at this length before matching —
# a defense against pathologically large pages, applied per page, not per
# claim (callers should cache the normalized form across claims sharing a
# URL; see ``verify_claims``).
_MAX_PAGE_TEXT_CHARS = 200_000

_MIN_FUZZY_QUOTE_LEN = 25
_FUZZY_MATCH_THRESHOLD = 0.85

_WHITESPACE_RE = re.compile(r"\s+")

# Typographic punctuation folded to its plain-ASCII equivalent so a page's
# smart quotes/en-dashes don't defeat an otherwise-exact match.
_PUNCTUATION_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...",
})


@dataclass
class QuoteVerification:
    """Result of checking one quote against one page's text."""
    status: str            # "verified" | "fuzzy_match" | "not_found"
    match_score: float     # 1.0 exact, else best similarity, 0.0 if not_found
    matched_snippet: str   # the matched text (normalized form), for audit


def _normalize(text: str) -> str:
    """Lowercase, NFKC-normalized, whitespace-collapsed, ASCII-punctuation
    form of ``text``, used on both sides of every comparison."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_PUNCTUATION_TRANSLATION)
    text = text.lower()
    return _WHITESPACE_RE.sub(" ", text).strip()


def _best_fuzzy_match(norm_quote: str, norm_page: str) -> tuple[float, str]:
    """Sliding-window fuzzy search. Returns ``(best_ratio, best_snippet)``."""
    quote_len = len(norm_quote)
    page_len = len(norm_page)
    if quote_len == 0 or page_len == 0:
        return 0.0, ""

    best_ratio = 0.0
    best_snippet = ""
    for factor in (0.8, 1.0, 1.2):
        window = max(1, min(page_len, int(quote_len * factor)))
        step = max(1, window // 8)
        positions = list(range(0, max(1, page_len - window + 1), step))
        if positions[-1] != page_len - window and page_len > window:
            positions.append(page_len - window)  # always check the tail
        for i in positions:
            candidate = norm_page[i:i + window]
            if not candidate:
                continue
            ratio = SequenceMatcher(None, norm_quote, candidate).ratio()
            if ratio > best_ratio:
                best_ratio, best_snippet = ratio, candidate
    return best_ratio, best_snippet


def _match_normalized(norm_quote: str, norm_page: str) -> QuoteVerification:
    if not norm_quote or not norm_page:
        return QuoteVerification(status="not_found", match_score=0.0, matched_snippet="")

    idx = norm_page.find(norm_quote)
    if idx != -1:
        return QuoteVerification(status="verified", match_score=1.0, matched_snippet=norm_quote)

    if len(norm_quote) < _MIN_FUZZY_QUOTE_LEN:
        # Fuzzy matching on very short strings is too permissive to trust.
        return QuoteVerification(status="not_found", match_score=0.0, matched_snippet="")

    best_ratio, best_snippet = _best_fuzzy_match(norm_quote, norm_page)
    if best_ratio >= _FUZZY_MATCH_THRESHOLD:
        return QuoteVerification(
            status="fuzzy_match", match_score=round(best_ratio, 4), matched_snippet=best_snippet)
    return QuoteVerification(
        status="not_found", match_score=round(best_ratio, 4), matched_snippet="")


def verify_quote_on_page(quote: str, page_text: str) -> QuoteVerification:
    """Check whether ``quote`` appears (verbatim or near-verbatim) in
    ``page_text``. Never raises; a blank quote or page always yields
    ``"not_found"``."""
    norm_quote = _normalize(quote)
    norm_page = _normalize((page_text or "")[:_MAX_PAGE_TEXT_CHARS])
    return _match_normalized(norm_quote, norm_page)


def verify_claims(
    claims: list,
    page_cache: dict,
    fetch_fn: Callable[[str], Optional[str]],
    max_verify_fetches: int = 5,
) -> list:
    """Verify every claim's quote against ``page_cache``, fetching missing
    pages (up to ``max_verify_fetches`` new fetches) via ``fetch_fn``.

    Mutates each claim's ``quote_verified`` / ``quote_verification_status`` /
    ``quote_match_score`` / ``quote_matched_snippet`` in place and returns
    the same list. Never touches ``quote`` or ``original_quote`` — quote
    correction is the caller's responsibility.

    ``page_cache`` maps ``source_url -> full page text`` for pages already
    fully fetched during collection (Firecrawl/plain-fetch pages); a claim
    whose ``source_url`` is not a key in it (e.g. a Serper-snippet URL whose
    full page was never retrieved) triggers a fresh ``fetch_fn(url)`` call,
    capped at ``max_verify_fetches`` per invocation. A successful fetch is
    written back into ``page_cache`` so later claims referencing the same
    URL reuse it instead of re-fetching. Claims beyond the cap are left
    untouched (default ``"not_checked"``), not marked as failed.

    Each unique page's text is normalized once (not once per claim) for
    performance, since several claims commonly share one ``source_url``.
    ``fetch_fn`` exceptions are caught and treated as a fetch failure —
    this function never raises.
    """
    norm_page_cache: dict[str, str] = {}
    fetch_count = 0

    for claim in claims:
        url = getattr(claim, "source_url", None)
        if not url:
            continue

        if url not in norm_page_cache:
            if url in page_cache:
                norm_page_cache[url] = _normalize(page_cache[url][:_MAX_PAGE_TEXT_CHARS])
            else:
                if fetch_count >= max_verify_fetches:
                    continue  # cap reached — leave as "not_checked"
                fetch_count += 1
                try:
                    fetched_text = fetch_fn(url)
                except Exception:
                    fetched_text = None
                if not fetched_text:
                    claim.quote_verified = False
                    claim.quote_verification_status = "fetch_failed"
                    claim.quote_match_score = 0.0
                    claim.quote_matched_snippet = ""
                    continue
                page_cache[url] = fetched_text
                norm_page_cache[url] = _normalize(fetched_text[:_MAX_PAGE_TEXT_CHARS])

        norm_page = norm_page_cache.get(url, "")
        if not norm_page:
            # A URL that WAS fetched (or cached) but yielded no usable text.
            claim.quote_verified = False
            claim.quote_verification_status = "fetch_failed"
            claim.quote_match_score = 0.0
            claim.quote_matched_snippet = ""
            continue

        verification = _match_normalized(_normalize(claim.quote), norm_page)
        claim.quote_verification_status = verification.status
        claim.quote_match_score = verification.match_score
        claim.quote_matched_snippet = verification.matched_snippet
        claim.quote_verified = verification.status in ("verified", "fuzzy_match")

    return claims
