import configparser
import json
import os
import stat

import store


def test_slug():
    assert store.slug("My Drive") == "My_Drive"
    assert store.slug("a/b\\c") == "a_b_c"
    assert store.slug("..hidden..") == "hidden"
    assert store.slug("   ") == "account"
    assert store.slug("") == "account"
    assert store.slug("ok-name_1.2") == "ok-name_1.2"


def test_state_created_with_secret_key_and_persisted():
    s = store.state()
    assert s["accounts"] == {}
    assert len(s["secret_key"]) == 64
    with open(store.STATE_FILE) as f:
        assert json.load(f)["secret_key"] == s["secret_key"]


def test_state_loads_existing_file():
    with open(store.STATE_FILE, "w") as f:
        json.dump({"secret_key": "abc", "accounts": {"x": {"id": "x"}}}, f)
    assert store.state()["secret_key"] == "abc"
    assert store.get_account("x") == {"id": "x"}


def test_update_persists_and_returns_result():
    assert store.update(lambda s: s.setdefault("foo", 1)) == 1
    store._state = None  # force reload from disk
    assert store.state()["foo"] == 1
    assert not os.path.exists(store.STATE_FILE + ".tmp")


def test_password_roundtrip():
    assert store.check_password("anything") is False  # none set yet
    store.set_password("correct horse")
    assert store.check_password("correct horse") is True
    assert store.check_password("wrong") is False
    assert "correct horse" not in store.state()["password"]


def test_hash_password_salted_and_deterministic_with_salt():
    assert store.hash_password("pw") != store.hash_password("pw")
    assert store.hash_password("pw", "salt") == store.hash_password("pw", "salt")


def test_new_account_defaults():
    acc = store.new_account("My Drive", "cid", "sec", "drive123")
    assert acc["folder"] == "My_Drive"
    assert acc["connected"] is False and acc["enabled"] is True
    assert acc["schedule"] == store.DEFAULT_SCHEDULE
    assert acc["schedule"] is not store.DEFAULT_SCHEDULE
    assert acc["shared_drive_id"] == "drive123"
    assert store.get_account(acc["id"]) == acc


def test_get_account_missing():
    assert store.get_account("nope") is None


def test_write_rclone_conf_and_keeps_token():
    acc = store.new_account("A", "cid", "sec", "td")
    store.write_rclone_conf(acc, '{"access_token": "t"}')
    path = store.conf_path(acc["id"])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    acc["shared_drive_id"] = ""
    store.write_rclone_conf(acc)  # rewrite without a token keeps the old one
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path)
    sec = cp["gdrive"]
    assert sec["type"] == "drive"
    assert sec["client_id"] == "cid" and sec["client_secret"] == "sec"
    assert sec["scope"] == "drive.readonly"
    assert sec["team_drive"] == ""
    assert sec["token"] == '{"access_token": "t"}'


def test_write_rclone_conf_percent_in_secret():
    acc = store.new_account("A", "cid", "se%cret")
    store.write_rclone_conf(acc)
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(store.conf_path(acc["id"]))
    assert cp["gdrive"]["client_secret"] == "se%cret"


def test_delete_account_removes_state_and_conf():
    acc = store.new_account("A", "c", "s")
    store.write_rclone_conf(acc)
    store.delete_account(acc["id"])
    assert store.get_account(acc["id"]) is None
    assert not os.path.exists(store.conf_path(acc["id"]))
    store.delete_account(acc["id"])  # idempotent


def test_delete_missing_conf_warns(caplog):
    acc = store.new_account("A", "c", "s")  # conf file never written
    with caplog.at_level("WARNING", logger="cloudclone.store"):
        store.delete_account(acc["id"])
    assert "already missing" in caplog.text
