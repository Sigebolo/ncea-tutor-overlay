"""Post-turn auto-log for NCEA photo grading (site patch helper).

The model never calls log_mistake on its own (proven across Flash/Pro/
Gemini turns), so the server writes the per-student wrong book itself
when a photo-grading turn ends with a verdict. Same file + schema as
LogMistakeTool, so Wrong-Only lights up identically.

Trigger (photo path, unchanged):
 1. turn carried a photo (attachment record with image type/mime/name)
 2. student has a served entry (per-student served ledger, take last)
 3. final reply carries a verdict (POS -> resolve, NEG -> mistake)
Trigger (text path, numeric gate first):
 1. no photo
 2. student's text is a grading turn (matches GRADE_RE - the same signal
    that injects the answer key into the grading prompt, so the two
    gates never drift apart)
 3. the grade block's own _number_check on the student's claimed numbers
    vs the verified key the model was handed:
      MISMATCH -> log a mistake regardless of the model's prose
      MATCH    -> suppress regardless of the model's prose
      no numbers -> fall back to _text_verdict (a narrow below-target
                    phrase set with a strong-correct-phrase veto; praise
                    turns like "考试不扣分" never fire)
Exemptions: no uid / admin / no served entry / log_mistake already
called this turn / write errors (log + skip, never break the turn).
"""

import json
import logging
import re
import time

import importlib.util
import os

logger = logging.getLogger(__name__)

NEG = ("merit", "没到 e", "未到 e", "扣分", "漏了", "错在", "不对", "错误",
       "不完整", "差一点", "achieved", "not achieved")
POS = ("满分", "完全正确", "excellence 达成", "拿满", "全部正确")

ENTRY_RE = re.compile(r"^\d{5}-\d{4}-Q")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic")


def _photo_records(msg):
    """Image attachment dicts from this turn (for QB image refs)."""
    out = []
    try:
        meta = getattr(msg, "metadata", None) or {}
        for r in meta.get("_attachment_records") or []:
            if not isinstance(r, dict):
                continue
            if r.get("type") == "image" or str(r.get("mime_type") or "").startswith("image/"):
                out.append(r)
        for m in getattr(msg, "media", None) or []:
            if isinstance(m, dict) and m.get("type") == "image" and m not in out:
                out.append(m)
    except Exception:
        pass
    return out


def _has_photo(msg) -> bool:
    if _photo_records(msg):
        return True
    try:
        meta = getattr(msg, "metadata", None) or {}
        for r in meta.get("_attachment_records") or []:
            if not isinstance(r, dict):
                continue
            if str(r.get("filename") or r.get("path") or "").lower().endswith(_IMG_EXT):
                return True
    except Exception:
        pass
    return False


def _student_uid(msg):
    try:
        actor = getattr(msg, "actor", None)
        uid = getattr(actor, "id", None)
        if uid and not getattr(actor, "is_admin", False):
            return str(uid)
    except Exception:
        pass
    return ""


def _tool_called(turn_events, name: str) -> bool:
    try:
        for e in turn_events or []:
            if isinstance(e, dict) and e.get("type") == "tool_call":
                if name in json.dumps(e.get("metadata", {})):
                    return True
                if name in str(e.get("content", "")):
                    return True
    except Exception:
        pass
    return False


def classify_verdict(final: str):
    """Return ('resolve'|'mistake'|None, detail). Pure function (unit-tested)."""
    text = final or ""
    low = text.lower()
    has_neg = any(k in low for k in NEG)
    has_pos = any(k in text for k in POS)
    if has_pos and not has_neg:
        return "resolve", ""
    if not has_neg:
        return None, ""
    grade = "below-target"
    for g in ("not achieved", "achieved", "merit"):
        if g in low:
            grade = g
            break
    gap = ""
    for line in text.split("\n"):
        s = line.strip()
        if len(s) > 8 and any(k in s.lower() for k in NEG):
            gap = s[:120]
            break
    return "mistake", {"result": grade, "gap": gap or "(verdict without detail)"}


def _last_served(root):
    try:
        from pathlib import Path
        p = Path(root) / "ncea_served.jsonl"
        if not p.exists():
            return ""
        eid = ""
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

def _last_graded(root):
    """Last graded ledger (entry_id, answer) - what the key injection
    actually targeted this session. Newer, authoritative signal; falls
    back to ('', '') when the ledger is empty (pre-ledger sessions)."""
    try:
        from pathlib import Path
        p = Path(root) / "ncea_graded.jsonl"
        if not p.exists():
            return "", ""
        eid, ans = "", ""
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                v = json.loads(ln)
                if v.get("entry_id"):
                    eid = v["entry_id"]
                    ans = str(v.get("answer", "") or "")
            except Exception:
                continue
        return eid, ans
    except Exception:
        return "", ""

