"""Who is making a request. `Sessions` is the real sign-in (app/accounts.py); `LocalOwner` is a stub for
`make app` on a laptop, which treats every request as the owner, must be asked for by name and refuses to run when
hosted.

Every route needs a signed-in user unless it is on `PUBLIC`: `RequireSession` checks that before any route runs,
so a new route is private until it is added there. Any failure while working out who is asking counts as signed
out."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute, Match

from coffeedata import shots

from .accounts import SESSION_DAYS, Account, Accounts

COOKIE = "session"
log = logging.getLogger(__name__)

# The only routes that answer someone signed out: (method, path as registered). Everything else needs a session.
PUBLIC: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/static/{path:path}"),  # hashed styles, script, fonts and icons
        ("GET", "/sw.js"),
        ("GET", "/manifest.webmanifest"),
        ("GET", "/robots.txt"),
        ("GET", "/health"),
        ("GET", "/healthz"),
        ("GET", "/offline"),  # the service worker's fallback page, cached at install
        ("GET", "/signin"),
        ("POST", "/api/signin"),
        ("GET", "/join"),
        ("POST", "/api/invites/check"),
        ("POST", "/api/join"),
        ("GET", "/reset"),
        ("POST", "/api/reset"),
        # Email recovery (spec §4, §5): asking for a reset link, and opening a verify link on any device.
        ("GET", "/forgot"),
        ("POST", "/api/forgot"),
        ("GET", "/verify"),
        ("POST", "/api/verify"),
    }
)


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    role: str = "member"  # owner or member


class Auth(Protocol):
    def user(self, request: HTTPConnection) -> User | None:
        """The signed-in user, or None. Never read from the request body."""
        ...


def current_user(auth: Auth, request: HTTPConnection) -> User | None:
    """The signed-in user, worked out once per request. Any error on the way (a bad secret, a failed read) counts
    as signed out: logged, never shown."""
    state = request.state
    if not hasattr(state, "user"):
        try:
            state.user = auth.user(request)
        except Exception:  # noqa: BLE001 - fail closed
            log.exception("working out who is signed in failed; treating the request as signed out")
            state.user, state.auth_failed = None, True
    return state.user


def public_route(routes: Iterable[BaseRoute], scope: dict[str, Any]) -> bool:
    """Whether the route this request reaches is on PUBLIC. A HEAD is the GET it stands for. A path no route
    matches is not public (it gets the sign-in check, then the 404)."""
    method = "GET" if scope.get("method") == "HEAD" else scope.get("method", "")
    scope = scope | {"method": method}
    for route in routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return (method, getattr(route, "path", "")) in PUBLIC
    return False


class RequireSession:
    """Default deny: a request (or a WebSocket) for anything but a PUBLIC route needs a signed-in user before the
    route, or its body, is read. Signed out, a screen goes to sign-in, the API answers 401 and a WebSocket is
    closed. Marks the request public or private for the cache headers (web.middleware) and tags its log line with
    the user."""

    def __init__(
        self,
        app: Callable,
        routes: Callable[[], Iterable[BaseRoute]],
        auth: Auth,
        tag: Callable[[str], None] | None = None,
    ):
        self.app, self.routes, self.auth, self.tag = app, routes, auth, tag

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        try:
            public = public_route(self.routes(), scope)
        except Exception:  # noqa: BLE001 - fail closed
            log.exception("matching %s to a route failed", scope.get("path"))
            public = False
        conn.state.public = public
        user = None if public else current_user(self.auth, conn)
        if user is not None and self.tag is not None:
            self.tag(user.user_id)
        if public or user is not None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await signed_out(Request(scope))(scope, receive, send)


def signed_out(request: Request) -> Response:
    """A screen (any GET outside /api/) goes to sign-in, as it always has; the API answers 401."""
    if request.method in ("GET", "HEAD") and not request.url.path.startswith("/api/"):
        return RedirectResponse("/signin", 303)
    return JSONResponse({"detail": "sign in first"}, 401)


class LocalOwner:
    """Every request is the one configured owner (COFFEEDATA_OWNER_ID, COFFEEDATA_OWNER). Local use only."""

    name = "local"
    accounts = None

    def __init__(self, user_id: str | None = None, username: str | None = None):
        if hosted():
            raise RuntimeError("the local sign-in stub must not run on a public host")
        if os.environ.get("COFFEEDATA_LOCAL_OWNER") != "1":
            raise RuntimeError(
                "the local sign-in stub runs only with COFFEEDATA_LOCAL_OWNER=1 (make app sets it)"
            )
        self.owner = User(
            user_id or shots.owner_user_id(),
            username or os.environ.get("COFFEEDATA_OWNER") or "owner",
            "owner",
        )

    def user(self, request: HTTPConnection) -> User | None:
        return self.owner


class Sessions:
    """The signed session cookie: HttpOnly, Secure, SameSite=Lax, host-only, 90 days, renewed on use."""

    name = "accounts"

    def __init__(self, accounts: Accounts, secure: bool = True):
        self.accounts, self.secure = accounts, secure

    def user(self, request: HTTPConnection) -> User | None:
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


SECRETS = (
    "HF_TOKEN",
    "ANTHROPIC_API_KEY",
    "AUTH_PEPPER",
    "SESSION_SECRET",
    "OWNER_SETUP_CODE",
    "EMAIL_KEY",
    "RESEND_API_KEY",
)


def clean_secrets() -> None:
    """Strip whitespace from the secrets as the environment hands them over. A value stored with a trailing newline
    (`gcloud secrets versions add … --data-file=-` keeps one) would never match a typed setup code, and an HF token
    with a newline is an invalid header. Done once, in place, so every reader (huggingface_hub and the Anthropic
    client included) sees the clean value."""
    for name in SECRETS:
        value = os.environ.get(name)
        if value is not None and value != value.strip():
            os.environ[name] = value.strip()


def hosted() -> bool:
    """On Cloud Run (K_SERVICE is set on every service) or a Hugging Face Space (SPACE_ID)."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("SPACE_ID"))


