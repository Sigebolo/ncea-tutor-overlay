"""Tests for the JEV semantic gate prototype (scripts/ncea_jev_gate.py).

Offline only: the pure policy (decide / build_state / build_questions) and
the CLI self-test. The live API path is exercised by --smoke, not here.

Run: .venv\\Scripts\\python.exe -m pytest tests/test_jev_gate.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "ncea_jev_gate.py")

spec = importlib.util.spec_from_file_location("ncea_jev_gate", SCRIPT)
jg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jg)


@pytest.fixture(autouse=True)
def _no_ledger_writes(monkeypatch, tmp_path):
    """Keep every default path inside pytest's tmp dir.

    Patching only _write_event was not enough: _root_for() mkdir's the
    student's directory before the write is attempted, so a real
    data/users/u_test/ was still created by any test that reached
    _evaluate. Tests that need a specific root override this fixture.
    """
    monkeypatch.setattr(jg, "_write_event", lambda root, rec: None)
    monkeypatch.setattr(jg, "_root_for", lambda uid: str(tmp_path))


def noul(v):
    return {"type": "noul", "noul": v}


def choice(c, conf=0.9):
    return {"type": "choice", "choice": c, "confidence": conf}


# --- Rule 1: definite answer vs the verified key ---------------------------

def test_definite_answer_with_no_key_guards():
    v = jg.decide({"states_definite_answer": noul(0.95)},
                  key_present=False, serve_present=False)
    assert v["verdict"] == "guard"
    assert v["reasons"] == ["definite-answer-no-key"]


def test_definite_answer_denied_by_key_guards():
    v = jg.decide({"states_definite_answer": noul(0.9),
                   "answer_backed_by_key": noul(0.1)},
                  key_present=True, serve_present=False)
    assert v["verdict"] == "guard"
    assert v["reasons"] == ["answer-not-backed-by-key"]


def test_definite_answer_backed_by_key_passes():
    v = jg.decide({"states_definite_answer": noul(0.9),
                   "answer_backed_by_key": noul(0.95)},
                  key_present=True, serve_present=False)
    assert v == {"verdict": "pass", "reasons": []}


def test_soft_definite_answer_does_not_fire():
    """Noul 0.6 is the uncertain middle - code must not act on it."""
    v = jg.decide({"states_definite_answer": noul(0.6),
                   "answer_backed_by_key": noul(0.1)},
                  key_present=True, serve_present=False)
    assert v["verdict"] == "pass"


# --- Rule 2: composed / mismatched question --------------------------------

def test_composed_question_with_nothing_served_guards():
    v = jg.decide({"presents_question_to_attempt": noul(0.92)},
                  key_present=False, serve_present=False)
    assert v["reasons"] == ["question-not-served"]


def test_served_question_mismatch_guards():
    v = jg.decide({"presents_question_to_attempt": noul(0.9),
                   "question_matches_verified": noul(0.1)},
                  key_present=False, serve_present=True)
    assert v["reasons"] == ["question-mismatch"]


def test_concept_teaching_only_passes():
    """Teaching a concept mentions formulas, not an attemptable question."""
    v = jg.decide({"presents_question_to_attempt": noul(0.15)},
                  key_present=False, serve_present=False)
    assert v["verdict"] == "pass"


# --- Rule 2's excuses: refusal, and membership in the bank -----------------

def test_a_decline_alone_does_not_excuse_the_mismatch():
    """A decline reports intent, not evidence. The tutor says it will not
    supply the asked-for question - that says nothing about whether what it
    offers instead is real, and the fabricated production reply declined at
    0.91. Without in-bank membership the mismatch stands."""
    v = jg.decide({"presents_question_to_attempt": noul(0.85),
                   "question_matches_verified": noul(0.04),
                   "declines_and_substitutes": noul(0.95)},
                  key_present=False, serve_present=True)
    assert v["reasons"] == ["question-mismatch"]


def test_decline_plus_a_real_substitute_passes():
    """The honest shape the excuse exists for: declined, and the entry it
    offers really is in the bank (the live 91392-exm-2022-Q1c case)."""
    v = jg.decide({"presents_question_to_attempt": noul(0.85),
                   "question_matches_verified": noul(0.04),
                   "declines_and_substitutes": noul(0.95)},
                  key_present=False, serve_present=True,
                  cited_in_bank=True)
    assert v == {"verdict": "pass", "reasons": []}


def test_substitute_that_resolves_to_a_bank_entry_passes():
    v = jg.decide({"presents_question_to_attempt": noul(0.85),
                   "question_matches_verified": noul(0.04)},
                  key_present=False, serve_present=True,
                  cited_in_bank=True)
    assert v == {"verdict": "pass", "reasons": []}


def test_in_bank_excuses_the_not_served_branch_too():
    """A photo-grading turn restates the question with nothing served this
    turn; if it quotes a real bank entry that is not a composed question."""
    v = jg.decide({"presents_question_to_attempt": noul(0.9)},
                  key_present=True, serve_present=False,
                  cited_in_bank=True)
    assert v["verdict"] == "pass"


def test_soft_decline_does_not_excuse_a_mismatch():
    """Noul only counts at the ends - 0.6 is not a refusal."""
    v = jg.decide({"presents_question_to_attempt": noul(0.9),
                   "question_matches_verified": noul(0.1),
                   "declines_and_substitutes": noul(0.6)},
                  key_present=False, serve_present=True)
    assert v["reasons"] == ["question-mismatch"]


def test_a_real_substitute_also_excuses_the_location_claim():
    """Live case: Rule 2 stopped firing but Rule 3 re-guarded the same
    honest reply, because offering a different verified question is
    SUPPOSED to disagree with the served row."""
    v = jg.decide({"presents_question_to_attempt": noul(0.93),
                   "question_matches_verified": noul(0.03),
                   "declines_and_substitutes": noul(0.95),
                   "location_claim": choice("contradicts", 0.95)},
                  key_present=False, serve_present=True,
                  cited_in_bank=True)
    assert v == {"verdict": "pass", "reasons": []}


def test_a_decline_alone_does_not_excuse_the_location_claim_either():
    """Same reply without membership: Rule 3 stands on its own."""
    v = jg.decide({"declines_and_substitutes": noul(0.91),
                   "location_claim": choice("contradicts", 0.81)},
                  key_present=False, serve_present=True)
    assert v["reasons"] == ["location-contradicts-verified"]


def test_in_bank_substitute_excuses_the_location_claim():
    v = jg.decide({"location_claim": choice("contradicts", 0.9)},
                  key_present=False, serve_present=True, cited_in_bank=True)
    assert v["verdict"] == "pass"


def test_wrong_location_with_no_substitute_still_guards():
    """The class Rule 3 exists for: relabels the served question without
    offering anything verifiable."""
    v = jg.decide({"location_claim": choice("contradicts", 0.99),
                   "declines_and_substitutes": noul(0.13)},
                  key_present=False, serve_present=True)
    assert v["reasons"] == ["location-contradicts-verified"]


def test_excuse_does_not_weaken_rule_1():
    """A fabricated answer value is still guarded on a turn that also
    happened to cite a real question."""
    v = jg.decide({"states_definite_answer": noul(0.96),
                   "answer_backed_by_key": noul(0.14),
                   "presents_question_to_attempt": noul(0.9),
                   "question_matches_verified": noul(0.05)},
                  key_present=True, serve_present=True,
                  cited_in_bank=True)
    assert v["reasons"] == ["answer-not-backed-by-key"]


# --- Rule 3: location claim ------------------------------------------------

def test_location_contradiction_with_confidence_guards():
    v = jg.decide({"location_claim": choice("contradicts", 0.8)},
                  key_present=False, serve_present=True)
    assert v["reasons"] == ["location-contradicts-verified"]


def test_location_contradiction_low_confidence_passes():
    v = jg.decide({"location_claim": choice("contradicts", 0.4)},
                  key_present=False, serve_present=True)
    assert v["verdict"] == "pass"


def test_location_not_established_passes():
    v = jg.decide({"location_claim": choice("not_established")},
                  key_present=False, serve_present=True)
    assert v["verdict"] == "pass"


# --- fail-open shapes ------------------------------------------------------

def test_empty_answers_pass():
    assert jg.decide({}, False, False)["verdict"] == "pass"


def test_malformed_answers_pass():
    v = jg.decide({"states_definite_answer": "oops",
                   "location_claim": None}, False, False)
    assert v["verdict"] == "pass"


def test_multiple_reasons_accumulate():
    v = jg.decide({"states_definite_answer": noul(0.9),
                   "presents_question_to_attempt": noul(0.9)},
                  key_present=False, serve_present=False)
    assert set(v["reasons"]) == {"definite-answer-no-key", "question-not-served"}


# --- state / question contract --------------------------------------------

def test_state_keeps_evidence_slots_separate():
    s = jg.build_state(student_message="学生说A", tutor_reply="回复B",
                       served_block="S", answer_key_block="K")
    assert s["student_message"] == "学生说A"
    assert s["verified_blocks"]["served"] == "S"
    assert s["verified_blocks"]["answer_key"] == "K"


def test_state_does_not_ship_student_history_by_default():
    """No question reads known_entry_ids, and the class it would cover is
    already handled deterministically by check_output - so the per-student
    behavioural history must not leave the deployment for nothing."""
    s = jg.build_state(known_entry_ids=["91579-2024-Q1d"])
    assert "known_entry_ids" not in s
    s2 = jg.build_state(known_entry_ids=["91579-2024-Q1d"],
                        include_entry_ids=True)
    assert s2["known_entry_ids"] == ["91579-2024-Q1d"]


def test_no_question_reads_entry_ids():
    """Ties the omission above to reality: if a question ever starts reading
    the ids, this fails and the default has to be revisited."""
    blob = "\n".join(str(v) for v in jg.build_questions().values())
    assert "entry_id" not in blob


def test_questions_cover_the_three_gates():
    q = jg.build_questions()
    assert q["states_definite_answer"]["type"] == "noul"
    assert q["answer_backed_by_key"]["type"] == "noul"
    assert q["presents_question_to_attempt"]["type"] == "noul"
    assert q["question_matches_verified"]["type"] == "noul"
    assert q["declines_and_substitutes"]["type"] == "noul"
    assert q["location_claim"]["type"] == "choice"
    assert set(q["location_claim"]["criteria"]) == {
        "supports", "contradicts", "not_established"}


def test_cli_selftest_passes():
    assert jg.main(["--selftest"]) == 0


def test_no_key_fails_open():
    """maybe_jev_guard must keep the original reply when the key is absent."""
    import asyncio
    old = os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        out = asyncio.run(jg.maybe_jev_guard(
            final="the answer is 3.65", uid="u_test"))
        assert out == ""
    finally:
        if old is not None:
            os.environ["TYPESAFE_API_KEY"] = old


# --- runtime knobs ---------------------------------------------------------

def test_gate_mode_defaults_to_shadow():
    old = os.environ.pop(jg.MODE_ENV, None)
    try:
        assert jg.gate_mode() == "shadow"
    finally:
        if old is not None:
            os.environ[jg.MODE_ENV] = old


def test_gate_mode_reads_env_and_rejects_junk():
    old = os.environ.get(jg.MODE_ENV)
    try:
        os.environ[jg.MODE_ENV] = "ENFORCE"
        assert jg.gate_mode() == "enforce"
        os.environ[jg.MODE_ENV] = "yolo"
        assert jg.gate_mode() == "shadow"   # unknown value never escalates
    finally:
        if old is None:
            os.environ.pop(jg.MODE_ENV, None)
        else:
            os.environ[jg.MODE_ENV] = old


def test_mode_off_never_calls():
    import asyncio
    out = asyncio.run(jg.maybe_jev_guard(
        final="the answer is 3.65", uid="u_test", mode="off"))
    assert out == ""


# --- shadow vs enforce dispatch --------------------------------------------

_GUARDY = ("这题的最终答案是 3.65，来自 2024 年 Q1(d)。"
           "标准答案就是这个，请核对你的作业。")


def _guard_answers():
    return {"answers": {"states_definite_answer": {"noul": 0.97},
                        "answer_backed_by_key": {"noul": 0.02}},
            "model": "jev-1.13.0"}


def _no_evidence(uid, now=None, turn_id=""):
    return {"served_block": "", "answer_key_block": "",
            "known_entry_ids": [], "last_served": "", "last_graded": "",
            "has_verified": False, "turn": turn_id, "evidence": {}}


def test_enforce_rewrites_the_reply_on_a_guard(monkeypatch):
    import asyncio

    calls = []

    def fake(*a, **k):          # call_systemone is SYNC (blocking urllib)
        calls.append(k)
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    out = asyncio.run(jg.maybe_jev_guard(
        final=_GUARDY, uid="u_test", mode="enforce"))
    assert out == jg.REPLACEMENT
    assert jg.GUARDED_MARKER in out
    assert len(calls) == 1


def test_enforce_passes_a_clean_reply_through(monkeypatch):
    import asyncio

    def fake(*a, **k):
        return {"answers": {"states_definite_answer": {"noul": 0.1}},
                "model": "jev-1.13.0"}

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    out = asyncio.run(jg.maybe_jev_guard(
        final=_GUARDY, uid="u_test", mode="enforce"))
    assert out == ""


def test_shadow_never_rewrites_even_on_a_guard(monkeypatch):
    """The whole point of the shadow phase: measure, do not act."""
    import asyncio
    seen = []

    def fake(*a, **k):
        seen.append(1)
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def go():
        out = await jg.maybe_jev_guard(final=_GUARDY, uid="u_test")
        await asyncio.sleep(0.05)   # let the off-turn task finish
        return out

    assert asyncio.run(go()) == ""      # did not touch the reply
    assert seen == [1]                  # but did decide + audit


def test_shadow_does_not_make_the_student_wait(monkeypatch):
    """In shadow the verdict cannot change the reply, so blocking on it is
    pure cost - the call must run off the turn."""
    import asyncio
    import time as _time

    def slow(*a, **k):          # a slow BLOCKING call, like urllib timing out
        _time.sleep(5)
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", slow)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def go():
        t0 = _time.time()
        out = await jg.maybe_jev_guard(final=_GUARDY, uid="u_test")
        return out, _time.time() - t0

    out, elapsed = asyncio.run(go())
    assert out == ""
    assert elapsed < 1.0, f"shadow blocked the turn for {elapsed:.2f}s"


def test_unknown_mode_does_not_escalate_to_enforce(monkeypatch):
    """A typo'd mode must fall back to the env/default, never to the branch
    that rewrites live replies. Unrecognised != most aggressive."""
    import asyncio
    called = []

    def fake(*a, **k):
        called.append(1)
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.delenv(jg.MODE_ENV, raising=False)
    out = asyncio.run(jg.maybe_jev_guard(
        final=_GUARDY, uid="u_test", mode="yolo"))
    assert out == ""


def test_mode_is_compared_case_insensitively(monkeypatch):
    import asyncio

    def fake(*a, **k):
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    out = asyncio.run(jg.maybe_jev_guard(
        final=_GUARDY, uid="u_test", mode="ENFORCE"))
    assert out == jg.REPLACEMENT


def test_norm_mode_rejects_junk():
    assert jg.norm_mode(" off ") == "off"
    assert jg.norm_mode("Shadow") == "shadow"
    assert jg.norm_mode("yolo") is None
    assert jg.norm_mode(None) is None


# --- settings file (the PM2-safe way to supply the key) --------------------

@pytest.fixture
def _clean_cfg():
    """Reset the config cache around a test - it is module-level state."""
    jg.gate_config(force=True)
    yield
    jg.gate_config(force=True)


def test_api_key_comes_from_the_settings_file(tmp_path, monkeypatch, _clean_cfg):
    cfg = tmp_path / "jev_gate.json"
    cfg.write_text('{"api_key": " file-key "}', encoding="utf-8")
    monkeypatch.delenv(jg.KEY_ENV, raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    os.environ.pop("TYPESAFE_API_KEY", None)
    assert jg.gate_api_key(str(cfg)) == "file-key"


def test_environment_wins_over_the_settings_file(tmp_path, monkeypatch,
                                                 _clean_cfg):
    cfg = tmp_path / "jev_gate.json"
    cfg.write_text('{"api_key": "file-key"}', encoding="utf-8")
    monkeypatch.setenv(jg.KEY_ENV, "env-key")
    assert jg.gate_api_key(str(cfg)) == "env-key"


def test_missing_or_junk_settings_file_is_not_fatal(tmp_path, monkeypatch,
                                                    _clean_cfg):
    monkeypatch.delenv(jg.KEY_ENV, raising=False)
    assert jg.gate_api_key(str(tmp_path / "nope.json")) == ""
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    assert jg.gate_api_key(str(bad)) == ""
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    assert jg.gate_api_key(str(arr)) == ""


def test_mode_can_come_from_the_settings_file(tmp_path, monkeypatch,
                                              _clean_cfg):
    cfg = tmp_path / "jev_gate.json"
    cfg.write_text('{"mode": "enforce"}', encoding="utf-8")
    monkeypatch.delenv(jg.MODE_ENV, raising=False)
    assert jg.gate_mode(path=str(cfg)) == "enforce"


def test_junk_mode_in_the_settings_file_does_not_escalate(tmp_path,
                                                          monkeypatch,
                                                          _clean_cfg):
    cfg = tmp_path / "jev_gate.json"
    cfg.write_text('{"mode": "yolo"}', encoding="utf-8")
    monkeypatch.delenv(jg.MODE_ENV, raising=False)
    assert jg.gate_mode(path=str(cfg)) == "shadow"


def test_config_read_is_cached_between_calls(tmp_path, monkeypatch,
                                             _clean_cfg):
    """The no-key fast path runs on the event loop - it must not read disk
    on every single turn."""
    cfg = tmp_path / "jev_gate.json"
    cfg.write_text('{"api_key": "one"}', encoding="utf-8")
    assert jg.gate_api_key(str(cfg)) == "one"
    cfg.write_text('{"api_key": "two"}', encoding="utf-8")
    assert jg.gate_api_key(str(cfg)) == "one"        # cached (TTL)
    assert jg.gate_config(str(cfg), force=True)["api_key"] == "two"


def test_gate_no_ops_when_neither_env_nor_file_has_a_key(monkeypatch,
                                                         _clean_cfg):
    import asyncio
    monkeypatch.delenv(jg.KEY_ENV, raising=False)
    monkeypatch.setattr(jg, "gate_config", lambda *a, **k: {})
    out = asyncio.run(jg.maybe_jev_guard(
        final="this answer is 3.65", uid="u_test", mode="enforce"))
    assert out == ""


def test_all_blocking_work_runs_off_the_event_loop(monkeypatch):
    """Evidence gathering reads a bank file, the call is urllib and the
    audit appends - all three are blocking. The whole evaluation must run
    in a worker thread, or a slow one stalls every concurrent turn."""
    import asyncio
    import threading
    seen = {}

    def fake(*a, **k):
        seen["thread"] = threading.current_thread().name
        return {"verdict": "pass", "reasons": []}

    monkeypatch.setattr(jg, "_evaluate_sync", fake)
    asyncio.run(jg._evaluate(None, "x", "shadow", "u", "", "", [], 1.0))
    assert seen["thread"] != "MainThread"


def test_evaluation_runs_on_a_dedicated_pool(monkeypatch):
    """Must NOT be asyncio.to_thread: that shares the process-wide default
    executor with unrelated blocking work, so a burst of slow JEV calls would
    starve file IO and sibling helpers. Free tiers are slow and rate-limited -
    i.e. exactly when that burst shows up."""
    src = open(os.path.join(HERE, "..", "scripts", "ncea_jev_gate.py"),
               encoding="utf-8").read()
    # Strip comment lines: the rationale comments necessarily NAME the thing
    # we are banning, and only actual calls matter here.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "to_thread(" not in code
    assert "run_in_executor(" in code
    assert jg._get_executor() is jg._get_executor()      # one pool, cached


def test_gate_max_workers_is_bounded():
    old = os.environ.get(jg.MAX_WORKERS_ENV)
    try:
        os.environ[jg.MAX_WORKERS_ENV] = "3"
        assert jg.gate_max_workers() == 3
        os.environ[jg.MAX_WORKERS_ENV] = "999"
        assert jg.gate_max_workers() == 32
        os.environ[jg.MAX_WORKERS_ENV] = "0"
        assert jg.gate_max_workers() == 1
        os.environ[jg.MAX_WORKERS_ENV] = "junk"
        assert jg.gate_max_workers() == jg.DEFAULT_MAX_WORKERS
    finally:
        if old is None:
            os.environ.pop(jg.MAX_WORKERS_ENV, None)
        else:
            os.environ[jg.MAX_WORKERS_ENV] = old


def test_burst_sheds_instead_of_queueing(monkeypatch):
    """Under saturation the gate must degrade to fail-open (today's
    behaviour), not make students wait behind a third-party call."""
    import asyncio
    rows = []

    def boom(*a, **k):
        raise AssertionError("must not reach the vendor while saturated")

    monkeypatch.setattr(jg, "_evaluate_sync", boom)
    monkeypatch.setattr(jg, "gate_max_workers", lambda default=None: 1)
    monkeypatch.setattr(jg, "_write_event", lambda r, rec: rows.append(rec))
    monkeypatch.setattr(jg, "_root_for", lambda uid: "/tmp/jev-root")
    monkeypatch.setattr(jg, "_in_flight", jg.SHED_FACTOR)   # at the threshold
    out = asyncio.run(jg._evaluate(None, "Q1 答案是 3.65", "shadow", "u",
                                   "", "", [], 1.0))
    assert out is None                       # fail-open, caller keeps the reply
    assert len(rows) == 1
    assert rows[0]["verdict"] == "skipped" and rows[0]["reason"] == "burst"


def test_shed_threshold_is_above_the_pool_size(monkeypatch):
    """Shedding at or below the worker count would drop work the pool could
    actually have absorbed."""
    monkeypatch.setattr(jg, "gate_max_workers", lambda default=None: 4)
    assert jg.SHED_FACTOR >= 1


def test_evaluate_survives_missing_asyncio_primitives(monkeypatch):
    """Degrade, do not crash - and do not run the work twice."""
    import asyncio
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return {"verdict": "pass", "reasons": []}

    monkeypatch.setattr(jg, "_get_running_loop", None)
    monkeypatch.setattr(jg, "_evaluate_sync", fake)
    out = asyncio.run(jg._evaluate(None, "x", "shadow", "u", "", "", [], 1.0))
    assert out == {"verdict": "pass", "reasons": []}
    assert calls == [1]


def test_blocking_call_does_not_freeze_other_turns(monkeypatch):
    """The JEV request runs in a thread, so a slow one must not stall the
    event loop for everybody else."""
    import asyncio
    import time as _time

    def slow(*a, **k):
        _time.sleep(1.0)
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", slow)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def go():
        ticks = []

        async def ticker():
            t0 = _time.time()
            while _time.time() - t0 < 0.4:
                ticks.append(1)
                await asyncio.sleep(0.01)

        t = asyncio.ensure_future(ticker())
        await jg.maybe_jev_guard(final=_GUARDY, uid="u_test", mode="enforce")
        await t
        return len(ticks)

    assert asyncio.run(go()) > 10      # the loop stayed responsive


def test_cost_gate_skips_the_call_entirely(monkeypatch):
    import asyncio
    called = []

    def fake(*a, **k):
        called.append(1)
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    out = asyncio.run(jg.maybe_jev_guard(
        final="好的同学，收到啦！", uid="u_test", mode="enforce"))
    assert out == "" and called == []


def test_guard_marker_is_idempotent(monkeypatch):
    """Never re-guard our own replacement text."""
    import asyncio
    called = []

    def fake(*a, **k):
        called.append(1)
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    out = asyncio.run(jg.maybe_jev_guard(
        final=jg.GUARDED_MARKER + "\n" + _GUARDY, uid="u_test",
        mode="enforce"))
    assert out == "" and called == []


def test_gate_timeout_is_clamped():
    old = os.environ.get(jg.TIMEOUT_ENV)
    try:
        os.environ[jg.TIMEOUT_ENV] = "999"
        assert jg.gate_timeout() == 15.0
        os.environ[jg.TIMEOUT_ENV] = "0.001"
        assert jg.gate_timeout() == 0.5
        os.environ[jg.TIMEOUT_ENV] = "nonsense"
        assert jg.gate_timeout() == jg.DEFAULT_TIMEOUT
    finally:
        if old is None:
            os.environ.pop(jg.TIMEOUT_ENV, None)
        else:
            os.environ[jg.TIMEOUT_ENV] = old


# --- cost gate -------------------------------------------------------------

def test_verified_turn_always_qualifies():
    assert jg.looks_question_related("ok", verified=True)
    assert jg.looks_question_related("ok", photo=True)


def test_short_chat_turn_is_skipped():
    assert not jg.looks_question_related("好的，收到！", )
    assert not jg.looks_question_related("")


def test_answer_bearing_reply_qualifies():
    assert jg.looks_question_related(
        "这题的最终答案是 3.65，来自 2024 Q1(d)，标准答案见下方。")


def test_long_unrelated_reply_is_skipped():
    assert not jg.looks_question_related(
        "好的同学，我明白你的意思了。我们下节课继续，学习计划已经安排好了。"
        "记得按时休息，明天见哦，加油！")


# --- evidence gathering (read-only, tmp ledgers) ---------------------------

def test_ledger_rows_skips_junk_and_keeps_order(tmp_path):
    (tmp_path / "ncea_served.jsonl").write_text(
        '{"ts": 1, "entry_id": "A"}\nnot json\n\n{"ts": 2, "entry_id": "B"}\n',
        encoding="utf-8")
    rows = jg._ledger_rows(str(tmp_path), "ncea_served.jsonl")
    assert [r["entry_id"] for r in rows] == ["A", "B"]


def test_ledger_entry_ids_spans_both_ledgers(tmp_path):
    (tmp_path / "ncea_served.jsonl").write_text(
        '{"entry_id": "91579-2024-Q1d"}\n', encoding="utf-8")
    (tmp_path / "ncea_graded.jsonl").write_text(
        '{"entry_id": "91392-2025-Q3bi", "answer": "5.68"}\n',
        encoding="utf-8")
    assert jg.ledger_entry_ids(str(tmp_path)) == [
        "91392-2025-Q3bi", "91579-2024-Q1d"]


def test_fresh_row_counts_as_this_turn():
    row, fresh = jg._this_turn([{"ts": 1000, "entry_id": "A"}], 1000 + 10)
    assert fresh and row["entry_id"] == "A"


def test_stale_row_does_not_count_as_this_turn():
    """An earlier turn's question must never score this turn's reply."""
    row, fresh = jg._this_turn(
        [{"ts": 1000, "entry_id": "A"}], 1000 + jg.RECENCY_WINDOW + 1)
    assert not fresh
    assert row["entry_id"] == "A"      # still available for audit


def test_missing_timestamp_is_not_fresh():
    _, fresh = jg._this_turn([{"entry_id": "A"}], 10 ** 9)
    assert not fresh


def test_empty_ledger_is_not_fresh():
    assert jg._this_turn([], 5) == ({}, False)


def test_render_key_carries_entry_and_answer():
    block = jg._render_key(
        {"entry_id": "91579-2024-Q1d", "answer": "4.21"})
    assert "91579-2024-Q1d" in block and "4.21" in block
    assert "VERIFIED ANSWER KEY" in block


# --- per-turn attribution (the cross-turn mixing fix) ----------------------

def test_turn_row_selects_the_row_written_by_this_turn():
    """Two turns inside the same recency window: only the id can decide."""
    rows = [{"ts": 1000, "entry_id": "OLD", "turn": "t1"},
            {"ts": 1010, "entry_id": "NEW", "turn": "t2"}]
    row, fresh, src = jg._turn_row(rows, 1011, "t1")
    assert fresh and src == "turn" and row["entry_id"] == "OLD"


def test_turn_row_reports_absence_when_the_ledger_is_stamp_aware():
    """Served on an earlier turn is NOT served this turn - the honest
    answer is 'no served evidence', not the previous turn's question."""
    rows = [{"ts": 1000, "entry_id": "OLD", "turn": "t1"}]
    assert jg._turn_row(rows, 1001, "t2") == ({}, False, "turn")


