"""A student's own provider key — stored in their OWN catalog, never shared.

One deployment key pays for every turn, so a student who already has their own
provider account (OpenAI, DeepSeek, NVIDIA, a school's Azure deployment…) has
no way to use it here: the settings page that edits provider profiles is
administrator-only, and it writes the shared catalog. This router gives an
ordinary signed-in student one place to paste a key of their own.

Where the key lives is the whole safety story. The profile is written to the
caller's own model catalog (``data/users/<uid>/settings/model_catalog.json``
via :func:`deeptutor.multi_user.personal_models.owner_catalog_service`), marked
``owner_bound`` so it is never lent to another account through grants and never
appears in the administrator's model list. The runtime already merges
owner-bound profiles from the owner's catalog on the resolution path
(:func:`~deeptutor.multi_user.personal_models.merge_personal_llm_profiles`), so
no new resolution code is needed — only the write surface and the "prefer my
own key" ordering in the turn preparer.

Admins are refused on purpose: their own catalog IS the shared one, so a key
saved here would silently become everyone's provider. They have Settings.

Endpoints (absolute paths, the router is mounted with no prefix):

    GET    /api/ncea/personal-key          what is configured (key masked)
    PUT    /api/ncea/personal-key          save / replace the key + models
    DELETE /api/ncea/personal-key          remove it
    POST   /api/ncea/personal-key/models   list the provider's model ids
    GET    /my-key                         the page a student actually uses
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["personal-key"])

# The auth dependency is not decoration: it is what puts the signed-in user in
# the request context. Without it ``get_current_user()`` falls back to the
# LOCAL ADMIN, so every student was told "an administrator's own catalog IS the
# shared one" and the page could never save anything (caught on AU while
# deploying, 2026-11-02). Guarded so a tree without the router still imports.
try:
    from deeptutor.api.routers.auth import require_auth as _require_auth

    _AUTH = [Depends(_require_auth)]
except Exception:  # pragma: no cover
    _AUTH = []

PROFILE_PREFIX = "llm-profile-own-key"
KEEP = "***"  # what GET returns for a stored key, and PUT accepts to keep it


class OwnKeyPayload(BaseModel):
    base_url: str = ""
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    name: str = ""


class ModelListPayload(BaseModel):
    base_url: str = ""
    api_key: str = ""


def _user():
    from deeptutor.multi_user.context import get_current_user

    return get_current_user()


def _catalog():
    from deeptutor.multi_user.personal_models import owner_catalog_service

    return owner_catalog_service()


def _profile_id(user_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", str(user_id).lower())[:16] or "me"
    return f"{PROFILE_PREFIX}-{slug}"


def _models_block(model_ids: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in model_ids:
        model = str(raw or "").strip()
        if not model:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-") or "model"
        out.append({"id": f"m-own-{slug}", "model": model, "name": model})
    return out


def _profile(catalog: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    pid = _profile_id(user_id)
    for item in catalog.get("services", {}).get("llm", {}).get("profiles", []) or []:
        if isinstance(item, dict) and str(item.get("id") or "") == pid:
            return item
    return None


def _public(profile: dict[str, Any] | None) -> dict[str, Any]:
    if not profile:
        return {"configured": False, "base_url": "", "name": "", "api_key": "", "models": []}
    models = [
        str(model.get("model") or "")
        for model in profile.get("models", []) or []
        if isinstance(model, dict) and model.get("model")
    ]
    return {
        "configured": True,
        "base_url": str(profile.get("base_url") or ""),
        "name": str(profile.get("name") or ""),
        "api_key": KEEP if profile.get("api_key") else "",
        "models": models,
    }


def _require_student() -> tuple[str, bool]:
    user = _user()
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in first.")
    if getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=403,
            detail=(
                "An administrator's own catalog IS the shared one. "
                "Add the provider under Settings → Model providers instead."
            ),
        )
    return user_id, bool(getattr(user, "is_admin", False))


def _usage_totals(user_id: str, since: float) -> dict[str, Any]:
    """Token spend recorded for this student's own turns.

    Every turn the loop finishes writes a ``cost_summary`` into its ``result``
    event, so the ledger is already there - this only reads it. ``since`` scopes
    the number to the life of the saved key: a student who brought their own
    provider wants to see what THEY have spent, not the account's whole history
    on the shared key. Read-only, and never allowed to break the page.
    """
    empty: dict[str, Any] = {
        "turns": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usd": 0.0,
    }
    try:
        # scope_for_user resolves the path without creating any directories,
        # so opening this page as a student does not mkdir their workspace.
        from deeptutor.multi_user.paths import scope_for_user

        db = scope_for_user(user_id, is_admin=False).root / "user" / "chat_history.db"
        if not db.is_file():
            return empty
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT timestamp, metadata_json FROM turn_events "
                "WHERE type = 'result'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a usage read must never fail the page
        logger.warning("own-key usage read failed", exc_info=True)
        return empty

    out = dict(empty)
    for ts, raw in rows:
        try:
            if since and float(ts or 0) < since:
                continue
            summary = (json.loads(raw or "{}").get("metadata") or {}).get("cost_summary")
        except Exception:  # noqa: BLE001 - one bad row must not hide the rest
            continue
        if not isinstance(summary, dict):
            continue
        out["turns"] += 1
        out["prompt_tokens"] += int(summary.get("prompt_tokens") or 0)
        out["completion_tokens"] += int(summary.get("completion_tokens") or 0)
        out["total_tokens"] += int(summary.get("total_tokens") or 0)
        out["usd"] += float(summary.get("total_cost_usd") or 0)
    out["usd"] = round(out["usd"], 6)
    return out


@router.get("/api/ncea/personal-key/usage", dependencies=_AUTH)
async def own_key_usage() -> dict[str, Any]:
    """What this student's turns have cost since they saved their key."""
    user_id, _ = _require_student()
    profile = _profile(_catalog().load(), user_id)
    since = float((profile or {}).get("usage_since") or 0)
    totals = _usage_totals(user_id, since)
    models = [str(m.get("model") or "") for m in (profile or {}).get("models") or [] if m.get("model")]
    return {
        **totals,
        "configured": bool(profile),
        "model": models[0] if models else "",
        # ISO only when the student actually has a key; an absent key means the
        # shared model, and the page says so instead of implying ownership.
        "since": (
            datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds")
            if since
            else None
        ),
    }


