"""
lusha_api_tester.py

Standalone Streamlit diagnostics app for the Lusha V3 API.
Purpose: inspect what the API key can return — company data, decision makers,
and optionally revealed contact details. NOT part of the lead prioritizer.

Usage:
    streamlit run lusha_api_tester.py

Secrets (.streamlit/secrets.toml):
    LUSHA_API_KEY = "your_key_here"

Lusha V3 base URL: https://api.lusha.com
Auth header: api_key: <key>

Endpoints used:
  GET  /v3/account/usage                  — account/key health check
  POST /v3/companies/search               — search company by name/domain
  POST /v3/companies/search-and-enrich    — search + enrich (may consume credits)
  POST /v3/companies/enrich               — enrich company by Lusha ID
  POST /v3/contacts/decision-makers       — free contact previews for a company
  POST /v3/contacts/enrich                — reveal email/phone by contact ID (costs credits)

NOTE: Payload shapes are based on the Lusha V3 OpenAPI schema (June 2026).
      Lines marked [LUSHA SCHEMA] should be re-verified if the schema changes.
"""

import json
import re
import urllib.parse

import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LUSHA_BASE_URL = "https://api.lusha.com"

RATE_LIMIT_HEADERS = [
    "x-rate-limit-daily",
    "x-daily-requests-left",
    "x-daily-usage",
    "x-rate-limit-hourly",
    "x-hourly-requests-left",
    "x-hourly-usage",
    "x-rate-limit-minute",
    "x-minute-requests-left",
    "x-minute-usage",
]

ERROR_MESSAGES = {
    401: "401 — API key missing or invalid.",
    402: "402 — Insufficient credits / payment required.",
    403: "403 — Account inactive or forbidden.",
    404: "404 — Endpoint bestaat niet. Controleer of de app het juiste Lusha V3 endpoint gebruikt.",
    429: "429 — Rate limit exceeded. Probeer later opnieuw.",
    451: "451 — Juridische of AVG/GDPR-beperking op dit record.",
}

# Reveal fields available for /v3/companies/enrich
COMPANY_REVEAL_FIELDS = [
    "employeesByLocation",
    "employeesByDepartment",
    "employeesBySeniority",
    "competitors",
    "intent",
]

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def normalize_domain(raw: str) -> str:
    """
    Convert any URL or domain string to a bare domain for API calls.
      https://www.example.com/path  ->  example.com
      www.example.com               ->  example.com
      example.com                   ->  example.com
    """
    raw = raw.strip()
    if not raw:
        return ""
    if not re.match(r"https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host.lower()


def lusha_request(
    method: str,
    endpoint: str,
    api_key: str,
    payload: dict | None = None,
    params: dict | None = None,
) -> tuple[int, dict, dict]:
    """
    Execute a Lusha API request.

    Returns:
        (status_code, response_json, response_headers)
        On network/decode errors status_code is 0 and response_json contains 'error'.
    """
    url = f"{LUSHA_BASE_URL}{endpoint}"
    headers = {
        "api_key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        resp = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=payload,
            params=params,
            timeout=15,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"error": "Kon JSON-response niet decoderen.", "raw": resp.text[:500]}
        return resp.status_code, data, dict(resp.headers)
    except requests.exceptions.Timeout:
        return 0, {"error": "Request timed out (>15 s)."}, {}
    except requests.exceptions.ConnectionError as exc:
        return 0, {"error": f"Verbindingsfout: {exc}"}, {}


def flatten_dict(d: dict, parent_key: str = "", sep: str = ".") -> dict:
    """Recursively flatten a nested dict to single-level with dotted keys."""
    items: list = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep).items())
        elif isinstance(v, list):
            items.append((new_key, json.dumps(v, ensure_ascii=False)))
        else:
            items.append((new_key, v))
    return dict(items)


def extract_rate_limit_headers(headers: dict) -> dict:
    """Return only Lusha rate-limit headers (case-insensitive)."""
    lower = {k.lower(): v for k, v in headers.items()}
    return {h: lower[h] for h in RATE_LIMIT_HEADERS if h in lower}


