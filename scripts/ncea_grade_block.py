"""Deterministic grading-key injection (site patch helper).

Photo-grading turns in fresh sessions have no answer key in context,
so the model "recalls" one (27 vs 13.5 vs 3.65 spiral, E2E-proven).
When the turn looks like grading AND an entry resolves, inject the
verified answer key. Never invents: unresolvable -> silent.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# A grading turn must be recognised from the student's own wording. The first
# pattern set only knew "mark my"/"grade"; a student who answered the question
# the tutor just served writes "please mark it" / "is this correct" / "how did I
# do" - none matched, so no verified key was injected and the model graded from
# memory (E2E-proven: answered 2110 correctly, got "you have not sent a photo").
# Every added alternative stays a REQUEST TO BE MARKED, never a bare "mark" or
# "check" ("mark the point on the graph" is work, not a marking request).
GRADE_RE = re.compile(
    r"对吗|对不对|打分|批改|改一下|帮我改|给我改|看下我做|看看我做|做得.*吗|判分|评改|"
    r"grade|"
    r"mark\s+(?:it|this|these|mine|my)\b|please\s+mark\b|"
    r"check\s+(?:it|this|these|mine|my\s+(?:answer|work|working))\b|"
    r"is\s+(?:this|that|it)\s+(?:correct|right)\b|"
    r"did\s+i\s+(?:get|do)\b|how\s+did\s+i\s+do\b|what\s+did\s+i\s+get\b",
    re.I,
)

# A grading turn that names no standard and no part ("please mark it") can only
# be attributed through the served ledger: the question the tutor just handed
# over. The pointer is trusted only while it is fresh, because a stale one would
# grade today's working against last week's key - and _number_check would then
# call a correct answer MISMATCH. 45 min covers answer-then-mark in one sitting.
RECENT_SERVED_SECONDS = 45 * 60
CODE_RE = re.compile(r"(?<!\d)(9\d{4})(?!\d)")
PART_RE = re.compile(
    r"Q\s*(\d+)\s*\(?([a-z]+)\)?(?:\(?([a-z]+)\)?)?"
    r"|Question\s+(One|Two|Three|Four|Five|\d+)\s*\(?([a-z]+)\)?(?:\(?([a-z]+)\)?)?", re.I)
QN = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?:\s*年)?(?!\d)")
_YEAR_BEFORE_PART_RE = re.compile(
    r"\b(20\d{2})\s*(?:年\s*)?(?:Q\s*\d|\bQuestion\b|第?\s*题)", re.I)


def _year_hint(text):
    """Year tied to a cited part ('2024 Q1(d)') or the last named year.

    Returns None when the text names no year. Used to pick among
    same-part entries across years (91261/91262/91267 keep 2024 AND
    2025 entries for the same Q-part, so a bare first-match can inject
    the wrong paper's key)."""
    t = text or ""
    m = _YEAR_BEFORE_PART_RE.search(t)
    if m:
        return m.group(1)
    years = YEAR_RE.findall(t)
    return years[-1] if years else None


# Trailing-period aware: 'pH = 5.68.' must yield 5.68 - a period directly
# after the number is the sentence end, not a decimal continuation. The
# lookahead only rejects a decimal tail ('.5') or a following digit; it
# still rejects an embedded decimal '.' mid-number (e.g. '3.14' stays
# one token) and a sub-letter subscript ('10^-3' is the sci fold's job,
# not NUM_RE's). The key line 'pH = 5.68.' used to drop 5.68 because the
# period after it read as a decimal continuation - making a correct claim
# MISMATCH. Symmetric on the student side: Chinese '。' already passed.
NUM_RE = re.compile(r"(?<![\d.])(-?\d+(?:\.\d+)?)(?!\d)(?!\.\d)")
# Citation tokens that carry no answer claim: 'Q1(d) 写完了', '91579 考什么',
# '2024 的卷子'. Stripped before the claim is extracted so 'Q1(d)' does not
# masquerade as a claimed value of 1 (shared by the injection line and the
# auto-log gate - one function, both sides).
_CITE_RE = re.compile(
    r"Q\s*\d+\s*(?:\(?[a-z]+\)?)"
    r"|Question\s+(?:One|Two|Three|Four|Five|\d+)\s*(?:\(?[a-z]+\)?)"
    r"|\b9\d{4}\b"
    r"|\b20\d{2}\s*年?"
    r"|第?\s*\d+\s*题", re.I)


# The verified banks write scientific notation two ways that must fold to
# ONE value each, or a correct answer in one spelling reads as MISMATCH
# against a key in the other ('1.10e-12' vs '1.1 × 10⁻¹²'):
#   * ASCII e-notation: 1.10e-12, 9.79e-3
#   * ×10 with a superscript/plain exponent: 9.79 × 10⁻³, 2.11e-6
# Superscript digits/minus appear in mixed codepoints (Latin-1 U+00B2/3/9
# and the U+2070-2079 block; minus as U+2212 or U+207B), so normalize ALL
# of them before tokenizing. A bare scan of '9.79 × 10⁻³' yields [9.79, 10]
# and a correct claim would not match - hence the fold.
_SUPER_MAP = {
    "\u00b2": "2", "\u00b3": "3", "\u00b9": "1",
    "\u2070": "0", "\u2071": "1", "\u2072": "2", "\u2073": "3",
    "\u2074": "4", "\u2075": "5", "\u2076": "6", "\u2077": "7",
    "\u2078": "8", "\u2079": "9",
    "\u2212": "-", "\u207b": "-",
}
# mantissa [e/E] [+-] exp     -> 1.10e-12. The lookbehind keeps '1.10e-12'
# from folding as mantissa '10' (the '.10' is part of 1.10).
_SCI_E_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*[eE]\s*([+-]?)\s*(\d+)")
# mantissa × 10 [+-] exp      -> 9.79 × 10⁻³ (after _SUPER_MAP translate)
_SCI_M10_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*[×x*]\s*10\s*([+-]?)\s*(\d+)")


def _sci_value(mant, sign, exp):
    """mantissa × 10^±exp - the sign flips the EXPONENT, not the value."""
    e = int(exp)
    if sign == "-":
        e = -e
    return float(mant) * (10.0 ** e)


def _numbers(text):
    t = (text or "").translate(str.maketrans(_SUPER_MAP))
    out = []
    try:
        for m in _SCI_E_RE.finditer(t):
            out.append(_sci_value(m.group(1), m.group(2), m.group(3)))
        t = _SCI_E_RE.sub(" ", t)
        for m in _SCI_M10_RE.finditer(t):
            out.append(_sci_value(m.group(1), m.group(2), m.group(3)))
        t = _SCI_M10_RE.sub(" ", t)
        out.extend(float(x) for x in NUM_RE.findall(t))
    except Exception:
        return []
    return out


def _claimed_numbers(text):
    """Student's numeric claims with citations stripped (see _CITE_RE)."""
    return _numbers(_CITE_RE.sub(" ", text or ""))


def _number_check(user_text, answer):
    """Deterministic digit compare: student's LAST number vs key numbers.

    Returns '' (no claim to check) or a MATCH/MISMATCH verdict line.
    This kills the whole class where the model blesses a wrong final
    number (0.41 vs key 3.65) after 'verifying' only the student's own
    arithmetic. Numbers don't misread; equations do.

    Citation stripping (_claimed_numbers): 'Q1(d) 写完了' must NOT count
    as claiming 1 - the part citation is navigation, not an answer."""
    mine = _claimed_numbers(user_text)
    keys = _numbers(answer)
    if not mine or not keys:
        return ""
    claimed, hit = mine[-1], False
    for k in keys:
        if k == 0 and claimed == 0:
            hit = True
            break
        # Relative tolerance only. The old absolute floor (0.015) let two
        # keys a factor of ten apart MATCH at the e-4..e-12 magnitudes the
        # chemistry/physics verified keys live at (1.3e-4 vs 1.3e-5 would
        # have blessed a wrong power of ten). Near-zero plain values keep
        # working: the zero-equality branch above covers them.
        if abs(claimed - k) <= 0.02 * abs(k):
            hit = True
            break
    if hit:
        return (f"NUMBER CHECK: student claims {claimed:g}, key contains "
                f"a matching number. Proceed to grade the working.")
    return (
        f"NUMBER CHECK: student claims {claimed:g}, key numbers are "
        f"{', '.join('%g' % k for k in keys)} - MISMATCH. The final "
        f"answer is wrong. Say so FIRST, then point at the first "
        f"diverging line. Do NOT declare it correct under any branch.\n"
        f"RE-WORK (Socratic, never give the value): ask the student to "
        f"check the FIRST line where their working stops matching the key "
        f"step by step - one targeted question, no worked answer, no "
        f"correction spelled out; they redo that step and you re-grade."
    )


def _qkey(qstr):
    m = re.match(r"Q(\d+)([a-z]*)", qstr, re.I)
    return (int(m.group(1)), m.group(2).lower()) if m else (-1, "")


def _read_bank(code):
    from deeptutor.tools.builtin import _ncea_parse_bank
    from deeptutor.tools.builtin import _ncea_read_skill_file as _read
    try:
        text = _read(f"references/qbank_{code}.md")
    except Exception:
        return []
    if not text:
        return []
    return _ncea_parse_bank(text)


def _recent_served(uid, max_age=RECENT_SERVED_SECONDS):
    """True when this student's last served row is younger than ``max_age``."""
    try:
        import time
        from pathlib import Path
        from deeptutor.multi_user.paths import scope_for_user
        p = Path(scope_for_user(uid, is_admin=False).root) / "ncea_served.jsonl"
        if not p.exists():
            return False
        last = 0
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                v = json.loads(ln)
            except Exception:
                continue
            if v.get("entry_id"):
                last = int(v.get("ts") or 0)
        return bool(last) and (time.time() - last) <= max_age
    except Exception:
        return False


def _last_served(uid):
    try:
        from pathlib import Path
        from deeptutor.multi_user.paths import scope_for_user
        p = Path(scope_for_user(uid, is_admin=False).root) / "ncea_served.jsonl"
        eid = ""
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines():
                try:
                    v = json.loads(ln).get("entry_id", "")
                    if v:
                        eid = v
                except Exception:
                    continue
        return eid
    except Exception:
        return ""


def _log_graded(uid, entry, code, year, answer, turn_id=""):
    """Append the injected entry to the per-student graded ledger.

    Lets post-turn autolog attribute a text answer to the last GRADed
    entry (what the key injection actually targeted) rather than the
    last-served one - a consolidation turn in the same chat can move
    the served pointer without moving the graded one. Never breaks
    the turn: write errors are swallowed.

    ``turn_id`` is the turn this key was injected for. The post-turn JEV
    gate reads this ledger to rebuild "what did THIS turn inject", and
    several turns can sit inside its recency window - without the id a
    key injected two turns ago would be scored against today's reply.
    """
    try:
        import time
        from pathlib import Path
        from deeptutor.multi_user.paths import scope_for_user
        p = Path(scope_for_user(uid, is_admin=False).root) / "ncea_graded.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(time.time()), "entry_id": entry.get("id", ""),
               "code": code, "year_hint": year,
               "answer": answer[:300], "turn": str(turn_id or "")}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("ncea graded-ledger write skipped", exc_info=True)


