"""Citation audit must flag fabricated paper citations (no LLM).

Covers scripts/nightly_review.py audit_text (deeptutor-agnostic, stdlib only):
- FAIL fixture = real production reply that cited substances absent from
  the cited paper's KB (CBr4 case, caught by hand, must be caught by code).
- PASS fixture = reply citing substances present in the KB corpus.

Run: .venv\\Scripts\\python.exe -m pytest tests/test_citation_audit.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import nightly_review as nr


def test_flags_fabricated_substances():
    # The audit decides whether a cited substance appears in the cited paper,
    # so it needs the real knowledge base. That lives in a deployment, never in
    # this repository, and a clone without one cannot answer the question.
    if not os.path.isdir(os.path.join("data", "knowledge_bases")):
        pytest.skip("citation audit needs a knowledge base under data/knowledge_bases")
    nr.reset_state()
    nr.audit_text("test", open("tests/fixtures/cbr4_reply.txt", encoding="utf-8").read())
    assert len(nr.issues) == 1, nr.issues
    assert "carbon tetrabromide" in nr.issues[0]


def test_passes_grounded_citation():
    nr.reset_state()
    nr.audit_text("test", open("tests/fixtures/o2_graphite_reply.txt", encoding="utf-8").read())
    assert nr.issues == [], nr.issues


def test_ignores_text_without_location():
    nr.reset_state()
    nr.audit_text("test", "Oxygen is molecular and graphite is covalent, let us discuss.")
    assert nr.issues == []
