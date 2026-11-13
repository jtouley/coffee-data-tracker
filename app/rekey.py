"""Rotate EMAIL_KEY: re-seal every member's email under a new key (docs/app/RUNNING.md, "Email", "Rotating keys").

    EMAIL_KEY=<current> EMAIL_KEY_NEW=<new> HF_TOKEN=... COFFEEDATA_USERS=hf://datasets/<owner>/<users> \\
        uv run python -m app.rekey
    EMAIL_KEY=<new> ... uv run python -m app.rekey --check     # every email opens with this key

Reads the keys from the environment only, never the command line, and prints counts, never an address.
"""

from __future__ import annotations

import os
import secrets
import sys

from coffeedata.lake import Lake

from .accounts import Accounts


def main(argv: list[str]) -> int:
    key = os.environ.get("EMAIL_KEY", "").strip()
    if not key:
        print("Set EMAIL_KEY (the current key).", file=sys.stderr)
        return 2
    # Only the contacts are touched: the pepper and session secret aren't needed, so throwaway ones stand in.
    accounts = Accounts(Lake.users(), secrets.token_hex(16), secrets.token_hex(32), None, email_key=key)
    if "--check" in argv:
        bad = accounts.unreadable()
        print(
            f"{len(accounts.contacts) - len(bad)} of {len(accounts.contacts)} emails open with this EMAIL_KEY."
        )
        return 1 if bad else 0
    new = os.environ.get("EMAIL_KEY_NEW", "").strip()
    if not new or new == key:
        print("Set EMAIL_KEY_NEW to the new key (openssl rand -base64 32).", file=sys.stderr)
        return 2
    if accounts.unreadable():
        print(
            "Some emails don't open with EMAIL_KEY: is it the current key? Nothing changed.", file=sys.stderr
        )
        return 1
    n = accounts.rekey(new)
    print(f"Re-sealed {n} emails. Now put EMAIL_KEY_NEW in the EMAIL_KEY secret and deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
