"""Tests for the saved-server list (launcher/store.py)."""

import pytest

from launcher import store


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    """Point the store at a throwaway directory for every test.

    ``_config_dir()``/``_store_path()`` read the env var at call time, so
    monkeypatching it is enough -- no module reload needed.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_load_servers_empty_when_no_store_file():
    assert store.load_servers() == []


def test_add_server_assigns_id_and_persists():
    server = store.add_server(
        {"name": "Lab DL380", "host": "ilo3.example", "generation": "ilo3"}
    )
    assert server["id"]
    assert [s["id"] for s in store.load_servers()] == [server["id"]]


def test_add_server_strips_and_defaults_optional_fields():
    server = store.add_server(
        {"name": " Lab ", "host": " ilo.example ", "generation": "ilo100"}
    )
    assert server["name"] == "Lab"
    assert server["host"] == "ilo.example"
    assert server["username"] == ""
    assert server["port"] is None
    assert server["ssl"] is False


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "", "host": "h", "generation": "ilo3"},
        {"name": "n", "host": "", "generation": "ilo3"},
        {"name": "n", "host": "h", "generation": "not-a-real-generation"},
    ],
)
def test_add_server_rejects_invalid_input(fields):
    with pytest.raises(ValueError):
        store.add_server(fields)


def test_update_server_overwrites_fields_but_keeps_id():
    server = store.add_server({"name": "A", "host": "a", "generation": "ilo3"})
    updated = store.update_server(
        server["id"], {"name": "B", "host": "b", "generation": "ilo100"}
    )
    assert updated["id"] == server["id"]
    assert updated["name"] == "B"
    assert updated["generation"] == "ilo100"
    assert store.get_server(server["id"])["name"] == "B"


def test_update_server_unknown_id_raises_keyerror():
    with pytest.raises(KeyError):
        store.update_server("does-not-exist", {"name": "n", "host": "h", "generation": "ilo3"})


def test_remove_server_returns_false_when_not_found():
    assert store.remove_server("does-not-exist") is False


def test_remove_server_deletes_and_returns_true():
    server = store.add_server({"name": "A", "host": "a", "generation": "ilo3"})
    assert store.remove_server(server["id"]) is True
    assert store.load_servers() == []


def test_get_server_returns_none_when_not_found():
    assert store.get_server("does-not-exist") is None


def test_corrupt_store_file_treated_as_empty(tmp_path):
    cfg_dir = tmp_path / "ilo-console"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "servers.json").write_text("{not valid json", encoding="utf-8")
    assert store.load_servers() == []
