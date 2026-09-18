import unittest

from hubspot_audit.normalization import (
    normalize_activity,
    normalize_company,
    normalize_contact,
    normalize_deal,
    normalize_domain,
    normalize_email,
)


class NormalizeDomainTests(unittest.TestCase):
    def test_strips_scheme_and_www(self):
        self.assertEqual(normalize_domain("https://www.Example.com/"), "example.com")

    def test_none_and_empty(self):
        self.assertIsNone(normalize_domain(None))
        self.assertIsNone(normalize_domain(""))

    def test_bare_domain_lowercased(self):
        self.assertEqual(normalize_domain("Example.COM"), "example.com")


class NormalizeEmailTests(unittest.TestCase):
    def test_lowercases_and_trims(self):
        self.assertEqual(normalize_email("  Foo@Example.com "), "foo@example.com")

    def test_none(self):
        self.assertIsNone(normalize_email(None))


class NormalizeRecordTests(unittest.TestCase):
    def test_company_associations(self):
        record = {
            "id": "1",
            "properties": {"name": "Acme", "domain": "Acme.COM", "parent_company_id": "5"},
            "associations": {},
        }
        company = normalize_company(record)
        self.assertEqual(company.normalized_domain, "acme.com")
        self.assertEqual(company.parent_company_id, "5")

    def test_contact_company_ids_from_associations(self):
        record = {
            "id": "2",
            "properties": {"email": "a@b.com"},
            "associations": {"companies": {"results": [{"id": "10"}, {"id": "11"}]}},
        }
        contact = normalize_contact(record)
        self.assertEqual(contact.company_ids, ["10", "11"])

    def test_deal_amount_parsing_handles_missing_and_bad_values(self):
        record = {"id": "3", "properties": {"amount": ""}, "associations": {}}
        self.assertIsNone(normalize_deal(record).amount)
        record2 = {"id": "4", "properties": {"amount": "1234.5"}, "associations": {}}
        self.assertEqual(normalize_deal(record2).amount, 1234.5)

    def test_activity_task_overdue_flag(self):
        record = {
            "id": "5",
            "properties": {"hs_task_status": "NOT_STARTED", "hs_task_due_date": "1700000000000"},
            "associations": {},
        }
        activity = normalize_activity(record, "task")
        self.assertTrue(activity.overdue)

        record["properties"]["hs_task_status"] = "COMPLETED"
        activity2 = normalize_activity(record, "task")
        self.assertFalse(activity2.overdue)


if __name__ == "__main__":
    unittest.main()
