"""Tests for ILo100Client's control-arbitration handling.

The server defaults every fresh connection to view-only (see
docs/PROTOCOL_ILO100.md) -- the real console client only escapes that by
sending RequestPrimaryControl after a human clicks "OK" on a dialog. These
tests exercise that logic without a live socket.
"""

import pytest

from ilo100console import power as pwr
from ilo100console import protocol as p
from ilo100console.client import ILo100Client


def _make_client():
    return ILo100Client("example.invalid", "admin", "hunter2")


def test_send_power_action_falls_back_to_status_on_transient_failure(monkeypatch):
    # This BMC's httpd is known to reset mid-response intermittently -- the
    # action itself may have landed even though we lost the confirmation, so
    # a failure here should resync via a status read rather than leave the
    # caller (and the UI lamp) on a stale reading with no idea what happened.
    c = _make_client()

    def flaky_action(host, username, password, action, **kwargs):
        raise pwr.PowerControlError("connection reset")

    def working_status(host, username, password, **kwargs):
        return "ON"

    monkeypatch.setattr(pwr, "send_power_action", flaky_action)
    monkeypatch.setattr(pwr, "get_power_status", working_status)

    assert c.send_power_action(pwr.POWER_UP) == "ON"


def test_send_power_action_propagates_if_status_fallback_also_fails(monkeypatch):
    c = _make_client()

    def flaky_action(host, username, password, action, **kwargs):
        raise pwr.PowerControlError("connection reset")

    def flaky_status(host, username, password, **kwargs):
        raise pwr.PowerControlError("still reset")

    monkeypatch.setattr(pwr, "send_power_action", flaky_action)
    monkeypatch.setattr(pwr, "get_power_status", flaky_status)

    with pytest.raises(pwr.PowerControlError):
        c.send_power_action(pwr.POWER_UP)


def test_auto_requests_control_when_offered_and_view_only():
    c = _make_client()
    sent = []
    c.session.send = lambda frame: sent.append(frame)

    mus = p.MultiUserState(flags=0x101EEF)  # view-only, allowed to take control
    assert mus.has_full_control is False
    c._apply_multi_user_state(mus)

    assert sent == [p.encode_request_primary_control()]
    assert c._requested_control is True
    assert c._has_full_control is False


def test_does_not_re_request_control_on_repeated_view_only_state():
    c = _make_client()
    sent = []
    c.session.send = lambda frame: sent.append(frame)

    mus = p.MultiUserState(flags=0x101EEF)
    c._apply_multi_user_state(mus)
    c._apply_multi_user_state(mus)  # same view-only state again

    assert sent == [p.encode_request_primary_control()]  # only sent once


def test_control_changed_callback_fires_on_transition_to_full_control():
    c = _make_client()
    c.session.send = lambda frame: None
    events = []
    c.on_control_changed = lambda has: events.append(has)

    c._apply_multi_user_state(p.MultiUserState(flags=0x101EEF))  # view-only
    assert events == []  # still view-only, no transition yet

    c._apply_multi_user_state(p.MultiUserState(flags=0x101EE7))  # now full control
    assert events == [True]
    assert c._has_full_control is True


def test_does_not_request_control_when_not_allowed():
    c = _make_client()
    sent = []
    c.session.send = lambda frame: sent.append(frame)

    # user_status view-only (1<<3), but allowed_to_take_full_control bit clear.
    mus = p.MultiUserState(flags=0x08)
    c._apply_multi_user_state(mus)

    assert sent == []


def test_power_poller_fires_callback_on_first_status(monkeypatch):
    # Power can change through means entirely outside this app (the device's
    # own browser web UI, a physical button, IPMI) -- the poller must pick
    # that up rather than leaving the lamp stuck on whatever it read at
    # connect time. First iteration should fire immediately, no interval wait.
    c = _make_client()
    events = []
    c.on_power_status = lambda status: events.append(status)

    def one_shot_status():
        c._stop.set()  # stop after this single poll so the test doesn't hang
        return "ON"

    monkeypatch.setattr(c, "get_power_status", one_shot_status)
    c._run_power_poller()

    assert events == ["ON"]
    assert c._last_power_status == "ON"


def test_power_poller_does_not_refire_on_unchanged_status(monkeypatch):
    c = _make_client()
    c._last_power_status = "ON"
    events = []
    c.on_power_status = lambda status: events.append(status)

    def one_shot_status():
        c._stop.set()
        return "ON"

    monkeypatch.setattr(c, "get_power_status", one_shot_status)
    c._run_power_poller()

    assert events == []  # unchanged from _last_power_status -- no callback


def test_power_poller_survives_transient_failure(monkeypatch):
    c = _make_client()
    events = []
    c.on_power_status = lambda status: events.append(status)

    def failing_status():
        c._stop.set()  # still stop after one iteration for the test
        raise pwr.PowerControlError("connection reset")

    monkeypatch.setattr(c, "get_power_status", failing_status)
    c._run_power_poller()  # must not raise

    assert events == []
