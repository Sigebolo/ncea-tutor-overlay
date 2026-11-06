"""Deterministic NCEA question serving - context-injection helper (site patch).

Seven models across three families never call serve_question when the choice
is left to them, and the KB seed truncates mid-question (4000 chars/KB). So
when a message asks for a question and the standard resolves, the agent loop
calls this helper BEFORE the KB seed: it picks the bank entry with the same
code path as the serve_question tool (same per-student served ledger) and
returns a SERVED block to inject into the prompt. The model never chooses
whether a question exists - it only teaches the one already served.

This module also owns the per-turn id stamp (`set_current_turn_id` /
`current_turn_id`). The post-turn JEV gate reads the served/graded ledgers to
reconstruct what THIS turn injected, and a row can only be attributed to the
right turn if the turn's identity is written with it - the recency window
alone cannot tell two turns minutes apart apart. The runtime site patch stamps
it once per turn; ServeQuestionTool writes it onto its served-ledger row.
"""
import contextvars
import logging
import re

logger = logging.getLogger(__name__)

# Deliberately a ContextVar and not an argument: the model can call
# serve_question directly on a path this module never sees, and those rows
# carry exactly the same consequence as a deterministically served one. The
# default "" keeps every pre-existing caller working, and an unstamped row
# reads as "unknown turn" - the gate then falls back to its recency rule.
_TURN_ID: contextvars.ContextVar = contextvars.ContextVar(
    "ncea_current_turn_id", default="")


def set_current_turn_id(turn_id) -> None:
    """Stamp the turn id for the current async context. Never raises."""
    try:
        _TURN_ID.set(str(turn_id or ""))
    except Exception:
        pass


def current_turn_id() -> str:
    """This turn's id, or '' when nothing stamped one (fail-open)."""
    try:
        return _TURN_ID.get() or ""
    except Exception:
        return ""

INTENT_RE = re.compile(
    r"真题|来一道|出一道|给我一道|换一道|练一道|要一道|刷题|做一道|给我出|再出一道|"
    r"考我|考你|考一下|考一题|考一道|考个题|考考|测测我|测验|"
    r"past\s*papers?|practice\s+questions?|new\s+questions?|"
    r"quiz\s+me|test\s+me|"
    r"serve\s+me|"
    r"questions?\s+to\s+(practice|try|attempt|work\s+on)|"
    r"(one\s+more|another|next|new|more)\s+(question|problem|exercise)s?|"
    r"practice\s+(maths?|mathematics|algebra|geometry|calculus|statistics|"
    r"physics|chemistry|bio(logy)?|english)\b|"
    r"(give|send|show|get|try|pull|want|need)\s+(me\s+)?"
    r"(a\s+|an\s+|another\s+|one\s+|real\s+|new\s+)*.{0,40}?questions?",
    re.I,
)

# NOTE: bare 考 is deliberately absent (考试 / 考完试 / 高考 are not requests
# for a question). Only the compounds above match a "test me" intent.

CODE_RE = re.compile(r"(?<!\d)(9\d{4})(?!\d)")
KB_CODE_RE = re.compile(r"AS(9\d{4})")

KEYWORD_MAP = {
    "积分": "91579", "微积分": "91579", "integration": "91579",
    "微分": "91578", "differentiation": "91578", "calculus": "91579",
    "概率": "91586", "probability": "91586",
    "力学": "91171", "mechanics": "91171",
    "波": "91170", "waves": "91170",
    "电磁": "91173", "电学": "91173", "electricity": "91173",
    "细胞": "91156", "cells": "91156",
    "遗传": "91157", "genetic": "91157",
    "基因": "91159", "gene expression": "91159",
}

# Subject-level words (the paper's name, not a method). These resolve across a
# family in preference order: the first code the student is actually granted
# wins, so "calculus" serves 91579 when they have it (operator default) and
# still serves 91578 to a differentiation-only student instead of going silent.
# Method-level words (积分 / 微分) stay exact - naming one you are not granted
# is a real scope miss and must stay silent.
SUBJECT_PREFS = {
    "微积分": ("91579", "91578"),
    "calculus": ("91579", "91578"),
    # "practice math level 1" (Mark, 2026-10-31) named no code and no method
    # keyword, so nothing resolved and the loop improvised. Level-1 Math
    # resolves within the grant: Reasoning first (the KB families actually
    # shipped), then Number.
    "mathematics": ("91947", "91946"),
    "maths": ("91947", "91946"),
    "math": ("91947", "91946"),
}