def test_turn_row_falls_back_to_recency_without_stamps():
    """Pre-stamp installs and the direct serve_question path keep working."""
    rows = [{"ts": 1000, "entry_id": "A"}]
    row, fresh, src = jg._turn_row(rows, 1005, "t9")
    assert fresh and src == "recency" and row["entry_id"] == "A"


def test_turn_row_without_a_turn_id_is_pure_recency():
    rows = [{"ts": 1000, "entry_id": "A"}]
    assert jg._turn_row(rows, 1005, "")[2] == "recency"
    assert jg._turn_row([], 5, "")[2] == "none"
    # an unstamped row is still subject to the window, not to the turn id
    stale = 1000 + jg.RECENCY_WINDOW + 1
    assert jg._turn_row(rows, stale, "t1")[2] == "none"


def test_gather_evidence_does_not_mix_two_turns(tmp_path, monkeypatch):
    """The live false positive: a question served LAST turn paired with a key
    injected THIS turn, both fresh by clock, guarded a correct reply."""
    (tmp_path / "ncea_served.jsonl").write_text(
        '{"ts": 1000, "entry_id": "91267-2025-Q2b", "turn": "t1"}\n',
        encoding="utf-8")
    (tmp_path / "ncea_graded.jsonl").write_text(
        '{"ts": 1010, "entry_id": "91392-2025-Q3bi", "answer": "5.68",'
        ' "turn": "t2"}\n', encoding="utf-8")
    monkeypatch.setattr(jg, "_root_for", lambda uid: str(tmp_path))
    ev = jg.gather_evidence("u", now=1011, turn_id="t2")
    assert ev["served_block"] == ""           # last turn's question is gone
    assert ev["last_served"] == ""
    assert "5.68" in ev["answer_key_block"]   # this turn's key survives
    assert ev["last_graded"] == "91392-2025-Q3bi"
    assert ev["evidence"] == {"served": "turn", "graded": "turn"}


