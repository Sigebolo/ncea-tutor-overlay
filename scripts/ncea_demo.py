"""Google sign-in and a 15-day trial for the public demo.

Installed as ``deeptutor/api/routers/ncea_demo.py`` (see apply_site_patches.py)
and mounted with no auth dependency: the whole point is that a student who has
never seen this app can sign in with Google, land in the tutor, and use it for
NCEA_DEMO_TRIAL_DAYS days.

Configuration is env-only, so a deploy never has to touch the database:

    NCEA_DEMO_GOOGLE_CLIENT_ID       OAuth 2.0 client id       (required)
    NCEA_DEMO_GOOGLE_CLIENT_SECRET   OAuth 2.0 client secret   (required)
    NCEA_DEMO_TRIAL_DAYS             trial length, default 15
    NCEA_DEMO_PUBLIC_URL             origin for the redirect, e.g.
                                     <your-public-host>

Without the two credentials the demo entry point is simply closed
(``/api/demo/config`` reports ``enabled: false``); nothing else changes.

Accounts made here are ordinary ``standard`` accounts with a random password
hash, so they cannot be password-logged-into: Google is the only door. The trial
ledger lives in ``data/system/ncea_demo/trials.json`` and is keyed by the
lowercased Google email. It is deliberately separate from the user record:
``save_user`` normalises its fields and would drop a trial column.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["demo"])

COOKIE_NAME = "dt_token"
STATE_COOKIE = "ncea_demo_state"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "openid email profile"


# --------------------------------------------------------------------------
# configuration and the trial ledger
# --------------------------------------------------------------------------


def _env(name: str) -> str:
    return str(os.getenv(name, "") or "").strip()


def client_id() -> str:
    return _env("NCEA_DEMO_GOOGLE_CLIENT_ID")


def client_secret() -> str:
    return _env("NCEA_DEMO_GOOGLE_CLIENT_SECRET")


def trial_days() -> int:
    try:
        return max(1, int(_env("NCEA_DEMO_TRIAL_DAYS") or 15))
    except ValueError:
        return 15


def demo_enabled() -> bool:
    return bool(client_id() and client_secret())


def _trial_path():
    from deeptutor.multi_user.paths import SYSTEM_ROOT

    return SYSTEM_ROOT / "ncea_demo" / "trials.json"


def _read_trials() -> dict[str, Any]:
    path = _trial_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # A corrupt ledger must not silently restart everyone's trial.
        logger.error("demo trial ledger unreadable at %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_trials(data: dict[str, Any]) -> None:
    from deeptutor.services.file_io import atomic_write_text

    path = _trial_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def trial_record(username: str) -> dict[str, Any] | None:
    return _read_trials().get(username.strip().lower())


def trial_state(username: str) -> dict[str, Any] | None:
    """Remaining-days view for one demo account, or ``None`` if not a demo one."""
    record = trial_record(username)
    if not record:
        return None
    ends = str(record.get("trial_ends_at") or "")
    try:
        end_dt = datetime.fromisoformat(ends)
    except ValueError:
        return {"trial_ends_at": ends, "days_left": 0, "expired": True}
    left = end_dt - _now()
    return {
        "trial_ends_at": ends,
        "days_left": max(0, left.days),
        "seconds_left": max(0, int(left.total_seconds())),
        "expired": left.total_seconds() <= 0,
    }


def ensure_trial_active() -> None:
    """FastAPI dependency: refuse NEW turns once a demo trial has run out.

    Non-demo accounts (the operator's own logins, school accounts) have no
    ledger row and are untouched. History stays readable for an expired demo
    account on purpose — the ask was "can still sign in, cannot send".
    """
    from deeptutor.multi_user.context import get_current_user

    try:
        user = get_current_user()
    except Exception:  # noqa: BLE001 - no request context: not a demo turn
        return
    username = str(getattr(user, "username", "") or "")
    if not username:
        return
    state = trial_state(username)
    if state and state.get("expired"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "trial_expired",
                "message": (
                    "Your free trial has ended. You can still read your history, "
                    "but new questions need a subscription."
                ),
                "trial_ends_at": state.get("trial_ends_at"),
            },
        )


# --------------------------------------------------------------------------
# Google OAuth (authorization-code flow, no SDK)
# --------------------------------------------------------------------------


def _origin(request: Request) -> str:
    configured = _env("NCEA_DEMO_PUBLIC_URL").rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _redirect_uri(request: Request) -> str:
    return f"{_origin(request)}/api/demo/google/callback"


def _decode_id_token(id_token: str) -> dict[str, Any]:
    """Read the claims out of the id_token Google just handed us over TLS."""
    parts = id_token.split(".")
    if len(parts) < 2:
        raise ValueError("malformed id_token")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode()))


def _post_form(url: str, fields: dict[str, str], timeout: int = 20) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _upsert_demo_user(email: str) -> tuple[str, str, str]:
    """Create the account on first sign-in; return ``(user_id, role, username)``.

    The stored password hash is a random secret nobody holds, so Google is the
    only way in even though the account lives in the normal user store.
    """
    from deeptutor.multi_user import identity
    from deeptutor.multi_user.identity import load_users, save_user
    from deeptutor.services.auth import hash_password

    username = email.strip().lower()
    users = load_users() or {}
    if username not in users:
        record = save_user(
            username, hash_password(secrets.token_urlsafe(32)), role="user", preset="standard"
        )
        # save_user promotes the FIRST account in an empty store to admin - a
        # bootstrap-safety rule. A public demo sign-up must never inherit it:
        # anyone on the internet would become the operator. Measured on AU
        # (users.json empty), where the first Google sign-in came back admin.
        if str(record.get("role") or "") == "admin":
            record["role"] = "user"
            with identity._USERS_WRITE_LOCK:
                users = load_users() or {}
                users[username] = record
                identity._write_users(users)
            logger.warning(
                "demo: demoted %s to 'user' - a demo sign-up must never be admin", username
            )
        else:
            users = load_users() or {}
        logger.info("demo: first Google sign-in created account %s", username)
    return str(users[username]["id"]), str(users[username]["role"]), username


def _settings_path(*parts: str):
    from deeptutor.runtime.home import get_runtime_home

    return get_runtime_home() / "data" / "user" / "settings" / "/".join(parts)


def _active_llm() -> tuple[str, str]:
    """The catalog's active llm profile/model, or ``("", "")``."""
    try:
        catalog = json.loads(_settings_path("model_catalog.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "", ""
    llm = ((catalog.get("services") or {}).get("llm")) or {}
    profile = str(llm.get("active_profile_id") or "")
    model = str(llm.get("active_model_id") or "")
    if not profile or not model:
        profiles = llm.get("profiles") or []
        if profiles and isinstance(profiles[0], dict):
            profile = profile or str(profiles[0].get("id") or "")
            models = profiles[0].get("models") or []
            if models and isinstance(models[0], dict):
                model = model or str(models[0].get("id") or "")
    return profile, model


def _ncea_knowledge_bases() -> list[dict[str, str]]:
    """Every shipped NZQA knowledge base, as grant references."""
    from deeptutor.runtime.home import get_runtime_home

    root = get_runtime_home() / "data" / "knowledge_bases"
    if not root.is_dir():
        return []
    names = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("NZQA_"))
    return [{"id": f"admin:kb:{name}", "name": name} for name in names]


def _provision_demo_workspace(user_id: str) -> None:
    """First sign-in: make the demo usable without an operator.

    A grant is an admin write, and on a box whose user store starts empty
    nobody can perform one — yet without it a new student gets no knowledge
    base, no ``ncea-tutor`` skill and no model, i.e. an empty workspace that
    cannot answer an NCEA question. This writes the same grant an operator
    would, server-side, once per account.
    """
    from deeptutor.multi_user.grants import load_grant, save_grant

    if (load_grant(user_id).get("knowledge_bases") or []):
        return  # already provisioned (or an operator has tuned it since)
    profile, model = _active_llm()
    grant: dict[str, Any] = {
        "knowledge_bases": _ncea_knowledge_bases(),
        "skills": [{"skill_id": "ncea-tutor"}],
        "models": {"llm": ([{"profile_id": profile, "model_ids": [model]}] if profile and model else [])},
        "exec_enabled": False,
    }
    save_grant(user_id, grant)
    logger.info(
        "demo: provisioned %d KBs, skills=%s, model=%s/%s",
        len(grant["knowledge_bases"]), grant["skills"], profile, model,
    )


def _start_trial(username: str) -> None:
    data = _read_trials()
    key = username.strip().lower()
    if key in data:
        return
    started = _now()
    data[key] = {
        "created_at": started.isoformat(),
        "trial_ends_at": (started + timedelta(days=trial_days())).isoformat(),
    }
    _write_trials(data)


@router.get("/api/demo/config")
async def demo_config(request: Request) -> dict[str, Any]:
    """What the demo page needs to decide whether to offer the button."""
    return {
        "enabled": demo_enabled(),
        "trial_days": trial_days(),
        "login_url": f"{_origin(request)}/api/demo/google/start",
    }


@router.get("/api/demo/trial")
async def demo_trial() -> dict[str, Any]:
    """Remaining trial time for the signed-in demo student."""
    from deeptutor.multi_user.context import get_current_user

    try:
        username = str(getattr(get_current_user(), "username", "") or "")
    except Exception:  # noqa: BLE001
        username = ""
    state = trial_state(username) if username else None
    return {"is_demo": state is not None, "trial": state, "trial_days": trial_days()}


@router.get("/api/demo/google/start")
async def google_start(request: Request):
    if not demo_enabled():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": _redirect_uri(request),
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
            "prompt": "select_account",
            "access_type": "online",
        }
    )
    response = RedirectResponse(f"{_AUTH_URL}?{query}", status_code=302)
    response.set_cookie(
        key=STATE_COOKIE,
        value=state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=_origin(request).startswith("https://"),
    )
    return response


