"""The chat loop's two latency contracts (measured from production, not guessed).

Evidence this locks in (AU, 166 real turns, 2026-09-21):

* Turn span p50 34.5s / p90 126.3s / max 492.9s, 20 turns over 2 minutes.
* LLM rounds were p50 74% and p90 99.5% of that span — tools, RAG and gaps
  are noise by comparison.
* The prompt was **2.2x the model's window**: `context_budget` reported
  `used_tokens 36460` against `window 16384`, and the single largest
  controllable segment was the `## tools` block at 11,722 tokens — of which
  ~9.8k was the knowledge-base inventory: the chat loop prepends `kb_note` to
  the tool manifest, and `kb_note` lists EVERY document name of EVERY
  attached KB (73 KBs / 547 documents on that partner). Reading those 73
  manifests also cost 9.5s per turn.
* `max_rounds` bounds how many rounds a turn may run, never how long a round
  takes. The worst observed turn was ONE round: 492.9s for a 720-character
  reply.

So there are two independent fixes, and these tests hold them apart:

1. the inventory keeps its counts and drops the file names — `kb_files`
   already answers "what is in here", and the prompt already points at it;
2. the loop gets a wall-clock ceiling as well as a round ceiling, and a
   capability that declares its own round budget (solve, research, visualize)
   is deliberately left without one.
3. the shape of that prompt stops being duplicated. The same partner attached
   73 KBs, and pre-seeding took the first three **in registry order** with no
   relevance test: a Level-1 chemistry question retrieved three Level-2
   *biology* KBs for ~16-23s, injected their passages, and the model then
   reported the subject as absent. Meanwhile the KB names were listed twice
   (once in the rag note, once in the inventory) and every line carried
   "(llamaindex, index ready)".

Run: .venv\\Scripts\\python.exe -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from deeptutor.knowledge.manifest import (
    UNAVAILABLE_REMOTE,
    KbDocument,
    KbManifest,
    render_manifest_note,
)
from deeptutor.agents.loop.pipeline import _seed_kbs, _seed_tokens


def _doc(name: str) -> KbDocument:
    return KbDocument(name=name, size=10)


def _kbs() -> list[KbManifest]:
    """A 2-KB stand-in for the 73-KB partner: same shape, readable size."""
    return [
        KbManifest(
            name="NZQA_B2_AS91156_Cells",
            provider="llamaindex",
            status="ready",
            total=3,
            matched=3,
            documents=tuple(_doc(n) for n in ("cells-a.pdf", "cells-b.pdf", "cells-c.pdf")),
        ),
        KbManifest(
            name="NZQA_C3_AS91392_Equilibrium",
            provider="llamaindex",
            status="ready",
            total=2,
            matched=2,
            documents=(_doc("2025-exm.pdf"), _doc("sched.pdf")),
        ),
    ]


# --- fix 1: the inventory keeps counts, not the file-name listing -------------


def test_default_still_lists_every_document() -> None:
    """Callers that ask for the full inventory are unaffected."""
    note = render_manifest_note(_kbs(), language="zh")
    for name in ("cells-a.pdf", "cells-b.pdf", "2025-exm.pdf", "sched.pdf"):
        assert name in note


def test_can_drop_documents_while_keeping_every_count() -> None:
    """The compact form loses no count and no KB name."""
    note = render_manifest_note(_kbs(), language="zh", include_documents=False)
    for name in ("cells-a.pdf", "cells-b.pdf", "2025-exm.pdf", "sched.pdf"):
        assert name not in note
    assert "NZQA_B2_AS91156_Cells" in note
    assert "NZQA_C3_AS91392_Equilibrium" in note
    assert "共 3 个文档" in note
    assert "共 2 个文档" in note


def test_compact_form_is_smaller() -> None:
    """The whole point: fewer tokens for the same authoritative counts."""
    full = render_manifest_note(_kbs(), language="zh")
    compact = render_manifest_note(_kbs(), language="zh", include_documents=False)
    assert len(compact) < len(full)


def test_the_swapped_paragraph_still_names_the_tool() -> None:
    """Dropping the names must not claim the block still answers for them.

    The full-authority paragraph says file names are authoritative *here*.
    That sentence is a lie once the names are gone, so the compact form must
    carry the paragraph that sends those questions to ``kb_files``.
    """
    compact = render_manifest_note(_kbs(), language="zh", include_documents=False)
    assert "kb_files" in compact
    assert "文件名不在此列出" in compact
    full = render_manifest_note(_kbs(), language="zh")
    assert "文件名不在此列出" not in full

    compact_en = render_manifest_note(_kbs(), language="en", include_documents=False)
    assert "kb_files" in compact_en
    assert "File names are deliberately not listed here" in compact_en


def test_a_healthy_kb_does_not_repeat_its_provider_and_status() -> None:
    """"(llamaindex, index ready)" on every line is the same string, 73 times.

    It only carries information when it reports something worth acting on, so
    the compact form keeps it for a KB that is not ready and drops it for one
    that is.
    """
    compact = render_manifest_note(_kbs(), language="zh", include_documents=False)
    assert "索引就绪" not in compact
    assert "llamaindex" not in compact

    broken = [KbManifest(name="Broken_KB", provider="llamaindex", status="error", total=2)]
    note = render_manifest_note(broken, language="zh", include_documents=False)
    assert "索引失败" in note


# --- fix 3: pre-seed on relevance, and stop repeating the names ----------------


_KEYS = [
    "NZQA_B2_AS91156_Cells",
    "NZQA_B2_AS91157_Genetics",
    "NZQA_B2_AS91158_Ecology",
    "NZQA_C3_AS91392_Equilibrium",
    "NZQA_C3_AS91393_Redox",
    "NZQA_M2_AS91267_Probability",
]


def test_a_standard_code_in_the_query_selects_its_own_kb() -> None:
    """The code is how this corpus is addressed. "91392" means that KB."""
    assert _seed_kbs(_KEYS, "91392 2025 Q3b(i) question") == ["NZQA_C3_AS91392_Equilibrium"]
    assert _seed_kbs(_KEYS, "91393 redox potential") == ["NZQA_C3_AS91393_Redox"]


def test_a_topic_word_in_the_kb_name_selects_its_own_kb() -> None:
    assert _seed_kbs(_KEYS, "equilibrium Kc calculation") == ["NZQA_C3_AS91392_Equilibrium"]


def test_an_unrelated_query_seeds_nothing_rather_than_the_first_three() -> None:
    """This is the 7-minute turn's bug: three biology KBs for chemistry.

    Seeding nothing costs nothing — the model still has `rag` and picks its
    own KB — whereas seeding the wrong three costs seconds and teaches it
    that the subject is absent.
    """
    assert _seed_kbs(_KEYS, "give me a real question for testing") == []
    assert _seed_kbs(_KEYS, "ncea level 10 chemsity") == []


def test_a_small_selection_is_still_seeded_wholesale() -> None:
    """A deliberate 2-3 KB selection is a choice, not a lottery."""
    few = _KEYS[:3]
    assert _seed_kbs(few, "nothing here matches anything") == few
    assert _seed_kbs(_KEYS[:2], "") == _KEYS[:2]


def test_ranking_is_bounded_deterministic_and_tie_broken_by_order() -> None:
    """Two matches beat one, and a tie keeps the caller's registry order."""
    picked = _seed_kbs(_KEYS, "91392 equilibrium")
    assert picked == ["NZQA_C3_AS91392_Equilibrium"]
    tie = _seed_kbs(_KEYS, "cells genetics ecology")
    assert tie == _KEYS[:3]
    assert _seed_kbs(_KEYS, "cells genetics ecology") == tie


