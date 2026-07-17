"""Tests for the certificate-renewal orchestrator.

``ILoSession`` and ``acme`` are fully mocked here -- this only checks that
``certrenew.renew_certificate`` calls its collaborators in the right order
and always cleans up the DNS TXT record it created, even when a later phase
blows up.
"""

import pytest

from ilo3console import certrenew


class _FakeIloSession:
    """Stands in for ILoSession: two ``request_csr`` calls before "ready",
    matching the real iLO's pending-then-ready behavior."""

    CSR_NOT_AVAILABLE = "JS_CERT_NOT_AVAILABLE"

    def __init__(self, host):
        self.host = host
        self.csr_calls = 0
        self.imported_certificate = None
        self.reset_calls = 0

    @classmethod
    def login(cls, host, username, password):  # noqa: ARG003
        return cls(host)

    def request_csr(self, **kwargs):  # noqa: ARG002
        self.csr_calls += 1
        if self.csr_calls < 2:
            return {"message": "JS_CERT_NOT_AVAILABLE", "details": None}
        return {"csr_pem": "CSR_PEM", "country": None}

    def import_certificate(self, certificate_pem):
        self.imported_certificate = certificate_pem
        return {"message": "ok"}

    def reset_ilo(self, *, cause="diags"):  # noqa: ARG002
        self.reset_calls += 1
        return {"message": "success", "raw": ""}


class _FakeAcmeClient:
    def __init__(self, account_key, *, directory_url, timeout=30.0):  # noqa: ARG002
        self.calls = []
        self.authz_status = "pending"
        self.raise_on_poll_authorization = False

    def ensure_account(self, contact_email):
        self.calls.append(("ensure_account", contact_email))

    def new_order(self, domains):
        self.calls.append(("new_order", domains))
        return "https://x/order/1", {"authorizations": ["https://x/authz/1"]}

    def get_authorization(self, authz_url):
        self.calls.append(("get_authorization", authz_url))
        return {
            "status": self.authz_status,
            "identifier": {"value": "hyper1-ilo.example.com"},
            "challenges": [{"type": "dns-01", "url": "https://x/chall/1", "token": "tok"}],
        }

    def dns01_challenge(self, authz):
        self.calls.append(("dns01_challenge", authz["identifier"]["value"]))
        return "https://x/chall/1", "_acme-challenge.hyper1-ilo.example.com", "recvalue"

    def notify_ready(self, challenge_url):
        self.calls.append(("notify_ready", challenge_url))

    def poll_authorization(self, authz_url):
        self.calls.append(("poll_authorization", authz_url))
        if self.raise_on_poll_authorization:
            raise certrenew.acme.AcmeError("validation failed")

    def finalize(self, order, csr_pem):
        self.calls.append(("finalize", csr_pem))

    def poll_order(self, order_url):
        self.calls.append(("poll_order", order_url))
        return {"certificate": "https://x/cert"}

    def download_certificate(self, order):
        self.calls.append(("download_certificate",))
        # Real ACME servers return leaf + intermediate; only the leaf should
        # make it to ILoSession.import_certificate (see _leaf_certificate).
        return (
            "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
            "-----BEGIN CERTIFICATE-----\nintermediate\n-----END CERTIFICATE-----\n"
        )


@pytest.fixture
def fake_collaborators(monkeypatch, tmp_path):
    created_sessions: list[_FakeIloSession] = []

    class _TrackedFakeIloSession(_FakeIloSession):
        @classmethod
        def login(cls, host, username, password):  # noqa: ARG003
            session = cls(host)
            created_sessions.append(session)
            return session

    monkeypatch.setattr(certrenew, "ILoSession", _TrackedFakeIloSession)
    monkeypatch.setattr(
        certrenew.acme.AcmeAccountKey, "load_or_create", staticmethod(lambda path: object())
    )

    fake_client_holder: dict = {"sessions": created_sessions}

    def fake_client_factory(account_key, *, directory_url, timeout=30.0):  # noqa: ARG001
        client = _FakeAcmeClient(account_key, directory_url=directory_url)
        fake_client_holder["client"] = client
        return client

    monkeypatch.setattr(certrenew.acme, "AcmeClient", fake_client_factory)
    return fake_client_holder


def test_renew_certificate_completes_and_imports_the_certificate(fake_collaborators, tmp_path):
    holder = fake_collaborators
    dns_events = []

    def create_dns(name, value):
        dns_events.append(("create", name, value))
        return "record-handle"

    def delete_dns(handle):
        dns_events.append(("delete", handle))

    progress_calls = []
    certrenew.renew_certificate(
        "hyper1-ilo.example.com",
        "admin",
        "secret",
        create_dns_txt_record=create_dns,
        delete_dns_txt_record=delete_dns,
        account_key_path=tmp_path / "account.pem",
        on_progress=lambda phase, message: progress_calls.append(phase),
        dns_propagation_wait=0,
        csr_poll_interval=0,
    )

    client = holder["client"]
    call_names = [c[0] for c in client.calls]
    assert call_names == [
        "ensure_account",
        "new_order",
        "get_authorization",
        "dns01_challenge",
        "notify_ready",
        "poll_authorization",
        "finalize",
        "poll_order",
        "download_certificate",
    ]
    assert dns_events == [
        ("create", "_acme-challenge.hyper1-ilo.example.com", "recvalue"),
        ("delete", "record-handle"),
    ]
    assert progress_calls[0] == "login"
    assert "csr" in progress_calls
    assert "resetting" in progress_calls
    assert progress_calls[-1] == "done"

    session = holder["sessions"][0]
    # Only the leaf certificate should have been submitted, not the intermediate.
    expected_leaf = "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
    assert session.imported_certificate == expected_leaf
    assert session.reset_calls == 1


