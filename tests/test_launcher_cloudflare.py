"""Tests for the minimal Cloudflare DNS client (zone lookup + TXT record
create/delete), used to satisfy ACME's DNS-01 challenge. All I/O goes
through ``urllib.request.urlopen``, monkeypatched here rather than hitting
the real API.
"""

import json
from io import BytesIO

import pytest

from launcher import cloudflare


class _FakeResponse:
    def __init__(self, body: dict, status: int = 200):
        self._body = json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cf-token-123", "cf-token-123"),
        ("  cf-token-123  ", "cf-token-123"),
        ("cf-token-123\n", "cf-token-123"),
        ('"cf-token-123"', "cf-token-123"),
        ("'cf-token-123'", "cf-token-123"),
        ("Bearer cf-token-123", "cf-token-123"),
        ("bearer cf-token-123", "cf-token-123"),
        ("Bearer  cf-token-123  ", "cf-token-123"),
    ],
)
def test_clean_token_strips_common_paste_artifacts(raw, expected):
    assert cloudflare._clean_token(raw) == expected  # noqa: SLF001


def test_find_zone_id_matches_exact_hostname_first(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        calls.append(req.full_url)
        return _FakeResponse({"success": True, "result": [{"id": "zone123"}]})

    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", fake_urlopen)
    zone_id = cloudflare.find_zone_id("tok", "hyper1-ilo.internal.example.com")
    assert zone_id == "zone123"
    assert len(calls) == 1
    assert "hyper1-ilo.internal.example.com" in calls[0]


def test_find_zone_id_walks_parent_domains(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        calls.append(req.full_url)
        # Only the registered zone (2 labels) has a match.
        if calls[-1].endswith("name=example.com"):
            return _FakeResponse({"success": True, "result": [{"id": "zone456"}]})
        return _FakeResponse({"success": True, "result": []})

    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", fake_urlopen)
    zone_id = cloudflare.find_zone_id("tok", "hyper1-ilo.internal.example.com")
    assert zone_id == "zone456"
    assert len(calls) == 3  # full host, internal.example.com, example.com


def test_find_zone_id_raises_when_no_zone_matches(monkeypatch):
    monkeypatch.setattr(
        cloudflare.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse({"success": True, "result": []}),  # noqa: ARG005
    )
    with pytest.raises(cloudflare.CloudflareError, match="no Cloudflare zone found"):
        cloudflare.find_zone_id("tok", "host.example.com")


def test_create_txt_record_returns_record_id(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data)
        return _FakeResponse({"success": True, "result": {"id": "rec789"}})

    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", fake_urlopen)
    record_id = cloudflare.create_txt_record("tok", "zone123", "_acme-challenge.host", "value123")
    assert record_id == "rec789"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/zones/zone123/dns_records")
    assert captured["body"] == {
        "type": "TXT",
        "name": "_acme-challenge.host",
        "content": "value123",
        "ttl": 120,
    }


def test_delete_txt_record_sends_delete(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse({"success": True, "result": {}})

    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", fake_urlopen)
    cloudflare.delete_txt_record("tok", "zone123", "rec789")
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/zones/zone123/dns_records/rec789")


def test_request_raises_on_api_level_failure(monkeypatch):
    monkeypatch.setattr(
        cloudflare.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(  # noqa: ARG005
            {"success": False, "errors": [{"message": "Invalid token"}]}
        ),
    )
    with pytest.raises(cloudflare.CloudflareError, match="Invalid token"):
        cloudflare.create_txt_record("badtok", "zone123", "name", "value")


def test_request_raises_on_http_error(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        body = json.dumps({"success": False, "errors": [{"message": "not found"}]}).encode()
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, BytesIO(body))

    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(cloudflare.CloudflareError, match="not found"):
        cloudflare.delete_txt_record("tok", "zone123", "rec789")
