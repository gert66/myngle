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
this app can never trigger a per-field contact-reveal charge. Companies/
prospecting itself IS billed, via the `companySearch` entry in
/v3/account/usage's `pricing` block. Confirmed live (2026-07-14) to be
1 credit per 25 results, rounded up PER PAGE CALL -- not per company, and
not a flat per-result rate as Lusha's public docs example suggests. A
partial last page (e.g. 30 of a possible 50) still rounds up to the same
credit cost as a full page, which `estimate_credits_for_download` mirrors.
This app calls /v3/account/usage first and shows your real balance and
pricing before any bulk pull runs -- note that the account-wide "remaining"
balance itself lags real usage by some delay (confirmed empirically: it
does not decrement immediately after a call that reports a nonzero
`billing.creditsCharged`), so trust the per-call/per-run credit counts this
app surfaces over the sidebar balance for "what did this run just cost".

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

import math
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


def estimate_credits_for_download(total_results: int, page_size: int = _PAGE_SIZE) -> int:
    """Estimate the credits ``fetch_all_companies`` will spend to pull
    ``total_results`` results, from Lusha's confirmed ``companySearch``
    pricing: 1 credit per 25 results, rounded up PER PAGE CALL rather than
    over the total. A partial last page (e.g. 30 of a possible 50-result
    page) still rounds up to the same cost as a full page, so this mirrors
    ``fetch_all_companies``'s actual page-by-page billing instead of a
    naive ``ceil(total_results / 25)``."""
    if total_results <= 0:
        return 0
    full_pages, remainder = divmod(total_results, page_size)
    credits = full_pages * math.ceil(page_size / 25)
    if remainder:
        credits += math.ceil(remainder / 25)
    return credits


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


