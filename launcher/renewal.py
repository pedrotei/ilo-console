"""Background certificate-renewal jobs for saved iLO 3 servers.

Renewing a certificate (RIBCL CSR generation can alone take several minutes,
plus DNS propagation and ACME validation) is far too slow for a single
request/response call from the UI, so this runs it in a daemon thread and
exposes a small polled job-status API, the same shape as
``certmonitor.py``'s ``_lock``/``_statuses`` -- except these are one-shot
jobs instead of a periodic loop.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from ilo3console import certrenew

from . import cloudflare, store

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def get_job_status(job_id: str) -> dict[str, Any] | None:
    """Return the latest status for a renewal job, or None if unknown."""
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _set_status(job_id: str, **fields: Any) -> None:
    with _lock:
        _jobs[job_id].update(fields)


def _make_dns_callbacks(cf_token: str, host: str) -> tuple[Any, Any]:
    zone_id = cloudflare.find_zone_id(cf_token, host)

    def create(name: str, value: str) -> str:
        return cloudflare.create_txt_record(cf_token, zone_id, name, value)

    def delete(record_id: str) -> None:
        cloudflare.delete_txt_record(cf_token, zone_id, record_id)

    return create, delete


def _run_job(job_id: str, server_id: str, host: str, username: str, password: str) -> None:
    from . import certmonitor  # pylint:disable=import-outside-toplevel,cyclic-import

    try:
        cf_token = store.get_cloudflare_token()
        if not cf_token:
            raise RuntimeError("no Cloudflare API token saved -- add one in Settings")
        create_dns, delete_dns = _make_dns_callbacks(cf_token, host)
        acme_settings = store.get_acme_settings()

        def on_progress(phase: str, message: str) -> None:
            _set_status(job_id, phase=phase, message=message)

        certrenew.renew_certificate(
            host,
            username,
            password,
            create_dns_txt_record=create_dns,
            delete_dns_txt_record=delete_dns,
            account_key_path=store.acme_account_key_path(),
            country=acme_settings.get("country", ""),
            state=acme_settings.get("state", ""),
            locality=acme_settings.get("locality", ""),
            organization_name=acme_settings.get("organization_name", ""),
            organization_unit=acme_settings.get("organization_unit", ""),
            contact_email=acme_settings.get("contact_email") or None,
            on_progress=on_progress,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _set_status(job_id, error=str(exc), done=True)
        return
    finally:
        certmonitor.check_now(server_id)
    _set_status(job_id, done=True)


def start_renewal(server_id: str, password: str | None = None) -> str:
    """Kick off a renewal job for a saved server; returns its job id.

    Raises ``ValueError`` if the server isn't found/isn't an iLO 3, or if
    there's no password to log into it with (either passed in or already
    saved in the OS keyring).
    """
    server = store.get_server(server_id)
    if server is None:
        raise ValueError("server not found")
    if server.get("generation") != "ilo3":
        raise ValueError("certificate renewal is only supported for iLO 3")
    password = password or store.get_password(server_id)
    if not password:
        raise ValueError("no password available -- enter one or save it for this server")
    if not store.get_cloudflare_token():
        raise ValueError("no Cloudflare API token saved -- add one in Settings")

    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "phase": "starting",
            "message": "Starting...",
            "done": False,
            "error": None,
        }
    threading.Thread(
        target=_run_job,
        args=(job_id, server_id, server["host"], server.get("username") or "", password),
        daemon=True,
    ).start()
    return job_id
