"""Tests for linkedin_backfill — the pure validation + record-transform core.
No network: the Lusha lookup is injected as a plain dict-backed stub."""

from linkedin_backfill import (
    STATUS_ALREADY_VALID,
    STATUS_BACKFILLED,
    STATUS_NO_DOMAIN,
    STATUS_NO_LINKEDIN,
    STATUS_REJECTED_NOT_COMPANY,
    STATUS_SKIPPED_BUDGET,
    LookupBudget,
    apply_linkedin_url,
    backfill_details_bucket,
    build_backfill_manifest,
    build_backfilled_run,
    count_backfill_candidates,
    is_company_linkedin_url,
    needs_backfill,
    pick_detail_domain,
)

NOW = "2026-07-23T12:00:00+00:00"


class TestIsCompanyLinkedInUrl:
    def test_accepts_company_page(self):
        assert is_company_linkedin_url("https://www.linkedin.com/company/audi")

    def test_accepts_country_subdomain_company(self):
        assert is_company_linkedin_url("https://it.linkedin.com/company/audi-italia")

    def test_accepts_showcase(self):
        assert is_company_linkedin_url("https://www.linkedin.com/showcase/audi-sport")

    def test_rejects_job_posting(self):
        # Audi Italia's real stored link — an unrelated vacancy.
        assert not is_company_linkedin_url(
            "https://linkedin.com/jobs/view/coach-at-msx-international-4436748354/")

    def test_rejects_feed_post(self):
        assert not is_company_linkedin_url(
            "https://www.linkedin.com/posts/some-co_activity-7132704355987677184")

    def test_rejects_personal_profile(self):
        assert not is_company_linkedin_url("https://it.linkedin.com/in/valeria-ventura")

    def test_rejects_school(self):
        assert not is_company_linkedin_url("https://www.linkedin.com/school/some-university")

    def test_rejects_company_with_no_slug(self):
        assert not is_company_linkedin_url("https://www.linkedin.com/company/")

    def test_rejects_non_linkedin_host(self):
        assert not is_company_linkedin_url("https://evil-linkedin.com/company/x")

    def test_rejects_lookalike_host(self):
        assert not is_company_linkedin_url("https://notlinkedin.com/company/x")

    def test_rejects_empty_and_non_string(self):
        assert not is_company_linkedin_url("")
        assert not is_company_linkedin_url(None)
        assert not is_company_linkedin_url(123)

    def test_rejects_garbage(self):
        assert not is_company_linkedin_url("not a url")


class TestPickDetailDomain:
    def test_prefers_domain_field(self):
        assert pick_detail_domain({"domain": "audi.it", "website_url": "https://audi.it"}) == "audi.it"

    def test_falls_back_to_website(self):
        assert pick_detail_domain({"website_url": "https://audi.it/en"}) == "https://audi.it/en"

    def test_empty_when_nothing(self):
        assert pick_detail_domain({}) == ""
        assert pick_detail_domain({"domain": "  "}) == ""


class TestNeedsBackfill:
    def test_false_when_valid_company_url_present(self):
        assert not needs_backfill({"linkedin_url": "https://www.linkedin.com/company/audi"})

    def test_true_when_junk_url(self):
        assert needs_backfill({"linkedin_url": "https://linkedin.com/jobs/view/x-123"})

    def test_true_when_absent(self):
        assert needs_backfill({})


class TestApplyLinkedInUrl:
    def test_backfills_company_url(self):
        detail = {"company_id": "c1", "linkedin_url": "https://linkedin.com/jobs/view/x-1"}
        new, status = apply_linkedin_url(
            detail, "https://it.linkedin.com/company/audi-italia", now_iso=NOW)
        assert status == STATUS_BACKFILLED
        assert new["linkedin_url"] == "https://it.linkedin.com/company/audi-italia"
        assert new["linkedin_backfill_audit"]["previous_linkedin_url"] == \
            "https://linkedin.com/jobs/view/x-1"
        # original untouched (new dict)
        assert detail["linkedin_url"] == "https://linkedin.com/jobs/view/x-1"

    def test_rejects_non_company_url_and_leaves_record(self):
        detail = {"company_id": "c1", "linkedin_url": "old"}
        new, status = apply_linkedin_url(
            detail, "https://www.linkedin.com/jobs/view/x-2", now_iso=NOW)
        assert status == STATUS_REJECTED_NOT_COMPANY
        assert new is detail  # unchanged

    def test_no_linkedin_when_empty(self):
        detail = {"company_id": "c1"}
        new, status = apply_linkedin_url(detail, "", now_iso=NOW)
        assert status == STATUS_NO_LINKEDIN
        assert new is detail


class TestLookupBudget:
    def test_unlimited(self):
        b = LookupBudget(None)
        assert all(b.take() for _ in range(1000))

    def test_bounded(self):
        b = LookupBudget(2)
        assert b.take() is True
        assert b.take() is True
        assert b.take() is False
        assert b.take() is False

    def test_zero(self):
        assert LookupBudget(0).take() is False