def test_gather_evidence_legacy_ledger_keeps_recency(tmp_path, monkeypatch):
    """Unstamped rows (an install predating the turn id) behave exactly as
    before, so the fix cannot regress a running deployment."""
    (tmp_path / "ncea_served.jsonl").write_text(
        '{"ts": 1000, "entry_id": "91579-2024-Q1d"}\n', encoding="utf-8")
    monkeypatch.setattr(jg, "_root_for", lambda uid: str(tmp_path))
    ev = jg.gather_evidence("u", now=1005, turn_id="t9")
    assert "91579-2024-Q1d" in ev["served_block"]
    assert ev["evidence"] == {"served": "recency", "graded": "none"}


def test_gather_evidence_drops_a_stale_turn_even_without_recency(
        tmp_path, monkeypatch):
    (tmp_path / "ncea_graded.jsonl").write_text(
        '{"ts": 1, "entry_id": "X", "answer": "1", "turn": "t0"}\n',
        encoding="utf-8")
    monkeypatch.setattr(jg, "_root_for", lambda uid: str(tmp_path))
    ev = jg.gather_evidence("u", now=10 ** 9, turn_id="t1")
    assert ev["answer_key_block"] == "" and ev["has_verified"] is False


def test_student_turn_id_reads_the_stamped_metadata():
    class M:
        metadata = {"_ncea_turn_id": "partner-p1-abc"}

    assert jg._student_turn_id(M()) == "partner-p1-abc"
    assert jg._student_turn_id(None) == ""
    assert jg._student_turn_id(type("M", (), {"metadata": None})()) == ""


