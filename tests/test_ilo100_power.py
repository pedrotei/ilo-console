"""Tests for chassis power control (a separate HTTP/CGI channel from the
Mahogany KVM socket -- see ilo100console/power.py)."""

import pytest

from ilo100console import power

_CHASSIS_HTML_ON = (
    '<TR><TD><STRONG>Power Status:</STRONG></TD><TD>ON</TD></TR>'
)
_CHASSIS_HTML_OFF = (
    '<TR><TD><STRONG>Power Status:</STRONG></TD><TD>OFF</TD></TR>'
)


def test_parse_status_on():
    assert power._parse_status(_CHASSIS_HTML_ON) == "ON"


def test_parse_status_off():
    assert power._parse_status(_CHASSIS_HTML_OFF) == "OFF"


def test_parse_status_missing_raises():
    with pytest.raises(power.PowerControlError):
        power._parse_status("<html>nothing here</html>")


def test_send_power_action_rejects_unknown_action():
    with pytest.raises(ValueError):
        power.send_power_action("host.invalid", "admin", "pw", "NotARealAction")


def test_send_power_action_builds_correct_query(monkeypatch):
    captured = {}

    def fake_open(host, username, password, *, web_port=None, use_ssl=False,
                  timeout=15.0, query=""):
        captured["query"] = query
        return _CHASSIS_HTML_ON

    monkeypatch.setattr(power, "_open_chassis_html", fake_open)
    status = power.send_power_action("host.invalid", "admin", "pw", power.POWER_UP)
    assert status == "ON"
    assert captured["query"] == "PwrCtrl=PowerUp&Button1=Apply"


def test_get_power_status_retries_transient_reset(monkeypatch):
    # This BMC's httpd is known to reset mid-response intermittently; a
    # read-only status check should retry through that rather than fail.
    monkeypatch.setattr(power, "_TRANSIENT_RETRY_DELAYS", (0, 0, 0))
    attempts = []

    def flaky_open(host, username, password, *, web_port=None, use_ssl=False,
                    timeout=15.0, query=""):
        attempts.append(1)
        if len(attempts) < 3:
            raise power._TransientConnectionError("connection reset")
        return _CHASSIS_HTML_ON

    monkeypatch.setattr(power, "_open_chassis_html", flaky_open)
    assert power.get_power_status("host.invalid", "admin", "pw") == "ON"
    assert len(attempts) == 3


def test_get_power_status_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(power, "_TRANSIENT_RETRY_DELAYS", (0, 0, 0))

    def always_fails(host, username, password, *, web_port=None, use_ssl=False,
                      timeout=15.0, query=""):
        raise power.PowerControlError("connection reset")

    monkeypatch.setattr(power, "_open_chassis_html", always_fails)
    with pytest.raises(power.PowerControlError):
        power.get_power_status("host.invalid", "admin", "pw")


def test_get_power_status_uses_empty_query(monkeypatch):
    captured = {}

    def fake_open(host, username, password, *, web_port=None, use_ssl=False,
                  timeout=15.0, query=""):
        captured["query"] = query
        return _CHASSIS_HTML_OFF

    monkeypatch.setattr(power, "_open_chassis_html", fake_open)
    status = power.get_power_status("host.invalid", "admin", "pw")
    assert status == "OFF"
    assert captured["query"] == ""