class TestBackfillDetailsBucket:
    def _bucket(self):
        return {
            "c1": {"company_id": "c1", "domain": "audi.it"},  # needs lookup -> company URL
            "c2": {"company_id": "c2", "linkedin_url": "https://www.linkedin.com/company/kept"},  # already valid
            "c3": {"company_id": "c3"},  # no domain
            "c4": {"company_id": "c4", "domain": "junk.it"},  # lookup returns junk -> rejected
            "c5": {"company_id": "c5", "domain": "nolink.it"},  # lookup returns "" -> no_linkedin
        }

    def _lookup(self):
        table = {
            "audi.it": "https://it.linkedin.com/company/audi-italia",
            "junk.it": "https://www.linkedin.com/jobs/view/x-9",
            "nolink.it": "",
        }
        return lambda domain: table.get(domain, "")

    def test_each_status_counted(self):
        counters: dict = {}
        out = backfill_details_bucket(
            self._bucket(), self._lookup(), now_iso=NOW,
            budget=LookupBudget(None), counters=counters)
        assert counters[STATUS_BACKFILLED] == 1
        assert counters[STATUS_ALREADY_VALID] == 1
        assert counters[STATUS_NO_DOMAIN] == 1
        assert counters[STATUS_REJECTED_NOT_COMPANY] == 1
        assert counters[STATUS_NO_LINKEDIN] == 1
        assert out["c1"]["linkedin_url"] == "https://it.linkedin.com/company/audi-italia"
        assert out["c2"]["linkedin_url"] == "https://www.linkedin.com/company/kept"
        assert "linkedin_url" not in out["c4"]

    def test_budget_stops_lookups(self):
        counters: dict = {}
        calls = []
        def counting_lookup(domain):
            calls.append(domain)
            return "https://www.linkedin.com/company/x"
        out = backfill_details_bucket(
            self._bucket(), counting_lookup, now_iso=NOW,
            budget=LookupBudget(1), counters=counters)
        # Only one company that needs a lookup actually got one.
        assert len(calls) == 1
        assert counters.get(STATUS_BACKFILLED, 0) == 1
        assert counters.get(STATUS_SKIPPED_BUDGET, 0) >= 1

    def test_checkpoint_reapplied_without_lookup(self):
        counters: dict = {}
        calls = []
        def counting_lookup(domain):
            calls.append(domain)
            return "https://www.linkedin.com/company/fresh"
        checkpoint = {
            "c1": {"status": STATUS_BACKFILLED,
                   "linkedin_url": "https://it.linkedin.com/company/from-checkpoint"},
        }
        out = backfill_details_bucket(
            self._bucket(), counting_lookup, now_iso=NOW,
            budget=LookupBudget(None), counters=counters, checkpoint=checkpoint)
        # c1 came from the checkpoint, so it wasn't looked up again.
        assert "audi.it" not in calls
        assert out["c1"]["linkedin_url"] == "https://it.linkedin.com/company/from-checkpoint"


class TestBuildBackfilledRun:
    def test_list_items_pass_through_and_manifest_counts(self):
        current = {
            "list_items": [{"company_id": "c1"}],
            "detail_files": {
                "company-details-0.json": {
                    "c1": {"company_id": "c1", "domain": "audi.it"},
                },
            },
            "manifest": {"generated_at": "orig"},
        }
        lookup = lambda d: "https://it.linkedin.com/company/audi-italia"
        run = build_backfilled_run(
            current, lookup, country_folder="italy", run_folder="r1",
            now_iso=NOW, budget=LookupBudget(None))
        assert run["list_items"] == current["list_items"]  # unchanged
        assert run["manifest"]["companies_backfilled"] == 1
        assert run["manifest"]["run_type"] == "linkedin_backfill"
        assert run["manifest"]["source_current_manifest"] == {"generated_at": "orig"}


class TestCountCandidates:
    def test_counts(self):
        current = {
            "detail_files": {
                "f0.json": {
                    "c1": {"domain": "a.it"},  # needs
                    "c2": {"linkedin_url": "https://www.linkedin.com/company/x"},  # already
                    "c3": {},  # no domain
                },
            },
        }
        needs, already, no_domain = count_backfill_candidates(current)
        assert (needs, already, no_domain) == (1, 1, 1)


class TestBuildBackfillManifest:
    def test_totals(self):
        counters = {
            STATUS_BACKFILLED: 3, STATUS_ALREADY_VALID: 5,
            STATUS_NO_DOMAIN: 2, STATUS_REJECTED_NOT_COMPANY: 1,
        }
        m = build_backfill_manifest(
            country_folder="italy", source_current_manifest=None,
            run_folder="r1", counters=counters, generated_at=NOW)
        assert m["companies_total"] == 11
        assert m["companies_backfilled"] == 3
        assert m["companies_rejected_not_company"] == 1
        assert m["promoted_to_current"] is False
