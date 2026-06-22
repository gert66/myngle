"""Simple HQ query builder for Lead Prioritizer v2.

Stage 1 — dormant foundation. No Serper or Anthropic calls yet.

Query strategy
--------------
- If a domain is known, derive the domain root and query ``{domain_root} headquarters``.
- If no domain is known, fall back to ``{company_name} headquarters``.
- Do not use the company legal name when a domain is available.
- Do not use extra query ladders or quoted company-name queries.
- Forbidden query terms: ``head office``, ``global headquarters``,
  ``corporate headquarters``, ``parent company``, ``sede legale``, ``site:``.

Examples
--------
>>> derive_domain_root("ibm.com")
'ibm'
>>> build_simple_hq_query("IBM", "ibm.com")
('ibm headquarters', 'ibm.com')

>>> derive_domain_root("bodycote.com")
'bodycote'
>>> build_simple_hq_query("Bodycote", "bodycote.com")
('bodycote headquarters', 'bodycote.com')

>>> derive_domain_root("nadara.com")
'nadara'
>>> build_simple_hq_query("Nadara", "nadara.com")
('nadara headquarters', 'nadara.com')

>>> derive_domain_root("datwyler.com")
'datwyler'
>>> build_simple_hq_query("Datwyler", "datwyler.com")
('datwyler headquarters', 'datwyler.com')

>>> build_simple_hq_query("Some Company", None)
('Some Company headquarters', None)
"""

from __future__ import annotations

from typing import Optional


def derive_domain_root(domain: str) -> str:
    """Return the registrable part of a domain without TLD or subdomains.

    >>> derive_domain_root("ibm.com")
    'ibm'
    >>> derive_domain_root("www.bodycote.com")
    'bodycote'
    >>> derive_domain_root("nadara.com")
    'nadara'
    """
    # Strip scheme if accidentally included
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]

    # Drop path / query
    domain = domain.split("/")[0]

    # Drop subdomains (keep second-to-last label before TLD)
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def build_simple_hq_query(
    company_name: str, domain: Optional[str]
) -> tuple[str, Optional[str]]:
    """Return ``(query_string, domain_or_None)`` for a simple HQ search.

    Uses domain root when a domain is provided; falls back to company_name otherwise.

    >>> build_simple_hq_query("IBM", "ibm.com")
    ('ibm headquarters', 'ibm.com')
    >>> build_simple_hq_query("Some Company", None)
    ('Some Company headquarters', None)
    """
    if domain:
        root = derive_domain_root(domain)
        query = f"{root} headquarters"
        return query, domain

    query = f"{company_name} headquarters"
    return query, None
