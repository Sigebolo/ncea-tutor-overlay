# NCEA Tutor — a standards-aware overlay for DeepTutor

An overlay for [DeepTutor](https://github.com/HKUDS/DeepTutor) (HKUDS, Apache-2.0)
that turns a general tutoring agent into an **NCEA / NZQA Achievement Standard**
tutor: it grades against verified answers, refuses to release a grade it cannot
back with evidence, and keeps a locator index instead of reprinting exam papers.

This repository contains **only the overlay**. It is not a deployment, and it
bundles no third-party content.

## What is here

| Path | Purpose |
|---|---|
| `scripts/apply_site_patches.py` | Idempotent patches applied to the installed `deeptutor` package |
| `scripts/ncea_grade_block.py` | Injects the verified-answer block into a grading turn |
| `scripts/ncea_jev_gate.py` | Blocks any reply whose grade is not backed by verified evidence |
| `scripts/nightly_numbers.py`, `scripts/nightly_review.py` | Offline audits over stored transcripts |
| `scripts/expiry_auto_disable.py` | Trial expiry sweep |
| `skills/ncea-tutor/` | Agent skill definition and the question-locator index |
| `alignment/` | Teaching plans and the standard-to-skill mapping |
| `tests/` | Unit tests for the gate, the guard, the audit rules and the patcher |

## What is deliberately not here

- **No exam papers.** NZQA papers are Crown copyright. The index records the
  *location* (year, question, part), the verified answer and the verification
  level — never the question text.
- **No student data.** No transcripts, accounts, e-mail addresses or ledgers.
- **No deployment state.** No hostnames, credentials, model keys or grants.

## Install

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install "deeptutor==1.7.0" pytest
python scripts/apply_site_patches.py     # patch the installed package
python -m pytest tests -q                # 8 suites
```

The patcher is idempotent: re-running it re-applies the same edits and reports
which ones were already present. It patches the installed package rather than a
checked-out tree, so it must run in the same environment as `deeptutor`.

## Design rules

1. A grade may only be released when the turn carries verified evidence.
   `ncea_jev_gate.py` enforces this: a reply with `verdict=guard` never reaches
   the student.
2. The tutor names a question by location; the student works from their own copy
   of the paper.
3. Arithmetic inside a verified answer is re-checked before it is served.
4. Every site patch lives in `apply_site_patches.py`, so an upgrade can be
   re-applied and reviewed instead of lost in `site-packages`.

## License

Apache-2.0. See `LICENSE` and `NOTICE`. Built on DeepTutor by HKUDS.
