"""Persistent JSON state (accounts, schedules, password) plus rclone config helpers."""
import configparser
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import uuid

log = logging.getLogger("cloudclone.store")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
RCLONE_DIR = os.path.join(CONFIG_DIR, "rclone")

_lock = threading.RLock()
_state = None

DEFAULT_SCHEDULE = {"freq": "daily", "time": "02:00", "weekday": 6, "monthday": 1}


def _load():
    global _state
    if _state is None:
        os.makedirs(RCLONE_DIR, exist_ok=True)
        if os.path.exists(STATE_FILE):
            log.info("Loading state from %s", STATE_FILE)
            with open(STATE_FILE) as f:
                _state = json.load(f)
        else:
            log.info("No state file at %s; starting fresh", STATE_FILE)
            _state = {}
        _state.setdefault("accounts", {})
        if "secret_key" not in _state:
            _state["secret_key"] = secrets.token_hex(32)
            log.info("Generated a new session secret key")
            _save()
    return _state


def _save():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def state():
    with _lock:
        return _load()


def update(fn):
    """Run fn(state) under the lock and persist the result."""
    with _lock:
        s = _load()
        result = fn(s)
        _save()
        return result


# ---- password -------------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1)
    return f"{salt}${h.hex()}"


def check_password(password):
    stored = state().get("password")
    if not stored:
        return False
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_password(password, salt), stored)


def set_password(password):
    update(lambda s: s.__setitem__("password", hash_password(password)))


# ---- accounts -------------------------------------------------------------

def slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "account"


def new_account(name, client_id, client_secret, shared_drive_id=""):
    acc_id = uuid.uuid4().hex[:8]
    acc = {
        "id": acc_id,
        "name": name,
        "folder": slug(name),
        "client_id": client_id,
        "client_secret": client_secret,
        "shared_drive_id": shared_drive_id,
        "email": "",
        "connected": False,
        "schedule": dict(DEFAULT_SCHEDULE),
        "enabled": True,
        "folders": [],  # empty = whole Drive
        "last_run": None,
    }
    update(lambda s: s["accounts"].__setitem__(acc_id, acc))
    log.info("Created account %s (%s)", acc_id, name)
    return acc


def get_account(acc_id):
    return state()["accounts"].get(acc_id)


def conf_path(acc_id):
    return os.path.join(RCLONE_DIR, f"{acc_id}.conf")


def write_rclone_conf(acc, token_json=None):
    """(Re)write the rclone config for an account, keeping any existing token."""
    path = conf_path(acc["id"])
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path)
    if not cp.has_section("gdrive"):
        cp.add_section("gdrive")
    sec = cp["gdrive"]
    sec["type"] = "drive"
    sec["client_id"] = acc["client_id"]
    sec["client_secret"] = acc["client_secret"]
    sec["scope"] = "drive.readonly"
    sec["team_drive"] = acc.get("shared_drive_id", "")
    if token_json:
        sec["token"] = token_json
    with open(path, "w") as f:
        cp.write(f)
    os.chmod(path, 0o600)


def delete_account(acc_id):
    update(lambda s: s["accounts"].pop(acc_id, None))
    try:
        os.remove(conf_path(acc_id))
    except FileNotFoundError:
        log.warning("rclone config for account %s was already missing on delete", acc_id)
    log.info("Deleted account %s", acc_id)
