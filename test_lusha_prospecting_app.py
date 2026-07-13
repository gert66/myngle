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
