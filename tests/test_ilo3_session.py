"""Tests for ``ILoSession.request_csr``/``import_certificate`` -- specifically
that the iLO's "still generating" response (an HTTP 500 whose body is still
normal JSON, confirmed via a real browser capture against live hardware) is
parsed rather than left to raise as an unhandled ``HTTPError``.
"""

import json
import urllib.error
from io import BytesIO

from ilo3console.session import ILoSession


def _session():
    return ILoSession.from_token("h", "sessionkey123")


def test_request_csr_parses_500_not_available_response(monkeypatch):
    body = json.dumps({"message": "JS_CERT_NOT_AVAILABLE", "details": None}).encode()

    def fake_open(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, BytesIO(body))

    session = _session()
    monkeypatch.setattr(session._opener, "open", fake_open)  # noqa: SLF001

    result = session.request_csr(
        country="US",
        state="Texas",
        locality="Houston",
        organization_name="Example Corp",
        organization_unit="IT",
        common_name="ilo.example.com",
    )
    assert result == {"message": "JS_CERT_NOT_AVAILABLE", "details": None}


def test_request_csr_parses_ready_response(monkeypatch):
    # Confirmed against real hardware: once ready, the response has a wholly
    # different shape from the pending one -- no "message"/"details" at all,
    # just the (mostly-null) subject fields plus "csr_pem".
    csr_pem = "-----BEGIN CERTIFICATE REQUEST-----\nMII...\n-----END CERTIFICATE REQUEST-----\n"
    body = json.dumps(
        {
            "country": None,
            "state": None,
            "locality": None,
            "organization_name": None,
            "common_name": None,
            "alternative_name": None,
            "organization_unit": None,
            "csr_pem": csr_pem,
        }
    ).encode()

    class _FakeResponse:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    session = _session()
    monkeypatch.setattr(session._opener, "open", lambda req, timeout=None: _FakeResponse())  # noqa: SLF001, ARG005

    result = session.request_csr(
        country="US",
        state="Texas",
        locality="Houston",
        organization_name="Example Corp",
        organization_unit="IT",
        common_name="ilo.example.com",
    )
    assert result["csr_pem"] == csr_pem


def test_request_csr_sends_expected_payload(monkeypatch):
    captured = {}

    class _FakeResponse:
        def read(self):
            return json.dumps({"message": "JS_CERT_NOT_AVAILABLE", "details": None}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["cookie"] = req.headers.get("Cookie")
        return _FakeResponse()

    session = _session()
    monkeypatch.setattr(session._opener, "open", fake_open)  # noqa: SLF001

    session.request_csr(
        country="US",
        state="Texas",
        locality="Houston",
        organization_name="Example Corp",
        organization_unit="IT",
        common_name="ilo.example.com",
    )
    assert captured["url"].endswith("/json/csr")
    assert captured["body"] == {
        "country": "US",
        "state": "Texas",
        "locality": "Houston",
        "organization_name": "Example Corp",
        "organization_unit": "IT",
        "common_name": "ilo.example.com",
        "method": "create_csr",
        "session_key": "sessionkey123",
    }
    assert captured["cookie"] == "sessionKey=sessionkey123"


def test_import_certificate_sends_expected_payload(monkeypatch):
    captured = {}

    class _FakeResponse:
        def read(self):
            return json.dumps({"message": "ok"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _FakeResponse()

    session = _session()
    monkeypatch.setattr(session._opener, "open", fake_open)  # noqa: SLF001

    cert_pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
    result = session.import_certificate(cert_pem)

    assert captured["url"].endswith("/json/certificate")
    assert captured["body"] == {
        "method": "import_certificate",
        "certificate_data": cert_pem,
        "session_key": "sessionkey123",
    }
    assert result == {"message": "ok"}


def test_import_certificate_parses_error_response_on_non_200(monkeypatch):
    body = json.dumps({"message": "some error"}).encode()

    def fake_open(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, BytesIO(body))

    session = _session()
    monkeypatch.setattr(session._opener, "open", fake_open)  # noqa: SLF001

    result = session.import_certificate("bogus")
    assert result == {"message": "some error"}


def test_import_certificate_treats_empty_200_body_as_success(monkeypatch):
    # Confirmed against real hardware: a successful import returns an empty
    # 200 body, not JSON like every other /json/* endpoint -- json.loads("")
    # would otherwise crash right after the real work already succeeded.
    class _FakeResponse:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    session = _session()
    monkeypatch.setattr(session._opener, "open", lambda req, timeout=None: _FakeResponse())  # noqa: SLF001, ARG005

    cert_pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
    result = session.import_certificate(cert_pem)
    assert result == {"message": "success", "raw": ""}


def test_reset_ilo_sends_expected_payload(monkeypatch):
    captured = {}

    class _FakeResponse:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _FakeResponse()

    session = _session()
    monkeypatch.setattr(session._opener, "open", fake_open)  # noqa: SLF001

    result = session.reset_ilo()

    assert captured["url"].endswith("/json/ilo_status")
    assert captured["body"] == {
        "method": "reset_ilo",
        "cause": "diags",
        "session_key": "sessionkey123",
    }
    assert result == {"message": "success", "raw": ""}


def test_reset_ilo_accepts_a_custom_cause(monkeypatch):
    captured = {}

    class _FakeResponse:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=None):  # noqa: ARG001
        captured["body"] = json.loads(req.data)
        return _FakeResponse()

    session = _session()
    monkeypatch.setattr(session._opener, "open", fake_open)  # noqa: SLF001

    session.reset_ilo(cause="cert_import")
    assert captured["body"]["cause"] == "cert_import"