def test_audit_row_records_the_turn_and_evidence_source(monkeypatch, tmp_path):
    """Operator reading depends on this: turn_stamped=0 in the report means
    the runtime stamp never landed and the weak window rule is what ran."""
    (tmp_path / "ncea_graded.jsonl").write_text(
        '{"ts": 1010, "entry_id": "91392-2025-Q3bi", "answer": "5.68",'
        ' "turn": "t2"}\n', encoding="utf-8")
    rows = []
    monkeypatch.setattr(jg, "_root_for", lambda uid: str(tmp_path))
    monkeypatch.setattr(jg, "_write_event", lambda root, rec: rows.append(rec))
    monkeypatch.setattr(jg, "call_systemone", lambda *a, **k: _guard_answers())

    class M:
        content = "这题的答案对吗？"
        metadata = {"_ncea_turn_id": "t2"}

    ev = jg._evaluate_sync(M(), _GUARDY, "shadow", "u", None, None, None, 1.0)
    assert ev["verdict"] == "guard"
    assert len(rows) == 1 and rows[0]["turn"] == "t2"
    # graded: this turn's row (turn). served: no ledger at all (none).
    assert rows[0]["evidence"] == {"served": "none", "graded": "turn"}
    assert rows[0]["key_present"] is True     # this turn's key was found
    assert rows[0]["serve_present"] is False   # ...and nothing was served


