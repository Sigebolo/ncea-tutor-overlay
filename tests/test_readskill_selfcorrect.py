"""read_skill misses must be self-correcting (never send the model renaming).

Proven production failure (deeptutor 1.6.4): a missing qbank FILE was
reported as a missing SKILL, sending the model on a renaming spiral
(nccea-tutor, ncea_tutor, ...) that ate the whole turn with no answer.

We vendored a patch for 1.6.4 (scripts/patches/builtin___init__.py.patch,
now retired): on 1.6.7 upstream natively distinguishes the two cases via
SkillFileNotFoundError + a per-case hint. These tests lock the NATIVE
1.6.7 contract:

- wrong name -> "skill not found" + points at the (already-injected)
  Skills section, not a renaming invitation
- missing file in existing skill -> blames the FILE, never the skill

Run: .venv\\Scripts\\python.exe -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from deeptutor.tools.builtin import ReadSkillTool


def _patched_service(tmp_path: Path, monkeypatch) -> None:
    """Point the tool's service factory at tmp. The tool does
    ``from deeptutor.services.skill import get_skill_service`` INSIDE
    execute, so the patch must land on that SOURCE module."""
    from deeptutor.services import skill as skill_pkg
    from deeptutor.services.skill.service import SkillService

    monkeypatch.setattr(skill_pkg, "get_skill_service",
                        lambda: SkillService(root=tmp_path))


def _make_skill(root: Path, name: str, body: str = "# hi\n") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n{body}",
        encoding="utf-8",
    )


def test_miss_lists_available_names(tmp_path: Path, monkeypatch) -> None:
    """1.6.7 native: typo'd name -> 'skill not found' + direction to the
    Skills section (the full name list the model already has in context)."""
    _make_skill(tmp_path, "ncea-tutor")
    _patched_service(tmp_path, monkeypatch)
    result = asyncio.run(ReadSkillTool().execute(name="ncea-tutro"))
    assert not result.success
    assert "skill not found" in result.content
    assert "Skills section" in result.content


def test_hit_still_reads(tmp_path: Path, monkeypatch) -> None:
    _make_skill(tmp_path, "ncea-tutor", body="# NCEA rules\n")
    _patched_service(tmp_path, monkeypatch)
    result = asyncio.run(ReadSkillTool().execute(name="ncea-tutor"))
    assert result.success
    assert "NCEA rules" in result.content


def test_missing_file_blames_file_not_skill(tmp_path: Path, monkeypatch) -> None:
    """The killer case: existing skill + missing qbank file must NOT say
    'skill not found' (that lie caused the renaming spiral)."""
    _make_skill(tmp_path, "ncea-tutor", body="# NCEA rules\n")
    _patched_service(tmp_path, monkeypatch)
    result = asyncio.run(ReadSkillTool().execute(
        name="ncea-tutor", file="references/qbank_91164.md"))
    assert not result.success
    assert "file not found" in result.content
    assert "skill not found" not in result.content
    # Native recovery hint: both releases name the file and list what the skill
    # actually contains. 1.6.7 says "check the skill's SKILL.md or
    # references/", 1.7.0 says "…which holds: SKILL.md". Either satisfies the
    # contract — blame the FILE, never invite a rename.
    assert (
        "check the skill's SKILL.md or references/" in result.content
        or "which holds:" in result.content
    )
