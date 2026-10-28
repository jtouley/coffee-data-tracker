"""What every response gets: security headers, long-cached hashed static files and the session renewal."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import HTTPException, Request, Response

STATIC = Path(__file__).parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"

# No inline script anywhere, so script-src needs no nonce. The JSON data blocks (<script type="application/json">)
# aren't executed and CSP doesn't apply to them.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
# The camera is only for photographing a bag, so only that screen may ask for it.
CAMERA_PATHS = ("/bag/new",)


def permissions_policy(path: str) -> str:
    camera = "(self)" if path in CAMERA_PATHS else "()"
    return (
        f"camera={camera}, microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), "
        "interest-cohort=(), browsing-topics=()"
    )


def secure_headers(response: Response, path: str) -> None:
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = permissions_policy(path)
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if "Cache-Control" not in h:
        h["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"


class Assets:
    """Static files under content-hashed names (`app.3f2a1c9b.css`), so they can be cached for a year. A
    stylesheet's `url(/static/...)` references are rewritten to the hashed names first, so a font change
    changes the stylesheet's name too. Small enough to keep in memory."""

    def __init__(self, root: Path = STATIC):
        self.url: dict[str, str] = {}
        self.files: dict[str, tuple[bytes, str]] = {}
        paths = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: (p.suffix == ".css", p))
        for p in paths:
            rel = p.relative_to(root).as_posix()
            data = p.read_bytes()
            if p.suffix == ".css":
                text = data.decode()
                for name, url in self.url.items():
                    text = text.replace(f"url(/static/{name})", f"url({url})")
                data = text.encode()
            digest = hashlib.sha256(data).hexdigest()[:10]
            stem, _, ext = rel.rpartition(".")
            hashed = f"/static/{stem}.{digest}.{ext}"
            media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            self.url[rel], self.files[hashed] = hashed, (data, media)

    def __call__(self, rel: str) -> str:
        return self.url[rel]

    def response(self, path: str) -> Response:
        hit = self.files.get(path)
        if hit is None:
            raise HTTPException(404, "not found")
        data, media = hit
        return Response(data, media_type=media, headers={"Cache-Control": IMMUTABLE})


Next = Callable[[Request], Awaitable[Response]]


def middleware(
    set_cookie: Callable[[Response, object], None] | None,
    cookie: str,
    on_error: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request, Next], Awaitable]:
    """The outermost layer: every response, errors included, gets the security headers, and a session due for
    renewal gets a fresh cookie unless the route already set or cleared one (sign-out, a password change)."""

    async def mw(request: Request, call_next: Next) -> Response:
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - shown as the error screen; logged by on_error
            response = await on_error(request)
        secure_headers(response, request.url.path)
        acct = getattr(request.state, "renew", None)
        already = any(v.startswith(f"{cookie}=") for v in response.headers.getlist("set-cookie"))
        if acct is not None and set_cookie is not None and not already:
            set_cookie(response, acct)
        return response

    return mw


class LimitBody:
    """Refuse request bodies over a size, counting what arrives rather than trusting Content-Length (a chunked
    body has none). Limits are per path prefix, longest first; anything else gets `default`."""

    def __init__(self, app: Callable, default: int, limits: dict[str, int] | None = None):
        self.app, self.default = app, default
        self.limits = sorted((limits or {}).items(), key=lambda kv: -len(kv[0]))

    def limit(self, path: str) -> int:
        return next((n for prefix, n in self.limits if path.startswith(prefix)), self.default)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limit(scope["path"])
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            status = 400 if not declared.isdigit() else 413
            return await self._refuse(scope, receive, send, status)
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > limit:
                return await self._refuse(scope, receive, send, 413)
        sent = False

        async def replay() -> dict:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    async def _refuse(self, scope: dict, receive: Callable, send: Callable, status: int) -> None:
        detail = "That request is too large." if status == 413 else "Bad Content-Length."
        response = Response(
            json.dumps({"detail": detail}),
            status,
            media_type="application/json",
            headers={"Connection": "close"},
        )
        secure_headers(response, scope["path"])
        await response(scope, receive, send)


# What vulnerability scanners ask for: dotfiles and dot-folders (.env, .git/config, .aws/credentials), other stacks'
# scripts and config files, and WordPress or PHP admin paths. The app serves none of these: its own files are
# /static/<name>.<hash>.<ext>, /sw.js, /manifest.webmanifest and /robots.txt. /.well-known/ stays open.
PROBE = re.compile(
    r"(^|/)\.(?!well-known/)"
    r"|\.(php\d?|aspx?|jsp|cgi|env|bak|sql|ini|ya?ml|cfg)$"
    r"|^/(wp-|cgi-bin/|vendor/|phpmyadmin)",
    re.IGNORECASE,
)


class RejectProbes:
    """Answer scanner paths with an empty 404 before any other layer runs: no logging, routing or template.
    Outermost, so a burst of them costs as little as possible (it can't stop them reaching the instance)."""

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http" and PROBE.search(scope["path"]):
            response = Response(b"", 404, headers={"Cache-Control": "no-store", "Connection": "close"})
            secure_headers(response, scope["path"])
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


def client_ip(request: Request, hops: int) -> str:
    """The phone's address for rate limits. Behind `hops` proxies that each append to X-Forwarded-For, it's that
    many entries from the right; anything further left was sent by the client and can't be trusted."""
    if hops > 0:
        chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return request.client.host if request.client else ""
