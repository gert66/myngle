# Sales Cockpit Lead List Intake SOP

## Goal
Turn an uploaded caller list into a safe company-first workset before any company can appear for cold calling.

The original CSV/XLSX remains immutable. Intake creates derived company records only. Enrichment, Cockpit publication and HubSpot writes are separate later stages.

## Required order
1. Preserve the original upload and immutable metadata.
2. Detect file type, sheet, delimiter, encoding and header row.
3. Normalize source rows and identify contact fields where they are explicit.
4. Collapse rows to unique companies using domain first, VAT second, normalized company name + country third.
5. Run customer/account-management protection checks.
6. Run existing Sales Cockpit duplicate checks.
7. Resolve missing company domains, using a consistent work-email domain only as a hint.
8. Enrich and score only eligible companies.
9. Reattach preserved contact rows to their company.
10. Publish approved companies to the selected caller's Cockpit list.
11. Sync to HubSpot only when explicitly enabled for that stage.

No stage may skip steps 5 or 6.

## Acceptance fixtures received 2026-09-16

### Luana - Switzerland CSV
- File format: semicolon-delimited CSV, Windows-1252 compatible.
- Source rows: 375 contact rows.
- Unique companies after deterministic company-name + Switzerland normalization: 329.
- Duplicate company rows collapsed at intake: 46.
- The file has no company-domain column.
- 297 companies have one consistent non-generic work-email domain that can seed domain resolution.
- 4 companies have conflicting work-email domains and must not auto-select one.
- 28 companies have no usable work-email domain hint.
- All 375 source rows have a LinkedIn profile field populated.
- Expected first post-intake list status: `checking`, not `ready`.

### Elisabetta - current XLSX
- Workbook size: 398 contact rows on the populated export sheet plus empty helper sheets.
- The intake loader must select `Sheet 1_Export_Contacts_2026-01` automatically.
- Unique company domains: 383.
- Duplicate company rows collapsed at intake: 15.
- All 398 rows contain a company domain.
- 3 rows have a blank company name but a usable company domain. These remain valid domain-identified companies.
- Company Country and Company City take precedence over the contact Country and City columns.
- The `Direct Email` column contains call notes rather than actual email addresses in this file, so free text must not be treated as an email.
- Expected first post-intake list status: `checking` unless another validation issue is found.
