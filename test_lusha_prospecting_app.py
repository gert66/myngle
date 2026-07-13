"""Tests for lusha_prospecting_app.py's pure request-building and pagination
logic. No real Lusha calls -- fetch_prospecting_page is monkeypatched with an
in-memory fake paginator."""

import lusha_prospecting_app as app


# ---------------------------------------------------------------------------
# size_band_label
# ---------------------------------------------------------------------------

def test_size_band_label_bounded_range():
    assert app.size_band_label({"min": 51, "max": 200}) == "51–200"


def test_size_band_label_open_ended():
    assert app.size_band_label({"min": 100001}) == "100001+"


# ---------------------------------------------------------------------------
# build_prospecting_request
# ---------------------------------------------------------------------------

_URUGUAY = {"country": "Uruguay", "continent": "South America", "countryGrouping": "latam"}


def test_build_prospecting_request_shape():
    body = app.build_prospecting_request(
        location=_URUGUAY, size_bands=app.SIZE_BANDS, page=2,
        excluded_industry_ids=[5, 10],
    )
    assert body["pagination"] == {"page": 2, "size": app._PAGE_SIZE}
    include = body["filters"]["companies"]["include"]
    assert include["locations"] == [_URUGUAY]
    assert include["sizes"] == app.SIZE_BANDS
    assert body["filters"]["companies"]["exclude"] == {"mainIndustriesIds": [5, 10]}


def test_build_prospecting_request_no_exclude_block_when_empty():
    body = app.build_prospecting_request(
        location=_URUGUAY, size_bands=app.SIZE_BANDS, page=0, excluded_industry_ids=[],
    )
    assert "exclude" not in body["filters"]["companies"]


def test_build_prospecting_request_no_exclude_block_when_none():
    body = app.build_prospecting_request(
        location=_URUGUAY, size_bands=app.SIZE_BANDS, page=0,
    )
    assert "exclude" not in body["filters"]["companies"]


def test_build_prospecting_request_included_main_industry_ids():
    body = app.build_prospecting_request(
        location=_URUGUAY, size_bands=app.SIZE_BANDS, page=0,
        included_main_industry_ids=[17],
    )
    assert body["filters"]["companies"]["include"]["mainIndustriesIds"] == [17]
    assert "exclude" not in body["filters"]["companies"]


def test_build_prospecting_request_include_and_exclude_can_coexist():
    body = app.build_prospecting_request(
        location=_URUGUAY, size_bands=app.SIZE_BANDS, page=0,
        included_main_industry_ids=[17], excluded_industry_ids=[5],
    )
    assert body["filters"]["companies"]["include"]["mainIndustriesIds"] == [17]
    assert body["filters"]["companies"]["exclude"] == {"mainIndustriesIds": [5]}


# ---------------------------------------------------------------------------
# fetch_main_industries / resolve_industry_labels
# ---------------------------------------------------------------------------

_INDUSTRIES_RESPONSE = {
    "values": [
        {"main_industry": "Government", "main_industry_id": 10,
         "sub_industries": [{"value": "Military", "id": 53}]},
        {"main_industry": "Community & Nonprofit Organizations", "main_industry_id": 5,
         "sub_industries": [{"value": "Fundraising", "id": 7}]},
        {"main_industry": "Technology, Information & Media", "main_industry_id": 17,
         "sub_industries": []},
    ],
}


class _FakeGetResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_main_industries_returns_values_list(monkeypatch):
    monkeypatch.setattr(
        app.requests, "get", lambda *a, **k: _FakeGetResponse(_INDUSTRIES_RESPONSE))
    values = app.fetch_main_industries("key")
    assert [v["main_industry_id"] for v in values] == [10, 5, 17]


def test_resolve_industry_labels_maps_by_main_industry_id(monkeypatch):
    monkeypatch.setattr(
        app.requests, "get", lambda *a, **k: _FakeGetResponse(_INDUSTRIES_RESPONSE))
    labels = app.resolve_industry_labels("key", [5, 10])
    assert labels == {10: "Government", 5: "Community & Nonprofit Organizations"}


def test_resolve_industry_labels_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(app.requests, "get", boom)
    assert app.resolve_industry_labels("key", [5, 10]) == {}


# ---------------------------------------------------------------------------
# fetch_all_companies -- pagination / dedup / stopping conditions
# ---------------------------------------------------------------------------

def _fake_fetcher(pages_by_index, calls):
    def fetch(key, body):
        calls.append(body["pagination"]["page"])
        return pages_by_index[body["pagination"]["page"]]

    return fetch


def _page(results, total, credits=1):
    return {
        "results": results,
        "pagination": {"page": 0, "size": app._PAGE_SIZE, "total": total},
        "billing": {"creditsCharged": credits},
    }


def test_fetch_all_companies_stops_when_total_reached(monkeypatch):
    pages = {
        0: _page([{"id": "1"}, {"id": "2"}], total=3),
        1: _page([{"id": "3"}], total=3),
    }
    calls = []
    monkeypatch.setattr(app, "fetch_prospecting_page", _fake_fetcher(pages, calls))
    monkeypatch.setattr(app, "_PAGE_SIZE", 2)

    companies, stats = app.fetch_all_companies(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS)

    assert [c["id"] for c in companies] == ["1", "2", "3"]
    assert calls == [0, 1]
    assert stats == {
        "total_reported": 3, "pages_fetched": 2, "credits_charged": 2,
        "companies_collected": 3,
    }