@router.get("/api/demo/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/demo?error={urllib.parse.quote(error)}", status_code=302)
    if not code:
        raise HTTPException(status_code=400, detail="missing authorization code")
    expected = request.cookies.get(STATE_COOKIE) or ""
    if not expected or not secrets.compare_digest(expected, state or ""):
        raise HTTPException(status_code=400, detail="state mismatch — restart the sign-in")

    try:
        tokens = _post_form(
            _TOKEN_URL,
            {
                "code": code,
                "client_id": client_id(),
                "client_secret": client_secret(),
                "redirect_uri": _redirect_uri(request),
                "grant_type": "authorization_code",
            },
        )
        claims = _decode_id_token(str(tokens.get("id_token") or ""))
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("demo: Google token exchange failed: %s", exc)
        return RedirectResponse("/demo?error=google_exchange_failed", status_code=302)

    email = str(claims.get("email") or "")
    if not email or claims.get("email_verified") is False:
        return RedirectResponse("/demo?error=email_not_verified", status_code=302)

    user_id, role, username = _upsert_demo_user(email)
    _start_trial(username)
    try:
        _provision_demo_workspace(user_id)
    except Exception:  # noqa: BLE001 - a broken grant must not block sign-in
        logger.warning("demo: workspace provisioning failed for %s", username, exc_info=True)

    from deeptutor.services.auth import create_token

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_token(username, role, user_id),
        max_age=12 * 3600,
        httponly=True,
        samesite="lax",
        secure=_origin(request).startswith("https://"),
    )
    response.delete_cookie(STATE_COOKIE)
    logger.info("demo: %s signed in with Google", username)
    return response


