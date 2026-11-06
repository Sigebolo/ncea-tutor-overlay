"""Post-turn output guard v2 (site patch helper).

Pre-turn blocks decide what the model MAY teach (serve/grade/review
injection); this is the last gate on what actually GOES OUT. If the final
reply presents a question entry the student was never served or graded
(no such id in ncea_served.jsonl / ncea_graded.jsonl), the citation is
unverified - replace the reply with a short safe refusal instead of
sending a possibly fabricated question.

Fail-open everywhere: no uid / admin / no citation / cited entry known /
idempotence marker / write errors -> return "" (keep the original final).

Runs AFTER autolog in the post-turn hook so the verdict gate still sees
the raw final; history keeps the raw text for audit, the user gets the
safe replacement ("拒发": refuse to send, not silent edit).
"""

import json
import logging
import re
import time

logger = logging.getLogger(__name__)

# Full verified-bank id, e.g. 91579-2025-Q1e / 91392-2022-Q1aiii.
_ENTRY_RE = re.compile(r"(?<!\d)(\d{5})-(\d{4})-(Q[A-Za-z0-9]+)")
# Loose citation triple parts (code / year / Q-part) for replies that cite
# without a full id.
_CODE_RE = re.compile(r"(?<!\d)(9\d{4})(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_PART_RE = re.compile(
    r"Q\s*\d+\s*\(?[a-z]+\)?(?:\(?[a-z]+\)?)?"
    r"|Question\s+(?:One|Two|Three|Four|Five|\d+)\s*\(?[a-z]+\)?", re.I)
# Serving-format signals: the reply presents itself as delivering a
# question (not teaching about one). Correct serving turns carry these
# too, but their entry is in the served set so they stay exempt.
_SERVE_MARKERS = ("location:", "pdf", "served", "白纸")
# Idempotence: never guard our own replacement.
GUARDED_MARKER = "[WITHDRAWN - unverified question]"

# Trailing junk the model appends to ids (the F2 lesson: entry id with a
# `**` markdown tail). Stripped before the membership check so
# **91392-2025-Q3bi** still resolves to the bank id.
_TAIL_STRIP = "*_`.,;:!?)\\]}\"'，。；：！？”’"

REPLACEMENT = (
    "[WITHDRAWN - unverified question]\n"
    "I couldn't verify that question against the question bank, so I'm "
    "not sending it. Tell me the standard code (e.g. 91579) and I'll serve "
    "a verified past-paper question with its PDF link - or send a photo of "
    "the paper and I'll mark it against the key."
    "（刚才引用的题目在题库里核验不到，已撤回不发送；告诉我 standard 代码我就从 verified bank 取真题，或直接拍照按 key 判分。）"
)


def _norm_entry(code, year, qraw):
    q = qraw.rstrip(_TAIL_STRIP)
    return f"{code}-{year}-{q}"


def extract_entry_ids(text):
    """Full bank ids cited in the text (markdown tails stripped)."""
    return [_norm_entry(c, y, q) for c, y, q in _ENTRY_RE.findall(text or "")]


def _has_citation_triple(text):
    """Code + year + Q-part co-occurring within a 120-char window."""
    t = text or ""
    for m in _CODE_RE.finditer(t):
        win = t[max(0, m.start() - 60):m.end() + 60]
        if _YEAR_RE.search(win) and _PART_RE.search(win):
            return True, m.group(1)
    return False, ""


def check_output(final, known_ids):
    """Pure gate: (\"guard\", cited) or (\"pass\", \"\").

    Rule 1 (exact): a full bank id never served/graded to this student.
    Rule 2 (coarse): a code+year+part citation in serving format whose
    code-year pair the student was never served/graded. Deliberately
    narrow - plain teaching mentions without serving markers pass.
    """
    text = final or ""
    if not text or GUARDED_MARKER in text:
        return "pass", ""
    known = set(known_ids or ())
    for eid in extract_entry_ids(text):
        if eid not in known:
            return "guard", eid
    if any(mk in text.lower() for mk in _SERVE_MARKERS):
        hit, code = _has_citation_triple(text)
        if hit:
            for kid in known:
                if kid.startswith(code + "-"):
                    return "pass", ""
            return "guard", code
    return "pass", ""


def _student_uid(msg):
    try:
        actor = getattr(msg, "actor", None)
        uid = getattr(actor, "id", None)
        if uid and not getattr(actor, "is_admin", False):
            return str(uid)
    except Exception:
        pass
    return ""


def _known_ids(root):
    """Served + graded entry ids for this student (audit ledgers)."""
    out = set()
    try:
        from pathlib import Path
        for name in ("ncea_served.jsonl", "ncea_graded.jsonl"):
            p = Path(root) / name
            if not p.exists():
                continue
            for ln in p.read_text(encoding="utf-8").splitlines():
                try:
                    v = (json.loads(ln) or {}).get("entry_id", "")
                    if v:
                        out.add(str(v).rstrip(_TAIL_STRIP))
                except Exception:
                    continue
    except Exception:
        pass
    return out


async def maybe_guard_output(msg=None, final="") -> str:
    """Return the safe replacement, or \"\" to keep the original final."""
    try:
        if not final or GUARDED_MARKER in final:
            return ""
        uid = _student_uid(msg)
        if not uid:
            return ""
        from deeptutor.multi_user.paths import scope_for_user
        root = scope_for_user(uid, is_admin=False).root
        verdict, cited = check_output(final, _known_ids(root))
        if verdict != "guard":
            return ""
        try:
            from pathlib import Path
            p = Path(root) / "ncea_guard.jsonl"
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": int(time.time()), "kind": "guard-rewrite",
                     "cited": cited}, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning("ncea guard audit write skipped", exc_info=True)
        logger.warning("ncea output guard rewrote uid=%s cited=%s", uid, cited)
        return REPLACEMENT
    except Exception:
        logger.warning("ncea output guard skipped", exc_info=True)
        return ""