def _e_year(eid):
    m = re.match(r"^\d{5}-(\d{4})-", eid or "")
    return m.group(1) if m else ""


def _resolve(code_hint, part_hint, uid, year_hint=None):
    """Return (code, entry) or (None, None). Pure-ish (unit-tested core).

    year_hint: when the student names a year next to the part (e.g.
    '2024 Q1(d)'), that year's bank entry is preferred; otherwise the
    sticky year of the last-served same-code entry; otherwise first-match
    in bank order (the pre-fix behaviour, kept as the safe fallback)."""
    m_last = re.match(r"^(\d{5})-(\d{4})-(Q\S*)$", _last_served(uid) or "")
    codes = []
    if code_hint and len(code_hint) == 5:
        codes = [code_hint]
        sticky_year = m_last.group(2) if (
            m_last and m_last.group(1) == code_hint) else ""
    else:
        if not m_last:
            return None, None
        codes = [m_last.group(1)]
        sticky_year = m_last.group(2)
    target_year = year_hint or sticky_year
    for code in codes:
        try:
            entries = _read_bank(code)
        except Exception:
            continue
        if not entries:
            continue
        if part_hint[0] > 0:
            if target_year:
                for e in entries:
                    if (_e_year(e["id"]) == target_year
                            and _qkey(e["id"].rsplit("-", 1)[-1]) == part_hint):
                        return code, e
            for e in entries:
                if _qkey(e["id"].rsplit("-", 1)[-1]) == part_hint:
                    return code, e
            for e in entries:
                qn, _ = _qkey(e["id"].rsplit("-", 1)[-1])
                if qn == part_hint[0]:
                    return code, e
        else:
            m2 = re.match(r"^(\d{5})-(\d{4})-(Q\S*)$", _last_served(uid) or "")
            if m2:
                for e in entries:
                    if e["id"] == m2.group(0):
                        return code, e
        return code, None
    return None, None


