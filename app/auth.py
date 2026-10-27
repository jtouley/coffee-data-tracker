"""Who is making a request. `Sessions` is the real sign-in (app/accounts.py); `LocalOwner` is a stub for
`make app` on a laptop, which treats every request as the owner and refuses to run when hosted."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request, Response

from coffeedata import shots

from .accounts import SESSION_DAYS, Account, Accounts

COOKIE = "session"


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
    accounts = None

    def __init__(self, user_id: str | None = None, username: str | None = None):
        if hosted():
            raise RuntimeError("the local sign-in stub must not run on a public host")
        self.owner = User(
            user_id or shots.owner_user_id(),
            username or os.environ.get("COFFEEDATA_OWNER") or "owner",
            "owner",
        )

    def user(self, request: Request) -> User | None:
        return self.owner


class Sessions:
    """The signed session cookie: HttpOnly, Secure, SameSite=Lax, host-only, 90 days, renewed on use."""

    name = "accounts"

    def __init__(self, accounts: Accounts, secure: bool = True):
        self.accounts, self.secure = accounts, secure

    def user(self, request: Request) -> User | None:
        hit = self.accounts.session(request.cookies.get(COOKIE))
        if hit is None:
            return None
        acct, renew = hit
        if renew:
            request.state.renew = acct  # the middleware reissues the cookie on the way out
        return User(acct.user_id, acct.username, acct.role)

    def set_cookie(self, response: Response, acct: Account) -> None:
        response.set_cookie(
            COOKIE,
            self.accounts.issue(acct),
            max_age=SESSION_DAYS * 86400,
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE, path="/", secure=self.secure, httponly=True, samesite="lax")


def hosted() -> bool:
    """On Cloud Run (K_SERVICE is set on every service) or a Hugging Face Space (SPACE_ID)."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("SPACE_ID"))


def missing_secrets() -> list[str]:
    """Sign-in secrets a hosted app needs and doesn't have (names only)."""
    if not (hosted() or os.environ.get("SESSION_SECRET")):
        return []
    # OWNER_SETUP_CODE isn't required: without it nobody can create the owner (the screen says so), and once the
    # owner exists it only resets the owner's password, once per value.
    need = {"AUTH_PEPPER": 16, "SESSION_SECRET": 32}
    if hosted():
        need["HF_TOKEN"] = 1  # the hosted app writes to Hugging Face datasets
    return [k for k, n in need.items() if len(os.environ.get(k, "")) < n]


def from_env() -> Auth:
    """Real sign-in when hosted or wherever SESSION_SECRET is set; the local stub otherwise."""
    if not (hosted() or os.environ.get("SESSION_SECRET")):
        return LocalOwner()
    from coffeedata.lake import Lake

    accounts = Accounts(
        Lake.users(),
        os.environ.get("AUTH_PEPPER", ""),
        os.environ.get("SESSION_SECRET", ""),
        os.environ.get("OWNER_SETUP_CODE"),
    )
    return Sessions(accounts, secure=os.environ.get("COFFEEDATA_INSECURE_COOKIES") != "1")
