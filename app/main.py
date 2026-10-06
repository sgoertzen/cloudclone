import json
import logging
import os
import secrets
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import runner
import store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "warning").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cloudclone")


class _QuietAccessLog(logging.Filter):
    """The UI polls /api/state constantly; show those access lines only at DEBUG."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and args[1] == "GET" and str(args[2]).split("?")[0] == "/api/state":
            if logging.getLogger("uvicorn.access").getEffectiveLevel() > logging.DEBUG:
                return False
            record.levelno, record.levelname = logging.DEBUG, "DEBUG"
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietAccessLog())

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# With PUBLIC_URL (e.g. https://gdrive.example.com behind your reverse proxy) Google redirects straight back to the app;
# otherwise fall back to the loopback paste-back flow.
REDIRECT_URI = f"{PUBLIC_URL}/oauth/callback" if PUBLIC_URL else "http://127.0.0.1:53682/"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

VERSION = os.environ.get("APP_VERSION", "dev")

app = FastAPI(title="CloudClone")
app.add_middleware(SessionMiddleware, secret_key=store.state()["secret_key"], same_site="lax", max_age=60 * 60 * 24 * 30)
scheduler = BackgroundScheduler()


def require_auth(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(401, "Not logged in")


# ---- scheduling -----------------------------------------------------------

def _trigger(sch):
    hour, minute = (int(x) for x in sch["time"].split(":"))
    if sch["freq"] == "daily":
        return CronTrigger(hour=hour, minute=minute)
    if sch["freq"] == "weekly":
        return CronTrigger(day_of_week=int(sch["weekday"]), hour=hour, minute=minute)
    if sch["freq"] == "monthly":
        return CronTrigger(day=int(sch["monthday"]), hour=hour, minute=minute)
    return None


def reschedule(acc):
    job_id = f"sync-{acc['id']}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    trig = _trigger(acc["schedule"]) if acc["enabled"] and acc["connected"] else None
    if trig:
        scheduler.add_job(runner.enqueue, trig, args=[acc["id"], "scheduled"], id=job_id,
                          misfire_grace_time=3600, coalesce=True)
        log.info("Scheduled %s sync for %s at %s", acc["schedule"]["freq"], acc["name"], acc["schedule"]["time"])


def _next_run(acc):
    job = scheduler.get_job(f"sync-{acc['id']}")
    return job.next_run_time.isoformat() if job and job.next_run_time else None


@app.on_event("startup")
def startup():
    log.info("CloudClone starting (data: %s, config: %s, redirect URI: %s)",
             store.DATA_DIR, store.CONFIG_DIR, REDIRECT_URI)
    runner.start_worker()
    scheduler.start()
    accounts = store.state()["accounts"].values()
    for acc in accounts:
        reschedule(acc)
    log.info("Loaded %d account(s)", len(accounts))


# ---- auth -----------------------------------------------------------------

class Password(BaseModel):
    password: str


@app.get("/api/session")
def session(request: Request):
    return {"configured": bool(store.state().get("password")), "authed": bool(request.session.get("auth"))}


@app.post("/api/setup")
def setup(body: Password, request: Request):
    if store.state().get("password"):
        raise HTTPException(400, "Already configured")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    store.set_password(body.password)
    log.info("Initial password set")
    request.session["auth"] = True
    return {"ok": True}


@app.post("/api/login")
def login(body: Password, request: Request):
    if not store.check_password(body.password):
        log.warning("Failed login attempt from %s", request.client.host if request.client else "unknown")
        raise HTTPException(401, "Wrong password")
    request.session["auth"] = True
    log.info("Login from %s", request.client.host if request.client else "unknown")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ---- accounts -------------------------------------------------------------

class Schedule(BaseModel):
    freq: str = "daily"      # daily | weekly | monthly
    time: str = "02:00"
    weekday: int = 6         # 0=Mon
    monthday: int = 1

    def validated(self):
        if self.freq not in ("daily", "weekly", "monthly"):
            raise HTTPException(400, "Invalid frequency")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", self.time):
            raise HTTPException(400, "Time must be HH:MM")
        if not 0 <= self.weekday <= 6 or not 1 <= self.monthday <= 28:
            raise HTTPException(400, "Invalid day")
        return self.model_dump()


class AccountIn(BaseModel):
    name: str
    client_id: str = ""        # blank = use the saved default client
    client_secret: str = ""
    save_as_default: bool = False
    shared_drive_id: str = ""


class AccountUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    shared_drive_id: str | None = None
    schedule: Schedule | None = None
    folders: list[str] | None = None


def _view(acc):
    out = {k: v for k, v in acc.items() if k != "client_secret"}
    out["next_run"] = _next_run(acc)
    out["destination"] = os.path.join(store.DATA_DIR, acc["folder"])
    return out


@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state():
    return {
        "accounts": [_view(a) for a in store.state()["accounts"].values()],
        "run": runner.status(),
        "redirect_uri": REDIRECT_URI,
        "direct_oauth": bool(PUBLIC_URL),
        "has_default_client": bool(store.state().get("oauth_client")),
        "disk": _disk(),
        "version": VERSION,
    }


def _disk():
    try:
        u = shutil.disk_usage(store.DATA_DIR)
        return {"free": u.free, "total": u.total}
    except OSError as e:
        log.warning("Could not read disk usage for %s: %s", store.DATA_DIR, e)
        return None


@app.post("/api/accounts", dependencies=[Depends(require_auth)])
def create_account(body: AccountIn):
    if not body.name.strip():
        raise HTTPException(400, "Name required")
    folder = store.slug(body.name)
    if any(a["folder"] == folder for a in store.state()["accounts"].values()):
        raise HTTPException(400, "An account with that name already exists")
    cid, secret = body.client_id.strip(), body.client_secret.strip()
    if not cid or not secret:
        default = store.state().get("oauth_client")
        if not default:
            raise HTTPException(400, "Client ID and Secret are required")
        cid, secret = default["client_id"], default["client_secret"]
    elif body.save_as_default:
        store.update(lambda s: s.__setitem__("oauth_client", {"client_id": cid, "client_secret": secret}))
    acc = store.new_account(body.name.strip(), cid, secret, body.shared_drive_id.strip())
    store.write_rclone_conf(acc)
    log.info("Account %s (%s) created", acc["id"], acc["name"])
    return _view(acc)


@app.patch("/api/accounts/{acc_id}", dependencies=[Depends(require_auth)])
def update_account(acc_id: str, body: AccountUpdate):
    acc = store.get_account(acc_id)
    if not acc:
        raise HTTPException(404)

    def apply(s):
        a = s["accounts"][acc_id]
        if body.enabled is not None:
            a["enabled"] = body.enabled
        if body.shared_drive_id is not None:
            a["shared_drive_id"] = body.shared_drive_id.strip()
        if body.schedule is not None:
            a["schedule"] = body.schedule.validated()
        if body.folders is not None:
            a["folders"] = sorted({f.strip("/") for f in body.folders if f.strip("/")})
        # The destination folder is fixed at creation so a rename never orphans backed-up files.
        if body.name:
            a["name"] = body.name.strip()
    store.update(apply)
    acc = store.get_account(acc_id)
    store.write_rclone_conf(acc)
    reschedule(acc)
    log.info("Account %s (%s) updated", acc_id, acc["name"])
    return _view(acc)


@app.get("/api/accounts/{acc_id}/folders", dependencies=[Depends(require_auth)])
def list_folders(acc_id: str, path: str = ""):
    """Sub-folders of `path` in the account's Drive (one level, for the lazy folder picker)."""
    acc = store.get_account(acc_id)
    if not acc or not acc["connected"]:
        raise HTTPException(400, "Connect the Google account first")
    try:
        p = subprocess.run(
            ["rclone", "lsjson", f"gdrive:{path.strip('/')}", "--dirs-only", "--config", store.conf_path(acc_id)],
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log.warning("Listing folders for account %s timed out", acc_id)
        raise HTTPException(504, "Google Drive took too long to respond")
    if p.returncode != 0:
        log.error("rclone lsjson failed for account %s (exit %s): %s", acc_id, p.returncode, p.stderr.strip())
        raise HTTPException(502, p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "rclone failed")
    return sorted((d["Name"] for d in json.loads(p.stdout)), key=str.lower)


@app.delete("/api/accounts/{acc_id}", dependencies=[Depends(require_auth)])
def delete_account(acc_id: str):
    st = runner.status()
    if (st["current"] or {}).get("account_id") == acc_id:
        raise HTTPException(400, "Cancel the running sync first")
    if scheduler.get_job(f"sync-{acc_id}"):
        scheduler.remove_job(f"sync-{acc_id}")
    store.delete_account(acc_id)  # backed-up files in /data are intentionally left in place
    log.info("Account %s deleted (backed-up files left in place)", acc_id)
    return {"ok": True}


# ---- Google OAuth (manual paste-back flow; Google blocks LAN-IP redirects) -

@app.get("/api/accounts/{acc_id}/auth-url", dependencies=[Depends(require_auth)])
def auth_url(acc_id: str, request: Request):
    acc = store.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    nonce = request.session["oauth_nonce"] = secrets.token_urlsafe(16)
    q = urlencode({
        "client_id": acc["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": f"{acc_id}.{nonce}",
    })
    return {"url": f"{AUTH_URL}?{q}"}


class AuthCode(BaseModel):
    code: str


@app.post("/api/accounts/{acc_id}/auth-code", dependencies=[Depends(require_auth)])
def auth_code(acc_id: str, body: AuthCode):
    code = body.code.strip()
    if code.startswith("http"):  # user pasted the whole failed-redirect URL
        qs = parse_qs(urlparse(code).query)
        if "error" in qs:
            raise HTTPException(400, f"Google returned: {qs['error'][0]}")
        code = (qs.get("code") or [""])[0]
    return _exchange(acc_id, code)


def _exchange(acc_id, code):
    acc = store.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    if not code:
        raise HTTPException(400, "No authorization code found")

    try:
        r = requests.post(TOKEN_URL, data={
            "code": code, "client_id": acc["client_id"], "client_secret": acc["client_secret"],
            "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
        }, timeout=30)
    except requests.RequestException as e:
        log.error("Could not reach Google to exchange the code for account %s: %s", acc_id, e)
        raise HTTPException(502, "Could not reach Google; try again")
    if r.status_code != 200:
        log.warning("Google rejected the authorization code for account %s (HTTP %s)", acc_id, r.status_code)
        raise HTTPException(400, f"Google rejected the code: {r.json().get('error_description', r.text)}")
    tok = r.json()
    if not tok.get("refresh_token"):
        log.warning("Google returned no refresh token for account %s", acc_id)
        raise HTTPException(400, "Google did not return a refresh token; remove the app's access at "
                                 "myaccount.google.com/permissions and try again")
    expiry = datetime.now(timezone.utc) + timedelta(seconds=tok.get("expires_in", 3600))
    token_json = json.dumps({
        "access_token": tok["access_token"], "token_type": "Bearer",
        "refresh_token": tok["refresh_token"], "expiry": expiry.isoformat(),
    })
    store.write_rclone_conf(acc, token_json)

    email = ""
    try:
        me = requests.get("https://www.googleapis.com/drive/v3/about?fields=user",
                          headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30).json()
        email = me.get("user", {}).get("emailAddress", "")
    except Exception as e:
        log.warning("Could not look up the Google email for account %s: %s", acc_id, e)

    def apply(s):
        s["accounts"][acc_id].update(connected=True, email=email)
    store.update(apply)
    reschedule(store.get_account(acc_id))
    log.info("Account %s connected to Google%s", acc_id, f" as {email}" if email else "")
    return _view(store.get_account(acc_id))


@app.get("/oauth/callback", dependencies=[Depends(require_auth)])
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    acc_id, _, nonce = state.partition(".")
    expected = request.session.pop("oauth_nonce", None)
    try:
        if error:
            log.warning("OAuth callback returned error for account %s: %s", acc_id, error)
            raise HTTPException(400, f"Google returned: {error}")
        if not expected or not secrets.compare_digest(nonce, expected):
            log.warning("OAuth callback with invalid state for account %s", acc_id)
            raise HTTPException(400, "Invalid OAuth state; start again from the app")
        _exchange(acc_id, code)
    except HTTPException as e:
        log.warning("OAuth callback failed for account %s: %s", acc_id, e.detail)
        return HTMLResponse(f"<p>Connecting failed: {e.detail}</p><p><a href='/'>Back</a></p>", status_code=e.status_code)
    return RedirectResponse("/")


# ---- runs -----------------------------------------------------------------

@app.post("/api/accounts/{acc_id}/run", dependencies=[Depends(require_auth)])
def run_now(acc_id: str):
    acc = store.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    if not acc["connected"]:
        raise HTTPException(400, "Connect the Google account first")
    err = runner.enqueue(acc_id, "manual")
    if err:
        log.warning("Manual run for account %s not queued: %s", acc_id, err)
        raise HTTPException(409, err)
    log.info("Manual run queued for account %s", acc_id)
    return {"ok": True}


@app.post("/api/cancel", dependencies=[Depends(require_auth)])
def cancel():
    cancelled = runner.cancel()
    log.info("Cancel requested (%s)", "sync stopping" if cancelled else "nothing running")
    return {"cancelled": cancelled}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
