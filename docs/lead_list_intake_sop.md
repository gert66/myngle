# Sales Cockpit Lead List Intake SOP

## Purpose
Accept caller-owned CSV/XLSX lead lists without allowing malformed source data, duplicate companies, or current customers to flow straight into the Sales Cockpit.

The original upload is immutable. Intake produces normalized derived rows and a company workset. Validation happens before supplier calls, enrichment, HubSpot writes, or Cockpit publication.

## Minimum upload standard
One source row represents one contact-person/company relationship.

Required at upload/list level:
- caller selected explicitly in the upload UI;
- country/sales market supplied in the file or selected as upload metadata;
- XLSX or CSV file.

Required per usable row:
- company name;
- a company identifier: domain/website/VAT, or company name + country;
- a contact identifier/channel: contact name, valid work email, usable phone number, or LinkedIn profile.

A missing company name may be repaired only when a strong company identifier such as domain/VAT is present. Rows without a resolvable company identity are quarantined.

## Source fields we do not trust
- Caller/owner/user columns never determine Sales Cockpit ownership. The upload selection is authoritative.
- Free text in email, phone or domain columns is never coerced into that data type.
- Work-email domains are resolver hints, not automatic proof of company identity.

Historical calling data must be preserved. If an old export contains notes in an email-shaped field, the value is copied to `legacy_call_notes` and excluded from the normalized email field.

## Quality gate
### GREEN / READY
The list satisfies the minimum standard and has no structural contamination that requires review. Missing enrichment data, such as company domain, does not by itself make a clean list AMBER.

GREEN is the only state that may automatically continue to customer-protection checks and enrichment.

### AMBER / AUTO_REPAIR
The list is structurally usable but contains small deterministic repairs, for example:
- a few missing company names with a strong domain/VAT identifier;
- exact duplicate contact rows;
- isolated malformed values that can be quarantined safely.

### AMBER / REVIEW_REQUIRED
Human review is required before supplier calls or publication. Examples:
- repeated call notes/free text stored in email-shaped columns;
- a small number of rows fail minimum requirements;
- ambiguous source semantics where automatic interpretation could lose history.

### RED / REJECTED
Automatic processing stops. Examples:
- caller was not selected;
- no company-name column exists;
- no country/market is available;
- more than 5% of rows, or at least 10 rows, fail minimum identity/market requirements;
- file contains no usable lead rows.

## Required processing order
1. Preserve original file, filename, file hash, import timestamp and source metadata.
2. Detect workbook sheet/header or CSV delimiter/encoding.
3. Map and normalize source fields without deleting raw information.
4. Run the quality gate.
5. Stop on RED. Hold REVIEW_REQUIRED. Apply deterministic AUTO_REPAIR rules where safe.
6. Split contacts from companies and collapse companies by domain first, VAT second, normalized company name + country third.
7. Run current-customer/account-management suppression before enrichment or publication.
8. Run existing Sales Cockpit duplicate/caller-collision checks.
9. Resolve missing domains using existing cache/work-email hints first, then approved discovery sources.
10. Enrich only eligible companies through the existing enrichment stack.
11. Reattach all preserved contacts and historical call information.
12. Publish approved companies as a Prospect List for the explicitly selected caller.
13. Sync to HubSpot only when that stage is explicitly enabled.

Steps 7 and 8 are mandatory and fail closed.

## Enrichment cost rule
Validation and customer suppression happen before Serper, Crawl4AI, Firecrawl, Gemini, or Lusha use. Existing/cached company data is reused first. Lusha is used only for missing person-level contact data when needed.

## Acceptance fixtures received 2026-09-16
### Luana - Switzerland CSV
- 375 contact rows.
- 329 company names.
- 342 valid work emails.
- 297 companies have one consistent non-generic work-email domain hint.
- 4 companies have conflicting work-email domains and must not auto-select one.
- 28 companies have no usable work-email domain hint.
- Calling history fields are explicit (`Called`, `Reached`, `I/NI`, `Notes`, `Email`, `Called 2`, `Reached 2`) and must be preserved.
- Expected structural result with caller=Luana and market=Switzerland: `GREEN / READY`.

### Elisabetta - current XLSX
- 398 contact rows on `Sheet 1_Export_Contacts_2026-01`.
- 398 populated company-domain values, 383 unique normalized domains.
- 380 nonblank unique company names; 3 rows have a blank company name but a usable domain.
- `User` is `Pietro Rivalta` throughout and must be ignored for ownership. Caller is assigned explicitly to Elisabetta.
- `Company Country`/`Company City` take precedence over contact Country/City.
- Multiple email-shaped Lusha columns contain call notes/free text. These values must be preserved as legacy notes and never accepted as email addresses.
- Expected structural result: `AMBER / REVIEW_REQUIRED`, not automatic enrichment.

## Recommended user-facing preview
Show: row count, unique companies, assigned caller, quality status, blocked/review rows, domain-resolution count, duplicate-contact count, customer-suppression status, and concise issue examples. Do not start enrichment until the quality gate allows it.
