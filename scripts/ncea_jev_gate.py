"""JEV semantic gate (site patch helper; shadow-first rollout).

Deterministic gates (serve/grade/review blocks + output_guard + autolog) own
the STRUCTURED hallucination classes: bank entry ids, code+year+part
citations, and digit comparison against a verified key. They are blind to
the semantic classes, because those carry structure but no verifiable id:

  A. an unresolvable grading turn -> the model "recalls" an answer key
  B. the model COMPOSES a question (parts/marks, looks real, never served)
  C. a Location / paper attribution that is wrong while the id parses
  D. a "the correct value is X" assertion inside concept teaching

JEV (TypeSafe System One) returns typed answers + probabilities/confidence
that ordinary code can branch on, which is exactly the missing layer. The
design rule is TypeSafe's own: questions describe judgments; code owns
thresholds and side effects.

This module is intentionally self-contained and fail-open:
  * no TYPESAFE_API_KEY / timeout / any error -> "" (keep the original reply)
  * mode="shadow"  -> decide and write the audit ledger, never rewrite
  * mode="enforce" -> return the bilingual refusal replacement on a guard
  * mode="off"     -> do nothing (env kill switch)

Evidence for the judgement is REBUILT read-only from the per-student
ledgers (ncea_served.jsonl / ncea_graded.jsonl), not by re-running the
serve/grade blocks - those WRITE those ledgers, so calling them here would
double-log and re-inject. A row counts as "this turn's injection" when it
carries THIS turn's id (the runtime stamps one per turn and the serve/grade
writers copy it onto their row). Rows with no turn id fall back to
RECENCY_WINDOW, which alone cannot separate two turns minutes apart -
the failure it used to produce is documented in HANDOFF_JEV_GATE.md.

Runs AFTER autolog + maybe_guard_output in the post-turn hook.
See HANDOFF_JEV_GATE.md for the plan, thresholds and the production hook.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# Pin a VERSIONED model when thresholds depend on behaviour (TypeSafe's own
# calibration guidance); log the model the response reports on every call.
MODEL_PINNED = "jev-1.13.0"
MODEL_LATEST = "jev-latest"
DEFAULT_TIMEOUT = 4.0

# --- thresholds (conservative: Noul only counts at the ENDS) ----------------
NOUL_HIGH = 0.80      # "the statement is true" - require this to act
NOUL_LOW = 0.20       # "the statement is false" - require this to act
CHOICE_MIN_CONF = 0.60
EVENT_FILE = "ncea_jev_gate.jsonl"

# A ledger row older than this is stale: the pre-turn of THIS turn did not
# inject it, so it must not be used as this turn's verified evidence.
# Generous because a long agent turn can take minutes.
RECENCY_WINDOW = 900

# --- runtime knobs (env; shadow by default) --------------------------------
# NCEA_JEV_GATE_MODE=shadow | enforce | off
# NCEA_JEV_GATE_TIMEOUT=4.0        (clamped to 0.5..15s)
# TYPESAFE_JEV_MODEL=jev-1.13.0    (pin; thresholds ride on the version)
MODE_ENV = "NCEA_JEV_GATE_MODE"
TIMEOUT_ENV = "NCEA_JEV_GATE_TIMEOUT"
MODEL_ENV = "TYPESAFE_JEV_MODEL"
KEY_ENV = "TYPESAFE_API_KEY"

# Settings file, in the same place DEPLOYMENT.md sec.10 keeps every other
# secret (data/user/settings/, gitignored). PM2 starts the app from a captured
# shell env with no ecosystem file and no .env, so an exported variable does
# not reliably survive `pm2 restart` or a reboot - a file does. Env still WINS
# when set, which keeps one-off and test runs working unchanged.
#
#   {"api_key": "...", "mode": "shadow"}
#
CONFIG_RELPATH = os.path.join("data", "user", "settings", "jev_gate.json")
CONFIG_TTL = 60.0
# (ts, resolved_path, value) in one tuple so the swap is a single atomic
# assignment - this is read from the event loop AND from the worker thread.
# The path is part of the key: caching without it made a read of path B reuse
# path A's data whenever both fell inside the TTL.
_cfg_cache = (0.0, "", {})

# Dedicated, BOUNDED worker pool for the gate. Deliberately NOT
# asyncio.to_thread: that uses the process-wide default executor, which every
# other blocking task in the app shares - so a burst of slow JEV calls would
# starve unrelated file IO and sibling helpers. A throttled/slow vendor is
# exactly what a free tier is, and exactly when that burst arrives.
MAX_WORKERS_ENV = "NCEA_JEV_GATE_MAX_WORKERS"
DEFAULT_MAX_WORKERS = 4
# In-flight >= workers * SHED_FACTOR -> shed this turn instead of queueing it.
SHED_FACTOR = 2

# 
# The "free tier" lesson, stated once: the price is not the constraint, the
# RATE LIMIT is. Free does not mean unlimited, and it certainly does not mean
# fast. So the gate bounds its own concurrency, and sheds rather than queues -
# under burst it degrades to fail-open (today's behaviour) instead of making
# students wait behind a third-party call. Shed rows are written to the ledger
# for the same reason error rows are: a gap must never read as "clean".

# Cheap pre-filter so greetings/logistics turns never cost a call. When the
# turn already carries a verified block or a photo we always call. When in
# doubt we CALL: a false positive costs one request, a false negative
# silently reopens the hole this gate exists to close.
MIN_LEN = 60
_QUESTION_MARKERS = (
    "答案", "对不对", "标准", "批改", "判分", "评改", "真题", "题库",
    "answer", "correct", "past paper", "marks", "mark)",
    "分）", "分)", "(a)", "(b)", "(c)",
    "q1", "q2", "q3", "q4", "question", "ncea", "题",
)

GUARDED_MARKER = "[JEV-GUARD - unverified]"

REPLACEMENT = (
    "[JEV-GUARD - unverified]\n"
    "I couldn't verify this reply against the served question and its answer "
    "key, so I'm holding it back rather than risk teaching the wrong thing. "
    "Tell me the standard code (e.g. 91579) and I'll serve a verified "
    "past-paper question with its PDF link, or send the paper photo and I'll "
    "mark it against the verified key."
    "（这一轮的回复我核验不过——题目归属或答案对不上已验证的题/答案 key，先不发出。"
    "告诉我 standard 代码我就从 verified bank 取真题；拍照则按 key 判分。）"
)


class JevError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Pure logic (unit-tested, no network, no deeptutor imports)
# ---------------------------------------------------------------------------

def build_state(student_message="", tutor_reply="", served_block="",
                answer_key_block="", known_entry_ids=None,
                include_entry_ids=False):
    """JSON state with evidence in separate slots. THIS IS WHAT LEAVES THE
    DEPLOYMENT, so every field has to earn its place.

    Slots matter: the model must not treat what the STUDENT typed as verified
    evidence, and the bank/key text is kept apart from the chat. Both sides are
    English by default anyway (see skills/ncea-tutor/SKILL.md 'Language'), so
    the separation is about PROVENANCE - what was verified vs what was merely
    said - not about language.

    known_entry_ids is NOT sent by default. No question in build_questions()
    reads it, and the id-membership class it would inform ("cites an entry the
    student was never served") is already owned deterministically by
    ncea_output_guard.check_output. Shipping a per-student behavioural
    history to a third party for nothing is data we do not need to leak -
    opt in with include_entry_ids=True only if a question starts using it.
    """
    state = {
        "student_message": student_message or "",
        "tutor_reply": tutor_reply or "",
        "verified_blocks": {
            "served": served_block or "",
            "answer_key": answer_key_block or "",
        },
    }
    if include_entry_ids:
        state["known_entry_ids"] = [str(x) for x in (known_entry_ids or [])]
    return state


VALID_MODES = ("shadow", "enforce", "off")


def norm_mode(value):
    """shadow | enforce | off, or None when the value is not recognised."""
    v = str(value or "").strip().lower()
    return v if v in VALID_MODES else None


def config_path(path=None):
    return path or os.path.join(os.getcwd(), CONFIG_RELPATH)


def _read_config(resolved):
    """Best-effort read of the gate's settings file. Never raises."""
    try:
        with open(resolved, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def gate_config(path=None, force=False):
    """The gate's settings dict, cached for CONFIG_TTL seconds.

    Cached because the no-key fast path in maybe_jev_guard runs on the event
    loop and must not do a disk read per turn. Cache key = resolved path.
    """
    global _cfg_cache
    ts, cached_path, value = _cfg_cache
    resolved = config_path(path)
    now = time.monotonic()
    if force or resolved != cached_path or now - ts > CONFIG_TTL:
        _cfg_cache = (now, resolved, _read_config(resolved))
        value = _cfg_cache[2]
    return value


def gate_api_key(path=None):
    """TYPESAFE_API_KEY from the environment, else the settings file."""
    return (os.environ.get(KEY_ENV)
            or str(gate_config(path).get("api_key") or "")).strip()


def gate_mode(default="shadow", path=None):
    """shadow | enforce | off - environment, else settings file, else default."""
    return (norm_mode(os.environ.get(MODE_ENV))
            or norm_mode(gate_config(path).get("mode"))
            or default)


def gate_timeout(default=DEFAULT_TIMEOUT):
    try:
        t = float(os.environ.get(TIMEOUT_ENV) or default)
    except (TypeError, ValueError):
        return default
    return max(0.5, min(15.0, t))


def gate_model(default=MODEL_PINNED):
    return (os.environ.get(MODEL_ENV) or "").strip() or default


def looks_question_related(reply, verified=False, photo=False):
    """Should this turn cost a JEV call at all? (cost gate)

    A verified block or a photo always qualifies. A short chat turn with no
    digit never does. Everything else is a marker scan - deliberately
    generous, see _QUESTION_MARKERS."""
    if verified or photo:
        return True
    text = (reply or "").strip()
    if not text:
        return False
    if len(text) < MIN_LEN and not re.search(r"\d", text):
        return False
    low = text.lower()
    return any(m in low for m in _QUESTION_MARKERS)


def build_questions():
    """The fan-out: five focused judgments, one call."""
    return {
        "states_definite_answer": {
            "type": "noul",
            "instructions": (
                "Does the tutor's reply state a specific correct answer or "
                "final numeric result for a question, as established fact?"
            ),
        },
        "answer_backed_by_key": {
            "type": "noul",
            "instructions": (
                "Is every specific answer value asserted in the reply "
                "supported by the verified answer key text? Answer true only "
                "when the key actually contains the asserted value."
            ),
        },
        "presents_question_to_attempt": {
            "type": "noul",
            "instructions": (
                "Does the reply present an exam question for the student to "
                "attempt (question number, parts or marks), rather than only "
                "explaining a concept?"
            ),
        },
        "question_matches_verified": {
            "type": "noul",
            "instructions": (
                "Is the exam question in the reply the SAME question as the "
                "verified served question, rather than a reworded, "
                "reconstructed or invented one?"
            ),
        },
        "declines_and_substitutes": {
            "type": "noul",
            "instructions": (
                "Does the reply decline to supply or compose the question "
                "it was asked for, and instead point the student at a "
                "different question it presents as already verified or "
                "available?"
            ),
        },
        "location_claim": {
            "type": "choice",
            "instructions": (
                "How does the reply's claim about which paper/year/part a "
                "question comes from relate to the verified block?"
            ),
            "criteria": {
                "supports": "The claim agrees with the verified block.",
                "contradicts": "The claim disagrees with the verified block.",
                "not_established": "The reply makes no such claim, or the "
                                   "verified block is empty.",
            },
        },
    }


def _noul(answers, name):
    a = (answers or {}).get(name)
    if not isinstance(a, dict):
        return None
    v = a.get("noul")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def decide(answers, key_present, serve_present, cited_in_bank=False):
    """Pure policy: {verdict, reasons}. code owns the thresholds.

    cited_in_bank is the deterministic half of Rule 2: the question the reply
    presents resolves to a REAL entry in the verified bank (computed by
    cite_in_bank, which reads the bank file - so it is passed in rather than
    looked up here, keeping this function pure).
    """
    reasons = []
    definite = _noul(answers, "states_definite_answer")
    backed = _noul(answers, "answer_backed_by_key")
    presents = _noul(answers, "presents_question_to_attempt")
    matches = _noul(answers, "question_matches_verified")

    # Rule 1 - a definite answer with no injected key, or one the key denies.
    if definite is not None and definite >= NOUL_HIGH:
        if not key_present:
            reasons.append("definite-answer-no-key")
        elif backed is not None and backed <= NOUL_LOW:
            reasons.append("answer-not-backed-by-key")

    # Why Rules 2 and 3 need an excuse at all (live, AU shadow, 2026-09-21):
    # the student asked for 91392-2025-Q3b(i), which the bank could not
    # serve, so the serve block injected a DIFFERENT entry - and the tutor
    # quite correctly refused to invent the one it was asked for and offered
    # a verified entry instead. Every "is this the one we served?" question
    # answers no to a reply like that, so entry IDENTITY against this turn's
    # served row guarded honest, fully verified answers (the deterministic
    # output guard passed them, because that one compares at CODE level).
    #
    # The excuse is therefore membership, and ONLY membership: the question
    # the reply presents must resolve to a real entry in the verified bank
    # (cite_in_bank - a deterministic read of the bank file, not a model
    # opinion).
    #
    # declines_and_substitutes is deliberately NOT sufficient on its own. It
    # reports what the tutor SAYS it is doing; it is not evidence that what
    # it hands over is real. Live, ledger-verified (AU 2026-09-21 00:20:32):
    # a reply declined at 0.91, offered a "substitute", and attached an
    # INVENTED identifying stem ("ice cream shop ... mean 1000 g, standard
    # deviation 35 g") to a real filename - a fabricated question wearing a
    # real paper's location, which is precisely the class this gate exists
    # for. The decline was truthful; the substitute was not. It measured
    # in_bank = false, so membership alone catches it, while honest shapes
    # that never decline (a photo-grade turn quoting a real entry, nothing
    # served this turn) keep their excuse.
    #
    # The signal is still recorded per turn - answers.declines_and_substitutes
    # stays in the ledger row - because it is useful context when a human
    # reads a guard. It just does not decide anything here.
    substituted = bool(cited_in_bank)

    # Rule 2 - presents a question that was not served / does not match.
    if presents is not None and presents >= NOUL_HIGH:
        if not serve_present:
            if not substituted:
                reasons.append("question-not-served")
        elif matches is not None and matches <= NOUL_LOW and not substituted:
            reasons.append("question-mismatch")

    # Rule 3 - location claim contradicts the verified block. A reply that is
    # openly offering a different verified question is EXPECTED to disagree
    # with the served row, so it is excused here too (the same live case) -
    # otherwise Rule 3 just re-guards what Rule 2 let through.
    loc = (answers or {}).get("location_claim")
    if isinstance(loc, dict) and loc.get("choice") == "contradicts" \
            and not substituted:
        try:
            conf = float(loc.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= CHOICE_MIN_CONF:
            reasons.append("location-contradicts-verified")

    return {"verdict": "guard" if reasons else "pass", "reasons": reasons}


# ---------------------------------------------------------------------------
# Rule 2's deterministic half: is the question in the reply a REAL bank entry?
# ---------------------------------------------------------------------------

_BANK_CACHE = {}
_CODE_ANY_RE = re.compile(r"(?<!\d)(9\d{4})(?!\d)")
_FULL_ID_RE = re.compile(r"(?<!\d)(\d{5})-(20\d{2})-(Q[A-Za-z0-9]+)")
_WS_RE = re.compile(r"\s+")
# The reply renders maths nicely (L⁻¹); the bank file is plain ASCII (L-1).
# Fold to the bank's form or a verbatim quotation stops looking verbatim.
_UNICODE_FOLD = str.maketrans({
    "\u207b": "-", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3",
    "\u2070": "0", "\u2074": "4", "\u2075": "5",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
})
# Shortest quoted fragment that still means "reproducing bank text". Lower
# than this and bare words like "Location" or "PDF" would match any file.
_MIN_QUOTE = 18
# Wrapping punctuation the tutor puts around a quoted stem (it quotes the
# bank's own "Identifying stem" line). Stripped so the quotation still
# matches the bank's bare text.
_QUOTE_STRIP = "\"'\u201c\u201d\u2018\u2019()[]{}:,;."


def _norm_text(value):
    t = str(value or "").translate(_UNICODE_FOLD)
    t = t.replace("`", " ").replace("*", " ").replace("_", " ")
    return _WS_RE.sub(" ", t).strip()


# A reply that names the paper - `91267-exm-2024.pdf` - proves nothing about
# WHICH question it is presenting. Live proof (AU, 2026-09-21): a reply
# attached an invented stem ("ice cream shop ... mean 1000 g, sd 35 g") to a
# REAL filename and thereby satisfied the in-bank excuse, so a fabricated
# question was judged `pass`. Filenames and URLs are therefore scrubbed out
# of the bank text before any fragment matching: only the entry's identity
# ("Identifying stem") or answer can demonstrate membership.
_ASSET_RE = re.compile(r"\S*\.pdf\S*|(?:https?://|www\.)\S+")


def _scrub_assets(value):
    return _ASSET_RE.sub(" ", value)


def _bank_text(code):
    """Raw text of the verified bank file for a standard code, or "".

    Cached: Rule 2 asks once per turn and the file is a few kB. Prefers the
    installed helper's reader, then falls back to the skill directory so an
    offline replay (fixtures, --cases) exercises the same code path.
    """
    if code in _BANK_CACHE:
        return _BANK_CACHE[code]
    text = ""
    try:
        from deeptutor.tools.builtin import _ncea_read_skill_file as _read
        text = _read(f"references/qbank_{code}.md") or ""
    except Exception:                      # not installed / offline replay
        text = ""
    if not text:
        try:
            from pathlib import Path
            here = Path(__file__).resolve()
            for root in (Path.cwd(), here.parent, here.parent.parent,
                         here.parent.parent.parent):
                cand = (root / "skills" / "ncea-tutor" / "references"
                        / f"qbank_{code}.md")
                if cand.is_file():
                    text = cand.read_text(encoding="utf-8")
                    break
        except Exception:
            text = ""
    _BANK_CACHE[code] = text
    return text


def _quote_candidates(reply):
    """Normalised fragments of the reply long enough to mean a quotation."""
    out = []
    for raw in str(reply or "").splitlines():
        for frag in (raw, *raw.split(",")):
            frag = _norm_text(frag).strip(_QUOTE_STRIP)
            if len(frag) >= _MIN_QUOTE and any(c.isdigit() for c in frag):
                out.append(frag)
    return out


def cite_in_bank(reply, bank_text_lookup=None):
    """An entry id/code the reply presents that is REAL in the verified bank.

    MEMBERSHIP, not identity. The reply may quote ANY entry in the bank, not
    only the one this turn's serve block happened to inject. A composed
    question quotes nothing verbatim, because it was never in any bank file.

    Two ways to resolve, cheapest first:
      1. a full bank id appearing in that code's bank file;
      2. a >= _MIN_QUOTE-character fragment of the reply appearing in the
         bank file - the tutor reproduces the identifying stem (or the
         answer) when it offers a real entry.

    Quotes of the PAPER'S FILENAME do not count (see _ASSET_RE): naming
    `91267-exm-2024.pdf` is not evidence about which question is on offer,
    and a reply has been observed attaching a fabricated stem to a real
    filename. Only the entry's own text - stem or answer - counts.

    Returns the id (or the code) that resolved, else "".

    Deliberately biased toward NO MATCH: a miss only means Rule 2 keeps its
    original wording, whereas a bogus match would retire the rule. The
    semantic decline signal in decide() covers the cases this misses.
    """
    text = _norm_text(reply)
    if not text:
        return ""
    ids = [f"{c}-{y}-{q}" for c, y, q in _FULL_ID_RE.findall(text)]
    for code in dict.fromkeys(_CODE_ANY_RE.findall(text)):
        raw = (bank_text_lookup(code) if bank_text_lookup
               else _bank_text(code))
        blob = _scrub_assets(_norm_text(raw))
        if not blob:
            continue
        for eid in ids:
            if eid.startswith(code + "-") and eid in blob:
                return eid
        for frag in _quote_candidates(reply):
            if frag in blob:
                return code
    return ""


# ---------------------------------------------------------------------------
# Transport (stdlib only - no asset on the typesafe-sdk being installed)
# ---------------------------------------------------------------------------

def call_systemone(state, questions, api_key=None, model=MODEL_PINNED,
                   timeout=DEFAULT_TIMEOUT):
    """POST /v1/systemone and return the parsed response. Raises JevError."""
    key = api_key or gate_api_key()
    if not key:
        raise JevError(f"{KEY_ENV} not set (env and {CONFIG_RELPATH})")
    body = json.dumps(
        {"state": state, "model": model, "questions": questions}
    ).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise JevError(f"HTTP {e.code}: {detail}") from e
    except Exception as e:  # timeout, DNS, bad JSON
        raise JevError(str(e)) from e


# ---------------------------------------------------------------------------
# Production-shaped entry point (mirrors maybe_guard_output)
# ---------------------------------------------------------------------------

def _student_uid(msg):
    try:
        actor = getattr(msg, "actor", None)
        uid = getattr(actor, "id", None)
        if uid and not getattr(actor, "is_admin", False):
            return str(uid)
    except Exception:
        pass
    return ""


def _has_photo(msg):
    """Is this turn carrying an image? (grading turns are the key case)"""
    try:
        meta = getattr(msg, "metadata", None) or {}
        for r in meta.get("_attachment_records") or []:
            if not isinstance(r, dict):
                continue
            if r.get("type") == "image" or str(
                    r.get("mime_type") or "").startswith("image/"):
                return True
        for m in getattr(msg, "media", None) or []:
            if isinstance(m, dict) and m.get("type") == "image":
                return True
    except Exception:
        pass
    return False


def _root_for(uid):
    """Per-student data root, or None. Never raises (fail-open)."""
    try:
        from deeptutor.multi_user.paths import scope_for_user
        return scope_for_user(uid, is_admin=False).root
    except Exception:
        return None


def _ledger_rows(root, name):
    """All parseable dict rows of a per-student ledger, in order."""
    out = []
    try:
        from pathlib import Path
        p = Path(root) / name
        if not p.exists():
            return out
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue
            if isinstance(row, dict):
                out.append(row)
    except Exception:
        pass
    return out


def ledger_entry_ids(root):
    """Every entry id this student was ever served or graded."""
    out = set()
    for name in ("ncea_served.jsonl", "ncea_graded.jsonl"):
        for row in _ledger_rows(root, name):
            eid = str(row.get("entry_id") or "").strip()
            if eid:
                out.add(eid)
    return sorted(out)


def _this_turn(rows, now):
    """(row, fresh) for a ledger's last row. fresh == written inside
    RECENCY_WINDOW, i.e. by THIS turn's pre-turn injection."""
    if not rows:
        return {}, False
    row = rows[-1]
    try:
        age = now - int(row.get("ts") or 0)
    except (TypeError, ValueError):
        return row, False
    return row, 0 <= age <= RECENCY_WINDOW


def _turn_row(rows, now, turn_id):
    """(row, fresh, source) for THIS turn's injection in one ledger.

    Turn attribution first: a row whose ``turn`` equals this turn's id IS
    this turn's injection, exactly - no window, no guessing. Rows written by
    other turns can never match, which is the whole point: several turns can
    sit inside RECENCY_WINDOW, and pairing a question served two turns ago
    with a key injected this turn is how a correct reply got guarded.

    Two fallbacks, both deliberate:
      * the ledger carries NO turn stamp at all (an install whose runtime
        stamp is missing) -> the recency rule, i.e. yesterday's behaviour;
      * the ledger IS stamp-aware but has no row for this turn -> nothing was
        injected this turn, and that emptiness is the honest evidence.
    """
    if turn_id:
        stamped = [r for r in rows if str(r.get("turn") or "") == turn_id]
        if stamped:
            return stamped[-1], True, "turn"
        if any(str(r.get("turn") or "") for r in rows):
            return {}, False, "turn"
    row, fresh = _this_turn(rows, now)
    return row, fresh, ("recency" if fresh else "none")


def _bank_entry(entry_id):
    """Best-effort bank lookup for the Location line (needs site install)."""
    try:
        from deeptutor.agents._shared import ncea_grade_block as gb
        m = re.match(r"^(\d{5})-", entry_id or "")
        if not m:
            return {}
        for e in gb._read_bank(m.group(1)) or []:
            if e.get("id") == entry_id:
                return e
    except Exception:
        pass
    return {}


def _render_served(entry_id):
    try:
        loc = str((_bank_entry(entry_id).get("bullets") or {})
                  .get("location", "") or "")
    except Exception:
        loc = ""
    return ("[SERVED NCEA QUESTION - delivered to this student this turn]\n"
            f"Entry: {entry_id}\n" + (f"Location: {loc}\n" if loc else ""))


def _render_key(row):
    return ("[VERIFIED ANSWER KEY - injected for grading this turn]\n"
            f"Entry: {row.get('entry_id', '')}\n"
            f"Correct answer: {row.get('answer', '')}\n")


def gather_evidence(uid, now=None, turn_id=""):
    """Read-only: what did THIS turn's pre-turn actually inject?

    Returns {"served_block", "answer_key_block", "known_entry_ids",
             "last_served", "last_graded", "has_verified", "evidence"}.

    ``turn_id`` selects the rows written BY THIS TURN (see _turn_row). With no
    turn id - or a ledger with no stamps on it - it degrades to the recency
    window, which is the older, weaker guarantee.

    Never re-runs build_serve_block / build_grade_block: those WRITE the
    ledgers, so calling them here would double-log and re-inject.
    """
    now = int(time.time() if now is None else now)
    turn_id = str(turn_id or "")
    out = {"served_block": "", "answer_key_block": "",
           "known_entry_ids": [], "last_served": "", "last_graded": "",
           "has_verified": False, "turn": turn_id, "evidence": {}}
    root = _root_for(uid)
    if root is None:
        return out
    out["known_entry_ids"] = ledger_entry_ids(root)

    srow, sfresh, ssrc = _turn_row(
        _ledger_rows(root, "ncea_served.jsonl"), now, turn_id)
    grow, gfresh, gsrc = _turn_row(
        _ledger_rows(root, "ncea_graded.jsonl"), now, turn_id)
    out["evidence"] = {"served": ssrc, "graded": gsrc}
    out["last_served"] = str(srow.get("entry_id") or "") if sfresh else ""
    out["last_graded"] = str(grow.get("entry_id") or "") if gfresh else ""

    if sfresh and out["last_served"]:
        out["served_block"] = _render_served(out["last_served"])
    if gfresh and out["last_graded"]:
        out["answer_key_block"] = _render_key(grow)
    out["has_verified"] = bool(out["served_block"] or out["answer_key_block"])
    return out


def _student_turn_id(msg):
    """The id the runtime stamped on this turn, or ''. Fail-open."""
    try:
        return str((getattr(msg, "metadata", None) or {})
                   .get("_ncea_turn_id") or "")
    except Exception:
        return ""


def _write_event(root, rec):
    try:
        from pathlib import Path
        p = Path(root) / EVENT_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("ncea jev audit write skipped", exc_info=True)


# Strong refs to in-flight shadow tasks: the event loop only keeps a weak
# one, so without this a task can be garbage-collected mid-request and the
# audit row silently disappears.
_BG_TASKS = set()


try:
    from asyncio import get_running_loop as _get_running_loop
    from asyncio import ensure_future as _ensure_future
except ImportError:                              # pragma: no cover
    _get_running_loop = _ensure_future = None

# Module-level pool state. _in_flight is only ever mutated from the event-loop
# thread (the executor thread never touches it), so it needs no lock.
_executor = None
_executor_lock = threading.Lock()
_in_flight = 0


def gate_max_workers(default=DEFAULT_MAX_WORKERS):
    try:
        n = int(os.environ.get(MAX_WORKERS_ENV) or default)
    except (TypeError, ValueError):
        return default
    return max(1, min(32, n))


def _get_executor():
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _executor = ThreadPoolExecutor(
                    max_workers=gate_max_workers(),
                    thread_name_prefix="ncea-jev")
    return _executor


def _write_shed(uid, mode, reason):
    root = _root_for(uid)
    if root:
        _write_event(root, {
            "ts": int(time.time()), "kind": "jev-gate", "mode": mode,
            "uid": uid, "verdict": "skipped", "reason": reason,
            "reasons": [], "answers": {}, "latency_ms": 0, "model": "",
        })


async def _evaluate(msg, final, mode, uid, served_block, answer_key_block,
                    known_entry_ids, timeout, turn_id=None):
    """Run _evaluate_sync on the gate's own bounded pool.

    Sheds (returns None, writes a "skipped" row) when the pool is saturated
    rather than queueing: an unbounded queue would convert a slow vendor into
    student-visible latency, and on a free tier this is the normal case, not
    the edge case.
    """
    global _in_flight
    if _get_running_loop is None:                # pragma: no cover
        return _evaluate_sync(msg, final, mode, uid, served_block,
                              answer_key_block, known_entry_ids, timeout,
                              turn_id)
    if _in_flight >= gate_max_workers() * SHED_FACTOR:
        _write_shed(uid, mode, "burst")
        return None
    _in_flight += 1
    try:
        loop = _get_running_loop()
        return await loop.run_in_executor(
            _get_executor(), _evaluate_sync, msg, final, mode, uid,
            served_block, answer_key_block, known_entry_ids, timeout,
            turn_id)
    finally:
        _in_flight -= 1


def _evaluate_sync(msg, final, mode, uid, served_block, answer_key_block,
                   known_entry_ids, timeout, turn_id=None):
    """One JEV call + one audit row, fully synchronous. Returns the verdict
    dict, or None when the cost gate skipped the turn (no call was made).

    Synchronous on purpose: it does THREE kinds of blocking IO - a bank file
    read while rebuilding the served evidence, the urllib request, and the
    audit append. `_evaluate` runs the whole thing in a worker thread, so
    none of them can stall the event loop for every other concurrent turn.

    Evidence: explicit kwargs win (tests / replay); None means "rebuild it
    from this student's ledgers", which is what the production hook does -
    it passes nothing but msg and final.
    """
    if turn_id is None:
        turn_id = _student_turn_id(msg)
    ev = {}
    if served_block is None or answer_key_block is None \
            or known_entry_ids is None:
        ev = gather_evidence(uid, turn_id=turn_id)
    if served_block is None:
        served_block = ev.get("served_block", "")
    if answer_key_block is None:
        answer_key_block = ev.get("answer_key_block", "")
    if known_entry_ids is None:
        known_entry_ids = ev.get("known_entry_ids") or []
    has_verified = bool((served_block or "").strip()
                        or (answer_key_block or "").strip())
    if not looks_question_related(final, verified=has_verified,
                                  photo=_has_photo(msg)):
        return None
    state = build_state(
        student_message=str(getattr(msg, "content", "") or ""),
        tutor_reply=final,
        served_block=served_block,
        answer_key_block=answer_key_block,
        known_entry_ids=known_entry_ids)
    root = _root_for(uid)
    t0 = time.time()
    try:
        resp = call_systemone(state, build_questions(), model=gate_model(),
                              timeout=timeout or gate_timeout())
        answers = resp.get("answers") or {}
    except Exception as exc:
        # A failed call must still leave a row. During shadow the ledger IS
        # the product, and a silent gap reads as "clean" while the gate is in
        # fact broken - the worst possible way to discover it at enforce time.
        if root:
            _write_event(root, {
                "ts": int(time.time()), "kind": "jev-gate", "mode": mode,
                "uid": uid, "verdict": "error", "reasons": [],
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "latency_ms": int((time.time() - t0) * 1000),
                "model": "", "answers": {},
            })
        raise
    # Deterministic half of Rule 2: does the question in the reply resolve
    # to a real bank entry? Reads a bank file, so it belongs here in the
    # worker thread (never on the event loop) rather than inside decide().
    cited = cite_in_bank(final)
    verdict = decide(
        answers,
        key_present=bool((answer_key_block or "").strip()),
        serve_present=bool((served_block or "").strip()),
        cited_in_bank=bool(cited))
    if root:
        _write_event(root, {
            "ts": int(time.time()), "kind": "jev-gate", "mode": mode,
            "uid": uid, "verdict": verdict["verdict"],
            "reasons": verdict["reasons"],
            "serve_present": bool((served_block or "").strip()),
            "key_present": bool((answer_key_block or "").strip()),
            "cited_in_bank": cited,
            "turn": str(turn_id or ""),
            "evidence": dict((ev or {}).get("evidence") or {}),
            "served": str((ev or {}).get("last_served", "")),
            "graded": str((ev or {}).get("last_graded", "")),
            "chars": len(final),
            "latency_ms": int((time.time() - t0) * 1000),
            "model": resp.get("model", ""),
            "usage": resp.get("usage", {}),
            "answers": answers,
        })
    return verdict


async def _safe_evaluate(*args):
    try:
        return await _evaluate(*args)
    except Exception:
        logger.warning("ncea jev gate call failed", exc_info=True)
        return None


def _spawn(coro):
    """Fire-and-forget with a strong ref. Returns the task, or None when
    there is no running loop (caller then awaits inline).

    The strong ref matters: the loop only keeps a weak one, so a bare
    create_task can be garbage-collected mid-request and the audit row
    would silently disappear - poisoning the calibration data.
    """
    if _get_running_loop is None:               # pragma: no cover
        return None
    try:
        task = _ensure_future(coro)
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return task
    except Exception:
        return None


async def maybe_jev_guard(msg=None, final="", served_block=None,
                          answer_key_block=None, known_entry_ids=None,
                          uid=None, mode=None, timeout=None, turn_id=None):
    """Return the safe replacement (enforce + guard), else "".

    Fail-open on every error path: no key, no uid, admin, timeout, mode=off,
    a short/unrelated turn, guard already applied, or any exception -> keep
    the original reply.

    In SHADOW the verdict cannot change the reply, so waiting for it would be
    pure cost - the call runs off the turn (see _spawn) and this returns
    immediately with the reply untouched. In ENFORCE it blocks, because the
    verdict decides whether the student ever sees the text.

    ``turn_id`` is normally read off the message (the runtime stamps
    ``_ncea_turn_id`` once per turn); it tells the evidence rebuild WHICH
    turn's served/graded rows to use instead of a wall-clock window that
    cannot separate two turns minutes apart.
    """
    try:
        if not final or GUARDED_MARKER in final:
            return ""
        # An unrecognised explicit mode must NOT fall through to the enforce
        # branch below - normalise it back to the env/default first, so a
        # typo can never escalate from shadow to rewriting live replies.
        mode = norm_mode(mode) or gate_mode()
        if mode == "off":
            return ""
        uid = uid or _student_uid(msg)
        if not uid:
            return ""
        if not gate_api_key():
            return ""
        args = (msg, final, mode, uid, served_block, answer_key_block,
                known_entry_ids, timeout, turn_id)
        if mode == "shadow":
            if _spawn(_safe_evaluate(*args)) is None:
                await _safe_evaluate(*args)   # no loop: degrade to inline
            return ""
        verdict = await _safe_evaluate(*args)
        if not verdict or verdict["verdict"] != "guard":
            return ""
        logger.warning("ncea jev gate enforce uid=%s reasons=%s",
                       uid, ",".join(verdict["reasons"]))
        return REPLACEMENT
    except Exception:
        logger.warning("ncea jev gate skipped", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# CLI: --selftest (offline) / --smoke / --case (live)
# ---------------------------------------------------------------------------

_SELFTEST_ANSWERS = [
    # A: recalled key on an unresolvable grading turn -> guard
    ({"definite-answer-no-key"},
     {"states_definite_answer": {"noul": 0.95},
      "answer_backed_by_key": {"noul": 0.05}},
     {"key_present": False, "serve_present": False}),
    # A: key present but the reply asserts a value the key denies -> guard
    ({"answer-not-backed-by-key"},
     {"states_definite_answer": {"noul": 0.9},
      "answer_backed_by_key": {"noul": 0.1}},
     {"key_present": True, "serve_present": False}),
    # A: key present and backed -> pass
    (set(),
     {"states_definite_answer": {"noul": 0.9},
      "answer_backed_by_key": {"noul": 0.95}},
     {"key_present": True, "serve_present": False}),
    # B: composed question, nothing served -> guard
    ({"question-not-served"},
     {"presents_question_to_attempt": {"noul": 0.92}},
     {"key_present": False, "serve_present": False}),
    # B: served but the reply's question does not match -> guard
    ({"question-mismatch"},
     {"presents_question_to_attempt": {"noul": 0.9},
      "question_matches_verified": {"noul": 0.1}},
     {"key_present": False, "serve_present": True}),
    # B: refused to supply it, and what it offers instead IS a bank entry
    # -> excused (the honest shape the excuse exists for)
    (set(),
     {"presents_question_to_attempt": {"noul": 0.9},
      "question_matches_verified": {"noul": 0.05},
      "declines_and_substitutes": {"noul": 0.95}},
     {"key_present": False, "serve_present": True,
      "cited_in_bank": True}),
    # B: the SAME decline without membership -> guard. Live 2026-09-21
    # 00:20:32: a truthful decline carrying a question whose identifying
    # stem was invented (real filename, 0.91 decline). A decline reports
    # intent, never that the substitute is real.
    ({"question-mismatch"},
     {"presents_question_to_attempt": {"noul": 0.85},
      "question_matches_verified": {"noul": 0.04},
      "declines_and_substitutes": {"noul": 0.91}},
     {"key_present": False, "serve_present": True}),
    # B: the presented question resolves to a real bank entry -> excused
    (set(),
     {"presents_question_to_attempt": {"noul": 0.9},
      "question_matches_verified": {"noul": 0.05}},
     {"key_present": False, "serve_present": True,
      "cited_in_bank": True}),
    # B: nothing served, but the reply quotes a real bank entry -> excused
    (set(),
     {"presents_question_to_attempt": {"noul": 0.9}},
     {"key_present": False, "serve_present": False,
      "cited_in_bank": True}),
    # B: "declined" at a soft score is not an excuse -> guard stands
    ({"question-mismatch"},
     {"presents_question_to_attempt": {"noul": 0.9},
      "question_matches_verified": {"noul": 0.1},
      "declines_and_substitutes": {"noul": 0.6}},
     {"key_present": False, "serve_present": True}),
    # ambiguous middle -> pass (conservative)
    (set(),
     {"states_definite_answer": {"noul": 0.55},
      "presents_question_to_attempt": {"noul": 0.6}},
     {"key_present": False, "serve_present": False}),
    # C: contradiction with enough confidence -> guard
    ({"location-contradicts-verified"},
     {"location_claim": {"type": "choice", "choice": "contradicts",
                         "confidence": 0.8}},
     {"key_present": False, "serve_present": True}),
    # C: contradiction but low confidence -> pass
    (set(),
     {"location_claim": {"type": "choice", "choice": "contradicts",
                         "confidence": 0.4}},
     {"key_present": False, "serve_present": True}),
    # C: contradiction on a turn that openly offers a VERIFIED alternative
    # -> excused (whose Rule 3 half would otherwise re-guard what Rule 2
    #    had just let through)
    (set(),
     {"presents_question_to_attempt": {"noul": 0.93},
      "question_matches_verified": {"noul": 0.03},
      "declines_and_substitutes": {"noul": 0.95},
      "location_claim": {"type": "choice", "choice": "contradicts",
                         "confidence": 0.95}},
     {"key_present": False, "serve_present": True,
      "cited_in_bank": True}),
    # C: the same decline with NO membership -> Rule 3 stands by itself
    ({"location-contradicts-verified"},
     {"declines_and_substitutes": {"noul": 0.91},
      "location_claim": {"type": "choice", "choice": "contradicts",
                         "confidence": 0.81}},
     {"key_present": False, "serve_present": True}),
    # C: contradiction excused because the presented question is a bank entry
    (set(),
     {"location_claim": {"type": "choice", "choice": "contradicts",
                         "confidence": 0.9}},
     {"key_present": False, "serve_present": True,
      "cited_in_bank": True}),
]


def _selftest():
    ok = True
    for i, (want, answers, flags) in enumerate(_SELFTEST_ANSWERS, 1):
        got = decide(answers, flags["key_present"], flags["serve_present"],
                     cited_in_bank=bool(flags.get("cited_in_bank")))
        if got["verdict"] != ("guard" if want else "pass") or \
                set(got["reasons"]) != want:
            ok = False
            print(f"FAIL case {i}: want={want or 'pass'} got={got}")
    # schema sanity: the question contract JEV expects
    q = build_questions()
    assert q["states_definite_answer"]["type"] == "noul"
    assert q["declines_and_substitutes"]["type"] == "noul"
    assert q["location_claim"]["type"] == "choice"
    assert "contradicts" in q["location_claim"]["criteria"]
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def _smoke(model=MODEL_PINNED):
    state = build_state(
        student_message="给我一道91579微积分真题，这题答案是 3.65 吗？",
        tutor_reply="是的，这道题最终答案是 3.65，来自 2024 年 Q1(d)。",
        served_block="[SERVED NCEA QUESTION ...] 91579-2024-Q1d",
        answer_key_block="[VERIFIED ANSWER KEY] Entry: 91579-2024-Q1d\n"
                         "Correct answer: 4.21",
        known_entry_ids=["91579-2024-Q1d"])
    resp = call_systemone(state, build_questions(), model=model)
    print("model:", resp.get("model"))
    print(json.dumps(resp.get("answers"), indent=2, ensure_ascii=False))
    v = decide(resp.get("answers") or {}, key_present=True, serve_present=True)
    print("DECIDE:", json.dumps(v, ensure_ascii=False))
    return 0


def _run_case(rec, model=MODEL_PINNED):
    state = build_state(
        student_message=rec.get("student_message", ""),
        tutor_reply=rec.get("tutor_reply", rec.get("reply", "")),
        served_block=rec.get("served_block", ""),
        answer_key_block=rec.get("answer_key_block", ""),
        known_entry_ids=rec.get("known_entry_ids"))
    resp = call_systemone(state, build_questions(), model=model)
    # A fixture may pin cited_in_bank so its expectation is deterministic
    # offline; otherwise resolve it against the real bank files.
    cited = rec.get("cited_in_bank")
    if cited is None:
        cited = cite_in_bank(rec.get("tutor_reply", rec.get("reply", "")))
    v = decide(resp.get("answers") or {},
               key_present=bool(rec.get("answer_key_block")),
               serve_present=bool(rec.get("served_block")),
               cited_in_bank=bool(cited))
    return resp, v


def _case(path, model=MODEL_PINNED):
    rec = json.loads(open(path, encoding="utf-8").read())
    resp, v = _run_case(rec, model)
    print("model:", resp.get("model"))
    print(json.dumps(resp.get("answers"), indent=2, ensure_ascii=False))
    print("DECIDE:", json.dumps(v, ensure_ascii=False))
    return 0


def _batch_cases(path, model=MODEL_PINNED):
    """Live batch over a JSONL of labelled cases (batch=False to dry-run)."""
    ok = n = 0
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rec = json.loads(line)
        if rec.get("batch") is False:
            continue
        n += 1
        name = rec.get("name", f"case{i}")
        want = rec.get("expect", "")
        try:
            resp, v = _run_case(rec, model)
        except JevError as e:
            print(f"[{name}] ERROR {e}")
            continue
        got = v["verdict"]
        mark = "OK " if (not want or got == want) else "XX "
        if mark == "OK ":
            ok += 1
        a = resp.get("answers") or {}
        print(f"{mark}[{name}] want={want or '?':5} got={got:5} "
              f"reasons={','.join(v['reasons']) or '-'} "
              f"| nouls=" + ",".join(
                  f"{k.split('_')[0]}={a[k].get('noul')}"
                  for k in ("states_definite_answer", "answer_backed_by_key",
                            "presents_question_to_attempt",
                            "question_matches_verified",
                            "declines_and_substitutes")
                  if isinstance(a.get(k), dict)))
        loc = a.get("location_claim")
        if isinstance(loc, dict) and loc.get("choice"):
            print(f"      location={loc['choice']} conf={loc.get('confidence')}")
    print(f"\nBATCH: {ok}/{n} matched expectation (model={model})")
    return 0 if ok == n else 1


def summarize_events(events):
    """Operator report over ncea_jev_gate.jsonl rows. Pure function.

    This is what you read after the shadow days: how often the gate WOULD
    have withheld a reply, why, and what it costs. Pure so it is testable.
    """
    events = [e for e in (events or []) if isinstance(e, dict)]
    n = len(events)
    guards = [e for e in events if str(e.get("verdict")) == "guard"]
    errors = [e for e in events if str(e.get("verdict")) == "error"]
    skipped = [e for e in events if str(e.get("verdict")) == "skipped"]
    # Only rows where a verdict was actually reached can tell you anything
    # about the policy. Errors and sheds are reported separately and must
    # never dilute the rate into looking safe.
    evaluated = n - len(errors) - len(skipped)
    reasons, modes, models = {}, {}, {}
    cited = 0
    stamped = 0
    evid = {}
    lat = []
    for e in events:
        if e.get("cited_in_bank"):
            cited += 1
        if str(e.get("turn") or ""):
            stamped += 1
        for src in (e.get("evidence") or {}).values():
            evid[str(src)] = evid.get(str(src), 0) + 1
        m = str(e.get("mode") or "?")
        modes[m] = modes.get(m, 0) + 1
        mod = str(e.get("model") or "?")
        models[mod] = models.get(mod, 0) + 1
        for r in e.get("reasons") or []:
            reasons[str(r)] = reasons.get(str(r), 0) + 1
        try:
            v = int(e.get("latency_ms") or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            lat.append(v)
    lat.sort()

    def _pct(p):
        if not lat:
            return 0
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    return {
        "calls": n,
        "guards": len(guards),
        "evaluated": evaluated,
        "errors": len(errors),
        "skipped": len(skipped),
        # guard_rate is over calls that REACHED a verdict - a broken or
        # shed-heavy gate must not be able to dilute it toward zero and
        # look safe.
        "guard_rate": (round(len(guards) / evaluated, 3)
                       if evaluated else 0.0),
        "error_rate": round(len(errors) / n, 3) if n else 0.0,
        "skip_rate": round(len(skipped) / n, 3) if n else 0.0,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        # How often Rule 2's deterministic excuse fired (the reply quoted a
        # real bank entry). A guard row that also has this set is still worth
        # reading: it means Rule 1 or Rule 3 fired, not the mismatch branch.
        "cited_in_bank": cited,
        # Is per-turn attribution actually live? `turn_stamped` counts rows
        # that carried a turn id, and `evidence` says which rule selected each
        # ledger's evidence: "turn" is exact, "recency"/"none" means the weak
        # window rule ran (unstamped install, or a row predating the stamp).
        # A shadow readout of 0 here is the signal that the runtime stamp did
        # not land, and that the cross-turn mixing it fixes is still possible.
        "turn_stamped": stamped,
        "evidence": dict(sorted(evid.items())),
        "modes": modes,
        "models": models,
        "latency_ms": {"p50": _pct(0.5), "p95": _pct(0.95),
                       "max": lat[-1] if lat else 0},
    }


def _report(paths):
    """--report PATH[: read one or more ncea_jev_gate.jsonl files."""
    rows = []
    for path in paths:
        for ln in open(path, encoding="utf-8").read().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except Exception:
                continue
    print(json.dumps(summarize_events(rows), indent=2, ensure_ascii=False))
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true",
                    help="offline policy checks (no network)")
    ap.add_argument("--smoke", action="store_true",
                    help="one live call against the API")
    ap.add_argument("--case", metavar="FILE",
                    help="live verdict for one JSON case file")
    ap.add_argument("--cases", metavar="FILE",
                    help="live batch verdicts for a JSONL of cases")
    ap.add_argument("--report", metavar="FILE", nargs="+",
                    help="summarise one or more ncea_jev_gate.jsonl ledgers")
    ap.add_argument("--model", default=MODEL_PINNED)
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.smoke:
        return _smoke(args.model)
    if args.case:
        return _case(args.case, args.model)
    if args.cases:
        return _batch_cases(args.cases, args.model)
    if args.report:
        return _report(args.report)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
