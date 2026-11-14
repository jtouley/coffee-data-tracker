"""admin.registry.coffee: the owner's command center (docs/admin/README.md).

Six pages behind passkey sign-in (auth.py): Week (did it land, what failed), Queue (the build's flags by product,
each product's case, and the rule settings), Jobs (start, watch and cancel the pipeline's Cloud Run jobs), Release
(a build's gate and changes; draft, Publish, Roll back), Catalog (every product; include or exclude it), Log (every
write). Every route but PUBLIC needs a live session, checked in one place
(`gate`) before any route runs, so a route added later is signed-in by default, and an unknown path answers 401 like
a known one. A misconfigured service answers 503 to everything. The lake is opened read-only and never written:
writes go to the control plane, and the lake changes only through the pipeline's jobs (writes.py).

Run it with `uvicorn --factory admin.main:create_app` (`make admin` locally; docs/admin/README.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import mimetypes
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from coffeedata import decisions, quality, rules
from coffeedata.lake import Lake

from . import jobs, logs, queries
from .auth import SESSION_S, Auth, AuthError, AuthUnavailable, Session
from .catalog import FACETS, Catalog, Filters
from .config import Settings, from_env
from .controlplane import Conflict, ControlPlaneError
from .logs import emit
from .runjobs import JobsError
from .store import StoreError
from .writes import StartedUnrecorded, Writes

HERE = Path(__file__).parent
APP_STATIC = HERE.parent / "app" / "static"
COOKIE_SECURE = "__Host-admin"  # host-only, Secure, path=/
COOKIE_LOCAL = "admin_session"  # http://localhost only
PAGES = ("week", "queue", "jobs", "release", "catalog", "log")
# What each page's data region reads, named on its loading state.
READS = {
    "week": "job_manifests, fetches, builds and serving_log",
    "queue": "mart_flags, fetches and the rule settings",
    "jobs": "job_runs and Cloud Run executions",
    "release": "builds, release_changes, serving_log and drafts",
    "catalog": "product_decisions, dim_coffee and decisions",
    "log": "the control plane's audit",
}
AUDITED = ("job_start", "job_cancel", "decide", "undo", "publish", "discard", "evaluation", "settings", "crawl_flag")  # fmt: skip
WRITES_PER_10_MIN = 60
SLOW_S = 8  # the page says the lake is slow after this long (admin.js)
WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BUILD_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")

# Reachable signed out. Everything else needs a session (tests/test_admin.py walks every route).
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/signin"),
    ("GET", "/enroll"),
    ("GET", "/recover"),
    ("POST", "/api/auth/signin/begin"),
    ("POST", "/api/auth/signin/finish"),
    ("POST", "/api/auth/enroll/begin"),
    ("POST", "/api/auth/enroll/finish"),
    ("POST", "/api/auth/recover/begin"),
    ("POST", "/api/auth/recover/finish"),
}
PUBLIC_PREFIX = "/static/"

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def secure_headers(response: Response, static: bool = False) -> None:
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "same-origin"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
    )
    h["X-Robots-Tag"] = "noindex, nofollow"
    if not static:
        h["Cache-Control"] = "no-store"


class Assets:
    """The app's stylesheet and fonts (the same look) and the admin's own files, under content-hashed names."""

    def __init__(self) -> None:
        self.url: dict[str, str] = {}
        self.files: dict[str, tuple[bytes, str]] = {}
        sources = [
            (APP_STATIC / "fonts", "fonts"),
            (APP_STATIC / "app.css", "app.css"),
            (HERE / "static", ""),
        ]
        paths: list[tuple[str, Path]] = []
        for src, rel in sources:
            if src.is_file():
                paths.append((rel, src))
            else:
                paths += [
                    ((f"{rel}/" if rel else "") + p.relative_to(src).as_posix(), p)
                    for p in src.rglob("*")
                    if p.is_file()
                ]
        for rel, p in sorted(paths, key=lambda x: (x[0].endswith(".css"), x[0])):
            data = p.read_bytes()
            if rel.endswith(".css"):
                text = data.decode()
                for name, url in self.url.items():
                    text = text.replace(f"url(/static/{name})", f"url({url})")
                data = text.encode()
            stem, _, ext = rel.rpartition(".")
            hashed = f"/static/{stem}.{hashlib.sha256(data).hexdigest()[:10]}.{ext}"
            self.url[rel] = hashed
            self.files[hashed] = (data, mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    def __call__(self, rel: str) -> str:
        return self.url[rel]


class LakeTimeout(Exception):
    pass


# The deadline of the page part being read: every lake read of one part shares it (Reader.run).
DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("admin_deadline", default=None)


class Reader:
    """Runs a query function (lake, con, *args) on the lake's own small thread pool, so slow reads never hold the
    threads sign-in uses. All the reads of one page part share one deadline (`timeout_s` from the part's start), well
    under Cloud Run's request timeout; at the deadline DuckDB is interrupted and the part shows its timeout state.
    Query functions read through `con`, so the interrupt reaches them. `before` runs first in the thread: tests use it
    to make the lake slow or broken."""

    def __init__(self, lake: Lake, timeout_s: float = 25.0, before: Callable[[], None] | None = None):
        self.lake, self.timeout_s, self.before = lake, timeout_s, before
        self._listed = time.monotonic()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lake")

    def start(self) -> None:
        DEADLINE.set(time.monotonic() + self.timeout_s)

    async def run(self, fn: Callable, *args) -> Any:
        deadline = DEADLINE.get() or time.monotonic() + self.timeout_s
        left = deadline - time.monotonic()
        if left <= 0:
            raise LakeTimeout(f"the lake didn't answer in {self.timeout_s:g} s")
        if self.lake.remote and time.monotonic() - self._listed > 300:  # see releases written since
            quality.forget_listings()
            self._listed = time.monotonic()
        con = self.lake.connect()

        def call():
            if self.before:
                self.before()
            try:
                return fn(self.lake, con, *args)
            finally:
                with contextlib.suppress(Exception):
                    con.close()

        future = asyncio.get_running_loop().run_in_executor(self.pool, call)
        try:
            return await asyncio.wait_for(future, left)
        except TimeoutError as e:
            with contextlib.suppress(Exception):
                con.interrupt()  # the thread closes the connection when the query stops
            raise LakeTimeout(f"the lake didn't answer in {self.timeout_s:g} s") from e


def _client_ip(request: Request, hops: int) -> str:
    if hops > 0:
        chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return request.client.host if request.client else ""


def _fmt_n(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _fmt_t(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return v.astimezone(UTC).strftime("%a %d %b %H:%M")
    return str(v)


def create_app(
    settings: Settings | None = None,
    reader: Reader | None = None,
    slow_s: float = SLOW_S,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """The admin app. `reader`, `slow_s` and `now` are for tests (a slow or broken lake, a fixed clock); the service
    uses the defaults."""
    settings = settings or from_env()
    logs.setup()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    assets = Assets()
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals |= {
        "asset": assets,
        "pages": PAGES,
        "n": _fmt_n,
        "t": _fmt_t,
        "slow_s": slow_s,
        "new_key": new_key,
    }
    cookie = COOKIE_SECURE if settings.secure_cookies else COOKIE_LOCAL
    auth: Auth | None = None
    if not settings.problems:
        assert settings.store is not None
        auth = Auth(
            settings.store, settings.session_secret, settings.enroll_token, settings.rp_id, settings.origin
        )
    else:
        emit("ERROR", event="admin_switched_off", problems=settings.problems)
    lake = Lake(settings.lake_url, read_only=True)
    reader = reader or Reader(lake, settings.lake_timeout_s)
    catalog = Catalog(reader.lake, settings.cache_dir)
    writes: Writes | None = None
    if not settings.writes_off:
        assert settings.control and settings.jobs and settings.snapshots
        writes = Writes(settings.control, settings.jobs, settings.snapshots, reader.run, catalog=catalog,
                        **({"now": now} if now else {}))  # fmt: skip
    else:
        emit("WARNING", event="admin_writes_off", reasons=settings.writes_off)
    limiter = Limiter()
    app.state.settings, app.state.auth, app.state.reader, app.state.writes = settings, auth, reader, writes

    def page(name: str, status: int = 200, **ctx) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(**ctx), status)

    def audit(event: str, request: Request, outcome: str) -> None:
        emit(
            "INFO" if outcome == "ok" else "WARNING",
            event="admin_auth",
            action=event,
            outcome=outcome,
            ip=_client_ip(request, settings.proxy_hops),
        )

    # --- the gate: every request, before any route ------------------------------------------------------

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path, method = request.url.path, request.method
        static = path.startswith(PUBLIC_PREFIX)
        if auth is None:  # switched off: fail closed, say why without values
            if static:
                response = await call_next(request)
            elif path == "/health":
                response = JSONResponse({"ok": False, "detail": "misconfigured"}, 503)
            elif method == "GET" and not path.startswith(("/api/", "/part/")):
                response = page("off.html", 503, problems=settings.problems)
            else:
                response = JSONResponse({"detail": "The admin is switched off."}, 503)
            secure_headers(response, static)
            return response
        request.state.session = None
        public = static or (method, path) in PUBLIC or (method == "HEAD" and ("GET", path) in PUBLIC)
        try:
            if method == "POST" and not _same_origin(request):
                response = JSONResponse({"detail": "Refused: not from this site."}, 403)
            elif public:
                if (method, path) in {("GET", "/signin"), ("GET", "/enroll"), ("GET", "/recover")}:
                    request.state.session = await asyncio.to_thread(auth.session, request.cookies.get(cookie))
                response = await call_next(request)
            else:
                s = await asyncio.to_thread(auth.session, request.cookies.get(cookie))
                if s is None:
                    response = _signed_out(request, ended=bool(request.cookies.get(cookie)))
                else:
                    request.state.session = s
                    response = await call_next(request)
        except AuthUnavailable as e:
            emit("ERROR", event="admin_auth_unavailable", error=str(e))
            response = (
                page("off.html", 503, problems=[], unavailable=True)
                if method == "GET" and not path.startswith(("/api/", "/part/"))
                else JSONResponse({"detail": "Sign-in is unavailable. Try again shortly."}, 503)
            )
        secure_headers(response, static)
        return response

    def _same_origin(request: Request) -> bool:
        """The app's CSRF rule: a fetch from this origin (header, Fetch Metadata and Origin all agree)."""
        if request.headers.get("x-requested-with") != "fetch":
            return False
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return False
        return request.headers.get("origin", "").rstrip("/") == settings.origin

    def _signed_out(request: Request, ended: bool) -> Response:
        path = request.url.path
        if request.method == "GET" and not path.startswith(("/api/", "/part/")):
            q = {"next": path + (f"?{request.url.query}" if request.url.query else "")}
            if ended:
                q["ended"] = "1"
            r: Response = RedirectResponse(f"/signin?{urlencode(q)}", 303)
        else:
            r = JSONResponse({"detail": "Sign in first.", "ended": ended}, 401)
        if ended:
            r.delete_cookie(
                cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="strict"
            )
        return r

    def _set_session(response: Response, value: str) -> None:
        response.set_cookie(cookie, value, max_age=SESSION_S, path="/", secure=settings.secure_cookies,
                            httponly=True, samesite="strict")  # fmt: skip

    def _next(raw: object) -> str:
        """Only a path on this site: never another origin (`//evil`, `https://…`)."""
        if (
            isinstance(raw, str)
            and raw
            and raw.startswith("/")
            and not raw.startswith("//")
            and "\\" not in raw
        ):
            u = urlsplit(raw)
            if not u.scheme and not u.netloc:
                return raw
        return "/week"

    # --- public -------------------------------------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/static/{rest:path}")
    async def static(rest: str):
        hit = assets.files.get(f"/static/{rest}")
        if hit is None:
            return Response(b"", 404)
        data, media = hit
        return Response(
            data, media_type=media, headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    @app.get("/signin", response_class=HTMLResponse)
    async def signin(request: Request, next: str | None = None, ended: str | None = None):
        assert auth is not None
        if request.state.session:
            return RedirectResponse(_next(next), 303)
        if await asyncio.to_thread(auth.enroll_open):
            return RedirectResponse("/enroll", 303)
        return page("signin.html", ended=bool(ended), next=_next(next), title="Sign in")

    @app.get("/enroll", response_class=HTMLResponse)
    async def enroll(request: Request):
        assert auth is not None
        is_open = await asyncio.to_thread(auth.enroll_open)
        return page("enroll.html", open=is_open, signed_in=bool(request.state.session), title="Enroll")

    @app.get("/recover", response_class=HTMLResponse)
    async def recover(request: Request):
        return page("recover.html", title="Recover")

    async def _json(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise AuthError("Bad request.")
        return body

    async def _auth_call(request: Request, action: str, fn: Callable[[dict], Any]) -> Response:
        try:
            body = await _json(request)
            result = await asyncio.to_thread(fn, body)
        except AuthError as e:
            audit(action, request, "refused")
            return JSONResponse({"detail": e.message}, e.status)
        audit(action, request, "ok")
        return result

    def _session_response(value: str, extra: dict | None = None, to: str = "/week") -> JSONResponse:
        r = JSONResponse({"ok": True, "next": to} | (extra or {}))
        _set_session(r, value)
        return r

    def ip(request: Request) -> str:
        return _client_ip(request, settings.proxy_hops)

    @app.post("/api/auth/signin/begin")
    async def signin_begin(request: Request):
        assert auth is not None
        return await _auth_call(
            request, "signin_begin", lambda b: JSONResponse(auth.signin_begin(ip(request)))
        )

    @app.post("/api/auth/signin/finish")
    async def signin_finish(request: Request):
        assert auth is not None
        return await _auth_call(
            request,
            "signin",
            lambda b: _session_response(
                auth.signin_finish(str(b.get("ceremony", "")), b.get("credential") or {}, ip(request)),
                to=_next(b.get("next")),
            ),
        )

    @app.post("/api/auth/enroll/begin")
    async def enroll_begin(request: Request):
        assert auth is not None
        return await _auth_call(
            request,
            "enroll_begin",
            lambda b: JSONResponse(auth.enroll_begin(str(b.get("token", "")), ip(request))),
        )

    @app.post("/api/auth/enroll/finish")
    async def enroll_finish(request: Request):
        assert auth is not None

        def go(b):
            value, codes = auth.enroll_finish(str(b.get("ceremony", "")), b.get("credential") or {})
            return _session_response(value, {"codes": codes})

        return await _auth_call(request, "enroll", go)

    @app.post("/api/auth/recover/begin")
    async def recover_begin(request: Request):
        assert auth is not None
        return await _auth_call(
            request,
            "recover_begin",
            lambda b: JSONResponse(auth.recover_begin(str(b.get("code", "")), ip(request))),
        )

    @app.post("/api/auth/recover/finish")
    async def recover_finish(request: Request):
        assert auth is not None
        return await _auth_call(
            request,
            "recover",
            lambda b: _session_response(
                auth.recover_finish(str(b.get("ceremony", "")), b.get("credential") or {})
            ),
        )

    # --- signed in: account -------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def home():
        return RedirectResponse("/week", 303)

    @app.get("/account", response_class=HTMLResponse)
    async def account(request: Request):
        assert auth is not None
        s: Session = request.state.session
        creds = await asyncio.to_thread(auth.credentials)
        rule = await asyncio.to_thread(auth.add_rule, s)
        left = await asyncio.to_thread(auth.recovery_left)
        return page("account.html", creds=list(creds.values()), rule=rule, session=s, title="Account", codes_left=left,
                    expires=datetime.fromtimestamp(s.expires_at, UTC), on="account")  # fmt: skip

    @app.post("/api/auth/add/begin")
    async def add_begin(request: Request):
        assert auth is not None
        s = request.state.session
        return await _auth_call(
            request, "add_begin", lambda b: JSONResponse(auth.add_begin(s, b.get("code"), ip(request)))
        )

    @app.post("/api/auth/add/finish")
    async def add_finish(request: Request):
        assert auth is not None
        s = request.state.session
        return await _auth_call(
            request,
            "add_passkey",
            lambda b: (
                auth.add_finish(s, str(b.get("ceremony", "")), b.get("credential") or {}),
                JSONResponse({"ok": True}),
            )[1],
        )

    @app.post("/api/auth/signout")
    async def signout(request: Request):
        assert auth is not None
        await asyncio.to_thread(auth.revoke, request.state.session)
        audit("signout", request, "ok")
        r = JSONResponse({"ok": True, "next": "/signin"})
        r.delete_cookie(cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="strict")
        return r

    @app.post("/api/auth/signout-all")
    async def signout_all(request: Request):
        assert auth is not None
        await asyncio.to_thread(auth.revoke_all)
        audit("signout_all", request, "ok")
        r = JSONResponse({"ok": True, "next": "/signin"})
        r.delete_cookie(cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="strict")
        return r

    # --- signed in: pages ---------------------------------------------------------------------------------
    # Each page is a shell that renders at once (no lake read) and a part (/part/<page>) that admin.js loads into
    # it, so the page shows the loading, slow and error states while the lake is read. The writes (jobs, decisions,
    # drafts, Publish) go through `write`, below.

    def _week_arg(v: str | None) -> str | None:
        return v if v and WEEK_RE.match(v) else None

    def _build_arg(v: str | None) -> str | None:
        return v if v and BUILD_RE.match(v) else None

    def shell(request: Request, name: str) -> HTMLResponse:
        q = request.url.query
        return page("shell.html", on=name, part=f"/part/{name}" + (f"?{q}" if q else ""), reading=READS[name],
                    title=name.capitalize(), writes_off=settings.writes_off)  # fmt: skip

    def shell_route(name: str) -> Callable:
        async def route(request: Request) -> HTMLResponse:
            return shell(request, name)

        return route

    for name in PAGES:  # one shell route per page, all alike
        app.add_api_route(f"/{name}", shell_route(name), methods=["GET"], response_class=HTMLResponse)

    async def part(name: str, build: Callable, files: str | None = None) -> HTMLResponse:
        """A page's data region: its template with the query's result, or the error state."""
        files = files or READS[name]
        started = datetime.now(UTC)
        reader.start()  # one deadline for all of this part's reads
        try:
            ctx = await build()
        except LakeTimeout as e:
            return page("part_error.html", 504, files=files, error=str(e))
        except ControlPlaneError as e:
            emit("ERROR", event="admin_control_plane_failed", page=name, error=str(e)[:200])
            return page("part_error.html", 503, files="the control plane", error=_reason(e), control=True)
        except Exception as e:  # noqa: BLE001 - shown as the error state; the class only, never a trace
            emit("ERROR", event="admin_read_failed", page=name, error=type(e).__name__)
            return page("part_error.html", 502, files=files, error=_reason(e))
        return page(
            f"part_{name}.html", read_at=started, files=files, **({"writes_off": settings.writes_off} | ctx)
        )

    DOWN = "the control plane can't be read right now, so decisions, drafts and runs aren't shown"

    async def plane(read: Callable[[], Awaitable[Any]], default: Any, down: list[str]) -> Any:
        """A control-plane read for a page that also reads the lake: on an outage the page keeps the lake's part,
        says so once, and offers no action (`down` gets the reason)."""
        try:
            return await read()
        except ControlPlaneError as e:
            emit("ERROR", event="admin_control_plane_failed", error=str(e)[:200])
            if DOWN not in down:
                down.append(DOWN)
            return default

    async def common(week: str | None) -> dict:
        weeks = await reader.run(queries.weeks)
        served = await reader.run(queries.served)
        chosen = week if week in weeks else (weeks[0] if weeks else week)
        stale = None
        if served and weeks and str(served.get("run_date"))[:10] < weeks[0]:
            stale = {"served": str(served.get("run_date"))[:10], "latest": weeks[0]}
        return {"weeks": weeks, "week": chosen, "served": served, "stale": stale}

    @app.get("/part/week", response_class=HTMLResponse)
    async def week_part(week: str | None = None):
        async def build():
            c = await common(_week_arg(week))
            data = await reader.run(queries.week, c["week"]) if c["week"] else None
            fails = await reader.run(queries.failures, c["week"]) if c["week"] else None
            trend = await reader.run(queries.failure_trend)
            return c | {
                "data": data,
                "failures": fails,
                "trend": trend,
                "connectors": jobs.FIELDS["connector"].choices,
            }

        return await part("week", build)

    @app.get("/part/release", response_class=HTMLResponse)
    async def release_part(request: Request, build: str | None = None):
        p = request.query_params

        async def make():
            off = list(settings.writes_off)
            rel = await reader.run(queries.releases)
            served = await reader.run(queries.served)  # read once: pending and readiness are given it
            all_builds = rel["builds"]
            ids = [b["build_id"] for b in all_builds]
            runs, drafts, pending = {}, [], None
            if writes:
                runs = {r["run_id"]: r for r in await plane(lambda: writes.runs(20), [], off)}
                drafts = await plane(lambda: asyncio.to_thread(writes.control.drafts, 20), [], off)
                pending = await plane(lambda: writes.pending(served, drafts), None, off)
            chosen = _build_arg(build)
            if chosen not in ids:  # the newest draft that was built, else the served build, else the newest
                built = [rel["by_draft"][d["draft_id"]] for d in drafts if d["draft_id"] in rel["by_draft"]]
                chosen = (
                    (built[0] if built else None)
                    or (served or {}).get("build_id")
                    or (ids[0] if ids else None)
                )
            info = next((b for b in all_builds if b["build_id"] == chosen), None)
            draft = next((d for d in drafts if info and d["draft_id"] == info["draft_id"]), None)
            ready = (
                await plane(lambda: writes.readiness(draft, rel, served), None, off)
                if writes and draft
                else None
            )
            try:
                offset = max(0, int(p.get("offset") or 0))
            except ValueError:
                offset = 0
            gate = await reader.run(queries.gate, chosen) if chosen else None
            changes = await reader.run(queries.changes, chosen, p.get("table") or None, p.get("change") or None,
                                       p.get("reason") or None, 200, offset) if chosen else None  # fmt: skip
            for d in drafts:
                b = rel["by_draft"].get(d["draft_id"])
                bi = next((x for x in all_builds if x["build_id"] == b), None)
                d["build_id"], d["state"] = b, _draft_state(d, bi, served, runs.get(d["run_id"]))
            return {"builds": all_builds, "build": chosen, "info": info, "served": served, "drafts": drafts,
                    "draft": draft, "ready": ready, "pending": pending, "gate": gate, "changes": changes,
                    "filters": dict(p), "offset": offset, "writes_off": off}  # fmt: skip

        return await part("release", make)

    @app.get("/part/queue", response_class=HTMLResponse)
    async def queue_part(request: Request):
        """The served build's open flags by product, or one product's case (?case=<coffee_id>), and the rule
        settings. Decisions, live checks and crawl flags come from the control plane; the flags from the build."""
        p = request.query_params
        f = {k: (p.get(k) or "").strip()[:200] or None for k in ("roaster", "rule", "state", "case")}
        f["rule"] = f["rule"] if f["rule"] in rules.RULE_TYPES else None
        f["state"] = f["state"] if f["state"] in queries.QUEUE_STATES else None
        try:
            offset = max(0, int(p.get("offset") or 0))
        except ValueError:
            offset = 0

        async def make():
            served = await reader.run(queries.served)
            build = (served or {}).get("build_id")
            off = list(settings.writes_off)
            ctx: dict = {"served": served, "build": build, "f": f,
                         "states": queries.QUEUE_STATES, "writes_off": off,
                         "settings": {"version_id": None, "doc": rules.default()}}  # fmt: skip
            if not build:
                return ctx

            async def cp(fn, *args, default=None):  # a control-plane read; on an outage the page reads on
                async def read():
                    await writes.ready()
                    return await asyncio.to_thread(fn, *args)

                return await plane(read, default, off) if writes else default

            if f["case"]:
                c = await reader.run(queries.case, build, f["case"])
                ctx["case"], ctx["connectors"] = c, jobs.FIELDS["connector"].choices
                if c:
                    c["product"]["url"] = queries.safe_url(c["product"]["url"])
                if c and writes:
                    pr = c["product"]
                    _with_latest([c["summary"]], await cp(writes.control.latest, [pr["coffee_id"]],
                                 [pr["input_hash"]] if pr["input_hash"] else [],
                                 default={"coffee_id": {}, "input_hash": {}}), "decided")  # fmt: skip
                    ctx["crawl"] = ((await cp(writes.control.roaster_states, [pr["roaster_id"]], default={}))
                                    .get(pr["roaster_id"]))  # fmt: skip
                    test = await cp(writes.control.recent_test, pr["roaster_id"], 7 * 24)
                    ctx["test"] = test
                    if test:
                        ctx["answers"] = (await reader.run(queries.test_results, [test["test_id"]])).get(
                            test["test_id"], [])  # fmt: skip
                return ctx
            q = await reader.run(queries.queue, build, f["roaster"], f["rule"], f["state"], offset)
            ctx["q"], ctx["pager"] = q, [(k, f[k]) for k in ("roaster", "rule", "state") if f[k]]
            if (
                q.get("rows") and writes
            ):  # decided in the control plane since the build: say so, offer no decision
                latest = await cp(writes.control.latest, [r["coffee_id"] for r in q["rows"]],
                                  [r["input_hash"] for r in q["rows"] if r["input_hash"]],
                                  default={"coffee_id": {}, "input_hash": {}})  # fmt: skip
                _with_latest(q["rows"], latest, "decided")
            ctx["settings"] = (
                await cp(writes.control.settings, default=ctx["settings"]) if writes else ctx["settings"]
            )
            ctx["history"] = await cp(writes.control.settings_history, 5, default=[]) if writes else []
            # shown as the deployed code reads it: a rule type retired since it was saved is left out, so an edit made
            # from this text still checks
            ctx["settings_json"] = json.dumps(rules.check(ctx["settings"]["doc"], strict=False), indent=2)
            return ctx

        return await part("queue", make)

    @app.get("/part/jobs", response_class=HTMLResponse)
    async def jobs_part(request: Request):
        async def make():
            off = list(settings.writes_off)
            runs = await plane(lambda: writes.runs(30), [], off) if writes else []
            return {"writes_off": off, "actions": [a for a in jobs.ACTIONS.values() if a.page == "jobs"], "fields": jobs.FIELDS,
                    "runs": runs, "running": any(r["status"] in ("pending", "running") for r in runs),
                    "prefill": dict(request.query_params),
                    "today": datetime.now(UTC).date().isoformat()}  # fmt: skip

        return await part("jobs", make, "job_runs and Cloud Run executions")

    @app.get("/part/catalog", response_class=HTMLResponse)
    async def catalog_part(request: Request, build: str | None = None):
        f = Filters.from_query(request.query_params)

        async def make():
            rel = await reader.run(queries.releases)
            served = (await reader.run(queries.served)) or {}
            ids = [b["build_id"] for b in rel["builds"]]
            chosen = _build_arg(build)
            chosen = chosen if chosen in ids else served.get("build_id") or (ids[0] if ids else None)
            data = await reader.run(lambda _lake, _con: catalog.search(chosen, f)) if chosen else None
            off = list(settings.writes_off)
            if data and writes:  # each row's latest decision, by coffee and by version
                latest = await plane(lambda: asyncio.to_thread(
                    writes.control.latest, [r["coffee_id"] for r in data["rows"]],
                    [r["input_hash"] for r in data["rows"] if r.get("input_hash")]),
                    {"coffee_id": {}, "input_hash": {}}, off)  # fmt: skip
                _with_latest(data["rows"], latest, "decision")
            return {"builds": rel["builds"], "build": chosen, "data": data, "f": f, "facets": FACETS,
                    "qs": _qs(request, f, chosen), "writes_off": off}  # fmt: skip

        return await part("catalog", make)

    @app.get("/part/log", response_class=HTMLResponse)
    async def log_part(action: str | None = None, offset: int = 0):
        async def make():
            if not writes:
                return {"rows": [], "total": 0, "action": None, "offset": 0, "actions": AUDITED}
            a = action if action in AUDITED else None
            rows, total = await asyncio.to_thread(writes.control.audit_log, 100, max(0, offset), a)
            return {"rows": rows, "total": total, "action": a, "offset": max(0, offset), "actions": AUDITED}

        return await part("log", make, "the control plane's audit")

    # --- signed in: writes --------------------------------------------------------------------------------------
    # Every write: the gate's CSRF check (POST, same origin) and session first; then writes on, the per-session
    # limit, a JSON body with an idempotency key, and the write itself, which records its audit row in the same
    # transaction (writes.py). Refusals say why; nothing is half done.

    def actor(request: Request) -> str:
        s: Session = request.state.session
        return f"owner (passkey {s.credential_id[:8]})"

    def on() -> Writes:
        """The writes, inside `write` (which has answered 503 already when they are off)."""
        assert writes is not None
        return writes

    async def write(
        request: Request, fn: Callable[[str, str, dict], Awaitable[Any]], replay: bool = True
    ) -> Response:
        if writes is None:
            return JSONResponse({"detail": "Writes are off: " + "; ".join(settings.writes_off) + "."}, 503)
        if not limiter.allow(request.state.session.key):
            return JSONResponse({"detail": "Too many writes in a few minutes. Wait, then try again."}, 429)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("key"), str):
            return JSONResponse({"detail": "Bad request."}, 400)
        try:
            # A key already used (a double submit, a retry): its first answer, before any check that the first
            # write's own effect would now fail (the job it started is running).
            key = f"{request.url.path}:{body['key']}"  # one write's key never replays another write's answer
            first = await on().replay(key) if replay else None
            out = first if first is not None else await fn(actor(request), key, body)
        except jobs.Refused as e:
            return JSONResponse(
                {"detail": "Refused.", "reasons": e.reasons, "overridable": e.overridable}, 409
            )
        except Conflict as e:
            return JSONResponse({"detail": str(e)}, 409)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, 400)
        except (ControlPlaneError, StoreError) as e:
            emit("ERROR", event="admin_write_failed", path=request.url.path, error=str(e)[:200])
            return JSONResponse({"detail": "The control plane couldn't record this, so nothing was done. "
                                           "Try again shortly."}, 503)  # fmt: skip
        except StartedUnrecorded as e:
            return JSONResponse({"detail": f"Cloud Run {e.verb} {e}, but the control plane couldn't record it. "
                                           "See Jobs and Cloud Run before trying again."}, 500)  # fmt: skip
        except JobsError as e:
            emit("ERROR", event="admin_write_failed", path=request.url.path, error=str(e)[:200])
            return JSONResponse({"detail": str(e)}, 502)
        return JSONResponse(out)

    @app.post("/api/jobs/{action}/plan")
    async def job_plan(request: Request, action: str):
        async def go(_actor, _key, body):
            p = await on().plan(action, body)
            return {"steps": [{"job": s.job, "args": s.args} for s in p.steps], "refusals": p.refusals,
                    "window": p.window, "params": p.params}  # fmt: skip

        return await write(request, go, replay=False)  # a preview acts on nothing

    @app.post("/api/jobs/{action}/runs")
    async def job_start(request: Request, action: str):
        return await write(request, lambda a, k, b: on().start(a, k, action, b, b.get("override") or None))

    @app.post("/api/jobs/runs/{run_id}/cancel")
    async def job_cancel(request: Request, run_id: str):
        return await write(request, lambda a, k, b: on().cancel(a, k, run_id))

    @app.post("/api/decisions")
    async def decide(request: Request):
        return await write(request, lambda a, k, b: on().decide(a, k, b))

    @app.post("/api/decisions/{target}/undo")
    async def undo(request: Request, target: str):
        return await write(request, lambda a, k, b: on().undo(a, k, target, b.get("reason")))

    @app.post("/api/settings/check")
    async def settings_check(request: Request):
        return await write(request, lambda _a, _k, b: on().check_settings(b.get("doc")), replay=False)

    @app.post("/api/settings")
    async def settings_save(request: Request):
        return await write(request, lambda a, k, b: on().save_settings(a, k, b))

    @app.post("/api/roasters/{roaster_id}/crawl")
    async def crawl_flag(request: Request, roaster_id: str):
        return await write(request, lambda a, k, b: on().set_crawl(a, k, roaster_id, b))

    @app.post("/api/drafts")
    async def draft(request: Request):
        return await write(request, lambda a, k, b: on().build_draft(a, k, b.get("override") or None))

    @app.post("/api/drafts/{draft_id}/publish")
    async def publish(request: Request, draft_id: str):
        return await write(
            request,
            lambda a, k, b: on().publish(
                a, k, draft_id, b.get("typed"), b.get("reason"), b.get("override") or None
            ),
        )

    @app.post("/api/evaluations")
    async def evaluation(request: Request):
        return await write(
            request, lambda a, k, b: on().evaluation(a, k, str(b.get("path") or ""), b.get("reason"))
        )

    @app.post("/api/drafts/{draft_id}/discard")
    async def discard(request: Request, draft_id: str):
        return await write(request, lambda a, k, b: on().discard(a, k, draft_id))

    @app.post("/api/releases/{build_id}/rollback")
    async def rollback(request: Request, build_id: str):
        return await write(
            request, lambda a, k, b: on().rollback(a, k, build_id, b.get("reason"), b.get("override") or None)
        )

    @app.get("/api/decisions/export")
    async def export(request: Request):
        """The effective decisions as a snapshot document: the input of `coffeedata decide export-labels`."""
        if writes is None:
            return JSONResponse({"detail": "Writes are off: no control plane."}, 503)
        try:
            await writes.ready()
            effective = await asyncio.to_thread(writes.control.effective)
            served = await reader.run(queries.served)
            hashes = [d["subject"] for d in effective if d["subject_kind"] == "input_hash"]
            if (
                hashes and served
            ):  # each version's coffees, so export-labels checks every one against held-out sets
                members = await reader.run(lambda _l, _c: catalog.members(served["build_id"], [], hashes))
                for d in effective:
                    if d["subject_kind"] == "input_hash":
                        d["coffee_ids"] = sorted(
                            m["coffee_id"] for m in members if m["input_hash"] == d["subject"]
                        )
        except ControlPlaneError:
            return JSONResponse({"detail": "The control plane can't be read right now."}, 503)
        doc = decisions.snapshot("export", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), effective)
        return JSONResponse(doc, headers={"Content-Disposition": 'attachment; filename="decisions.json"'})

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        return page("error.html", 404, message="There is no such page.", title="Not found")

    return app


