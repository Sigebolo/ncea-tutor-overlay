"""Regression tests for the nightly numbers-audit rule tweaks (2026-09-16).

Two false-positive classes observed on the 9/16 audit run, both traced to
harness turns (chemdrill1 / chemprobe2), not student traffic:

  1. ENTRYID_RE flagged ids with markdown tails ('91267-2025-Q1a**' — the
     reply bolded the id). The old regex grabbed the tail chars into the
     id, which then 'did not exist' in by_id. Fix: bank id grammar is
     Q + [A-Za-z0-9]*; any other char terminates the match.
  2. Probe/harness sessions (session_key chemdrill1, chemprobe2, gate*,
     ...) audited as if they were student traffic. Fix: sessions whose
     key matches PROBE_SESSION_RE are skipped entirely.

Run: .venv\\Scripts\\python.exe -m pytest tests/test_numbers_audit_rules.py -q
"""
from __future__ import annotations

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "nightly_numbers.py")

spec = importlib.util.spec_from_file_location("nightly_numbers", SCRIPT)
nr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nr)


# --- 1. entry-id regex tolerates markdown tails -----------------------------

def test_plain_id_matches():
    m = nr.ENTRYID_RE.search("Served question: 91390-2025-Q3c today")
    assert m and m.group(0) == "91390-2025-Q3c"


def test_bold_tail_id_matches_clean():
    """The 9/16 false positive: '**91267-2025-Q1a**' — tail must not be eaten."""
    m = nr.ENTRYID_RE.search("91267-2025-Q1a**")
    assert m and m.group(0) == "91267-2025-Q1a"


def test_paren_tail_id_matches_clean():
    """'91579-2025-Q1e（below-target）' — full-width paren terminates the id."""
    m = nr.ENTRYID_RE.search("91579-2025-Q1e（below-target）：正确答案是")
    assert m and m.group(0) == "91579-2025-Q1e"


def test_nested_ids_still_parse():
    for eid in ("91392-2022-Q1aiii", "91392-2025-Q3bi", "91579-2021-Q1bi"):
        m = nr.ENTRYID_RE.search(eid)
        assert m and m.group(0) == eid, eid


def test_bogus_id_still_caught():
    """A fabricated id (grammar-valid but not in banks) must still match —
    the audit then flags it as unknown-entry-id via by_id lookup."""
    m = nr.ENTRYID_RE.search("91579-2025-Q9z**")
    assert m and m.group(0) == "91579-2025-Q9z"


# --- 2. probe/harness session exemption --------------------------------------

def test_probe_session_keys_are_exempt():
    for key in ("chemdrill1", "chemprobe2", "gatedrill1", "mathdrill1",
                "drill1", "gate1", "injectB", "probe_m2", "smoke1",
                "ui91579", "test1", "verify10", "zt_open"):
        assert nr.PROBE_SESSION_RE.match(key), key


def test_student_session_keys_are_not_exempt():
    for key in ("web-uhgu2w80", "web-abc123", "chat-2026-09-16"):
        assert not nr.PROBE_SESSION_RE.match(key), key
