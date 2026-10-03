"""Runs rclone syncs one at a time and tracks live progress."""
import json
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


def enqueue(acc_id, trigger="manual"):
    """Queue a sync. Returns an error string, or None on success."""
    with _lock:
        if _current and _current["account_id"] == acc_id:
            return "Already running"
        if acc_id in _queued:
            return "Already queued"
        _queued.append(acc_id)
    _queue.put((acc_id, trigger))
    return None


def cancel():
    global _cancel
    with _lock:
        if _proc and _proc.poll() is None:
            _cancel = True
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
            _finish(acc_id, trigger, _now(), "failed", str(e), {})


def _run(acc_id, trigger):
    global _current, _proc, _cancel
    acc = store.get_account(acc_id)
    if not acc:
        return
    started = _now()
    if not acc.get("connected"):
        _finish(acc_id, trigger, started, "failed", "Account is not connected to Google", {})
        return

    dest = os.path.join(store.DATA_DIR, acc["folder"])
    os.makedirs(dest, exist_ok=True)
    cmd = ["rclone", "sync", "gdrive:", dest, "--config", store.conf_path(acc_id), *RCLONE_FLAGS]
    folders = acc.get("folders") or []
    if folders:
        # Only the chosen folders (and everything under them) are synced and, being a mirror, pruned.
        filt = store.conf_path(acc_id) + ".filter"
        with open(filt, "w") as f:
            for p in folders:
                f.write(f"+ /{_glob_escape(p)}/**\n")
            f.write("- *\n")
        cmd += ["--filter-from", filt]

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
            continue
        if "stats" in msg:
            st = last_stats = msg["stats"]
            with _lock:
                _current.update(
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
        elif msg.get("level") in ("error", "warning") or msg.get("msg", "").endswith(("Copied (new)", "Copied (replaced existing)", "Deleted")):
            text = f'{msg.get("object", "")}: {msg.get("msg", "")}'.strip(": ")
            with _lock:
                _current["log"].append(text)
            if msg.get("level") == "error":
                errors.append(text)
    code = _proc.wait()

    if _cancel:
        status_, error = "cancelled", "Cancelled by user"
    elif code == 0:
        status_, error = "success", ""
    else:
        status_, error = "failed", "; ".join(errors[-3:]) or f"rclone exited with code {code}"
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
    store.update(apply)
    with _lock:
        _current = None
        _proc = None


threading.Thread(target=_worker, daemon=True).start()
