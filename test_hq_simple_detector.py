"""Pytest tests for hq_simple_detector."""

import pytest
from hq_simple_detector import build_simple_hq_query, derive_domain_root, detect_hq_from_serper_payload


# ---------------------------------------------------------------------------
# derive_domain_root — standard cases
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
# derive_domain_root — public suffix / pseudo-TLD edge cases (Step 2C)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("input_domain, expected", [
    # .co.uk — registrable label is one level above the pseudo-TLD
    ("example.co.uk", "example"),
    ("www.example.co.uk", "example"),
    ("https://www.example.co.uk/about", "example"),
    ("subdomain.example.co.uk", "example"),
    # .com.au
    ("example.com.au", "example"),
    ("www.example.com.au", "example"),
    # .co.jp
    ("example.co.jp", "example"),
    ("www.example.co.jp", "example"),
    # Simple single-level ccTLDs — two-part domain, no pseudo-TLD
    ("subdomain.example.de/path", "example"),   # subdomain stripped, .de is simple TLD
    ("example.fr", "example"),
    ("example.it", "example"),
    # Subdomain with simple ccTLD
    ("corporate.acme.de", "acme"),
    ("www.acme.fr", "acme"),
])
def test_derive_domain_root_public_suffix(input_domain, expected):
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


# ---------------------------------------------------------------------------
# Step 2B — detect_hq_from_serper_payload
# All tests use synthetic payloads; no live API calls.
# ---------------------------------------------------------------------------

# ── A. Foreign HQ from knowledgeGraph ────────────────────────────────────────

