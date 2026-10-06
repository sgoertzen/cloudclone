import io
import json
import os

import pytest

import runner
import store


@pytest.fixture(autouse=True)
def reset_runner(monkeypatch):
    monkeypatch.setattr(runner, "_queued", [])
    monkeypatch.setattr(runner, "_current", None)
    monkeypatch.setattr(runner, "_proc", None)
    monkeypatch.setattr(runner, "_cancel", False)
    monkeypatch.setattr(runner, "_queue", runner.queue.Queue())


def test_glob_escape():
    assert runner._glob_escape("a*b?[c]{d}\\e") == "a\\*b\\?\\[c\\]\\{d\\}\\\\e"
    assert runner._glob_escape("plain/path") == "plain/path"


def test_build_filter():
    assert runner.build_filter(["Photos", "Docs/2020*"]) == "+ /Photos/**\n+ /Docs/2020\\*/**\n- *\n"


def test_build_command_with_and_without_filter():
    cmd = runner.build_command("abc", "/data/x")
    assert cmd[:4] == ["rclone", "sync", "gdrive:", "/data/x"]
    assert cmd[cmd.index("--config") + 1] == store.conf_path("abc")
    assert "--filter-from" not in cmd
    cmd = runner.build_command("abc", "/data/x", "/f")
    assert cmd[-2:] == ["--filter-from", "/f"]


def test_stats_update_maps_fields_and_defaults():
    assert runner.stats_update({})["bytes"] == 0
    assert runner.stats_update({})["transferring"] == []
    out = runner.stats_update({
        "bytes": 5, "totalBytes": 10, "checks": 1, "totalChecks": 2, "transfers": 3, "totalTransfers": 4,
        "deletes": 1, "errors": 2, "speed": 1.5, "eta": 9,
        "transferring": [{"name": "a", "size": 10, "bytes": 5, "extra": 1}, {"name": "b"}],
    })
    assert out["total_bytes"] == 10 and out["total_checks"] == 2 and out["total_transfers"] == 4
    assert out["eta"] == 9
    assert out["transferring"] == [{"name": "a", "size": 10, "bytes": 5}, {"name": "b", "size": 0, "bytes": 0}]


@pytest.mark.parametrize("msg,expected", [
    ({"level": "error", "object": "f.txt", "msg": "boom"}, "f.txt: boom"),
    ({"level": "warning", "msg": "careful"}, "careful"),
    ({"level": "info", "object": "a", "msg": "Copied (new)"}, "a: Copied (new)"),
    ({"level": "info", "object": "a", "msg": "Copied (replaced existing)"}, "a: Copied (replaced existing)"),
    ({"level": "info", "object": "a", "msg": "Deleted"}, "a: Deleted"),
    ({"level": "info", "object": "a", "msg": "Unchanged skipping"}, None),
    ({}, None),
])
def test_log_text(msg, expected):
    assert runner.log_text(msg) == expected


def test_final_outcome():
    assert runner.final_outcome(True, 1, ["e"]) == ("cancelled", "Cancelled by user")
    assert runner.final_outcome(False, 0, ["e"]) == ("success", "")
    assert runner.final_outcome(False, 3, []) == ("failed", "rclone exited with code 3")
    assert runner.final_outcome(False, 1, ["a", "b", "c", "d"]) == ("failed", "b; c; d")


def test_enqueue_dedupes_running_and_queued():
    assert runner.enqueue("a") is None
    assert runner.enqueue("a") == "Already queued"
    assert runner.enqueue("b", "scheduled") is None
    assert runner._queue.get_nowait() == ("a", "manual")
    assert runner.status()["queued"] == ["a", "b"]
    runner._current = {"account_id": "c", "log": [], "transferring": []}
    assert runner.enqueue("c") == "Already running"


def test_status_returns_copies():
    runner._current = {"account_id": "a", "log": runner.deque(["x"]), "transferring": [1]}
    st = runner.status()
    st["current"]["log"].append("y")
    assert list(runner._current["log"]) == ["x"]
    assert runner.status()["current"]["log"] == ["x"]


def test_status_idle():
    assert runner.status() == {"current": None, "queued": []}


def test_cancel_without_process():
    assert runner.cancel() is False


def test_cancel_terminates_running_process():
    class P:
        terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True
    runner._proc = P()
    assert runner.cancel() is True
    assert runner._proc.terminated and runner._cancel is True


def test_cancel_ignores_finished_process():
    class P:
        def poll(self): return 0
    runner._proc = P()
    assert runner.cancel() is False


class FakeProc:
    def __init__(self, lines, code):
        self.stderr = io.StringIO("".join(l + "\n" for l in lines))
        self._code = code

    def wait(self):
        return self._code

    def poll(self):
        return self._code


