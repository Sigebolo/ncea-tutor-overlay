"""Nightly numbers audit: location/URL claims vs verified banks (no LLM).

Run on AU: .venv/bin/python scripts/nightly_numbers.py [--hours N]
Reads partner user sessions, flags:
  F1 unserved serving: reply cites year+Q but turn has no serve record
     (serve_question tool call or served-ledger entry within the turn window)
  F2 unknown location: cited (standard?, year, Q) not in any bank entry
  F3 rogue URL: exam/PDF link not in references/pdf_links.md
Exit 0 always (report, don't alarm). Pure stdlib.
"""
import glob
import json
import os
import re
import sys
import time

HOURS = float(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else 24.0 * 30

# The bank references moved twice: 2026-10 the workspace skill (per-user
# partner dirs stopped carrying them), and the legacy path below still
# resolves on the master machine. First existing candidate wins, so the
# same script runs on AU and locally.
REF_CANDIDATES = (
    os.path.join("data", "user", "workspace", "skills", "ncea-tutor", "references"),
    os.path.join("skills", "ncea-tutor", "references"),
    os.path.join("data", "partners", "partner-208ae166", "workspace",
                 "user", "workspace", "skills", "ncea-tutor", "references"),
)
REF = next((p for p in REF_CANDIDATES if os.path.isdir(p)), REF_CANDIDATES[0])

HDR_RE = re.compile(r"^##\s+(\d{5})-(\d{4})-(Q\S*)")
# Probe/drift turns: session_keys written by our own e2e/probe scripts. They
# exercise refusal paths and pre-bank states on purpose, so their "flags" are
# artifacts of the harness, not student-facing behaviour (provenance:
# 2026-09-16 audit triage — chemdrill1/chemprobe2 flags were all harness
# turns). The student-facing path always uses UI/web-* session keys.
PROBE_SESSION_RE = re.compile(
    r"^(probe|drill|gate|test|smoke|mathdrill|chemdrill|chemprobe|xie_|ui[0-9]|"
    r"gatedrill|inject[A-Z]|m2smoke|y11smoke|verify\d*|zt_)")
YEARQ_RE = re.compile(r"(20\d{2})\s*(?:卷|年)?\s*[·•.]?\s*Q\s*(\d+)\s*(?:\(([a-eivx]+)\))?", re.I)
QTEXT_RE = re.compile(r"Question\s+(One|Two|Three|Four|Five|\d+)(?:\s*\(([a-eivx]+)\))?", re.I)
ENTRYID_RE = re.compile(r"\b(\d{5})-(\d{4})-(Q[A-Za-z0-9]*)(?=[^A-Za-z0-9]|$)")
PDFLINK_RE = re.compile(r"https?://\S+?\.pdf\b", re.I)
URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)
CODE_RE = re.compile(r"(?<!\d)(9\d{4})(?!\d)")

QN = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

EN_CODES = {str(c) for c in (
    list(range(91098, 91108)) + list(range(91472, 91481)) +
    list(range(91924, 91928)))}
GRADE_ASSIGN_RE = re.compile(
    r"(给你|达到|判|评为|拿 ?到|属于|是)\s*(Excellence|Merit|Achieved)",
    re.I)


def qkey(qstr):
    m = re.match(r"Q(\d+)([a-z]*)", qstr, re.I)
    return (int(m.group(1)), m.group(2).lower()) if m else (-1, "")


def load_banks():
    entries = {}  # (code,year,qnum) -> id ; ids set; urls set
    parts = {}    # (code,year,qnum) -> set of subparts
    by_id = {}
    urls = set()
    for fp in glob.glob(os.path.join(REF, "qbank_*.md")):
        code = os.path.basename(fp)[6:-3]
        cur = None
        for line in open(fp, encoding="utf-8").read().split("\n"):
            m = HDR_RE.match(line.strip())
            if m:
                cur = {"code": m.group(1), "year": int(m.group(2)),
                       "q": m.group(3), "id": line.strip()[3:].split()[0]}
                qm = re.match(r"Q(\d+)", cur["q"])
                cur["qnum"] = int(qm.group(1)) if qm else -1
                by_id[cur["id"]] = cur
                key = (cur["code"], cur["year"], cur["qnum"])
                entries[key] = cur["id"]
                qn, qp = qkey(cur["q"])
                parts.setdefault(key, set()).add(qp)
            if line.strip().startswith("## Omitted"):
                break
    lp = os.path.join(REF, "pdf_links.md")
    if os.path.exists(lp):
        for line in open(lp, encoding="utf-8").read().split("\n"):
            m = URL_RE.search(line)
            if m:
                urls.add(m.group(0).rstrip(".,;"))
    return entries, parts, by_id, urls


def turn_ts(ev):
    ts = [e.get("timestamp", 0) for e in ev if e.get("timestamp")]
    return (min(ts), max(ts)) if ts else (0, 0)


