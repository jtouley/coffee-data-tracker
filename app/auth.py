"""Who is making a request. The plan's sign-in (username, password, invite codes, signed session cookies) goes
behind this interface in a later milestone; for now there is only the local-development stub."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from coffeedata import shots


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    role: str = "member"  # owner or member


class Auth(Protocol):
    def user(self, request: Request) -> User | None:
        """The signed-in user, or None. Never read from the request body."""
        ...


class LocalOwner:
    """Every request is the one configured owner (COFFEEDATA_OWNER_ID, COFFEEDATA_OWNER). Local use only."""

    name = "local"

    def __init__(self, user_id: str | None = None, username: str | None = None):
        if os.environ.get("SPACE_ID"):  # set on every Hugging Face Space
            raise RuntimeError("the local sign-in stub must not run on a Space")
        self.owner = User(
            user_id or shots.owner_user_id(),
            username or os.environ.get("COFFEEDATA_OWNER") or "owner",
            "owner",
        )

    def user(self, request: Request) -> User | None:
        return self.owner