def test_only_a_multi_digit_code_becomes_a_token() -> None:
    """"Q3b" must not yield "3", which half the registry would match."""
    tokens = _seed_tokens("NZQA_C3_AS91392_Equilibrium")
    assert "91392" in tokens
    assert "as91392" in tokens
    assert "c3" in tokens
    assert "3" not in _seed_tokens("Q3b")


def test_the_seed_path_uses_the_ranking_not_a_slice() -> None:
    """Guards the wiring: a kept-around slice would silently undo all of it."""
    import inspect

    from deeptutor.agents.loop import pipeline as pipeline_mod

    src = inspect.getsource(pipeline_mod.AgenticLoopPipeline._retrieve_kb_seed_block)
    assert "kbs = _seed_kbs(kbs, query)" in src
    assert "kbs = kbs[:KB_SEED_MAX_KBS]" not in src


def test_the_kb_names_are_not_repeated_when_the_inventory_lists_them() -> None:
    """The rag note keeps its instruction, not a second copy of the list."""
    from deeptutor.agents.loop.pipeline import AgenticLoopPipeline

    p = AgenticLoopPipeline.__new__(AgenticLoopPipeline)
    p.language = "zh"
    p._selected_kbs = lambda ctx: ["A", "B"]
    p._coexisting_rag_kbs = lambda ctx: ["NZQA_C3_AS91392_Equilibrium", "NZQA_B2_AS91156_Cells"]
    p._kb_manifest_system_note = lambda: "\n## 知识库清单\n- A\n- B\n"
    p._pageindex_system_note = lambda: ""

    note = p._kb_system_note(None)
    assert "NZQA_C3_AS91392_Equilibrium" not in note
    assert "2 个知识库" in note
    assert "kb_name" in note

    # With no inventory to point at, the names must come back.
    p._kb_manifest_system_note = lambda: ""
    fallback = p._kb_system_note(None)
    assert "NZQA_C3_AS91392_Equilibrium" in fallback


def test_unavailable_and_empty_kbs_are_handled_in_both_forms() -> None:
    """Non-enumerable and empty KBs take the same branch either way."""
    kbs = [
        KbManifest(name="Remote_KB", unavailable=UNAVAILABLE_REMOTE),
        KbManifest(name="Empty_KB", total=0, matched=0),
    ]
    for flag in (True, False):
        note = render_manifest_note(kbs, language="en", include_documents=flag)
        assert "Remote_KB" in note
        assert "Empty_KB" in note
        assert "cannot be read" in note
        assert "no documents yet" in note


