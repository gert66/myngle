"""Property discovery for targeted evidence lookups.

Resolves, per object type, a fixed requested-property list (intersected
with what the snapshot's property definitions actually contain -- never
assumed present) plus discovered "custom" (mYngle) property candidates,
capped at a configurable maximum so a single lookup batch never requests an
unbounded number of properties.

Custom-field rule (documented, not just inferred): a discovered property
definition is treated as a custom/mYngle candidate if ANY of --

  1. ``hubspotDefined`` is explicitly ``False`` (HubSpot's own flag for a
     portal-created, non-stock property);
  2. ``createdUserId`` is present/non-null (a human created it -- HubSpot's
     own stock properties never carry a creator);
  3. ``groupName`` is present, ``hubspotDefined`` is NOT explicitly
     ``True``, and the group does not start with the default
     ``companyinformation`` / ``contactinformation`` group prefixes (the
     HubSpot-shipped default property groups for these two object types).

Rule 3 deliberately excludes any definition with ``hubspotDefined is
True``: on a live portal there are dozens of HubSpot-shipped default
property groups other than ``companyinformation``/``contactinformation``
(``emailinformation``, ``socialmediainformation``, ``web_analytics``,
``conversioninformation``, ...); without this exclusion rule 3 alone would
flag all of them as "custom" and, combined with the cap, could push
genuine mYngle fields into overflow. Properties already covered by the
fixed requested list are always excluded from the "custom candidate" set
(they are requested directly, not rediscovered).

When the number of discovered candidates exceeds ``custom_cap``, fields
matched by rule 1 or rule 2 (a human-created or explicitly-non-stock
property -- the strongest signal of a genuine mYngle field) are included
ahead of any field matched *only* by rule 3, so a plausible default-group
false positive never displaces a genuine mYngle field. Within each tier,
candidates are ordered alphabetically for determinism. The rule(s) that
matched each *included* property are persisted in the resolved dict under
``custom_properties_rules`` (e.g. ``{"myngle_b": ["createdUserId"]}``).
"""
from __future__ import annotations

from .snapshot import Snapshot

DEFAULT_CUSTOM_PROPERTY_CAP = 40

_DEFAULT_GROUP_PREFIXES = ("companyinformation", "contactinformation")

REQUESTED_PROPERTIES = {
    "companies": [
        "createdate",
        "hs_lastmodifieddate",
        "name",
        "domain",
        "hs_object_source",
        "hs_object_source_label",
        "hs_object_source_detail_1",
        "hs_object_source_detail_2",
        "hs_object_source_id",
        "hs_object_source_user_id",
        "lifecyclestage",
        "hubspot_owner_id",
        "hs_num_open_deals",
        "num_associated_contacts",
        "num_associated_deals",
        "hs_last_sales_activity_timestamp",
        "notes_last_updated",
        "notes_last_contacted",
        "hs_analytics_source",
        "hs_analytics_source_data_1",
        "hs_analytics_source_data_2",
        "hs_latest_source",
        "hs_lead_status",
        "hs_is_target_account",
        "hs_merged_object_ids",
        "industry",
        "country",
        "city",
        "numberofemployees",
        "annualrevenue",
        "type",
    ],
    "contacts": [
        "createdate",
        "lastmodifieddate",
        "email",
        "firstname",
        "lastname",
        "hs_object_source",
        "hs_object_source_label",
        "hs_object_source_detail_1",
        "hs_object_source_detail_2",
        "hs_object_source_id",
        "hs_object_source_user_id",
        "lifecyclestage",
        "hubspot_owner_id",
        "hs_lead_status",
        "associatedcompanyid",
        "num_associated_deals",
        "hs_email_domain",
        "hs_last_sales_activity_timestamp",
        "notes_last_updated",
        "notes_last_contacted",
        "hs_analytics_source",
        "hs_analytics_source_data_1",
        "hs_analytics_source_data_2",
        "hs_latest_source",
        "hs_email_last_send_date",
        "hs_email_last_open_date",
        "hs_email_bounce_status",
        "hs_marketable_status",
        "hs_analytics_num_page_views",
        "hs_merged_object_ids",
        "jobtitle",
        "company",
        "country",
    ],
}


_RULE_HUBSPOT_DEFINED_FALSE = "hubspotDefined_false"
_RULE_CREATED_USER_ID = "createdUserId"
_RULE_NON_DEFAULT_GROUP = "non_default_group"
_PRIORITY_RULES = (_RULE_HUBSPOT_DEFINED_FALSE, _RULE_CREATED_USER_ID)


def matched_custom_rules(prop: dict) -> list:
    """Return the list of documented rule names (possibly empty) that match
    a single property definition dict (as read from
    ``raw/properties_<object>.json``)."""
    rules = []
    if prop.get("hubspotDefined") is False:
        rules.append(_RULE_HUBSPOT_DEFINED_FALSE)
    if prop.get("createdUserId"):
        rules.append(_RULE_CREATED_USER_ID)
    group = prop.get("groupName")
    if (
        group
        and prop.get("hubspotDefined") is not True
        and not str(group).lower().startswith(_DEFAULT_GROUP_PREFIXES)
    ):
        rules.append(_RULE_NON_DEFAULT_GROUP)
    return rules


def is_custom_property(prop: dict) -> bool:
    """Apply the documented custom/mYngle-candidate rule to a single
    property definition dict."""
    return bool(matched_custom_rules(prop))


def resolve_properties(
    snapshot: Snapshot, object_type: str, *, custom_cap: int = DEFAULT_CUSTOM_PROPERTY_CAP
) -> dict:
    """Resolve the property list to request for ``object_type`` against
    what ``snapshot`` actually reports as available. Never assumes a fixed
    property exists -- every name is checked against the snapshot's
    ``properties_<object_type>.json`` before being requested."""
    fixed = REQUESTED_PROPERTIES.get(object_type, [])
    definitions = snapshot.properties(object_type)
    available_names = {p.get("name") for p in definitions if p.get("name")}

    requested_available = [name for name in fixed if name in available_names]
    unavailable = [name for name in fixed if name not in available_names]

    candidate_rules = {}
    for prop in definitions:
        name = prop.get("name")
        if not name or name in fixed:
            continue
        rules = matched_custom_rules(prop)
        if rules:
            candidate_rules[name] = rules

    priority_tier = sorted(
        name for name, rules in candidate_rules.items() if any(r in _PRIORITY_RULES for r in rules)
    )
    secondary_tier = sorted(name for name in candidate_rules if name not in priority_tier)
    ordered_candidates = priority_tier + secondary_tier

    custom_included = ordered_candidates[:custom_cap]
    custom_overflow = ordered_candidates[custom_cap:]

    return {
        "object_type": object_type,
        "properties_requested": requested_available + custom_included,
        "properties_unavailable": unavailable,
        "custom_properties_discovered": len(ordered_candidates),
        "custom_properties_included": custom_included,
        "custom_properties_overflow": custom_overflow,
        "custom_property_cap": custom_cap,
        "custom_properties_rules": {name: candidate_rules[name] for name in custom_included},
    }
