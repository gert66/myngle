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

NOTE: Endpoint payload shapes are based on the Lusha V3 OpenAPI schema.
      If the schema changes, review the payload dicts marked with:
      # [LUSHA SCHEMA] - verify against current OpenAPI spec
"""

import json
import re
import io
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
    401: "401 — API-sleutel ontbreekt of is ongeldig.",
    402: "402 — Onvoldoende credits of betaling vereist.",
    403: "403 — Account inactief of toegang verboden.",
    429: "429 — Rate limit overschreden. Probeer later opnieuw.",
    451: "451 — Juridische of AVG/GDPR-beperking op dit record.",
}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def normalize_domain(raw: str) -> str:
    """
    Convert any URL or domain string to a bare domain suitable for API calls.
    Examples:
        https://www.example.com/path  -> example.com
        www.example.com               -> example.com
        example.com                   -> example.com
    """
    raw = raw.strip()
    if not raw:
        return ""
    # Add scheme so urlparse works reliably
    if not re.match(r"https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    host = parsed.hostname or ""
    # Strip leading www.
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
        response_json is {} on network / decode errors (error key added).
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
    """Recursively flatten a nested dict to a single-level dict with dotted keys."""
    items: list = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep).items())
        elif isinstance(v, list):
            # Serialize lists as JSON strings to keep dataframe cells readable
            items.append((new_key, json.dumps(v, ensure_ascii=False)))
        else:
            items.append((new_key, v))
    return dict(items)


def extract_rate_limit_headers(headers: dict) -> dict:
    """Return only the Lusha rate-limit headers (case-insensitive lookup)."""
    lower = {k.lower(): v for k, v in headers.items()}
    return {h: lower[h] for h in RATE_LIMIT_HEADERS if h in lower}


def response_to_dataframe(records: list[dict]) -> pd.DataFrame:
    """Flatten a list of dicts and return a DataFrame."""
    if not records:
        return pd.DataFrame()
    flat = [flatten_dict(r) for r in records]
    return pd.DataFrame(flat)


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
    elif status_code in ERROR_MESSAGES:
        msg = ERROR_MESSAGES[status_code]
        api_msg = data.get("message") or data.get("error") or ""
        st.error(f"{msg}{(' — ' + api_msg) if api_msg else ''}")
    elif status_code in (200, 201):
        st.success(f"HTTP {status_code} — Verzoek geslaagd.")
    else:
        st.warning(f"HTTP {status_code}")


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def json_bytes(data: dict | list) -> bytes:
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# API call wrappers
# ---------------------------------------------------------------------------


def call_account_status(api_key: str):
    """
    Check account/plan status.
    # [LUSHA SCHEMA] - Lusha V3 does not document a dedicated account endpoint;
    # using /v3/company/search with an empty-ish payload as a key health check.
    # Replace with a proper /account or /usage endpoint if Lusha adds one.
    """
    # Minimal search to verify the key works without consuming credits
    payload = {"company": {"name": "Lusha"}, "reveal": {}}  # [LUSHA SCHEMA]
    return lusha_request("POST", "/v3/company/search", api_key, payload=payload)


def call_search_company(api_key: str, name: str, domain: str, country: str):
    """
    Search company by name and/or domain.
    # [LUSHA SCHEMA] - POST /v3/company/search
    """
    company: dict = {}
    if name:
        company["name"] = name
    if domain:
        company["website"] = domain  # [LUSHA SCHEMA] field name may be 'domain' or 'website'
    if country:
        company["country"] = country
    payload = {"company": company}  # no reveal block → no credits consumed
    return lusha_request("POST", "/v3/company/search", api_key, payload=payload)


def call_search_enrich_company(
    api_key: str,
    name: str,
    domain: str,
    country: str,
    reveal_company: bool,
):
    """
    Search and enrich company.
    # [LUSHA SCHEMA] - POST /v3/company/search with reveal block
    The 'reveal' field controls which paid fields are returned.
    """
    company: dict = {}
    if name:
        company["name"] = name
    if domain:
        company["website"] = domain  # [LUSHA SCHEMA]
    if country:
        company["country"] = country
    reveal: dict = {}
    if reveal_company:
        reveal["company"] = True  # [LUSHA SCHEMA] - check exact key name in OpenAPI
    payload = {"company": company, "reveal": reveal}
    return lusha_request("POST", "/v3/company/search", api_key, payload=payload)


def call_decision_makers(
    api_key: str,
    name: str,
    domain: str,
    country: str,
    max_contacts: int,
    reveal_email: bool,
    reveal_phone: bool,
):
    """
    Find decision makers for a company.
    # [LUSHA SCHEMA] - POST /v3/company/contacts  (or /v3/contacts/search)
    The reveal block controls whether email/phone are returned (costs credits).
    """
    company: dict = {}
    if name:
        company["name"] = name
    if domain:
        company["website"] = domain  # [LUSHA SCHEMA]
    if country:
        company["country"] = country
    reveal: dict = {}
    if reveal_email:
        reveal["email"] = True  # [LUSHA SCHEMA]
    if reveal_phone:
        reveal["phone"] = True  # [LUSHA SCHEMA]
    payload = {
        "company": company,
        "contacts": {"limit": max_contacts},  # [LUSHA SCHEMA] - field name may differ
        "reveal": reveal,
    }
    # [LUSHA SCHEMA] - endpoint path may be /v3/company/contacts or /v3/contacts/search
    return lusha_request("POST", "/v3/company/contacts", api_key, payload=payload)


def call_enrich_contact(api_key: str, contact_id: str, reveal_email: bool, reveal_phone: bool):
    """
    Enrich a single contact by ID.
    # [LUSHA SCHEMA] - POST /v3/contacts/enrich
    """
    reveal: dict = {}
    if reveal_email:
        reveal["email"] = True  # [LUSHA SCHEMA]
    if reveal_phone:
        reveal["phone"] = True  # [LUSHA SCHEMA]
    payload = {
        "contacts": [{"id": contact_id}],  # [LUSHA SCHEMA]
        "reveal": reveal,
    }
    return lusha_request("POST", "/v3/contacts/enrich", api_key, payload=payload)


# ---------------------------------------------------------------------------
# UI sections
# ---------------------------------------------------------------------------


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
    )


def section_company_table(data: dict) -> None:
    """Extract and display company record(s) from response."""
    # [LUSHA SCHEMA] - response key may be 'company', 'companies', or 'data'
    companies = []
    for key in ("company", "companies", "data", "result"):
        val = data.get(key)
        if isinstance(val, dict):
            companies = [val]
            break
        if isinstance(val, list):
            companies = val
            break

    if not companies:
        st.info("Geen bedrijfsdata in response gevonden.")
        return

    df = response_to_dataframe(companies)
    st.subheader("Bedrijfsdata")
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "⬇ Download bedrijven CSV",
        data=csv_bytes(df),
        file_name="lusha_companies.csv",
        mime="text/csv",
    )


def section_contact_table(data: dict) -> None:
    """Extract and display contact records from response."""
    # [LUSHA SCHEMA] - response key may be 'contacts', 'decisionMakers', 'data'
    contacts = []
    for key in ("contacts", "decisionMakers", "decision_makers", "data", "result"):
        val = data.get(key)
        if isinstance(val, list):
            contacts = val
            break
        if isinstance(val, dict):
            contacts = [val]
            break

    if not contacts:
        st.info("Geen contactdata in response gevonden.")
        return

    # Surface which fields were returned vs available-but-not-revealed
    revealed: list[dict] = []
    not_revealed: list[str] = []
    for c in contacts:
        flat = flatten_dict(c)
        revealed.append(flat)
        for field in ("email", "phone", "phoneNumbers", "emailAddresses"):
            if field in c and c[field] is None:
                not_revealed.append(field)

    df = pd.DataFrame(revealed)
    st.subheader("Contacten / Decision Makers")
    st.dataframe(df, use_container_width=True)

    if not_revealed:
        st.warning(
            f"Velden beschikbaar maar niet onthuld (vereisen reveal + credits): "
            f"{', '.join(set(not_revealed))}"
        )

    st.download_button(
        "⬇ Download contacten CSV",
        data=csv_bytes(df),
        file_name="lusha_contacts.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Lusha API Tester", page_icon="🔍", layout="wide")
    st.title("🔍 Lusha API Tester (diagnostisch)")
    st.caption(
        "Standalone diagnostics-app — geen onderdeel van de lead prioritizer. "
        "Gebruik uitsluitend voor handmatige API-tests."
    )

    # --- API key -----------------------------------------------------------
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

    # --- Input fields ------------------------------------------------------
    st.sidebar.header("Zoekparameters")
    company_name = st.sidebar.text_input("Bedrijfsnaam", placeholder="bijv. Acme Corp")
    company_url_raw = st.sidebar.text_input(
        "Bedrijfs-URL of domein", placeholder="bijv. https://www.acme.com"
    )
    country = st.sidebar.text_input("Land (optioneel)", placeholder="bijv. NL of Netherlands")
    max_contacts = st.sidebar.number_input(
        "Max. decision makers / contacten", min_value=1, max_value=25, value=5
    )

    domain = normalize_domain(company_url_raw) if company_url_raw else ""
    if domain:
        st.sidebar.caption(f"Genormaliseerd domein: `{domain}`")

    # --- Reveal checkboxes -------------------------------------------------
    st.sidebar.markdown("---")
    st.sidebar.header("Reveal-instellingen (credits!)")
    st.sidebar.warning(
        "⚠️ Reveal-opties kunnen Lusha-credits verbruiken. "
        "Zet alleen aan als je dit bewust wilt."
    )
    reveal_company = st.sidebar.checkbox("Reveal bedrijfsdata", value=True)
    reveal_email = st.sidebar.checkbox("Reveal e-mailadressen", value=False)
    reveal_phone = st.sidebar.checkbox("Reveal telefoonnummers", value=False)

    # --- Buttons -----------------------------------------------------------
    st.markdown("### Acties")

    if not api_key:
        st.warning("Voer eerst een API-sleutel in via de sidebar of secrets.toml.")
        return

    col1, col2, col3, col4 = st.columns(4)

    run_account = col1.button("🔑 Test sleutel / account")
    run_search = col2.button("🔎 Zoek bedrijf")
    run_enrich = col3.button("🏢 Zoek + verrijk bedrijf")
    run_dm = col4.button("👥 Decision makers")

    # --- Contact enrich (optional follow-up) ------------------------------
    st.markdown("---")
    with st.expander("Contact-ID's verrijken (optioneel follow-up)", expanded=False):
        contact_ids_raw = st.text_area(
            "Contact-ID's (één per regel)",
            placeholder="abc123\ndef456",
            height=100,
        )
        if reveal_email or reveal_phone:
            st.warning("⚠️ Reveal e-mail/telefoon staat aan — dit verbruikt credits per contact.")
        run_enrich_contacts = st.button("Verrijk geselecteerde contacten")

    st.markdown("---")

    # --- Execute actions --------------------------------------------------

    if run_account:
        st.subheader("Sleutel / account status")
        with st.spinner("Bezig…"):
            status, data, headers = call_account_status(api_key)
        section_raw_output(status, data, headers)
        if isinstance(data, dict):
            section_company_table(data)

    if run_search:
        if not company_name and not domain:
            st.error("Vul minimaal een bedrijfsnaam of domein in.")
        else:
            st.subheader("Bedrijf zoeken")
            with st.spinner("Bezig…"):
                status, data, headers = call_search_company(api_key, company_name, domain, country)
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_company_table(data)

    if run_enrich:
        if not company_name and not domain:
            st.error("Vul minimaal een bedrijfsnaam of domein in.")
        else:
            if reveal_email or reveal_phone:
                st.warning("⚠️ Reveal e-mail/telefoon staat aan — dit verbruikt credits.")
            st.subheader("Bedrijf zoeken + verrijken")
            with st.spinner("Bezig…"):
                status, data, headers = call_search_enrich_company(
                    api_key, company_name, domain, country, reveal_company
                )
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_company_table(data)
                section_contact_table(data)

    if run_dm:
        if not company_name and not domain:
            st.error("Vul minimaal een bedrijfsnaam of domein in.")
        else:
            if reveal_email or reveal_phone:
                st.warning(
                    "⚠️ Reveal e-mail/telefoon staat aan — dit verbruikt credits per contact."
                )
            st.subheader("Decision Makers / Contacten")
            with st.spinner("Bezig…"):
                status, data, headers = call_decision_makers(
                    api_key,
                    company_name,
                    domain,
                    country,
                    int(max_contacts),
                    reveal_email,
                    reveal_phone,
                )
            section_raw_output(status, data, headers)
            if isinstance(data, dict):
                section_company_table(data)
                section_contact_table(data)

    if run_enrich_contacts:
        ids = [line.strip() for line in contact_ids_raw.splitlines() if line.strip()]
        if not ids:
            st.error("Voer minimaal één contact-ID in.")
        else:
            if reveal_email or reveal_phone:
                st.warning(
                    f"⚠️ Reveal staat aan voor {len(ids)} contact(en) — dit verbruikt credits."
                )
            st.subheader(f"Contacten verrijken ({len(ids)} IDs)")
            for cid in ids:
                st.markdown(f"**Contact ID: `{cid}`**")
                with st.spinner(f"Verrijken {cid}…"):
                    status, data, headers = call_enrich_contact(
                        api_key, cid, reveal_email, reveal_phone
                    )
                section_raw_output(status, data, headers)
                if isinstance(data, dict):
                    section_contact_table(data)
                st.markdown("---")


if __name__ == "__main__":
    main()