def local_stub() -> bool:
    """The local stub was asked for by name, off any public host, with no real sign-in configured."""
    return (
        os.environ.get("COFFEEDATA_LOCAL_OWNER") == "1"
        and not hosted()
        and not os.environ.get("SESSION_SECRET")
    )


def missing_secrets() -> list[str]:
    """Sign-in settings the app needs and doesn't have (names only). Without them it serves nothing."""
    if local_stub():
        return []
    # OWNER_SETUP_CODE isn't required: without it nobody can create the owner (the screen says so), and once the
    # owner exists it only resets the owner's password, once per value.
    need = {"AUTH_PEPPER": 16, "SESSION_SECRET": 32}
    if hosted():
        need["HF_TOKEN"] = 1  # the hosted app writes to Hugging Face datasets
    missing = [k for k, n in need.items() if len(os.environ.get(k, "")) < n]
    if hosted() and os.environ.get("COFFEEDATA_INSECURE_COOKIES") == "1":
        missing.append("COFFEEDATA_INSECURE_COOKIES (remove it: cookies are always Secure on a public host)")
    return missing


def email_key() -> str | None:
    """EMAIL_KEY, when the email features can work: it's a valid key, and (hosted) there's a way to send. Neither is
    in missing_secrets(): without them the app starts and the email features stay hidden (spec §5)."""
    from .contacts import EmailCrypto, EmailKeyError

    key = os.environ.get("EMAIL_KEY") or None
    if not key or (hosted() and not os.environ.get("RESEND_API_KEY")):
        return None
    try:
        EmailCrypto(key)
    except EmailKeyError as e:
        log.error("email features are off: %s", e)  # what's wrong with it, never the value
        return None
    return key


def from_env() -> Auth:
    """Real sign-in, or the local stub where it was asked for (missing_secrets has refused anything else)."""
    if local_stub():
        return LocalOwner()
    from coffeedata.lake import Lake

    accounts = Accounts(
        Lake.users(),
        os.environ.get("AUTH_PEPPER", ""),
        os.environ.get("SESSION_SECRET", ""),
        os.environ.get("OWNER_SETUP_CODE"),
        email_key=email_key(),
    )
    insecure = os.environ.get("COFFEEDATA_INSECURE_COOKIES") == "1" and not hosted()
    return Sessions(accounts, secure=not insecure)
