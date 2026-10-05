import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main
import runner
import store


@pytest.fixture(scope="module", autouse=True)
def paused_scheduler():
    """Started (so jobs get a next_run_time) but paused (so nothing ever fires)."""
    main.scheduler.start(paused=True)
    yield
    main.scheduler.shutdown(wait=False)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(runner, "_queued", [])
    monkeypatch.setattr(runner, "_current", None)
    monkeypatch.setattr(runner, "_proc", None)
    monkeypatch.setattr(runner, "_queue", runner.queue.Queue())
    for job in main.scheduler.get_jobs():
        job.remove()
    return TestClient(main.app)  # no `with`: startup (scheduler/worker) must not run


@pytest.fixture
def authed(client):
    assert client.post("/api/setup", json={"password": "password1"}).status_code == 200
    return client


def make_account(client, name="Acct", **extra):
    r = client.post("/api/accounts", json={"name": name, "client_id": "cid", "client_secret": "sec", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _connect(acc_id):
    store.update(lambda s: s["accounts"][acc_id].update(connected=True))


# ---- _trigger / reschedule --------------------------------------------------

def _fields(trig):
    return {f.name: str(f) for f in trig.fields}


def test_trigger_daily_weekly_monthly():
    d = _fields(main._trigger({"freq": "daily", "time": "03:15"}))
    assert (d["hour"], d["minute"]) == ("3", "15")
    w = _fields(main._trigger({"freq": "weekly", "time": "23:59", "weekday": 2}))
    assert (w["day_of_week"], w["hour"], w["minute"]) == ("2", "23", "59")
    m = _fields(main._trigger({"freq": "monthly", "time": "00:00", "monthday": 15}))
    assert (m["day"], m["hour"]) == ("15", "0")
    assert main._trigger({"freq": "never", "time": "00:00"}) is None


def test_reschedule_adds_replaces_and_removes(authed):
    acc_id = make_account(authed)["id"]
    job_id = f"sync-{acc_id}"
    assert main.scheduler.get_job(job_id) is None  # not connected yet

    connected = {**store.get_account(acc_id), "connected": True}
    main.reschedule(connected)
    assert main.scheduler.get_job(job_id) is not None
    main.reschedule(connected)  # replaces rather than raising on duplicate id
    assert len(main.scheduler.get_jobs()) == 1

    main.reschedule({**connected, "enabled": False})
    assert main.scheduler.get_job(job_id) is None


# ---- Schedule validation ----------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"freq": "hourly"}, {"time": "24:00"}, {"time": "2:00"}, {"time": "12:60"},
    {"weekday": 7}, {"weekday": -1}, {"monthday": 0}, {"monthday": 29},
])
def test_schedule_rejects_invalid(kw):
    with pytest.raises(main.HTTPException) as e:
        main.Schedule(**kw).validated()
    assert e.value.status_code == 400


def test_schedule_valid():
    assert main.Schedule(freq="weekly", time="23:59", weekday=0, monthday=28).validated() == {
        "freq": "weekly", "time": "23:59", "weekday": 0, "monthday": 28}


# ---- auth -------------------------------------------------------------------

def test_session_states(client):
    assert client.get("/api/session").json() == {"configured": False, "authed": False}
    client.post("/api/setup", json={"password": "password1"})
    assert client.get("/api/session").json() == {"configured": True, "authed": True}


def test_setup_rejects_short_password_and_second_setup(client):
    assert client.post("/api/setup", json={"password": "short"}).status_code == 400
    assert client.post("/api/setup", json={"password": "password1"}).status_code == 200
    assert client.post("/api/setup", json={"password": "password2"}).status_code == 400


def test_login_logout(authed):
    authed.post("/api/logout")
    assert authed.get("/api/state").status_code == 401
    assert authed.post("/api/login", json={"password": "nope"}).status_code == 401
    assert authed.post("/api/login", json={"password": "password1"}).status_code == 200
    assert authed.get("/api/state").status_code == 200


@pytest.mark.parametrize("method,path", [
    ("get", "/api/state"), ("post", "/api/accounts"), ("patch", "/api/accounts/x"),
    ("delete", "/api/accounts/x"), ("get", "/api/accounts/x/folders"),
    ("get", "/api/accounts/x/auth-url"), ("post", "/api/accounts/x/auth-code"),
    ("post", "/api/accounts/x/run"), ("post", "/api/cancel"), ("get", "/oauth/callback"),
])
def test_endpoints_require_auth(client, method, path):
    assert getattr(client, method)(path).status_code == 401


# ---- accounts ---------------------------------------------------------------

