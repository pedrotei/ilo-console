"""Tests for the saved-server list (launcher/store.py)."""

import pytest

from launcher import store


class _FakeKeyringErrors:
    class PasswordDeleteError(Exception):
        pass


class _FakeKeyring:
    """In-memory stand-in for the ``keyring`` package, so tests don't depend
    on a real OS keyring backend being available."""

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
    """Point the store at a throwaway directory for every test.

    ``_config_dir()``/``_store_path()`` read the env var at call time, so
    monkeypatching it is enough -- no module reload needed.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _fake_keyring(monkeypatch):
    """Swap the real OS keyring for an in-memory fake for every test."""
    monkeypatch.setattr(store, "keyring", _FakeKeyring())


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


# -- reorder ----------------------------------------------------------------


def test_reorder_servers_persists_new_order():
    a = store.add_server({"name": "A", "host": "a", "generation": "ilo3"})
    b = store.add_server({"name": "B", "host": "b", "generation": "ilo3"})
    c = store.add_server({"name": "C", "host": "c", "generation": "ilo3"})
    reordered = store.reorder_servers([c["id"], a["id"], b["id"]])
    assert [s["id"] for s in reordered] == [c["id"], a["id"], b["id"]]
    assert [s["id"] for s in store.load_servers()] == [c["id"], a["id"], b["id"]]


def test_reorder_servers_rejects_missing_id():
    a = store.add_server({"name": "A", "host": "a", "generation": "ilo3"})
    store.add_server({"name": "B", "host": "b", "generation": "ilo3"})
    with pytest.raises(ValueError):
        store.reorder_servers([a["id"]])  # missing B


def test_reorder_servers_rejects_unknown_id():
    a = store.add_server({"name": "A", "host": "a", "generation": "ilo3"})
    with pytest.raises(ValueError):
        store.reorder_servers([a["id"], "not-a-real-id"])


# -- password (OS keyring) -------------------------------------------------


def test_add_server_without_save_password_stores_nothing():
    server = store.add_server({"name": "A", "host": "a", "generation": "ilo3"})
    assert server["has_saved_password"] is False
    assert store.get_password(server["id"]) is None


def test_add_server_with_save_password_stores_in_keyring_not_json():
    server = store.add_server(
        {
            "name": "A", "host": "a", "generation": "ilo3",
            "save_password": True, "password": "hunter2",
        }
    )
    assert server["has_saved_password"] is True
    assert "password" not in server
    assert store.get_password(server["id"]) == "hunter2"


def test_update_server_edit_without_touching_password_keeps_it():
    server = store.add_server(
        {
            "name": "A", "host": "a", "generation": "ilo3",
            "save_password": True, "password": "hunter2",
        }
    )
    updated = store.update_server(
        server["id"],
        {"name": "A renamed", "host": "a", "generation": "ilo3", "save_password": True},
    )
    assert updated["has_saved_password"] is True
    assert store.get_password(server["id"]) == "hunter2"


def test_update_server_replaces_saved_password():
    server = store.add_server(
        {
            "name": "A", "host": "a", "generation": "ilo3",
            "save_password": True, "password": "hunter2",
        }
    )
    store.update_server(
        server["id"],
        {
            "name": "A", "host": "a", "generation": "ilo3",
            "save_password": True, "password": "new-password",
        },
    )
    assert store.get_password(server["id"]) == "new-password"


def test_update_server_unchecking_save_password_clears_it():
    server = store.add_server(
        {
            "name": "A", "host": "a", "generation": "ilo3",
            "save_password": True, "password": "hunter2",
        }
    )
    updated = store.update_server(
        server["id"],
        {"name": "A", "host": "a", "generation": "ilo3", "save_password": False},
    )
    assert updated["has_saved_password"] is False
    assert store.get_password(server["id"]) is None


def test_remove_server_deletes_saved_password():
    server = store.add_server(
        {
            "name": "A", "host": "a", "generation": "ilo3",
            "save_password": True, "password": "hunter2",
        }
    )
    store.remove_server(server["id"])
    assert store.get_password(server["id"]) is None
