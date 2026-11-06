"""Nightly tutor monitoring: failures, loop storms, empty replies, bank deltas,
citation audit.

Read-only. Run on AU: .venv/bin/python scripts/nightly_review.py [--hours N]
Covers: user sqlite scopes (home turns) + partner jsonl sessions.

Storm definition: a turn with >=6 identical tool calls (same tool+args),
or >=12 tool calls total. Empty reply: assistant message with <20 chars
and no attachments/artifacts. Citation audit: Location claims checked
against the cited standard's KB corpus (no LLM involved).
"""
import glob
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter

try:
    HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
except ValueError:
    HOURS = 24.0  # imported by tests; CLI default applies
CUTOFF = time.time() - HOURS * 3600
STORM_SAME = 6
STORM_TOTAL = 12

issues: list[str] = []

SUBSTS = ["sodium chloride", "barium hydroxide", "copper sulfate", "cuso4",
          "carbon tetrabromide", "cbr4", "oxygen", "graphite", "diamond",
          "ethanol", "zinc", "nacl", "cbr3cl", "propanal", "propanoic",
          "gold", "ammonia", "methanol"]
_kb_text_cache: dict[str, str] = {}


def flag(msg):
    issues.append(msg)
    print("FLAG:", msg)


def reset_state():
    issues.clear()
    _kb_text_cache.clear()


def _node_texts(nodes) -> list[str]:
    out = []
    items = nodes.values() if isinstance(nodes, dict) else (nodes or [])
    for n in items:
        if not isinstance(n, dict):
            continue
        inner = n.get("__data__") if isinstance(n.get("__data__"), dict) else n
        t = inner.get("text", "") if isinstance(inner, dict) else ""
        if t:
            out.append(str(t))
    return out


def kb_corpus(kb_name: str) -> str:
    if kb_name not in _kb_text_cache:
        txt = ""
        try:
            base = os.path.join("data", "knowledge_bases", kb_name, "version-1")
            ds = json.load(open(os.path.join(base, "docstore.json"), encoding="utf-8"))
            if isinstance(ds, dict):
                for key in ("docstore/data", "docstore/docs", "docs"):
                    blob = ds.get(key)
                    if blob is None and "/" in key:
                        blob = ds.get("docstore", {}).get(key.split("/", 1)[1])
                    texts = _node_texts(blob)
                    if texts:
                        txt = " ".join(texts)
                        break
            txt = txt.lower()
        except Exception:
            txt = ""
        _kb_text_cache[kb_name] = txt
    return _kb_text_cache[kb_name]


def kb_for_code(code: str) -> str:
    try:
        cat = json.load(open("data/knowledge_bases/kb_config.json", encoding="utf-8"))
        for name in cat.get("knowledge_bases", {}):
            if f"AS{code}" in name and "REF_" not in name:
                return name
    except Exception:
        pass
    return ""


def audit_text(who: str, text: str) -> None:
    for m in re.finditer(r"Location:\*{0,2}\s*(\d{4})[^Q]{0,80}?Question\s+(\w+)", text):
        year, q = m.group(1), m.group(2)
        code_m = re.search(r"(?:AS)?(91\d{3}|915\d{2}|919\d{2})", text[max(0, m.start() - 300):m.start()])
        code = code_m.group(1) if code_m else ""
        kb = kb_for_code(code) if code else ""
        if not kb:
            continue
        corpus = kb_corpus(kb)
        if not corpus:
            flag(f"{who}: cited {year} Q{q} ({code}) UNVERIFIABLE (KB text unreadable)")
            continue
        low_reply = text.lower()
        missing = [s for s in SUBSTS if s in low_reply and s not in corpus]
        if missing:
            flag(f"{who}: cited {year} Q{q} ({code}) mentions absent from KB: {missing}")


def review_sqlite() -> None:
    for dbp in sorted(glob.glob("data/users/*/user/chat_history.db") + ["data/user/chat_history.db"]):
        who = "admin" if dbp.startswith("data/user/") else dbp.split("/")[1][:12]
        try:
            db = sqlite3.connect(dbp)
        except Exception as e:
            flag(f"{who}: db unreadable {e}")
            continue
        try:
            bad = list(db.execute(
                "select id, session_id, status, error from turns "
                "where created_at >= ? and status not in ('completed')", (CUTOFF,)))
        except Exception:
            bad = []
        for tid, sid, st, err in bad:
            if st == "waiting_input":
                print(f"[{who}] turn {tid[:24]} awaiting user input (not a failure)")
                continue
            flag(f"{who}: turn {tid[:24]} status={st} err={(err or '')[:120]}")
        try:
            rows = list(db.execute(
                "select turn_id, content, metadata_json from turn_events "
                "where timestamp >= ? and (type='tool_call' or content in "
                "('imagegen','rag','read_skill','code_execution','question_bank'))",
                (CUTOFF,)))
        except Exception:
            rows = []
        by_turn: dict[str, list] = {}
        for tid, content, meta in rows:
            by_turn.setdefault(tid, []).append((content, meta or ""))
        for tid, calls in by_turn.items():
            if len(calls) >= STORM_TOTAL:
                flag(f"{who}: turn {tid[:24]} made {len(calls)} tool calls (storm?)")
            sigs = Counter((c, m[:80]) for c, m in calls)
            for sig, n in sigs.most_common(3):
                if n >= STORM_SAME:
                    flag(f"{who}: turn {tid[:24]} repeated {sig[0]} x{n} (loop!)")
        try:
            empt = list(db.execute(
                "select session_id, substr(content,1,60) from messages "
                "where role='assistant' and length(trim(content)) < 20"))
            for sid, c in empt[-5:]:
                flag(f"{who}: near-empty reply in {sid[:24]}: {c!r}")
        except Exception:
            pass
        try:
            tot = db.execute("select count(*) from notebook_entries").fetchone()[0]
            wr = db.execute("select count(*) from notebook_entries where is_correct=0").fetchone()[0]
            print(f"[{who}] bank total={tot} wrong={wr}")
        except Exception:
            pass
        try:
            rows = db.execute("select content from messages where role='assistant'").fetchall()
        except Exception:
            continue
        for (content,) in rows[-30:]:
            if content and "Location:" in content:
                audit_text(who, content)


def review_partners() -> None:
    for path in sorted(glob.glob("data/partners/*/sessions/*.jsonl")):
        if os.path.getmtime(path) < CUTOFF:
            continue
        pid = path.split("/")[2]
        try:
            lines = open(path, encoding="utf-8").read().strip().split("\n")
        except Exception as e:
            flag(f"{pid}: session unreadable {e}")
            continue
        tools = Counter()
        for raw in lines:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            ts = ev.get("timestamp")
            try:
                t = float(ts) if isinstance(ts, (int, float)) else 0
            except Exception:
                t = 0
            if t and t < CUTOFF:
                continue
            c = ev.get("content") or ""
            if isinstance(c, str) and ("tool_call" in raw[:200] or "imagegen" in c[:40]):
                tools[c[:60]] += 1
            if isinstance(c, str) and "Location:" in c:
                audit_text(pid, c)
        for sig, n in tools.most_common(5):
            if n >= STORM_SAME:
                flag(f"{pid}: repeated call x{n}: {sig[:80]}")
        print(f"[{pid}] session lines={len(lines)} recent-tool-kinds={len(tools)}")


def main() -> int:
    print(f"=== tutor monitor: last {HOURS}h ===")
    print("=== citation audit included (KB corpus, no LLM) ===")
    review_sqlite()
    review_partners()
    print(f"\n=== done: {len(issues)} flags ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
