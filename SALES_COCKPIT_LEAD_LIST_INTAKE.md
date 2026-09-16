# Sales Cockpit lead-list intake

## Scope

This is the controlled self-service path for lists uploaded by authorised sales users.
The original XLSX/CSV is preserved unchanged. Processing is company-first while all
source contact rows remain traceable to their company.

Initial rollout stops before automatic enrichment/publication until protection checks
have completed. No browser user receives direct access to enrichment credentials,
HubSpot write credentials or GCS publication credentials.

## User roles

- `owner_admin`: full Control Center plus Lead Lists; can see all uploads and exceptions.
- `list_uploader`: Lead Lists only; can upload and view own lists.

Current intended uploaders are Giulio and Mattia. Caller assignment is selected per list.

## Pipeline

1. Upload XLSX/CSV and immutable metadata.
2. Detect sheet, header and relevant columns.
3. Normalize contact rows and group them into unique companies.
4. Run duplicate and customer-protection checks before paid enrichment.
5. Resolve missing domains only for companies still eligible to proceed.
6. Enrich and score eligible new companies once.
7. Reattach preserved contacts and list/caller provenance.
8. Publish approved companies/list membership to the Sales Cockpit.
9. Sync to HubSpot only after the company identity is confirmed.

Statuses:
`uploaded -> checking -> enriching -> review_required/ready -> published -> hubspot_synced`.
`failed` is terminal for that processing attempt, while the original upload remains intact.

## Protection rules

Protection runs before Serper, Zyte, Firecrawl, AI scoring or other paid enrichment.

- Exact Account Management account match: block from cold-calling intake.
- HubSpot `customer_type` in an active mYngle customer category: block.
- HubSpot `Customer-Lost-NB (Company)`: manual review, never silently reactivate.
- `hs_current_customer=yes` by itself is not a block. In this portal that flag is also
  present on many prospect and lead company records.
- `type=CUSTOMER` or lifecycle `customer` without an explicit active customer type:
  manual review.
- Exact existing Sales Cockpit company: reuse that company and add list membership;
  do not create or re-enrich a duplicate.
- Multiple/ambiguous matches: manual review.
- Only a company with no protected exact match becomes `clear_new`.

Exact domain or VAT is preferred for identity. Normalized company-name matches are a
review signal unless the surrounding evidence makes the identity unambiguous.

## File handling rules

The importer accepts common schema variants rather than requiring one exact template.
Company-specific columns take precedence over contact geography. A field is treated as
an email only when it passes email syntax validation, because historical caller files
can contain call notes in email-like columns.

If a company domain is absent, a non-generic work-email domain may be used as a resolver
hint. It is not accepted as proof of company identity. Conflicting email-domain hints
require resolution.

## Rollout gates

Phase 1: upload and intake normalization, with owner visibility and audit trail.
Phase 2: read-only customer/Cockpit protection checks and exception reporting.
Phase 3: domain resolution plus enrichment for `clear_new` companies.
Phase 4: owner-approved publication and list membership/caller assignment.
Phase 5: controlled HubSpot synchronization. Automatic publication can be enabled only
after the earlier phases have passed real-list acceptance tests.