_DEMO_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NCEA Tutor — free {days}-day demo</title>
<style>
 body{{margin:0;font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;min-height:100vh;align-items:center;justify-content:center}}
 .card{{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:40px;max-width:460px;width:90%}}
 h1{{font-size:24px;margin:0 0 8px}} p{{color:#94a3b8;margin:0 0 24px}}
 a.btn{{display:flex;align-items:center;justify-content:center;gap:10px;background:#fff;color:#1f2937;
        text-decoration:none;font-weight:600;padding:12px 18px;border-radius:10px}}
 a.btn:hover{{background:#f1f5f9}} .note{{font-size:13px;color:#64748b;margin-top:18px}}
 .err{{background:#7f1d1d;color:#fecaca;padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:14px}}
</style></head><body><div class="card">
 <h1>NCEA Tutor demo</h1>
 <p>Sign in with Google and use the tutor free for {days} days. No card, no setup.</p>
 {error_block}
 {body}
 <p class="note">We store your Google email as your account name and nothing else.</p>
</div></body></html>"""


@router.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request, error: str = "") -> HTMLResponse:
    days = trial_days()
    if not demo_enabled():
        body = "<p><strong>Demo sign-in is not configured yet.</strong> Ask the operator to set the Google OAuth credentials.</p>"
    else:
        body = '<a class="btn" href="/api/demo/google/start">Continue with Google</a>'
    error_block = f'<div class="err">Sign-in failed: {error}</div>' if error else ""
    return HTMLResponse(
        _DEMO_HTML.format(days=days, body=body, error_block=error_block)
    )


__all__ = ["router", "ensure_trial_active", "trial_state", "trial_days", "demo_enabled"]
