"""Minimal ACME v2 (RFC 8555) client, just enough to get a DNS-01-validated
certificate for a CSR someone else generated (here, an iLO's own RIBCL
``CERTIFICATE_SIGNING_REQUEST``).

Hand-rolled on ``cryptography`` + ``urllib`` rather than pulling in the
``acme``/``josepy`` dependency chain, matching how ``session.py`` and
``certcheck.py`` already talk raw HTTPS/TLS themselves instead of using
``requests``. Deliberately has no knowledge of any particular DNS provider --
callers read the ``(record_name, record_value)`` pair from
:meth:`AcmeClient.dns01_challenge` and create/delete that TXT record
themselves (see ``launcher/cloudflare.py`` and ``ilo3console/certrenew.py``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

DEFAULT_DIRECTORY_URL = "https://acme-v02.api.letsencrypt.org/directory"
STAGING_DIRECTORY_URL = "https://acme-staging-v02.api.letsencrypt.org/directory"

_TERMINAL_INVALID_STATUSES = ("invalid", "expired", "revoked", "deactivated")


class AcmeError(Exception):
    """Raised on any ACME protocol/transport failure."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class AcmeAccountKey:
    """The ECDSA (P-256) keypair identifying an ACME account."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self.private_key = private_key

    @classmethod
    def load_or_create(cls, path: Path) -> AcmeAccountKey:
        """Load the account key from ``path``, generating and saving one
        (mode 0600) if it doesn't exist yet."""
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise AcmeError(f"{path} does not contain an EC private key")
            return cls(key)
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pem)
        path.chmod(0o600)
        return cls(key)

    @property
    def jwk(self) -> dict:
        """The account key's public part as a JSON Web Key."""
        numbers = self.private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
        }

    @property
    def thumbprint(self) -> str:
        """RFC 7638 JWK thumbprint, used to build DNS-01 key authorizations."""
        canonical = json.dumps(self.jwk, separators=(",", ":"), sort_keys=True).encode()
        return _b64url(hashlib.sha256(canonical).digest())

    def sign(self, signing_input: bytes) -> bytes:
        """ES256-sign ``signing_input``, returning the raw ``r||s`` JWS
        signature (JWS wants fixed-width raw values, not the DER encoding
        ``cryptography``'s ``sign()`` produces)."""
        der_sig = self.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


