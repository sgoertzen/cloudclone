"""Runs rclone syncs one at a time and tracks live progress."""
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone

import store

RCLONE_FLAGS = [
    "--drive-export-formats", "docx,xlsx,pptx,svg",
    "--drive-skip-shortcuts",
    "--check-first",          # compare everything first so total size is known for the progress bar
    "--transfers", "4",
    "--checkers", "8",
    "--retries", "3",
    "--stats", "1s",
    "--use-json-log",
    "-v",
]

log = logging.getLogger("cloudclone.runner")

_queue = queue.Queue()
_lock = threading.Lock()
_queued = []          # account ids waiting
_current = None       # live progress dict for the running sync
_proc = None
_cancel = False


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _glob_escape(path):
    return re.sub(r"([\\*?\[\]{}])", r"\\\1", path)


def build_filter(folders):
    """Contents of an rclone filter file that includes only `folders` (and everything under them).

    An entry "*" or ending in "/*" ("Photos/*", or "*" for the Drive root) means only the files directly in that folder.
    """
    lines = [f"+ /{_glob_escape(p[:-1])}*" if p == "*" or p.endswith("/*") else f"+ /{_glob_escape(p)}/**"
             for p in folders]
    lines.append("- *")
    return "\n".join(lines) + "\n"


def build_command(acc_id, dest, filter_path=None):
    cmd = ["rclone", "sync", "gdrive:", dest, "--config", store.conf_path(acc_id), *RCLONE_FLAGS]
    if filter_path:
        cmd += ["--filter-from", filter_path]
    return cmd


def stats_update(st):
    """Map an rclone `stats` object to the fields of the live progress dict."""
    return dict(
        bytes=st.get("bytes", 0), total_bytes=st.get("totalBytes", 0),
        checks=st.get("checks", 0), total_checks=st.get("totalChecks", 0),
        transfers=st.get("transfers", 0), total_transfers=st.get("totalTransfers", 0),
        deletes=st.get("deletes", 0), errors=st.get("errors", 0),
        speed=st.get("speed", 0), eta=st.get("eta"),
        transferring=[
            {"name": t.get("name"), "size": t.get("size", 0), "bytes": t.get("bytes", 0)}
            for t in (st.get("transferring") or [])
        ],
    )


_LOGGED_MSGS = ("Copied (new)", "Copied (replaced existing)", "Deleted")


def log_text(msg):
    """Text for the live log if this rclone JSON log line is worth showing, else None."""
    if msg.get("level") in ("error", "warning") or msg.get("msg", "").endswith(_LOGGED_MSGS):
        return f'{msg.get("object", "")}: {msg.get("msg", "")}'.strip(": ")
    return None


def final_outcome(cancelled, code, errors):
    """(status, error) for a finished rclone process."""
    if cancelled:
        return "cancelled", "Cancelled by user"
    if code == 0:
        return "success", ""
    return "failed", "; ".join(errors[-3:]) or f"rclone exited with code {code}"


def start_worker():
    log.info("Starting sync worker")
    threading.Thread(target=_worker, daemon=True).start()


def enqueue(acc_id, trigger="manual"):
    """Queue a sync. Returns an error string, or None on success."""
    with _lock:
        if _current and _current["account_id"] == acc_id:
            log.warning("Not queueing %s sync for account %s: already running", trigger, acc_id)
            return "Already running"
        if acc_id in _queued:
            log.warning("Not queueing %s sync for account %s: already queued", trigger, acc_id)
            return "Already queued"
        _queued.append(acc_id)
    _queue.put((acc_id, trigger))
    log.info("Queued %s sync for account %s", trigger, acc_id)
    return None


def cancel():
    global _cancel
    with _lock:
        if _proc and _proc.poll() is None:
            _cancel = True
            log.info("Terminating running rclone process")
            _proc.terminate()
            return True
    return False