def test_create_account_hides_secret(authed):
    acc = make_account(authed, "My Drive")
    assert "client_secret" not in acc
    assert acc["destination"].endswith("My_Drive")
    assert acc["next_run"] is None
    state = authed.get("/api/state").json()
    assert [a["name"] for a in state["accounts"]] == ["My Drive"]
    assert all("client_secret" not in a for a in state["accounts"])


def test_create_account_validation(authed):
    assert authed.post("/api/accounts", json={"name": "  "}).status_code == 400
    # no credentials and no default saved
    assert authed.post("/api/accounts", json={"name": "A"}).status_code == 400
    make_account(authed, "A b")
    dup = authed.post("/api/accounts", json={"name": "A_b", "client_id": "c", "client_secret": "s"})
    assert dup.status_code == 400  # same slug


def test_save_default_client_reused(authed):
    make_account(authed, "One", save_as_default=True)
    assert authed.get("/api/state").json()["has_default_client"] is True
    acc = authed.post("/api/accounts", json={"name": "Two"}).json()
    assert store.get_account(acc["id"])["client_id"] == "cid"


def test_update_account(authed):
    acc = make_account(authed, "Orig")
    r = authed.patch(f"/api/accounts/{acc['id']}", json={
        "name": " Renamed ", "enabled": False, "shared_drive_id": " sd ",
        "schedule": {"freq": "monthly", "time": "04:30", "monthday": 5},
        "folders": ["/b/", "a", "a", "", "/"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Renamed" and body["enabled"] is False
    assert body["shared_drive_id"] == "sd"
    assert body["folders"] == ["a", "b"]
    assert body["schedule"]["freq"] == "monthly"
    assert body["folder"] == "Orig"  # destination never moves on rename


def test_update_account_errors(authed):
    assert authed.patch("/api/accounts/missing", json={}).status_code == 404
    acc = make_account(authed)
    r = authed.patch(f"/api/accounts/{acc['id']}", json={"schedule": {"freq": "bad"}})
    assert r.status_code == 400


def test_delete_account(authed):
    acc = make_account(authed)
    assert authed.delete(f"/api/accounts/{acc['id']}").status_code == 200
    assert authed.get("/api/state").json()["accounts"] == []


def test_delete_account_blocked_while_running(authed, monkeypatch):
    acc = make_account(authed)
    monkeypatch.setattr(runner, "_current", {"account_id": acc["id"], "log": [], "transferring": []})
    assert authed.delete(f"/api/accounts/{acc['id']}").status_code == 400
    assert store.get_account(acc["id"]) is not None


# ---- folders ----------------------------------------------------------------

def test_list_folders(authed, monkeypatch):
    acc = make_account(authed)
    _connect(acc["id"])
    run = MagicMock(return_value=MagicMock(returncode=0, stdout=json.dumps([{"Name": "b"}, {"Name": "A"}]), stderr=""))
    monkeypatch.setattr(main.subprocess, "run", run)
    r = authed.get(f"/api/accounts/{acc['id']}/folders", params={"path": "/x/y/"})
    assert r.json() == ["A", "b"]
    assert run.call_args.args[0][2] == "gdrive:x/y"


def test_list_folders_errors(authed, monkeypatch):
    assert authed.get("/api/accounts/missing/folders").status_code == 400
    acc = make_account(authed)
    assert authed.get(f"/api/accounts/{acc['id']}/folders").status_code == 400  # not connected
    _connect(acc["id"])
    monkeypatch.setattr(main.subprocess, "run",
                        lambda *a, **k: MagicMock(returncode=1, stdout="", stderr="first\nlast line\n"))
    r = authed.get(f"/api/accounts/{acc['id']}/folders")
    assert (r.status_code, r.json()["detail"]) == (502, "last line")
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: MagicMock(returncode=1, stdout="", stderr=""))
    assert authed.get(f"/api/accounts/{acc['id']}/folders").json()["detail"] == "rclone failed"

    def timeout(*a, **k):
        raise main.subprocess.TimeoutExpired("rclone", 1)
    monkeypatch.setattr(main.subprocess, "run", timeout)
    assert authed.get(f"/api/accounts/{acc['id']}/folders").status_code == 504


# ---- OAuth ------------------------------------------------------------------

def test_auth_url(authed):
    acc = make_account(authed)
    assert authed.get("/api/accounts/missing/auth-url").status_code == 404
    url = authed.get(f"/api/accounts/{acc['id']}/auth-url").json()["url"]
    assert url.startswith(main.AUTH_URL + "?")
    q = main.parse_qs(main.urlparse(url).query)
    assert q["client_id"] == ["cid"] and q["scope"] == [main.SCOPE]
    assert q["state"][0].startswith(acc["id"] + ".")


def _token_response(status=200, body=None):
    r = MagicMock(status_code=status, text="raw")
    r.json.return_value = body if body is not None else {
        "access_token": "at", "refresh_token": "rt", "expires_in": 100}
    return r


@pytest.fixture
def google(monkeypatch):
    post = MagicMock(return_value=_token_response())
    get = MagicMock(return_value=MagicMock(json=lambda: {"user": {"emailAddress": "me@example.com"}}))
    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.requests, "get", get)
    return post, get


def test_auth_code_connects_account(authed, google):
    post, _ = google
    acc = make_account(authed)
    r = authed.post(f"/api/accounts/{acc['id']}/auth-code", json={"code": "  the-code "})
    assert r.status_code == 200
    assert r.json()["connected"] is True and r.json()["email"] == "me@example.com"
    assert post.call_args.kwargs["data"]["code"] == "the-code"
    with open(store.conf_path(acc["id"])) as f:
        conf = f.read()
    assert "refresh_token" in conf and "rt" in conf
    assert main.scheduler.get_job(f"sync-{acc['id']}") is not None


def test_auth_code_accepts_pasted_redirect_url(authed, google):
    post, _ = google
    acc = make_account(authed)
    authed.post(f"/api/accounts/{acc['id']}/auth-code",
                json={"code": "http://127.0.0.1:53682/?code=4%2Fabc&scope=x"})
    assert post.call_args.kwargs["data"]["code"] == "4/abc"


def test_auth_code_error_cases(authed, google):
    post, _ = google
    acc = make_account(authed)
    url = f"/api/accounts/{acc['id']}/auth-code"
    r = authed.post(url, json={"code": "http://x/?error=access_denied"})
    assert r.status_code == 400 and "access_denied" in r.json()["detail"]
    assert authed.post(url, json={"code": "http://x/?foo=1"}).status_code == 400  # no code in URL
    assert authed.post(url, json={"code": ""}).status_code == 400
    assert authed.post("/api/accounts/missing/auth-code", json={"code": "c"}).status_code == 404

    post.return_value = _token_response(400, {"error_description": "bad grant"})
    r = authed.post(url, json={"code": "c"})
    assert r.status_code == 400 and "bad grant" in r.json()["detail"]

    post.return_value = _token_response(200, {"access_token": "at"})
    r = authed.post(url, json={"code": "c"})
    assert r.status_code == 400 and "refresh token" in r.json()["detail"]
    assert store.get_account(acc["id"])["connected"] is False


def test_exchange_survives_userinfo_failure(authed, google):
    _, get = google
    get.side_effect = RuntimeError("network")
    acc = make_account(authed)
    r = authed.post(f"/api/accounts/{acc['id']}/auth-code", json={"code": "c"})
    assert r.json()["connected"] is True and r.json()["email"] == ""


def test_oauth_callback(authed, google):
    acc = make_account(authed)
    url = authed.get(f"/api/accounts/{acc['id']}/auth-url").json()["url"]
    state = main.parse_qs(main.urlparse(url).query)["state"][0]

    r = authed.get("/oauth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
    assert store.get_account(acc["id"])["connected"] is True
    # nonce is single-use
    r = authed.get("/oauth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 400 and "Invalid OAuth state" in r.text


def test_oauth_callback_bad_state_and_error(authed, google):
    acc = make_account(authed)
    authed.get(f"/api/accounts/{acc['id']}/auth-url")
    r = authed.get("/oauth/callback", params={"code": "c", "state": f"{acc['id']}.wrong"})
    assert r.status_code == 400
    assert store.get_account(acc["id"])["connected"] is False
    r = authed.get("/oauth/callback", params={"error": "access_denied"})
    assert r.status_code == 400 and "access_denied" in r.text


# ---- runs -------------------------------------------------------------------

def test_run_now(authed):
    assert authed.post("/api/accounts/missing/run").status_code == 404
    acc = make_account(authed)
    assert authed.post(f"/api/accounts/{acc['id']}/run").status_code == 400  # not connected
    _connect(acc["id"])
    assert authed.post(f"/api/accounts/{acc['id']}/run").json() == {"ok": True}
    assert authed.post(f"/api/accounts/{acc['id']}/run").status_code == 409  # already queued
    assert authed.get("/api/state").json()["run"]["queued"] == [acc["id"]]


def test_cancel_endpoint(authed):
    assert authed.post("/api/cancel").json() == {"cancelled": False}


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
