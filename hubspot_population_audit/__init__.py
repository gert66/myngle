"""Read-only HubSpot population audit: reconciliation, cohorting, and
evidence-based classification of a HubSpot portal's company/contact
population, built on top of an immutable extraction snapshot.

Nothing in this package ever performs a HubSpot write. See
``hubspot_client.py`` for the enforced read-only guard.
"""