def test_report_exposes_whether_turn_stamping_is_live():
    r = jg.summarize_events([
        {"verdict": "pass", "turn": "t2",
         "evidence": {"served": "turn", "graded": "turn"}},
        {"verdict": "pass", "turn": "",
         "evidence": {"served": "recency", "graded": "none"}},
    ])
    assert r["turn_stamped"] == 1
    assert r["evidence"] == {"none": 1, "recency": 1, "turn": 2}


# --- operator report -------------------------------------------------------

def test_report_counts_guards_and_reasons():
    r = jg.summarize_events([
        {"verdict": "guard", "reasons": ["definite-answer-no-key"],
         "mode": "shadow", "model": "jev-1.13.0", "latency_ms": 300},
        {"verdict": "pass", "reasons": [], "mode": "shadow",
         "model": "jev-1.13.0", "latency_ms": 500},
    ])
    assert r["calls"] == 2 and r["guards"] == 1 and r["guard_rate"] == 0.5
    assert r["reasons"] == {"definite-answer-no-key": 1}
    assert r["modes"] == {"shadow": 2}
    assert r["models"] == {"jev-1.13.0": 2}
    assert r["latency_ms"] == {"p50": 500, "p95": 500, "max": 500}


def test_report_survives_an_empty_ledger():
    assert jg.summarize_events([])["guard_rate"] == 0.0
    assert jg.summarize_events(["junk"])["calls"] == 0


