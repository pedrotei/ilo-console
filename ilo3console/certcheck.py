"""TLS certificate inspection for iLO 3's HTTPS listener.

A lightweight, read-only check -- opens the same legacy-TLS handshake
``session.py`` uses for the real API session, but only to inspect the
server's certificate (expiry, issuer, self-signed-ness). No login, no
session token minted.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timezone

from .session import legacy_tls_context


@dataclass
class CertInfo:
    """A snapshot of an iLO 3's current HTTPS certificate."""

    subject: str
    issuer: str
    not_before: datetime
    not_after: datetime

    @property
    def is_self_signed(self) -> bool:
        """True if the certificate's issuer and subject are identical."""
        return self.subject == self.issuer

    @property
    def days_remaining(self) -> int:
        """Whole days until expiry (negative if already expired)."""
        return (self.not_after - datetime.now(timezone.utc)).days

    @property
    def expired(self) -> bool:
        """True if the certificate's expiry date has already passed."""
        return self.days_remaining < 0

    @property
    def not_yet_valid(self) -> bool:
        """True if the certificate's start date is still in the future."""
        return datetime.now(timezone.utc) < self.not_before

    @property
    def invalid(self) -> bool:
        """True if the certificate is expired or not yet in its validity window."""
        return self.expired or self.not_yet_valid


def fetch_certificate(host: str, port: int = 443, *, timeout: float = 5.0) -> CertInfo:
    """Connect to ``host``:``port`` and inspect the server's TLS certificate.

    Raises ``OSError``/``ssl.SSLError`` on connection or handshake failure,
    and ``ImportError`` if the optional ``cryptography`` package (the
    ``ilo3`` extra) isn't installed -- there's no pure-stdlib way to parse
    certificate fields out of the raw DER bytes.
    """
    from cryptography import x509  # pylint:disable=import-outside-toplevel

    ctx = legacy_tls_context()
    with (
        socket.create_connection((host, port), timeout=timeout) as raw,
        ctx.wrap_socket(raw, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    return CertInfo(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        not_before=cert.not_valid_before.replace(tzinfo=timezone.utc),
        not_after=cert.not_valid_after.replace(tzinfo=timezone.utc),
    )
