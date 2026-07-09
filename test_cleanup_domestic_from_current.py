import unittest

from cleanup_domestic_from_current import filter_foreign_hq_only


def make_item(company_id: str, foreign: bool) -> dict:
    return {
        "company_id": company_id,
        "company_name": company_id.title(),
        "foreign_hq_detected_for_export": foreign,
    }


class TestFilterForeignHqOnly(unittest.TestCase):
    def test_keeps_only_foreign_hq_items(self):
        items = [make_item("a", True), make_item("b", False), make_item("c", True)]
        details = {"a": {"company_id": "a"}, "b": {"company_id": "b"}, "c": {"company_id": "c"}}

        kept_items, kept_details, dropped_items = filter_foreign_hq_only(items, details)

        self.assertEqual([i["company_id"] for i in kept_items], ["a", "c"])
        self.assertEqual(set(kept_details.keys()), {"a", "c"})
        self.assertEqual([i["company_id"] for i in dropped_items], ["b"])

    def test_missing_field_treated_as_not_foreign(self):
        items = [{"company_id": "x", "company_name": "X"}]
        kept_items, kept_details, dropped_items = filter_foreign_hq_only(items, {})
        self.assertEqual(kept_items, [])
        self.assertEqual([i["company_id"] for i in dropped_items], ["x"])

    def test_no_dropped_when_all_foreign(self):
        items = [make_item("a", True), make_item("b", True)]
        details = {"a": {}, "b": {}}
        kept_items, kept_details, dropped_items = filter_foreign_hq_only(items, details)
        self.assertEqual(len(kept_items), 2)
        self.assertEqual(dropped_items, [])

    def test_preserves_order(self):
        items = [make_item("b", True), make_item("a", True)]
        kept_items, _, _ = filter_foreign_hq_only(items, {})
        self.assertEqual([i["company_id"] for i in kept_items], ["b", "a"])

    def test_details_without_matching_list_item_are_dropped(self):
        items = [make_item("a", True)]
        details = {"a": {"company_id": "a"}, "orphan": {"company_id": "orphan"}}
        _, kept_details, _ = filter_foreign_hq_only(items, details)
        self.assertEqual(set(kept_details.keys()), {"a"})


if __name__ == "__main__":
    unittest.main()