# Grading-turn signal + gate machinery for the TEXT path: the grade block
# is the single source of truth for (a) what counts as a grading turn and
try:
    from deeptutor.agents._shared import ncea_grade_block as _gb
    _GRADE_RE = _gb.GRADE_RE
except Exception:
    # Local sibling fallback: the grade block lives next to this helper in
    # both the repo (scripts/) and the install site (agents/_shared/).
    # Loading the same file keeps the gate's resolver/number-check
    # byte-identical to the injection's - they can never drift apart.
    _gb = None
    try:
        _gb_spec = importlib.util.spec_from_file_location(
            "ncea_grade_block",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "ncea_grade_block.py"))
        if _gb_spec is not None:
            _gb = importlib.util.module_from_spec(_gb_spec)
            _gb_spec.loader.exec_module(_gb)
    except Exception:
        pass
    if _gb is None:
        import re as _re
        _GRADE_RE = _re.compile(
            r"对吗|对不对|打分|批改|改一下|帮我看|做得.*吗|grade|mark my|check my|判分|评改")
    else:
        _GRADE_RE = _gb.GRADE_RE

# Definite wrong-answer statements the model emits when it concludes the
# student is below target. Deliberately narrow: no bare "错误"/"扣分"/"error"
# (the latter also appears in praise, e.g. "考试不扣分", which must NOT
# trigger).
_DEF_NEG = ("不对", "是错的", "错了", "incorrect", "wrong", "made an error",
            "no achievement", "not achieved", "得 n", "below-target",
            "below target", "低于目标", "未达到", "没到 merit",
            "没到 excellence", "didn't pass", "not pass")
# Strong-correct phrases that suppress a mistake filing even if a soft
# negative word co-occurs (a "nice work, just add this" turn).
_STRONG_POS = ("完全正确", "全部正确", "都对", "答对", "答案正确", "合格",
               "掌握了", "掌握补集", "漂亮", "满分", "全对", "做对了",
               "completely correct", "fully correct", "perfect", "not wrong")


def _norm_final(final: str) -> str:
    return (final or "").lower()



def _text_verdict(final: str):
    """Deterministic-ish text grading verdict. Returns ('mistake', detail)
    or (None, ''). Fires ONLY on an explicit wrong-answer statement that no
    strong-correct phrase overrides; stays silent on praise/ambiguous turns.
    Pure function (unit-tested)."""
    text = final or ""
    low = _norm_final(text)
    if any(p in text for p in _STRONG_POS) or any(p in low for p in _STRONG_POS):
        return None, ""
    if not any(n in low for n in _DEF_NEG):
        return None, ""
    gap = ""
    for line in text.split("\n"):
        s = line.strip()
        if len(s) > 8 and any(n in s.lower() for n in _DEF_NEG):
            gap = s[:120]
            break
    return "mistake", {"result": "below-target",
                       "gap": gap or "(text verdict: below-target)"}

def _text_gate(user_text, uid, root):
    """(entry_id, key_answer) of the verified key the model was handed on
    THIS turn, or ("", "") when no key was injected.

    Mirrors build_grade_block's fire condition exactly (code hint OR part
    hint - photo turns are the photo path's business), so the gate and the
    injection can never drift apart. Prefers the graded ledger's last row:
    its answer field is byte-identical to the injected key; a fresh
    _resolve covers sessions predating the ledger."""
    if _gb is None:
        return "", ""
    try:
        m = _gb.CODE_RE.search(user_text or "")
        code_hint = m.group(1) if m else ""
        part_hint = _gb._parse_part(user_text)
        if not code_hint and part_hint[0] < 0:
            return "", ""            # no key injected this turn
        eid_g, ans_g = _last_graded(root)
        if eid_g and ans_g:
            return eid_g, ans_g
        year = _gb._year_hint(user_text)
        _, entry = _gb._resolve(code_hint, part_hint, uid, year_hint=year)
        if entry:
            return (str(entry.get("id", "")),
                    str(entry.get("bullets", {}).get("answer", "")))
    except Exception:
        logger.warning("ncea text-gate resolve skipped", exc_info=True)
    return "", ""