def response_to_dataframe(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    return pd.DataFrame([flatten_dict(r) for r in records])


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def json_bytes(data: dict | list) -> bytes:
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# UI output helpers
# ---------------------------------------------------------------------------


def show_rate_limits(headers: dict) -> None:
    rl = extract_rate_limit_headers(headers)
    if rl:
        with st.expander("Rate-limit headers", expanded=False):
            st.table(pd.DataFrame(rl.items(), columns=["Header", "Waarde"]))


def show_status(status_code: int, data: dict) -> None:
    if status_code == 0:
        st.error(data.get("error", "Onbekende fout."))
        return
    if status_code >= 500:
        st.error(f"{status_code} — Server-fout bij Lusha. Probeer later opnieuw.")
        return
    api_msg = ""
    if isinstance(data, dict):
        api_msg = data.get("message") or data.get("error") or ""
    if status_code in ERROR_MESSAGES:
        st.error(f"{ERROR_MESSAGES[status_code]}{(' — ' + str(api_msg)) if api_msg else ''}")
    elif status_code in (200, 201):
        st.success(f"HTTP {status_code} — Verzoek geslaagd.")
    else:
        st.warning(f"HTTP {status_code}{(' — ' + str(api_msg)) if api_msg else ''}")


def section_raw_output(status_code: int, data: dict | list, headers: dict) -> None:
    show_status(status_code, data if isinstance(data, dict) else {})
    show_rate_limits(headers)
    with st.expander("Raw JSON response", expanded=True):
        st.json(data)
    st.download_button(
        "⬇ Download raw JSON",
        data=json_bytes(data),
        file_name="lusha_response.json",
        mime="application/json",
        key=f"dl_json_{id(data)}",
    )


def section_company_table(data: dict) -> None:
    """
    Extract company records from a Lusha V3 response and display as table.
    Lusha V3 returns records under 'results'; older keys kept as fallback.
    Each result object may have a 'data' sub-key with the actual company dict.
    """
    companies: list[dict] = []

    # Primary: V3 results array — each item may wrap company data in 'data'
    results = data.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                company_data = item.get("data") or item
                if isinstance(company_data, dict):
                    companies.append(company_data)

    # Fallbacks for other response shapes
    if not companies:
        for key in ("company", "companies", "data", "result"):
            val = data.get(key)
            if isinstance(val, dict):
                companies = [val]
                break
            if isinstance(val, list) and val and isinstance(val[0], dict):
                companies = val
                break

    if not companies:
        st.info("Geen bedrijfsdata in response gevonden (keys: " + ", ".join(data.keys()) + ").")
        return

    df = response_to_dataframe(companies)
    st.subheader("Bedrijfsdata")
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "⬇ Download bedrijven CSV",
        data=csv_bytes(df),
        file_name="lusha_companies.csv",
        mime="text/csv",
        key=f"dl_comp_{id(df)}",
    )


def section_contact_table(data: dict) -> None:
    """
    Extract contact/decision-maker records from a Lusha V3 response.
    Lusha V3 returns contacts under 'results'; per-result errors also surfaced.
    """
    contacts: list[dict] = []
    errors_per_result: list[str] = []

    results = data.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                if item.get("error"):
                    errors_per_result.append(str(item["error"]))
                contact_data = item.get("data") or item
                if isinstance(contact_data, dict) and contact_data:
                    contacts.append(contact_data)

    # Fallbacks
    if not contacts:
        for key in ("contacts", "decisionMakers", "decision_makers", "data", "result"):
            val = data.get(key)
            if isinstance(val, list) and val:
                contacts = val
                break
            if isinstance(val, dict) and val:
                contacts = [val]
                break

    if errors_per_result:
        st.warning("Fouten in resultaten: " + " | ".join(errors_per_result))

    if not contacts:
        st.info("Geen contactdata in response gevonden.")
        return

    revealed: list[dict] = []
    not_revealed_fields: list[str] = []
    for c in contacts:
        flat = flatten_dict(c)
        revealed.append(flat)
        # Surface null fields that indicate available-but-not-revealed data
        for field in ("emails", "phones", "email", "phone", "phoneNumbers", "emailAddresses"):
            if field in c and c[field] is None:
                not_revealed_fields.append(field)

    df = pd.DataFrame(revealed)
    st.subheader("Contacten / Decision Makers")
    st.dataframe(df, use_container_width=True)

    if not_revealed_fields:
        st.warning(
            "Velden beschikbaar maar niet onthuld (vereisen reveal + credits): "
            + ", ".join(sorted(set(not_revealed_fields)))
        )

    st.download_button(
        "⬇ Download contacten CSV",
        data=csv_bytes(df),
        file_name="lusha_contacts.csv",
        mime="text/csv",
        key=f"dl_cont_{id(df)}",
    )


