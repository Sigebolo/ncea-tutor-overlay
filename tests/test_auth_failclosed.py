"""Fail-closed regression tests for the auth-store corruption fixes.

Covers the vendored patches in scripts/patches/ (deeptutor 1.6.4):
  - identity._read_json(strict=True): corrupt users.json raises
    CorruptUserStoreError instead of reading as "no users" (which would open
    the first-registration admin-promotion gate and let the next save
    permanently overwrite the damaged file).
  - grants.load_grant: corrupt grant file raises CorruptGrantError instead of
    silently serving an empty grant (which strips the student's KBs/models/
    skill with no error surfaced).
  - grants.save_grant: writes atomically (no half-written file on crash).

Run: .venv\\Scripts\\python.exe -m pytest tests/ -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeptutor.multi_user import grants, identity


@pytest.fixture()
def iso_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user store + grant dir at an isolated tmp tree."""
    auth_dir = tmp_path / "system" / "auth"
    grants_dir = tmp_path / "system" / "grants"
    auth_dir.mkdir(parents=True)
    grants_dir.mkdir(parents=True)
    monkeypatch.setattr(identity, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(identity, "USERS_FILE", auth_dir / "users.json")
    monkeypatch.setattr(grants, "GRANTS_DIR", grants_dir)
    monkeypatch.setattr(grants, "ensure_system_dirs", lambda: None)
    return tmp_path


def _write_users(users_file: Path, users: dict) -> None:
    users_file.write_text(json.dumps(users), encoding="utf-8")


# --- user store -----------------------------------------------------------

def test_missing_users_file_reads_empty(iso_dirs: Path) -> None:
    """Fresh boot with no store must still work (no raise)."""
    assert identity.load_users() == {}


def test_corrupt_users_file_raises(iso_dirs: Path) -> None:
    identity.USERS_FILE.write_text("{not valid json\x00", encoding="utf-8")
    with pytest.raises(identity.CorruptUserStoreError):
        identity.load_users()


def test_non_object_users_file_raises(iso_dirs: Path) -> None:
    identity.USERS_FILE.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(identity.CorruptUserStoreError):
        identity.load_users()


def test_corrupt_store_blocks_save_and_preserves_bytes(iso_dirs: Path) -> None:
    """save_user must not overwrite a damaged store (recovery stays possible)."""
    damaged = b"{half-written\x00garbage"
    identity.USERS_FILE.write_bytes(damaged)
    with pytest.raises(identity.CorruptUserStoreError):
        identity.save_user("mallory", "somehash")
    assert identity.USERS_FILE.read_bytes() == damaged


def test_bad_entries_still_canonicalized_not_raised(iso_dirs: Path) -> None:
    """Guards against over-strictness: junk *entries* are dropped, file kept."""
    _write_users(
        identity.USERS_FILE,
        {"alice": {"id": "u_alice", "hash": "x", "role": "admin"}, "bob": 123},
    )
    users = identity.load_users()
    assert "alice" in users
    assert "bob" not in users


# --- grants ---------------------------------------------------------------

def test_missing_grant_is_empty(iso_dirs: Path) -> None:
    grant = grants.load_grant("u_nobody")
    assert grant["user_id"] == "u_nobody"
    assert grant["knowledge_bases"] == []


def test_corrupt_grant_raises(iso_dirs: Path) -> None:
    (grants.GRANTS_DIR / "u_s1.json").write_text("{\"kbs\": ", encoding="utf-8")
    with pytest.raises(grants.CorruptGrantError):
        grants.load_grant("u_s1")


def test_non_object_grant_raises(iso_dirs: Path) -> None:
    (grants.GRANTS_DIR / "u_s2.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(grants.CorruptGrantError):
        grants.load_grant("u_s2")


def test_save_grant_round_trip_is_valid_json(iso_dirs: Path) -> None:
    _write_users(
        identity.USERS_FILE,
        {"alice": {"id": "u_test1", "hash": "x", "role": "user", "preset": "standard"}},
    )
    saved = grants.save_grant(
        "u_test1",
        {
            "models": {"llm": [{"profile_id": "p", "model_ids": ["m"]}]},
            "knowledge_bases": [{"resource_id": "admin:kb:K"}],
            "skills": [{"skill_id": "ncea-tutor"}],
        },
    )
    assert saved["knowledge_bases"] == [{"resource_id": "admin:kb:K"}]
    raw = (grants.GRANTS_DIR / "u_test1.json").read_text(encoding="utf-8")
    assert json.loads(raw)["user_id"] == "u_test1"  # complete file, parses
    assert grants.load_grant("u_test1")["skills"] == [{"skill_id": "ncea-tutor"}]
