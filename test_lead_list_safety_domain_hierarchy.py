from lead_list_safety import prematch_companies


def test_exact_name_and_subdomain_relation_matches():
    existing = [{
        "company_id": "kia-existing",
        "company_name": "Kia Worldwide",
        "domain": "worldwide.kia.com",
    }]
    report = prematch_companies([{
        "company_key": "incoming",
        "company_name": "Kia Worldwide",
        "email_domain_hint": "kia.com",
    }], existing)
    assert report["summary"]["matched_existing"] == 1
    assert report["entries"][0]["existing_company_id"] == "kia-existing"
    assert report["entries"][0]["match_basis"] == "exact_name_and_strong_domain"


def test_same_name_but_unrelated_domain_stays_ambiguous():
    existing = [{"company_id": "sk", "company_name": "SK Innovation", "domain": "skinnovation.com"}]
    report = prematch_companies([{
        "company_key": "incoming", "company_name": "SK Innovation", "email_domain_hint": "sk.com"
    }], existing)
    assert report["summary"]["ambiguous"] == 1
    assert report["entries"][0]["match_basis"] == "exact_name_only"