def _parse_part(text):
    """Locate a part like 'Q3c', 'Q3b(i)', 'Q1(a)(iii)', 'Question Two (d)'.

    Nested sub-parts are folded flat ('Q3b(i)' -> (3, 'bi')); that matches
    the flat tails the verified bank entries carry (91392-2025-Q3bi,
    91392-2022-Q1aiii) so the year resolver hits the SAME year's key."""
    m = PART_RE.search(text or "")
    if not m:
        return (-1, "")
    if m.group(1):
        letters = ((m.group(2) or "").lower() + (m.group(3) or "").lower())
        return (int(m.group(1)), letters)
    w = (m.group(4) or "")
    q = QN.get(w.lower(), int(w) if w.isdigit() else -1)
    letters = ((m.group(5) or "").lower() + (m.group(6) or "").lower())
    return (q, letters)


async def build_grade_block(text, uid, has_photo=False, turn_id="") -> str:
    """Return '' or a verified ANSWER KEY block for grading this turn.

    ``turn_id`` rides onto the graded-ledger row so the post-turn JEV gate
    can tell this turn's injection from an earlier one (see _log_graded).
    """
    if not uid:
        return ""
    grading = bool(has_photo) or bool(GRADE_RE.search(text or ""))
    if not grading:
        return ""
    m = CODE_RE.search(text or "")
    code_hint = m.group(1) if m else ""
    part_hint = _parse_part(text)
    if not code_hint and not has_photo and part_hint[0] < 0:
        # "please mark it" names neither standard nor part: the student is
        # answering the question this chat just served, and re-stating its code
        # is not something a student does. Resolve through the served ledger
        # (fresh only - see RECENT_SERVED_SECONDS). No ledger row, or one that
        # has gone stale, still resolves to nothing, exactly as before.
        if not _recent_served(uid):
            return ""
    year = _year_hint(text or "")
    code, entry = _resolve(code_hint, part_hint, uid, year_hint=year)
    if not entry:
        return ""
    bullets = entry.get("bullets", {})
    loc = bullets.get("location", "")
    answer = bullets.get("answer", "")
    if not answer:
        return ""
    _log_graded(uid, entry, code, year, answer, turn_id)
    logger.warning("ncea grade block injected: uid=%s entry=%s", uid, entry["id"])
    numline = _number_check(text, answer)
    return (
        "[VERIFIED ANSWER KEY - deterministic, grade against this]\n"
        f"Entry: {entry['id']}\n"
        f"Location: {loc}\n"
        f"Correct answer: {answer}\n"
        + (numline + "\n" if numline else "") +
        "RULES: quote the student's photo back first; compare their FINAL "
        "number to the key above with run_code; on mismatch point at the "
        "first diverging line. NEVER restate the answer from memory - "
        "these lines ARE the answer. If the tool result disagrees with "
        "these lines, the lines win."
    )