def _patch_popen(monkeypatch, lines, code=0):
    calls = []

    def popen(cmd, **kw):
        calls.append(cmd)
        return FakeProc(lines, code)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    return calls


def _connected_account(**kw):
    acc = store.new_account("Acct", "cid", "sec")
    store.update(lambda s: s["accounts"][acc["id"]].update(connected=True, **kw))
    return acc["id"]


def test_run_success(monkeypatch):
    acc_id = _connected_account()
    lines = [
        "not json",
        json.dumps({"level": "info", "object": "a.txt", "msg": "Copied (new)"}),
        json.dumps({"level": "error", "object": "b.txt", "msg": "bad"}),
        json.dumps({"stats": {"bytes": 100, "transfers": 2, "checks": 5, "deletes": 1}}),
    ]
    calls = _patch_popen(monkeypatch, lines, 0)
    runner._run(acc_id, "manual")

    assert "--filter-from" not in calls[0]
    assert os.path.isdir(os.path.join(store.DATA_DIR, "Acct"))
    last = store.get_account(acc_id)["last_run"]
    assert last["status"] == "success" and last["error"] == ""
    assert (last["bytes"], last["transfers"], last["checks"], last["deletes"]) == (100, 2, 5, 1)
    assert last["trigger"] == "manual"
    assert runner.status()["current"] is None


def test_run_failure_reports_errors(monkeypatch):
    acc_id = _connected_account()
    _patch_popen(monkeypatch, [json.dumps({"level": "error", "object": "x", "msg": "denied"})], 1)
    runner._run(acc_id, "scheduled")
    last = store.get_account(acc_id)["last_run"]
    assert last["status"] == "failed" and last["error"] == "x: denied"


def test_run_writes_filter_for_chosen_folders(monkeypatch):
    acc_id = _connected_account(folders=["Photos"])
    calls = _patch_popen(monkeypatch, [], 0)
    runner._run(acc_id, "manual")
    filt = calls[0][calls[0].index("--filter-from") + 1]
    with open(filt) as f:
        assert f.read() == "+ /Photos/**\n- *\n"


def test_run_cancelled(monkeypatch):
    acc_id = _connected_account()
    proc = FakeProc([], -15)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: proc)
    real_wait = proc.wait
    proc.wait = lambda: (setattr(runner, "_cancel", True), real_wait())[1]
    runner._run(acc_id, "manual")
    assert store.get_account(acc_id)["last_run"]["status"] == "cancelled"


def test_run_not_connected_records_failure():
    acc = store.new_account("N", "c", "s")
    runner._run(acc["id"], "manual")
    last = store.get_account(acc["id"])["last_run"]
    assert last["status"] == "failed" and "not connected" in last["error"]


def test_run_unknown_account_is_noop():
    runner._run("missing", "manual")  # must not raise


def test_finish_ignores_deleted_account():
    runner._finish("gone", "manual", "t", "success", "", {})
    assert runner.status()["current"] is None


def test_failed_sync_is_logged(monkeypatch, caplog):
    acc_id = _connected_account()
    _patch_popen(monkeypatch, [json.dumps({"level": "error", "object": "x", "msg": "denied"})], 1)
    with caplog.at_level("ERROR", logger="cloudclone.runner"):
        runner._run(acc_id, "manual")
    assert "x: denied" in caplog.text
    assert "Sync for Acct failed" in caplog.text


def test_not_connected_is_logged(caplog):
    acc = store.new_account("N", "c", "s")
    with caplog.at_level("WARNING", logger="cloudclone.runner"):
        runner._run(acc["id"], "scheduled")
    assert "not connected" in caplog.text


def test_unparseable_output_logged(monkeypatch, caplog):
    acc_id = _connected_account()
    _patch_popen(monkeypatch, ["garbage line"], 0)
    with caplog.at_level("WARNING", logger="cloudclone.runner"):
        runner._run(acc_id, "manual")
    assert "Unparseable rclone output" in caplog.text


def test_sync_lifecycle_logged_at_info(monkeypatch, caplog):
    acc_id = _connected_account()
    _patch_popen(monkeypatch, [json.dumps({"stats": {"transfers": 2}})], 0)
    with caplog.at_level("INFO", logger="cloudclone.runner"):
        runner.enqueue(acc_id)
        runner._run(acc_id, "manual")
    assert "Queued manual sync" in caplog.text
    assert "Starting manual sync for Acct" in caplog.text
    assert "finished: 2 transferred" in caplog.text


def test_duplicate_enqueue_warns(caplog):
    runner.enqueue("a")
    with caplog.at_level("WARNING", logger="cloudclone.runner"):
        runner.enqueue("a")
    assert "already queued" in caplog.text
