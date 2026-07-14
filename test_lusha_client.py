"""Tests for lusha_client.py's contact-reveal additions: availability
flags parsed from canReveal, and the reveal_contact_details() call to
POST /v3/contacts/enrich. No real Lusha calls -- requests.post is
monkeypatched with an in-memory fake response."""

import pytest

import lusha_client as lc


# ---------------------------------------------------------------------------
# _extract_reveal_availability
# ---------------------------------------------------------------------------

def test_reveal_availability_both_fields_present():
    raw = {"canReveal": [{"field": "emails", "credits": 1}, {"field": "phones", "credits": 5}]}
    assert lc._extract_reveal_availability(raw) == {"emailAvailable": True, "phoneAvailable": True}


def test_reveal_availability_only_email():
    raw = {"canReveal": [{"field": "emails", "credits": 1}]}
    assert lc._extract_reveal_availability(raw) == {"emailAvailable": True, "phoneAvailable": False}


def test_reveal_availability_neither_present():
    raw = {"canReveal": []}
    assert lc._extract_reveal_availability(raw) == {"emailAvailable": False, "phoneAvailable": False}


def test_reveal_availability_missing_can_reveal_key():
    assert lc._extract_reveal_availability({}) == {"emailAvailable": False, "phoneAvailable": False}


# ---------------------------------------------------------------------------
# _normalise_contact -- new contactId / availability fields
# ---------------------------------------------------------------------------

def test_normalise_contact_exposes_public_contact_id():
    raw = {
        "id": "v1.abc123",
        "firstName": "Jan", "lastName": "de Vries",
        "canReveal": [{"field": "phones", "credits": 5}],
    }
    c = lc._normalise_contact(raw)
    assert c["contactId"] == "v1.abc123"
    assert c["_lushaId"] == "v1.abc123"
    assert c["phoneAvailable"] is True
    assert c["emailAvailable"] is False


# ---------------------------------------------------------------------------
# reveal_contact_details
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(response=self)
            raise err

    def json(self):
        return self._payload


def test_reveal_contact_details_empty_ids_returns_empty_without_calling_api(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not call the API for an empty id list")
    monkeypatch.setattr(lc.requests, "post", boom)
    assert lc.reveal_contact_details([]) == {}


def test_reveal_contact_details_rejects_more_than_100_ids(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not call the API when the id cap is exceeded")
    monkeypatch.setattr(lc.requests, "post", boom)
    with pytest.raises(ValueError):
        lc.reveal_contact_details([f"v1.{i}" for i in range(101)])


def test_reveal_contact_details_parses_email_and_phone(monkeypatch):
    monkeypatch.setenv("LUSHA_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({
            "requestId": "r1",
            "results": [{
                "id": "v1.abc123",
                "emails": [{"email": "jan@example.com", "type": "work"}],
                "phones": [{"number": "+31 6 1234 5678", "type": "mobile"}],
            }],
        })

    monkeypatch.setattr(lc.requests, "post", fake_post)
    result = lc.reveal_contact_details(["v1.abc123"])

    assert result == {"v1.abc123": {"email": "jan@example.com", "phone": "+31 6 1234 5678"}}
    assert captured["json"] == {"ids": ["v1.abc123"]}
    assert captured["url"].endswith("/v3/contacts/enrich")


def test_reveal_contact_details_omits_ids_with_nothing_revealed(monkeypatch):
    monkeypatch.setenv("LUSHA_API_KEY", "test-key")

    def fake_post(url, headers, json, timeout):
        return _FakeResponse({"results": [{"id": "v1.xyz"}]})  # no emails/phones keys at all

    monkeypatch.setattr(lc.requests, "post", fake_post)
    result = lc.reveal_contact_details(["v1.xyz"])
    assert result == {"v1.xyz": {"email": "", "phone": ""}}


def test_reveal_contact_details_maps_http_error_to_safe_message(monkeypatch):
    monkeypatch.setenv("LUSHA_API_KEY", "test-key")

    def fake_post(url, headers, json, timeout):
        return _FakeResponse({"message": "raw internal detail"}, status_code=402)

    monkeypatch.setattr(lc.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="Insufficient Lusha credits"):
        lc.reveal_contact_details(["v1.abc123"])