def grant_codes(kb_items) -> set:
    """Standard codes a student may be examined on, read from grant KB names.

    Same assumption as the serve tool's scope check: KB names carry ``AS<code>``
    (e.g. ``NZQA_C3_AS91579_Integration``).
    """
    names = " ".join(
        ((i.get("name") or i.get("resource_id") or i.get("id") or "") if isinstance(i, dict)
         else str(i))
        for i in (kb_items or [])
    )
    return set(KB_CODE_RE.findall(names))


def resolve_standard(text: str, allowed: set) -> str:
    """5-digit code if stated, else the unique method keyword in the grant,
    else the first granted preference of a subject-level word. '' if unclear.

    Scope narrows ambiguity: a message naming two topics where the student is
    granted only one serves that one (previously any two hits went silent).
    Never returns a code outside ``allowed``.
    """
    m = CODE_RE.search(text or "")
    if m:
        return m.group(1) if m.group(1) in allowed else ""
    low = (text or "").lower()
    hits = {code for kw, code in KEYWORD_MAP.items() if kw in low}
    hits &= allowed
    if len(hits) == 1:
        return hits.pop()
    if hits:
        return ""  # two distinct named topics inside the grant: real ambiguity
    for kw, prefs in SUBJECT_PREFS.items():
        if kw in low:
            for code in prefs:
                if code in allowed:
                    return code
    return ""


async def build_serve_block(text: str, uid: str, turn_id: str = "") -> str:
    """Return '' (no injection) or a SERVED / SERVE-UNAVAILABLE block.

    ``uid`` is the real chatting student (partner turns carry sender_id in
    context metadata). Everything student-scoped - grant check and the
    served ledger - runs inside that student's user context.

    ``turn_id`` stamps the ledger row the serve tool appends. Belt and braces
    on top of the runtime's per-turn stamp: an install whose runtime patch is
    missing still attributes the deterministic path.
    """
    if not uid or not INTENT_RE.search(text or ""):
        return ""
    from deeptutor.multi_user.grants import load_grant

    allowed = grant_codes((load_grant(uid) or {}).get("knowledge_bases"))
    code = resolve_standard(text, allowed)
    if not code:
        return ""  # no standard named, or not one this student can be examined on
    from deeptutor.multi_user.identity import get_user_by_id
    from deeptutor.multi_user.models import CurrentUser
    from deeptutor.multi_user.paths import scope_for_user, user_context
    from deeptutor.tools.builtin import ServeQuestionTool

    token = None
    if turn_id:
        try:
            token = _TURN_ID.set(str(turn_id))
        except Exception:
            token = None
    try:
        record = get_user_by_id(uid)
        username = record[0] if record else uid
        student = CurrentUser(
            id=uid,
            username=username,
            role="user",
            scope=scope_for_user(uid, is_admin=False),
        )
        with user_context(student):
            result = await ServeQuestionTool().execute(standard=code)
    except Exception:
        logger.warning("ncea serve block failed", exc_info=True)
        return ""
    finally:
        if token is not None:
            try:
                _TURN_ID.reset(token)
            except Exception:
                pass
    content = (result.content or "").strip()
    if result.success and content:
        logger.info("ncea serve block injected: standard=%s uid=%s", code, uid)
        return (
            "[SERVED NCEA QUESTION - deterministic, verified bank entry]\n"
            "The student asked for a question. ONE verified question has been "
            "served below. Teach THIS question only: restate the question for "
            "the student (Location + the identifying stem, and the full setup "
            "if the entry gives one), always give the NZQA PDF link so they can "
            "see the original paper WITH its diagrams and figures. Grade "
            "against the answer key, but NEVER state, hint, or compute the "
            "answer before the student has attempted the question - the key "
            "in this block is for marking THEIR working, not for telling. "
            "If the entry carries a [QUESTION PAGE "
            "IMAGE] note, your reply MUST open with the note's Markdown image "
            "line, copied verbatim (the file is already presented - do not "
            "re-present it), so the extracted question page renders inline. "
            "If the student says they do not have the paper, tell them the PDF "
            "link IS the paper (free on nzqa.govt.nz) and walk them through "
            "the stem - never invent a diagram, never generate an image of "
            "the question, and never substitute any other question.\n\n" + content
        )
    logger.info("ncea serve block unavailable: standard=%s uid=%s", code, uid)
    return (
        "[SERVE UNAVAILABLE - anti-fabrication policy]\n" + content + "\n"
        "Do NOT compose, reconstruct, estimate, or paste any exam question. "
        "Teach concepts instead, or offer the student a verified PDF link."
    )