# --- fix 2: the wall-clock ceiling -------------------------------------------


def _pipeline(**kwargs):
    from deeptutor.agents.chat.agentic_pipeline import AgenticChatPipeline

    return AgenticChatPipeline(language="zh", **kwargs)


def test_chat_loop_has_a_positive_wall_clock_ceiling() -> None:
    """Chat is the loop the user waits on, so it is bounded in seconds too."""
    assert _pipeline().time_budget_s > 0


def test_explicit_round_budget_opts_out_of_the_wall_clock() -> None:
    """solve / research / visualize declare their own budget and keep it.

    Those passes are long-running on purpose (replan cycles, research blocks),
    so a chat ceiling reaching into them would cut off the work mid-flight.
    """
    for given in (5, 12):
        p = _pipeline(max_rounds=given)
        assert p.max_rounds == given
        assert p.time_budget_s == 0


def test_time_budget_is_configurable_and_zero_disables_it(monkeypatch) -> None:
    from deeptutor.agents.loop import pipeline as pipeline_mod

    def chat_params(**over):
        base = {
            "temperature": 0.2,
            "max_rounds": 8,
            "time_budget_s": 120,
            "exploring": {"max_tokens": 1600},
            "responding": {"max_tokens": 8000},
        }
        base.update(over)
        return base

    monkeypatch.setattr(pipeline_mod, "get_chat_params", lambda: chat_params(time_budget_s=45))
    assert _pipeline().time_budget_s == 45

    monkeypatch.setattr(pipeline_mod, "get_chat_params", lambda: chat_params(time_budget_s=0))
    assert _pipeline().time_budget_s == 0


def test_malformed_and_negative_budgets_never_become_negative(monkeypatch) -> None:
    """A typo must fall back to the default, not to "no ceiling" or a negative."""
    from deeptutor.agents.loop import pipeline as pipeline_mod
    from deeptutor.agents.loop.pipeline import DEFAULT_TIME_BUDGET_S

    monkeypatch.setattr(
        pipeline_mod,
        "get_chat_params",
        lambda: {"temperature": 0.2, "max_rounds": 8, "time_budget_s": "not-a-number"},
    )
    assert _pipeline().time_budget_s == DEFAULT_TIME_BUDGET_S

    monkeypatch.setattr(
        pipeline_mod,
        "get_chat_params",
        lambda: {"temperature": 0.2, "max_rounds": 8, "time_budget_s": -30},
    )
    assert _pipeline().time_budget_s == 0


def test_property_clamps_a_corrupt_field() -> None:
    """The reading is defensive: a bad attribute cannot produce a negative wait."""
    from deeptutor.agents.loop.pipeline import AgenticLoopPipeline, DEFAULT_TIME_BUDGET_S

    p = AgenticLoopPipeline.__new__(AgenticLoopPipeline)
    p._time_budget_s = "gone"
    assert p.time_budget_s == DEFAULT_TIME_BUDGET_S
    p._time_budget_s = -1
    assert p.time_budget_s == 0


def _loop_source() -> str:
    """Source of the loop's control flow, wherever this release keeps it.

    1.6.x ran it inside ``AgentLoop._run_loop``; 1.7.0 split it into
    ``loop_host`` (effects) + ``python_scheduler`` (control flow). The
    contract under test is the same on both, so read whichever exists.
    """
    import inspect

    from deeptutor.agents.loop import agent_loop

    src = inspect.getsource(agent_loop.AgentLoop._run_loop)
    try:
        from deeptutor.agents.loop import python_scheduler
    except ImportError:  # 1.6.x: the loop has not been split yet
        return src
    return src + inspect.getsource(python_scheduler.run_loop)


def test_settlement_trip_is_capped_at_one_extra_round() -> None:
    """A time trip must not hand the tail back to MAX_SETTLEMENT_ROUNDS.

    Regression guard on the wiring, not the number: if the loop stopped
    capping a time trip's settlement phase, the ceiling would bound
    exploration only and the turn could still run for minutes afterwards.
    """
    src = _loop_source()
    assert "settlement_limit = 1 if over_budget" in src
    assert "deadline = time.monotonic() + budget_s if budget_s > 0 else None" in src
    assert 'reason="time" if over_budget else "rounds"' in src


def test_settlement_trip_says_which_budget_ran_out() -> None:
    """The reader is told the clock ran out, not that the round budget did."""
    import inspect

    from deeptutor.agents.loop import agent_loop

    src = inspect.getsource(agent_loop.AgentLoop._begin_settlement)
    assert "notices.loop_settlement_time" in src
    assert "notices.loop_settlement" in src
    # …and the loop has to actually pass the reason through, or the two branches
    # above are dead code and every trip reports "rounds".
    assert 'reason="time" if over_budget else "rounds"' in _loop_source()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
