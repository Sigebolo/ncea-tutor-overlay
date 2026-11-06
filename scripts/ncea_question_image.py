"""Question-page image for a served NCEA question (site helper).

A served bank entry knows the paper file and the question location, but the
chat only ever showed text plus a PDF link - so a student without the paper
("I don't have the paper. You need show me the question and picture it had"
- Mark, 2026-10-31) never saw the question WITH its diagram, and the model
was left to improvise figures with imagegen.

This helper closes that gap: fetch the exam PDF (once, cached per box),
find the page that starts the question, render that page to a PNG inside
the student's own workspace, and publish it through the same
workspace_present machinery the chat UI already understands - so the reply
carries an openable image card, and the SERVED block tells the model to
copy the Markdown image line verbatim so the picture renders inline.

Everything is best-effort and fail-open by design: no PDF link, no
renderer, no workspace binding, no network - the helper returns
("", [], []) and the serve degrades to text + link exactly as before.
Never raise into the turn.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# "Question One (a)" / "Question 3(b)" -> the word or digit that starts it.
QNUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

DOWNLOAD_TIMEOUT_S = 30
DOWNLOAD_CAP_BYTES = 25 * 1024 * 1024
RENDER_ZOOM = 1.6  # ~115 dpi: legible maths typesetting, ~300-600KB PNG


def _question_number(loc: str) -> int:
    """Question number from a bank Location, 0 when unparseable."""
    m = re.search(r"question\s*(one|two|three|four|five|six|seven|eight|nine|ten|\d+)",
                  loc or "", re.I)
    if not m:
        return 0
    token = m.group(1).lower()
    return QNUM_WORDS.get(token) or (int(token) if token.isdigit() else 0)


def _heading_re(number: int) -> re.Pattern:
    """Matches the question heading as NCEA prints it, in any casing."""
    words = [w for w, n in QNUM_WORDS.items() if n == number]
    alt = "|".join([str(number)] + [w.capitalize() for w in words] + words)
    return re.compile(rf"question\s*(?:#)?\s*({alt})\b", re.I)


# Any question heading, used as the FORWARD BOUND when walking to a sub-part:
# a part we cannot find must never be satisfied by the NEXT question's part.
# The number is captured so a CONTINUATION header of the SAME question (NCEA
# repeats "Question Three" atop the page a question carries over to) does not
# truncate the walk - only a different number does.
_ANY_HEADING_RE = re.compile(
    r"question\s*(?:#)?\s*(one|two|three|four|five|six|seven|eight|nine|ten|\d+)",
    re.I,
)


def _heading_number(token: str) -> int:
    """Heading capture (digit or word) -> the question number it names."""
    t = (token or "").lower()
    return int(t) if t.isdigit() else QNUM_WORDS.get(t, 0)


def _question_part(loc: str) -> str:
    """Sub-part letter from a bank Location ("Question Two (c)" -> "c").

    NCEA prints each part on its own page: the heading page is part (a). A
    bank entry for "Question Two (c)" therefore used to serve page 8 (the
    triangle for (a)) beside a factorisation question printed on page 10 -
    the student saw a diagram belonging to a different sub-question.
    """
    m = re.search(r"question\b[^()]*\(([a-z])\)", loc or "", re.I)
    return m.group(1).lower() if m else ""


def _fetch_pdf(url: str, cache_dir: Path) -> Path | None:
    """Cached copy of the exam paper, downloaded once per box."""
    if not url:
        return None
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if not name.lower().endswith(".pdf"):
        return None
    dest = cache_dir / name
    if dest.is_file() and dest.stat().st_size > 1024:
        return dest
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ncea-tutor)"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            blob = resp.read(DOWNLOAD_CAP_BYTES + 1)
        if len(blob) > DOWNLOAD_CAP_BYTES or not blob.startswith(b"%PDF"):
            logger.warning("ncea_question_image: bad download for %s", url)
            return None
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(blob)
        os.replace(tmp, dest)
        return dest
    except Exception:
        logger.warning("ncea_question_image: download failed for %s", url, exc_info=True)
        return None


def _render_question_page(pdf: Path, number: int,
                          part: str = "") -> tuple[Path, int] | None:
    """PNG of the page the served sub-part is printed on, next to the PDF.

    The heading is the anchor: banks record the question (not the page), and
    NCEA prints "Question ONE" at the start of every question. A page whose
    only mention is a later cross-reference never wins - first match does.

    When the Location also names a part, walk forward from the heading page
    to the page that OPENS with that part, stopping at the next question's
    heading - so a part we cannot find degrades to the heading page instead
    of picking up the following question's identically-lettered part.
    """
    if not number:
        return None
    try:
        import pymupdf
    except Exception:
        logger.warning("ncea_question_image: pymupdf not installed")
        return None
    try:
        doc = pymupdf.open(pdf)
        try:
            heading = _heading_re(number)
            page_no = None
            for i in range(doc.page_count):
                if heading.search(doc[i].get_text()):
                    page_no = i
                    break
            if page_no is None:
                return None
            if part:
                part_re = re.compile(
                    rf"^\s*\(\s*{re.escape(part.lower())}\s*\)", re.I | re.M)
                for i in range(page_no, doc.page_count):
                    text = doc[i].get_text()
                    if i > page_no:
                        other = _ANY_HEADING_RE.search(text)
                        if other and _heading_number(other.group(1)) not in (0, number):
                            break  # a DIFFERENT question: keep the heading page
                    if part_re.search(text):
                        page_no = i
                        break
            out = pdf.with_name(
                f"{pdf.stem}-q{number}{part.lower()}-p{page_no + 1}.png")
            if out.is_file() and out.stat().st_size > 10_000:
                return out, page_no + 1  # already rendered for this paper
            pix = doc[page_no].get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM))
            pix.save(out)
            return out, page_no + 1
        finally:
            doc.close()
    except Exception:
        logger.warning("ncea_question_image: render failed for %s q%s",
                       pdf.name, number, exc_info=True)
        return None


async def present_question_image(*, loc: str, pdf_url: str, code: str,
                                 entry_id: str) -> tuple[str, list, list]:
    """Extract, present, and describe the question-page image.

    Returns ``(note, sources, workspace_items)``; every failure degrades to
    ("", [], []) and the serve stays text + link.
    """
    number = _question_number(loc)
    if not number:
        return "", [], []
    part = _question_part(loc)
    from deeptutor.runtime.home import get_runtime_home

    cache_dir = get_runtime_home() / "data" / "cache" / "ncea_pdfs"
    pdf = _fetch_pdf(pdf_url, cache_dir)
    if pdf is None:
        return "", [], []
    rendered = _render_question_page(pdf, number, part)
    if rendered is None:
        return "", [], []
    png, page_no = rendered
    try:
        from deeptutor.services.workspace import get_content_workspace_service

        service = get_content_workspace_service()
        binding = service.current_binding()
        rel_dir = PurePosixPath("outputs") / "ncea_questions"
        target_dir = service.resolve(binding, str(rel_dir))
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        name = f"{(entry_id or f'{code}-q{number}').replace('/', '-')}-p{page_no}.png"
        target = target_dir / name
        # Copy, not move: the cached render is reused by the next student
        # served the same question.
        target.write_bytes(png.read_bytes())
        rel = str(rel_dir / name)

        from deeptutor.tools.workspace import WorkspacePresentTool

        presented = await WorkspacePresentTool().execute(
            items=[{
                "path": rel,
                "title": f"{loc}",
                "caption": (f"Question page {page_no} extracted from the official "
                            f"NZQA paper ({code}) so you can read the question "
                            f"with its original diagram."),
            }]
        )
        rows = list((presented.metadata or {}).get("workspace_items") or [])
        sources = list(getattr(presented, "sources", None) or [])
        if not rows:
            # Publish refused (no workspace binding in this context, path
            # unresolvable, ...). Fail open - but say WHY, loudly: the first
            # production run failed exactly here and "" looked identical to
            # "helper never ran".
            logger.warning(
                "ncea_question_image: workspace_present returned no rows for %s "
                "(success=%s): %s | %s",
                entry_id, getattr(presented, "success", "?"),
                str(getattr(presented, "content", ""))[:300],
                (presented.metadata or {}),
            )
            return "", [], []
        # The bare workspace-relative path only resolves in the browser when
        # the message carries the attachment record - and in the
        # deterministic serve path nothing in the loop's event stream ever
        # sees this publish, so the record is absent and the img 404s (seen
        # on AU: rendered as a broken-image card). The item's own absolute
        # URL needs no attachment record: same-origin, session-authenticated.
        img_url = str(rows[0].get("url") or "")
        if not img_url.startswith("/"):
            img_url = rel
        note = (
            f"[QUESTION PAGE IMAGE] page {page_no} of the official NZQA paper, "
            f"extracted and already presented as `{rel}` so the student can "
            f"see the question with its original diagram.\n"
            f"Your reply MUST begin with exactly this line (copy it verbatim, "
            f"then continue normally):\n"
            f"![Question page {page_no} - {loc}]({img_url})"
        )
        return note, sources, rows
    except Exception:
        logger.warning("ncea_question_image: present failed for %s", entry_id,
                       exc_info=True)
        try:
            png.unlink(missing_ok=True)
        except Exception:
            pass
        return "", [], []


if __name__ == "__main__":
    # Offline self-test: python ncea_question_image.py <pdf> <location>
    import sys

    sys.argv  # usage: selftest renders straight from a local PDF, no server
    _pdf = Path(sys.argv[1])
    _loc = sys.argv[2]
    _out = _render_question_page(_pdf, _question_number(_loc),
                                 _question_part(_loc))
    if _out is None:
        print("FAIL: no page found")
        raise SystemExit(1)
    _png, _page = _out
    print(f"OK: question on page {_page}, {_png} ({_png.stat().st_size} bytes)")
