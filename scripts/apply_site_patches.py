"""Re-apply the vendored deeptutor==1.6.4 fail-closed patches after a fresh pip install.

Site-packages is NOT in git and every machine pip-installs deeptutor
independently, so these fixes must be re-applied whenever the venv is
rebuilt or deeptutor is upgraded (an upgrade WIPES them — read the patch
files first and check upstream fixed it).

Usage:
  .venv/Scripts/python.exe scripts/apply_site_patches.py [path-to-site-packages]
  python3 scripts/apply_site_patches.py ~/.venv/lib/python3.11/site-packages

Exits non-zero if the stock text is not found (version drift) — never
half-applies. Review artifacts: scripts/patches/*.patch
Regression tests: tests/test_auth_failclosed.py
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import py_compile
import sys
from pathlib import Path

EXPECTED_VERSION = "1.6.7"
# 1.7.0 is a supported target with its own table (EDITS_170). Anything else is
# refused: a table that half-anchors leaves fail-open hooks with nothing behind
# them, which is the failure mode this installer exists to prevent.
SUPPORTED_VERSIONS = ("1.6.7", "1.7.0")

EDIT_IDENTITY_EXC = '''LEGACY_SECRET_FILE = PROJECT_ROOT / "data" / "user" / "auth_secret"


class CorruptUserStoreError(ValueError):'''

EDIT_IDENTITY_EXC_OLD = '''LEGACY_SECRET_FILE = PROJECT_ROOT / "data" / "user" / "auth_secret"'''

EDIT_IDENTITY_EXC_NEW = '''LEGACY_SECRET_FILE = PROJECT_ROOT / "data" / "user" / "auth_secret"


class CorruptUserStoreError(ValueError):
    """The canonical user store exists but cannot be parsed.

    Deliberately loud: a corrupt ``users.json`` must fail closed (login and
    registration refuse) rather than read as "no users", which would let the
    next /register caller promote themselves to admin and let the following
    save permanently overwrite the damaged file.
    """'''

EDIT_READ_JSON_OLD = '''def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}'''

EDIT_READ_JSON_NEW = '''def _read_json(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Read a JSON object from *path*.

    Missing files always read as ``{}`` (fresh boot). When *strict* is true,
    an existing-but-unreadable file raises :class:`CorruptUserStoreError`
    instead of returning ``{}`` — the canonical user store must fail closed.
    Legacy/migration callers keep the tolerant default.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        if strict:
            raise CorruptUserStoreError(f"User store at {path} is unreadable: {exc}") from exc
        logger.warning("Failed to read %s: %s", path, exc)
        return {}
    if not isinstance(loaded, dict):
        if strict:
            raise CorruptUserStoreError(f"User store at {path} is not a JSON object")
        logger.warning("User store at %s is not a JSON object; ignoring", path)
        return {}
    return loaded'''

EDIT_LOAD_USERS_OLD = '''    if USERS_FILE.exists():
        users = _read_json(USERS_FILE)'''

EDIT_LOAD_USERS_NEW = '''    if USERS_FILE.exists():
        # Strict: a damaged canonical store must raise (fail closed), never
        # masquerade as "no users" and open the first-registration admin gate.
        users = _read_json(USERS_FILE, strict=True)'''

EDIT_GRANTS_IMPORT_OLD = '''from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from .identity import get_user_by_id
from .paths import SYSTEM_ROOT, ensure_system_dirs

GRANTS_DIR = SYSTEM_ROOT / "grants"'''

EDIT_GRANTS_IMPORT_NEW = '''from copy import deepcopy
import json
import logging
from pathlib import Path
import re
from typing import Any

from deeptutor.services.file_io import atomic_write_text

from .identity import get_user_by_id
from .paths import SYSTEM_ROOT, ensure_system_dirs

logger = logging.getLogger(__name__)

GRANTS_DIR = SYSTEM_ROOT / "grants"'''

EDIT_GRANT_EXC_OLD = '''_EXTENSION_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")'''

EDIT_GRANT_EXC_NEW = '''_EXTENSION_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class CorruptGrantError(ValueError):
    """A stored grant file exists but cannot be parsed.

    Raised instead of falling back to an empty grant: an empty fallback
    silently revokes everything the student was assigned (KBs, models,
    skill, tools) with no error surfaced anywhere.
    """'''

EDIT_LOAD_GRANT_OLD = '''def load_grant(user_id: str) -> dict[str, Any]:
    path = grant_path(user_id)
    if not path.exists():
        return empty_grant(user_id)
    try:
        return normalize_grant(user_id, json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return empty_grant(user_id)'''

EDIT_LOAD_GRANT_NEW = '''def load_grant(user_id: str) -> dict[str, Any]:
    """Load and normalize the stored grant for *user_id*.

    A missing file means "never assigned" and yields an empty grant. An
    existing-but-unreadable file raises :class:`CorruptGrantError` instead:
    silently serving an empty grant would strip the student's KBs, models
    and skill with no error anywhere, which is worse than a loud failure.
    """
    path = grant_path(user_id)
    if not path.exists():
        return empty_grant(user_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_grant(user_id)
    except Exception as exc:
        logger.error(
            "Stored grant for user %s at %s is unreadable; refusing to serve "
            "an empty grant: %s",
            user_id,
            path,
            exc,
        )
        raise CorruptGrantError(
            f"Stored grant for user {user_id} at {path} is unreadable: {exc}. "
            "Restore it from backup; the file was left untouched."
        ) from exc
    if not isinstance(payload, dict):
        raise CorruptGrantError(
            f"Stored grant for user {user_id} at {path} is not a JSON object. "
            "Restore it from backup; the file was left untouched."
        )
    return normalize_grant(user_id, payload)'''

EDIT_SAVE_GRANT_OLD = '''    path.write_text(json.dumps(grant, indent=2, ensure_ascii=False), encoding="utf-8")'''

EDIT_SAVE_GRANT_NEW = '''    # Atomic: a crash mid-write must never leave a half-written grant behind,
    # which is exactly the corruption load_grant() refuses to serve silently.
    atomic_write_text(path, json.dumps(grant, indent=2, ensure_ascii=False))'''

EDIT_MEDIAGEN_OLD = '''        return _artifact_result(
            artifacts,
            empty_message="Image generation produced no saved files.",
            workspace_id=workspace_id,
            prompt=prompt,
            count=len(artifacts),
            kind="image",
        )'''

EDIT_MEDIAGEN_NEW = '''        result = _artifact_result(
            artifacts,
            empty_message="Image generation produced no saved files.",
            workspace_id=workspace_id,
            prompt=prompt,
            count=len(artifacts),
            kind="image",
        )
        # Partner-chat inline image override (site patch, adapted for 1.6.7):
        # the partner web chat does not render artifact cards and does not
        # linkify plain paths, so 1.6.7's "already presented" guidance leaves
        # the student with no way to open the picture. Append direct URLs and
        # require an inline markdown image instead.
        try:
            rows = [r for r in (result.metadata or {}).get("artifacts", [])
                    if isinstance(r, dict) and r.get("url") and r.get("filename")]
            if rows:
                listing = "\\n".join(
                    f"Direct file URL for {r['filename']}: {r['url']}" for r in rows)
                result = ToolResult(
                    content=(
                        result.content
                        + "\\n\\nPartner-chat surface override: this chat does not "
                        + "render artifact cards.\\n" + listing + "\\n"
                        + "Embed the image in your reply as ![filename](url) on "
                        + "its own line so the student sees it inline."
                    ),
                    sources=result.sources,
                    metadata=result.metadata,
                    success=result.success,
                )
        except Exception:
            logger.debug("imagegen inline override skipped", exc_info=True)
        return result'''

EDIT_PREPLOG_OLD = '''import asyncio
import contextlib
import time'''

EDIT_PREPLOG_NEW = '''import asyncio
import contextlib
import logging
import time'''

EDIT_CONCIERGE_OLD = '''        try:
            from deeptutor.multi_user.learning_access import apply_learning_policy

            payload = apply_learning_policy({**payload, "capability": capability})
        except PermissionError as exc:
            raise RuntimeError(str(exc)) from exc'''

EDIT_CONCIERGE_NEW = '''        try:
            from deeptutor.multi_user.learning_access import apply_learning_policy

            payload = apply_learning_policy({**payload, "capability": capability})
        except PermissionError as exc:
            raise RuntimeError(str(exc)) from exc

        try:
            from deeptutor.multi_user.context import get_current_user_or_none
            from deeptutor.multi_user.grants import load_grant

            # Concierge: students never attach KBs by hand. When a non-admin
            # turn arrives with no KBs attached, mount everything the grant
            # allows. Explicit client attachments always win; any failure
            # leaves the payload untouched (fail open to old behavior).
            if not payload.get("knowledge_bases"):
                user = get_current_user_or_none()
                if user is not None and not user.is_admin:
                    grant = load_grant(user.id)
                    mounted: list[str] = []
                    for item in grant.get("knowledge_bases") or []:
                        if isinstance(item, dict):
                            ref = (item.get("resource_id") or item.get("id") or "").strip()
                        else:
                            ref = str(item or "").strip()
                        if ref and ref not in mounted:
                            mounted.append(ref)
                    if mounted:
                        payload = {**payload, "knowledge_bases": mounted}
        except Exception:
            logging.getLogger(__name__).debug("concierge KB mount skipped", exc_info=True)'''

EDIT_SCOPECLAMP_OLD = '''            if not payload.get("knowledge_bases"):
                user = get_current_user_or_none()
                if user is not None and not user.is_admin:
                    grant = load_grant(user.id)
                    mounted: list[str] = []
                    for item in grant.get("knowledge_bases") or []:
                        if isinstance(item, dict):
                            ref = (item.get("resource_id") or item.get("id") or "").strip()
                        else:
                            ref = str(item or "").strip()
                        if ref and ref not in mounted:
                            mounted.append(ref)
                    if mounted:
                        payload = {**payload, "knowledge_bases": mounted}'''

EDIT_SCOPECLAMP_NEW = '''            user = get_current_user_or_none()
            if user is not None and not user.is_admin:
                grant = load_grant(user.id)
                allowed: set[str] = set()
                order: list[str] = []
                for item in grant.get("knowledge_bases") or []:
                    if isinstance(item, dict):
                        ref = (item.get("resource_id") or item.get("id") or item.get("name") or "").strip()
                    else:
                        ref = str(item or "").strip()
                    if ref and ref not in order:
                        order.append(ref)
                    if ref:
                        allowed.add(ref)
                        if ref.startswith("admin:kb:"):
                            allowed.add(ref[len("admin:kb:"):])
                        else:
                            allowed.add("admin:kb:" + ref)
                if allowed:
                    # Scope clamp: partner turns arrive with the partner's
                    # full library pre-loaded, which used to sail through
                    # and seed Bio KBs into Physics turns. Students only
                    # ever see granted KBs: keep the granted subset, else
                    # fall back to the whole grant as plain names.
                    kept: list[str] = []
                    for kb in payload.get("knowledge_bases") or []:
                        s = str(kb or "").strip()
                        if s and s in allowed and s not in kept:
                            kept.append(s)
                    if kept:
                        payload = {**payload, "knowledge_bases": kept}
                    else:
                        payload = {**payload, "knowledge_bases": [
                            r[len("admin:kb:"):] if r.startswith("admin:kb:") else r
                            for r in order
                        ]}'''

EDIT_P10_OLD = '''        with user_context(partner_user(self.partner_id, name=self.config.name)):
            skills_manifest = self._build_skills_manifest()
            kb_names = self._list_kb_names()'''

EDIT_P10_NEW = '''        with user_context(partner_user(self.partner_id, name=self.config.name)):
            skills_manifest = self._build_skills_manifest()
            kb_names = self._list_kb_names()
        # Scope clamp (site patch): partner turns used to see the partner's
        # whole library (Bio KBs seeded into Physics turns). Clamp to the
        # chatting student's grant. Admin/legacy actors untouched.
        try:
            _actor = getattr(msg, "actor", None)
            _uid = getattr(_actor, "id", None)
            _is_admin = bool(getattr(_actor, "is_admin", False))
            if _uid and not _is_admin:
                from deeptutor.multi_user.grants import load_grant as _load_grant
                _grant = _load_grant(_uid) or {}
                _allowed: set[str] = set()
                for _item in _grant.get("knowledge_bases") or []:
                    if isinstance(_item, dict):
                        _ref = (_item.get("resource_id") or _item.get("id") or _item.get("name") or "").strip()
                    else:
                        _ref = str(_item or "").strip()
                    if _ref:
                        _allowed.add(_ref)
                        if _ref.startswith("admin:kb:"):
                            _allowed.add(_ref[len("admin:kb:"):])
                        else:
                            _allowed.add("admin:kb:" + _ref)
                if _allowed:
                    _kept = [k for k in (kb_names or []) if str(k).strip() in _allowed]
                    if _kept:
                        kb_names = _kept
        except Exception:
            logger.warning("partner KB scope clamp skipped", exc_info=True)'''

EDIT_TOOLS_OLD = '''class LoadToolsTool(_PromptHintsMixin, BaseTool):'''

EDIT_TOOLS_NEW = '''# NCEA deterministic serving (site patch, 2026-09-11): the model must NEVER
# select, compose, or reconstruct exam questions. serve_question picks a
# verified bank entry by code (no LLM involved); log_mistake is the ONLY
# writer of the per-student wrong book. Reviewed standards allowlist is
# intentional — unreviewed banks stay unreachable until a human clears them.

_NCEA_REVIEWED = frozenset({
    "91164", "91165", "91166", "91170", "91171", "91173",
    "91390", "91391", "91392", "91523", "91524", "91526",
    "91577", "91578", "91579", "91584", "91925", "91927",
})

# English is read-with, never served: essay prompts depend on the
# school's chosen texts, grading descriptors are subjective, and there
# are no numeric keys. Even reviewed English banks stay out.
_NCEA_NEVER_SERVE = frozenset({
    "91098", "91099", "91100", "91101", "91102", "91103", "91104",
    "91105", "91106", "91107", "91472", "91473", "91474", "91475",
    "91476", "91477", "91478", "91479", "91480", "91924", "91925",
    "91926", "91927",
})

_NCEA_ENTRY_RE = None


def _ncea_entry_re():
    global _NCEA_ENTRY_RE
    if _NCEA_ENTRY_RE is None:
        import re
        _NCEA_ENTRY_RE = re.compile(r"^##\\s+(\\d{5}-\\d{4}-Q\\S*)")
    return _NCEA_ENTRY_RE


def _ncea_skill_services():
    """Skill services to search for the ncea-tutor package, best first.

    The request-scoped service is rooted at whatever workspace the caller
    is bound to. In a partner turn that is the PARTNER's user
    (``partner_<id>``), which holds no skill grants and usually no skills
    directory at all, so every bank read returned None and the verified
    question bank vanished for the whole turn: no answer key for photo
    grading, no serveable entries, and the JEV gate unable to confirm a
    citation was in-bank. The admin workspace is where the shipped
    ``ncea-tutor`` package (with ``references/qbank_*.md``) actually lives,
    so it is always searched as a fallback - the request-scoped copy still
    wins when it holds the file.
    """
    from deeptutor.services.skill import get_skill_service
    from deeptutor.services.skill.service import SkillNotFoundError
    from deeptutor.multi_user.paths import get_admin_path_service
    services = [get_skill_service()]
    try:
        service_cls = __import__(
            "deeptutor.services.skill.service", fromlist=["SkillService"]
        ).SkillService
        admin_root = (get_admin_path_service().get_workspace_dir() / "skills")
        seen = {str(getattr(services[0], "root", ""))}
        if str(admin_root) not in seen:
            services.append(service_cls(root=admin_root))
    except Exception:
        logger.debug("ncea tools: admin skill scope unavailable", exc_info=True)
    return services, (SkillNotFoundError,)


def _ncea_read_skill_file(rel_path: str):
    services, missing = _ncea_skill_services()
    try:
        from deeptutor.services.skill.service import SkillFileNotFoundError
        missing = missing + (SkillFileNotFoundError,)
    except Exception:
        pass
    for service in services:
        try:
            return service.read_skill_file("ncea-tutor", rel_path)
        except missing:
            # Missing FILE falls through too: a request-scoped skill that
            # carries SKILL.md but not references/qbank_*.md must not shadow
            # the complete admin package.
            continue
    return None


def _ncea_student_root():
    from deeptutor.multi_user.context import get_current_user
    from deeptutor.multi_user.paths import scope_for_user
    user = get_current_user()
    if user is None or user.is_admin:
        return None, None
    return user.id, scope_for_user(user.id, is_admin=False).root


def _ncea_parse_bank(text: str) -> list[dict]:
    entries: list[dict] = []
    rx = _ncea_entry_re()
    cur: dict | None = None
    for line in text.split("\\n"):
        m = rx.match(line.strip())
        if m:
            if cur is not None:
                entries.append(cur)
            cur = {"id": m.group(1), "header": line.strip(), "bullets": {}}
            continue
        if line.strip().startswith("## Omitted"):
            break
        if cur is not None and line.strip().startswith("- "):
            body = line.strip()[2:]
            if ":" in body:
                k, v = body.split(":", 1)
                cur["bullets"][k.strip().lower()] = v.strip()
    if cur is not None:
        entries.append(cur)
    return entries


def _ncea_servable(entries: list[dict]) -> list[dict]:
    out = []
    for e in entries:
        ver = e["bullets"].get("verified", "")
        if "MEDIUM" in ver or "LOW" in ver:
            continue
        out.append(e)
    high = [e for e in out if "HIGH" in e["bullets"].get("verified", "")]
    rest = [e for e in out if e not in high]
    return high + rest


class ServeQuestionTool(_PromptHintsMixin, BaseTool):
    """Serve the next verified NCEA question — deterministic, no composition."""

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="serve_question",
            description=(
                "Serve the next verified NCEA exam question for a standard. "
                "Reads the verified bank, skips entries this student already "
                "did, and returns Location + topic + identifying stem + "
                "verified answer + NZQA PDF link. Call this for EVERY "
                "question request. NEVER compose, reword, retrieve, or "
                "reconstruct a question any other way."
            ),
            parameters=[
                ToolParameter(
                    name="standard",
                    type="string",
                    description="5-digit standard code, e.g. '91579'.",
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        import re
        import time
        code = re.sub(r"\\D", "", str(kwargs.get("standard") or ""))
        if len(code) != 5:
            return ToolResult(
                content="(serve_question error: standard must be a 5-digit code.)",
                success=False,
            )
        if code not in _NCEA_REVIEWED:
            return ToolResult(
                content=(
                    f"(serve_question: standard {code} has no reviewed bank — "
                    f"Rung 3: teach concepts, set no questions.)"
                ),
                success=False,
            )
        uid, root = _ncea_student_root()
        if uid is None:
            return ToolResult(
                content="(serve_question error: no student context.)",
                success=False,
            )
        text = _ncea_read_skill_file(f"references/qbank_{code}.md")
        if text is None:
            return ToolResult(
                content=(
                    f"(serve_question: qbank_{code}.md not found — "
                    f"Rung 3: teach concepts, set no questions.)"
                ),
                success=False,
            )
        servable = _ncea_servable(_ncea_parse_bank(text))
        if not servable:
            return ToolResult(
                content=(
                    f"(serve_question: standard {code} has no servable "
                    f"entries — teach concepts, set no questions.)"
                ),
                success=False,
            )
        done: set[str] = set()
        try:
            served_path = root / "ncea_served.jsonl"
            if served_path.exists():
                import json as _json
                for ln in served_path.read_text(encoding="utf-8").splitlines():
                    try:
                        done.add(_json.loads(ln).get("entry_id", ""))
                    except Exception:
                        continue
        except Exception:
            pass
        pick = next((e for e in servable if e["id"] not in done), None)
        if pick is None:
            # Every verified entry for this standard has already been served.
            # Refusing here is what left a student hunting for their own paper:
            # every bank holds 4-11 entries, so a trial student reaches the end
            # of one in a few sittings. Re-serving the entry served LONGEST ago
            # keeps the anti-fabrication rule intact - it is still a verified
            # bank entry, served verbatim - and it is revision, which is what a
            # student who has done them all needs. Only when the ledger cannot
            # name an entry (no rows at all) does the tool still refuse.
            import json as _json2
            order: list[str] = []
            try:
                _served_path2 = root / "ncea_served.jsonl"
                if _served_path2.exists():
                    for ln in _served_path2.read_text(encoding="utf-8").splitlines():
                        try:
                            eid = _json2.loads(ln).get("entry_id", "")
                        except Exception:
                            continue
                        if eid and eid not in order:
                            order.append(eid)
            except Exception:
                order = []
            by_id = {e["id"]: e for e in servable}
            pick = next((by_id[i] for i in order if i in by_id), None)
        if pick is None:
            return ToolResult(
                content=(
                    f"(serve_question: standard {code} bank exhausted — "
                    f"drill the mistake list instead of new questions.)"
                ),
                success=False,
            )
        loc = pick["bullets"].get("location", "")
        # The year must stand alone: a bare (19|20) scan matches inside
        # the standard code itself for 92xxx / 9194x standards
        # ("92047-exm-2024.pdf" -> "2047"), which silently dropped the
        # PDF link. Require the year not to be preceded by a digit.
        ym = (re.search(r"(?<![0-9])(19|20)[0-9]{2}", loc)
              or re.search(r"(?<![0-9])(19|20)[0-9]{2}", pick["header"]))
        pdf = ""
        try:
            links = _ncea_read_skill_file("references/pdf_links.md") or ""
            in_sec = False
            for ln in links.split("\\n"):
                if ln.strip() == f"## {code}":
                    in_sec = True
                    continue
                if ln.strip().startswith("## "):
                    in_sec = False
                if in_sec and ym:
                    mm = re.match(r"\\s*-\\s*(\\d{4})\\s*:\\s*(\\S+)", ln)
                    if mm and mm.group(1) == ym.group(0):
                        pdf = mm.group(2)
                        break
        except Exception:
            pdf = ""
        try:
            import json as _json
            with open(root / "ncea_served.jsonl", "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(
                    {"ts": int(time.time()), "entry_id": pick["id"]},
                    ensure_ascii=False) + "\\n")
        except Exception:
            pass
        stem = pick["bullets"].get("identifying stem", "")
        topic = pick["bullets"].get("topic", "")
        answer = pick["bullets"].get("answer", "")
        return ToolResult(
            content=(
                f"SERVED {pick['id']}\\n"
                f"Location: {loc}\\n"
                f"Topic: {topic}\\n"
                f"Stem (confirm identity, max 2 lines): {stem}\\n"
                f"PDF: {pdf or '(link not on file — ask student to open NZQA paper)'}\\n"
                f"Answer key (for grading only, NEVER show first): {answer}\\n"
                f"RULE: send Location + PDF link only. White paper. No pasted text."
            ),
            metadata={"entry_id": pick["id"], "standard": code},
        )


class LogMistakeTool(_PromptHintsMixin, BaseTool):
    """The ONLY writer of the per-student wrong book. Refuses fake entries."""

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="log_mistake",
            description=(
                "Record a marking result in the student's wrong book "
                "(per-student file, never shared). action='log' files a "
                "below-target result; 'resolve' marks it corrected; 'list' "
                "returns the Wrong Only list. Call 'log' for EVERY "
                "below-target photo grade, 'list' at each session start. "
                "entry_id must be a real bank id like '91579-2025-Q1d' — "
                "anything else is refused."
            ),
            parameters=[
                ToolParameter(
                    name="action",
                    type="string",
                    description="'log', 'resolve', or 'list'.",
                ),
                ToolParameter(
                    name="entry_id",
                    type="string",
                    description="Bank entry id, e.g. '91579-2025-Q1d'.",
                    required=False,
                ),
                ToolParameter(
                    name="result",
                    type="string",
                    description="'merit' | 'achieved' | 'not-achieved' (below target).",
                    required=False,
                ),
                ToolParameter(
                    name="gap",
                    type="string",
                    description="One line: what missed Excellence.",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        import json as _json
        import re
        import time
        uid, root = _ncea_student_root()
        if uid is None:
            return ToolResult(
                content="(log_mistake error: no student context.)",
                success=False,
            )
        action = str(kwargs.get("action") or "log").strip().lower()
        path = root / "ncea_mistakes.jsonl"
        if action == "list":
            return ToolResult(content=self._render(path), success=True)
        entry_id = str(kwargs.get("entry_id") or "").strip()
        if not re.match(r"^\\d{5}-\\d{4}-Q", entry_id):
            return ToolResult(
                content=(
                    f"(log_mistake refused: {entry_id!r} is not a real bank "
                    f"entry id (want like '91579-2025-Q1d'). Composed "
                    f"questions never enter the book.)"
                ),
                success=False,
            )
        if action == "log":
            result = str(kwargs.get("result") or "").strip() or "below-target"
            gap = str(kwargs.get("gap") or "").strip() or "(no gap noted)"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(
                    {"ts": int(time.time()), "type": "mistake",
                     "entry_id": entry_id, "result": result, "gap": gap},
                    ensure_ascii=False) + "\\n")
            return ToolResult(
                content=f"LOGGED {entry_id} ({result}): {gap}\\n" + self._render(path),
                success=True,
            )
        if action == "resolve":
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(
                    {"ts": int(time.time()), "type": "resolve",
                     "entry_id": entry_id},
                    ensure_ascii=False) + "\\n")
            return ToolResult(
                content=f"RESOLVED {entry_id}\\n" + self._render(path),
                success=True,
            )
        return ToolResult(
            content="(log_mistake error: action must be log, resolve, or list.)",
            success=False,
        )

    @staticmethod
    def _render(path) -> str:
        import json as _json
        state: dict[str, dict] = {}
        try:
            if path.exists():
                for ln in path.read_text(encoding="utf-8").splitlines():
                    try:
                        r = _json.loads(ln)
                    except Exception:
                        continue
                    eid = r.get("entry_id", "")
                    if not eid:
                        continue
                    if r.get("type") == "resolve":
                        state.pop(eid, None)
                    else:
                        state[eid] = r
        except Exception:
            pass
        if not state:
            return "Wrong Only: 0 — clean."
        lines = [f"Wrong Only: {len(state)}"]
        for eid, r in state.items():
            lines.append(f"- {eid} ({r.get('result', '?')}): {r.get('gap', '')}")
        return "\\n".join(lines)


class LoadToolsTool(_PromptHintsMixin, BaseTool):'''

EDIT_TUPLE_OLD = '''    "mastery_new_session",
)'''

EDIT_TUPLE_NEW = '''    "mastery_new_session",
    "serve_question",
    "log_mistake",
)'''

EDIT_SPECS_OLD = '''            ("read_skill", "ReadSkillTool"),'''

EDIT_SPECS_NEW = '''            ("read_skill", "ReadSkillTool"),
            ("serve_question", "ServeQuestionTool"),
            ("log_mistake", "LogMistakeTool"),'''

# (1.6.7 upstream fixed the missing-file-vs-missing-skill ambiguity via
# SkillFileNotFoundError — our former EDIT_READS edit is retired.)

EDIT_S11B_OLD = '''def _ncea_parse_bank(text: str) -> list[dict]:
    entries: list[dict] = []
    rx = _ncea_entry_re()
    cur: dict | None = None
    for line in text.split("\\n"):
        m = rx.match(line.strip())
        if m:
            if cur is not None:
                entries.append(cur)
            cur = {"id": m.group(1), "header": line.strip(), "bullets": {}}
            continue
        if line.strip().startswith("## Omitted"):
            break
        if cur is not None and line.strip().startswith("- "):
            body = line.strip()[2:]
            if ":" in body:
                k, v = body.split(":", 1)
                cur["bullets"][k.strip().lower()] = v.strip()
    if cur is not None:
        entries.append(cur)
    return entries


def _ncea_servable(entries: list[dict]) -> list[dict]:
    out = []
    for e in entries:
        ver = e["bullets"].get("verified", "")
        if "MEDIUM" in ver or "LOW" in ver:
            continue
        out.append(e)
    high = [e for e in out if "HIGH" in e["bullets"].get("verified", "")]
    rest = [e for e in out if e not in high]
    return high + rest'''

EDIT_S11B_NEW = '''def _ncea_parse_bank(text: str) -> list[dict]:
    entries: list[dict] = []
    rx = _ncea_entry_re()
    cur: dict | None = None
    for line in text.split("\\n"):
        m = rx.match(line.strip())
        if m:
            if cur is not None:
                entries.append(cur)
            cur = {"id": m.group(1), "header": line.strip(),
                   "bullets": {}, "raw": line.strip() + "\\n"}
            continue
        if line.strip().startswith("## Omitted"):
            break
        if cur is None:
            continue
        cur["raw"] += line + "\\n"
        if line.strip().startswith("- "):
            body = line.strip()[2:]
            if ":" in body:
                k, v = body.split(":", 1)
                cur["bullets"][k.strip().lower()] = v.strip()
    if cur is not None:
        entries.append(cur)
    return entries


def _ncea_servable(entries: list[dict]) -> list[dict]:
    # Grade on the RAW entry text: MEDIUM/LOW flags live on continuation
    # lines, not just the Verified bullet (proven: 91171-2022-Q2c,
    # 91579-2023-Q1a). Ungraded re-done entries serve after HIGH ones.
    out = []
    for e in entries:
        blob = e.get("raw", "")
        if "MEDIUM" in blob or "LOW" in blob:
            continue
        out.append(e)
    high = [e for e in out if "HIGH" in e.get("raw", "")]
    rest = [e for e in out if e["id"] not in {h["id"] for h in high}]
    return high + rest'''


INSTALL_SERVEBLOCK = (
    "ncea_serve_block.py",
    "deeptutor/agents/_shared/ncea_serve_block.py",
)

INSTALL_AUTOLOG = (
    "ncea_autolog.py",
    "deeptutor/agents/_shared/ncea_autolog.py",
)

INSTALL_REVIEW = (
    "ncea_review_block.py",
    "deeptutor/agents/_shared/ncea_review_block.py",
)

INSTALL_GRADE = (
    "ncea_grade_block.py",
    "deeptutor/agents/_shared/ncea_grade_block.py",
)

INSTALL_GUARD = (
    "ncea_output_guard.py",
    "deeptutor/agents/_shared/ncea_output_guard.py",
)

# JEV semantic gate: the layer ABOVE the deterministic guards. Shadow by
# default (decides + audits, never rewrites); needs TYPESAFE_API_KEY in the
# service environment, and without it the helper no-ops entirely.
INSTALL_JEVGATE = (
    "ncea_jev_gate.py",
    "deeptutor/agents/_shared/ncea_jev_gate.py",
)

# Public demo: Google sign-in + the 15-day trial ledger and its new-turn gate.
INSTALL_DEMO = (
    "ncea_demo.py",
    "deeptutor/api/routers/ncea_demo.py",
)

# Question page image: fetch the official exam PDF, render the page the
# question starts on, present it through workspace_present so the chat shows
# the question WITH its diagram instead of text + a link the student may not
# be able to open (Mark, 2026-10-31: "I don't have the paper").
INSTALL_QIMAGE = (
    "ncea_question_image.py",
    "deeptutor/agents/_shared/ncea_question_image.py",
)

# One live turn per account. Every turn is paid for by ONE shared provider key,
# so two windows are two concurrent agent loops - twice the tokens, and the
# student reads whichever answer finished first. Keyed by account, so the
# socket, /chat and the stream endpoint all pass through the same slot.
INSTALL_TURNLIMIT = (
    "ncea_turn_limit.py",
    "deeptutor/multi_user/ncea_turn_limit.py",
)

# A student's own provider key, written to their OWN catalog (never the shared
# one) and used for their turns. Needs the page at /my-key to be usable at all.
INSTALL_OWNKEY = (
    "ncea_personal_key.py",
    "deeptutor/api/routers/ncea_personal_key.py",
)

# Where a student's own key is actually INSTALLED for the turn. The router
# above stores it; this is what makes the stored profile the model the turn
# runs on (a saved key that silently kept using the shared model was the
# 2026-11-03 AU finding).
INSTALL_OWNKEYSCOPE = (
    "ncea_own_key.py",
    "deeptutor/multi_user/ncea_own_key.py",
)

# Fingerprints of the helper sources this repo is meant to install.
#
# Why this exists (2026-11-03, AU production): the installer TRUSTS scripts/ -
# it copies whatever is sitting there. AU's scripts/ had drifted behind the
# repo, so a deploy "succeeded" and quietly installed an OLDER grade block over
# the newer one, removing the "please mark it" grading fix. Nothing failed,
# because from the installer's point of view the source was the truth.
#
# helpers.sha256 makes the repo, not the directory, the reference: a source that
# does not match it aborts the run before anything is written, and --verify
# reports it afterwards. Regenerate with --write-hashes when you mean to change
# a helper, and commit the manifest with it.
MANIFEST_NAME = "helpers.sha256"


def _helper_text(path: Path) -> str:
    """Helper source as the installer compares it: newlines normalised.

    A Windows checkout reads back CRLF while the installed file is LF. Both
    read_text() calls below are universal-newline, so hashing the normalised
    text keeps a line-ending difference from looking like a content difference.
    """
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _helper_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_helper_manifest() -> int:
    """Record the current helper sources in helpers.sha256."""
    lines = [
        f"{_helper_hash(_helper_text(_scripts_dir() / src))}  {src}"
        for src, _ in HELPERS
    ]
    (_scripts_dir() / MANIFEST_NAME).write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(f"wrote scripts/{MANIFEST_NAME}: {len(lines)} helper source(s)")
    return 0


def source_drift() -> list[str]:
    """Helper sources that are not the version committed in helpers.sha256.

    Empty when there is no manifest: a portable copy of these scripts has
    nothing to compare against, and refusing to run there would be worse than
    the risk. On a checkout that has one, drift is an error - see MANIFEST_NAME.
    """
    path = _scripts_dir() / MANIFEST_NAME
    if not path.is_file():
        return []
    wanted: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, name = line.split(None, 1)
            wanted[name.strip()] = digest
    except Exception:  # noqa: BLE001 - a corrupt manifest must be loud, not fatal
        return [f"scripts/{MANIFEST_NAME}: unreadable (regenerate with --write-hashes)"]
    problems = []
    for src, _ in HELPERS:
        source = _scripts_dir() / src
        if not source.is_file():
            continue  # preflight_helpers reports the missing source, not this
        want = wanted.get(src)
        if want is None:
            problems.append(
                f"scripts/{src}: not listed in {MANIFEST_NAME} (run --write-hashes)"
            )
        elif _helper_hash(_helper_text(source)) != want:
            problems.append(
                f"scripts/{src}: NOT THE COMMITTED VERSION "
                f"({MANIFEST_NAME} disagrees with the file)"
            )
    return problems


# Every helper that must exist in scripts/ AND be installed into the venv.
# A helper missing from scripts/ cannot be installed, and a helper added to
# an INSTALL_* tuple but forgotten here would escape the preflight/verify
# below - so keep this list in step with the tuples (a test asserts it).
HELPERS = (
    INSTALL_SERVEBLOCK,
    INSTALL_AUTOLOG,
    INSTALL_REVIEW,
    INSTALL_GRADE,
    INSTALL_GUARD,
    INSTALL_JEVGATE,
    INSTALL_DEMO,
    INSTALL_QIMAGE,
    INSTALL_TURNLIMIT,
    INSTALL_OWNKEY,
    INSTALL_OWNKEYSCOPE,
)

# --- one live turn per account, and a student's own key (2026-11-02) -------
# One shared provider key pays for every turn, so two browser windows are two
# concurrent agent loops. The per-session lock cannot stop that: a second
# window opens a DIFFERENT session key, and the socket starts its turn on the
# partner instance instead of inside the socket. The guard is keyed by account
# id and wrapped around the one place every door runs a turn.
# Anchor on an import that is identical in the PyPI wheel and in a ported
# 1.7.0 tree. The paths import line is NOT safe: the wheel imports
# ``get_current_path_service, user_context`` while a ported tree imports
# ``get_path_service_for_scope``, and anchoring there aborted the AU deploy
# (2026-11-02) with "stock text not found" while every other edit was fine.
EDIT_TURNLIMIT_IMPORT_OLD = '''from deeptutor.core.stream import StreamEvent, StreamEventType'''

EDIT_TURNLIMIT_IMPORT_NEW = '''from deeptutor.core.stream import StreamEvent, StreamEventType

# Site patch: one live turn per account (single-window rule). Fail-open - a
# tree patched before this helper shipped keeps the old, unpaced behaviour
# instead of failing to import the partner runtime.
try:
    from deeptutor.multi_user.ncea_turn_limit import single_turn as _ncea_single_turn
except Exception:  # pragma: no cover
    import contextlib as _ncea_contextlib

    @_ncea_contextlib.asynccontextmanager
    async def _ncea_single_turn(_actor=None):
        yield None'''

EDIT_TURNLIMIT_OLD = '''        async with self._lock_for(session_key, actor_id=personal_actor_id(msg.actor)):'''

EDIT_TURNLIMIT_NEW = '''        # Single-window rule (site patch): a second concurrent turn for this
        # account - another tab, a second device, a message from the phone -
        # is refused with a message telling the student what to do, rather
        # than queued behind this turn (a queue reads as a hang).
        async with self._lock_for(
            session_key, actor_id=personal_actor_id(msg.actor)
        ), _ncea_single_turn(msg.actor):'''

# A student who saved their own key runs on it without touching a picker: the
# personal row is their money, an admin grant is ours, and this is the only
# place a non-admin turn is pinned to a concrete model.
EDIT_OWNKEY_PREF_OLD = '''                llm_selection = {
                    "profile_id": assigned_llms[0].get("profile_id"),
                    "model_id": assigned_llms[0].get("model_id"),
                }'''

EDIT_OWNKEY_PREF_NEW = '''                # Prefer the student's OWN provider key: it is their account
                # paying, and saving a key with no further step should be
                # enough to start using it.
                assigned_llms.sort(
                    key=lambda item: 0 if item.get("source") == "personal" else 1
                )
                llm_selection = {
                    "profile_id": assigned_llms[0].get("profile_id"),
                    "model_id": assigned_llms[0].get("model_id"),
                }'''

# The own-key router is mounted next to the public demo router (no prefix: its
# routes carry absolute paths, and /my-key must be reachable without a token so
# the page can tell a signed-out visitor to sign in). Authentication is
# enforced per handler through get_current_user().
EDIT_OWNKEY_IMPORT_OLD = '''from deeptutor.api.routers import ncea_demo  # noqa: E402'''

EDIT_OWNKEY_IMPORT_NEW = '''from deeptutor.api.routers import ncea_demo  # noqa: E402
from deeptutor.api.routers import ncea_personal_key  # noqa: E402  (own API key)'''

EDIT_OWNKEY_MOUNT_OLD = '''app.include_router(ncea_demo.router)'''

EDIT_OWNKEY_MOUNT_NEW = '''app.include_router(ncea_demo.router)
# A student's own provider key: /api/ncea/personal-key and the /my-key page.
app.include_router(ncea_personal_key.router)'''

# The own-key pin in the preparer is too late: the partner runtime installs the
# turn's scoped LLM config before the preparer runs, from the deployment
# catalog. So a saved key was stored, preferred, and never used. Resolve the
# owner's own profile here instead - by explicit owner id, because a partner
# turn runs as a synthetic user and has no request context to look it up in.
EDIT_OWNKEY_SCOPE_IMPORT_OLD = '''    async def _ncea_single_turn(_actor=None):
        yield None'''

EDIT_OWNKEY_SCOPE_IMPORT_NEW = '''    async def _ncea_single_turn(_actor=None):
        yield None

# Site patch: a student's own provider key runs their turn. Kept out of the
# block above on purpose - a broken own-key helper must not also switch off the
# single-window rule. Each fails open on its own.
try:
    from deeptutor.multi_user.ncea_own_key import own_key_scope as _ncea_own_key_scope
except Exception:  # pragma: no cover
    def _ncea_own_key_scope(_owner_id="", _pinned=None):
        return None, None'''

EDIT_OWNKEY_SCOPE_OLD = '''            _config, llm_token = activate_llm_selection(selection)'''

EDIT_OWNKEY_SCOPE_NEW = '''            # The student's own provider key, if they saved one: they are the
            # one paying, and saving it is meant to be the whole setup. A
            # partner's pinned model still wins - that was a deliberate choice.
            _ncea_own_cfg, _ncea_own_tok = _ncea_own_key_scope(
                getattr(self.config, "owner_id", ""), selection
            )
            if _ncea_own_cfg is not None:
                _config, llm_token = _ncea_own_cfg, _ncea_own_tok
            else:
                _config, llm_token = activate_llm_selection(selection)'''


EDIT_REVIEWPIPE_OLD = '''    # ---- KB seed ----'''

EDIT_REVIEWPIPE_NEW = '''    async def _retrieve_ncea_review_block(self, context: UnifiedContext) -> str:
        """Deterministic wrong-book read (site patch; mirrors the serve
        block so listing mistakes needs no model initiative)."""
        from deeptutor.agents._shared.ncea_review_block import build_review_block

        uid = str((context.metadata or {}).get("_ncea_student_uid") or "")
        if not uid:
            return ""
        try:
            return await build_review_block(context.user_message or "", uid)
        except Exception:
            logger.warning("ncea review block failed", exc_info=True)
            return ""

    # ---- KB seed ----'''

EDIT_GRADEPIPE_OLD = '''    # ---- KB seed ----'''

EDIT_GRADEPIPE_NEW = '''    async def _retrieve_ncea_grade_block(self, context: UnifiedContext) -> str:
        """Deterministic grading-key injection (site patch; photo-grading
        turns in fresh sessions otherwise recall a different answer - the
        27/13.5/3.65 spiral)."""
        from deeptutor.agents._shared.ncea_grade_block import build_grade_block

        uid = str((context.metadata or {}).get("_ncea_student_uid") or "")
        if not uid:
            return ""
        try:
            has_photo = bool(getattr(context, "attachments", None))
            return await build_grade_block(context.user_message or "", uid, has_photo)
        except Exception:
            logger.warning("ncea grade block failed", exc_info=True)
            return ""

    # ---- KB seed ----'''

EDIT_JOIN2_OLD = '''            ncea_review_block = await self.pipeline._retrieve_ncea_review_block(self.context)'''

EDIT_ALLOWY11_OLD = '''    "91577", "91578", "91579", "91584", "91925", "91927",
})'''

EDIT_ALLOWY11_NEW = '''    "91577", "91578", "91579", "91584", "91925", "91927",
    "92022", "92023", "92046", "92047", "91946", "91947",
})'''

EDIT_ALLOWM2_OLD = '''    "92022", "92023", "92046", "92047", "91946", "91947",
})'''

EDIT_ALLOWM2_NEW = '''    "92022", "92023", "92046", "92047", "91946", "91947",
    "91261", "91262", "91267",
})'''

EDIT_ALLOWBIO_OLD = '''    "92022", "92023", "92046", "92047", "91946", "91947",
    "91261", "91262", "91267",
})'''

EDIT_ALLOWBIO_NEW = '''    "92022", "92023", "92046", "92047", "91946", "91947",
    "91261", "91262", "91267",
    "91156", "91157", "91159", "91603", "91605", "91606",
})'''

# 91586 (L3 Probability Distributions) has a 10-entry verified bank and is in the
# skill's KB catalog, but was never added to the reviewed set - serve_question
# refused it, so the tutor told students it had no questions for a standard it
# held ten of. Found by scripts/qbank_coverage.py on 2026-11-03.
EDIT_ALLOWSTAT_OLD = '''    "91156", "91157", "91159", "91603", "91605", "91606",
})'''

EDIT_ALLOWSTAT_NEW = '''    "91156", "91157", "91159", "91603", "91605", "91606",
    "91586",
})'''

EDIT_QBLIT_OLD = '''AssessmentSource = Literal["deep_question", "mastery_path", "immersive_reading", "book"]'''

EDIT_QBLIT_NEW = '''AssessmentSource = Literal["deep_question", "mastery_path", "immersive_reading", "book", "photo-grade"]'''

EDIT_QBPAT_OLD = '''        default="", pattern="^(deep_question|mastery_path|immersive_reading|book)?$"'''

EDIT_QBPAT_NEW = '''        default="", pattern="^(deep_question|mastery_path|immersive_reading|book|photo-grade)?$"'''

EDIT_QBSET_OLD = '''ASSESSMENT_SOURCES = frozenset({"deep_question", "mastery_path", "immersive_reading", "book"})'''

EDIT_QBSET_NEW = '''ASSESSMENT_SOURCES = frozenset({"deep_question", "mastery_path", "immersive_reading", "book", "photo-grade"})'''

# Written against the allowlist state AFTER the ALLOWY11/M2/BIO edits
# (they run earlier in EDITS). Production has the marker -> SKIP either way.
EDIT_NEVERSET_OLD = '''    "91577", "91578", "91579", "91584", "91925", "91927",
    "92022", "92023", "92046", "92047", "91946", "91947",
    "91261", "91262", "91267",
    "91156", "91157", "91159", "91603", "91605", "91606",
    "91586",
})

_NCEA_ENTRY_RE = None'''

EDIT_NEVERSET_NEW = '''    "91577", "91578", "91579", "91584", "91925", "91927",
})

# English is read-with, never served: essay prompts depend on the
# school's chosen texts, grading descriptors are subjective, and there
# are no numeric keys. Even reviewed English banks stay out.
_NCEA_NEVER_SERVE = frozenset({
    "91098", "91099", "91100", "91101", "91102", "91103", "91104",
    "91105", "91106", "91107", "91472", "91473", "91474", "91475",
    "91476", "91477", "91478", "91479", "91480", "91924", "91925",
    "91926", "91927",
})

_NCEA_ENTRY_RE = None'''

EDIT_NEVERCHECK_OLD = '''        if code not in _NCEA_REVIEWED:'''

EDIT_NEVERCHECK_NEW = '''        if code in _NCEA_NEVER_SERVE or code not in _NCEA_REVIEWED:'''

# Trial expiry auto-disable (owner decision 2026-09-16: silent close, no
# reminders). 1.6.7 carries a ``disabled`` flag on user records but never
# enforces it — login still succeeds with disabled=true. Two edits make the
# flag real: refuse at password check (authenticate) and refuse at token
# validation (decode_token), so pre-existing cookies die at the next request
# too. Both copies of authenticate() (services + identity) get the check.
EDIT_AUTHDISABLE_LOGIN_OLD = '''    record = users.get(username)
    if not record:
        return None

    hashed = record.get("hash", "") if isinstance(record, dict) else record
    if not verify_password(password, hashed):
        return None'''

EDIT_AUTHDISABLE_LOGIN_NEW = '''    record = users.get(username)
    if not record:
        return None

    if isinstance(record, dict) and record.get("disabled"):
        logger.info("Login refused for disabled account '%s'", username)
        return None

    hashed = record.get("hash", "") if isinstance(record, dict) else record
    if not verify_password(password, hashed):
        return None'''

EDIT_AUTHDISABLE_LOGIN2_OLD = EDIT_AUTHDISABLE_LOGIN_OLD
EDIT_AUTHDISABLE_LOGIN2_NEW = EDIT_AUTHDISABLE_LOGIN_NEW

EDIT_AUTHDISABLE_TOKEN_OLD = '''        username = payload.get("sub")
        if not username:
            return None
        user_id = str(payload.get("uid") or "")
        if not user_id:
            record = _load_users().get(str(username)) or {}
            user_id = str(record.get("id") or "")'''

EDIT_AUTHDISABLE_TOKEN_NEW = '''        username = payload.get("sub")
        if not username:
            return None
        record = _load_users().get(str(username)) or {}
        if isinstance(record, dict) and record.get("disabled"):
            return None  # disabled account: live cookies die at next request
        user_id = str(payload.get("uid") or "")
        if not user_id:
            user_id = str(record.get("id") or "")'''

EDIT_JOIN2_NEW = '''            ncea_review_block = await self.pipeline._retrieve_ncea_review_block(self.context)
            ncea_grade_block = await self.pipeline._retrieve_ncea_grade_block(self.context)'''
EDIT_JOIN_OLD = '''            ncea_serve_block = await self.pipeline._retrieve_ncea_serve_block(self.context)
            seed_block = "\\n\\n".join(
                block
                for block in (
                    ncea_serve_block.strip(),'''

EDIT_JOIN_NEW = '''            ncea_serve_block = await self.pipeline._retrieve_ncea_serve_block(self.context)
            ncea_review_block = await self.pipeline._retrieve_ncea_review_block(self.context)
            seed_block = "\\n\\n".join(
                block
                for block in (
                    ncea_serve_block.strip(),
                    ncea_review_block.strip(),'''

EDIT_AUTOLOG_OLD = '''                if final:
                    store.append(
                        session_key,
                        "assistant",
                        final,
                        channel=msg.channel,
                        metadata=activity_meta,
                        events=turn_events or None,
                    )
            return final'''

EDIT_AUTOLOG2_OLD = '''                        from deeptutor.agents._shared.ncea_autolog import maybe_autolog
                        maybe_autolog(msg=msg, final=final, turn_events=turn_events)'''

EDIT_AUTOLOG2_NEW = '''                        from deeptutor.agents._shared.ncea_autolog import maybe_autolog
                        await maybe_autolog(msg=msg, final=final, turn_events=turn_events)'''

EDIT_AUTOLOG_NEW = '''                if final:
                    store.append(
                        session_key,
                        "assistant",
                        final,
                        channel=msg.channel,
                        metadata=activity_meta,
                        events=turn_events or None,
                    )
                    # NCEA post-turn auto-log (site patch): photo-grading
                    # turns file the per-student wrong book deterministically
                    # (the model never calls log_mistake on its own).
                    try:
                        from deeptutor.agents._shared.ncea_autolog import maybe_autolog
                        await maybe_autolog(msg=msg, final=final, turn_events=turn_events)
                    except Exception:
                        logger.warning("ncea autolog skipped", exc_info=True)
            return final'''

# v2 output guard: runs AFTER autolog in the same post-turn slot, so the
# verdict gate still sees the raw final. Rewrites (not drops) the reply
# when it presents a question entry never served/graded to this student.
# OLD anchors on the applied autolog block (present on prod + fresh venv
# after the AUTOLOG edit above); marker "maybe_guard_output" makes it SKIP
# on replay. History keeps the raw text for audit; the user gets the safe one.
EDIT_GUARD_OLD = '''                    try:
                        from deeptutor.agents._shared.ncea_autolog import maybe_autolog
                        await maybe_autolog(msg=msg, final=final, turn_events=turn_events)
                    except Exception:
                        logger.warning("ncea autolog skipped", exc_info=True)
            return final'''

EDIT_GUARD_NEW = '''                    try:
                        from deeptutor.agents._shared.ncea_autolog import maybe_autolog
                        await maybe_autolog(msg=msg, final=final, turn_events=turn_events)
                    except Exception:
                        logger.warning("ncea autolog skipped", exc_info=True)
                    # NCEA output guard v2 (site patch): refuse to send a
                    # question citation never served/graded to this student
                    # (anti-fabrication). Fail-open: helper returns "" to keep
                    # the original final on any doubt or error.
                    try:
                        from deeptutor.agents._shared.ncea_output_guard import maybe_guard_output
                        guarded = await maybe_guard_output(msg=msg, final=final)
                        if guarded:
                            final = guarded
                    except Exception:
                        logger.warning("ncea output guard skipped", exc_info=True)
            return final'''

EDIT_JEVGATE_OLD = '''                    try:
                        from deeptutor.agents._shared.ncea_output_guard import maybe_guard_output
                        guarded = await maybe_guard_output(msg=msg, final=final)
                        if guarded:
                            final = guarded
                    except Exception:
                        logger.warning("ncea output guard skipped", exc_info=True)
            return final'''

EDIT_JEVGATE_NEW = '''                    try:
                        from deeptutor.agents._shared.ncea_output_guard import maybe_guard_output
                        guarded = await maybe_guard_output(msg=msg, final=final)
                        if guarded:
                            final = guarded
                    except Exception:
                        logger.warning("ncea output guard skipped", exc_info=True)
                    # NCEA JEV semantic gate (site patch): the layer ABOVE the
                    # deterministic guards. Catches structure-shaped
                    # hallucination that carries NO verifiable id - a recalled
                    # answer key, a self-composed question, a wrong paper
                    # attribution. SHADOW by default: it decides and writes
                    # ncea_jev_gate.jsonl but never rewrites the reply, so the
                    # false-positive rate can be measured before enforcement.
                    # Fail-open, and a no-op without TYPESAFE_API_KEY. Evidence
                    # is rebuilt READ-ONLY from the per-student ledgers; the
                    # helper accepts the turn's kwarg-free (msg, final) shape.
                    try:
                        from deeptutor.agents._shared.ncea_jev_gate import maybe_jev_guard
                        jev = await maybe_jev_guard(msg=msg, final=final)
                        if jev:
                            final = jev
                    except Exception:
                        logger.warning("ncea jev gate skipped", exc_info=True)
            return final'''

EDIT_SERVEBLOCK_OLD = '''    async def _retrieve_kb_seed_block(
'''

EDIT_SERVEBLOCK_NEW = '''    async def _retrieve_ncea_serve_block(self, context: UnifiedContext) -> str:
        """Deterministic question serving (site patch, helpers live in
        deeptutor/agents/_shared/ncea_serve_block.py - installed by this
        script; logic there, not here, to keep anchors tiny)."""
        from deeptutor.agents._shared.ncea_serve_block import build_serve_block

        uid = str((context.metadata or {}).get("_ncea_student_uid") or "")
        if not uid:
            return ""
        try:
            return await build_serve_block(context.user_message or "", uid)
        except Exception:
            logger.warning("ncea serve block failed", exc_info=True)
            return ""

    async def _retrieve_kb_seed_block(
'''

EDIT_LOOPJOIN_OLD = r'''            seed_block = "\n\n".join(
                block
                for block in (
                    seed_block.strip(),
                    capability_seed.strip(),
                    capability_briefing.strip(),
                )
                if block
            )'''

EDIT_LOOPJOIN_NEW = r'''            ncea_serve_block = await self.pipeline._retrieve_ncea_serve_block(self.context)
            seed_block = "\n\n".join(
                block
                for block in (
                    ncea_serve_block.strip(),
                    seed_block.strip(),
                    capability_seed.strip(),
                    capability_briefing.strip(),
                )
                if block
            )'''

# The first uid-stamp attempt inserted the stamp BEFORE the metadata dict was
# built, so it raised NameError, the except swallowed it, and the serve block
# silently no-op'd. REPAIR deletes that misplaced block (idempotent: its NEW
# text is the stock text, used here as the marker), then EDIT_UIDSTAMP places a
# self-contained stamp AFTER the dict exists.

EDIT_UIDSTAMP_REPAIR_OLD = '''            logger.warning("partner KB scope clamp skipped", exc_info=True)

        # NCEA deterministic serving (site patch): stamp the real chatting
        # student's id so the loop's serve-block injection can resolve grants
        # and the per-student served ledger. Leading underscore keeps it out
        # of channel_metadata (same convention as _attachment_records).
        try:
            if _uid and not _is_admin:
                metadata["_ncea_student_uid"] = _uid
        except Exception:
            logger.debug("ncea uid stamp skipped", exc_info=True)

        metadata: dict[str, Any] = {
'''

EDIT_UIDSTAMP_REPAIR_NEW = '''            logger.warning("partner KB scope clamp skipped", exc_info=True)

        metadata: dict[str, Any] = {
'''

EDIT_UIDSTAMP_OLD = '''            # NOTE: no ``wait_for_user_reply`` — an ask_user pause makes
            # the pending question the turn's reply (IM semantics).
        }
        channel_meta: dict[str, Any] = {}
'''

EDIT_UIDSTAMP_NEW = '''            # NOTE: no ``wait_for_user_reply`` — an ask_user pause makes
            # the pending question the turn's reply (IM semantics).
        }
        # NCEA deterministic serving (site patch): stamp the real chatting
        # student's id so the loop's serve-block injection can resolve grants
        # and the per-student served ledger. Deliberately placed AFTER the
        # metadata dict above — it must already exist — and self-contained
        # (no names borrowed from the clamp block), because a NameError here
        # is swallowed by the except and fails silently.
        try:
            _ncea_actor = getattr(msg, "actor", None)
            _ncea_uid = getattr(_ncea_actor, "id", None)
            if _ncea_uid and not bool(getattr(_ncea_actor, "is_admin", False)):
                metadata["_ncea_student_uid"] = str(_ncea_uid)
        except Exception:
            logger.debug("ncea uid stamp skipped", exc_info=True)

        channel_meta: dict[str, Any] = {}
'''

# Repair for installs patched before 2026-09-12: their serve_question
# still carries the buggy year scan (matched inside 92xxx/9194x codes).
EDIT_YEARFIX_OLD = r'''        ym = re.search(r"(19|20)\d{2}", loc) or re.search(r"(19|20)\d{2}", pick["header"])'''
EDIT_YEARFIX_NEW = r'''        # The year must stand alone: a bare (19|20) scan matches inside
        # the standard code itself for 92xxx / 9194x standards
        # ("92047-exm-2024.pdf" -> "2047"), which silently dropped the
        # PDF link. Require the year not to be preceded by a digit.
        ym = (re.search(r"(?<![0-9])(19|20)[0-9]{2}", loc)
              or re.search(r"(?<![0-9])(19|20)[0-9]{2}", pick["header"]))'''

# Repair for installs patched before 2026-11-02: an exhausted bank made the
# tutor refuse and tell the student to find their own paper. Re-use longest-ago
# entry instead (still a verified entry, still verbatim).
EDIT_RECYCLE_OLD = '''        pick = next((e for e in servable if e["id"] not in done), None)
        if pick is None:
            return ToolResult(
                content=(
                    f"(serve_question: standard {code} bank exhausted — "
                    f"drill the mistake list instead of new questions.)"
                ),
                success=False,
            )'''
EDIT_RECYCLE_NEW = '''        pick = next((e for e in servable if e["id"] not in done), None)
        if pick is None:
            # Every verified entry for this standard has already been served.
            # Refusing here is what left a student hunting for their own paper:
            # every bank holds 4-11 entries, so a trial student reaches the end
            # of one in a few sittings. Re-serving the entry served LONGEST ago
            # keeps the anti-fabrication rule intact - it is still a verified
            # bank entry, served verbatim - and it is revision, which is what a
            # student who has done them all needs. Only when the ledger cannot
            # name an entry (no rows at all) does the tool still refuse.
            import json as _json2
            order: list[str] = []
            try:
                _served_path2 = root / "ncea_served.jsonl"
                if _served_path2.exists():
                    for ln in _served_path2.read_text(encoding="utf-8").splitlines():
                        try:
                            eid = _json2.loads(ln).get("entry_id", "")
                        except Exception:
                            continue
                        if eid and eid not in order:
                            order.append(eid)
            except Exception:
                order = []
            by_id = {e["id"]: e for e in servable}
            pick = next((by_id[i] for i in order if i in by_id), None)
        if pick is None:
            return ToolResult(
                content=(
                    f"(serve_question: standard {code} bank exhausted — "
                    f"drill the mistake list instead of new questions.)"
                ),
                success=False,
            )'''

# --- per-turn id (2026-09-21) ------------------------------------------------
# The post-turn JEV gate rebuilds "what did THIS turn inject" from the
# served/graded ledgers. Reading the newest row inside a RECENCY_WINDOW mixed
# evidence from two different turns (a question served at turn N-1 paired with
# a key injected at turn N) and guarded a correct reply. A row can only be
# attributed to the right turn if the turn's id is written with it, so the
# runtime stamps one id per turn here and the two ledger writers copy it onto
# their row. Everything is fail-open: no stamp -> the gate keeps its old
# recency rule, and nothing else changes.
EDIT_TURNSTAMP_OLD = '''            turn_id = str(context.metadata.get("turn_id") or "")'''

EDIT_TURNSTAMP_NEW = '''            turn_id = str(context.metadata.get("turn_id") or "")
            # NCEA per-turn id (site patch), stamped ONCE per partner turn:
            # onto msg.metadata for the post-turn JEV gate, and into the
            # serving module's ContextVar so ServeQuestionTool stamps its
            # served-ledger row no matter who called it - the agent loop's
            # deterministic serve block or the model directly.
            try:
                msg.metadata["_ncea_turn_id"] = turn_id
                from deeptutor.agents._shared.ncea_serve_block import set_current_turn_id
                set_current_turn_id(turn_id)
            except Exception:
                logger.debug("ncea turn stamp skipped", exc_info=True)'''

# The served-ledger row must carry the turn. Anchored on the installed text
# (the tool source itself is inserted by EDIT_TOOLS below), so this is a
# chained edit: it applies ON TOP of the tool patch, on prod and on a fresh
# replay alike.
EDIT_SERVELEDGER_OLD = '''        try:
            import json as _json
            with open(root / "ncea_served.jsonl", "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(
                    {"ts": int(time.time()), "entry_id": pick["id"]},
                    ensure_ascii=False) + "\\n")
        except Exception:
            pass'''

EDIT_SERVELEDGER_NEW = '''        try:
            import json as _json
            # Stamp the turn this question was served in. Attribution only -
            # the post-turn JEV gate pairs the served evidence with the reply,
            # and an earlier turn's question must never be scored against a
            # later reply. Missing helper/unstamped -> "", and the gate falls
            # back to its recency rule.
            _ncea_cur_turn = None
            try:
                from deeptutor.agents._shared.ncea_serve_block import current_turn_id
                _ncea_cur_turn = current_turn_id
            except Exception:
                _ncea_cur_turn = None
            with open(root / "ncea_served.jsonl", "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(
                    {"ts": int(time.time()), "entry_id": pick["id"],
                     "turn": (_ncea_cur_turn() if _ncea_cur_turn else "")},
                    ensure_ascii=False) + "\\n")
        except Exception:
            pass'''

# Chained on the serve-block edit above: pass this turn's id through so the
# ledger row is attributable even when the runtime stamp is absent.
EDIT_SERVEBLOCK2_OLD = '''            return await build_serve_block(context.user_message or "", uid)'''

EDIT_SERVEBLOCK2_NEW = '''            # The third argument is the turn id: it rides onto the served
            # ledger row so the post-turn JEV gate can attribute the injection
            # to this turn instead of guessing by wall clock.
            return await build_serve_block(
                context.user_message or "", uid,
                str((context.metadata or {}).get("turn_id") or ""))'''

# Chained on the grade-block edit above.
EDIT_GRADEPIPE2_OLD = '''            return await build_grade_block(context.user_message or "", uid, has_photo)'''

EDIT_GRADEPIPE2_NEW = '''            # The fourth argument is the turn id: it rides onto the graded
            # ledger row, so the post-turn JEV gate can tell this turn's
            # key injection from an earlier one.
            return await build_grade_block(
                context.user_message or "", uid, has_photo,
                str((context.metadata or {}).get("turn_id") or ""))'''

# --- Latency patch 1: stop re-sending every attached KB's file list ----------
#
# The chat loop prepends `kb_note` to its tool manifest, and `kb_note` is the
# rag note + `render_manifest_note(...)`, which lists EVERY document name of
# EVERY attached KB. Measured on the AU partner (73 KBs / 547 documents), that
# one block was 10,549 tokens against a 16k-window model, re-sent on every
# round of every turn - and reads 73 manifests per turn to build it.
#
# Counts are what the block was for ("how many files are in here" cannot be
# answered from retrieved passages). The names were never needed inline: the
# note already points at `kb_files` for the full list, and that tool exists.
# Keeping the counts and dropping the enumeration takes the block to ~1.1k
# tokens without removing an ability the model had.
EDIT_KBNOTE_TEXT_EN_OLD = '''            "holds or whether one exists in it. Call kb_files for the full list "
            "or to filter by name."
        ),
    },'''

EDIT_KBNOTE_TEXT_EN_NEW = '''            "holds or whether one exists in it. Call kb_files for the full list "
            "or to filter by name."
        ),
        "authority_counts": (
            "Answer questions about how many documents a knowledge base holds "
            "from this inventory — it is authoritative. Retrieved passages "
            "only show what a search happened to match and must never be used "
            "to infer how many documents a knowledge base holds or whether one "
            "exists in it. File names are deliberately not listed here: call "
            "kb_files for the full list, to check whether a file exists, or to "
            "filter by name."
        ),
    },'''

EDIT_KBNOTE_TEXT_ZH_OLD = '''            "需要完整清单或按名称筛选时，调用 kb_files。"
        ),
    },'''

EDIT_KBNOTE_TEXT_ZH_NEW = '''            "需要完整清单或按名称筛选时，调用 kb_files。"
        ),
        "authority_counts": (
            "回答知识库有多少文档这类问题时，一律以本清单为准，它是权威事实。"
            "检索到的片段只代表某次搜索命中的内容，"
            "绝不能用来推断知识库有多少文档、或某个文档是否存在。"
            "文件名不在此列出：需要完整清单、确认某个文件是否存在、"
            "或按名称筛选时，一律调用 kb_files。"
        ),
    },'''

EDIT_KBNOTE_FN_OLD = '''def render_manifest_note(manifests: Sequence[KbManifest], *, language: str) -> str:
    """The system-prompt block: one line per KB, plus the authority rule.

    Empty when there is nothing to describe, so the caller can concatenate it
    unconditionally.
    """
    if not manifests:
        return ""
    language = _lang(language)
    text = _NOTE_TEXT[language]
    lines = [f"- {_note_line(manifest, language)}" for manifest in manifests]'''

EDIT_KBNOTE_NOTELINE_OLD = '''def _note_line(manifest: KbManifest, language: str) -> str:
    text = _NOTE_TEXT[language]
    head = f"{manifest.name}{_qualifier(manifest, language)}{_colon(language)}"
    if not manifest.enumerable:
        return f"{head}{_UNAVAILABLE_LABELS[language][manifest.unavailable]}"
    if not manifest.total:
        return f"{head}{text['empty']}"
    listed = "; ".join(document.name for document in manifest.documents)'''

EDIT_KBNOTE_FN_NEW = '''def render_manifest_note(
    manifests: Sequence[KbManifest],
    *,
    language: str,
    include_documents: bool = True,
) -> str:
    """The system-prompt block: one line per KB, plus the authority rule.

    Empty when there is nothing to describe, so the caller can concatenate it
    unconditionally.

    ``include_documents=False`` keeps every count but drops the enumerated
    file names. That is what the chat loop asks for: with dozens of KBs
    attached the listing is thousands of tokens of file names on every round
    of every turn, and the names stay reachable on demand through the
    ``kb_files`` tool. The counts are authoritative either way.
    """
    if not manifests:
        return ""
    language = _lang(language)
    text = _NOTE_TEXT[language]
    if not include_documents:
        # The paragraph claiming file names are authoritative must not
        # outlive the file names it describes.
        text = {**text, "authority": text["authority_counts"]}
    lines = [
        f"- {_note_line(manifest, language, include_documents=include_documents)}"
        for manifest in manifests
    ]'''

EDIT_KBNOTE_NOTELINE_NEW = '''def _note_line(
    manifest: KbManifest,
    language: str,
    *,
    include_documents: bool = True,
) -> str:
    text = _NOTE_TEXT[language]
    head = f"{manifest.name}{_qualifier(manifest, language)}{_colon(language)}"
    if not manifest.enumerable:
        return f"{head}{_UNAVAILABLE_LABELS[language][manifest.unavailable]}"
    if not manifest.total:
        return f"{head}{text['empty']}"
    if not include_documents:
        # Counts only, no names: the authority paragraph already points at
        # kb_files for the full list, so an "and N more" tail on every KB
        # line would just repeat that once per KB.
        return f"{head}{_total_label(manifest, text)}"
    listed = "; ".join(document.name for document in manifest.documents)'''

EDIT_KBNOTE_CALL_OLD = '''        note = render_manifest_note(self._kb_manifests, language=self.language)
        return f"\\n{note}" if note else ""'''

EDIT_KBNOTE_CALL_NEW = '''        note = render_manifest_note(
            self._kb_manifests, language=self.language, include_documents=False
        )
        return f"\\n{note}" if note else ""'''


# --- Latency patch 2: a wall-clock ceiling for one turn's loop ---------------
#
# `max_rounds` bounds how many LLM calls a turn may make; it says nothing
# about how long they take. Measured on AU, LLM rounds are 74% of the median
# turn and 99.5% of the slow ones, and one round was observed at 492.9s for a
# 720-character reply - so the round budget alone permits an 8-minute turn.
# Once the ceiling passes, the loop settles down the same path it already uses
# when the round budget runs out.
#
# Scoped deliberately: a capability that forwards its OWN round budget (solve,
# research, visualize) owns its long-running semantics and keeps no ceiling,
# so only the chat/partner loop - the one the user waits on - is bounded.
EDIT_LOOPBUDGET_CONST_OLD = '''# Exploring-loop budget: max LLM rounds in one turn's loop. A round without
# tool calls ends the loop early — that is the normal exit.
DEFAULT_MAX_ROUNDS = 8'''

EDIT_LOOPBUDGET_CONST_NEW = '''# Exploring-loop budget: max LLM rounds in one turn's loop. A round without
# tool calls ends the loop early — that is the normal exit.
DEFAULT_MAX_ROUNDS = 8
# Wall-clock ceiling for that same loop, in seconds, counted from the first
# round. Rounds and seconds are different failure modes: a round cap never
# bounds how long a round takes, and one slow round is enough to make a turn
# unusable. When this elapses the loop stops exploring and settles, which
# costs one further round (the answer round). ``0`` disables the ceiling.
DEFAULT_TIME_BUDGET_S = 120'''

EDIT_LOOPBUDGET_INIT_OLD = '''        self._max_rounds = _read_int(chat_cfg, key="max_rounds", default=DEFAULT_MAX_ROUNDS)
        self._exploring_max_tokens = _read_int(
            chat_cfg.get("exploring"), key="max_tokens", default=1600
        )
        self._respond_max_tokens = _read_int(
            chat_cfg.get("responding"), key="max_tokens", default=8000
        )
        # Per-capability overrides (e.g. deep solve forwards its own round
        # budget / temperature / answer-token cap, read from the solve
        # settings). Chat itself passes none and keeps the chat_cfg values.
        if max_rounds is not None:
            self._max_rounds = max(1, int(max_rounds))'''

EDIT_LOOPBUDGET_INIT_NEW = '''        self._max_rounds = _read_int(chat_cfg, key="max_rounds", default=DEFAULT_MAX_ROUNDS)
        self._time_budget_s = _read_int(
            chat_cfg, key="time_budget_s", default=DEFAULT_TIME_BUDGET_S
        )
        self._exploring_max_tokens = _read_int(
            chat_cfg.get("exploring"), key="max_tokens", default=1600
        )
        self._respond_max_tokens = _read_int(
            chat_cfg.get("responding"), key="max_tokens", default=8000
        )
        # Per-capability overrides (e.g. deep solve forwards its own round
        # budget / temperature / answer-token cap, read from the solve
        # settings). Chat itself passes none and keeps the chat_cfg values.
        if max_rounds is not None:
            self._max_rounds = max(1, int(max_rounds))
            # An explicit round budget is a declaration that this loop owns
            # its own long-running semantics (solve's replan cycles, a
            # research block). The chat wall clock is for the loop the user
            # waits on, so it does not reach into those.
            self._time_budget_s = 0'''

EDIT_LOOPBUDGET_PROP_OLD = '''    @property
    def max_rounds(self) -> int:
        return max(1, self._max_rounds)'''

EDIT_LOOPBUDGET_PROP_NEW = '''    @property
    def max_rounds(self) -> int:
        return max(1, self._max_rounds)

    @property
    def time_budget_s(self) -> int:
        """Wall-clock ceiling for one turn's loop; ``0`` means no ceiling."""
        try:
            return max(0, int(self._time_budget_s))
        except (TypeError, ValueError):
            return DEFAULT_TIME_BUDGET_S'''

EDIT_LOOPDEADLINE_IMPORT_OLD = '''import json
import logging
from types import SimpleNamespace'''

EDIT_LOOPDEADLINE_IMPORT_NEW = '''import json
import logging
import time
from types import SimpleNamespace'''

EDIT_LOOPDEADLINE_LOOP_OLD = '''        exploration_budget = max(1, self.pipeline.effective_max_rounds(self.context))
        settlement_started = False
        nudged_empty_finish = False
        finish_redirect_used = False
        continued_answer_parts: list[str] = []
        while True:
            settling = state.exploration_rounds >= exploration_budget
            if settling:
                if state.settlement_rounds >= MAX_SETTLEMENT_ROUNDS:
                    # A model may ignore the settlement directive and keep
                    # requesting tools. One tool-less call is the absolute
                    # stop, so malformed/empty tool cycles cannot run forever.
                    return await self._forced_finish(
                        messages,
                        state,
                        continued_answer_parts=continued_answer_parts,
                    )
                if not settlement_started:
                    await self._begin_settlement(messages)
                    settlement_started = True'''

EDIT_LOOPDEADLINE_LOOP_NEW = '''        exploration_budget = max(1, self.pipeline.effective_max_rounds(self.context))
        # Seconds, not rounds, are what the reader experiences: the round cap
        # bounds how many LLM calls the turn may make, never how long they
        # take. On a slow provider one round can run for minutes, so the loop
        # also settles once this wall clock elapses. ``0`` disables it.
        budget_s = self.pipeline.time_budget_s
        deadline = time.monotonic() + budget_s if budget_s > 0 else None
        settlement_started = False
        nudged_empty_finish = False
        finish_redirect_used = False
        continued_answer_parts: list[str] = []
        while True:
            over_budget = deadline is not None and time.monotonic() >= deadline
            settling = state.exploration_rounds >= exploration_budget or over_budget
            if settling:
                # A time trip gets exactly one settlement round — the answer
                # round — and then the hard stop: the clock is already spent,
                # so unlike the round counter there is no slack left to give a
                # model that keeps requesting tools.
                settlement_limit = 1 if over_budget else MAX_SETTLEMENT_ROUNDS
                if state.settlement_rounds >= settlement_limit:
                    # A model may ignore the settlement directive and keep
                    # requesting tools. One tool-less call is the absolute
                    # stop, so malformed/empty tool cycles cannot run forever.
                    return await self._forced_finish(
                        messages,
                        state,
                        continued_answer_parts=continued_answer_parts,
                    )
                if not settlement_started:
                    await self._begin_settlement(
                        messages,
                        reason="time" if over_budget else "rounds",
                    )
                    settlement_started = True'''

EDIT_LOOPDEADLINE_SETTLE_OLD = '''    async def _begin_settlement(self, messages: list[dict[str, Any]]) -> None:
        """Enter the bounded post-budget phase without dropping tool state."""
        await self.stream.progress(
            self.pipeline._t(
                "notices.loop_settlement",
                default=(
                    "Exploration budget reached; completing required follow-up "
                    "before the final answer."
                ),
            ),
            source=self.source,
            stage=self.stage,
            metadata={"trace_kind": "warning"},
        )'''

EDIT_LOOPDEADLINE_SETTLE_NEW = '''    async def _begin_settlement(
        self,
        messages: list[dict[str, Any]],
        *,
        reason: str = "rounds",
    ) -> None:
        """Enter the bounded post-budget phase without dropping tool state.

        ``reason`` selects the notice wording only; the phase itself is
        identical whether the clock or the round counter ended exploration,
        so a reader is told which budget actually ran out.
        """
        if reason == "time":
            notice = self.pipeline._t(
                "notices.loop_settlement_time",
                default=(
                    "Time budget reached; stopping further exploration and "
                    "completing required follow-up before the final answer."
                ),
            )
        else:
            notice = self.pipeline._t(
                "notices.loop_settlement",
                default=(
                    "Exploration budget reached; completing required follow-up "
                    "before the final answer."
                ),
            )
        await self.stream.progress(
            notice,
            source=self.source,
            stage=self.stage,
            metadata={"trace_kind": "warning"},
        )'''

# The loop's settlement notice is localized in the prompt packs. The
# wall-clock trip needs its own copy, or a Chinese UI reports the English
# default when the clock (not the round counter) is what ran out.
EDIT_SETTLENOTICE_EN_OLD = '''  loop_settlement: "Exploration budget reached; completing required follow-up before the final answer."'''

EDIT_SETTLENOTICE_EN_NEW = '''  loop_settlement: "Exploration budget reached; completing required follow-up before the final answer."
  loop_settlement_time: "Time budget reached; stopping further exploration and completing required follow-up before the final answer."'''

EDIT_SETTLENOTICE_ZH_OLD = '''  loop_settlement: "探索轮次预算已用尽；正在完成必要收尾，然后给出最终回答。"'''

EDIT_SETTLENOTICE_ZH_NEW = '''  loop_settlement: "探索轮次预算已用尽；正在完成必要收尾，然后给出最终回答。"
  loop_settlement_time: "时间预算已用尽；停止继续探索，正在完成必要收尾，然后给出最终回答。"'''

EDIT_CHATTIMEBUDGET_OLD = '''DEFAULT_CHAT_PARAMS: dict[str, Any] = {
    "temperature": 0.2,
    # Exploring-loop budget: max LLM rounds in one turn's loop (a round
    # without tool calls ends the loop early — the normal exit).
    "max_rounds": 8,'''

EDIT_CHATTIMEBUDGET_NEW = '''DEFAULT_CHAT_PARAMS: dict[str, Any] = {
    "temperature": 0.2,
    # Exploring-loop budget: max LLM rounds in one turn's loop (a round
    # without tool calls ends the loop early — the normal exit).
    "max_rounds": 8,
    # Wall-clock ceiling for that same loop, in seconds (0 disables it). The
    # two budgets fail differently: rounds never bound how long a round takes.
    "time_budget_s": 120,'''


# --- Latency patch 3: seed on relevance, and stop repeating the names ---------
#
# Two more per-round costs, both pure duplication (AU, 2026-09-21):
#
#   * pre-seeding took the first three attached KBs in registry order with no
#     relevance test at all, so a Level-1 chemistry question spent ~16-23s
#     retrieving three Level-2 *biology* KBs, injected their passages, and the
#     model then told the user the subject was not in the knowledge base;
#   * the KB names were listed twice (rag note + inventory) and every one of
#     the 73 inventory lines carried "(llamaindex, index ready)".

EDIT_SEEDRANK_CONST_OLD = '''KB_SEED_MAX_KBS = 3
KB_SEED_CHARS_PER_KB = 4000'''

EDIT_SEEDRANK_CONST_NEW = '''KB_SEED_MAX_KBS = 3
KB_SEED_CHARS_PER_KB = 4000
# Pre-seeding is a convenience, not the only way in: the model can call `rag`
# with any attached kb_name. With a handful of KBs, seeding them all is right.
# With dozens, seeding whichever few arrived first in registry order is a
# lottery — the reader is told those are the knowledge bases, the model then
# reports the subject as absent, and the turn pays seconds of retrieval for
# passages that cannot help. Past this many KBs, seed only what the query
# actually names.
KB_SEED_ALWAYS_IF_FEW = 3'''

EDIT_SEEDRANK_HELPERS_OLD = '''def _normalise_user_reply(raw: Any) -> tuple[str, list[dict[str, str]] | None]:'''

EDIT_SEEDRANK_HELPERS_NEW = '''def _seed_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric runs, plus any standard code inside them.

    KB names encode their subject as a standard code (``NZQA_C3_AS91392_
    Equilibrium``), and that is how this corpus is addressed: a query that
    says "91392" is asking for that KB, whatever else it says. Splitting the
    digits out of ``as91392`` / ``91392-exm-2025`` is what makes the match
    land.
    """
    tokens: set[str] = set()
    spaced = "".join(ch if ch.isalnum() else " " for ch in str(text or "").lower())
    for run in spaced.split():
        if len(run) >= 2:
            tokens.add(run)
        # Standard codes are the addressing scheme of this corpus, so the digit
        # run inside a token counts on its own — but only a multi-digit one:
        # a bare "3" out of "Q3b" would match half the names in the registry.
        digits = ("".join(ch if ch.isdigit() else " " for ch in run)).split()
        tokens.update(digit for digit in digits if len(digit) >= 4)
    return tokens


def _seed_kbs(kbs: list[str], query: str) -> list[str]:
    """Which attached KBs to pre-seed for this query (no model call).

    A small selection is a deliberate one, so it is seeded as before. A large
    one is only seeded where the query names something in the KB's own name,
    and is seeded with nothing when it does not: the model then chooses via
    ``rag`` instead of being handed the three KBs that happened to sort first.
    """
    if len(kbs) <= KB_SEED_ALWAYS_IF_FEW:
        return kbs[:KB_SEED_MAX_KBS]
    wanted = _seed_tokens(query)
    if not wanted:
        return []
    scored = sorted(
        (-len(wanted & _seed_tokens(kb)), index, kb) for index, kb in enumerate(kbs)
    )
    return [kb for score, _, kb in scored if score][:KB_SEED_MAX_KBS]


def _normalise_user_reply(raw: Any) -> tuple[str, list[dict[str, str]] | None]:'''

EDIT_SEEDRANK_CALL_OLD = '''        if len(kbs) > KB_SEED_MAX_KBS:
            kbs = kbs[:KB_SEED_MAX_KBS]
        results = await asyncio.gather(*(self._seed_search_one_kb(kb, query, stream) for kb in kbs))'''

EDIT_SEEDRANK_CALL_NEW = '''        kbs = _seed_kbs(kbs, query)
        if not kbs:
            return ""
        results = await asyncio.gather(*(self._seed_search_one_kb(kb, query, stream) for kb in kbs))'''

EDIT_KBNOTE_RAGNOTE_OLD = '''        rag_kbs = self._coexisting_rag_kbs(context)
        if rag_kbs:
            joined = ", ".join(rag_kbs)
            rag_note = (
                f"用户已挂载知识库：{joined}。调用 rag 时，kb_name 必须从其中选一个。"
                if self.language == "zh"
                else (
                    f"Attached knowledge bases: {joined}. When calling rag, kb_name "
                    "must be one of these names."
                )
            )
        return rag_note + self._kb_manifest_system_note() + self._pageindex_system_note()'''

EDIT_KBNOTE_RAGNOTE_NEW = '''        rag_kbs = self._coexisting_rag_kbs(context)
        manifest_note = self._kb_manifest_system_note()
        if rag_kbs:
            # The inventory below already lists every KB by name, so repeating
            # the names here was ~830 tokens on every round of every turn. Keep
            # the full list only when there is no inventory to point at.
            if manifest_note:
                rag_note = (
                    f"用户已挂载 {len(rag_kbs)} 个知识库，名称见下方清单。"
                    "调用 rag 时，kb_name 必须从其中选一个。"
                    if self.language == "zh"
                    else (
                        f"{len(rag_kbs)} knowledge bases are attached and are "
                        "listed below; when calling rag, kb_name must be one of "
                        "those names."
                    )
                )
            else:
                joined = ", ".join(rag_kbs)
                rag_note = (
                    f"用户已挂载知识库：{joined}。调用 rag 时，kb_name 必须从其中选一个。"
                    if self.language == "zh"
                    else (
                        f"Attached knowledge bases: {joined}. When calling rag, kb_name "
                        "must be one of these names."
                    )
                )
        return rag_note + manifest_note + self._pageindex_system_note()'''

# Runs AFTER EDIT_KBNOTE_NOTELINE_*, whose output is this edit's input: the
# compact branch is what patch 1 introduced, and this tightens it in place.
EDIT_NOTELINE_QUAL_OLD = '''    if not include_documents:
        # Counts only, no names: the authority paragraph already points at
        # kb_files for the full list, so an "and N more" tail on every KB
        # line would just repeat that once per KB.
        return f"{head}{_total_label(manifest, text)}"'''

EDIT_NOTELINE_QUAL_NEW = '''    if not include_documents:
        # Counts only, no names: the authority paragraph already points at
        # kb_files for the full list, so an "and N more" tail on every KB
        # line would just repeat that once per KB.
        #
        # The provider + "index ready" parenthetical is also dropped when the
        # KB is healthy: it is the same string on every line, dozens of lines
        # at a time, and it only carries information when it reports something
        # the reader can act on.
        if not (_status_label(manifest, language) and manifest.status != _READY):
            head = f"{manifest.name}{_colon(language)}"
        return f"{head}{_total_label(manifest, text)}"'''

EDIT_READY_CONST_OLD = '''_STATUS_LABELS: dict[str, dict[str, str]] = {'''

EDIT_READY_CONST_NEW = '''#: The one status that means "nothing to report" — see ``_note_line``.
_READY = "ready"


_STATUS_LABELS: dict[str, dict[str, str]] = {'''


# --- Latency patch 4: a hung provider call must fail fast, not for 10 minutes -
#
# The openai SDK ships a 600s read timeout. On a stream that is a per-chunk
# budget (silence between chunks); this codebase's own provider path already
# calls a stream "stalled" at 90s. The loop path had no guard at all, so one
# endpoint that accepts the connection and never answers held the user's turn
# for ten minutes — and the factory's retry defaults (8 retries, 5s base,
# exponential) could multiply that into half an hour. Measured live on AU,
# 2026-09-21: a 17k-token probe hung >600s twice.

EDIT_SDKTIMEOUT_OLD = '''    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url,
        "default_headers": headers or None,
    }'''

EDIT_SDKTIMEOUT_NEW = '''    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url,
        "default_headers": headers or None,
        # The SDK default is a 600s read timeout — ten minutes of silence
        # before failing. On a stream this is a per-chunk budget, and 90s of
        # silence is already this codebase's own definition of a stalled
        # stream; 120s keeps 30s of headroom over that judgment. A hung
        # endpoint must fail in 120s and let the caller's retry policy decide,
        # not hold the user's turn hostage.
        "timeout": httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=60.0),
    }'''

EDIT_RETRYDEFAULT_OLD = '''class LLMRetryConfig(BaseModel):
    max_retries: int = Field(default=8, description="Maximum retry attempts for LLM calls")
    base_delay: float = Field(default=5.0, description="Base delay between retries in seconds")'''

EDIT_RETRYDEFAULT_NEW = '''class LLMRetryConfig(BaseModel):
    # 8 retries at a 5s base (exponential, capped 120s) was the old default:
    # ~8.5 minutes of sleep alone before giving up, on top of each attempt's
    # own timeout. A tutor turn is interactive; 3 retries at 2s gives transient
    # errors four shots in ~14s of delay. LLM_RETRY__MAX_RETRIES still
    # overrides for batch jobs that want the old patience.
    max_retries: int = Field(default=3, description="Maximum retry attempts for LLM calls")
    base_delay: float = Field(default=2.0, description="Base delay between retries in seconds")'''


# --- the GitHub-sync O(n^2) stall (1.6.7 and 1.7.0 share this text) --------
# get_all_github_sources() listed the KBs once and then called
# get_github_sources(kb) per KB, and THAT method re-runs list_knowledge_bases(),
# which reloads kb_config.json and reconciles every KB's index versions. With
# 80 KBs that was 80 full config scans per sync cycle: 285k stat calls, 94s of
# CPU (measured 2026-11-01) run INSIDE the uvicorn event loop, because
# GitHubSourceSyncService._sync_one_cycle is async but does synchronous work.
# The whole server stopped answering HTTP for the duration of every cycle.
EDIT_GHALL_OLD = '''    def get_all_github_sources(self):
        """Scan every KB and return (kb_name, source_dict) pairs."""
        result = []
        for kb_name in self.list_knowledge_bases():
            for src in self.get_github_sources(kb_name):
                result.append((kb_name, src))
        return result'''

EDIT_GHALL_NEW = '''    def get_all_github_sources(self):
        """Scan every KB and return (kb_name, source_dict) pairs.

        The KB list is resolved ONCE and each metadata.json read directly.
        Going through get_github_sources(kb_name) here re-ran
        list_knowledge_bases() inside the loop, which reloads the whole
        kb_config.json and reconciles every KB's index versions: 80 KBs meant
        80 full config scans (~1.2s each, 285k stat calls) per sync cycle, 94s
        of synchronous CPU on the event loop that starved every HTTP request.
        """
        result = []
        for kb_name in self.list_knowledge_bases():
            metadata_file = self.base_dir / kb_name / "metadata.json"
            metadata = self._read_kb_metadata(metadata_file)
            for src in metadata.get("github_sources", []) or []:
                result.append((kb_name, src))
        return result'''


# --- public demo: Google sign-in + 15-day trial (1.6.7) ------------------
EDIT_DEMO_IMPORT_OLD = '''from deeptutor.api.routers.multi_user import router as multi_user_router  # noqa: E402'''

EDIT_DEMO_IMPORT_NEW = '''from deeptutor.api.routers.multi_user import router as multi_user_router  # noqa: E402
from deeptutor.api.routers import ncea_demo  # noqa: E402  (demo sign-in)'''

EDIT_DEMO_INCLUDE_OLD = '''app.include_router(auth.router, prefix="/api/auth", tags=["auth"])'''

EDIT_DEMO_INCLUDE_NEW = '''app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
# Public demo entry: Google sign-in and the trial ledger. Must stay outside
# the authenticated router block below - a first-time student has no token.
app.include_router(ncea_demo.router)'''

EDIT_DEMO_GATE_OLD = '''_USABLE = [Depends(usable_partner)]'''

EDIT_DEMO_GATE_NEW = '''_USABLE = [Depends(usable_partner)]

# Demo trials: blocks NEW turns once a trial has run out, and is a no-op for
# every account with no trial row (operator and school accounts). Missing
# helper => empty list => fail open, exactly like the other guard layers.
try:  # the helper is installed by scripts/apply_site_patches.py
    from deeptutor.api.routers.ncea_demo import ensure_trial_active

    _TRIAL_GATE = [Depends(ensure_trial_active)]
except Exception:  # noqa: BLE001
    _TRIAL_GATE = []'''

EDIT_DEMO_CHAT_OLD = '''@router.post("/{partner_id}/chat", dependencies=_USABLE)'''

EDIT_DEMO_CHAT_NEW = '''@router.post("/{partner_id}/chat", dependencies=[*_USABLE, *_TRIAL_GATE])'''

EDIT_DEMO_STREAM_OLD = '''@router.post("/{partner_id}/chat/execute-stream", dependencies=_USABLE)'''

EDIT_DEMO_STREAM_NEW = '''@router.post("/{partner_id}/chat/execute-stream", dependencies=[*_USABLE, *_TRIAL_GATE])'''

# 1.7.0 mounts routers through module manifests + RouterLoader instead of
# literal app.include_router(...) calls beside the auth include, so the demo
# router lands after mount_module_routes - still with no auth policy, which
# is the whole point: a first-time student has no token yet.
EDIT_DEMO_MOUNT_OLD = '''mount_module_routes(app, _router_loader)'''

EDIT_DEMO_MOUNT_NEW = '''mount_module_routes(app, _router_loader)
# Public demo entry: Google sign-in and the trial ledger. Caddy sends
# /demo* and /api/demo/* straight here, and a first-time student has no
# token yet, so this router carries no auth policy.
from deeptutor.api.routers import ncea_demo  # noqa: E402  (demo sign-in)
app.include_router(ncea_demo.router)'''


# Question page image (site patch; helper lives in
# deeptutor/agents/_shared/ncea_question_image.py). The serve tool appends the
# [QUESTION PAGE IMAGE] note and - critically - returns the workspace item in
# its own metadata, so the attachment persists even when the model says
# nothing about it.
EDIT_QIMAGE_OLD = r'''        stem = pick["bullets"].get("identifying stem", "")
        topic = pick["bullets"].get("topic", "")
        answer = pick["bullets"].get("answer", "")
        return ToolResult(
            content=(
                f"SERVED {pick['id']}\n"
                f"Location: {loc}\n"
                f"Topic: {topic}\n"
                f"Stem (confirm identity, max 2 lines): {stem}\n"
                f"PDF: {pdf or '(link not on file — ask student to open NZQA paper)'}\n"
                f"Answer key (for grading only, NEVER show first): {answer}\n"
                f"RULE: send Location + PDF link only. White paper. No pasted text."
            ),
            metadata={"entry_id": pick["id"], "standard": code},
        )'''

EDIT_QIMAGE_NEW = r'''        stem = pick["bullets"].get("identifying stem", "")
        topic = pick["bullets"].get("topic", "")
        answer = pick["bullets"].get("answer", "")
        # Question page image (site patch): the student may not own the paper
        # ("I don't have the paper" - Mark, 2026-10-31) and text + a PDF link
        # never showed the question WITH its diagram. Best-effort; any
        # failure keeps the plain text + link serve exactly as before.
        img_note, img_sources, img_items = "", [], []
        try:
            from deeptutor.agents._shared.ncea_question_image import (
                present_question_image,
            )
            img_note, img_sources, img_items = await present_question_image(
                loc=loc, pdf_url=pdf, code=code, entry_id=str(pick["id"]),
            )
        except Exception:
            img_note, img_sources, img_items = "", [], []
        content = (
            f"SERVED {pick['id']}\n"
            f"Location: {loc}\n"
            f"Topic: {topic}\n"
            f"Stem (confirm identity, max 2 lines): {stem}\n"
            f"PDF: {pdf or '(link not on file — ask student to open NZQA paper)'}\n"
            f"Answer key (for grading only, NEVER show first): {answer}\n"
            f"RULE: show Location + the question-page image + PDF link. "
            f"Never compose or reconstruct the question."
        )
        if img_note:
            content = img_note + "\n\n" + content
        meta = {"entry_id": pick["id"], "standard": code}
        if img_items:
            meta["workspace_items"] = img_items
        return ToolResult(
            content=content,
            sources=img_sources or None,
            metadata=meta,
        )'''


# (relpath, marker proving already-applied, old, new)
EDITS: list[tuple[str, str, str, str]] = [
    ("deeptutor/knowledge/manager.py", "The KB list is resolved ONCE",
     EDIT_GHALL_OLD, EDIT_GHALL_NEW),
    ("deeptutor/api/main.py", "from deeptutor.api.routers import ncea_demo",
     EDIT_DEMO_IMPORT_OLD, EDIT_DEMO_IMPORT_NEW),
    ("deeptutor/api/main.py", "app.include_router(ncea_demo.router)",
     EDIT_DEMO_INCLUDE_OLD, EDIT_DEMO_INCLUDE_NEW),
    ("deeptutor/api/routers/partners.py", "_TRIAL_GATE = [Depends(ensure_trial_active)]",
     EDIT_DEMO_GATE_OLD, EDIT_DEMO_GATE_NEW),
    ("deeptutor/api/routers/partners.py", 'chat", dependencies=[*_USABLE, *_TRIAL_GATE])',
     EDIT_DEMO_CHAT_OLD, EDIT_DEMO_CHAT_NEW),
    ("deeptutor/api/routers/partners.py", 'execute-stream", dependencies=[*_USABLE, *_TRIAL_GATE])',
     EDIT_DEMO_STREAM_OLD, EDIT_DEMO_STREAM_NEW),
    ("deeptutor/multi_user/identity.py", "class CorruptUserStoreError",
     EDIT_IDENTITY_EXC_OLD, EDIT_IDENTITY_EXC_NEW),
    ("deeptutor/multi_user/identity.py", "def _read_json(path: Path, *, strict: bool = False",
     EDIT_READ_JSON_OLD, EDIT_READ_JSON_NEW),
    ("deeptutor/multi_user/identity.py", "_read_json(USERS_FILE, strict=True)",
     EDIT_LOAD_USERS_OLD, EDIT_LOAD_USERS_NEW),
    ("deeptutor/multi_user/grants.py", "class CorruptGrantError",
     EDIT_GRANT_EXC_OLD, EDIT_GRANT_EXC_NEW),
    ("deeptutor/multi_user/grants.py", "from deeptutor.services.file_io import atomic_write_text",
     EDIT_GRANTS_IMPORT_OLD, EDIT_GRANTS_IMPORT_NEW),
    ("deeptutor/multi_user/grants.py", "refusing to serve ",
     EDIT_LOAD_GRANT_OLD, EDIT_LOAD_GRANT_NEW),
    ("deeptutor/multi_user/grants.py", "atomic_write_text(path, json.dumps(grant",
     EDIT_SAVE_GRANT_OLD, EDIT_SAVE_GRANT_NEW),
    ("deeptutor/tools/media_gen_tool.py", "Direct file URL for",
     EDIT_MEDIAGEN_OLD, EDIT_MEDIAGEN_NEW),
    ("deeptutor/services/session/turns/request_preparer.py", "import logging",
     EDIT_PREPLOG_OLD, EDIT_PREPLOG_NEW),
    ("deeptutor/services/session/turns/request_preparer.py", "Concierge: students never attach",
     EDIT_CONCIERGE_OLD, EDIT_CONCIERGE_NEW),
    ("deeptutor/services/session/turns/request_preparer.py", "Scope clamp: partner turns arrive",
     EDIT_SCOPECLAMP_OLD, EDIT_SCOPECLAMP_NEW),
    ("deeptutor/services/partners/runtime.py", "partner KB scope clamp skipped",
     EDIT_P10_OLD, EDIT_P10_NEW),
    ("deeptutor/tools/builtin/__init__.py", "class ServeQuestionTool",
     EDIT_TOOLS_OLD, EDIT_TOOLS_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"serve_question",\n    "log_mistake",',
     EDIT_TUPLE_OLD, EDIT_TUPLE_NEW),
    ("deeptutor/tools/builtin_specs.py", '("serve_question", "ServeQuestionTool")',
     EDIT_SPECS_OLD, EDIT_SPECS_NEW),
    ("deeptutor/tools/builtin/__init__.py", "Grade on the RAW entry text",
     EDIT_S11B_OLD, EDIT_S11B_NEW),
    ("deeptutor/tools/builtin/__init__.py", "(?<![0-9])(19|20)",
     EDIT_YEARFIX_OLD, EDIT_YEARFIX_NEW),
    ("deeptutor/tools/builtin/__init__.py", "Re-serving the entry served",
     EDIT_RECYCLE_OLD, EDIT_RECYCLE_NEW),
    ("deeptutor/tools/builtin/__init__.py", "_ncea_cur_turn",
     EDIT_SERVELEDGER_OLD, EDIT_SERVELEDGER_NEW),
    ("deeptutor/agents/loop/pipeline.py", "_retrieve_ncea_serve_block",
     EDIT_SERVEBLOCK_OLD, EDIT_SERVEBLOCK_NEW),
    # Must come AFTER the serve-block edit: its OLD anchor is that edit's NEW
    # text (applies on prod and on a fresh replay alike). The marker must be
    # unique per edit - a shared one would SKIP the second edit once the first
    # landed, which is exactly how a chained edit goes missing.
    ("deeptutor/agents/loop/pipeline.py", "guessing by wall clock",
     EDIT_SERVEBLOCK2_OLD, EDIT_SERVEBLOCK2_NEW),
    ("deeptutor/agents/loop/agent_loop.py", "_retrieve_ncea_serve_block(self.context)",
     EDIT_LOOPJOIN_OLD, EDIT_LOOPJOIN_NEW),
    # Order matters: REPAIR restores the stock text, then UIDSTAMP applies on it.
    ("deeptutor/services/partners/runtime.py", EDIT_UIDSTAMP_REPAIR_NEW,
     EDIT_UIDSTAMP_REPAIR_OLD, EDIT_UIDSTAMP_REPAIR_NEW),
    ("deeptutor/services/partners/runtime.py", "metadata[\"_ncea_student_uid\"] = str(",
     EDIT_UIDSTAMP_OLD, EDIT_UIDSTAMP_NEW),
    ("deeptutor/services/partners/runtime.py", "_ncea_turn_id",
     EDIT_TURNSTAMP_OLD, EDIT_TURNSTAMP_NEW),
    ("deeptutor/services/partners/runtime.py", "ncea autolog skipped",
     EDIT_AUTOLOG_OLD, EDIT_AUTOLOG_NEW),
    ("deeptutor/services/partners/runtime.py", "await maybe_autolog",
     EDIT_AUTOLOG2_OLD, EDIT_AUTOLOG2_NEW),
    ("deeptutor/services/partners/runtime.py", "maybe_guard_output",
     EDIT_GUARD_OLD, EDIT_GUARD_NEW),
    # Must come AFTER the guard edit: its OLD anchor is the guard's NEW text.
    ("deeptutor/services/partners/runtime.py", "maybe_jev_guard",
     EDIT_JEVGATE_OLD, EDIT_JEVGATE_NEW),
    ("deeptutor/agents/loop/pipeline.py", "ncea grade block failed",
     EDIT_GRADEPIPE_OLD, EDIT_GRADEPIPE_NEW),
    ("deeptutor/agents/loop/pipeline.py", "key injection from an earlier one",
     EDIT_GRADEPIPE2_OLD, EDIT_GRADEPIPE2_NEW),
    # Order matters: JOIN (review) must precede JOIN2 (grade) — JOIN2's
    # stock text contains the review line JOIN adds. Fresh-venv replay
    # aborts half-way if these are reversed (production was incremental
    # so the inversion went unnoticed).
    ("deeptutor/agents/loop/agent_loop.py", "ncea_review_block",
     EDIT_JOIN_OLD, EDIT_JOIN_NEW),
    ("deeptutor/agents/loop/agent_loop.py", "ncea_grade_block = await",
     EDIT_JOIN2_OLD, EDIT_JOIN2_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"92022", "92023", "92046"',
     EDIT_ALLOWY11_OLD, EDIT_ALLOWY11_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"91261", "91262", "91267"',
     EDIT_ALLOWM2_OLD, EDIT_ALLOWM2_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"91156", "91157", "91159", "91603", "91605", "91606"',
     EDIT_ALLOWBIO_OLD, EDIT_ALLOWBIO_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"91586"',
     EDIT_ALLOWSTAT_OLD, EDIT_ALLOWSTAT_NEW),
    ("deeptutor/api/routers/question_notebook.py", '"photo-grade"]',
     EDIT_QBLIT_OLD, EDIT_QBLIT_NEW),
    ("deeptutor/api/routers/question_notebook.py", 'photo-grade)?$"',
     EDIT_QBPAT_OLD, EDIT_QBPAT_NEW),
    ("deeptutor/services/session/sqlite_store.py", '"photo-grade"})',
     EDIT_QBSET_OLD, EDIT_QBSET_NEW),
    ("deeptutor/tools/builtin/__init__.py", "_NCEA_NEVER_SERVE",
     EDIT_NEVERSET_OLD, EDIT_NEVERSET_NEW),
    ("deeptutor/tools/builtin/__init__.py", "if code in _NCEA_NEVER_SERVE",
     EDIT_NEVERCHECK_OLD, EDIT_NEVERCHECK_NEW),
    ("deeptutor/agents/loop/pipeline.py", "ncea review block failed",
     EDIT_REVIEWPIPE_OLD, EDIT_REVIEWPIPE_NEW),
    ("deeptutor/services/auth.py", "refused for disabled account",
     EDIT_AUTHDISABLE_LOGIN_OLD, EDIT_AUTHDISABLE_LOGIN_NEW),
    ("deeptutor/services/auth.py", "die at next request",
     EDIT_AUTHDISABLE_TOKEN_OLD, EDIT_AUTHDISABLE_TOKEN_NEW),
    # Latency patch 1: KB inventory block, counts without the file names.
    ("deeptutor/knowledge/manifest.py", "File names are deliberately not listed here",
     EDIT_KBNOTE_TEXT_EN_OLD, EDIT_KBNOTE_TEXT_EN_NEW),
    ("deeptutor/knowledge/manifest.py", '"文件名不在此列出',
     EDIT_KBNOTE_TEXT_ZH_OLD, EDIT_KBNOTE_TEXT_ZH_NEW),
    ("deeptutor/knowledge/manifest.py", 'text["authority_counts"]',
     EDIT_KBNOTE_FN_OLD, EDIT_KBNOTE_FN_NEW),
    ("deeptutor/knowledge/manifest.py", "Counts only, no names",
     EDIT_KBNOTE_NOTELINE_OLD, EDIT_KBNOTE_NOTELINE_NEW),
    ("deeptutor/agents/loop/pipeline.py", "include_documents=False",
     EDIT_KBNOTE_CALL_OLD, EDIT_KBNOTE_CALL_NEW),
    # Latency patch 2: wall-clock ceiling for the chat loop.
    ("deeptutor/services/config/loader.py", '"time_budget_s": 120,',
     EDIT_CHATTIMEBUDGET_OLD, EDIT_CHATTIMEBUDGET_NEW),
    ("deeptutor/agents/loop/pipeline.py", "DEFAULT_TIME_BUDGET_S = 120",
     EDIT_LOOPBUDGET_CONST_OLD, EDIT_LOOPBUDGET_CONST_NEW),
    ("deeptutor/agents/loop/pipeline.py", 'key="time_budget_s"',
     EDIT_LOOPBUDGET_INIT_OLD, EDIT_LOOPBUDGET_INIT_NEW),
    ("deeptutor/agents/loop/pipeline.py", "def time_budget_s",
     EDIT_LOOPBUDGET_PROP_OLD, EDIT_LOOPBUDGET_PROP_NEW),
    ("deeptutor/agents/loop/agent_loop.py", "import logging\nimport time",
     EDIT_LOOPDEADLINE_IMPORT_OLD, EDIT_LOOPDEADLINE_IMPORT_NEW),
    ("deeptutor/agents/loop/agent_loop.py", "deadline = time.monotonic() + budget_s",
     EDIT_LOOPDEADLINE_LOOP_OLD, EDIT_LOOPDEADLINE_LOOP_NEW),
    ("deeptutor/agents/loop/agent_loop.py", 'reason: str = "rounds"',
     EDIT_LOOPDEADLINE_SETTLE_OLD, EDIT_LOOPDEADLINE_SETTLE_NEW),
    ("deeptutor/agents/chat/prompts/en/agentic_chat.yaml", "loop_settlement_time",
     EDIT_SETTLENOTICE_EN_OLD, EDIT_SETTLENOTICE_EN_NEW),
    ("deeptutor/agents/chat/prompts/zh/agentic_chat.yaml", "loop_settlement_time",
     EDIT_SETTLENOTICE_ZH_OLD, EDIT_SETTLENOTICE_ZH_NEW),
    # Latency patch 3: relevant KB pre-seed, and no repeated names.
    ("deeptutor/agents/loop/pipeline.py", "KB_SEED_ALWAYS_IF_FEW",
     EDIT_SEEDRANK_CONST_OLD, EDIT_SEEDRANK_CONST_NEW),
    ("deeptutor/agents/loop/pipeline.py", "def _seed_kbs(",
     EDIT_SEEDRANK_HELPERS_OLD, EDIT_SEEDRANK_HELPERS_NEW),
    ("deeptutor/agents/loop/pipeline.py", "_seed_kbs(kbs, query)",
     EDIT_SEEDRANK_CALL_OLD, EDIT_SEEDRANK_CALL_NEW),
    ("deeptutor/agents/loop/pipeline.py", "名称见下方清单",
     EDIT_KBNOTE_RAGNOTE_OLD, EDIT_KBNOTE_RAGNOTE_NEW),
    # Order matters: the READY constant must land before the line that uses it.
    ("deeptutor/knowledge/manifest.py", '_READY = "ready"',
     EDIT_READY_CONST_OLD, EDIT_READY_CONST_NEW),
    ("deeptutor/knowledge/manifest.py", "and manifest.status != _READY",
     EDIT_NOTELINE_QUAL_OLD, EDIT_NOTELINE_QUAL_NEW),
    # Latency patch 4: fail fast on a silent endpoint.
    ("deeptutor/services/llm/openai_http_client.py", '"timeout": httpx.Timeout(',
     EDIT_SDKTIMEOUT_OLD, EDIT_SDKTIMEOUT_NEW),
    ("deeptutor/config/settings.py", 'Field(default=3, description="Maximum retry attempts',
     EDIT_RETRYDEFAULT_OLD, EDIT_RETRYDEFAULT_NEW),
    # Question page image (same serve-tool tail text in both releases; the
    # 1.7.0 table carries its own copy so each table stays self-contained).
    ("deeptutor/tools/builtin/__init__.py", "Question page image (site patch)",
     EDIT_QIMAGE_OLD, EDIT_QIMAGE_NEW),
    # One window per account + a student's own provider key. Last on purpose:
    # the main.py anchors are the demo router's import and include, which the
    # demo edits above create.
    ("deeptutor/services/partners/runtime.py", "_ncea_single_turn",
     EDIT_TURNLIMIT_IMPORT_OLD, EDIT_TURNLIMIT_IMPORT_NEW),
    ("deeptutor/services/partners/runtime.py", "_ncea_single_turn(msg.actor)",
     EDIT_TURNLIMIT_OLD, EDIT_TURNLIMIT_NEW),
    ("deeptutor/services/session/turns/request_preparer.py", "Prefer the student's OWN provider key",
     EDIT_OWNKEY_PREF_OLD, EDIT_OWNKEY_PREF_NEW),
    ("deeptutor/api/main.py", "ncea_personal_key",
     EDIT_OWNKEY_IMPORT_OLD, EDIT_OWNKEY_IMPORT_NEW),
    ("deeptutor/api/main.py", "ncea_personal_key.router",
     EDIT_OWNKEY_MOUNT_OLD, EDIT_OWNKEY_MOUNT_NEW),
    # Must follow the turn-limit import edit: the anchor is the shim it adds.
    ("deeptutor/services/partners/runtime.py",
     "from deeptutor.multi_user.ncea_own_key import own_key_scope",
     EDIT_OWNKEY_SCOPE_IMPORT_OLD, EDIT_OWNKEY_SCOPE_IMPORT_NEW),
    ("deeptutor/services/partners/runtime.py", "_ncea_own_cfg",
     EDIT_OWNKEY_SCOPE_OLD, EDIT_OWNKEY_SCOPE_NEW),
]


# ===========================================================================
# deeptutor 1.7.0 table
#
# 1.7.0 is a modular rewrite. Three things follow from that and shape this
# table:
#
#  * partners / knowledge / notebook / questions ship as SEPARATE
#    distributions (`deeptutor-partners`, ...). Install all of them or the
#    core wheel is hollow; with them installed the moved files return to
#    their old import paths, so most anchors here are unchanged.
#  * the chat loop was split into loop_host.py (effects) + python_scheduler.py
#    (control flow), and `_run_loop` now passes a config dict. The wall-clock
#    ceiling therefore moves with the control flow, not with agent_loop.
#  * `AssessmentSource` moved to core/assessment.py and `ASSESSMENT_SOURCES`
#    is derived from it with get_args(). One edit there replaces the three
#    1.6.7 edits on the router, the query pattern and the sqlite store.
#
# Everything that still anchors on 1.7.0 text is reused verbatim from EDITS
# above; only the sites that moved are restated below.
# ===========================================================================

# NOT carried over, deliberately:
#
#  load_grant / save_grant hardening (CorruptGrantError + atomic_write_text).
#  1.7.0 upstream already raises GrantStorageError on an unreadable grant and
#  already writes through atomic_write_text. Re-adding ours would run the same
#  behaviour twice under two exception names, and ours would also delete
#  upstream's per-field validation on the way.
#
#  EDIT_UIDSTAMP_REPAIR_*. That edit only ever repaired text our own 1.6.7
#  edit had written; on stock 1.7.0 there is nothing to repair. The uid stamp
#  it guarded is re-established below (EDIT170_UIDSTAMP_*).

EDIT170_SPECS_OLD = '''BUILTIN_TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in BUILTIN_TOOL_SPECS)'''

EDIT170_SPECS_NEW = '''# NCEA deterministic serving (site patch): the catalog is built at boot from
# deeptutor.modules.catalog.manifests(), so the two NCEA tools have to be
# appended HERE. A name absent from BUILTIN_TOOL_SPECS cannot be mounted on a
# turn at all - the model never sees it in the tool manifest, and our whole
# anti-fabrication path (serve_question / log_mistake) is silently absent.
# BUILTIN_TOOL_SPEC_BY_NAME is derived after this point, so it sees them.
BUILTIN_TOOL_SPECS = BUILTIN_TOOL_SPECS + (
    BuiltinToolSpec("serve_question", "deeptutor.tools.builtin:ServeQuestionTool"),
    BuiltinToolSpec("log_mistake", "deeptutor.tools.builtin:LogMistakeTool"),
)

BUILTIN_TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in BUILTIN_TOOL_SPECS)'''

# The loop's seed now also carries the capability briefings and is joined from a
# list, so the NCEA blocks are fetched here and added to that same join.
EDIT170_SEEDJOIN_OLD = '''            seed_block = await self.pipeline._retrieve_kb_seed_block(self.context, self.stream)
            capability_seed = self.pipeline._capability_pre_loop_seed(self.context)
'''

EDIT170_SEEDJOIN_NEW = '''            seed_block = await self.pipeline._retrieve_kb_seed_block(self.context, self.stream)
            # NCEA deterministic injection (site patch): serve / review / grade
            # blocks are computed, not requested - the model must never have to
            # decide whether to look a question up or read the answer key.
            ncea_serve_block = await self.pipeline._retrieve_ncea_serve_block(self.context)
            ncea_review_block = await self.pipeline._retrieve_ncea_review_block(self.context)
            ncea_grade_block = await self.pipeline._retrieve_ncea_grade_block(self.context)
            capability_seed = self.pipeline._capability_pre_loop_seed(self.context)
'''

EDIT170_SEEDJOIN2_OLD = '''                for block in (
                    seed_block.strip(),
                    capability_seed.strip(),'''

EDIT170_SEEDJOIN2_NEW = '''                for block in (
                    seed_block.strip(),
                    ncea_serve_block.strip(),
                    ncea_review_block.strip(),
                    ncea_grade_block.strip(),
                    capability_seed.strip(),'''

# The single point where a turn's reply is final. `_run_turn` retries on the
# backup model and returns only after that, so hooking here (rather than inside
# _execute_turn) means a backup-model reply is guarded exactly like a primary
# one. Guard before the store write so nothing unguarded reaches the transcript.
EDIT170_HOOKS_OLD = '''            final, turn_events, generated_attachments = await self._run_turn(
                msg,
                store=store,
                on_event=on_event,
                delivery_meta=delivery_meta,
                options=options,
            )
            if options.persist:'''

EDIT170_HOOKS_NEW = '''            final, turn_events, generated_attachments = await self._run_turn(
                msg,
                store=store,
                on_event=on_event,
                delivery_meta=delivery_meta,
                options=options,
            )
            # NCEA post-turn layer (site patch). Order is load-bearing: the
            # auto-log records what was served/graded, the deterministic guard
            # then refuses a citation that was never served, and only then does
            # the JEV gate judge what neither of those could see (a recalled
            # answer key, a self-composed question, a wrong paper). Every one
            # of them fails open - a helper that is absent or raises leaves the
            # reply untouched rather than breaking the turn.
            try:
                from deeptutor.agents._shared.ncea_autolog import maybe_autolog
                await maybe_autolog(msg=msg, final=final, turn_events=turn_events)
            except Exception:
                logger.warning("ncea autolog skipped", exc_info=True)
            try:
                from deeptutor.agents._shared.ncea_output_guard import maybe_guard_output
                guarded = await maybe_guard_output(msg=msg, final=final)
                if guarded:
                    final = guarded
            except Exception:
                logger.warning("ncea output guard skipped", exc_info=True)
            try:
                from deeptutor.agents._shared.ncea_jev_gate import maybe_jev_guard
                jev = await maybe_jev_guard(msg=msg, final=final)
                if jev:
                    final = jev
            except Exception:
                logger.warning("ncea jev gate skipped", exc_info=True)
            if options.persist:'''

EDIT170_SCOPECLAMP_OLD = '''        knowledge_bases = list(
            overrides.get("knowledge_bases")
            if overrides.get("knowledge_bases") is not None
            else preferences.get("knowledge_bases") or []
        )
'''

EDIT170_SCOPECLAMP_NEW = '''        knowledge_bases = list(
            overrides.get("knowledge_bases")
            if overrides.get("knowledge_bases") is not None
            else preferences.get("knowledge_bases") or []
        )
        # Scope clamp (site patch): a partner turn arrives with the partner's
        # whole library pre-loaded, which used to sail through and seed Bio
        # KBs into Physics turns. A student only ever sees granted KBs, so keep
        # the granted subset; if nothing matches, fall back to the grant.
        try:
            from deeptutor.multi_user.grants import load_grant as _load_grant
            from deeptutor.multi_user.identity import get_current_user_or_none

            _user = get_current_user_or_none()
            if _user is not None and not _user.is_admin:
                _grant = _load_grant(_user.id) or {}
                _allowed: set[str] = set()
                _order: list[str] = []
                for _item in _grant.get("knowledge_bases") or []:
                    if isinstance(_item, dict):
                        _ref = (
                            _item.get("resource_id") or _item.get("id")
                            or _item.get("name") or ""
                        ).strip()
                    else:
                        _ref = str(_item or "").strip()
                    if not _ref:
                        continue
                    if _ref not in _order:
                        _order.append(_ref)
                    _allowed.add(_ref)
                    if _ref.startswith("admin:kb:"):
                        _allowed.add(_ref[len("admin:kb:"):])
                    else:
                        _allowed.add("admin:kb:" + _ref)
                if _allowed:
                    _kept = [
                        kb for kb in knowledge_bases
                        if kb and str(kb).strip() in _allowed
                    ]
                    knowledge_bases = _kept or [
                        r[len("admin:kb:"):] if r.startswith("admin:kb:") else r
                        for r in _order
                    ]
        except Exception:
            logger.warning("ncea KB scope clamp skipped", exc_info=True)
'''

# Partner KB clamp + the student uid. In 1.6.7 these were two edits on text
# that has since moved; here they are one insert in front of the metadata dict,
# which is where `_ncea_student_uid` has to land for the loop's serve block to
# resolve the student's grants and ledger.
EDIT170_PARTNERCLAMP_OLD = '''        metadata: dict[str, Any] = {
            "turn_id": turn_id,
            "source": "partner",'''

EDIT170_PARTNERCLAMP_NEW = '''        # NCEA scope clamp + uid stamp (site patch). The clamp keeps a
        # partner's whole library from seeding KBs the chatting student was
        # never granted; the stamp is what lets the loop resolve that
        # student's serve/review/grade ledgers at all. Admin actors untouched.
        _ncea_uid = ""
        try:
            _actor = getattr(msg, "actor", None)
            _uid = str(getattr(_actor, "id", "") or "")
            _is_admin = bool(getattr(_actor, "is_admin", False))
            if _uid and not _is_admin:
                from deeptutor.multi_user.grants import load_grant as _load_grant

                _grant = _load_grant(_uid) or {}
                _allowed: set[str] = set()
                for _item in _grant.get("knowledge_bases") or []:
                    if isinstance(_item, dict):
                        _ref = (
                            _item.get("resource_id") or _item.get("id")
                            or _item.get("name") or ""
                        ).strip()
                    else:
                        _ref = str(_item or "").strip()
                    if _ref:
                        _allowed.add(_ref)
                        if _ref.startswith("admin:kb:"):
                            _allowed.add(_ref[len("admin:kb:"):])
                        else:
                            _allowed.add("admin:kb:" + _ref)
                if _allowed:
                    _kept = [k for k in (kb_names or []) if str(k).strip() in _allowed]
                    if _kept:
                        kb_names = _kept
                _ncea_uid = _uid
        except Exception:
            logger.warning("partner KB scope clamp skipped", exc_info=True)

        metadata: dict[str, Any] = {
            "turn_id": turn_id,
            "source": "partner",'''

EDIT170_UIDSTAMP_OLD = '''        channel_meta: dict[str, Any] = {}'''

EDIT170_UIDSTAMP_NEW = '''        # Leading underscore keeps this out of channel_metadata (same convention
        # as _attachment_records); the loop reads it to find the student's
        # served / graded ledgers for this turn.
        if _ncea_uid:
            metadata["_ncea_student_uid"] = _ncea_uid
        channel_meta: dict[str, Any] = {}'''

EDIT170_ASSESS_OLD = '''AssessmentSource = Literal[
    "deep_question", "mastery_path", "immersive_reading", "book", "partner_chat", "import"
]'''

EDIT170_ASSESS_NEW = '''AssessmentSource = Literal[
    "deep_question", "mastery_path", "immersive_reading", "book", "partner_chat", "import",
    # photo-grade (site patch): a graded photo's attempt must be filed under its
    # own source, so the wrong book can tell a marked photo from a quiz. In
    # 1.7.0 ASSESSMENT_SOURCES is derived from this Literal with get_args(), so
    # this one edit also widens the sqlite store's accepted set and the
    # router's query validation - no second or third edit needed.
    "photo-grade",
]'''

# Wall-clock ceiling, ported to the scheduler split. 1.7.0 runs the loop in
# python_scheduler.run_loop (or its TypeScript twin), both driven by a config
# dict; loop_host.py holds the effects. The deadline belongs with the control
# flow that decides whether to open another round.
EDIT170_SCHED_IMPORT_OLD = '''from __future__ import annotations

from typing import Any'''

EDIT170_SCHED_IMPORT_NEW = '''from __future__ import annotations

import time
from typing import Any'''

EDIT170_SCHED_DEADLINE_OLD = '''    settlement_started = False
    empty_recoveries = 0
    finish_redirect_used = False
    recovery_used = False
'''

EDIT170_SCHED_DEADLINE_NEW = '''    settlement_started = False
    empty_recoveries = 0
    finish_redirect_used = False
    recovery_used = False
    # Seconds, not rounds, are what the reader experiences (site patch). The
    # round cap bounds how many LLM calls a turn may make, never how long they
    # take, so one slow round could hold a turn for minutes. 0 disables it.
    budget_s = int(config.get("timeBudgetS") or 0)
    deadline = time.monotonic() + budget_s if budget_s > 0 else None
'''

EDIT170_SCHED_SETTLE_OLD = '''        settling = state["exploration_rounds"] >= config["explorationBudget"]
        if settling:
            if state["settlement_rounds"] >= config["settlementBudget"]:'''

EDIT170_SCHED_SETTLE_NEW = '''        over_budget = deadline is not None and time.monotonic() >= deadline
        settling = state["exploration_rounds"] >= config["explorationBudget"] or over_budget
        if settling:
            # A time trip gets exactly one settlement round - the answer round -
            # and then the hard stop: the clock is already spent, so unlike the
            # round counter there is no slack left for a model that keeps
            # requesting tools.
            settlement_limit = 1 if over_budget else config["settlementBudget"]
            if state["settlement_rounds"] >= settlement_limit:'''

EDIT170_RUNLOOP_OLD = '''            config={
                "explorationBudget": max(1, self.pipeline.effective_max_rounds(self.context)),
                "settlementBudget": MAX_SETTLEMENT_ROUNDS,
                "emptyRecoveryBudget": MAX_REASONING_ONLY_RECOVERIES,
            },'''

EDIT170_RUNLOOP_NEW = '''            config={
                "explorationBudget": max(1, self.pipeline.effective_max_rounds(self.context)),
                "settlementBudget": MAX_SETTLEMENT_ROUNDS,
                "emptyRecoveryBudget": MAX_REASONING_ONLY_RECOVERIES,
                # Site patch: the wall-clock ceiling travels with the config so
                # both scheduler backends see the same budget.
                "timeBudgetS": self.pipeline.time_budget_s,
            },'''
# 1.6.7 raised CorruptGrantError from load_grant; 1.7.0 upstream raises
# GrantStorageError for the same condition. Our helpers and tests name the old
# one. An alias is one behaviour with two names; re-implementing it would be the
# same check twice under two exceptions, and would drop upstream's per-field
# validation on the way.
EDIT170_GRANT_EXC_OLD = '''class GrantStorageError(RuntimeError):
    """An existing grant cannot be read safely."""'''

EDIT170_GRANT_EXC_NEW = '''class GrantStorageError(RuntimeError):
    """An existing grant cannot be read safely."""


# Compatibility alias (site patch) - see the 1.7.0 note above this table.
CorruptGrantError = GrantStorageError'''
EDIT170_SETTLE_REASON_OLD = '''    async def begin_settlement(self) -> None:
        await self.loop._begin_settlement(self.messages)'''

EDIT170_SETTLE_REASON_NEW = '''    async def begin_settlement(self, reason: str = "rounds") -> None:
        await self.loop._begin_settlement(self.messages, reason=reason)'''

EDIT170_SCHED_SETTLE_CALL_OLD = '''            if not settlement_started:
                await invoke("begin_settlement")
                settlement_started = True'''

EDIT170_SCHED_SETTLE_CALL_NEW = '''            if not settlement_started:
                # The reader is told WHICH budget ran out (site patch): a clock
                # trip and a round trip need different advice, and "rounds" is
                # simply false when the wall clock is what fired.
                await invoke(
                    "begin_settlement",
                    reason="time" if over_budget else "rounds",
                )
                settlement_started = True'''


EDITS_170: list[tuple[str, str, str, str]] = [
    # -- the GitHub-sync stall, identical text in 1.6.7 and 1.7.0 ------------
    ("deeptutor/knowledge/manager.py", "The KB list is resolved ONCE",
     EDIT_GHALL_OLD, EDIT_GHALL_NEW),
    # -- the NCEA tool block, then the edits that refine it ------------------
    # Order is load-bearing: the block below CREATES ServeQuestionTool,
    # _ncea_parse_bank, the served-ledger write and the reviewed-code set, and
    # every edit after it narrows text that does not exist until it has landed.
    ("deeptutor/tools/builtin/__init__.py", "class ServeQuestionTool",
     EDIT_TOOLS_OLD, EDIT_TOOLS_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"serve_question",\n    "log_mistake",',
     EDIT_TUPLE_OLD, EDIT_TUPLE_NEW),
    ("deeptutor/tools/builtin/__init__.py", "Grade on the RAW entry text",
     EDIT_S11B_OLD, EDIT_S11B_NEW),
    ("deeptutor/tools/builtin/__init__.py", "(?<![0-9])(19|20)",
     EDIT_YEARFIX_OLD, EDIT_YEARFIX_NEW),
    ("deeptutor/tools/builtin/__init__.py", "Re-serving the entry served",
     EDIT_RECYCLE_OLD, EDIT_RECYCLE_NEW),
    ("deeptutor/tools/builtin/__init__.py", "_ncea_cur_turn",
     EDIT_SERVELEDGER_OLD, EDIT_SERVELEDGER_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"92022", "92023", "92046"',
     EDIT_ALLOWY11_OLD, EDIT_ALLOWY11_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"91261", "91262", "91267"',
     EDIT_ALLOWM2_OLD, EDIT_ALLOWM2_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"91156", "91157", "91159", "91603", "91605", "91606"',
     EDIT_ALLOWBIO_OLD, EDIT_ALLOWBIO_NEW),
    ("deeptutor/tools/builtin/__init__.py", '"91586"',
     EDIT_ALLOWSTAT_OLD, EDIT_ALLOWSTAT_NEW),
    ("deeptutor/tools/builtin/__init__.py", "_NCEA_NEVER_SERVE",
     EDIT_NEVERSET_OLD, EDIT_NEVERSET_NEW),
    ("deeptutor/tools/builtin/__init__.py", "if code in _NCEA_NEVER_SERVE",
     EDIT_NEVERCHECK_OLD, EDIT_NEVERCHECK_NEW),
    # The catalog is assembled from the module manifests now, so registration is
    # an append to the spec tuple instead of an entry in a literal list.
    ("deeptutor/tools/builtin_specs.py", 'BuiltinToolSpec("serve_question"',
     EDIT170_SPECS_OLD, EDIT170_SPECS_NEW),
    # -- deterministic injection into the loop -------------------------------
    ("deeptutor/agents/loop/pipeline.py", "_retrieve_ncea_serve_block",
     EDIT_SERVEBLOCK_OLD, EDIT_SERVEBLOCK_NEW),
    ("deeptutor/agents/loop/pipeline.py", "guessing by wall clock",
     EDIT_SERVEBLOCK2_OLD, EDIT_SERVEBLOCK2_NEW),
    ("deeptutor/agents/loop/pipeline.py", "ncea grade block failed",
     EDIT_GRADEPIPE_OLD, EDIT_GRADEPIPE_NEW),
    ("deeptutor/agents/loop/pipeline.py", "key injection from an earlier one",
     EDIT_GRADEPIPE2_OLD, EDIT_GRADEPIPE2_NEW),
    ("deeptutor/agents/loop/pipeline.py", "ncea review block failed",
     EDIT_REVIEWPIPE_OLD, EDIT_REVIEWPIPE_NEW),
    ("deeptutor/agents/loop/agent_loop.py", "ncea_serve_block = await",
     EDIT170_SEEDJOIN_OLD, EDIT170_SEEDJOIN_NEW),
    ("deeptutor/agents/loop/agent_loop.py", "ncea_grade_block.strip()",
     EDIT170_SEEDJOIN2_OLD, EDIT170_SEEDJOIN2_NEW),
    # -- the gate itself -----------------------------------------------------
    ("deeptutor/services/partners/runtime.py", "ncea jev gate skipped",
     EDIT170_HOOKS_OLD, EDIT170_HOOKS_NEW),
    # -- scope clamps --------------------------------------------------------
    ("deeptutor/services/partners/runtime.py", "partner KB scope clamp skipped",
     EDIT170_PARTNERCLAMP_OLD, EDIT170_PARTNERCLAMP_NEW),
    ("deeptutor/services/partners/runtime.py", "metadata[\"_ncea_student_uid\"] = _ncea_uid",
     EDIT170_UIDSTAMP_OLD, EDIT170_UIDSTAMP_NEW),
    ("deeptutor/services/session/turns/request_preparer.py", "ncea KB scope clamp skipped",
     EDIT170_SCOPECLAMP_OLD, EDIT170_SCOPECLAMP_NEW),
    # -- photo-grading as an assessment source -------------------------------
    ("deeptutor/core/assessment.py", '"photo-grade",',
     EDIT170_ASSESS_OLD, EDIT170_ASSESS_NEW),
    # -- latency: the wall-clock ceiling, now in the scheduler ---------------
    ("deeptutor/agents/loop/python_scheduler.py", "import time\nfrom typing import Any",
     EDIT170_SCHED_IMPORT_OLD, EDIT170_SCHED_IMPORT_NEW),
    ("deeptutor/agents/loop/python_scheduler.py", "budget_s = int(config.get",
     EDIT170_SCHED_DEADLINE_OLD, EDIT170_SCHED_DEADLINE_NEW),
    ("deeptutor/agents/loop/python_scheduler.py", "settlement_limit = 1 if over_budget",
     EDIT170_SCHED_SETTLE_OLD, EDIT170_SCHED_SETTLE_NEW),
    ("deeptutor/agents/loop/agent_loop.py", '"timeBudgetS"',
     EDIT170_RUNLOOP_OLD, EDIT170_RUNLOOP_NEW),
    # -- latency patches that still anchor unchanged --------------------------
    ("deeptutor/services/config/loader.py", '"time_budget_s": 120,',
     EDIT_CHATTIMEBUDGET_OLD, EDIT_CHATTIMEBUDGET_NEW),
    ("deeptutor/agents/loop/pipeline.py", "DEFAULT_TIME_BUDGET_S = 120",
     EDIT_LOOPBUDGET_CONST_OLD, EDIT_LOOPBUDGET_CONST_NEW),
    ("deeptutor/agents/loop/pipeline.py", 'key="time_budget_s"',
     EDIT_LOOPBUDGET_INIT_OLD, EDIT_LOOPBUDGET_INIT_NEW),
    ("deeptutor/agents/loop/pipeline.py", "def time_budget_s",
     EDIT_LOOPBUDGET_PROP_OLD, EDIT_LOOPBUDGET_PROP_NEW),
    ("deeptutor/agents/chat/prompts/en/agentic_chat.yaml", "loop_settlement_time",
     EDIT_SETTLENOTICE_EN_OLD, EDIT_SETTLENOTICE_EN_NEW),
    ("deeptutor/agents/chat/prompts/zh/agentic_chat.yaml", "loop_settlement_time",
     EDIT_SETTLENOTICE_ZH_OLD, EDIT_SETTLENOTICE_ZH_NEW),
    ("deeptutor/agents/loop/pipeline.py", "KB_SEED_ALWAYS_IF_FEW",
     EDIT_SEEDRANK_CONST_OLD, EDIT_SEEDRANK_CONST_NEW),
    ("deeptutor/agents/loop/pipeline.py", "def _seed_kbs(",
     EDIT_SEEDRANK_HELPERS_OLD, EDIT_SEEDRANK_HELPERS_NEW),
    ("deeptutor/agents/loop/pipeline.py", "_seed_kbs(kbs, query)",
     EDIT_SEEDRANK_CALL_OLD, EDIT_SEEDRANK_CALL_NEW),
    ("deeptutor/agents/loop/pipeline.py", "名称见下方清单",
     EDIT_KBNOTE_RAGNOTE_OLD, EDIT_KBNOTE_RAGNOTE_NEW),
    # -- fail-closed auth: a damaged store must never read as "no users" ------
    # Unchanged text on 1.7.0, so these are the 1.6.7 constants verbatim. They
    # are load-bearing: without them a corrupt users.json reads as zero users,
    # the first /register becomes admin, and the next save overwrites the
    # damaged file - the one failure that cannot be undone from a backup.
    ("deeptutor/multi_user/identity.py", "class CorruptUserStoreError",
     EDIT_IDENTITY_EXC_OLD, EDIT_IDENTITY_EXC_NEW),
    ("deeptutor/multi_user/identity.py", "def _read_json(path: Path, *, strict: bool = False",
     EDIT_READ_JSON_OLD, EDIT_READ_JSON_NEW),
    ("deeptutor/multi_user/identity.py", "_read_json(USERS_FILE, strict=True)",
     EDIT_LOAD_USERS_OLD, EDIT_LOAD_USERS_NEW),
    # Upstream 1.7.0 hardened load_grant/save_grant itself, so only the name our
    # callers use has to be restored (see EDIT170_GRANT_EXC_NEW).
    ("deeptutor/multi_user/grants.py", "CorruptGrantError = GrantStorageError",
     EDIT170_GRANT_EXC_OLD, EDIT170_GRANT_EXC_NEW),
    # -- a disabled account must not authenticate, even with a valid token -----
    ("deeptutor/services/auth.py", "refused for disabled account",
     EDIT_AUTHDISABLE_LOGIN_OLD, EDIT_AUTHDISABLE_LOGIN_NEW),
    ("deeptutor/services/auth.py", "die at next request",
     EDIT_AUTHDISABLE_TOKEN_OLD, EDIT_AUTHDISABLE_TOKEN_NEW),
    # -- the KB inventory: counts stay, file names go -------------------------
    # One block was ~10.5k tokens against a 16k window on the AU partner (73
    # KBs / 547 documents) and was re-sent on every round of every turn, plus
    # 73 manifest reads per turn to build it. Counts are what the block is for;
    # the names stay one call away through kb_files. Order matters: the READY
    # constant, then the compact authority paragraph, then the signature, then
    # the line that uses the constant.
    ("deeptutor/knowledge/manifest.py", '_READY = "ready"',
     EDIT_READY_CONST_OLD, EDIT_READY_CONST_NEW),
    ("deeptutor/knowledge/manifest.py", "File names are deliberately not listed here",
     EDIT_KBNOTE_TEXT_EN_OLD, EDIT_KBNOTE_TEXT_EN_NEW),
    ("deeptutor/knowledge/manifest.py", '"文件名不在此列出',
     EDIT_KBNOTE_TEXT_ZH_OLD, EDIT_KBNOTE_TEXT_ZH_NEW),
    ("deeptutor/knowledge/manifest.py", 'text["authority_counts"]',
     EDIT_KBNOTE_FN_OLD, EDIT_KBNOTE_FN_NEW),
    ("deeptutor/knowledge/manifest.py", "Counts only, no names",
     EDIT_KBNOTE_NOTELINE_OLD, EDIT_KBNOTE_NOTELINE_NEW),
    ("deeptutor/agents/loop/pipeline.py", "include_documents=False",
     EDIT_KBNOTE_CALL_OLD, EDIT_KBNOTE_CALL_NEW),
    ("deeptutor/knowledge/manifest.py", "and manifest.status != _READY",
     EDIT_NOTELINE_QUAL_OLD, EDIT_NOTELINE_QUAL_NEW),
    # -- which budget ran out (the reason travels with the scheduler) ---------
    ("deeptutor/agents/loop/agent_loop.py", 'reason: str = "rounds"',
     EDIT_LOOPDEADLINE_SETTLE_OLD, EDIT_LOOPDEADLINE_SETTLE_NEW),
    ("deeptutor/agents/loop/loop_host.py", 'async def begin_settlement(self, reason: str = "rounds")',
     EDIT170_SETTLE_REASON_OLD, EDIT170_SETTLE_REASON_NEW),
    ("deeptutor/agents/loop/python_scheduler.py", 'reason="time" if over_budget',
     EDIT170_SCHED_SETTLE_CALL_OLD, EDIT170_SCHED_SETTLE_CALL_NEW),
    # -- public demo: Google sign-in + 15-day trial --------------------------
    # Ported from the 1.6.7 table: the three partners.py gate edits are stock
    # text in both releases; only the mount site moved (see
    # EDIT_DEMO_MOUNT_*). The trial gate must wrap the chat routes or the
    # demo would serve turns after day 15.
    ("deeptutor/api/main.py", "app.include_router(ncea_demo.router)",
     EDIT_DEMO_MOUNT_OLD, EDIT_DEMO_MOUNT_NEW),
    ("deeptutor/api/routers/partners.py", "_TRIAL_GATE = [Depends(ensure_trial_active)]",
     EDIT_DEMO_GATE_OLD, EDIT_DEMO_GATE_NEW),
    ("deeptutor/api/routers/partners.py", 'chat", dependencies=[*_USABLE, *_TRIAL_GATE])',
     EDIT_DEMO_CHAT_OLD, EDIT_DEMO_CHAT_NEW),
    ("deeptutor/api/routers/partners.py", 'execute-stream", dependencies=[*_USABLE, *_TRIAL_GATE])',
     EDIT_DEMO_STREAM_OLD, EDIT_DEMO_STREAM_NEW),
    # Question page image (both releases: the serve tool's tail text is the
    # same patched block in 1.6.7 and 1.7.0).
    ("deeptutor/tools/builtin/__init__.py", "Question page image (site patch)",
     EDIT_QIMAGE_OLD, EDIT_QIMAGE_NEW),
    # One window per account + a student's own provider key. Last on purpose:
    # the main.py anchors are the demo router's import and include, which the
    # demo edits above create.
    ("deeptutor/services/partners/runtime.py", "_ncea_single_turn",
     EDIT_TURNLIMIT_IMPORT_OLD, EDIT_TURNLIMIT_IMPORT_NEW),
    ("deeptutor/services/partners/runtime.py", "_ncea_single_turn(msg.actor)",
     EDIT_TURNLIMIT_OLD, EDIT_TURNLIMIT_NEW),
    ("deeptutor/services/session/turns/request_preparer.py", "Prefer the student's OWN provider key",
     EDIT_OWNKEY_PREF_OLD, EDIT_OWNKEY_PREF_NEW),
    ("deeptutor/api/main.py", "ncea_personal_key",
     EDIT_OWNKEY_IMPORT_OLD, EDIT_OWNKEY_IMPORT_NEW),
    ("deeptutor/api/main.py", "ncea_personal_key.router",
     EDIT_OWNKEY_MOUNT_OLD, EDIT_OWNKEY_MOUNT_NEW),
    # Must follow the turn-limit import edit: the anchor is the shim it adds.
    ("deeptutor/services/partners/runtime.py",
     "from deeptutor.multi_user.ncea_own_key import own_key_scope",
     EDIT_OWNKEY_SCOPE_IMPORT_OLD, EDIT_OWNKEY_SCOPE_IMPORT_NEW),
    ("deeptutor/services/partners/runtime.py", "_ncea_own_cfg",
     EDIT_OWNKEY_SCOPE_OLD, EDIT_OWNKEY_SCOPE_NEW),
]


def edits_for(version: str) -> list[tuple[str, str, str, str]]:
    """The patch table for an installed deeptutor version."""
    if version == "1.7.0":
        return EDITS_170
    return EDITS


def find_site(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    here = Path(__file__).resolve()
    for cand in (
        here.parent.parent / ".venv" / "Lib" / "site-packages",   # Windows venv
        here.parent.parent / ".venv" / "lib",                     # Linux venv layout
    ):
        if (cand / "deeptutor").is_dir():
            return cand
        if cand.name == "lib":  # lib/python3.x/site-packages
            for sub in sorted(cand.glob("python*/site-packages")):
                if (sub / "deeptutor").is_dir():
                    return sub
    raise SystemExit("site-packages not found; pass it as argv[1]")


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def preflight_helpers() -> None:
    """Refuse to start while a helper SOURCE file is missing from scripts/.

    Why this exists (2026-09-21, AU production): a deploy shipped this
    installer but not scripts/ncea_output_guard.py. The EDITS had already
    been written when the helper loop reached the missing file and died, so
    production was left with five of six helpers installed and the output
    guard permanently absent - and nothing said so, because runtime.py
    imports that helper inside a try/except, turning the missing module into
    a SILENT no-op for the anti-fabrication gate.

    Checking before the first write makes that outcome unreachable: either
    every helper source is here, or nothing is touched at all.
    """
    drift = source_drift()
    if drift:
        raise SystemExit(
            "ABORT preflight: helper source(s) are not the committed version:\n"
            + "".join(f"  - {d}\n" for d in drift)
            + "Installing now would write these over the copies already in the "
              "venv - the exact way an old helper replaces a newer one. Ship "
              "the repo's scripts/ (or run --write-hashes if the change is "
              "intended), then re-run. Nothing was modified."
        )
    missing = [src for src, _ in HELPERS if not (_scripts_dir() / src).is_file()]
    if missing:
        raise SystemExit(
            "ABORT preflight: helper source file(s) missing from scripts/:\n"
            + "".join(f"  - {m}\n" for m in missing)
            + "A helper that is not in this directory cannot be installed, "
              "so deploying now would leave that protection layer absent "
              "while reporting success. Ship the source file(s) next to "
              "apply_site_patches.py and re-run. Nothing was modified."
        )


def install_helpers(site: Path) -> int:
    """Copy every helper source into the venv. Returns how many changed."""
    changed = 0
    for src, dst in HELPERS:
        source = _scripts_dir() / src
        if not source.is_file():          # preflight should have caught this
            raise SystemExit(f"ABORT {src}: helper source vanished mid-run.")
        target = site / dst
        content = source.read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == content:
            print(f"SKIP  {dst}  (helper already current)")
            continue
        target.write_text(content, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
        print(f"PATCH {dst}  (helper installed)")
        changed += 1
    return changed


def verify_helpers(site: Path) -> list[str]:
    """Problems with the INSTALLED helper set ([] when all good).

    Present, byte-identical to its source, and compiling. This is the check
    that was missing while production ran for weeks with ncea_output_guard
    absent: the hook was there, the module was not, and the runtime's
    fail-open made the gap invisible.
    """
    problems = []
    for src, dst in HELPERS:
        target = site / dst
        if not target.is_file():
            problems.append(f"{dst}: NOT INSTALLED")
            continue
        try:
            installed = target.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{dst}: unreadable ({exc})")
            continue
        source = _scripts_dir() / src
        if source.is_file() and installed != source.read_text(encoding="utf-8"):
            problems.append(f"{dst}: STALE (differs from scripts/{src})")
        try:
            py_compile.compile(str(target), doraise=True)
        except py_compile.PyCompileError as exc:
            problems.append(f"{dst}: DOES NOT COMPILE ({exc})")
    return problems


# --- the Google door on the pages students actually land on ----------------
# The demo entry lives at /demo, but an unauthenticated visit to the domain is
# sent to /login, which is a password form - and a Google account has no
# password here. On 2026-11-03 the operator hit exactly that and read it as
# "the Google sign-in is gone": nothing had removed it, it was simply never on
# the sign-in page. The sign-in pages are prerendered HTML inside the frontend
# package, so the door is added there, by a script that appends the button
# AFTER hydration - React never sees the node, so there is no mismatch to
# reconcile and no client-side re-render can drop it.
WEB_ENTRY_MARKER = "ncea-demo-entry"
WEB_ENTRY_PAGES = ("login.html", "register.html")
WEB_ENTRY_SCRIPT = r"""<script>/* ncea-demo-entry */
(function () {
  function place() {
    if (document.querySelector("[data-ncea-demo-entry]")) return;
    var link = document.querySelector('a[href^="/register"]')
            || document.querySelector('a[href^="/login"]');
    if (!link) { setTimeout(place, 400); return; }
    var row = link.parentNode;
    var box = document.createElement("div");
    box.setAttribute("data-ncea-demo-entry", "");
    box.style.cssText = "margin:22px auto 0;max-width:352px;"
      + "font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;text-align:center";
    box.innerHTML =
      '<div style="display:flex;align-items:center;gap:10px;color:#9ca3af;font-size:12px;margin:0 0 12px">'
      + '<span style="flex:1;height:1px;background:#e5e7eb"></span>or'
      + '<span style="flex:1;height:1px;background:#e5e7eb"></span></div>'
      + '<a href="/api/demo/google/start" style="display:flex;align-items:center;'
      + 'justify-content:center;gap:10px;padding:11px 16px;border:1px solid #d1d5db;'
      + 'border-radius:10px;background:#fff;color:#111827;font-weight:500;'
      + 'text-decoration:none;box-shadow:0 1px 2px rgba(16,24,40,.05)">'
      + 'Continue with Google</a>'
      + '<p style="margin:10px 0 0;color:#6b7280;font-size:12px">'
      + 'NCEA Tutor demo &mdash; 15 days free, no card</p>';
    row.parentNode.insertBefore(box, row.nextSibling);
  }
  function boot() { place(); setTimeout(place, 800); }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
</script>"""


def _web_app_dir(site: Path) -> Path:
    return site / "deeptutor_web" / ".next" / "server" / "app"


def _web_page_dirs(site: Path) -> list[Path]:
    """Every directory whose sign-in pages are SERVED, packaged copy first.

    `deeptutor start` does not serve the page from site-packages: it copies the
    package to <home>/data/<scope>/runtime/web and runs next-server there, and
    it only refreshes that copy when the packaged server.js mtime changes. So a
    patch to the package alone is invisible - measured on AU: the packaged
    login.html carried the door and the served page did not.
    """
    dirs = [_web_app_dir(site)]
    try:
        from deeptutor.runtime.home import get_runtime_home

        home = get_runtime_home()
    except Exception:  # noqa: BLE001 - auditing a tree with no deeptutor importable
        return dirs
    for cand in sorted(home.glob("data/**/runtime/web/.next/server/app")):
        if (cand / "login.html").is_file() and cand not in dirs:
            dirs.append(cand)
    return dirs


def install_web_demo_entry(site: Path) -> int:
    """Add the Google door to the sign-in pages. Returns how many changed."""
    packaged = _web_app_dir(site)
    if not any((packaged / name).is_file() for name in WEB_ENTRY_PAGES):
        raise SystemExit(
            f"ABORT: no sign-in page under {packaged} - the frontend package is "
            "not installed, so the Google door would be invisible while this "
            "run reported success. Nothing was modified."
        )
    changed = 0
    packaged_changed = False
    for app in _web_page_dirs(site):
        for name in WEB_ENTRY_PAGES:
            page = app / name
            if not page.is_file():
                continue
            html = page.read_text(encoding="utf-8")
            if WEB_ENTRY_MARKER in html:
                print(f"SKIP  {name}  (Google door already present: {app})")
                continue
            if "</body></html>" not in html:
                raise SystemExit(
                    f"ABORT {page}: the </body></html> anchor is missing - the "
                    "frontend build changed, so this page was left alone."
                )
            page.write_text(
                html.replace("</body></html>", WEB_ENTRY_SCRIPT + "</body></html>", 1),
                encoding="utf-8",
            )
            print(f"PATCH {name}  (Google door on the sign-in page: {app})")
            changed += 1
            packaged_changed = packaged_changed or app == packaged
    if packaged_changed:
        # The launcher decides whether to refresh a runtime copy by comparing
        # the packaged server.js mtime. Bump it, or a copy made before this
        # deploy keeps serving a page without the door for the rest of its life.
        server = site / "deeptutor_web" / "server.js"
        if server.is_file():
            server.touch()
            print(f"TOUCH {server.name}  (forces the runtime web copy to refresh)")
    return changed


def verify_web_demo_entry(site: Path) -> list[str]:
    """Problems with the Google door on the sign-in pages ([] when good)."""
    app = _web_app_dir(site)
    pages = [app / name for name in WEB_ENTRY_PAGES]
    # Runtime copies are what students actually load; a door only in the
    # package is a door nobody can see.
    for runtime in _web_page_dirs(site)[1:]:
        for name in WEB_ENTRY_PAGES:
            if (runtime / name).is_file():
                pages.append(runtime / name)
    problems = []
    for page in pages:
        if not page.is_file():
            problems.append(f"{page.name}: MISSING (frontend package not installed?)")
            continue
        if WEB_ENTRY_MARKER not in page.read_text(encoding="utf-8"):
            problems.append(f"{page.parent}: no Google sign-in entry")
    return problems


def report_helper_state(site: Path) -> int:
    """Print the installed-helper audit. Returns 0 when healthy, else 1."""
    problems = verify_helpers(site) + source_drift() + verify_web_demo_entry(site)
    if not problems:
        print(f"HELPERS OK: {len(HELPERS)}/{len(HELPERS)} installed, "
              "current, compiling.")
        return 0
    print("HELPERS INCOMPLETE - the app fails OPEN on every missing layer,"
          " so this looks like 'no problems' in the logs:")
    for p in problems:
        print(f"  - {p}")
    return 1



def main() -> None:
    raw = sys.argv[1:]
    verify_only = "--verify" in raw
    if "--write-hashes" in raw:
        raise SystemExit(write_helper_manifest())
    # Code-only deploy. The EDITS and the helpers are separate tracks: a
    # helper change is a behaviour change (the JEV gate, the output guard),
    # and shipping one is a decision of its own. Without this flag the only
    # way to land an edit was to sync every helper with it, so a repo whose
    # helper sources are AHEAD of production could not be patched at all.
    edits_only = "--edits-only" in raw
    argv = [a for a in raw if a not in ("--verify", "--edits-only")]
    # Patching a target tree whose dist metadata this interpreter cannot see
    # (a staged wheel tree, or a copy used for a dry run) - name the release
    # explicitly. The patch table is chosen from this value, so never guess it.
    assume_version = ""
    if "--assume-version" in argv:
        i = argv.index("--assume-version")
        assume_version = argv[i + 1]
        del argv[i : i + 2]
    site = find_site(argv[0] if argv else None)
    if verify_only:
        # Standalone audit: confirm every helper is installed, current and
        # compiling, without touching anything. This is the check that would
        # have caught the absent output guard in production.
        raise SystemExit(report_helper_state(site))
    try:
        ver = importlib.metadata.version("deeptutor")
    except importlib.metadata.PackageNotFoundError:
        ver = "unknown (source install?)"
    if assume_version:
        ver = assume_version
    if ver not in SUPPORTED_VERSIONS:
        raise SystemExit(
            f"deeptutor version is {ver}; this installer knows "
            f"{', '.join(SUPPORTED_VERSIONS)}. Read scripts/patches/*.patch "
            "first — upstream may have fixed this."
        )
    edits = edits_for(ver)
    # Before ANY write: every helper source must be present. A missing one
    # used to be discovered halfway through, after the EDITS had landed and
    # with a fail-open runtime quietly hiding the hole (see preflight_helpers).
    if edits_only:
        print(
            "--edits-only: the venv KEEPS its installed helpers. The EDITS "
            "below are\n"
            "             applied; helper sources in scripts/ are NOT copied. "
            "Only correct\n"
            "             when the installed helpers are already the version "
            "you mean to run.\n"
        )
    else:
        preflight_helpers()
    changed = 0
    print(f"deeptutor {ver}: applying the {len(edits)}-edit table for this release.\n")
    try:
        for rel, marker, old, new in edits:
            target = site / rel
            text = target.read_text(encoding="utf-8")
            if marker in text:
                print(f"SKIP  {rel}  (already patched)")
                continue
            if old not in text:
                raise SystemExit(
                    f"ABORT {rel}: stock text not found and marker absent — "
                    "version drift. No files were half-modified "
                    f"(this edit not applied: {marker[:40]}...)."
                )
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            # Only Python can be byte-compiled. An edit may also land in a
            # prompt pack (.yaml), and compiling that aborted the run AFTER
            # the write - the half-applied state this script exists to avoid.
            if target.suffix == ".py":
                py_compile.compile(str(target), doraise=True)
            print(f"PATCH {rel}")
            changed += 1
        # NCEA helpers (separate entries: parallel tracks, do not merge).
        if not edits_only:
            changed += install_helpers(site)
            changed += install_web_demo_entry(site)
    except BaseException:
        # A crash from here leaves the EDITS applied and the helper set
        # partly installed - exactly the state that hid the absent output
        # guard in production. Never exit quietly from it.
        print()
        report_helper_state(site)
        raise
    ok = report_helper_state(site)
    print(f"done: {changed} edit(s) applied, rest already present.")
    if ok and not edits_only:
        raise SystemExit(1)
    if edits_only:
        # The audit above is informational here: the flag's whole purpose is
        # to leave the helper set alone. It must still be READ, because a
        # missing helper is a hole and a stale one is a different program -
        # but it is not this run's failure.
        print(
            "NOTE  --edits-only: helper state above was audited, NOT changed. "
            "Any\n"
            "      STALE/MISSING line is a deliberate difference from "
            "scripts/, not\n"
            "      a failure of this run."
        )


if __name__ == "__main__":
    main()