def served_in_window(uid, t0, t1):
    hits = []
    for base in (os.path.join("data", "users", uid),
                 os.path.join("data", "partners", "*")):
        for fp in glob.glob(os.path.join(base, "ncea_served.jsonl")):
            try:
                for ln in open(fp, encoding="utf-8").read().splitlines():
                    try:
                        r = json.loads(ln)
                        if t0 - 600 <= r.get("ts", 0) <= t1 + 600:
                            hits.append(r.get("entry_id", ""))
                    except Exception:
                        continue
            except Exception:
                continue
    return hits


def served_ever(uid):
    """All entry ids ever deterministically served to this student."""
    out = set()
    for base in (os.path.join("data", "users", uid),
                 os.path.join("data", "partners", "*")):
        for fp in glob.glob(os.path.join(base, "ncea_served.jsonl")):
            try:
                for ln in open(fp, encoding="utf-8").read().splitlines():
                    try:
                        v = json.loads(ln).get("entry_id", "")
                        if v:
                            out.add(v)
                    except Exception:
                        continue
            except Exception:
                continue
    return out


def audit_reply(entries, parts, by_id, urls, uid, reply, ev, photo, ever):
    flags = []
    tag = "F2p" if photo else "F2"
    codes = set(CODE_RE.findall(reply))

    def exempted(code, year, q, part=""):
        for eid in ever:
            b = by_id.get(eid)
            if not b:
                continue
            if code != "?" and b["code"] != code:
                continue
            if b["year"] != year or b["qnum"] != q:
                continue
            _, ep = qkey(b["q"])
            if not part or not ep or part in ep or ep in part:
                return True
        return False

    for m in YEARQ_RE.finditer(reply):
        year, q, part = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        hits = [c for c in codes if (c, year, q) in entries]
        if not hits:
            cand = list(codes) or ["?"]
            if not any(exempted(c, year, q, part) for c in cand):
                flags.append(f"{tag} unknown-location {cand[0]}-{year}-Q{q}")
        elif part and not any(part in (p or "") for p in parts.get((hits[0], year, q), set())):
            if not exempted(hits[0], year, q, part):
                flags.append(f"{tag} unknown-part {hits[0]}-{year}-Q{q}({part})")
    for m in QTEXT_RE.finditer(reply):
        w, part = m.group(1), (m.group(2) or "").lower()
        q = QN.get(w.lower(), int(w) if w.isdigit() else -1)
        if q < 0:
            continue
        years = set(int(x) for x in re.findall(r"(20\d{2})", reply[:m.start()][-60:])) or \
            set(int(x) for x in re.findall(r"(20\d{2})", reply))
        cand = [c for c in codes for y in years if (c, y, q) in entries]
        if codes and not cand:
            if not any(exempted(c, y, q, part) for c in codes for y in years):
                flags.append(f"{tag} unknown-location Q{q}" + (f"({part})" if part else ""))
    for m in ENTRYID_RE.finditer(reply):
        if m.group(0) not in by_id:
            flags.append(f"{tag} unknown-entry-id {m.group(0)}")
    if codes & EN_CODES and GRADE_ASSIGN_RE.search(reply):
        flags.append("F4 english-grade-assign (subjective grade needs human review)")
    for m in URL_RE.finditer(reply):
        u = m.group(0).rstrip(".,;")
        host = re.sub(r"^https?://", "", u).split("/")[0].lower()
        if u in urls:
            continue
        if host.endswith("nzqa.govt.nz"):
            continue
        if u.lower().endswith(".pdf"):
            flags.append(f"F3 rogue-pdf-link {u[:80]}")
        else:
            flags.append(f"F3b off-domain-link {u[:80]}")
    if flags:
        t0, t1 = turn_ts(ev)
        served = served_in_window(uid, t0, t1)
        has_tool = any(e.get("type") == "tool_call" and "serve_question" in json.dumps(e)
                       for e in ev)
        if not served and not has_tool:
            flags.append("F1 unserved-serving (no serve record this turn)")
    return flags


def _existing_user_ids() -> dict:
    """{user_id: username} for accounts that still exist.

    The 1.7.0 layout keeps one chat_history.db per user and the file outlives
    deleted accounts, so the db alone cannot tell a student from a cleaned-up
    harness run - the user store can.
    """
    p = os.path.join("data", "system", "auth", "users.json")
    out = {}
    try:
        data = json.load(open(p, encoding="utf-8"))
        rows = data if isinstance(data, list) else (data.get("users") or data)
        if isinstance(rows, dict):
            rows = list(rows.values())
        for d in rows:
            if isinstance(d, dict) and d.get("id"):
                out[str(d["id"])] = str(d.get("username") or "")
    except Exception:
        pass
    return out