# ---------------------------------------------------------------------------
# API call wrappers — all endpoints verified against Lusha V3 OpenAPI (June 2026)
# ---------------------------------------------------------------------------


def call_account_status(api_key: str):
    """GET /v3/account/usage — account health / key validation, no credits."""
    return lusha_request("GET", "/v3/account/usage", api_key)


def call_search_company(api_key: str, name: str, domain: str):
    """
    POST /v3/companies/search
    Search only — no enrichment, no credits consumed.
    [LUSHA SCHEMA] companies array with name/domain; country not in V3 schema for this endpoint.
    """
    company: dict = {"clientReferenceId": "manual-test-1"}
    if name:
        company["name"] = name
    if domain:
        company["domain"] = domain  # [LUSHA SCHEMA] field is 'domain', not 'website'
    payload = {
        "companies": [company],
        "options": {"includePartialProfiles": True},  # [LUSHA SCHEMA]
    }
    return lusha_request("POST", "/v3/companies/search", api_key, payload=payload)


def call_search_enrich_company(api_key: str, name: str, domain: str):
    """
    POST /v3/companies/search-and-enrich
    Combines search + enrich in one call. May consume credits.
    [LUSHA SCHEMA] no separate 'reveal' block; enrichment is implicit.
    """
    company: dict = {"clientReferenceId": "manual-test-1"}
    if name:
        company["name"] = name
    if domain:
        company["domain"] = domain  # [LUSHA SCHEMA]
    payload = {
        "companies": [company],
        "options": {"includePartialProfiles": True},  # [LUSHA SCHEMA]
    }
    return lusha_request("POST", "/v3/companies/search-and-enrich", api_key, payload=payload)


def call_enrich_companies_by_id(api_key: str, company_ids: list[str], reveal_fields: list[str]):
    """
    POST /v3/companies/enrich
    Enrich known company IDs; reveal_fields controls optional paid data.
    [LUSHA SCHEMA] 'ids' array + 'reveal' list of field names.
    """
    payload: dict = {"ids": company_ids}
    if reveal_fields:
        payload["reveal"] = reveal_fields  # [LUSHA SCHEMA]
    return lusha_request("POST", "/v3/companies/enrich", api_key, payload=payload)


def call_decision_makers(api_key: str, domain: str, company_lusha_id: str, max_contacts: int):
    """
    POST /v3/contacts/decision-makers
    Returns free contact previews (no email/phone reveal, no credits).
    Identify the company by domain OR Lusha company ID.
    [LUSHA SCHEMA] supply either 'domain' or 'companyId' (not both required).
    """
    company: dict = {}
    if company_lusha_id:
        company["id"] = company_lusha_id  # [LUSHA SCHEMA]
    elif domain:
        company["domain"] = domain  # [LUSHA SCHEMA]
    payload = {
        "company": company,
        "contacts": {"limit": max_contacts},  # [LUSHA SCHEMA] - field name may differ
    }
    return lusha_request("POST", "/v3/contacts/decision-makers", api_key, payload=payload)


def call_enrich_contacts(api_key: str, contact_ids: list[str], reveal_email: bool, reveal_phone: bool):
    """
    POST /v3/contacts/enrich
    Reveal emails and/or phones for known contact IDs. Costs credits.
    [LUSHA SCHEMA] 'ids' array + 'reveal' list: 'emails' | 'phones'.
    """
    reveal: list[str] = []
    if reveal_email:
        reveal.append("emails")   # [LUSHA SCHEMA]
    if reveal_phone:
        reveal.append("phones")   # [LUSHA SCHEMA]
    payload: dict = {"ids": contact_ids}
    if reveal:
        payload["reveal"] = reveal
    return lusha_request("POST", "/v3/contacts/enrich", api_key, payload=payload)