def test_report_exposes_a_broken_gate():
    """Errors must be their own bucket and must not dilute guard_rate -
    a gate that fails half the time is not '50% safe', it is broken."""
    r = jg.summarize_events([
        {"verdict": "error"}, {"verdict": "error"},
        {"verdict": "guard", "reasons": ["question-not-served"]},
        {"verdict": "pass", "reasons": []},
    ])
    assert r["errors"] == 2 and r["error_rate"] == 0.5
    assert r["guard_rate"] == 0.5        # 1 guard / 2 that actually ran


def test_report_error_rate_is_zero_on_clean_ledger():
    r = jg.summarize_events([{"verdict": "pass"}])
    assert r["errors"] == 0 and r["error_rate"] == 0.0
    assert r["guard_rate"] == 0.0


def test_report_separates_sheds_from_failures():
    """A shed-heavy gate is not a broken gate, but it is NOT clean either -
    neither may hide inside guard_rate's denominator."""
    r = jg.summarize_events([
        {"verdict": "skipped", "reason": "burst"},
        {"verdict": "skipped", "reason": "burst"},
        {"verdict": "error"},
        {"verdict": "guard", "reasons": ["question-not-served"]},
    ])
    assert r["errors"] == 1 and r["skipped"] == 2 and r["evaluated"] == 1
    assert r["guard_rate"] == 1.0
    assert r["skip_rate"] == 0.5 and r["error_rate"] == 0.25


def test_failed_call_still_writes_an_error_row(monkeypatch):
    """A silent gap in the ledger would read as 'clean' during shadow."""
    import asyncio
    rows = []

    def boom(*a, **k):
        raise jg.JevError("HTTP 503: upstream")

    monkeypatch.setattr(jg, "call_systemone", boom)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setattr(jg, "_write_event", lambda root, rec: rows.append(rec))
    monkeypatch.setattr(jg, "_root_for", lambda uid: "/tmp/jev-root")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    out = asyncio.run(jg.maybe_jev_guard(
        final=_GUARDY, uid="u_test", mode="enforce"))
    assert out == ""                      # fail-open
    assert len(rows) == 1 and rows[0]["verdict"] == "error"
    assert "503" in rows[0]["error"]


def test_successful_call_writes_exactly_one_row(monkeypatch):
    import asyncio
    rows = []

    def fake(*a, **k):
        return _guard_answers()

    monkeypatch.setattr(jg, "call_systemone", fake)
    monkeypatch.setattr(jg, "gather_evidence", _no_evidence)
    monkeypatch.setattr(jg, "_write_event", lambda root, rec: rows.append(rec))
    monkeypatch.setattr(jg, "_root_for", lambda uid: "/tmp/jev-root")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    asyncio.run(jg.maybe_jev_guard(
        final=_GUARDY, uid="u_test", mode="enforce"))
    assert len(rows) == 1 and rows[0]["verdict"] == "guard"


# --- wire-up: the patch chain must actually chain --------------------------

def test_jev_gate_edit_anchors_on_the_guard_edit():
    """EDIT_JEVGATE_OLD must be found in the text EDIT_GUARD_NEW installs,
    or apply_site_patches aborts with 'stock text not found' on a fresh venv."""
    spec2 = importlib.util.spec_from_file_location(
        "apply_site_patches",
        os.path.join(HERE, "..", "scripts", "apply_site_patches.py"))
    asp = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(asp)
    assert asp.EDIT_JEVGATE_OLD in asp.EDIT_GUARD_NEW
    assert "ncea_output_guard" not in asp.EDIT_JEVGATE_NEW.split(
        "NCEA JEV semantic gate")[1]
    # The marker must be unique to the JEV edit, or it pre-skips itself.
    assert async_marker_ok(asp)


def async_marker_ok(asp):
    marker = "maybe_jev_guard"
    assert marker not in asp.EDIT_GUARD_NEW
    assert marker in asp.EDIT_JEVGATE_NEW
    assert asp.INSTALL_JEVGATE[1].endswith("ncea_jev_gate.py")
    return True


def test_turn_stamp_edits_are_chained_and_self_consistent():
    """The per-turn id has to reach the ledgers through edits that apply on
    BOTH a fresh venv and an already-patched production, so:
      * each chained edit anchors on the text the edit above it installs;
      * each NEW text is not itself a valid OLD (or it re-applies forever);
      * each edit's marker is unique per file (a duplicated marker SKIPs the
        later edit, which is how a chained edit silently goes missing)."""
    asp = _asp()
    assert asp.EDIT_SERVEBLOCK2_OLD in asp.EDIT_SERVEBLOCK_NEW
    assert asp.EDIT_GRADEPIPE2_OLD in asp.EDIT_GRADEPIPE_NEW
    assert asp.EDIT_SERVELEDGER_OLD in asp.EDIT_TOOLS_NEW
    # The stamp edit PREPENDS to its anchor (so OLD is a prefix of NEW); the
    # other three REPLACE their anchor, so a re-run must not find it again.
    assert asp.EDIT_TURNSTAMP_OLD in asp.EDIT_TURNSTAMP_NEW
    for old, new in ((asp.EDIT_SERVELEDGER_OLD, asp.EDIT_SERVELEDGER_NEW),
                     (asp.EDIT_SERVEBLOCK2_OLD, asp.EDIT_SERVEBLOCK2_NEW),
                     (asp.EDIT_GRADEPIPE2_OLD, asp.EDIT_GRADEPIPE2_NEW)):
        assert old not in new
    for rel in {r for r, _, _, _ in asp.EDITS}:
        markers = [m for r, m, _, _ in asp.EDITS if r == rel]
        assert len(markers) == len(set(markers)), rel


