from lead_list_protection import decide_protection


def test_active_customer_type_blocks_before_enrichment():
    decision = decide_protection(hubspot_records=[{
        "customer_type": "Customer-Success-AM (Company)",
        "type": "CUSTOMER",
        "lifecyclestage": "customer",
    }])
    assert decision.status == "blocked_current_customer"
    assert decision.may_enrich is False


def test_current_customer_flag_alone_does_not_block():
    decision = decide_protection(hubspot_records=[{
        "hs_current_customer": "yes",
        "type": "PROSPECT",
        "lifecyclestage": "lead",
    }])
    assert decision.status == "clear_new"
    assert decision.may_enrich is True


def test_lost_customer_requires_review():
    decision = decide_protection(hubspot_records=[{
        "customer_type": "Customer-Lost-NB (Company)",
    }])
    assert decision.status == "review_required"


def test_existing_cockpit_company_is_reused_not_reenriched():
    decision = decide_protection(cockpit_exact_matches=[{"company_id": "viseca-ch"}])
    assert decision.status == "reuse_existing_cockpit"
    assert decision.existing_company_id == "viseca-ch"
    assert decision.may_enrich is False
    assert decision.may_publish_as_new is False


def test_ambiguous_customer_signal_requires_review():
    decision = decide_protection(hubspot_records=[{
        "customer_type": "",
        "type": "CUSTOMER",
        "lifecyclestage": "customer",
    }])
    assert decision.status == "review_required"


def test_am_exact_match_is_hard_block():
    decision = decide_protection(am_exact_match=True)
    assert decision.status == "blocked_current_customer"
    assert decision.may_enrich is False


def test_multiple_cockpit_matches_require_review():
    decision = decide_protection(cockpit_exact_matches=[
        {"company_id": "a"}, {"company_id": "b"},
    ])
    assert decision.status == "review_required"