class AcmeClient:
    """A logged-in-or-about-to-be session against one ACME directory."""

    def __init__(
        self,
        account_key: AcmeAccountKey,
        *,
        directory_url: str = DEFAULT_DIRECTORY_URL,
        timeout: float = 30.0,
    ) -> None:
        self.account_key = account_key
        self.directory_url = directory_url
        self.timeout = timeout
        self._directory: dict | None = None
        self._nonce: str | None = None
        self._account_url: str | None = None

    # -- transport ----------------------------------------------------------

    def directory(self) -> dict:
        """Fetch (and cache) the ACME server's directory of endpoint URLs."""
        if self._directory is None:
            req = urllib.request.Request(self.directory_url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._capture_nonce(resp)
                self._directory = json.loads(resp.read().decode())
        return self._directory

    def _capture_nonce(self, resp) -> None:
        nonce = resp.headers.get("Replay-Nonce")
        if nonce:
            self._nonce = nonce

    def _fresh_nonce(self) -> str:
        if self._nonce:
            nonce, self._nonce = self._nonce, None
            return nonce
        req = urllib.request.Request(self.directory()["newNonce"], method="HEAD")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            nonce = resp.headers.get("Replay-Nonce")
        if not nonce:
            raise AcmeError("ACME server did not return a Replay-Nonce")
        return nonce

    def _jws(self, url: str, payload: dict | None) -> bytes:
        protected = {"alg": "ES256", "nonce": self._fresh_nonce(), "url": url}
        if self._account_url:
            protected["kid"] = self._account_url
        else:
            protected["jwk"] = self.account_key.jwk
        protected_b64 = _b64url(json.dumps(protected).encode())
        payload_b64 = "" if payload is None else _b64url(json.dumps(payload).encode())
        signing_input = f"{protected_b64}.{payload_b64}".encode()
        signature_b64 = _b64url(self.account_key.sign(signing_input))
        return json.dumps(
            {"protected": protected_b64, "payload": payload_b64, "signature": signature_b64}
        ).encode()

    def _post_raw(self, url: str, payload: dict | None) -> tuple[bytes, object]:
        """POST a JWS-signed request; retries once on a stale-nonce error."""
        for attempt in range(2):
            req = urllib.request.Request(
                url,
                data=self._jws(url, payload),
                headers={"Content-Type": "application/jose+json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._capture_nonce(resp)
                    return resp.read(), resp.headers
            except urllib.error.HTTPError as exc:
                self._capture_nonce(exc)
                raw = exc.read()
                try:
                    err = json.loads(raw)
                except json.JSONDecodeError:
                    err = {"detail": raw.decode("utf-8", "replace")}
                if attempt == 0 and str(err.get("type", "")).endswith("badNonce"):
                    continue
                raise AcmeError(f"ACME request to {url} failed: {err.get('detail', err)}") from exc
        raise AcmeError(f"ACME request to {url} failed after a nonce retry")

    def _post(self, url: str, payload: dict | None) -> tuple[dict, object]:
        raw, headers = self._post_raw(url, payload)
        return (json.loads(raw) if raw else {}), headers

    # -- account/order flow ---------------------------------------------------

    def ensure_account(self, contact_email: str | None = None) -> str:
        """Register (or, given the same account key, re-identify) the ACME
        account. Returns its account URL."""
        payload = {"termsOfServiceAgreed": True}
        if contact_email:
            payload["contact"] = [f"mailto:{contact_email}"]
        _, headers = self._post(self.directory()["newAccount"], payload)
        account_url = headers.get("Location")
        if not account_url:
            raise AcmeError("ACME newAccount response was missing a Location header")
        self._account_url = account_url
        return account_url

    def new_order(self, domains: list[str]) -> tuple[str, dict]:
        """Create an order for ``domains``. Returns ``(order_url, order)``."""
        payload = {"identifiers": [{"type": "dns", "value": d} for d in domains]}
        order, headers = self._post(self.directory()["newOrder"], payload)
        order_url = headers.get("Location")
        if not order_url:
            raise AcmeError("ACME newOrder response was missing a Location header")
        return order_url, order

    def get_authorization(self, authz_url: str) -> dict:
        """POST-as-GET an authorization object."""
        data, _ = self._post(authz_url, None)
        return data

    def dns01_challenge(self, authz: dict) -> tuple[str, str, str]:
        """Pick the ``dns-01`` challenge out of an authorization.

        Returns ``(challenge_url, record_name, record_value)`` -- create a
        TXT record at ``record_name`` with content ``record_value``, then
        call :meth:`notify_ready` with ``challenge_url``.
        """
        for challenge in authz.get("challenges", []):
            if challenge.get("type") == "dns-01":
                key_authorization = f"{challenge['token']}.{self.account_key.thumbprint}"
                record_value = _b64url(hashlib.sha256(key_authorization.encode()).digest())
                record_name = f"_acme-challenge.{authz['identifier']['value']}"
                return challenge["url"], record_name, record_value
        raise AcmeError("authorization has no dns-01 challenge")

    def notify_ready(self, challenge_url: str) -> None:
        """Tell the ACME server the challenge (DNS record) is in place."""
        self._post(challenge_url, {})

    def poll_authorization(
        self, authz_url: str, *, timeout: float = 120.0, interval: float = 3.0
    ) -> dict:
        """Poll an authorization until the CA reports ``valid`` (or raise on
        ``invalid``/timeout)."""
        deadline = time.monotonic() + timeout
        while True:
            authz = self.get_authorization(authz_url)
            status = authz.get("status")
            if status == "valid":
                return authz
            if status in _TERMINAL_INVALID_STATUSES:
                raise AcmeError(f"ACME authorization for {authz.get('identifier')} failed: {authz}")
            if time.monotonic() >= deadline:
                raise AcmeError(f"timed out waiting for ACME authorization {authz_url}")
            time.sleep(interval)

    def finalize(self, order: dict, csr_pem: str) -> None:
        """Submit the (already-signed-elsewhere) CSR to finalize the order."""
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        csr_der_b64 = _b64url(csr.public_bytes(serialization.Encoding.DER))
        self._post(order["finalize"], {"csr": csr_der_b64})

    def poll_order(self, order_url: str, *, timeout: float = 120.0, interval: float = 3.0) -> dict:
        """Poll an order until it's ``valid`` (certificate issued) or raise
        on ``invalid``/timeout."""
        deadline = time.monotonic() + timeout
        while True:
            order, _ = self._post(order_url, None)
            status = order.get("status")
            if status == "valid":
                return order
            if status == "invalid":
                raise AcmeError(f"ACME order failed: {order}")
            if time.monotonic() >= deadline:
                raise AcmeError(f"timed out waiting for ACME order {order_url} to finalize")
            time.sleep(interval)

    def download_certificate(self, order: dict) -> str:
        """Fetch the issued certificate (PEM fullchain) for a ``valid`` order."""
        cert_url = order.get("certificate")
        if not cert_url:
            raise AcmeError("ACME order has no certificate URL")
        raw, _ = self._post_raw(cert_url, None)
        return raw.decode("utf-8")
