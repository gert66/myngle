"""Pytest tests for hq_simple_detector."""

import pytest
from hq_simple_detector import build_simple_hq_query, derive_domain_root


# ---------------------------------------------------------------------------
# derive_domain_root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("input_domain, expected", [
    ("ibm.com", "ibm"),
    ("bodycote.com", "bodycote"),
    ("www.bodycote.com", "bodycote"),
    ("https://nadara.com/about", "nadara"),
    ("http://www.datwyler.com", "datwyler"),
    ("corporate.ibm.com", "ibm"),
    ("alphastream.io", "alphastream"),
    ("[www.bodycote.com](https://www.bodycote.com)", "bodycote"),
    ("", ""),
    ("   ", ""),
])
def test_derive_domain_root(input_domain, expected):
    assert derive_domain_root(input_domain) == expected


# ---------------------------------------------------------------------------
# build_simple_hq_query — domain provided
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("company, domain, expected", [
    ("IBM", "ibm.com", ("ibm", "ibm headquarters")),
    ("Bodycote", "bodycote.com", ("bodycote", "bodycote headquarters")),
    ("Datwyler", "datwyler.com", ("datwyler", "datwyler headquarters")),
    ("Nadara", "nadara.com", ("nadara", "nadara headquarters")),
    ("AlphaStream Technologies B.V.", "alphastream.io", ("alphastream", "alphastream headquarters")),
])
def test_build_simple_hq_query_with_domain(company, domain, expected):
    assert build_simple_hq_query(company, domain) == expected


# ---------------------------------------------------------------------------
# build_simple_hq_query — no domain (company-name fallback)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("company, suffix_fragment", [
    ("Some Company S.p.A.", "s.p.a"),
    ("Acme Ltd.", "ltd"),
    ("MegaCorp GmbH", "gmbh"),
    ("DataFlow B.V.", "b.v"),
    ("Holdings S.A.", "s.a"),
])
def test_build_simple_hq_query_no_domain_strips_legal_suffix(company, suffix_fragment):
    root, query = build_simple_hq_query(company, None)
    assert suffix_fragment not in root
    assert root in query
    assert query.endswith("headquarters")


def test_build_simple_hq_query_no_domain_example():
    assert build_simple_hq_query("Some Company S.p.A.", None) == (
        "some company",
        "some company headquarters",
    )


def test_build_simple_hq_query_returns_strings_not_none():
    root, query = build_simple_hq_query("Unnamed", None)
    assert isinstance(root, str)
    assert isinstance(query, str)
