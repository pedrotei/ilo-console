"""Tests for the hand-rolled ACME v2 client.

All network I/O goes through ``urllib.request.urlopen``, so tests monkeypatch
that single call site with a small fake responder rather than touching the
network. JWS signing and the DNS-01 key-authorization digest are checked
against RFC 8555's own worked example (section 8.1) so the crypto math is
verified independently of any real ACME server.
"""

import base64
import hashlib
import json
from io import BytesIO

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from ilo3console import acme

# -- RFC 8555 8.1 worked example -----------------------------------------------
# account key thumbprint "abc", token "evaGxfADs6pSRb2LAv9IZf17Dt3juxGJ-PCt92wr-oA"
# -> key authorization "evaGxfADs6pSRb2LAv9IZf17Dt3juxGJ-PCt92wr-oA.abc"
_RFC_TOKEN = "evaGxfADs6pSRb2LAv9IZf17Dt3juxGJ-PCt92wr-oA"
_RFC_THUMBPRINT = "abc"


class _FakeAccountKey:
    thumbprint = _RFC_THUMBPRINT


def test_dns01_challenge_computes_rfc8555_key_authorization_digest():
    client = acme.AcmeClient.__new__(acme.AcmeClient)
    client.account_key = _FakeAccountKey()
    authz = {
        "identifier": {"type": "dns", "value": "example.org"},
        "challenges": [{"type": "dns-01", "url": "https://x/chall/1", "token": _RFC_TOKEN}],
    }
    challenge_url, record_name, record_value = client.dns01_challenge(authz)
    assert challenge_url == "https://x/chall/1"
    assert record_name == "_acme-challenge.example.org"
    expected_key_auth = f"{_RFC_TOKEN}.{_RFC_THUMBPRINT}"
    expected_digest = base64.urlsafe_b64encode(
        hashlib.sha256(expected_key_auth.encode()).digest()
    ).rstrip(b"=").decode()
    assert record_value == expected_digest


def test_dns01_challenge_raises_without_a_dns01_entry():
    client = acme.AcmeClient.__new__(acme.AcmeClient)
    client.account_key = _FakeAccountKey()
    authz = {"identifier": {"type": "dns", "value": "x"}, "challenges": [{"type": "http-01"}]}
    with pytest.raises(acme.AcmeError, match="dns-01"):
        client.dns01_challenge(authz)


# -- account key / JWS signing -------------------------------------------------


def test_account_key_load_or_create_persists_and_reloads(tmp_path):
    path = tmp_path / "account.pem"
    key1 = acme.AcmeAccountKey.load_or_create(path)
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    key2 = acme.AcmeAccountKey.load_or_create(path)
    assert key1.jwk == key2.jwk


def test_account_key_thumbprint_is_stable_for_same_key(tmp_path):
    key = acme.AcmeAccountKey.load_or_create(tmp_path / "account.pem")
    assert key.thumbprint == key.thumbprint
    assert isinstance(key.thumbprint, str) and key.thumbprint


def test_sign_produces_a_verifiable_es256_signature(tmp_path):
    key = acme.AcmeAccountKey.load_or_create(tmp_path / "account.pem")
    signing_input = b"protected.payload"
    raw_sig = key.sign(signing_input)
    assert len(raw_sig) == 64
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    der_sig = encode_dss_signature(r, s)
    key.private_key.public_key().verify(der_sig, signing_input, ec.ECDSA(hashes.SHA256()))


# -- HTTP transport (fake urlopen) ---------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict, status: int = 200):
        self._body = body
        self.headers = headers
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeUrlopen:
    """Records requests and replays canned responses keyed by URL."""

    def __init__(self, responses: dict[str, _FakeResponse]):
        self.responses = responses
        self.requests = []

    def __call__(self, req, timeout=None):  # noqa: ARG002
        self.requests.append(req)
        return self.responses[req.full_url]


def _account_key(tmp_path):
    return acme.AcmeAccountKey.load_or_create(tmp_path / "account.pem")


def test_directory_fetches_and_caches(tmp_path, monkeypatch):
    directory = {"newNonce": "https://x/nonce", "newAccount": "https://x/acct"}
    fake = _FakeUrlopen(
        {"https://x/dir": _FakeResponse(json.dumps(directory).encode(), {"Replay-Nonce": "n1"})}
    )
    monkeypatch.setattr(acme.urllib.request, "urlopen", fake)
    client = acme.AcmeClient(_account_key(tmp_path), directory_url="https://x/dir")
    assert client.directory() == directory
    assert client.directory() == directory  # cached, no second request
    assert len(fake.requests) == 1


