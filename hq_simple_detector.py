"""Simple HQ query builder for Lead Prioritizer v2.

Stage 1 — dormant foundation. No Serper or Anthropic calls are made here.

Query strategy
--------------
- If a domain is known, derive the domain root and return
  ``(domain_root, "{domain_root} headquarters")``.
- If no domain is known, derive a safe fallback root from ``company_name``
  and return ``(fallback_root, "{fallback_root} headquarters")``.
- Do not use the company legal name when a domain is available.
- Do not use extra query ladders or quoted company-name queries.
- Forbidden query terms: ``head office``, ``global headquarters``,
  ``corporate headquarters``, ``parent company``, ``sede legale``, ``site:``.

Return value
------------
Both functions return ``(root, query)`` — the root always comes first so
downstream pipeline steps can reference it independently.

Examples
--------
>>> derive_domain_root("ibm.com")
'ibm'
>>> build_simple_hq_query("IBM", "ibm.com")
('ibm', 'ibm headquarters')

>>> derive_domain_root("bodycote.com")
'bodycote'
>>> build_simple_hq_query("Bodycote", "bodycote.com")
('bodycote', 'bodycote headquarters')

>>> derive_domain_root("nadara.com")
'nadara'
>>> build_simple_hq_query("Nadara", "nadara.com")
('nadara', 'nadara headquarters')

>>> derive_domain_root("datwyler.com")
'datwyler'
>>> build_simple_hq_query("Datwyler", "datwyler.com")
('datwyler', 'datwyler headquarters')

>>> build_simple_hq_query("Some Company S.p.A.", None)
('some company', 'some company headquarters')
"""

from __future__ import annotations

import re
from typing import Optional

# Legal-form suffixes to strip when building a company-name fallback root.
# Kept deliberately conservative — only unambiguous suffixes.
_LEGAL_SUFFIXES_RE = re.compile(
    r"""
    [\s,\.\-]+          # optional separator before suffix
    (?:
        s\.?p\.?a\.?    # S.p.A. / SPA
      | s\.?r\.?l\.?    # S.r.l. / SRL
      | s\.?a\.?s\.?    # S.a.s.
      | s\.?n\.?c\.?    # S.n.c.
      | b\.?v\.?        # B.V.
      | n\.?v\.?        # N.V.
      | gmbh            # GmbH
      | ag              # AG
      | ltd\.?          # Ltd
      | llc             # LLC
      | inc\.?          # Inc.
      | corp\.?         # Corp.
      | s\.?a\.?        # S.A.  (must come after s.a.s.)
      | aps             # ApS
      | a\/s            # A/S
      | oy              # Oy
      | ab              # AB
      | plc             # PLC
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def derive_domain_root(domain: str) -> str:
    """Return the registrable label of a domain (no TLD, no subdomains).

    Handles common messy inputs without crashing on blanks or malformed values.

    >>> derive_domain_root("ibm.com")
    'ibm'
    >>> derive_domain_root("www.bodycote.com")
    'bodycote'
    >>> derive_domain_root("https://nadara.com/about")
    'nadara'
    >>> derive_domain_root("http://www.datwyler.com")
    'datwyler'
    >>> derive_domain_root("")
    ''
    """
    if not domain or not domain.strip():
        return ""

    d = domain.strip()

    # Strip scheme
    d = re.sub(r"^https?://", "", d, flags=re.IGNORECASE)

    # Strip markdown link syntax: [text](url) — take the url part
    md_match = re.match(r"^\[.*?\]\((https?://)?([^)]+)\)", d)
    if md_match:
        d = md_match.group(2)

    # Drop path, query, fragment
    d = d.split("/")[0].split("?")[0].split("#")[0]

    # Drop port
    d = d.split(":")[0]

    d = d.strip().lower()
    if not d:
        return ""

    # Drop subdomains: keep the second-to-last label before the TLD.
    # This handles www.example.com → example, corporate.ibm.com → ibm, etc.
    parts = [p for p in d.split(".") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def _company_name_fallback_root(company_name: str) -> str:
    """Derive a safe, lowercase query root from a company name.

    Strips legal-form suffixes (S.p.A., Ltd, GmbH, …) and collapses whitespace.
    """
    root = company_name.strip()
    # Remove legal suffixes
    root = _LEGAL_SUFFIXES_RE.sub("", root).strip(" ,.")
    # Collapse internal whitespace; lowercase
    root = re.sub(r"\s+", " ", root).lower()
    return root


def build_simple_hq_query(
    company_name: str,
    domain: Optional[str],
) -> tuple[str, str]:
    """Return ``(root, query)`` for a simple HQ search.

    - When *domain* is provided the root is the domain's registrable label and
      the query is ``"{root} headquarters"``.  The company legal name is NOT used.
    - When *domain* is absent or blank the root is derived from *company_name*
      (legal suffixes stripped, lower-cased).

    Both elements of the returned tuple are plain strings — never ``None``.

    >>> build_simple_hq_query("IBM", "ibm.com")
    ('ibm', 'ibm headquarters')
    >>> build_simple_hq_query("Bodycote", "bodycote.com")
    ('bodycote', 'bodycote headquarters')
    >>> build_simple_hq_query("Datwyler", "datwyler.com")
    ('datwyler', 'datwyler headquarters')
    >>> build_simple_hq_query("Nadara", "nadara.com")
    ('nadara', 'nadara headquarters')
    >>> build_simple_hq_query("Some Company S.p.A.", None)
    ('some company', 'some company headquarters')
    """
    if domain and domain.strip():
        root = derive_domain_root(domain.strip())
        if root:
            return root, f"{root} headquarters"

    # No domain (or derive_domain_root returned empty) — use company name
    root = _company_name_fallback_root(company_name)
    if not root:
        root = company_name.strip().lower()
    return root, f"{root} headquarters"
