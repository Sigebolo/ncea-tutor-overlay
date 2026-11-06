"""Trial-expiry auto-disable: silent close (no reminders), enforced at login.

Owner decision 2026-09-16: Zac (9/16) + Elise (9/17) trial accounts close
automatically at expiry — no email/WeChat, chat history kept, revive is a
manual ops action. Covers:

  1. scripts/expiry_auto_disable.py date-gated flipping of users.json
     (before -> keep, on/after -> disable, already-disabled -> no-op,
      non-2026 year -> refuse to touch anything)
  2. the vendored enforcement: authenticate() and decode_token() refuse
     disabled accounts (1.6.7 stock carried the field but never checked it)

Run: .venv\\Scripts\\python.exe -m pytest tests/test_expiry_disable.py -q
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "expiry_auto_disable.py"

from deeptutor.services import auth as _auth

# The refusal is one of the overlay's patches: stock 1.7.0 carries the `disabled`
# field and never checks it, so on a plain install only the script half of this
# suite can run. Detected from the installed source, not assumed.
_PATCHED_AUTH = "refused for disabled account" in inspect.getsource(_auth)


@pytest.fixture(autouse=True)
def _needs_patched_auth(request):
    if request.node.name in {
        "test_authenticate_refuses_disabled",
        "test_decode_token_refuses_disabled",
    } and not _PATCHED_AUTH:
        pytest.skip(
            "needs the patched deeptutor (disabled accounts are refused at login)"
        )


def _load_script():
    spec = importlib.util.spec_from_file_location("expiry_auto_disable", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    """Isolated users.json for the script (script reads it via users_file())."""
    data = tmp_path / "data" / "system" / "auth"
    data.mkdir(parents=True)
    users = {
        "Zac": {"id": "u_zac", "hash": "x", "role": "user", "disabled": False},
        "Elise": {"id": "u_elise", "hash": "x", "role": "user", "disabled": False},
        "Allen": {"id": "u_allen", "hash": "x", "role": "user", "disabled": False},
    }
    (data / "users.json").write_text(json.dumps(users), encoding="utf-8")
    return data / "users.json"


_MODULE = None


def _mod():
    global _MODULE
    if _MODULE is None:
        _MODULE = _load_script()
    return _MODULE


def _run(store_path: Path, monkeypatch, iso_day: str) -> str:
    mod = _mod()
    monkeypatch.setattr(mod, "users_file", lambda: str(store_path))
    monkeypatch.setattr(sys, "argv", ["expiry_auto_disable.py", "--date", iso_day])
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main()
    assert rc == 0
    return buf.getvalue()


# --- script behaviour -------------------------------------------------------

def test_before_expiry_keeps_accounts(store: Path, monkeypatch) -> None:
    out = _run(store, monkeypatch, "2026-09-15")
    assert "keep Zac" in out and "keep Elise" in out
    users = json.loads(store.read_text(encoding="utf-8"))
    assert users["Zac"]["disabled"] is False
    assert users["Elise"]["disabled"] is False


def test_on_zac_expiry_disables_only_zac(store: Path, monkeypatch) -> None:
    out = _run(store, monkeypatch, "2026-09-16")
    assert "DISABLED Zac" in out
    assert "keep Elise" in out
    users = json.loads(store.read_text(encoding="utf-8"))
    assert users["Zac"]["disabled"] is True
    assert users["Elise"]["disabled"] is False
    assert users["Allen"]["disabled"] is False  # not a trial account


def test_after_both_expiry_all_disabled(store: Path, monkeypatch) -> None:
    _run(store, monkeypatch, "2026-09-18")
    users = json.loads(store.read_text(encoding="utf-8"))
    assert users["Zac"]["disabled"] is True
    assert users["Elise"]["disabled"] is True


def test_rerun_is_idempotent(store: Path, monkeypatch) -> None:
    _run(store, monkeypatch, "2026-09-18")
    out = _run(store, monkeypatch, "2026-09-18")
    assert "already disabled" in out and "DISABLED" not in out
    users = json.loads(store.read_text(encoding="utf-8"))
    assert users["Zac"]["disabled"] is True


def test_non_2026_year_is_a_noop(store: Path, monkeypatch) -> None:
    out = _run(store, monkeypatch, "2027-01-05")
    assert "doing nothing" in out
    users = json.loads(store.read_text(encoding="utf-8"))
    assert users["Zac"]["disabled"] is False


# --- vendored enforcement ----------------------------------------------------

def test_authenticate_refuses_disabled(monkeypatch, tmp_path: Path) -> None:
    import deeptutor.services.auth as sa

    monkeypatch.setattr(sa, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        sa, "_load_users",
        lambda: {"Zac": {"id": "u_zac", "hash": sa.hash_password("pw"),
                         "role": "user", "disabled": True},
                 "Allen": {"id": "u_allen", "hash": sa.hash_password("pw"),
                           "role": "user", "disabled": False}})
    assert sa.authenticate("Zac", "pw") is None
    assert sa.authenticate("Allen", "pw") is not None


def test_decode_token_refuses_disabled(monkeypatch) -> None:
    import deeptutor.services.auth as sa

    monkeypatch.setattr(sa, "AUTH_ENABLED", True)
    monkeypatch.setattr(sa, "AUTH_SECRET", "test-secret")
    token = sa.create_token("Zac", "user", "u_zac")
    assert token

    monkeypatch.setattr(
        sa, "_load_users",
        lambda: {"Zac": {"id": "u_zac", "hash": "x", "role": "user",
                         "disabled": True}})
    assert sa.decode_token(token) is None
