"""Imagegen delivery contract on 1.6.6: workspace_present flow, no URL pasting.

History: on 1.6.4 we vendored a patch appending direct /files URLs + an
inline-markdown rule (partner cards didn't render). 1.6.6 redesigned the
flow natively (present-before-link via workspace_present, explicit
"do not paste an internal download URL"), so the vendored patch was
RETIRED. This test locks the native contract: success path mentions
workspace_present and contains no internal download URL.

Run: .venv\\Scripts\\python.exe -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def test_execute_uses_present_flow_not_urls(tmp_path: Path, monkeypatch) -> None:
    import deeptutor.services.imagegen as imagegen_service
    from deeptutor.services.path_service import PathService
    from deeptutor.tools.media_gen_tool import ImagegenTool

    async def fake_generate(prompt: str, **kwargs):
        assert prompt.strip()
        return [(b"fakepng-bytes", "image/png")]

    monkeypatch.setattr(imagegen_service, "generate_image", fake_generate)
    monkeypatch.setattr(PathService, "get_public_outputs_root", lambda self: tmp_path)
    workdir = tmp_path / "workspace" / "chat" / "chat" / "t1" / "media"
    workdir.mkdir(parents=True)
    result = asyncio.run(
        ImagegenTool().execute(prompt="blue H spheres", _workspace_dir=str(workdir))
    )
    assert result.success
    assert "workspace_present" in result.content
    assert "/files/outputs/" not in result.content
    names = [a.get("filename", "") for a in result.metadata.get("artifacts", [])]
    assert names and all(n and n in result.content for n in names)


def test_execute_empty_prompt_still_fails_cleanly(tmp_path: Path) -> None:
    from deeptutor.tools.media_gen_tool import ImagegenTool

    result = asyncio.run(ImagegenTool().execute(prompt="  ", _workspace_dir=str(tmp_path)))
    assert not result.success