def fetch_main_industries(key: str) -> list[dict]:
    """Live list of Lusha's main industries, each with its
    ``sub_industries``. Confirmed live shape (2026-07-13, 17 main
    industries): ``{"main_industry": "Government", "main_industry_id": 10,
    "sub_industries": [{"value": "Military", "id": 53}, ...]}``. Raises on
    failure -- unlike ``resolve_industry_labels`` this feeds an actual
    query, so a silent empty result would look identical to "no industries
    excluded" and change what gets fetched without any error shown."""
    resp = requests.get(
        _BASE_URL + _INDUSTRY_LABELS_ENDPOINT, headers=_headers(key), timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    values = resp.json().get("values")
    return values if isinstance(values, list) else []


def resolve_industry_labels(key: str, ids: list[int]) -> dict[int, str]:
    """Best-effort main_industry_id -> main_industry label lookup for
    display only ("excluding: Government, Community"). Returns a partial/
    empty dict rather than raising on an unexpected response shape --
    cosmetic, not required for the actual prospecting query to work."""
    try:
        values = fetch_main_industries(key)
        return {
            v["main_industry_id"]: v.get("main_industry", str(v["main_industry_id"]))
            for v in values
            if isinstance(v, dict) and v.get("main_industry_id") in ids
        }
    except Exception:
        return {}


def build_prospecting_request(
    *, location: dict, size_bands: list[dict], page: int,
    excluded_industry_ids: "list[int] | None" = None,
    included_main_industry_ids: "list[int] | None" = None,
) -> dict:
    """One page of a companies/prospecting request body, in the shape of
    the working curl templates this app replaces.

    ``included_main_industry_ids`` and ``excluded_industry_ids`` are
    mutually exclusive query strategies, not meant to be combined: pass
    the exclude list to fetch everyone except a few industries (results
    carry no industry label), or the include list to fetch exactly one/a
    few industries at a time (see ``fetch_companies_by_sector`` -- the
    result set has NO industry field either way, but a caller looping the
    include filter one industry-id at a time already knows the label from
    which call found it, for free, without a paid Enrich call)."""
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
    if included_main_industry_ids:
        body["filters"]["companies"]["include"]["mainIndustriesIds"] = list(included_main_industry_ids)
    if excluded_industry_ids:
        body["filters"]["companies"]["exclude"] = {
            "mainIndustriesIds": list(excluded_industry_ids),
        }
    return body


def resolve_secret(secrets, key_name: str) -> str:
    """``os.environ`` takes priority (explicit override), then a
    Streamlit-``st.secrets``-like mapping (``.streamlit/secrets.toml``,
    auto-loaded by Streamlit -- ``secrets`` may be ``None`` outside a
    running app, or when no secrets.toml exists at all), else empty.
    Never raises."""
    value = os.environ.get(key_name, "")
    if value:
        return value
    if secrets is None:
        return ""
    try:
        return str(secrets.get(key_name, "") or "")
    except Exception:
        return ""


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
    included_main_industry_ids: "list[int] | None" = None,
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
            included_main_industry_ids=included_main_industry_ids,
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


def fetch_companies_by_sector(
    key: str, *, location: dict, size_bands: list[dict],
    main_industries: list[dict], progress_callback=None,
) -> "tuple[list[dict], dict]":
    """Fetch every company matching ``location``/``size_bands``, one main
    industry at a time (``include.mainIndustriesIds=[id]`` per call rather
    than the single ``exclude``-based call). Each result is annotated with
    ``main_industry``/``main_industry_id`` from the loop iteration that
    found it -- known for free, since we filtered for exactly that
    industry, with no separate (paid) Enrich call needed.

    Trade-off, by construction: a company Lusha has NOT tagged with ANY
    main industry can never match an ``include.mainIndustriesIds`` filter,
    so it is never returned by this function at all -- unlike the single
    exclude-based call (``fetch_all_companies`` with
    ``excluded_industry_ids``), which returns everyone regardless of
    whether they have an industry label. Use ``fetch_all_companies`` first
    and diff by id against this function's results if you need to find
    that "no industry assigned" remainder too.

    ``progress_callback(industry_index, industry_count, industry_label,
    companies_collected_so_far)`` is called after each industry finishes.

    Returns ``(companies, stats)`` where ``stats`` has
    ``{"credits_charged", "companies_collected", "per_industry"}`` --
    ``per_industry`` is ``{main_industry_id: company_count}``.
    """
    seen_ids: set = set()
    companies: list[dict] = []
    credits_charged = 0
    per_industry: dict = {}

    for i, industry in enumerate(main_industries):
        industry_id = industry["main_industry_id"]
        industry_label = industry.get("main_industry", str(industry_id))
        batch, batch_stats = fetch_all_companies(
            key, location=location, size_bands=size_bands,
            included_main_industry_ids=[industry_id],
        )
        credits_charged += batch_stats["credits_charged"]
        new_count = 0
        for c in batch:
            cid = c.get("id")
            if cid is not None:
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
            c = dict(c)
            c["main_industry"] = industry_label
            c["main_industry_id"] = industry_id
            companies.append(c)
            new_count += 1
        per_industry[industry_id] = new_count
        if progress_callback is not None:
            progress_callback(i, len(main_industries), industry_label, len(companies))

    stats = {
        "credits_charged": credits_charged,
        "companies_collected": len(companies),
        "per_industry": per_industry,
    }
    return companies, stats


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import io

    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Lusha Prospecting", page_icon="\U0001f50e", layout="wide")
    st.title("\U0001f50e Lusha Prospecting — bulk company download")
    st.caption(
        "Fetches only companies/prospecting results (id, name, domain, "
        "basic firmographics) — never companies/enrich or a contact "
        "endpoint, so this never triggers a per-field reveal charge for "
        "email/phone. Prospecting itself IS billed — confirmed via live "
        "testing to be 1 credit per 25 results, rounded up per page call — "
        "check that below against your own account before pulling everything."
    )

    with st.sidebar:
        st.header("API key")
        default_key = resolve_secret(getattr(st, "secrets", None), "LUSHA_API_KEY")
        api_key = st.text_input(
            "Lusha API key", value=default_key, type="password",
            help="Defaults from the LUSHA_API_KEY environment variable, "
                 "otherwise .streamlit/secrets.toml; override here for a "
                 "one-off session.",
        )
        if st.button("\U0001f4b3 Test account & pricing", disabled=not api_key):
            try:
                st.session_state["_lusha_account_usage"] = get_account_usage(api_key)
            except Exception as exc:
                st.error(f"Could not fetch account info: {exc}")
        usage = st.session_state.get("_lusha_account_usage")
        if usage:
            credits = usage.get("credits", {})
            c1, c2 = st.columns(2)
            c1.metric("Credits remaining", credits.get("remaining", "?"))
            c2.metric("Credits total", credits.get("total", "?"))
            pricing = usage.get("pricing", {})
            search_price = pricing.get("companySearch")
            if search_price:
                st.caption(
                    f"Price per prospecting page on this plan: "
                    f"{search_price.get('credits', '?')} credit(s) per "
                    f"{search_price.get('perQuantity', '?')} results, "
                    "rounded up per page call."
                )
            else:
                st.caption(
                    "Could not get the exact companies/prospecting price "
                    "from the account info — confirmed via live testing to "
                    "typically be 1 credit per 25 results, rounded up per "
                    "page call; verify this against your own contract "
                    "before pulling at scale."
                )

    if not api_key:
        st.info("Enter an API key on the left to get started.")
        return

    st.subheader("1. Country")
    country_query = st.text_input("Country", value="Uruguay", key="country_query")
    if st.button("\U0001f50d Look up country", disabled=not country_query):
        try:
            st.session_state["_location_matches"] = find_locations(api_key, country_query)
        except Exception as exc:
            st.error(f"Lookup failed: {exc}")

    matches = st.session_state.get("_location_matches") or []
    location = None
    if matches:
        labels = [
            f"{m.get('country', '?')} — continent: {m.get('continent', '?')}, "
            f"grouping: {m.get('countryGrouping', '?')}"
            for m in matches
        ]
        idx = st.selectbox(
            "Choose the exact match (straight from Lusha)", options=list(range(len(matches))),
            format_func=lambda i: labels[i], key="location_select",
        )
        location = matches[idx]
    elif country_query:
        st.caption("Click '\U0001f50d Look up country' to resolve the exact Lusha location.")

    st.subheader("2. Company size (employees)")
    chosen_bands: list[dict] = []
    cols = st.columns(len(SIZE_BANDS))
    for i, band in enumerate(SIZE_BANDS):
        if cols[i].checkbox(size_band_label(band), value=True, key=f"size_band_{i}"):
            chosen_bands.append(band)

    st.subheader("3. Industry exclusion")
    exclude_on = st.checkbox(
        "Exclude Government & Community (industry IDs 5 and 10 — as in "
        "the existing queries)",
        value=True, key="exclude_industries_cb",
    )
    excluded_ids = list(DEFAULT_EXCLUDED_INDUSTRY_IDS) if exclude_on else []
    if exclude_on:
        labels_map = resolve_industry_labels(api_key, excluded_ids)
        if labels_map:
            st.caption("Will be excluded: " + ", ".join(
                f"{labels_map.get(i, i)} (id {i})" for i in excluded_ids))

    if location and chosen_bands:
        with st.expander("\U0001f4c4 Preview of the API call (page 0)"):
            st.json(build_prospecting_request(
                location=location, size_bands=chosen_bands, page=0,
                excluded_industry_ids=excluded_ids,
            ))

    st.divider()
    st.subheader("4. Fetch")
    ready = bool(location and chosen_bands)
    if not ready:
        st.caption("First choose a country (look it up) and at least one size band.")

    if st.button("\U0001f9ea Test 1 page first", disabled=not ready):
        try:
            body = build_prospecting_request(
                location=location, size_bands=chosen_bands, page=0,
                excluded_industry_ids=excluded_ids,
            )
            data = fetch_prospecting_page(api_key, body)
            pagination = data.get("pagination", {})
            billing = data.get("billing", {})
            total = pagination.get("total")
            msg = (
                f"Total matches reported by Lusha: {total if total is not None else '?'} — "
                f"credits for this test page: {billing.get('creditsCharged', '?')}"
            )
            if total is not None:
                st.session_state["_lusha_preview_total"] = total
                msg += (
                    f" — estimated credits to fetch all {total} results: "
                    f"~{estimate_credits_for_download(total)}"
                )
            st.success(msg)
            st.dataframe(pd.DataFrame(data.get("results") or []), use_container_width=True)
        except Exception as exc:
            st.error(f"Test call failed: {exc}")

    preview_total = st.session_state.get("_lusha_preview_total")
    if preview_total is not None:
        st.caption(
            f"\U0001f4ca Last checked: {preview_total} matching results — "
            f"estimated cost to fetch all of them: ~{estimate_credits_for_download(preview_total)} credits. "
            "(Re-run 'Test 1 page first' if you changed the filters above — "
            "this number won't update on its own.)"
        )

    if st.button("\U0001f680 Fetch everything", type="primary", disabled=not ready):
        progress_bar = st.progress(0.0)
        status = st.empty()

        def _progress(page, collected, total):
            progress_bar.progress(min(1.0, collected / total) if total else 0.0)
            status.text(f"Page {page + 1} — {collected} of {total or '?'} companies fetched…")

        try:
            companies, stats = fetch_all_companies(
                api_key, location=location, size_bands=chosen_bands,
                excluded_industry_ids=excluded_ids, progress_callback=_progress,
            )
        except Exception as exc:
            st.error(f"Fetch failed: {exc}")
        else:
            st.session_state["_lusha_results"] = companies
            st.success(
                f"Done: {stats['companies_collected']} companies fetched over "
                f"{stats['pages_fetched']} page(s) (of {stats['total_reported']} "
                f"reported total), {stats['credits_charged']} credits used."
            )

    results = st.session_state.get("_lusha_results")
    if results:
        df = pd.DataFrame(results)
        st.subheader(f"Results ({len(df)})")
        st.dataframe(df, use_container_width=True)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Companies")
        st.download_button(
            "⬇️ Download as Excel", data=buf.getvalue(),
            file_name=f"lusha_prospecting_{country_query.strip().lower().replace(' ', '_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":  # pragma: no cover
    main()