def _clean_base_url(value: str) -> str:
    base_url = str(value or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", base_url):
        raise HTTPException(
            status_code=400,
            detail="The base URL must start with https:// (for example https://api.openai.com/v1).",
        )
    return base_url


@router.get("/api/ncea/personal-key", dependencies=_AUTH)
async def get_own_key() -> dict[str, Any]:
    user_id, _ = _require_student()
    try:
        profile = _profile(_catalog().load(), user_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken catalog must not 500 the page
        logger.warning("own-key read failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not read your settings: {exc}") from exc
    return _public(profile)


@router.put("/api/ncea/personal-key", dependencies=_AUTH)
async def save_own_key(payload: OwnKeyPayload) -> dict[str, Any]:
    user_id, _ = _require_student()
    base_url = _clean_base_url(payload.base_url)
    models = _models_block(payload.models)
    if not models:
        raise HTTPException(
            status_code=400,
            detail="Add at least one model id, for example gpt-4o-mini or deepseek-chat.",
        )
    service = _catalog()
    previous = _profile(service.load(), user_id)
    api_key = str(payload.api_key or "").strip()
    if api_key == KEEP:
        api_key = str((previous or {}).get("api_key") or "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Paste your provider API key.")

    # Usage is scoped to the life of this key, so the readout answers "what has
    # MY key cost me". Re-saving must not reset it - only a first save (or a
    # save after the key was removed) starts the clock.
    previous_since = float((previous or {}).get("usage_since") or 0) if previous else 0.0
    profile = {
        "usage_since": previous_since or time.time(),
        "id": _profile_id(user_id),
        "name": str(payload.name or "").strip() or "My own key",
        "binding": "openai",
        "base_url": base_url,
        "api_key": api_key,
        "api_version": "",
        "extra_headers": {},
        "api_format": "auto",
        "wire_api": "auto",
        "models": models,
        # Not an OAuth login, but still one person's credential paid for by one
        # person: flagged so it is never granted to another account and stays
        # visible only inside this user's own scope.
        "owner_bound": True,
        "source": "personal",
    }
    pid = profile["id"]

    def _write(catalog: dict[str, Any]) -> None:
        llm = catalog.setdefault("services", {}).setdefault("llm", {})
        profiles = [item for item in llm.get("profiles", []) if not (
            isinstance(item, dict) and str(item.get("id") or "") == pid
        )]
        profiles.append(profile)
        llm["profiles"] = profiles

    saved = service.update(_write)
    logger.info("own-key: %s saved profile %s (%d models)", user_id, pid, len(models))
    try:
        from deeptutor.services.config import get_model_catalog_service

        # The runtime resolves against the shared catalog with this user's
        # profiles merged in; dropping the cached instance is what makes the
        # new profile visible to the next turn without a restart.
        get_model_catalog_service().load()
    except Exception:  # noqa: BLE001 - cache warming is best effort
        logger.debug("own-key: catalog cache warm skipped", exc_info=True)
    return {"saved": True, **_public(_profile(saved, user_id))}


@router.delete("/api/ncea/personal-key", dependencies=_AUTH)
async def delete_own_key() -> dict[str, Any]:
    user_id, _ = _require_student()
    service = _catalog()
    pid = _profile_id(user_id)

    def _drop(catalog: dict[str, Any]) -> None:
        llm = catalog.setdefault("services", {}).setdefault("llm", {})
        llm["profiles"] = [
            item
            for item in llm.get("profiles", [])
            if not (isinstance(item, dict) and str(item.get("id") or "") == pid)
        ]
        if str(llm.get("active_profile_id") or "") == pid:
            llm["active_profile_id"] = None
            llm["active_model_id"] = None

    service.update(_drop)
    logger.info("own-key: %s removed profile %s", user_id, pid)
    return {"removed": True, "configured": False}


@router.post("/api/ncea/personal-key/models", dependencies=_AUTH)
async def list_own_key_models(payload: ModelListPayload) -> dict[str, Any]:
    """Ask the provider which model ids the key can see."""
    _require_student()
    base_url = _clean_base_url(payload.base_url)
    api_key = str(payload.api_key or "").strip()
    if api_key == KEEP:
        user_id = str(getattr(_user(), "id", "") or "")
        api_key = str((_profile(_catalog().load(), user_id) or {}).get("api_key") or "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Paste your provider API key first.")
    from deeptutor.services.llm.factory import fetch_models

    try:
        model_ids = await fetch_models("openai", base_url, api_key, "auto")
    except Exception as exc:  # noqa: BLE001 - surface the provider's own error
        raise HTTPException(status_code=502, detail=f"The provider refused that key: {exc}") from exc
    return {"models": [{"id": str(m), "name": str(m)} for m in model_ids]}


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Use your own API key</title>
<style>
 body{margin:0;font:16px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;justify-content:center;padding:32px 16px}
 .card{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:28px;max-width:560px;width:100%}
 h1{font-size:22px;margin:0 0 6px} p.sub{color:#94a3b8;margin:0 0 22px;font-size:14px}
 label{display:block;font-size:13px;color:#cbd5e1;margin:14px 0 6px}
 input,textarea{width:100%;box-sizing:border-box;background:#0f172a;color:#e2e8f0;border:1px solid #334155;
      border-radius:8px;padding:10px;font:14px/1.4 ui-monospace,Menlo,monospace}
 textarea{min-height:84px}
 .row{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}
 button{cursor:pointer;border:0;border-radius:9px;padding:11px 16px;font-weight:600;font-size:14px}
 .primary{background:#38bdf8;color:#082f49} .ghost{background:#334155;color:#e2e8f0}
 .danger{background:#7f1d1d;color:#fecaca}
 #msg{margin-top:16px;font-size:14px;min-height:20px;color:#a5b4fc}
 code{background:#0f172a;padding:1px 5px;border-radius:4px}
 .usage{margin-top:18px;padding:14px;background:#0f172a;border:1px solid #334155;
       border-radius:10px;font-size:13.5px;color:#cbd5e1;line-height:1.7}
 .usage b{color:#e2e8f0}
 .usage .hint{color:#64748b}
</style></head><body><div class="card">
 <h1>Use your own API key</h1>
 <p class="sub">Your key is stored in your own settings, never shared, and never shown back to you.
    The tutor will use it for your turns instead of the shared one.</p>
 <label for="base_url">Base URL</label>
 <input id="base_url" placeholder="https://api.openai.com/v1">
 <label for="api_key">API key</label>
 <input id="api_key" type="password" placeholder="sk-...">
 <label for="models">Model ids (one per line)</label>
 <textarea id="models" placeholder="gpt-4o-mini"></textarea>
 <div class="row">
   <button class="primary" onclick="save()">Save key</button>
   <button class="ghost" onclick="fetchModels()">Fetch models</button>
   <button class="danger" onclick="remove()">Remove key</button>
 </div>
 <div id="msg"></div>
 <div id="usage"></div>
 <p class="sub" style="margin-top:22px">Need help? Ask your teacher. Keys are per student: sharing one
    between two accounts breaks the one-window rule and gets both refused.</p>
</div>
<script>
 const msg = (t, bad) => { const el = document.getElementById('msg');
   el.textContent = t; el.style.color = bad ? '#fca5a5' : '#a5b4fc'; };
 const val = id => document.getElementById(id).value.trim();

 async function load() {
   try {
     const r = await fetch('/api/ncea/personal-key', {credentials: 'same-origin'});
     if (r.status === 401) { msg('Sign in first, then open this page again.', true); return; }
     const d = await r.json();
     if (d.configured) {
       document.getElementById('base_url').value = d.base_url || '';
       document.getElementById('api_key').value = d.api_key || '';
       document.getElementById('models').value = (d.models || []).join('\\n');
       msg('A key is saved for your account.');
     }
   } catch (e) { msg('Could not load your settings: ' + e, true); }
 }

 async function save() {
   const models = val('models').split('\\n').map(s => s.trim()).filter(Boolean);
   const r = await fetch('/api/ncea/personal-key', {
     method: 'PUT', credentials: 'same-origin',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({base_url: val('base_url'), api_key: val('api_key'), models})
   });
   const d = await r.json().catch(() => ({}));
   msg(r.ok ? 'Saved. Your next turn uses your key.' : (d.detail || 'Save failed.'), !r.ok);
 }

 async function fetchModels() {
   msg('Asking the provider…');
   const r = await fetch('/api/ncea/personal-key/models', {
     method: 'POST', credentials: 'same-origin',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({base_url: val('base_url'), api_key: val('api_key')})
   });
   const d = await r.json().catch(() => ({}));
   if (!r.ok) { msg(d.detail || 'The provider refused that key.', true); return; }
   const ids = (d.models || []).map(m => m.id);
   document.getElementById('models').value = ids.join('\\n');
   msg('Found ' + ids.length + ' models — pick the ones you want (delete the rest).');
 }

 async function remove() {
   const r = await fetch('/api/ncea/personal-key', {method: 'DELETE', credentials: 'same-origin'});
   msg(r.ok ? 'Removed. Your turns use the shared model again.' : 'Remove failed.', !r.ok);
   usage();
 }

 // Usage is informational: it never blocks the form, and it says nothing the
 // student cannot verify - the numbers come from their own turn ledger.
 async function usage() {
   const el = document.getElementById('usage');
   try {
     const r = await fetch('/api/ncea/personal-key/usage', {credentials: 'same-origin'});
     if (r.status === 401 || r.status === 403) return;
     const d = await r.json();
     if (r.status !== 200) { el.innerHTML = ''; return; }
     const n = x => Number(x || 0).toLocaleString();
     const since = d.since
       ? 'since ' + d.since.slice(0, 10)
       : 'all time';
     const who = d.configured
       ? 'on your own key' + (d.model ? ' (' + d.model + ')' : '')
       : '<span class="hint">on the shared model — add a key below to spend your own</span>';
     el.innerHTML = '<b>Your usage, ' + since + '</b><br>' +
       n(d.total_tokens) + ' tokens across ' + d.turns + ' turn' + (d.turns === 1 ? '' : 's') +
       ' <span class="hint">(' + n(d.prompt_tokens) + ' in / ' + n(d.completion_tokens) + ' out)</span><br>' +
       'about <b>$' + Number(d.usd || 0).toFixed(4) + '</b> ' + who;
   } catch (e) { /* leave the box empty rather than alarm the student */ }
 }
 load().then(usage);
</script></body></html>"""


@router.get("/my-key", response_class=HTMLResponse)
async def own_key_page() -> HTMLResponse:
    return HTMLResponse(_PAGE)


__all__ = ["router", "PROFILE_PREFIX", "_profile_id", "_models_block"]
