import json
import os
import shutil
import tempfile
import unittest

from hubspot_population_audit import properties
from hubspot_population_audit.snapshot import Snapshot

FIXTURE_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "sample_snapshot"
)

_COMPANY_DEFAULTS = [
    "createdate",
    "hs_lastmodifieddate",
    "name",
    "domain",
    "hs_object_source",
    "lifecyclestage",
    "hubspot_owner_id",
]
# Deliberately NOT included, to exercise properties_unavailable:
_COMPANY_UNAVAILABLE = ["annualrevenue", "type", "numberofemployees"]


def _company_property_definitions():
    defs = [
        {"name": name, "label": name, "type": "string", "hubspotDefined": True, "groupName": "companyinformation"}
        for name in _COMPANY_DEFAULTS
    ]
    # Rule 1: hubspotDefined explicitly False.
    defs.append({"name": "myngle_a", "label": "A", "hubspotDefined": False, "groupName": "companyinformation"})
    # Rule 2: createdUserId present.
    defs.append(
        {"name": "myngle_b", "label": "B", "hubspotDefined": True, "createdUserId": 555, "groupName": "companyinformation"}
    )
    # Rule 3: groupName outside the default company-info group, and
    # deliberately NOT hubspotDefined=True (rule 3 must never match a
    # definition HubSpot itself marks as a stock/default property).
    defs.append({"name": "myngle_c", "label": "C", "groupName": "myngle_group"})
    # Two more custom candidates purely to exercise the cap/overflow split.
    defs.append({"name": "myngle_d", "label": "D", "hubspotDefined": False})
    defs.append({"name": "myngle_e", "label": "E", "hubspotDefined": False})
    return defs


def _contact_property_definitions():
    defs = [
        {"name": name, "label": name, "type": "string", "hubspotDefined": True, "groupName": "contactinformation"}
        for name in ("createdate", "lastmodifieddate", "email", "firstname", "lastname", "lifecyclestage")
    ]
    defs.append(
        {"name": "myngle_contact_field", "label": "F", "hubspotDefined": True, "createdUserId": 42, "groupName": "contactinformation"}
    )
    return defs


class PropertyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        shutil.copytree(os.path.join(FIXTURE_SNAPSHOT, "raw"), os.path.join(self.tmp_dir, "raw"))
        with open(os.path.join(self.tmp_dir, "raw", "properties_companies.json"), "w", encoding="utf-8") as fh:
            json.dump({"properties": _company_property_definitions(), "extracted_at": "2026-09-18T00:00:00Z"}, fh)
        with open(os.path.join(self.tmp_dir, "raw", "properties_contacts.json"), "w", encoding="utf-8") as fh:
            json.dump({"properties": _contact_property_definitions(), "extracted_at": "2026-09-18T00:00:00Z"}, fh)
        self.snapshot = Snapshot(self.tmp_dir)

    def test_requested_properties_intersected_with_available(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=40)
        for name in _COMPANY_DEFAULTS:
            self.assertIn(name, resolved["properties_requested"])
        for name in _COMPANY_UNAVAILABLE:
            self.assertIn(name, resolved["properties_unavailable"])
            self.assertNotIn(name, resolved["properties_requested"])

    def test_custom_field_rule_1_hubspot_defined_false(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=40)
        self.assertIn("myngle_a", resolved["properties_requested"])

    def test_custom_field_rule_2_created_user_id(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=40)
        self.assertIn("myngle_b", resolved["properties_requested"])

    def test_custom_field_rule_3_non_default_group(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=40)
        self.assertIn("myngle_c", resolved["properties_requested"])

    def test_default_company_info_fields_are_never_custom(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=40)
        self.assertEqual(resolved["custom_properties_discovered"], 5)  # a, b, c, d, e

    def test_custom_property_cap_and_overflow(self):
        # Tiering: rule-1/rule-2 matches (myngle_a, myngle_b, myngle_d, myngle_e)
        # are ordered ahead of the rule-3-only match (myngle_c), so the cap of 3
        # is filled from the priority tier (alphabetically: a, b, d) before c.
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=3)
        self.assertEqual(resolved["custom_properties_included"], ["myngle_a", "myngle_b", "myngle_d"])
        self.assertEqual(resolved["custom_properties_overflow"], ["myngle_e", "myngle_c"])
        for name in resolved["custom_properties_overflow"]:
            self.assertNotIn(name, resolved["properties_requested"])

    def test_custom_properties_rules_persisted_for_included_properties(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=40)
        self.assertEqual(resolved["custom_properties_rules"]["myngle_a"], ["hubspotDefined_false"])
        self.assertEqual(resolved["custom_properties_rules"]["myngle_b"], ["createdUserId"])
        self.assertEqual(resolved["custom_properties_rules"]["myngle_c"], ["non_default_group"])

    def test_contacts_created_user_id_rule_and_no_false_positives(self):
        resolved = properties.resolve_properties(self.snapshot, "contacts", custom_cap=40)
        self.assertIn("myngle_contact_field", resolved["properties_requested"])
        self.assertEqual(resolved["custom_properties_discovered"], 1)

    def test_fixed_properties_never_double_counted_as_custom(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=40)
        for name in _COMPANY_DEFAULTS:
            self.assertNotIn(name, resolved["custom_properties_included"])
            self.assertNotIn(name, resolved["custom_properties_overflow"])


