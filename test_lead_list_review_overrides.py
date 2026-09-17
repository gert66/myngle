from lead_list_safety import apply_reviewed_match_overrides


def test_review_override_only_resolves_existing_candidate():
    prematch = {
        "summary": {"matched_existing": 0, "new": 0, "ambiguous": 1, "total": 1},
        "entries": [{
            "source_company_key": "k1",
            "company_name": "Example",
            "action": "ambiguous",
            "existing_company_id": None,
            "match_basis": "exact_name_only",
            "confidence": "review",
            "candidate_company_ids": ["existing-1"],
        }],
    }
    out = apply_reviewed_match_overrides(prematch, [{
        "source_company_key": "k1",
        "existing_company_id": "existing-1",
        "reviewer": "protected-live-review",
        "evidence": ["https://example.com"],
    }])
    assert out["summary"]["matched_existing"] == 1
    assert out["summary"]["ambiguous"] == 0
    assert out["entries"][0]["match_basis"] == "reviewed_identity_match"