def status():
    with _lock:
        cur = dict(_current) if _current else None
        if cur:
            cur["log"] = list(cur["log"])
            cur["transferring"] = list(cur["transferring"])
        return {"current": cur, "queued": list(_queued)}


def _worker():
    while True:
        acc_id, trigger = _queue.get()
        with _lock:
            if acc_id in _queued:
                _queued.remove(acc_id)
        try:
            _run(acc_id, trigger)
        except Exception as e:  # never let the worker die
            log.exception("Sync for account %s crashed", acc_id)
            _finish(acc_id, trigger, _now(), "failed", str(e), {})


def _run(acc_id, trigger):
    global _current, _proc, _cancel
    acc = store.get_account(acc_id)
    if not acc:
        log.warning("Skipping %s sync: account %s no longer exists", trigger, acc_id)
        return
    started = _now()
    if not acc.get("connected"):
        log.warning("Skipping %s sync for %s: account is not connected to Google", trigger, acc["name"])
        _finish(acc_id, trigger, started, "failed", "Account is not connected to Google", {})
        return

    dest = os.path.join(store.DATA_DIR, acc["folder"])
    os.makedirs(dest, exist_ok=True)
    filt = None
    folders = acc.get("folders") or []
    if folders:
        # Only the chosen folders (and everything under them) are synced and, being a mirror, pruned.
        filt = store.conf_path(acc_id) + ".filter"
        with open(filt, "w") as f:
            f.write(build_filter(folders))
    cmd = build_command(acc_id, dest, filt)

    log.info("Starting %s sync for %s -> %s%s", trigger, acc["name"], dest,
             f" (folders: {', '.join(folders)})" if folders else "")
    with _lock:
        _cancel = False
        _current = {
            "account_id": acc_id, "trigger": trigger, "started": started,
            "bytes": 0, "total_bytes": 0, "checks": 0, "total_checks": 0,
            "transfers": 0, "total_transfers": 0, "deletes": 0, "errors": 0,
            "speed": 0, "eta": None, "transferring": [], "log": deque(maxlen=30),
        }
        _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)

    last_stats, errors = {}, []
    for line in _proc.stderr:
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("Unparseable rclone output (%s): %s", acc["name"], line.strip()[:200])
            continue
        if "stats" in msg:
            st = last_stats = msg["stats"]
            with _lock:
                _current.update(**stats_update(st))
        else:
            text = log_text(msg)
            if text:
                if msg.get("level") == "error":
                    log.error("rclone (%s): %s", acc["name"], text)
                with _lock:
                    _current["log"].append(text)
                if msg.get("level") == "error":
                    errors.append(text)
    code = _proc.wait()

    status_, error = final_outcome(_cancel, code, errors)
    if status_ == "failed":
        log.error("Sync for %s failed: %s", acc["name"], error)
    elif status_ == "cancelled":
        log.warning("Sync for %s was cancelled", acc["name"])
    else:
        log.info("Sync for %s finished: %s transferred, %s checked, %s deleted, %s bytes", acc["name"],
                 last_stats.get("transfers", 0), last_stats.get("checks", 0),
                 last_stats.get("deletes", 0), last_stats.get("bytes", 0))
    _finish(acc_id, trigger, started, status_, error, last_stats)


def _finish(acc_id, trigger, started, status_, error, stats):
    global _current, _proc
    result = {
        "started": started, "finished": _now(), "status": status_, "error": error,
        "trigger": trigger, "bytes": stats.get("bytes", 0),
        "transfers": stats.get("transfers", 0), "deletes": stats.get("deletes", 0),
        "checks": stats.get("checks", 0),
    }

    def apply(s):
        if acc_id in s["accounts"]:
            s["accounts"][acc_id]["last_run"] = result
        else:
            log.warning("Finished sync for account %s, which was deleted meanwhile", acc_id)
    store.update(apply)
    with _lock:
        _current = None
        _proc = None
