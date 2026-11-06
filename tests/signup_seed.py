"""Accounts for the sign-up browser flow (tests/browser/signup_flow.js): an owner, a member, and join links in every
state, so the invite list shows open, used, revoked and expired rows. Used by tests/test_signup_browser.py."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from app.accounts import Accounts, _now
from coffeedata.lake import USER_TABLES, Lake

OWNER, OWNER_PW = "jtouley", "kettle orbit marble"
MEMBER, MEMBER_PW = "maya", "lantern quarry velvet"
SETUP = "SETUP-CODE-1234"
OLD_TERMS = "November 2, 2026"


def accounts(root: Path, pepper: str, secret: str, legal: str | None = None) -> Accounts:
    return Accounts(Lake(str(root), tables=USER_TABLES), pepper, secret, SETUP, legal_version=legal)


def seed(root: Path, pepper: str, secret: str) -> None:
    """Everyone accepted the terms dated OLD_TERMS."""
    a = accounts(root, pepper, secret, OLD_TERMS)
    owner = a.create_owner(SETUP, OWNER, OWNER_PW, agree=True)
    _, code = a.invite(owner.user_id, for_label="Maya, from the roastery")
    a.join(code, MEMBER, MEMBER_PW, "127.0.0.1", agree=True)
    a.invite(owner.user_id, for_label="Sam from the café")
    old, _ = a.invite(owner.user_id, for_label="Old link for Ana")
    a.revoke(owner.user_id, old.invite_id)
    lee, _ = a.invite(owner.user_id, for_label="Lee")
    now = _now()
    a._write_invite(replace(lee, created_at=now - timedelta(days=9), expires_at=now - timedelta(days=2)))
