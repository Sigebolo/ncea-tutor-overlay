"""Deterministic wrong-book read (site patch helper).

Mirrors the serve block: when the student asks for their mistakes, the
server reads the per-student ncea_mistakes.jsonl and injects the
Wrong-Only list. The model never needs to remember to call log_mistake
(action=list) - and E2E proves it won't.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

REVIEW_RE = re.compile(r"错题|订正|wrong|mistakes?|没改|未订正|订完")


def wrong_only(entries) -> list:
    """Fold mistake/resolve records (pure, unit-tested)."""
    state = {}
    for r in entries:
        if not isinstance(r, dict):
            continue
        eid = r.get("entry_id", "")
        if not eid:
            continue
        if r.get("type") == "resolve":
            state.pop(eid, None)
        else:
            state[eid] = r
    return [(eid, r.get("result", "?"), r.get("gap", "")) for eid, r in state.items()]


async def build_review_block(text: str, uid: str) -> str:
    """Return '' or a WRONG BOOK block for this student's own file."""
    if not uid or not REVIEW_RE.search(text or ""):
        return ""
    try:
        from pathlib import Path
        from deeptutor.multi_user.paths import scope_for_user
        p = Path(scope_for_user(uid, is_admin=False).root) / "ncea_mistakes.jsonl"
        recs = []
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines():
                try:
                    recs.append(json.loads(ln))
                except Exception:
                    continue
        items = wrong_only(recs)
        if not items:
            return ("[WRONG BOOK - deterministic]\nWrong Only: 0 - clean. "
                    "Say so in one line, then ask what to drill next.")
        lines = ["[WRONG BOOK - deterministic, verified ledger entries]",
                 "Relay this exact list. Re-serve the SAME Location + PDF "
                 "for correction (serve_question or the served ledger); "
                 "never substitute a 'similar' question.",
                 f"Wrong Only: {len(items)}"]
        for eid, result, gap in items:
            lines.append(f"- {eid} ({result}): {gap}")
        logger.warning("ncea review block injected: uid=%s n=%d", uid, len(items))
        return "\n".join(lines)
    except Exception:
        logger.warning("ncea review block failed", exc_info=True)
        return ""
