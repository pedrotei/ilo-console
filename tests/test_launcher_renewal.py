"""Tests for the certificate-renewal job tracker (launcher/renewal.py).

``certrenew.renew_certificate`` (the actual RIBCL/ACME/DNS work) and the
background thread are both mocked/synchronous here -- this only checks job
lifecycle bookkeeping: validation before starting, phase/status updates
during the run, and the terminal done/error state.
"""

import pytest

from ilo3console import certrenew
from launcher import certmonitor, cloudflare, renewal, store


class _FakeKeyringErrors:
    class PasswordDeleteError(Exception):
        pass


class _FakeKeyring:
    errors = _FakeKeyringErrors

    def __init__(self):
        self._data: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self._data[(service, username)] = password

    def get_password(self, service, username):
        return self._data.get((service, username))

    def delete_password(self, service, username):
        try:
            del self._data[(service, username)]
        except KeyError as exc:
            raise self.errors.PasswordDeleteError() from exc


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(store, "keyring", fake)


@pytest.fixture(autouse=True)
def _sync_thread(monkeypatch):
    """Run job threads inline so tests don't need to wait/poll for them."""

    class _SyncThread:
        def __init__(self, target, args=(), daemon=False):  # noqa: ARG002
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(renewal.threading, "Thread", _SyncThread)


@pytest.fixture(autouse=True)
def _no_real_dns_or_cert_recheck(monkeypatch):
    monkeypatch.setattr(cloudflare, "find_zone_id", lambda token, host: "zone-1")  # noqa: ARG005
    monkeypatch.setattr(certmonitor, "check_now", lambda server_id: None)  # noqa: ARG005


def _make_ilo3_server(*, with_password=True, with_token=True):
    server = store.add_server(
        {
            "name": "lab",
            "host": "hyper1-ilo.example.com",
            "generation": "ilo3",
            "username": "admin",
            "save_password": with_password,
            "password": "hunter2" if with_password else "",
        }
    )
    if with_token:
        store.set_cloudflare_token("cf-token")
    return server


def test_start_renewal_raises_for_unknown_server():
    with pytest.raises(ValueError, match="not found"):
        renewal.start_renewal("nope")


def test_start_renewal_raises_for_non_ilo3_server():
    server = store.add_server({"name": "x", "host": "h", "generation": "ilo100"})
    with pytest.raises(ValueError, match="iLO 3"):
        renewal.start_renewal(server["id"])


def test_start_renewal_raises_without_password():
    server = _make_ilo3_server(with_password=False)
    with pytest.raises(ValueError, match="password"):
        renewal.start_renewal(server["id"])


def test_start_renewal_raises_without_cloudflare_token():
    server = _make_ilo3_server(with_token=False)
    with pytest.raises(ValueError, match="Cloudflare"):
        renewal.start_renewal(server["id"])


def test_start_renewal_runs_job_to_completion(monkeypatch):
    server = _make_ilo3_server()

    def fake_renew(host, username, password, **kwargs):
        assert host == "hyper1-ilo.example.com"
        assert username == "admin"
        assert password == "hunter2"
        kwargs["on_progress"]("csr", "Requesting CSR...")
        kwargs["on_progress"]("done", "Done.")

    monkeypatch.setattr(certrenew, "renew_certificate", fake_renew)

    job_id = renewal.start_renewal(server["id"])
    status = renewal.get_job_status(job_id)
    assert status["done"] is True
    assert status["error"] is None
    assert status["phase"] == "done"


def test_start_renewal_records_error_on_failure(monkeypatch):
    server = _make_ilo3_server()

    def fake_renew(*a, **kw):  # noqa: ARG001
        raise RuntimeError("iLO refused the CSR request")

    monkeypatch.setattr(certrenew, "renew_certificate", fake_renew)

    job_id = renewal.start_renewal(server["id"])
    status = renewal.get_job_status(job_id)
    assert status["done"] is True
    assert "iLO refused the CSR request" in status["error"]


def test_start_renewal_uses_password_override_not_saved_one(monkeypatch):
    server = _make_ilo3_server()
    seen = {}

    def fake_renew(host, username, password, **kwargs):  # noqa: ARG001
        seen["password"] = password
        kwargs["on_progress"]("done", "Done.")

    monkeypatch.setattr(certrenew, "renew_certificate", fake_renew)
    renewal.start_renewal(server["id"], password="override-pw")
    assert seen["password"] == "override-pw"


def test_get_job_status_returns_none_for_unknown_job():
    assert renewal.get_job_status("does-not-exist") is None