def test_chained_edits_pass_the_turn_id_into_the_helpers():
    """The ledger row can only be attributed if the id actually reaches the
    helpers that write it - an unused stamp would leave the gate on the
    recency rule while the report claims stamping is live."""
    asp = _asp()
    assert "turn_id" in asp.EDIT_SERVEBLOCK2_NEW
    assert "turn_id" in asp.EDIT_GRADEPIPE2_NEW
    assert 'msg.metadata["_ncea_turn_id"]' in asp.EDIT_TURNSTAMP_NEW
    assert "set_current_turn_id" in asp.EDIT_TURNSTAMP_NEW
    assert '"turn"' in asp.EDIT_SERVELEDGER_NEW


# --- Rule 2's deterministic half: cite_in_bank -----------------------------

BANK = '''# Verified question index - AS91392

## 91392-2022-Q1c \u00b7 2022 Exam Q1(c) \u00b7 Excellence

- Location: 91392-exm-2022.pdf, Question One (c), mixing CaCl2 and AgNO3.
- Identifying stem: "30.0 mL of 0.0686 mol L-1 calcium chloride... 50.0 mL of 0.00154 mol L-1 silver nitrate".
- Answer: IP = 4.95e-5 > Ks 1.80e-10, so AgCl precipitates.
'''


def _lookup(text):
    return lambda code: text if code == "91392" else ""


def test_cite_in_bank_rejects_a_bare_location_quote():
    """Naming the paper is not evidence of which question is on offer. Live
    proof (AU 2026-09-21): a reply attached an invented stem to a real
    filename and satisfied the excuse, so a fabricated question passed."""
    reply = "**Location**  \n`91392-exm-2022.pdf`, Question One (c)\n"
    assert jg.cite_in_bank(reply, _lookup(BANK)) == ""


def test_cite_in_bank_resolves_a_quoted_stem_against_the_location():
    """Same reply, plus the entry's own identifying stem: real."""
    reply = ("**Location**  \n`91392-exm-2022.pdf`, Question One (c)\n"
             "**\u9898\u76ee\u8eab\u4efd\u786e\u8ba4**\n"
             "\u201c30.0 mL of 0.0686 mol L-1 calcium chloride... "
             "50.0 mL of 0.00154 mol L-1 silver nitrate\u201d")
    assert jg.cite_in_bank(reply, _lookup(BANK)) == "91392"


def test_cite_in_bank_resolves_a_full_id():
    reply = "见 91392-2022-Q1c 那道题（91392-exm-2022.pdf, Question One (c)）。"
    assert jg.cite_in_bank(reply, _lookup(BANK)) == "91392-2022-Q1c"


def test_cite_in_bank_folds_unicode_and_markdown():
    """The reply renders L\u207b\u00b9 where the bank file has L-1, and wraps
    the quoted stem in curly quotes."""
    reply = ("见 91392 这道题的身份确认：\n"
             "\u201c30.0 mL of 0.0686 mol L\u207b\u00b9 calcium chloride... "
             "50.0 mL of 0.00154 mol L\u207b\u00b9 silver nitrate\u201d")
    assert jg.cite_in_bank(reply, _lookup(BANK)) == "91392"


def test_cite_in_bank_rejects_a_composed_question():
    reply = ("好的，这是 2024 年的一道真题：\n"
             "Question 2 (a) A 0.20 kg ball is thrown vertically upwards at "
             "12 m s-1. Calculate the maximum height reached. (3 marks)")
    assert jg.cite_in_bank(reply, _lookup(BANK)) == ""


def test_cite_in_bank_rejects_a_fabricated_location():
    reply = ("这题来自 91579-exm-2025.pdf, Question Three (b)(ii)，"
             "标准答案是 3.65，你直接写就有 Excellence。")
    assert jg.cite_in_bank(reply, _lookup(BANK)) == ""


def test_cite_in_bank_rejects_a_bogus_full_id():
    reply = "见 91392-2019-Q9z 那道题（91392-exm-2019.pdf, Question Nine (z)）。"
    assert jg.cite_in_bank(reply, _lookup(BANK)) == ""


def test_cite_in_bank_ignores_short_fragments():
    """A bare code mention is not a quotation; 'PDF'/'Location' must not
    resolve against any bank file either."""
    assert jg.cite_in_bank("这是 91392 的题，先看 PDF。\nLocation:\n",
                           _lookup(BANK)) == ""


def test_cite_in_bank_handles_empty_input():
    assert jg.cite_in_bank("", _lookup(BANK)) == ""
    assert jg.cite_in_bank(None, _lookup(BANK)) == ""


def test_cite_in_bank_with_no_bank_text():
    assert jg.cite_in_bank("`91392-exm-2022.pdf`, Question One (c)",
                           _lookup("")) == ""


def test_cite_in_bank_reads_the_shipped_bank_file():
    """Integration: the real bank must resolve its own entry, with no
    injected lookup - this is the path the production hook uses.

    The reply names the entry the way the tutor does (location plus the
    entry id); it quotes no question text, because the shipped index keeps
    only locations, answers and verification."""
    jg._BANK_CACHE.clear()
    reply = ("**Location**\n`91392-exm-2022.pdf`, Question One (c)\n"
             "**Verified entry**\n`91392-2022-Q1c`")
    assert jg.cite_in_bank(reply) == "91392-2022-Q1c"


# The production reply that exposed the hole: a REAL filename with an
# INVENTED identifying stem, and an explicit decline. Verbatim from the AU
# session store (gatedrill1, 2026-09-21 00:20:32).
FABRICATED_SAME_FILENAME = (
    "**\u4e0d\u884c\uff0c\u8fd9\u9053\u9898\u6211\u4e0d\u80fd\u7ed9\u4f60\u201c"
    "\u4f4d\u7f6e\u201d\u3002**\n\n"
    "**\u4f60\u73b0\u5728\u80fd\u505a\u7684\uff1a**\n"
    "**\u5982\u679c\u4f60\u73b0\u5728\u6ca1\u6709 2025 \u5377\uff0c"
    "\u6211\u5c31\u7ed9\u4f60\u4e00\u9053\u53ef\u9a8c\u8bc1\u7684\u540c\u7c7b\u9898\uff1a**\n\n"
    "**Location**\n`91267-exm-2024.pdf`, Question One (b)\n\n"
    "**\u9898\u76ee\u8eab\u4efd\u786e\u8ba4\uff08\u6700\u591a\u4e24\u884c\uff09**\n"
    "\u201cA courier company weighs parcels. The masses are normally "
    "distributed with mean 1200 g and standard deviation 40 g.\u201d")