def test_foreign_hq_from_knowledge_graph():
    """KG location field → foreign HQ score 3, no manual review."""
    payload = {
        "knowledgeGraph": {
            "headquarters": "Armonk, New York, United States",
        },
        "organic": [
            {
                "title": "IBM - Technology Company",
                "snippet": "IBM is headquartered in Armonk, New York, United States.",
                "link": "https://www.ibm.com/about",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="IBM",
        domain="ibm.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.foreign_hq_simple is True
    assert result.sig_foreign_hq_score_for_next_scoring == 3.0
    assert result.needs_manual_review is False
    assert result.hq_detected_country == "United States"
    assert result.domain_root == "ibm"
    assert result.query_used == "ibm headquarters"


def test_foreign_hq_from_answer_box():
    """answerBox snippet → foreign HQ score 3."""
    payload = {
        "answerBox": {
            "snippet": "IBM is headquartered in Armonk, New York, United States.",
        },
        "organic": [],
    }
    result = detect_hq_from_serper_payload(
        company_name="IBM",
        domain="ibm.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.foreign_hq_simple is True
    assert result.sig_foreign_hq_score_for_next_scoring == 3.0
    assert result.hq_detected_country == "United States"


# ── B. Same-country HQ → score 0, no manual review ───────────────────────────

def test_same_country_hq():
    """HQ in same country as input_country → not foreign, score 0."""
    payload = {
        "organic": [
            {
                "title": "Example Italia - Chi siamo",
                "snippet": "Example Italia è headquartered in Milan, Italy.",
                "link": "https://www.example.it/about",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Example Italia",
        domain="example.it",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.foreign_hq_simple is False
    assert result.sig_foreign_hq_score_for_next_scoring == 0.0


def test_same_country_hq_via_kg():
    """KG headquarters in input country → not foreign."""
    payload = {
        "knowledgeGraph": {
            "headquarters": "Rome, Italy",
        },
        "organic": [],
    }
    result = detect_hq_from_serper_payload(
        company_name="Acme Italia",
        domain="acme.it",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.foreign_hq_simple is False
    assert result.sig_foreign_hq_score_for_next_scoring == 0.0


# ── C. Regional HQ guard ──────────────────────────────────────────────────────

def test_regional_hq_guard_emea():
    """EMEA headquarters in organic → regional guard fires, no score 3."""
    payload = {
        "organic": [
            {
                "title": "Acme EMEA Headquarters Milan",
                "snippet": "Acme's European headquarters is located in Milan, Italy.",
                "link": "https://www.acme.com/emea",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Acme Corp",
        domain="acme.com",
        input_country="Italy",
        serper_payload=payload,
    )
    # Regional HQ guard must prevent score 3
    assert result.sig_foreign_hq_score_for_next_scoring != 3.0
    # Must either be manual review or clearly not-foreign
    assert result.needs_manual_review is True or result.foreign_hq_simple is not True


def test_regional_hq_guard_italy_headquarters():
    """'Italy headquarters' phrase → regional guard, needs_manual_review."""
    payload = {
        "organic": [
            {
                "title": "GlobalCo Italy Headquarters - Rome",
                "snippet": "GlobalCo Italy headquarters is based in Rome, Italy.",
                "link": "https://www.globalco.com/italy",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="GlobalCo Italia",
        domain="globalco.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring != 3.0
    assert result.needs_manual_review is True or result.foreign_hq_simple is not True


# ── D. Unclear evidence → manual review, score 0 ────────────────────────────

def test_no_hq_evidence_needs_manual_review():
    """Payload with no HQ-related content → needs_manual_review, score 0."""
    payload = {
        "organic": [
            {
                "title": "Acme Company - Products",
                "snippet": "Acme sells industrial equipment and services worldwide.",
                "link": "https://www.acme.it/products",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Acme",
        domain="acme.it",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.needs_manual_review is True
    assert result.sig_foreign_hq_score_for_next_scoring == 0.0


def test_empty_payload_needs_manual_review():
    """Empty payload → needs_manual_review, score 0."""
    result = detect_hq_from_serper_payload(
        company_name="Unknown Corp",
        domain="unknown.com",
        input_country="Italy",
        serper_payload={},
    )
    assert result.needs_manual_review is True
    assert result.sig_foreign_hq_score_for_next_scoring == 0.0


# ── E. Query builder unchanged (Step 2A regression) ──────────────────────────

@pytest.mark.parametrize("company, domain, expected_root, expected_query", [
    ("IBM", "ibm.com", "ibm", "ibm headquarters"),
    ("Bodycote", "bodycote.com", "bodycote", "bodycote headquarters"),
    ("Datwyler", "datwyler.com", "datwyler", "datwyler headquarters"),
    ("Nadara", "nadara.com", "nadara", "nadara headquarters"),
    ("Some Company S.p.A.", None, "some company", "some company headquarters"),
])
def test_step2a_query_builder_unchanged(company, domain, expected_root, expected_query):
    root, query = build_simple_hq_query(company, domain)
    assert root == expected_root
    assert query == expected_query


def test_detect_hq_sets_domain_root_and_query_used():
    """domain_root and query_used on result match Step 2A output."""
    result = detect_hq_from_serper_payload(
        company_name="Bodycote",
        domain="bodycote.com",
        input_country="Italy",
        serper_payload={},
    )
    assert result.domain_root == "bodycote"
    assert result.query_used == "bodycote headquarters"


# ── Extra: places fallback ────────────────────────────────────────────────────

def test_places_fallback_foreign_hq():
    """Places/local results used when organic has no HQ evidence."""
    payload = {
        "organic": [
            {
                "title": "Datwyler - Cable Solutions",
                "snippet": "Datwyler offers cable management products.",
                "link": "https://www.datwyler.com/products",
            }
        ],
        "places": [
            {
                "title": "Datwyler Group Headquarters",
                "address": "Headquartered in Altdorf, Switzerland",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Datwyler",
        domain="datwyler.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.hq_detected_country == "Switzerland"
    assert result.foreign_hq_simple is True


# ---------------------------------------------------------------------------
# Step 2C — false-positive fixture tests
# These scenarios must NEVER produce an automatic score-3 foreign HQ signal.
# ---------------------------------------------------------------------------

# ── A. Regional HQ only ───────────────────────────────────────────────────────

def test_fp_a_european_headquarters_no_score3():
    """'European headquarters in Milan' → regional guard fires, score 0, manual review."""
    payload = {
        "organic": [
            {
                "title": "GlobalCo European Headquarters - Milan",
                "snippet": (
                    "GlobalCo has established its European headquarters in Milan, "
                    "Italy to serve the EMEA region."
                ),
                "link": "https://www.globalco.com/about",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="GlobalCo",
        domain="globalco.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring == 0.0
    assert result.needs_manual_review is True


def test_fp_a_emea_hq_no_score3():
    """Explicit 'EMEA headquarters' → regional guard fires, score 0."""
    payload = {
        "organic": [
            {
                "title": "Acme EMEA Headquarters",
                "snippet": "Acme EMEA headquarters is located in Zurich, Switzerland.",
                "link": "https://www.acme.com/emea",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Acme",
        domain="acme.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring == 0.0
    assert result.needs_manual_review is True


# ── B. Sales office / branch office ──────────────────────────────────────────

def test_fp_b_sales_office_no_score3():
    """Sales office mention produces no score 3."""
    payload = {
        "organic": [
            {
                "title": "CompanyX Sales Office Milan",
                "snippet": "CompanyX operates a sales office in Milan, Italy.",
                "link": "https://www.companyx.com/italy",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="CompanyX",
        domain="companyx.com",
        input_country="Germany",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring != 3.0
    # Either no evidence found (manual review) or not foreign
    assert result.needs_manual_review is True or result.foreign_hq_simple is not True


def test_fp_b_branch_office_triggers_regional_guard():
    """Branch office phrase alongside headquarters triggers regional guard."""
    payload = {
        "organic": [
            {
                "title": "CompanyX UK Branch Office",
                "snippet": (
                    "CompanyX has a branch office headquartered in London, "
                    "United Kingdom."
                ),
                "link": "https://www.companyx.com/uk",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="CompanyX",
        domain="companyx.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring != 3.0
    assert result.needs_manual_review is True or result.foreign_hq_simple is not True


# ── C. Directory-only evidence ────────────────────────────────────────────────

def test_fp_c_linkedin_only_no_score3():
    """Only LinkedIn organic result → directory-only guard, no score 3."""
    payload = {
        "organic": [
            {
                "title": "Acme Corp - LinkedIn",
                "snippet": "Acme Corp is headquartered in Zurich, Switzerland.",
                "link": "https://www.linkedin.com/company/acme-corp",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Acme Corp",
        domain="acme.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring != 3.0
    assert result.needs_manual_review is True


def test_fp_c_zoominfo_only_no_score3():
    """Only ZoomInfo organic result → directory-only guard, no score 3."""
    payload = {
        "organic": [
            {
                "title": "Bodycote International - ZoomInfo",
                "snippet": "Bodycote is headquartered in Macclesfield, United Kingdom.",
                "link": "https://www.zoominfo.com/c/bodycote",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Bodycote",
        domain="bodycote.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring != 3.0
    assert result.needs_manual_review is True


# ── D. Unrelated-domain evidence ─────────────────────────────────────────────

def test_fp_d_unrelated_domain_no_score3():
    """Evidence from an unrelated domain → domain-mismatch guard, needs_manual_review."""
    payload = {
        "organic": [
            {
                "title": "Bodycote Group - Corporate Profile",
                "snippet": "Bodycote is headquartered in Macclesfield, United Kingdom.",
                "link": "https://www.unrelated-example.com/bodycote",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Bodycote",
        domain="bodycote.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.sig_foreign_hq_score_for_next_scoring != 3.0
    assert result.needs_manual_review is True
    # Reason must mention domain mismatch
    assert result.hq_reason is not None
    assert "unrelated_domain" in (result.hq_reason or "")


# ── E. Same-country KG ────────────────────────────────────────────────────────

def test_fp_e_same_country_germany_kg():
    """KG headquarters Berlin, Germany with input_country Germany → not foreign."""
    payload = {
        "knowledgeGraph": {
            "headquarters": "Berlin, Germany",
        },
        "organic": [],
    }
    result = detect_hq_from_serper_payload(
        company_name="GermanCo",
        domain="germanCo.de",
        input_country="Germany",
        serper_payload=payload,
    )
    assert result.foreign_hq_simple is False
    assert result.sig_foreign_hq_score_for_next_scoring == 0.0


# ── F. Foreign HQ with official-domain evidence → score 3, no manual review ──

def test_fp_f_official_domain_evidence_score3():
    """Official domain result with clear HQ phrase → foreign score 3, no manual review."""
    payload = {
        "organic": [
            {
                "title": "Bodycote - About Us",
                "snippet": (
                    "Bodycote is the world's largest provider of heat treatment. "
                    "Headquartered in Macclesfield, United Kingdom."
                ),
                "link": "https://www.bodycote.com/about-us/",
            }
        ],
    }
    result = detect_hq_from_serper_payload(
        company_name="Bodycote",
        domain="bodycote.com",
        input_country="Italy",
        serper_payload=payload,
    )
    assert result.foreign_hq_simple is True
    assert result.sig_foreign_hq_score_for_next_scoring == 3.0
    assert result.needs_manual_review is False
    assert result.hq_detected_country == "United Kingdom"
