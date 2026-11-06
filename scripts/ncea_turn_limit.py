"""One live turn per account — the single-window rule.

Every turn on this box is paid for by ONE shared provider key, so two browser
windows are two concurrent agent loops: double the tokens, and the student
reads whichever answer happens to finish first. The per-session lock in
``PartnerRuntime`` cannot stop that — a second window opens a *different*
session key and never meets that lock, and the web socket path starts turns on
the partner instance rather than inside the socket.

This guard is keyed by ACCOUNT id, not by session, so every door passes
through it: the web socket, ``/chat``, ``/chat/execute-stream``, and a channel
message from the student's linked phone. A second concurrent turn is refused
with a message a student can act on; the reply is not silently queued, because
a queue looks like a hang.

Deliberately in-process: the app serves one worker, and the rule is about
pacing live turns, not a durable quota. A turn that dies without releasing
(cancelled task, killed worker, OOM) is taken over after ``STALE_SECONDS``, so
a crash can never lock a student out of their own account.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

#: How long a reservation may live without the turn releasing it before a new
#: turn is allowed to take the slot over. Longer than any real NCEA turn (the
#: slowest measured first turn on AU was ~30 s) and short enough that a student
#: who lost a turn to a crash is not stuck for the rest of the evening.
STALE_SECONDS = 45 * 60

BUSY_MESSAGE = (
    "You already have a question running in another window. "
    "This account works one window at a time — wait for that answer to finish "
    "(or close the other tab), then send this again."
)

#: uid -> monotonic-ish start time. Read and written under ``_LOCK``.
_ACTIVE: dict[str, float] = {}
_LOCK = threading.Lock()


class TurnBusy(RuntimeError):
    """The account already has a turn in flight.

    A ``RuntimeError`` on purpose: every caller of a turn already turns that
    into a message the client can see — an HTTP 400 on ``/chat``, an ``error``
    frame on the socket and on the stream endpoint.
    """


def _stale_seconds() -> float:
    raw = os.getenv("NCEA_TURN_STALE_SECONDS")
    try:
        return float(raw) if raw else float(STALE_SECONDS)
    except ValueError:
        return float(STALE_SECONDS)


def actor_id(actor: Any) -> str:
    """The account a turn belongs to, or ``""`` when it has none."""
    return str(getattr(actor, "id", "") or "")


def _exempt(actor: Any) -> bool:
    """Admins and un-attributed turns are not paced.

    The operator runs probes and drills against production, and a channel turn
    with no linked account has no student to pace.
    """
    return actor is None or bool(getattr(actor, "is_admin", False))


def active_since(actor: Any) -> float | None:
    """When this account's live turn started, or ``None`` if it has none."""
    uid = actor_id(actor)
    if not uid:
        return None
    with _LOCK:
        return _ACTIVE.get(uid)


def claim(actor: Any) -> str:
    """Reserve the account's one slot; returns the uid reserved (``""`` = exempt).

    Raises :class:`TurnBusy` when a live turn is already running for this
    account. A reservation older than :func:`_stale_seconds` is taken over.
    """
    if _exempt(actor):
        return ""
    uid = actor_id(actor)
    if not uid:
        return ""
    now = time.time()
    with _LOCK:
        started = _ACTIVE.get(uid)
        if started is not None and now - started < _stale_seconds():
            raise TurnBusy(BUSY_MESSAGE)
        if started is not None:
            logger.warning(
                "turn limit: %s held a slot for %.0fs without releasing; taking it over",
                uid,
                now - started,
            )
        _ACTIVE[uid] = now
    return uid


def release(uid: str) -> None:
    """Free the slot. Safe to call for a uid that holds nothing."""
    if not uid:
        return
    with _LOCK:
        _ACTIVE.pop(uid, None)


def reset() -> None:
    """Drop every reservation (tests, and an operator clearing a stuck account)."""
    with _LOCK:
        _ACTIVE.clear()


@contextlib.asynccontextmanager
async def single_turn(actor: Any) -> AsyncIterator[str]:
    """Hold the account's one slot for the duration of a turn."""
    uid = claim(actor)
    try:
        yield uid
    finally:
        release(uid)


__all__ = [
    "BUSY_MESSAGE",
    "STALE_SECONDS",
    "TurnBusy",
    "active_since",
    "actor_id",
    "claim",
    "release",
    "reset",
    "single_turn",
]