def test_cite_in_bank_rejects_an_invented_stem_on_a_real_filename():
    jg._BANK_CACHE.clear()
    assert jg.cite_in_bank(FABRICATED_SAME_FILENAME) == ""


def test_a_decline_alone_does_not_excuse_an_invented_question():
    """Live 2026-09-21 00:20:32: the reply refused to invent the asked-for
    question, offered a substitute, and the substitute's stem was invented.
    The decline must not be sufficient on its own."""
    v = jg.decide({"presents_question_to_attempt": noul(0.85),
                   "question_matches_verified": noul(0.04),
                   "declines_and_substitutes": noul(0.91),
                   "location_claim": choice("contradicts", 0.81)},
                  key_present=False, serve_present=True,
                  cited_in_bank=bool(jg.cite_in_bank(FABRICATED_SAME_FILENAME)))
    assert v["verdict"] == "guard"
    assert "question-mismatch" in v["reasons"]
    assert "location-contradicts-verified" in v["reasons"]


# --- installer self-check: a missing helper must fail LOUDLY --------------

def _asp():
    spec2 = importlib.util.spec_from_file_location(
        "apply_site_patches",
        os.path.join(HERE, "..", "scripts", "apply_site_patches.py"))
    asp = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(asp)
    return asp


def _all_sources(tmp_path, asp):
    for src, _ in asp.HELPERS:
        (tmp_path / src).write_text("x = 1\n", encoding="utf-8")


def _empty_site(tmp_path):
    site = tmp_path / "site"
    (site / "deeptutor" / "agents" / "_shared").mkdir(parents=True)
    return site


def test_helpers_registry_covers_every_install_tuple():
    """A helper added to an INSTALL_* tuple but forgotten in HELPERS would
    escape both the preflight and the verify."""
    asp = _asp()
    tuples = {v for k, v in vars(asp).items()
              if k.startswith("INSTALL_") and isinstance(v, tuple)}
    assert set(asp.HELPERS) == tuples
    assert len({h[0] for h in asp.HELPERS}) == len(asp.HELPERS)


def test_preflight_aborts_and_names_every_missing_source(monkeypatch,
                                                        tmp_path):
    """The 2026-09-21 incident: production kept five of six helpers because
    the sixth source was never shipped, and fail-open hid the gap. The
    preflight must refuse to start, name the file(s), and touch nothing."""
    asp = _asp()
    monkeypatch.setattr(asp, "_scripts_dir", lambda: tmp_path)
    (tmp_path / "ncea_serve_block.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        asp.preflight_helpers()
    msg = str(exc.value)
    assert "ncea_output_guard.py" in msg
    assert "ncea_jev_gate.py" in msg
    assert "Nothing was modified" in msg
    assert not list(tmp_path.glob("__pycache__"))


def test_preflight_passes_when_every_source_is_present(monkeypatch, tmp_path):
    asp = _asp()
    monkeypatch.setattr(asp, "_scripts_dir", lambda: tmp_path)
    _all_sources(tmp_path, asp)
    asp.preflight_helpers()          # must not raise


def test_install_then_verify_is_clean(monkeypatch, tmp_path):
    asp = _asp()
    monkeypatch.setattr(asp, "_scripts_dir", lambda: tmp_path)
    _all_sources(tmp_path, asp)
    site = _empty_site(tmp_path)
    assert asp.install_helpers(site) == len(asp.HELPERS)
    assert asp.verify_helpers(site) == []
    # The web-demo entry is a frontend edit, not a helper: with no built
    # frontend in the tree it reports a gap the installer cannot close. The
    # helper assertions above are the point of this test.
    if asp.verify_web_demo_entry(site):
        pytest.skip("web demo entry needs a built frontend")
    assert asp.report_helper_state(site) == 0
    # idempotent: a second pass installs nothing
    assert asp.install_helpers(site) == 0


def test_verify_flags_a_missing_helper(monkeypatch, tmp_path):
    """Exactly the production shape: the hook is present, the module is not."""
    asp = _asp()
    monkeypatch.setattr(asp, "_scripts_dir", lambda: tmp_path)
    _all_sources(tmp_path, asp)
    site = _empty_site(tmp_path)
    asp.install_helpers(site)
    (site / asp.INSTALL_GUARD[1]).unlink()
    problems = asp.verify_helpers(site)
    assert len(problems) == 1
    assert "NOT INSTALLED" in problems[0]
    assert asp.report_helper_state(site) == 1


def test_verify_flags_a_stale_helper(monkeypatch, tmp_path):
    asp = _asp()
    monkeypatch.setattr(asp, "_scripts_dir", lambda: tmp_path)
    _all_sources(tmp_path, asp)
    site = _empty_site(tmp_path)
    asp.install_helpers(site)
    (tmp_path / asp.INSTALL_GUARD[0]).write_text("x = 2\n", encoding="utf-8")
    problems = asp.verify_helpers(site)
    assert len(problems) == 1 and "STALE" in problems[0]


def test_verify_flags_an_uncompilable_helper(monkeypatch, tmp_path):
    asp = _asp()
    monkeypatch.setattr(asp, "_scripts_dir", lambda: tmp_path)
    _all_sources(tmp_path, asp)
    site = _empty_site(tmp_path)
    asp.install_helpers(site)
    bad = "def (:\n"
    (tmp_path / asp.INSTALL_GUARD[0]).write_text(bad, encoding="utf-8")
    (site / asp.INSTALL_GUARD[1]).write_text(bad, encoding="utf-8")
    problems = asp.verify_helpers(site)
    assert any("DOES NOT COMPILE" in p for p in problems)


def test_verify_only_mode_changes_nothing(monkeypatch, tmp_path, capsys):
    asp = _asp()
    monkeypatch.setattr(asp, "_scripts_dir", lambda: tmp_path)
    _all_sources(tmp_path, asp)
    site = _empty_site(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["apply_site_patches.py", str(site), "--verify"])
    with pytest.raises(SystemExit) as exc:
        asp.main()
    assert exc.value.code == 1
    assert "NOT INSTALLED" in capsys.readouterr().out
    assert not (site / asp.INSTALL_GUARD[1]).exists()