def test_fetch_all_companies_deduplicates_and_stops_on_no_new_results(monkeypatch):
    # Page 1 repeats page 0's ids entirely (e.g. an index shift) -- must not
    # loop forever, and must not double-count the duplicates.
    pages = {
        0: _page([{"id": "1"}, {"id": "2"}], total=None),
        1: _page([{"id": "1"}, {"id": "2"}], total=None),
    }
    calls = []
    monkeypatch.setattr(app, "fetch_prospecting_page", _fake_fetcher(pages, calls))
    monkeypatch.setattr(app, "_PAGE_SIZE", 2)

    companies, stats = app.fetch_all_companies(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS)

    assert [c["id"] for c in companies] == ["1", "2"]
    assert calls == [0, 1]
    assert stats["companies_collected"] == 2


def test_fetch_all_companies_respects_stop_flag(monkeypatch):
    pages = {
        0: _page([{"id": "1"}], total=100),
        1: _page([{"id": "2"}], total=100),
    }
    calls = []
    monkeypatch.setattr(app, "fetch_prospecting_page", _fake_fetcher(pages, calls))
    monkeypatch.setattr(app, "_PAGE_SIZE", 1)

    companies, stats = app.fetch_all_companies(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS,
        stop_flag=lambda: len(calls) >= 1,
    )

    assert calls == [0]
    assert [c["id"] for c in companies] == ["1"]


def test_fetch_all_companies_calls_progress_callback(monkeypatch):
    pages = {0: _page([{"id": "1"}], total=1)}
    monkeypatch.setattr(app, "fetch_prospecting_page", _fake_fetcher(pages, []))
    monkeypatch.setattr(app, "_PAGE_SIZE", 1)

    seen = []
    app.fetch_all_companies(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS,
        progress_callback=lambda page, collected, total: seen.append((page, collected, total)),
    )

    assert seen == [(0, 1, 1)]


def test_fetch_all_companies_retries_on_rate_limit(monkeypatch):
    calls = []

    def fetch(key, body):
        calls.append(body["pagination"]["page"])
        if len(calls) == 1:
            raise app.RateLimited("0")
        return _page([{"id": "1"}], total=1)

    monkeypatch.setattr(app, "fetch_prospecting_page", fetch)
    monkeypatch.setattr(app, "_PAGE_SIZE", 1)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)

    companies, stats = app.fetch_all_companies(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS)

    assert len(calls) == 2  # one 429, one success, same page retried
    assert [c["id"] for c in companies] == ["1"]


# ---------------------------------------------------------------------------
# fetch_companies_by_sector
# ---------------------------------------------------------------------------

_MAIN_INDUSTRIES = [
    {"main_industry": "Technology, Information & Media", "main_industry_id": 17},
    {"main_industry": "Finance", "main_industry_id": 9},
]


def test_fetch_companies_by_sector_labels_each_result(monkeypatch):
    def fake_fetch_all_companies(key, *, location, size_bands, included_main_industry_ids=None,
                                  excluded_industry_ids=None, progress_callback=None, stop_flag=None):
        industry_id = included_main_industry_ids[0]
        if industry_id == 17:
            return [{"id": "1"}, {"id": "2"}], {"credits_charged": 2}
        return [{"id": "3"}], {"credits_charged": 1}

    monkeypatch.setattr(app, "fetch_all_companies", fake_fetch_all_companies)

    companies, stats = app.fetch_companies_by_sector(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS, main_industries=_MAIN_INDUSTRIES)

    by_id = {c["id"]: c for c in companies}
    assert by_id["1"]["main_industry"] == "Technology, Information & Media"
    assert by_id["1"]["main_industry_id"] == 17
    assert by_id["3"]["main_industry"] == "Finance"
    assert by_id["3"]["main_industry_id"] == 9
    assert stats["credits_charged"] == 3
    assert stats["companies_collected"] == 3
    assert stats["per_industry"] == {17: 2, 9: 1}


def test_fetch_companies_by_sector_dedupes_across_industries(monkeypatch):
    # Same company id returned by two different industry loops (shouldn't
    # normally happen -- Lusha assigns one main industry per company -- but
    # must not be double-counted if it does).
    def fake_fetch_all_companies(key, *, location, size_bands, included_main_industry_ids=None,
                                  excluded_industry_ids=None, progress_callback=None, stop_flag=None):
        return [{"id": "1"}], {"credits_charged": 1}

    monkeypatch.setattr(app, "fetch_all_companies", fake_fetch_all_companies)

    companies, stats = app.fetch_companies_by_sector(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS, main_industries=_MAIN_INDUSTRIES)

    assert len(companies) == 1
    assert stats["companies_collected"] == 1
    # First industry to claim the id keeps it -- second loop's find is a dupe.
    assert stats["per_industry"] == {17: 1, 9: 0}


def test_fetch_companies_by_sector_calls_progress_callback(monkeypatch):
    def fake_fetch_all_companies(key, *, location, size_bands, included_main_industry_ids=None,
                                  excluded_industry_ids=None, progress_callback=None, stop_flag=None):
        return [{"id": str(included_main_industry_ids[0])}], {"credits_charged": 1}

    monkeypatch.setattr(app, "fetch_all_companies", fake_fetch_all_companies)

    seen = []
    app.fetch_companies_by_sector(
        "key", location=_URUGUAY, size_bands=app.SIZE_BANDS, main_industries=_MAIN_INDUSTRIES,
        progress_callback=lambda i, n, label, collected: seen.append((i, n, label, collected)),
    )
    assert seen == [
        (0, 2, "Technology, Information & Media", 1),
        (1, 2, "Finance", 2),
    ]