def db_turns(db_path: str, cutoff: float):
    """(uid_path_ts0, ts1, reply, prev_user_json, serve_tool_seen) per turn.

    Reads the per-user chat_history.db that 1.7.0 uses for every web turn.
    The events table carries the tool calls, so a deterministic serve is
    still distinguishable from an improvised one.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
    except Exception:
        return
    try:
        rows = list(conn.execute(
            "select role, content, created_at from messages order by id"))
        turns = []
        prev_user = ""
        prev_ts = 0.0
        for role, content, ts in rows:
            ts = float(ts or 0)
            if role == "user":
                prev_user = content or ""
                prev_ts = ts
                continue
            if role != "assistant":
                continue
            reply = content or ""
            if not reply.strip():
                continue
            t0 = prev_ts or (ts - 600)
            t1 = ts + 120
            serve_tool = False
            try:
                for (raw,) in conn.execute(
                    "select te.metadata_json from turn_events te "
                    "join turns t on t.id = te.turn_id "
                    "where te.type='tool_call' "
                    "and t.created_at between ? and ?", (t0 - 30, t1)):
                    if raw and "serve_question" in raw:
                        serve_tool = True
                        break
            except Exception:
                pass
            turns.append((t0, t1, reply, prev_user, serve_tool))
        return turns
    finally:
        conn.close()


def main():
    entries, parts, by_id, urls = load_banks()
    print(f"banks: {len(by_id)} entries, {len(urls)} pdf links")
    cutoff = time.time() - HOURS * 3600
    n_turns = n_flag = 0
    all_durs: list = []
    ever_cache: dict[str, set] = {}
    for path in sorted(glob.glob("data/partners/*/users/*/sessions/*.jsonl")):
        uid = path.split("/users/")[1].split("/")[0]
        session_key = os.path.basename(path)[:-6] if path.endswith(".jsonl") else os.path.basename(path)
        if PROBE_SESSION_RE.match(session_key):
            continue  # harness session: audit skips it entirely (no flags, no latency)
        if uid not in ever_cache:
            ever_cache[uid] = served_ever(uid)
        try:
            raws = open(path, encoding="utf-8").read().strip().split("\n")
        except Exception:
            continue
        objs = []
        for ln in raws:
            try:
                objs.append(json.loads(ln))
            except Exception:
                continue
        for i, o in enumerate(objs):
            if o.get("role") != "assistant":
                continue
            ev = o.get("events", [])
            t0, t1 = turn_ts(ev)
            if t1 > t0:
                all_durs.append(t1 - t0)
            if t0 and t0 < cutoff:
                continue
            reply = o.get("content", "") or ""
            if not re.search(r"20\d{2}|Question|Q\d|http|AS\s*9", reply):
                continue
            photo = False
            if i > 0 and objs[i - 1].get("role") == "user":
                u = json.dumps(objs[i - 1])
                photo = ("image" in u or ".png" in u or ".jpg" in u
                         or "attach" in u)
            n_turns += 1
            ever = ever_cache[uid]
            for f in audit_reply(entries, parts, by_id, urls, uid, reply, ev, photo, ever):
                n_flag += 1
                print(f"FLAG {uid} :: {f} :: {(reply[:70]).replace(chr(10), ' ')}")
    print(f"done: {n_turns} candidate turns, {n_flag} flags")

    # ---- 1.7.0 source: per-user chat_history.db (no partner session jsonl)
    existing = _existing_user_ids()
    harness = re.compile(r"example\.com|e2e|probe|drill|test", re.I)
    for path in sorted(glob.glob(os.path.join("data", "users", "*", "user",
                                              "chat_history.db"))):
        uid = path.split("/users/")[1].split("/")[0]
        username = existing.get(uid)
        if username is None:
            continue  # deleted account (harness cleanup): its db outlived it
        if harness.search(username):
            continue
        if uid not in ever_cache:
            ever_cache[uid] = served_ever(uid)
        turns = db_turns(path, cutoff) or []
        for t0, t1, reply, prev_user, serve_tool in turns:
            if t1 and t1 < cutoff:
                continue
            if not re.search(r"20\d{2}|Question|Q\d|http|AS\s*9", reply):
                continue
            photo = bool(re.search(r"image|\.png|\.jpg|attach", prev_user, re.I))
            n_turns += 1
            if t1 > t0:
                all_durs.append(min(t1 - t0, 900))
            ev = [{"type": "content", "timestamp": t0},
                  {"type": "content", "timestamp": t1}]
            if serve_tool:
                ev.append({"type": "tool_call", "tool": "serve_question"})
            for f in audit_reply(entries, parts, by_id, urls, uid, reply,
                                 ev, photo, ever_cache[uid]):
                n_flag += 1
                print(f"FLAG {uid} :: {f} :: {(reply[:70]).replace(chr(10), ' ')}")

    print(f"final: {n_turns} candidate turns (incl. chat dbs), {n_flag} flags")
    durs = sorted(all_durs)
    if durs:
        n = len(durs)
        print(f"latency: n={n} p50={durs[n // 2]:.0f}s "
              f"p90={durs[int(n * 0.9)]:.0f}s max={durs[-1]:.0f}s")


if __name__ == "__main__":
    main()
