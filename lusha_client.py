"""
lusha_client.py
---------------
Thin HTTP client for the Lusha V3 API.
Called only on explicit single-company lookup requests — never in batch.

Security:
- API key is read exclusively from the LUSHA_API_KEY environment variable.
- The key is never printed or logged.
- Raw Lusha responses are never returned to callers; only normalised dicts.
"""

import os
import re
import urllib.parse
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Endpoint constants — adjust here if Lusha path differs by account/version
# ---------------------------------------------------------------------------

_BASE_URL                  = "https://api.lusha.com"
_ENDPOINT_COMPANY_SEARCH   = "/v3/companies/search"
_ENDPOINT_DECISION_MAKERS  = "/v3/contacts/decision-makers"
_ENDPOINT_CONTACTS_ENRICH  = "/v3/contacts/enrich"
_REQUEST_TIMEOUT           = 15  # seconds

# Shared across every Lusha POST that can 4xx — never leak raw Lusha error
# bodies to the frontend, just a safe generic message per status code.
_SAFE_HTTP_MESSAGES = {
    401: "API key missing or invalid.",
    402: "Insufficient Lusha credits.",
    403: "Account inactive or access forbidden.",
    429: "Rate limit exceeded — please retry later.",
    451: "Legal/GDPR restriction on this record.",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("LUSHA_API_KEY", "")
    if not key:
        raise RuntimeError(
            "LUSHA_API_KEY environment variable is not set. "
            "Export it before running live lookups."
        )
    return key


def _headers(key: str) -> dict:
    return {
        "api_key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _normalize_domain(raw: str) -> str:
    """https://www.example.com/path  →  example.com"""
    raw = raw.strip()
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = raw.split("/")[0].split("?")[0]
    if raw.lower().startswith("www."):
        raw = raw[4:]
    return raw.lower()


def _post(endpoint: str, payload: dict, key: str) -> dict:
    url  = _BASE_URL + endpoint
    resp = requests.post(
        url,
        headers=_headers(key),
        json=payload,
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Contact normalisation
# ---------------------------------------------------------------------------

def _extract_name(contact: dict) -> str:
    first = contact.get("firstName") or contact.get("first_name") or ""
    last  = contact.get("lastName")  or contact.get("last_name")  or ""
    full  = contact.get("fullName")  or contact.get("full_name")  or contact.get("name") or ""
    if full:
        return full.strip()
    return f"{first} {last}".strip()


def _extract_email(contact: dict) -> str:
    """Return first available email string; empty string if none."""
    for key in ("emails", "emailAddresses"):
        val = contact.get(key)
        if isinstance(val, list) and val:
            item = val[0]
            if isinstance(item, dict):
                return item.get("email") or item.get("value") or ""
            if isinstance(item, str):
                return item
    return contact.get("email") or contact.get("workEmail") or ""


def _extract_phone(contact: dict) -> str:
    """Return first available phone string; empty string if none."""
    for key in ("phones", "phoneNumbers"):
        val = contact.get(key)
        if isinstance(val, list) and val:
            item = val[0]
            if isinstance(item, dict):
                return item.get("number") or item.get("phone") or item.get("value") or ""
            if isinstance(item, str):
                return item
    return contact.get("phone") or contact.get("mobilePhone") or ""


def _extract_linkedin(contact: dict) -> str:
    for key in ("linkedinUrl", "linkedin_url", "linkedIn", "linkedin"):
        val = contact.get(key)
        if val:
            return str(val)
    return ""


def _extract_job_title(raw: dict) -> str:
    """
    Extract job title string from a raw Lusha contact dict.
    Handles both simple string fields and nested jobTitle objects:
      { "title": "...", "departments": [...], "seniority": "..." }
    """
    # Nested object shape (Lusha V3 live responses)
    for key in ("jobTitle", "title"):
        val = raw.get(key)
        if isinstance(val, dict):
            return str(val.get("title") or val.get("name") or "").strip()
        if isinstance(val, str) and val:
            return val.strip()
    return str(raw.get("job_title") or "").strip()


def _extract_job_departments(raw: dict) -> str:
    """
    Extract department string from a raw Lusha contact dict.
    Handles nested jobTitle["departments"] list as well as plain fields.
    """
    # Nested shape: jobTitle.departments is a list
    for key in ("jobTitle", "title"):
        val = raw.get(key)
        if isinstance(val, dict):
            depts = val.get("departments")
            if isinstance(depts, list) and depts:
                return str(depts[0]).strip()
    # Plain department fields
    for key in ("department", "dept", "function", "jobFunction"):
        val = raw.get(key)
        if val and not isinstance(val, dict):
            return str(val).strip()
    return ""


def _extract_seniority(raw: dict) -> str:
    """
    Extract seniority from a raw Lusha contact dict.
    Handles nested jobTitle["seniority"] and plain seniority fields.
    """
    # Nested shape: jobTitle.seniority
    for key in ("jobTitle", "title"):
        val = raw.get(key)
        if isinstance(val, dict):
            sen = val.get("seniority")
            if sen:
                return str(sen).strip()
    # Plain seniority fields
    for key in ("seniority", "seniorityLevel", "level"):
        val = raw.get(key)
        if val and not isinstance(val, dict):
            return str(val).strip()
    return ""


def _extract_department(contact: dict) -> str:
    """Alias kept for backward compatibility — delegates to _extract_job_departments."""
    return _extract_job_departments(contact)


def _extract_reveal_availability(raw: dict) -> dict:
    """Whether email/phone can be revealed for this contact at all, from the
    `canReveal` list POST /v3/contacts/decision-makers returns alongside
    each free preview (confirmed live shape: ``[{"field": "emails",
    "credits": 1}, {"field": "phones", "credits": 5}]`` — phone reveal
    costs 5x an email reveal). A contact with no phone on file simply has
    no "phones" entry in canReveal, so this lets callers only offer the
    "reveal phone" action when Lusha actually has one, instead of spending
    a call to find out."""
    can_reveal = raw.get("canReveal") or []
    fields = {
        str(cr.get("field", "")).lower()
        for cr in can_reveal if isinstance(cr, dict)
    }
    return {
        "emailAvailable": "emails" in fields or "email" in fields,
        "phoneAvailable": "phones" in fields or "phone" in fields,
    }


def _normalise_contact(raw: dict) -> dict:
    """Map a raw Lusha contact dict to the mYngle normalised schema."""
    contact_id = raw.get("id") or raw.get("contactId") or ""
    return {
        "name":        _extract_name(raw),
        "jobTitle":    _extract_job_title(raw),
        "department":  _extract_job_departments(raw),
        "seniority":   _extract_seniority(raw),
        "email":       _extract_email(raw),
        "phone":       _extract_phone(raw),
        "linkedinUrl": _extract_linkedin(raw),
        "matchReason": "",   # filled by lusha_ranker
        "confidence":  0.0,  # filled by lusha_ranker
        "contactId":   contact_id,  # public — needed by callers to request reveal_contact_details later
        **_extract_reveal_availability(raw),
        "_lushaId":    contact_id,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_company_by_domain(domain: str) -> dict:
    """
    Search Lusha for a company by domain.
    Returns the first matching company dict (normalised), or {}.
    POST /v3/companies/search
    """
    key    = _api_key()
    domain = _normalize_domain(domain)
    payload = {
        "companies": [{"clientReferenceId": "myngle-lookup-1", "domain": domain}],
        "options":   {"includePartialProfiles": True},
    }
    try:
        data    = _post(_ENDPOINT_COMPANY_SEARCH, payload, key)
        results = data.get("results") or []
        if results:
            first = results[0]
            company_data = first.get("data") or first
            return {
                "lushaId":   company_data.get("id") or "",
                "name":      company_data.get("name") or "",
                "domain":    domain,
                "employees": company_data.get("employeeCount") or company_data.get("employees") or "",
                "industry":  company_data.get("industry") or "",
            }
        return {}
    except requests.HTTPError as exc:
        raise RuntimeError(f"Lusha company search failed: {exc.response.status_code}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Lusha request error: {exc}") from exc


def find_contacts(
    company_name: str,
    domain: str,
    country: Optional[str] = None,
) -> list[dict]:
    """
    Retrieve decision-maker contacts for a company.
    Uses POST /v3/contacts/decision-makers.
    Returns a list of normalised contact dicts (schema matches lusha_ranker input).

    This is a single-company lookup — never call in a loop without user intent.
    """
    key    = _api_key()
    domain = _normalize_domain(domain) if domain else ""

    # Build the minimal company identifier for the API
    # [LUSHA SCHEMA] companies array with exactly one identifier: domain or id
    company_entry: dict = {"clientReferenceId": "myngle-lookup-1"}
    if domain:
        company_entry["domain"] = domain
    elif company_name:
        # Fallback: name-only; support depends on account plan
        company_entry["name"] = company_name

    payload = {"companies": [company_entry]}

    try:
        data    = _post(_ENDPOINT_DECISION_MAKERS, payload, key)
        results = data.get("results") or []
        raw_contacts: list[dict] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            dm_list = result.get("decisionMakers") or []
            for dm in dm_list:
                if isinstance(dm, dict):
                    raw_contacts.append(_normalise_contact(dm))

        # Deduplicate: prefer _lushaId; fall back to (name, jobTitle) key
        seen: set[str] = set()
        contacts: list[dict] = []
        for c in raw_contacts:
            dedup_key = c.get("_lushaId") or (
                f"{c.get('name','').lower().strip()}|"
                f"{c.get('jobTitle','').lower().strip()}"
            )
            if dedup_key and dedup_key not in seen:
                seen.add(dedup_key)
                contacts.append(c)

        return contacts
    except requests.HTTPError as exc:
        code = exc.response.status_code
        raise RuntimeError(
            _SAFE_HTTP_MESSAGES.get(code, f"Lusha API error ({code}).")
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Lusha request failed: {exc}") from exc


def reveal_contact_details(contact_ids: list[str]) -> dict[str, dict]:
    """
    Reveal email + phone for specific contacts already found via
    ``find_contacts`` (pass their ``contactId``). Explicit, single-purpose
    call — never triggered automatically by a search, only when the caller
    (a user clicking "reveal" on one specific contact) asks for it.

    POST /v3/contacts/enrich, body ``{"ids": contact_ids}``. Confirmed live
    (2026-07-14): no separate revealEmails/revealPhones flag is needed —
    passing ``ids`` alone returns both ``emails`` and ``phones`` in the
    same list-of-dicts shape ``_extract_email``/``_extract_phone`` already
    parse. Billed per Lusha's own canReveal pricing (confirmed: 1 credit
    per email, 5 credits per phone) regardless of whether the caller only
    wanted one of the two -- there is no way to request just the phone.

    Lusha caps this endpoint at 100 ids per call; more than that raises
    before any request is made, so a caller can't accidentally fan out an
    unbounded reveal (and unbounded credit spend) in one call.

    Returns ``{contact_id: {"email": str, "phone": str}}`` for whichever
    ids Lusha returned data for; ids with nothing revealable are simply
    absent from the result rather than present with empty strings.
    """
    if not contact_ids:
        return {}
    if len(contact_ids) > 100:
        raise ValueError(
            f"reveal_contact_details called with {len(contact_ids)} ids; "
            "Lusha's own cap for this endpoint is 100 per call."
        )
    key = _api_key()
    payload = {"ids": list(contact_ids)}
    try:
        data = _post(_ENDPOINT_CONTACTS_ENRICH, payload, key)
        results = data.get("results") or []
        revealed: dict[str, dict] = {}
        for raw in results:
            if not isinstance(raw, dict):
                continue
            cid = raw.get("id") or raw.get("contactId") or ""
            if not cid:
                continue
            revealed[cid] = {
                "email": _extract_email(raw),
                "phone": _extract_phone(raw),
            }
        return revealed
    except requests.HTTPError as exc:
        code = exc.response.status_code
        raise RuntimeError(
            _SAFE_HTTP_MESSAGES.get(code, f"Lusha API error ({code}).")
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Lusha request failed: {exc}") from exc
