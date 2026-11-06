"""Hermetic home for the overlay's tests.

Several suites drive deeptutor code that resolves the model catalog and the
admin workspace through ``DEEPTUTOR_HOME``. Without one, they read whatever
deployment happens to sit next to the checkout, or fail with
``LLMConfigError: No active LLM model is configured``.

This conftest provisions a throwaway home *before* anything imports deeptutor,
so a fresh clone runs the whole suite with no configuration of its own. The
catalog points at a dead local port on purpose: no test may spend a real API
call, and a test that accidentally reaches the network fails loudly.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="ncea-overlay-"))
USER_DIR = HOME / "data" / "user"


def _write_catalog(user_dir: Path) -> None:
    settings = user_dir / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    catalog = {
        "version": 1,
        "connections": [
            {
                "id": "conn-test",
                "provider": "openai_compatible",
                "name": "Test endpoint (never called)",
                "api_key": "sk-test-not-a-real-key",
                "base_url": "http://127.0.0.1:9/v1",
                "api_version": "",
                "extra_headers": {},
            }
        ],
        "services": {
            "llm": {
                "active_profile_id": "llm-profile-test",
                "active_model_id": "m-test",
                "profiles": [
                    {
                        "id": "llm-profile-test",
                        "name": "Test profile",
                        "connection_id": "conn-test",
                        "models": [
                            {"id": "m-test", "model": "test-model", "name": "Test model"}
                        ],
                    }
                ],
            }
        },
    }
    (settings / "model_catalog.json").write_text(
        json.dumps(catalog, indent=2), encoding="utf-8"
    )


def _install_skill(user_dir: Path) -> None:
    """Stage the shipped skill where the admin workspace expects it."""
    src = REPO / "skills" / "ncea-tutor"
    if not src.is_dir():
        return
    dst = user_dir / "workspace" / "skills" / "ncea-tutor"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


_write_catalog(USER_DIR)
_install_skill(USER_DIR)
os.environ.setdefault("DEEPTUTOR_HOME", str(HOME))
