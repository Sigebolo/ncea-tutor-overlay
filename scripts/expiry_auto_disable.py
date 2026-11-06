"""Expiry auto-disable for trial accounts (Zac 9/16, Elise 9/17 - year-2026 gated).

Owner decision 2026-09-16: expiry closes silently - NO email/WeChat reminder,
no 3-day warning pass. This script is the whole mechanism:

  1. On/after an account's expiry date it flips ``disabled: true`` in
     ``data/system/auth/users.json`` (atomic write, same store the admin UI reads).
  2. Login refuses disabled accounts - via the vendored authenticate() patch
     (scripts/apply_site_patches.py EDIT_AUTHDISABLE*), which 1.6.7 stock needs
     because its ``disabled`` field is display-only (verified in source).

Idempotent: re-running after expiry flips nothing twice; reviving is an
explicit ops action (edit users.json / admin UI). Chat history is kept.

Cron (installed alongside the nightly audit):
    50 21 * * *  cd /home/Administrator/ncea-tutor && .venv/bin/python scripts/expiry_auto_disable.py >> data/cron/expiry_disable.log 2>&1
(21:50 Australia/Sydney = 11:50 UTC, the day BEFORE each expiry wall-crossing
in UTC terms; the script itself is date-exact either way.)

Run manually:  .venv/bin/python scripts/expiry_auto_disable.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Expiry walls (local NZ/AU calendar days, owner-set 2026-09-15).
TRIAL_EXPIRY = {
    "Zac": "2026-09-16",
    "Elise": "2026-09-17",
}
# Guard: the schedule is 2026-specific; refuse to disable anyone if the
# machine clock is in another year (stops a stale cron from nuking accounts
# in 2027 when nobody remembers this file).
GATE_YEAR = 2026

HERE = os.path.dirname(os.path.abspath(__file__))


def users_file() -> str:
    return os.path.join(HERE, "..", "data", "system", "auth", "users.json")


def load_store() -> dict:
    p = users_file()
    if not os.path.exists(p):
        return {}
    try:
        data = json.load(open(p, encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"ERROR unreadable users store ({exc}); aborting without changes")
        sys.exit(2)


def _write_users(users: dict) -> None:
    """Atomic write to the SAME file identity uses (duplicate of identity's
    private helper so the script works even if internals move)."""
    from deeptutor.services.file_io import atomic_write_text

    p = os.path.abspath(users_file())
    os.makedirs(os.path.dirname(p), exist_ok=True)
    atomic_write_text(p, json.dumps(users, indent=2, ensure_ascii=False))


def main() -> int:
    today = date.today()
    if "--date" in sys.argv:  # deterministic test hook
        today = date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    print(f"=== expiry auto-disable run {datetime.now(timezone.utc).isoformat()} "
          f"(effective date {today.isoformat()}) ===")
    if today.year != GATE_YEAR:
        print(f"year {today.year} != {GATE_YEAR}: schedule expired, doing nothing")
        return 0

    users = load_store()
    if not users:
        print("no users store; nothing to do")
        return 0

    changed = 0
    for username, iso in TRIAL_EXPIRY.items():
        record = users.get(username)
        if not isinstance(record, dict):
            print(f"skip {username}: no account in store")
            continue
        if record.get("disabled"):
            print(f"ok {username}: already disabled")
            continue
        expiry = date.fromisoformat(iso)
        if today < expiry:
            print(f"keep {username}: trial active until {iso}")
            continue
        record["disabled"] = True
        changed += 1
        print(f"DISABLED {username} (trial ended {iso})")

    if changed:
        _write_users(users)
        print(f"store written: {changed} account(s) disabled")
    else:
        print("no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