class Limiter:
    """At most `n` writes per session in 10 minutes (design §5.3: write limits per identity)."""

    def __init__(self, n: int = WRITES_PER_10_MIN):
        self.n = n
        self.seen: dict[str, list[float]] = {}

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.seen.get(who, []) if now - t < 600]
        if len(recent) >= self.n:
            self.seen[who] = recent
            return False
        self.seen[who] = [*recent, now]
        return True


def new_key() -> str:
    """A form's idempotency key: a double submit or a retry of it acts once."""
    return secrets.token_urlsafe(18)


def _draft_state(d: dict, build: dict | None, served: dict | None, run: dict | None) -> str:
    """A draft's state, derived (design §5.1): discarded, building, failed (its build), stale (the served release
    changed since), published (served now or before, from serving_log), or ready to review."""
    if d["last_event"] == "discarded":
        return "discarded"
    if build is None:
        return "failed" if run and run.get("status") in ("failed", "cancelled") else "building"
    if build.get("ever_served"):
        return "published"
    if not build["passed"]:
        return "failed"
    if (served or {}).get("build_id") != d["baseline"]:
        return "stale"
    return "publishing" if d["last_event"] == "publish_started" else "built"


def _with_latest(rows: list[dict], latest: dict[str, dict[str, dict]], into: str) -> None:
    """Each row's latest decision event, by coffee (dc) and by version (dh), and under `into` the approved one in
    effect, if any (the Catalog's and the Queue's rows alike)."""
    for r in rows:
        r["dc"] = latest["coffee_id"].get(r["coffee_id"])
        r["dh"] = latest["input_hash"].get(r.get("input_hash") or "")
        live = [d for d in (r["dc"], r["dh"]) if d and d["event"] == "approved"]
        r[into] = live[0] if live else None


def _reason(e: Exception) -> str:
    """What failed, in a line, without a trace or a path's secrets."""
    msg = str(e).splitlines()[0] if str(e) else ""
    msg = re.sub(r"hf://\S+|https?://\S+", "<lake>", msg)
    return f"{type(e).__name__}: {msg[:200]}" if msg else type(e).__name__


def _qs(request: Request, f: Filters, build: str | None) -> Callable[..., str]:
    """A link to this Catalog view with one filter changed or removed (None removes it), and the page reset."""
    base = {k: v for k, v in request.query_params.items() if k != "page"}
    if build:
        base["build"] = build

    def link(**change) -> str:
        q = dict(base)
        for k, v in change.items():
            if v is None:
                q.pop(k, None)
            else:
                q[k] = str(v)
        return "/catalog?" + urlencode(q)

    return link
