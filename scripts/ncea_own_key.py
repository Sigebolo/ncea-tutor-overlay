"""Run a student's turn on the provider key they saved for themselves.

Why this exists (found on AU, 2026-11-03)
-----------------------------------------
The turn preparer already prefers a student's own key when it pins a
selection, but it pins *after* the partner runtime has resolved and installed
the turn's scoped LLM config from the deployment catalog. So the pin was
recorded and never used: a key saved against a local mock endpoint received
zero requests while the turn answered happily from the shared model, and the
usage readout stayed at 0 turns.

A second reason the preparer's pin cannot do this job here: a partner turn runs
as a *synthetic* user, so :func:`deeptutor.multi_user.paths._resolve_owner`
answers "the deployment" and the owner's catalog is never consulted. The owner
id therefore travels explicitly with the partner config.

What it does: resolves the owner's own profile straight from their own catalog
file, installs it as this turn's scoped LLM config, and returns the token so
the caller can reset it. The deployment model stays the fallback — a broken or
half-saved profile must never take a student's tutoring away.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Returned whenever the turn should just use the deployment model.
_NO_OWN_KEY: tuple[None, None] = (None, None)


def _owner_catalog_path(owner_id: str) -> Path | None:
    """The owner's own model catalog, or None for the deployment account."""
    from deeptutor.multi_user.models import LOCAL_ADMIN_ID
    from deeptutor.multi_user.paths import get_path_service_for_scope, scope_for_user

    if not owner_id or owner_id == LOCAL_ADMIN_ID:
        # An administrator's own catalog IS the shared one: an own-key profile
        # in it would silently become every account's provider.
        return None
    try:
        scope = scope_for_user(owner_id, is_admin=False)
        return Path(get_path_service_for_scope(scope).get_settings_file("model_catalog"))
    except Exception:  # noqa: BLE001 - an unreadable scope must not break a turn
        logger.warning("own-key: could not resolve the catalog for %s", owner_id, exc_info=True)
        return None


def _own_profile(owner_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    """The owner's first owner-bound profile that names at least one model.

    Returns the catalog too: the resolver loads the *deployment* catalog unless
    it is handed one, so the profile has to travel with the caller.
    """
    from deeptutor.multi_user.model_access import is_owner_bound

    path = _owner_catalog_path(owner_id)
    if path is None or not path.exists():
        return None
    try:
        from deeptutor.services.config.model_catalog import ModelCatalogService

        catalog = ModelCatalogService.get_instance(path).load()
    except Exception:  # noqa: BLE001
        logger.warning("own-key: unreadable catalog %s", path, exc_info=True)
        return None
    profiles = catalog.get("services", {}).get("llm", {}).get("profiles") or []
    for profile in profiles:
        if not isinstance(profile, dict) or not is_owner_bound(profile):
            continue
        if any(isinstance(m, dict) and m.get("id") for m in profile.get("models") or []):
            return path, profile, catalog
    return None


def own_key_scope(owner_id: str = "", pinned: Any = None):
    """Install the owner's own-key LLM config for this turn.

    ``pinned`` is the partner's configured selection: when the partner names a
    model deliberately, that decision stands and this does nothing.

    Returns ``(LLMConfig, token)`` on success, or ``(None, None)`` so the caller
    keeps the deployment model. The caller must pass ``token`` to
    ``reset_llm_selection`` when the turn ends.
    """
    if pinned:
        return _NO_OWN_KEY
    found = _own_profile(owner_id)
    if found is None:
        return _NO_OWN_KEY
    path, profile, catalog = found

    model = next(m for m in profile["models"] if isinstance(m, dict) and m.get("id"))
    try:
        from deeptutor.services.config.model_catalog import ModelCatalogService
        from deeptutor.services.config.provider_runtime import resolve_llm_runtime_config
        from deeptutor.services.llm.config import set_scoped_llm_config
        from deeptutor.services.model_selection.runtime import llm_config_from_resolved

        resolved = resolve_llm_runtime_config(
            catalog,
            service=ModelCatalogService.get_instance(path),
            llm_selection={"profile_id": profile.get("id"), "model_id": model.get("id")},
        )
        config = llm_config_from_resolved(resolved)
        if not config.model:
            raise ValueError("the saved profile resolved to no model")
    except Exception:  # noqa: BLE001
        logger.warning(
            "own-key: %s's profile did not resolve; using the deployment model",
            owner_id,
            exc_info=True,
        )
        return _NO_OWN_KEY

    token = set_scoped_llm_config(config)
    logger.info(
        "own-key: turn on %s / %s for %s (%s)",
        profile.get("id"),
        config.model,
        owner_id,
        (config.effective_url or config.base_url or "")[:80],
    )
    return config, token
