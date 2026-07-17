"""Orchestrates getting a Let's Encrypt certificate onto an iLO 3.

Ties together three independent pieces: ``ILoSession.request_csr`` (ask the
iLO for a CSR, polling until it's ready), ``acme`` (RFC 8555 client), and a
DNS-01 challenge. Creating/deleting the ``_acme-challenge`` TXT record is the
caller's job, passed in as two small callbacks -- this module has no
knowledge of any specific DNS provider. ``launcher/renewal.py`` is what
wires those callbacks up against Cloudflare; keeping that dependency out of
here means this package (a standalone iLO 3 protocol library) doesn't reach
"up" into the launcher application that uses it.

CSR generation goes through the same ``/json/*`` HTTP API ``session.py``
already speaks for everything else -- **not** RIBCL. An earlier version of
this module used a hand-rolled RIBCL-over-raw-TLS-socket client, reconstructed
from HPE's scripting-guide documentation; testing against real hardware
proved that transport wrong for this iLO 3 firmware (its port 443 is a plain
HTTP(S) server that 405s anything that isn't a real HTTP request). The actual
request/response shapes here were captured straight from the iLO's own web UI
(``admin_security_sslCertCust.html``) via browser devtools.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import acme
from .session import ILoSession

ProgressCallback = Callable[[str, str], None]

_CERT_BLOCK_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)


def _noop_progress(phase: str, message: str) -> None:
    pass


def _leaf_certificate(fullchain_pem: str) -> str:
    """Return just the first (leaf) certificate from a PEM chain.

    ``AcmeClient.download_certificate`` returns the full leaf+intermediate
    chain, as ACME servers normally do -- but iLO's ``/json/certificate``
    enforces a hard 4096-character limit on ``certificate_data`` (confirmed
    against real hardware: HTTP 400 "Missing '\"' at end of string, or string
    longer than max (4096)." when handed the full chain), so only the leaf
    is submitted.
    """
    match = _CERT_BLOCK_RE.search(fullchain_pem)
    if not match:
        raise ValueError("no certificate found in the downloaded PEM chain")
    return match.group(0) + "\n"


def _poll_for_csr(
    session: ILoSession,
    *,
    common_name: str,
    country: str,
    state: str,
    locality: str,
    organization_name: str,
    organization_unit: str,
    max_wait: float,
    poll_interval: float,
) -> str:
    """Poll ``/json/csr`` until the iLO has generated a CSR, returning its PEM text."""
    deadline = time.monotonic() + max_wait
    while True:
        result = session.request_csr(
            country=country,
            state=state,
            locality=locality,
            organization_name=organization_name,
            organization_unit=organization_unit,
            common_name=common_name,
        )
        csr_pem = result.get("csr_pem")
        if csr_pem:
            return csr_pem
        if result.get("message") != ILoSession.CSR_NOT_AVAILABLE:
            raise RuntimeError(f"unexpected /json/csr response from {session.host}: {result!r}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {max_wait:.0f}s waiting for {session.host} to generate a CSR"
            )
        time.sleep(poll_interval)


def renew_certificate(
    host: str,
    username: str,
    password: str,
    *,
    create_dns_txt_record: Callable[[str, str], Any],
    delete_dns_txt_record: Callable[[Any], None],
    account_key_path: Path,
    country: str = "",
    state: str = "",
    locality: str = "",
    organization_name: str = "",
    organization_unit: str = "",
    contact_email: str | None = None,
    on_progress: ProgressCallback | None = None,
    acme_directory_url: str = acme.DEFAULT_DIRECTORY_URL,
    dns_propagation_wait: float = 30.0,
    csr_max_wait: float = 600.0,
    csr_poll_interval: float = 15.0,
) -> None:
    """Get a Let's Encrypt certificate onto ``host`` via DNS-01, end to end.

    ``create_dns_txt_record(name, value) -> record_handle`` creates the
    ``_acme-challenge`` TXT record; ``delete_dns_txt_record(record_handle)``
    removes it. Every record created is deleted again in a ``finally`` block
    regardless of how the renewal ends up.

    ``on_progress(phase, message)``, if given, is called at each step so a
    caller can surface live status -- phases are ``"login"``, ``"csr"``,
    ``"dns"``, ``"validating"``, ``"finalizing"``, ``"importing"``,
    ``"resetting"``, ``"done"``.
    """
    progress = on_progress or _noop_progress

    progress("login", f"Logging into {host}...")
    session = ILoSession.login(host, username, password)

    progress("csr", "Requesting CSR from iLO (this can take several minutes)...")
    csr_pem = _poll_for_csr(
        session,
        common_name=host,
        country=country,
        state=state,
        locality=locality,
        organization_name=organization_name,
        organization_unit=organization_unit,
        max_wait=csr_max_wait,
        poll_interval=csr_poll_interval,
    )

    account_key = acme.AcmeAccountKey.load_or_create(account_key_path)
    client = acme.AcmeClient(account_key, directory_url=acme_directory_url)
    client.ensure_account(contact_email)
    order_url, order = client.new_order([host])

    record_handles: list[Any] = []
    try:
        for authz_url in order["authorizations"]:
            authz = client.get_authorization(authz_url)
            if authz.get("status") == "valid":
                continue
            challenge_url, record_name, record_value = client.dns01_challenge(authz)

            progress("dns", f"Creating DNS TXT record {record_name}...")
            record_handles.append(create_dns_txt_record(record_name, record_value))

            progress("dns", f"Waiting {dns_propagation_wait:.0f}s for DNS propagation...")
            time.sleep(dns_propagation_wait)

            progress("validating", "Notifying Let's Encrypt and waiting for validation...")
            client.notify_ready(challenge_url)
            client.poll_authorization(authz_url)
    finally:
        for handle in record_handles:
            progress("dns", "Cleaning up DNS TXT record...")
            delete_dns_txt_record(handle)

    progress("finalizing", "Finalizing order and downloading certificate...")
    client.finalize(order, csr_pem)
    order = client.poll_order(order_url)
    cert_pem = client.download_certificate(order)

    progress("importing", "Importing certificate into iLO...")
    # The raw response is logged (session.py, at INFO level) rather than
    # surfaced here -- the failure-case shape is still unconfirmed, so it's
    # not yet meaningful to show the user beyond the phase itself.
    session.import_certificate(_leaf_certificate(cert_pem))

    # Confirmed against real hardware: unlike RIBCL's IMPORT_CERTIFICATE, this
    # endpoint does NOT restart iLO's HTTPS service on its own -- the newly
    # imported cert isn't actually served until an explicit reset.
    progress("resetting", "Resetting iLO to apply the new certificate...")
    session.reset_ilo()

    progress(
        "done",
        "Done -- iLO is rebooting its management processor to apply the new "
        "certificate (~30-60s).",
    )
