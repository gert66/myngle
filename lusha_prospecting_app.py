"""
lusha_prospecting_app.py
-------------------------
Streamlit app: bulk-download company prospecting results from the Lusha V3
API for one country and a chosen set of employee-size bands, excluding
Government/Community industries by default -- automating the same filter
shape as the manually-run curl template this app replaces (previously done
by hand in batches of ~1000 via Lusha's own UI).

Only ever calls /v3/companies/prospecting (id/name/domain + basic
firmographics) -- never /v3/companies/enrich or any contact endpoint, so
this app can never trigger a per-field contact-reveal charge. Per Lusha's
own API docs, companies/prospecting itself IS billed per result via the
`api_search` action (their published example shows 1 credit/result) --
that may differ from what your actual contract charges, which is exactly
why this app calls /v3/account/usage first and shows your real balance and
pricing before any bulk pull runs.

Country -> {country, continent, countryGrouping} is resolved through
Lusha's own filter-discovery endpoint rather than hand-built, since the
official schema uses camelCase `countryGrouping` (not the `country_grouping`
seen in some hand-written curl examples) and getting that field wrong can
silently return zero results instead of an error.

Pagination pages through results (Lusha's own cap: 1000 pages x 50 = 50,000
results) using the API's own `pagination.total`, de-duping by company `id`
as a safety net -- belt-and-braces against exactly the kind of drift that
made the old manual "batch of 1000 + exclude what I already have" UI
workaround necessary.

The `import streamlit`/`pandas` calls are deliberately lazy (inside `main`)
so the pure helper functions below can be imported and unit-tested without
Streamlit installed.

Run with:
    streamlit run lusha_prospecting_app.py
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests

_BASE_URL = "https://api.lusha.com"
_PROSPECTING_ENDPOINT = "/v3/companies/prospecting"
_LOCATIONS_FILTER_ENDPOINT = "/v3/companies/prospecting/filters/locations"
_INDUSTRY_LABELS_ENDPOINT = "/v3/companies/prospecting/filters/industriesLabels"
_ACCOUNT_USAGE_ENDPOINT = "/v3/account/usage"
_REQUEST_TIMEOUT = 30  # seconds
_PAGE_SIZE = 50  # Lusha's documented max results per page
_MAX_PAGES = 1000  # Lusha's documented cap (1000 x 50 = 50,000 results)

#: The 7 employee-size bands used in every prospecting query run so far
#: (Germany, Uruguay, ...) -- together they cover 51+ employees without
#: gaps or overlaps. Below 51 is deliberately excluded (Myngle's commercial
#: minimum) -- see commercial_fit_scoring.py's default_commercial_minimum_assumption.
SIZE_BANDS: list[dict] = [
    {"min": 51, "max": 200},
    {"min": 201, "max": 500},
    {"min": 501, "max": 1000},
    {"min": 1001, "max": 5000},
    {"min": 5001, "max": 10000},
    {"min": 10001, "max": 100000},
    {"min": 100001},
]

#: Industries excluded by default in the working queries this app is
#: modeled on: Government and Community.
DEFAULT_EXCLUDED_INDUSTRY_IDS: list[int] = [5, 10]


class RateLimited(Exception):
    """Raised when Lusha returns 429; caller decides how long to back off."""

    def __init__(self, retry_after: "str | None"):
        self.retry_after = retry_after
        super().__init__(f"Rate limited by Lusha (retry after {retry_after}s)")


def size_band_label(band: dict) -> str:
    return f"{band['min']}+" if "max" not in band else f"{band['min']}–{band['max']}"


def _headers(key: str) -> dict:
    return {"api_key": key, "Content-Type": "application/json", "Accept": "application/json"}


def get_account_usage(key: str) -> dict:
    """Credits/rate-limit/pricing snapshot for THIS account -- call before
    any bulk pull so the user sees real numbers for their own plan instead
    of Lusha's generic public-docs example."""
    resp = requests.get(
        _BASE_URL + _ACCOUNT_USAGE_ENDPOINT, headers=_headers(key), timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def find_locations(key: str, query: str) -> list[dict]:
    """Resolve free-text (e.g. "Uruguay") into Lusha's own location filter
    objects. Always go through Lusha's discovery endpoint rather than
    hand-building {country, continent, countryGrouping} -- that field is
    easy to get subtly wrong (casing, grouping label) and a wrong value
    tends to silently return zero results rather than an error."""
    resp = requests.get(
        _BASE_URL + _LOCATIONS_FILTER_ENDPOINT,
        headers=_headers(key), params={"query": query}, timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    values = resp.json().get("values")
    return values if isinstance(values, list) else []


def resolve_industry_labels(key: str, ids: list[int]) -> dict[int, str]:
    """Best-effort id -> label lookup for display only ("excluding:
    Government, Community"). Returns a partial/empty dict rather than
    raising on an unexpected response shape -- cosmetic, not required for
    the actual prospecting query to work."""
    try:
        resp = requests.get(
            _BASE_URL + _INDUSTRY_LABELS_ENDPOINT, headers=_headers(key), timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        values = resp.json().get("values")
        out: dict[int, str] = {}
        if isinstance(values, list):
            for v in values:
                if isinstance(v, dict) and v.get("id") in ids:
                    out[v["id"]] = v.get("label") or v.get("name") or str(v.get("id"))
        return out
    except Exception:
        return {}


def build_prospecting_request(
    *, location: dict, size_bands: list[dict], page: int,
    excluded_industry_ids: "list[int] | None" = None,
) -> dict:
    """One page of a companies/prospecting request body, in the shape of
    the working curl templates this app replaces."""
    body: dict = {
        "pagination": {"page": page, "size": _PAGE_SIZE},
        "filters": {
            "companies": {
                "include": {
                    "locations": [location],
                    "sizes": size_bands,
                },
            },
        },
    }
    if excluded_industry_ids:
        body["filters"]["companies"]["exclude"] = {
            "mainIndustriesIds": list(excluded_industry_ids),
        }
    return body


def fetch_prospecting_page(key: str, body: dict) -> dict:
    resp = requests.post(
        _BASE_URL + _PROSPECTING_ENDPOINT, headers=_headers(key), json=body, timeout=_REQUEST_TIMEOUT)
    if resp.status_code == 429:
        raise RateLimited(resp.headers.get("Retry-After"))
    resp.raise_for_status()
    return resp.json()


def fetch_all_companies(
    key: str, *, location: dict, size_bands: list[dict],
    excluded_industry_ids: "list[int] | None" = None,
    progress_callback=None, stop_flag=None,
) -> "tuple[list[dict], dict]":
    """Page through /v3/companies/prospecting until exhausted, using the
    API's own ``pagination.total`` rather than a fixed page count. De-dupes
    by company ``id`` as a safety net -- belt-and-braces against the kind
    of drift that made the old manual "batch of 1000 + exclude what I
    already have" workaround necessary in Lusha's UI.

    ``progress_callback(page, companies_collected_so_far, total_reported)``
    is called after every page. ``stop_flag`` is an optional ``() -> bool``
    checked between pages for cooperative cancellation.

    Returns ``(companies, stats)`` where ``stats`` has
    ``{"total_reported", "pages_fetched", "credits_charged", "companies_collected"}``.
    """
    seen_ids: set = set()
    companies: list[dict] = []
    total_reported: Optional[int] = None
    credits_charged = 0
    page = 0
    while True:
        if stop_flag is not None and stop_flag():
            break
        body = build_prospecting_request(
            location=location, size_bands=size_bands, page=page,
            excluded_industry_ids=excluded_industry_ids,
        )
        try:
            data = fetch_prospecting_page(key, body)
        except RateLimited as exc:
            time.sleep(float(exc.retry_after or 5))
            continue
        results = data.get("results") or []
        pagination = data.get("pagination") or {}
        billing = data.get("billing") or {}
        total_reported = pagination.get("total", total_reported)
        credits_charged += billing.get("creditsCharged", 0) or 0
        new_count = 0
        for c in results:
            cid = c.get("id")
            if cid is not None:
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
            companies.append(c)
            new_count += 1
        if progress_callback is not None:
            progress_callback(page, len(companies), total_reported)
        if not results or new_count == 0:
            break
        if total_reported is not None and (page + 1) * _PAGE_SIZE >= total_reported:
            break
        page += 1
        if page >= _MAX_PAGES:
            break
    stats = {
        "total_reported": total_reported,
        "pages_fetched": page + 1,
        "credits_charged": credits_charged,
        "companies_collected": len(companies),
    }
    return companies, stats


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import io

    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Lusha Prospecting", page_icon="\U0001f50e", layout="wide")
    st.title("\U0001f50e Lusha Prospecting — bulk bedrijven ophalen")
    st.caption(
        "Haalt uitsluitend companies/prospecting-resultaten op (id, naam, "
        "domein, basisfirmografie) — nooit companies/enrich of een "
        "contact-endpoint, dus nooit een per-veld reveal-kost voor e-mail/"
        "telefoon. Prospecting zelf wordt volgens Lusha's eigen API-docs wel "
        "per resultaat gerekend (voorbeeld: 1 credit/resultaat) — "
        "controleer dat hieronder tegen je eigen account voor je alles ophaalt."
    )

    with st.sidebar:
        st.header("API-key")
        env_key = os.environ.get("LUSHA_API_KEY", "")
        api_key = st.text_input(
            "Lusha API-key", value=env_key, type="password",
            help="Standaard gevuld vanuit de omgevingsvariabele LUSHA_API_KEY "
                 "als die gezet is; overschrijf hier voor een eenmalige sessie.",
        )
        if st.button("\U0001f4b3 Account & prijzen testen", disabled=not api_key):
            try:
                st.session_state["_lusha_account_usage"] = get_account_usage(api_key)
            except Exception as exc:
                st.error(f"Kon account-info niet ophalen: {exc}")
        usage = st.session_state.get("_lusha_account_usage")
        if usage:
            credits = usage.get("credits", {})
            c1, c2 = st.columns(2)
            c1.metric("Credits over", credits.get("remaining", "?"))
            c2.metric("Credits totaal", credits.get("total", "?"))
            pricing = usage.get("pricing", {})
            search_price = pricing.get("apiSearch") or pricing.get("api_search")
            if search_price:
                st.caption(f"Prijs per prospecting-resultaat op dit plan: {search_price}")
            else:
                st.caption(
                    "Kon de exacte prijs voor companies/prospecting niet uit "
                    "de account-info halen — Lusha's publieke voorbeeld "
                    "toont 1 credit/resultaat; verifieer dit tegen je eigen "
                    "contract voor je op grote schaal ophaalt."
                )

    if not api_key:
        st.info("Vul links een API-key in om te beginnen.")
        return

    st.subheader("1. Land")
    country_query = st.text_input("Land", value="Uruguay", key="country_query")
    if st.button("\U0001f50d Land opzoeken", disabled=not country_query):
        try:
            st.session_state["_location_matches"] = find_locations(api_key, country_query)
        except Exception as exc:
            st.error(f"Opzoeken mislukt: {exc}")

    matches = st.session_state.get("_location_matches") or []
    location = None
    if matches:
        labels = [
            f"{m.get('country', '?')} — continent: {m.get('continent', '?')}, "
            f"grouping: {m.get('countryGrouping', '?')}"
            for m in matches
        ]
        idx = st.selectbox(
            "Kies de exacte match (rechtstreeks van Lusha)", options=list(range(len(matches))),
            format_func=lambda i: labels[i], key="location_select",
        )
        location = matches[idx]
    elif country_query:
        st.caption("Klik '\U0001f50d Land opzoeken' om de exacte Lusha-locatie te bepalen.")

    st.subheader("2. Bedrijfsgrootte (medewerkers)")
    chosen_bands: list[dict] = []
    cols = st.columns(len(SIZE_BANDS))
    for i, band in enumerate(SIZE_BANDS):
        if cols[i].checkbox(size_band_label(band), value=True, key=f"size_band_{i}"):
            chosen_bands.append(band)

    st.subheader("3. Industrie-uitsluiting")
    exclude_on = st.checkbox(
        "Overheid & community uitsluiten (industrie-ID's 5 en 10 — zoals "
        "in de bestaande query's)",
        value=True, key="exclude_industries_cb",
    )
    excluded_ids = list(DEFAULT_EXCLUDED_INDUSTRY_IDS) if exclude_on else []
    if exclude_on:
        labels_map = resolve_industry_labels(api_key, excluded_ids)
        if labels_map:
            st.caption("Wordt uitgesloten: " + ", ".join(
                f"{labels_map.get(i, i)} (id {i})" for i in excluded_ids))

    if location and chosen_bands:
        with st.expander("\U0001f4c4 Voorbeeld van de API-aanroep (pagina 0)"):
            st.json(build_prospecting_request(
                location=location, size_bands=chosen_bands, page=0,
                excluded_industry_ids=excluded_ids,
            ))

    st.divider()
    st.subheader("4. Ophalen")
    ready = bool(location and chosen_bands)
    if not ready:
        st.caption("Kies eerst een land (opzoeken) en minstens één grootteband.")

    if st.button("\U0001f9ea Eerst 1 pagina testen", disabled=not ready):
        try:
            body = build_prospecting_request(
                location=location, size_bands=chosen_bands, page=0,
                excluded_industry_ids=excluded_ids,
            )
            data = fetch_prospecting_page(api_key, body)
            pagination = data.get("pagination", {})
            billing = data.get("billing", {})
            st.success(
                f"Totaal aantal matches volgens Lusha: {pagination.get('total', '?')} — "
                f"credits voor deze testpagina: {billing.get('creditsCharged', '?')}"
            )
            st.dataframe(pd.DataFrame(data.get("results") or []), use_container_width=True)
        except Exception as exc:
            st.error(f"Testaanroep mislukt: {exc}")

    if st.button("\U0001f680 Alles ophalen", type="primary", disabled=not ready):
        progress_bar = st.progress(0.0)
        status = st.empty()

        def _progress(page, collected, total):
            progress_bar.progress(min(1.0, collected / total) if total else 0.0)
            status.text(f"Pagina {page + 1} — {collected} van {total or '?'} bedrijven opgehaald…")

        try:
            companies, stats = fetch_all_companies(
                api_key, location=location, size_bands=chosen_bands,
                excluded_industry_ids=excluded_ids, progress_callback=_progress,
            )
        except Exception as exc:
            st.error(f"Ophalen mislukt: {exc}")
        else:
            st.session_state["_lusha_results"] = companies
            st.success(
                f"Klaar: {stats['companies_collected']} bedrijven opgehaald over "
                f"{stats['pages_fetched']} pagina('s) (van {stats['total_reported']} "
                f"gerapporteerd totaal), {stats['credits_charged']} credits gebruikt."
            )

    results = st.session_state.get("_lusha_results")
    if results:
        df = pd.DataFrame(results)
        st.subheader(f"Resultaten ({len(df)})")
        st.dataframe(df, use_container_width=True)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Companies")
        st.download_button(
            "⬇️ Download als Excel", data=buf.getvalue(),
            file_name=f"lusha_prospecting_{country_query.strip().lower().replace(' ', '_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":  # pragma: no cover
    main()