def _hubspot_default_group_definitions():
    """Many HubSpot-shipped default properties living in default groups
    other than companyinformation/contactinformation, exactly the false
    positive rule 3 must not produce."""
    defs = []
    for group in ("emailinformation", "web_analytics", "socialmediainformation", "conversioninformation"):
        for i in range(5):
            defs.append(
                {"name": f"{group}_{i}", "label": f"{group} {i}", "hubspotDefined": True, "groupName": group}
            )
    return defs


class CustomPropertyTieringTests(unittest.TestCase):
    """A mixed-definition regression for the cap-tiering fix: many
    hubspotDefined==True default-group properties (which must never be
    flagged custom) plus a couple of genuine, human-created mYngle fields
    whose names sort alphabetically last -- they must survive a small cap."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        shutil.copytree(os.path.join(FIXTURE_SNAPSHOT, "raw"), os.path.join(self.tmp_dir, "raw"))
        defs = [
            {"name": name, "label": name, "hubspotDefined": True, "groupName": "companyinformation"}
            for name in ("createdate", "hs_lastmodifieddate", "name")
        ]
        defs.extend(_hubspot_default_group_definitions())
        defs.append(
            {
                "name": "zz_myngle_1",
                "label": "Z1",
                "hubspotDefined": True,
                "createdUserId": 111,
                "groupName": "sales_properties",
            }
        )
        defs.append({"name": "zz_myngle_2", "label": "Z2", "hubspotDefined": False})
        with open(os.path.join(self.tmp_dir, "raw", "properties_companies.json"), "w", encoding="utf-8") as fh:
            json.dump({"properties": defs, "extracted_at": "2026-09-18T00:00:00Z"}, fh)
        self.snapshot = Snapshot(self.tmp_dir)

    def test_hubspot_defined_true_default_group_properties_are_never_custom(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=2)
        # Only the two genuine zz_myngle_* fields are discovered as custom --
        # none of the 20 hubspotDefined==True default-group properties count.
        self.assertEqual(resolved["custom_properties_discovered"], 2)

    def test_created_user_id_and_hubspot_defined_false_fields_survive_a_small_cap(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=2)
        self.assertIn("zz_myngle_1", resolved["properties_requested"])
        self.assertIn("zz_myngle_2", resolved["properties_requested"])
        self.assertEqual(resolved["custom_properties_overflow"], [])

    def test_matched_rules_persisted_for_the_mixed_fixture(self):
        resolved = properties.resolve_properties(self.snapshot, "companies", custom_cap=2)
        self.assertEqual(resolved["custom_properties_rules"]["zz_myngle_1"], ["createdUserId"])
        self.assertEqual(resolved["custom_properties_rules"]["zz_myngle_2"], ["hubspotDefined_false"])


class MinimalFixtureSnapshotTests(unittest.TestCase):
    """Sanity check against the real (minimal) sample_snapshot fixture,
    which carries no hubspotDefined/groupName/createdUserId metadata at
    all -- no field should be misclassified as custom in that case."""

    def test_no_false_positive_custom_fields_on_minimal_fixture(self):
        snapshot = Snapshot(FIXTURE_SNAPSHOT)
        resolved = properties.resolve_properties(snapshot, "companies")
        self.assertEqual(resolved["custom_properties_discovered"], 0)


if __name__ == "__main__":
    unittest.main()
