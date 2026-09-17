from lead_list_safety import prematch_companies


def test_name_and_existing_contact_email_domain_is_strong_match():
    existing = [{
        "company_id": "nexen",
        "company_name": "NEXEN TIRE",
        "domain": "newsroom.nexentire.com",
    }]
    details = {
        "nexen": {"contact": {"work_email": "person@nexentire.com"}}
    }
    report = prematch_companies(
        [{
            "company_key": "incoming",
            "company_name": "NEXEN TIRE",
            "email_domain_hint": "nexentire.com",
        }],
        existing,
        existing_details=details,
    )
    assert report["summary"]["matched_existing"] == 1
    assert report["entries"][0]["existing_company_id"] == "nexen"
    assert report["entries"][0]["match_basis"] == "exact_name_and_strong_domain"
