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


def _normalise_contact(raw: dict) -> dict:
    """Map a raw Lusha contact dict to the mYngle normalised schema."""
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
        "_lushaId":    raw.get("id") or raw.get("contactId") or "",
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
        _SAFE_MESSAGES = {
            401: "API key missing or invalid.",
            402: "Insufficient Lusha credits.",
            403: "Account inactive or access forbidden.",
            429: "Rate limit exceeded — please retry later.",
            451: "Legal/GDPR restriction on this record.",
        }
        raise RuntimeError(
            _SAFE_MESSAGES.get(code, f"Lusha API error ({code}).")
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Lusha request failed: {exc}") from exc