def test_renew_certificate_skips_already_valid_authorization(
    fake_collaborators, tmp_path, monkeypatch
):
    holder = fake_collaborators
    dns_events = []

    original_factory = certrenew.acme.AcmeClient

    def factory(account_key, *, directory_url, timeout=30.0):
        client = original_factory(account_key, directory_url=directory_url)
        client.authz_status = "valid"
        return client

    monkeypatch.setattr(certrenew.acme, "AcmeClient", factory)

    certrenew.renew_certificate(
        "hyper1-ilo.example.com",
        "admin",
        "secret",
        create_dns_txt_record=lambda n, v: dns_events.append(("create", n, v)) or "h",
        delete_dns_txt_record=lambda h: dns_events.append(("delete", h)),
        account_key_path=tmp_path / "account.pem",
        dns_propagation_wait=0,
        csr_poll_interval=0,
    )

    assert dns_events == []  # already valid -> no DNS record needed at all
    assert holder["client"].calls  # order flow still ran
    assert holder["sessions"][0].imported_certificate is not None


def test_renew_certificate_deletes_dns_record_even_if_validation_fails(
    fake_collaborators, tmp_path, monkeypatch
):
    dns_events = []

    original_factory = certrenew.acme.AcmeClient

    def factory(account_key, *, directory_url, timeout=30.0):
        client = original_factory(account_key, directory_url=directory_url)
        client.raise_on_poll_authorization = True
        return client

    monkeypatch.setattr(certrenew.acme, "AcmeClient", factory)

    with pytest.raises(certrenew.acme.AcmeError, match="validation failed"):
        certrenew.renew_certificate(
            "hyper1-ilo.example.com",
            "admin",
            "secret",
            create_dns_txt_record=lambda n, v: dns_events.append(("create", n, v)) or "h",
            delete_dns_txt_record=lambda h: dns_events.append(("delete", h)),
            account_key_path=tmp_path / "account.pem",
            dns_propagation_wait=0,
            csr_poll_interval=0,
        )

    assert ("delete", "h") in dns_events


def test_poll_for_csr_retries_until_ready(monkeypatch, tmp_path):  # noqa: ARG001
    session = _FakeIloSession("hyper1-ilo.example.com")
    csr_pem = certrenew._poll_for_csr(  # noqa: SLF001
        session,
        common_name="hyper1-ilo.example.com",
        country="",
        state="",
        locality="",
        organization_name="",
        organization_unit="",
        max_wait=60,
        poll_interval=0,
    )
    assert csr_pem == "CSR_PEM"
    assert session.csr_calls == 2


def test_poll_for_csr_times_out(monkeypatch):
    class _NeverReady(_FakeIloSession):
        def request_csr(self, **kwargs):  # noqa: ARG002
            return {"message": "JS_CERT_NOT_AVAILABLE", "details": None}

    with pytest.raises(TimeoutError, match="timed out"):
        certrenew._poll_for_csr(  # noqa: SLF001
            _NeverReady("h"),
            common_name="h",
            country="",
            state="",
            locality="",
            organization_name="",
            organization_unit="",
            max_wait=0,
            poll_interval=0,
        )


def test_poll_for_csr_raises_on_unexpected_response():
    class _Weird(_FakeIloSession):
        def request_csr(self, **kwargs):  # noqa: ARG002
            return {"unexpected": "shape"}

    with pytest.raises(RuntimeError, match="unexpected /json/csr response"):
        certrenew._poll_for_csr(  # noqa: SLF001
            _Weird("h"),
            common_name="h",
            country="",
            state="",
            locality="",
            organization_name="",
            organization_unit="",
            max_wait=60,
            poll_interval=0,
        )


def test_leaf_certificate_extracts_only_the_first_block():
    fullchain = (
        "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
        "-----BEGIN CERTIFICATE-----\nintermediate\n-----END CERTIFICATE-----\n"
    )
    assert (
        certrenew._leaf_certificate(fullchain)  # noqa: SLF001
        == "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
    )


def test_leaf_certificate_handles_a_single_certificate():
    single = "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
    assert certrenew._leaf_certificate(single) == single  # noqa: SLF001


def test_leaf_certificate_raises_when_no_certificate_present():
    with pytest.raises(ValueError, match="no certificate found"):
        certrenew._leaf_certificate("not a certificate")  # noqa: SLF001
