"""Read-only HubSpot CRM audit framework for mYngle.

Safety contract (see hubspot_audit/README.md for the full write-up):
- Every HubSpot call this package makes is a GET. Nothing here creates,
  updates, deletes, merges or archives CRM data.
- The package works fully with AI disabled (``AuditConfig.ai.enabled = False``).
- Nothing in this package ever prints or logs a credential value.
"""

__version__ = "0.1.0"
