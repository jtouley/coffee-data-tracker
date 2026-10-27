"""What every response gets: security headers, long-cached hashed static files and the session renewal."""

from __future__ import annotations

import hashlib
import mimetypes
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


def middleware(set_cookie: Callable[[Response, object], None] | None) -> Callable[[Request, Next], Awaitable]:
    async def mw(request: Request, call_next: Next) -> Response:
        response = await call_next(request)
        secure_headers(response, request.url.path)
        acct = getattr(request.state, "renew", None)
        if acct is not None and set_cookie is not None:
            set_cookie(response, acct)
        return response

    return mw