def test_ensure_account_signs_request_and_captures_location(tmp_path, monkeypatch):
    directory = {"newNonce": "https://x/nonce", "newAccount": "https://x/acct"}
    responses = {
        "https://x/dir": _FakeResponse(json.dumps(directory).encode(), {"Replay-Nonce": "n0"}),
        "https://x/nonce": _FakeResponse(b"", {"Replay-Nonce": "n1"}),
        "https://x/acct": _FakeResponse(
            json.dumps({"status": "valid"}).encode(),
            {"Replay-Nonce": "n2", "Location": "https://x/acct/123"},
        ),
    }
    fake = _FakeUrlopen(responses)
    monkeypatch.setattr(acme.urllib.request, "urlopen", fake)
    client = acme.AcmeClient(_account_key(tmp_path), directory_url="https://x/dir")

    account_url = client.ensure_account("me@example.org")

    assert account_url == "https://x/acct/123"
    assert client._account_url == "https://x/acct/123"  # noqa: SLF001
    post_req = fake.requests[-1]
    body = json.loads(post_req.data)
    protected = json.loads(base64.urlsafe_b64decode(body["protected"] + "=="))
    assert protected["url"] == "https://x/acct"
    # The directory fetch's own Replay-Nonce ("n0") is reused opportunistically,
    # so no separate newNonce round-trip happens here.
    assert protected["nonce"] == "n0"
    assert "jwk" in protected  # no account yet -> identify by key, not kid
    payload = json.loads(base64.urlsafe_b64decode(body["payload"] + "=="))
    assert payload["contact"] == ["mailto:me@example.org"]


def test_post_retries_once_on_bad_nonce(tmp_path, monkeypatch):
    import urllib.error

    directory = {"newNonce": "https://x/nonce", "newOrder": "https://x/order"}
    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        if req.full_url == "https://x/dir":
            return _FakeResponse(json.dumps(directory).encode(), {"Replay-Nonce": "n0"})
        if req.full_url == "https://x/nonce":
            return _FakeResponse(b"", {"Replay-Nonce": f"n{call_count['n'] + 1}"})
        if req.full_url == "https://x/order":
            call_count["n"] += 1
            if call_count["n"] == 1:
                body = json.dumps({"type": "urn:ietf:params:acme:error:badNonce"}).encode()
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", {"Replay-Nonce": "n2"}, BytesIO(body)
                )
            return _FakeResponse(
                json.dumps({"status": "pending"}).encode(),
                {"Replay-Nonce": "n3", "Location": "https://x/order/1"},
            )
        raise AssertionError(f"unexpected URL {req.full_url}")

    monkeypatch.setattr(acme.urllib.request, "urlopen", fake_urlopen)
    client = acme.AcmeClient(_account_key(tmp_path), directory_url="https://x/dir")
    client._account_url = "https://x/acct/123"  # noqa: SLF001

    order_url, order = client.new_order(["example.org"])
    assert order_url == "https://x/order/1"
    assert order["status"] == "pending"
    assert call_count["n"] == 2


def test_download_certificate_returns_raw_pem_not_json(tmp_path, monkeypatch):
    directory = {"newNonce": "https://x/nonce"}
    pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
    responses = {
        "https://x/dir": _FakeResponse(json.dumps(directory).encode(), {"Replay-Nonce": "n0"}),
        "https://x/nonce": _FakeResponse(b"", {"Replay-Nonce": "n1"}),
        "https://x/cert": _FakeResponse(pem.encode(), {"Replay-Nonce": "n2"}),
    }
    fake = _FakeUrlopen(responses)
    monkeypatch.setattr(acme.urllib.request, "urlopen", fake)
    client = acme.AcmeClient(_account_key(tmp_path), directory_url="https://x/dir")
    client._account_url = "https://x/acct/123"  # noqa: SLF001

    assert client.download_certificate({"certificate": "https://x/cert"}) == pem


def test_download_certificate_raises_without_a_certificate_url(tmp_path):
    client = acme.AcmeClient(_account_key(tmp_path))
    with pytest.raises(acme.AcmeError, match="no certificate URL"):
        client.download_certificate({})