# ---------------------------------------------------------------------------
# Main Streamlit app
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Lusha API Tester", page_icon="🔍", layout="wide")
    st.title("🔍 Lusha API Tester (diagnostisch)")
    st.caption(
        "Standalone diagnostics-app — geen onderdeel van de lead prioritizer. "
        "Gebruik uitsluitend voor handmatige API-tests."
    )

    # ── API key ──────────────────────────────────────────────────────────────
    api_key = ""
    try:
        api_key = st.secrets["LUSHA_API_KEY"]
        st.sidebar.success("API-sleutel geladen vanuit Streamlit secrets.")
    except (KeyError, FileNotFoundError):
        st.sidebar.warning("Geen API-sleutel in secrets gevonden. Voer handmatig in.")
        api_key = st.sidebar.text_input(
            "Lusha API-sleutel",
            type="password",
            placeholder="Plak hier je API-sleutel",
        )

    if api_key:
        masked = api_key[:4] + "****" + api_key[-4:] if len(api_key) > 8 else "****"
        st.sidebar.info(f"Actieve sleutel: `{masked}`")
    else:
        st.sidebar.error("Geen API-sleutel beschikbaar — app is niet functioneel.")

    st.sidebar.markdown("---")

    # ── Input fields ─────────────────────────────────────────────────────────
    st.sidebar.header("Zoekparameters")
    company_name = st.sidebar.text_input("Bedrijfsnaam", placeholder="bijv. Acme Corp")
    company_url_raw = st.sidebar.text_input(
        "Bedrijfs-URL of domein", placeholder="bijv. https://www.acme.com"
    )
    st.sidebar.caption(
        "Land wordt niet ondersteund door de V3 company search/enrich endpoints "
        "en is daarom niet opgenomen in de payload."
    )
    max_contacts = st.sidebar.number_input(
        "Max. decision makers / contacten", min_value=1, max_value=25, value=5
    )
    company_lusha_id = st.sidebar.text_input(
        "Lusha company ID (optioneel, voor decision makers)",
        placeholder="bijv. 16303253",
    )

    domain = normalize_domain(company_url_raw) if company_url_raw else ""
    if domain:
        st.sidebar.caption(f"Genormaliseerd domein: `{domain}`")

    # ── Reveal checkboxes ────────────────────────────────────────────────────
    st.sidebar.markdown("---")
    st.sidebar.header("Reveal-instellingen (credits!)")
    st.sidebar.warning(
        "⚠️ Reveal-opties kunnen Lusha-credits verbruiken. "
        "Zet alleen aan als je dit bewust wilt."
    )
    reveal_email = st.sidebar.checkbox("Reveal e-mailadressen (contact enrich)", value=False)
    reveal_phone = st.sidebar.checkbox("Reveal telefoonnummers (contact enrich)", value=False)

    # ── Action buttons ────────────────────────────────────────────────────────
    st.markdown("### Acties")

    if not api_key:
        st.warning("Voer eerst een API-sleutel in via de sidebar of secrets.toml.")
        return

    col1, col2, col3, col4 = st.columns(4)
    run_account = col1.button("🔑 Test sleutel / account")
    run_search  = col2.button("🔎 Zoek bedrijf")
    run_enrich  = col3.button("🏢 Zoek + verrijk bedrijf")
    run_dm      = col4.button("👥 Decision makers")

    # ── Company enrich by ID (follow-up after search) ─────────────────────────
    st.markdown("---")
    with st.expander("Company-ID's verrijken (optioneel follow-up na zoekresultaat)", expanded=False):
        st.info(
            "Kopieer Lusha company-ID's uit de zoekresultaten hierboven. "
            "Dit endpoint verrijkt met extra bedrijfsdata."
        )
        company_ids_raw = st.text_area(
            "Company-ID's (één per regel)",
            placeholder="16303253\n12790225",
            height=80,
        )
        st.markdown("**Optionele reveal-velden (standaard UIT — verbruiken mogelijk credits):**")
        reveal_choices = {f: st.checkbox(f, value=False, key=f"rev_{f}") for f in COMPANY_REVEAL_FIELDS}
        selected_reveal = [f for f, checked in reveal_choices.items() if checked]
        if selected_reveal:
            st.warning(f"⚠️ Reveal-velden geselecteerd: {', '.join(selected_reveal)} — dit kan credits kosten.")
        run_enrich_companies = st.button("Verrijk geselecteerde bedrijven")

    # ── Contact enrich by ID (follow-up after decision makers) ───────────────
    with st.expander("Contact-ID's verrijken (optioneel follow-up na decision makers)", expanded=False):
        st.info(
            "Kopieer contact-ID's uit de decision-makers resultaten. "
            "Zet Reveal e-mail/telefoon AAN in de sidebar om die velden te onthullen."
        )
        contact_ids_raw = st.text_area(
            "Contact-ID's (één per regel)",
            placeholder="4389064654\n4389064624",
            height=80,
        )
        if not reveal_email and not reveal_phone:
            st.warning("Geen reveal-velden geselecteerd in sidebar — enrich stuurt geen reveal-lijst mee.")
        else:
            st.warning(
                f"⚠️ Reveal staat aan: "
                f"{'e-mail ' if reveal_email else ''}"
                f"{'telefoon' if reveal_phone else ''}"
                f" — dit verbruikt credits per contact."
            )
        run_enrich_contacts = st.button("Verrijk geselecteerde contacten")

    st.markdown("---")

    # ── Execute: account status ───────────────────────────────────────────────
    if run_account:
        st.subheader("Sleutel / account status  —  GET /v3/account/usage")
        with st.spinner("Bezig…"):
            status, data, headers = call_account_status(api_key)
        section_raw_output(status, data, headers)

    # ── Execute: search company ───────────────────────────────────────────────
    if run_search:
        if not company_name and not domain:
            st.error("Vul minimaal een bedrijfsnaam of domein in.")
        else:
            st.subheader("Bedrijf zoeken  —  POST /v3/companies/search")
            with st.spinner("Bezig…"):
                status, data, headers = call_search_company(api_key, company_name, domain)
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_company_table(data)

    # ── Execute: search + enrich company ─────────────────────────────────────
    if run_enrich:
        if not company_name and not domain:
            st.error("Vul minimaal een bedrijfsnaam of domein in.")
        else:
            st.warning(
                "⚠️ Search-and-enrich verrijkt automatisch bedrijfsdata "
                "en kan Lusha-credits verbruiken."
            )
            st.subheader("Bedrijf zoeken + verrijken  —  POST /v3/companies/search-and-enrich")
            with st.spinner("Bezig…"):
                status, data, headers = call_search_enrich_company(api_key, company_name, domain)
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_company_table(data)

    # ── Execute: decision makers ──────────────────────────────────────────────
    if run_dm:
        if not domain and not company_lusha_id:
            st.error("Vul een domein of Lusha company ID in voor decision makers.")
        else:
            st.subheader("Decision Makers  —  POST /v3/contacts/decision-makers")
            st.info("Dit endpoint retourneert gratis contact-previews (geen reveal van e-mail/telefoon).")
            with st.spinner("Bezig…"):
                status, data, headers = call_decision_makers(
                    api_key, domain, company_lusha_id, int(max_contacts)
                )
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_contact_table(data)

    # ── Execute: company enrich by ID ─────────────────────────────────────────
    if run_enrich_companies:
        ids = [line.strip() for line in company_ids_raw.splitlines() if line.strip()]
        if not ids:
            st.error("Voer minimaal één company-ID in.")
        else:
            st.subheader(f"Bedrijven verrijken  —  POST /v3/companies/enrich  ({len(ids)} IDs)")
            with st.spinner("Bezig…"):
                status, data, headers = call_enrich_companies_by_id(api_key, ids, selected_reveal)
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_company_table(data)

    # ── Execute: contact enrich by ID ─────────────────────────────────────────
    if run_enrich_contacts:
        ids = [line.strip() for line in contact_ids_raw.splitlines() if line.strip()]
        if not ids:
            st.error("Voer minimaal één contact-ID in.")
        else:
            if not reveal_email and not reveal_phone:
                st.warning(
                    "Geen reveal geselecteerd — de API-call wordt uitgevoerd zonder reveal-lijst. "
                    "Je ziet alleen metadata, geen e-mail of telefoon."
                )
            st.subheader(f"Contacten verrijken  —  POST /v3/contacts/enrich  ({len(ids)} IDs)")
            with st.spinner("Bezig…"):
                status, data, headers = call_enrich_contacts(
                    api_key, ids, reveal_email, reveal_phone
                )
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_contact_table(data)


if __name__ == "__main__":
    main()