async def _store_photo(uid, rec):
    """Copy turn photo into the student's AttachmentStore; return its item."""
    try:
        from pathlib import Path
        src = rec.get("path") or ""
        p = Path(str(src))
        if not src or not p.exists():
            return None
        from deeptutor.services.partners.interaction import actor_for_account
        from deeptutor.multi_user.paths import user_context
        from deeptutor.services.storage.attachment_store import get_attachment_store
        actor = actor_for_account(uid)
        if actor is None:
            return None
        data = p.read_bytes()
        if len(data) > 8 * 1024 * 1024:
            return None
        name = str(rec.get("filename") or p.name or "photo.png")
        with user_context(actor):
            url = await get_attachment_store().put(
                session_id=f"ncea-{uid}",
                attachment_id=str(rec.get("id") or f"{uid}-{int(time.time())}"),
                filename=name, data=data,
                mime_type=str(rec.get("mime_type") or "image/png"))
        return {"id": str(rec.get("id") or ""), "url": str(url or ""),
                "filename": name,
                "mime_type": str(rec.get("mime_type") or "image/png")}
    except Exception:
        logger.warning("ncea photo store failed", exc_info=True)
        return None


async def maybe_autolog(msg=None, final="", turn_events=None) -> str:
    """Write the wrong-book record if warranted. Returns '' or entry_id.

    Two deterministic triggers, no model involvement:
      * photo path - a photo-grading turn whose verdict is below-target
        (unchanged: classify_verdict on the final reply).
      * text path  - a grading turn (GRADE_RE on the student's text) gated
        by the numeric check of the grade block itself (single source of
        truth, the key the model was actually handed):
          - student's last number vs key -> MISMATCH: log regardless of
            the model's prose (the model's "correct" was a misread);
          - MATCH: suppress, even if the model said "wrong" (numbers
            don't misread; the model's prose is the soft layer);
          - no numbers to compare -> fall back to the verdict gate
            (_text_verdict), original conservative behaviour.
    Entry attribution: text records attach to the graded ledger's last
    entry (what the key injection actually targeted), falling back to
    the last-served entry."""
    try:
        uid = _student_uid(msg)
        if not uid:
            return ""
        if _tool_called(turn_events, "log_mistake"):
            return ""
        from deeptutor.multi_user.paths import scope_for_user
        root = scope_for_user(uid, is_admin=False).root
        entry_id = _last_served(root)
        if not entry_id or not ENTRY_RE.match(entry_id):
            return ""

        photo = _has_photo(msg)
        user_text = str(getattr(msg, "content", "") or "")
        if photo:
            kind, detail = classify_verdict(final)
        else:
            # Text-only grading turn.
            if not _GRADE_RE.search(user_text):
                return ""
            gate_eid, gate_answer = _text_gate(user_text, uid, root)
            if gate_eid and ENTRY_RE.match(gate_eid):
                entry_id = gate_eid          # graded-ledger attribution
            if gate_answer and _gb is not None:
                numline = _gb._number_check(user_text, gate_answer)
                if "MISMATCH" in numline:
                    # Student's numbers diverge from the verified key -
                    # log regardless of the model's prose (kills the
                    # R6 class: model says "correct" but numbers don't).
                    kind, detail = "mistake", {
                        "result": "below-target",
                        "gap": "number does not match verified key"}
                elif numline:
                    # Numbers match the key - suppress regardless of the
                    # model's prose (kills the R7 class: model says "wrong"
                    # but the student's last number is in the key).
                    return ""
                else:
                    # No student numbers to compare - fall back to the
                    # verdict gate (original conservative behaviour).
                    kind, detail = _text_verdict(final)
                    if kind is None:
                        return ""
            else:
                # No key injected this turn - fall back to verdict gate.
                kind, detail = _text_verdict(final)
                if kind is None:
                    return ""
        if kind is None:
            return ""
        from pathlib import Path
        rec = {"ts": int(time.time()), "entry_id": entry_id}
        try:
            ut = user_text[:500]
            if ut.strip():
                rec["user_text"] = ut.strip()
        except Exception:
            pass
        if not photo:
            rec["source"] = "text-grade"
        if kind == "resolve":
            rec["type"] = "resolve"
        else:
            rec.update({"type": "mistake", "result": detail["result"],
                        "gap": detail["gap"]})
        photos = []
        if photo:
            for pr in _photo_records(msg)[:3]:
                item = await _store_photo(uid, pr)
                if item:
                    photos.append(item)
            if photos:
                rec["photos"] = photos
        with open(Path(root) / "ncea_mistakes.jsonl", "a",
                   encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.warning("ncea autolog %s %s %s", uid, kind, entry_id)
        return entry_id
    except Exception:
        logger.warning("ncea autolog skipped", exc_info=True)
        return ""

