"""The shot-logging app. Run locally with `make app`; it reads and writes the lake in COFFEEDATA_LAKE (./lake).

Screens are server-rendered HTML with a little JavaScript (static/app.js) and no build step. Every write is a
JSON POST that the phone first puts in an IndexedDB outbox, with an id it generated, so a retry is safe.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from coffeedata import shots
from coffeedata.lake import Lake
from coffeedata.models import SAFE_ID
from coffeedata.site import links as site_links

from . import labels, logs, photos, views, web
from .accounts import AccountError
from .auth import COOKIE, Auth, Sessions, User, clean_secrets, from_env, missing_secrets
from .store import Invalid, NotFound, Store, coffee_key

HERE = Path(__file__).parent
MAX_BODY = 64 * 1024  # JSON writes are a few hundred bytes
READS_PER_DAY = 10  # label reads per account per day (PLAN, "Cost")
PHOTO_MISS_TTL = 300  # seconds an unknown photo id answers 404 without another lookup
log = logging.getLogger(__name__)


class SignIn(Exception):
    """A screen asked for while signed out: go to the sign-in screen."""


def not_configured(missing: list[str]) -> FastAPI:
    """What the hosted app shows until its secrets are set: their names, never their values."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    text = (
        "This app isn't set up yet. Add these secrets (Secret Manager, see RUNNING.md), then deploy again: "
        + ", ".join(missing)
        + "."
    )

    @app.middleware("http")
    async def everything(request: Request, call_next: Any) -> Response:
        response = PlainTextResponse(text, 503)
        web.secure_headers(response, request.url.path)
        return response

    return app


def gear_counts(shots_: list) -> dict[str, int]:
    """Shots per piece of equipment, from one pass over one user's shots."""
    out: dict[str, int] = {}
    for sh in shots_:
        for e in {sh.grinder_id, sh.machine_id, sh.basket_id} - {None}:
            out[e] = out.get(e, 0) + 1
    return out


def create_app(
    lake: Lake | None = None,
    auth: Auth | None = None,
    photos_lake: Lake | None = None,
    labels_client: Any = None,
) -> FastAPI:
    if auth is None:
        clean_secrets()
        missing = missing_secrets()
        if missing:
            log.error("not configured; missing secrets: %s", ", ".join(missing))
            return not_configured(missing)
        auth = from_env()
    lake = lake or Lake()
    # Records from before accounts (user_id null) belong to the owner.
    store = Store(lake, shots.owner_user_id())
    logs.event("started", shots=len(store.shots), bags=len(store.bags), equipment=len(store.equipment),
               lake=lake.url, auth=getattr(auth, "name", type(auth).__name__))  # fmt: skip
    sessions = auth if isinstance(auth, Sessions) else None
    app = FastAPI(title="registry.coffee", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    assets = web.Assets()
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(g=views.g, ratio=views.ratio, shots_n=views.n_shots, asset=assets)
    # The public site's address and the beta-invite email, from the same settings the site is built with.
    templates.env.globals.update(site=site_links())
    # Added innermost first: the body limit, then compression, then (outermost) the headers every response gets.
    app.add_middleware(web.LimitBody, default=MAX_BODY, limits={"/api/photos/": photos.MAX_UPLOAD})
    app.add_middleware(GZipMiddleware, minimum_size=500)

    async def on_error(request: Request) -> Response:
        log.exception("%s %s failed", request.method, request.url.path)  # no headers or body: no tokens
        if wants_html(request):
            return page(request, "error.html", status=500, code=500)
        return JSONResponse({"detail": "Something went wrong on the server."}, 500)

    app.middleware("http")(web.middleware(sessions.set_cookie if sessions else None, COOKIE, on_error))  # type: ignore[arg-type]
    app.middleware("http")(logs.middleware)  # outermost: it sees the final status, error pages included
    hops = int(os.environ.get("COFFEEDATA_PROXY_HOPS") or 0)

    def ip(request: Request) -> str:
        return web.client_ip(request, hops)

    def user(request: Request) -> User:
        u = auth.user(request)
        if u is None:
            raise HTTPException(401, "sign in first")
        logs.set_user(u.user_id)
        return u

    def screen_user(request: Request) -> User:
        u = auth.user(request)
        if u is None:
            raise SignIn()
        logs.set_user(u.user_id)
        return u

    def same_origin(request: Request, x_requested_with: str | None) -> None:
        """CSRF: a cross-site form can't set X-Requested-With, and the browser says where a request came from.
        On top of SameSite=Lax cookies."""
        if not x_requested_with:
            raise HTTPException(403, "missing X-Requested-With")
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            raise HTTPException(403, "cross-site request")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-site request")

    def writer(request: Request, x_requested_with: Annotated[str | None, Header()] = None) -> User:
        same_origin(request, x_requested_with)
        return user(request)

    def anyone(request: Request, x_requested_with: Annotated[str | None, Header()] = None) -> None:
        same_origin(request, x_requested_with)

    Me = Annotated[User, Depends(screen_user)]
    Writer = Annotated[User, Depends(writer)]

    def tz(request: Request) -> tzinfo:
        try:
            return ZoneInfo(request.cookies.get("tz") or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            return UTC

    def page(request: Request, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        ctx = (
            {
                "me": None,
                "theme": request.cookies.get("theme", "auto"),
                "hand": request.cookies.get("hand", "right"),
            }
            | ctx
            | {"now": datetime.now(tz(request))}
        )
        return templates.TemplateResponse(request, template, ctx, status_code=status)

    # --- errors --------------------------------------------------------------------------------------

    def wants_html(request: Request) -> bool:
        return not request.url.path.startswith("/api/") and "text/html" in request.headers.get("accept", "")

    @app.exception_handler(SignIn)
    async def to_sign_in(request: Request, exc: SignIn) -> Response:
        return RedirectResponse("/signin", 303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if wants_html(request):
            return page(request, "error.html", status=exc.status_code, code=exc.status_code)
        return JSONResponse({"detail": exc.detail}, exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        return JSONResponse({"detail": [e["msg"] for e in exc.errors()]}, 422)

    # --- the shell -----------------------------------------------------------------------------------

    @app.get("/static/{path:path}")
    def static(path: str) -> Response:
        return assets.response(f"/static/{path}")

    fonts = ("archivo", "archivo-ext", "newsreader", "newsreader-ext")
    shell = [assets(f) for f in ("app.css", "app.js", *(f"fonts/{f}.woff2" for f in fonts))]
    sw = (
        (HERE / "sw.js")
        .read_text()
        .replace("__SHELL__", json.dumps(shell + ["/offline"]))
        .replace("__VERSION__", hashlib.sha256("|".join(shell).encode()).hexdigest()[:10])
    )

    @app.get("/sw.js")
    def service_worker() -> Response:
        # Unhashed and never cached, so a new deploy's worker (with the new asset names) is picked up.
        return Response(sw, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    manifest = json.dumps(
        {
            "name": "registry.coffee",
            "short_name": "Coffee",
            "description": "Log espresso shots at the machine and see how each bag dials in.",
            "id": "/",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#f6f5f2",
            "theme_color": "#f6f5f2",
            "icons": [
                {
                    "src": assets("icons/icon-192.png"),
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": assets("icons/icon-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": assets("icons/icon-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        }
    )

    @app.get("/manifest.webmanifest")
    def web_manifest() -> Response:
        return Response(manifest, media_type="application/manifest+json")

    @app.get("/robots.txt")
    def robots() -> PlainTextResponse:
        # Crawlers may look: every page but sign-in needs an account, so there's nothing for them to see.
        return PlainTextResponse("User-agent: *\nAllow: /\n")

    # /health for the uptime check: Google's front end answers any path ending in "z" itself (a 404 that never reaches
    # the app). /healthz stays for Cloud Run's startup probe, which calls the container directly.
    @app.get("/health")
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/offline", response_class=HTMLResponse)
    def offline(request: Request) -> HTMLResponse:
        return page(request, "offline.html")

    # --- signing in ----------------------------------------------------------------------------------

    def accounts_only() -> Sessions:
        if sessions is None:
            raise HTTPException(404, "sign-in is off in local development")
        return sessions

    @app.get("/signin", response_class=HTMLResponse, response_model=None)
    def signin(request: Request) -> HTMLResponse | RedirectResponse:
        if auth.user(request):
            return RedirectResponse("/", 303)
        s = accounts_only()
        return page(request, "signin.html", owner_setup=s.accounts.needs_owner)

    @app.post("/api/signin", dependencies=[Depends(anyone)])
    def post_signin(body: dict, request: Request) -> Response:
        s = accounts_only()
        username, password = str(body.get("username") or ""), str(body.get("password") or "")
        try:
            if s.accounts.needs_owner:
                acct = s.accounts.create_owner(
                    str(body.get("setup_code") or ""), username, password, ip(request)
                )
            else:
                acct = s.accounts.sign_in(username, password, ip(request))
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        response = JSONResponse({"ok": True})
        s.set_cookie(response, acct)
        return response

    @app.post("/api/signout")
    def post_signout(me: Writer) -> Response:
        response = JSONResponse({"ok": True})
        accounts_only().clear_cookie(response)
        return response

    @app.post("/api/signout-everywhere")
    def post_signout_everywhere(me: Writer) -> Response:
        s = accounts_only()
        s.accounts.sign_out_everywhere(me.user_id)
        response = JSONResponse({"ok": True})
        s.clear_cookie(response)
        return response

    @app.post("/api/password")
    def post_password(body: dict, me: Writer) -> Response:
        s = accounts_only()
        try:
            acct = s.accounts.change_password(
                me.user_id, str(body.get("current") or ""), str(body.get("new") or "")
            )
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        response = JSONResponse({"ok": True})
        s.set_cookie(response, acct)  # this phone stays signed in; the others don't
        return response

    def coffee_name(bag: Any, coffee_id: str | None, freetext: str | None) -> tuple[str, str]:
        """The serif name and the line under it."""
        c = store.coffee(coffee_id)
        if c:
            facts = ", ".join(x for x in (c.get("origin_country"), c.get("process")) if x)
            return c.get("title") or "", " · ".join(x for x in (c.get("roaster_name"), facts) if x)
        return freetext or (bag and bag.coffee_freetext) or "Unnamed coffee", "Not matched to the catalog"

    # --- invites, joining and resetting --------------------------------------------------------------

    @app.get("/join", response_class=HTMLResponse)
    def join_page(request: Request) -> HTMLResponse:
        # The code is after # in the link, so it never reaches the server or its logs; the page's script reads it.
        accounts_only()
        return page(request, "join.html")

    @app.get("/reset", response_class=HTMLResponse)
    def reset_page(request: Request) -> HTMLResponse:
        accounts_only()
        return page(request, "reset.html")

    @app.post("/api/invites/check", dependencies=[Depends(anyone)])
    def check_invite(body: dict, request: Request) -> dict:
        s = accounts_only()
        try:
            inv = s.accounts.lookup(str(body.get("code") or ""), ip(request))
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        exp = inv.expires_at.astimezone(tz(request))  # the viewer's day, as on the invite screen
        return {"kind": inv.kind, "expires": f"{exp:%B} {exp.day}"}

    @app.post("/api/join", dependencies=[Depends(anyone)])
    def post_join(body: dict, request: Request) -> Response:
        s = accounts_only()
        try:
            acct = s.accounts.join(
                str(body.get("code") or ""),
                str(body.get("username") or ""),
                str(body.get("password") or ""),
                ip(request),
            )
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        response = JSONResponse({"ok": True})
        s.set_cookie(response, acct)
        return response

    @app.post("/api/reset", dependencies=[Depends(anyone)])
    def post_reset(body: dict, request: Request) -> Response:
        s = accounts_only()
        try:
            acct = s.accounts.reset(str(body.get("code") or ""), str(body.get("password") or ""), ip(request))
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        response = JSONResponse({"ok": True})
        s.set_cookie(response, acct)
        return response

    @app.get("/invite", response_class=HTMLResponse)
    def invite_page(request: Request, me: Me) -> HTMLResponse:
        s = accounts_only()
        if me.role != "owner":
            raise HTTPException(404, "not found")
        z = tz(request)
        mine = counts_by_user()
        return page(
            request,
            "invite.html",
            me=me,
            people=[a for a in s.accounts.people() if a.role != "owner"],
            pending=s.accounts.pending(),
            shots_of=lambda uid: mine.get(uid, 0),
            date=lambda d: f"{d.astimezone(z):%A, %B} {d.astimezone(z).day}",
        )

    def counts_by_user() -> dict[str, int]:
        out: dict[str, int] = {}
        for r in list(store.shots.values()):  # a copy: another request may add a shot meanwhile
            if not r.deleted:
                out[r.item.user_id] = out.get(r.item.user_id, 0) + 1
        return out

    def owner_call(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except AccountError as e:
            raise HTTPException(400, str(e)) from None

    def code_reply(request: Request, inv: Any, code: str) -> dict:
        z = tz(request)
        e = inv.expires_at.astimezone(z)
        return {"invite_id": inv.invite_id, "kind": inv.kind, "code": code, "expires": f"{e:%A, %B} {e.day}"}

    @app.post("/api/invites")
    def post_invite(body: dict, request: Request, me: Writer) -> dict:
        s = accounts_only()
        kind = "reset" if body.get("kind") == "reset" else "join"
        inv, code = owner_call(s.accounts.invite, me.user_id, kind, body.get("for_user_id"))
        return code_reply(request, inv, code)

    @app.post("/api/invites/{invite_id}/renew")
    def renew_invite(invite_id: str, request: Request, me: Writer) -> dict:
        inv, code = owner_call(accounts_only().accounts.renew, me.user_id, invite_id)
        return code_reply(request, inv, code)

    @app.post("/api/invites/{invite_id}/revoke")
    def revoke_invite(invite_id: str, me: Writer) -> dict:
        owner_call(accounts_only().accounts.revoke, me.user_id, invite_id)
        return {"ok": True}

    @app.post("/api/people/{user_id}")
    def set_person(user_id: str, body: dict, me: Writer) -> dict:
        acct = owner_call(
            accounts_only().accounts.set_disabled, me.user_id, user_id, bool(body.get("disabled"))
        )
        return {"user_id": acct.user_id, "disabled": acct.disabled}

    # --- screens -------------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, me: Me) -> HTMLResponse:
        p = store.prefill(me.user_id)
        bag, last = p["bag"], p["last"]
        if not bag and not last:
            return page(request, "first_run.html", me=me, grinder=p["grinder"], basket=p["basket"])
        name, facts = coffee_name(bag, p["coffee_id"], p["coffee_freetext"])
        pulled = store.bag_shots(me.user_id, bag, last)
        z = tz(request)
        prev = pulled[-2] if len(pulled) > 1 else None
        return page(
            request,
            "today.html",
            p=p,
            name=name,
            facts=facts,
            shots=pulled,
            earlier=list(reversed(pulled[:-1]))[:3],
            target=views.target_svg(pulled) if pulled else "",
            summary=views.summary(pulled[-1], prev, z) if pulled else "",
            me=me,
            grinder_of=lambda s: store.gear(me.user_id, s.grinder_id),
            days_off=(datetime.now(z).date() - bag.roast_date).days if bag and bag.roast_date else None,
            day=lambda d: views.day(d, z),
            clock=lambda d: views.clock(d, z),
            # "7:06" today, "Sun 14:04" on an earlier day
            when=lambda d: (
                views.clock(d, z) if d.astimezone(z).date() == datetime.now(z).date() else views.day(d, z)
            ),
        )

    @app.get("/log", response_class=HTMLResponse, response_model=None)
    def log_shot(
        request: Request,
        me: Me,
        grinder: str | None = None,
        basket: str | None = None,
        machine: str | None = None,
        correct: str | None = None,
    ) -> HTMLResponse | RedirectResponse:
        fixing = None
        if (
            correct
        ):  # Correct: the same form, filled from that shot, saved under its id (a new record, latest wins)
            try:
                fixing = store.shot(me.user_id, correct)
            except NotFound:
                raise HTTPException(404, "no such shot") from None
            grinder, basket, machine = fixing.grinder_id, fixing.basket_id, fixing.machine_id
        p = store.prefill(me.user_id, grinder, basket, machine)
        if not p["grinder"] or not p["basket"]:
            return RedirectResponse("/setup", 303)
        if not p["bag"] and not p["last"]:
            return RedirectResponse("/bag/new", 303)
        name, _ = coffee_name(p["bag"], p["coffee_id"], p["coffee_freetext"])
        if fixing:
            # The shot's own gear, even retired; prefill would swap in the current defaults.
            for kind, gid in (
                ("grinder", fixing.grinder_id),
                ("basket", fixing.basket_id),
                ("machine", fixing.machine_id),
            ):
                p[kind] = store.gear(me.user_id, gid) or (p[kind] if kind != "machine" else None)
        g = p["grinder"]
        form = {
            "bag_id": p["bag"] and p["bag"].bag_id,
            "coffee_id": p["coffee_id"],
            "coffee_freetext": p["coffee_freetext"],
            "grinder_id": g.equipment_id,
            "basket_id": p["basket"].equipment_id,
            "machine_id": p["machine"] and p["machine"].equipment_id,
            "dose_g": p["dose_g"],
            "yield_g": p["yield_g"],
            "grinder_setting": p["grinder_setting"],
            "water_temp_c": p["water_temp_c"],
            "brew_method": p["brew_method"],
            "is_milk_drink": p["is_milk_drink"],
            "last_time_s": p["last_time_s"],
            "same_as_last": p["same_as_last"],
            "grind": {"min": g.scale_min, "max": g.scale_max, "step": g.scale_step},
        }
        if fixing:
            form |= {
                "bag_id": fixing.bag_id,
                "coffee_id": fixing.coffee_id,
                "coffee_freetext": fixing.coffee_freetext,
                "dose_g": fixing.dose_g,
                "yield_g": fixing.yield_g,
                "grinder_setting": fixing.grinder_setting,
                "water_temp_c": fixing.water_temp_c,
                "brew_method": fixing.brew_method,
                "is_milk_drink": fixing.is_milk_drink,
                "grinder_id": fixing.grinder_id,
                "basket_id": fixing.basket_id,
                "machine_id": fixing.machine_id,
                "correct": {
                    "shot_id": fixing.shot_id,
                    "brewed_at": fixing.brewed_at.isoformat(),
                    "time_s": fixing.time_s,
                    "taste": fixing.taste,
                    "rating": fixing.rating,
                    "notes": fixing.notes,
                },
            }
            bag = store.bag(me.user_id, fixing.bag_id)
            name, _ = coffee_name(bag, fixing.coffee_id, fixing.coffee_freetext)
        z = tz(request)
        when = f"{views.date(fixing.brewed_at, z)}, {views.clock(fixing.brewed_at, z)}" if fixing else None
        return page(request, "log.html", me=me, p=p, name=name, form=form, fixing=fixing, fixing_when=when)

    @app.get("/saved/{shot_id}", response_class=HTMLResponse)
    def saved(request: Request, shot_id: str, me: Me, new: bool = False) -> HTMLResponse:
        try:
            shot = store.shot(me.user_id, shot_id)
        except NotFound:
            raise HTTPException(404, "no such shot") from None
        bag = store.bag(me.user_id, shot.bag_id)
        pulled = [s for s in store.bag_shots(me.user_id, bag, shot) if s.brewed_at <= shot.brewed_at]
        prev = pulled[-2] if len(pulled) > 1 else None
        name, _ = coffee_name(bag, shot.coffee_id, shot.coffee_freetext)
        z = tz(request)
        return page(
            request,
            "saved.html",
            me=me,
            shot=shot,
            name=name,
            new=new,
            grinder=store.gear(me.user_id, shot.grinder_id),
            target=views.target_svg(pulled, 196),
            sequence=views.sequence(pulled),
            summary=views.summary(shot, prev, z),
            has_prev=prev is not None,
            clock=views.clock(shot.brewed_at, z),
        )

    # --- coffees, history, one coffee, one shot -------------------------------------------------------

    def bag_line(bag: Any, shots_: list) -> dict:
        name, facts = coffee_name(bag, bag.coffee_id, bag.coffee_freetext)
        best = store.best(shots_)
        return {"bag": bag, "name": name, "facts": facts, "shots": shots_, "best": best}

    @app.get("/coffees", response_class=HTMLResponse)
    def coffees(request: Request, me: Me) -> HTMLResponse:
        mine = store.shots_of(me.user_id)
        per_bag: dict[str | None, list] = {}
        for s in mine:
            per_bag.setdefault(s.bag_id, []).append(s)
        lines = [bag_line(b, per_bag.get(b.bag_id, [])) for b in store.bags_of(me.user_id)]
        z = tz(request)
        return page(request, "coffees.html", me=me, lines=lines, loose=[s for s in mine if not s.bag_id],
                    date=lambda d: views.date(d, z))  # fmt: skip

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, me: Me) -> HTMLResponse:
        """Every shot, newest first, grouped by bag."""
        mine = store.shots_of(me.user_id)
        by_key: dict[str, dict] = {}  # insertion order: the group whose newest shot is newest comes first
        for sh in reversed(mine):
            key = sh.bag_id or coffee_key(sh)
            if key not in by_key:
                bag = store.bag(me.user_id, sh.bag_id)
                name, _ = coffee_name(bag, sh.coffee_id, sh.coffee_freetext)
                by_key[key] = {"key": key, "bag": bag, "name": name, "shots": []}
            by_key[key]["shots"].append(sh)
        groups = list(by_key.values())
        z = tz(request)
        return page(request, "history.html", me=me, groups=groups, day=lambda d: views.day(d, z))

    @app.get("/coffee/{bag_id}", response_class=HTMLResponse)
    def coffee_page(request: Request, bag_id: str, me: Me) -> HTMLResponse:
        bag = store.bag(me.user_id, bag_id)
        if bag is None:
            raise HTTPException(404, "no such bag")
        everything = store.coffee_shots(me.user_id, bag)
        this_bag = [s for s in everything if s.bag_id == bag.bag_id]
        # Numbered in the order they were opened, which doesn't change as you switch between them.
        siblings = sorted(
            (b for b in store.bags_of(me.user_id) if coffee_key(b) == coffee_key(bag)),
            key=lambda b: (b.opened_on or date.min, b.bag_id),
        )
        name, facts = coffee_name(bag, bag.coffee_id, bag.coffee_freetext)
        if c := store.coffee(bag.coffee_id):
            facts = c.get("roaster_name") or ""  # the origin is under From the catalog
        z = tz(request)
        return page(
            request,
            "coffee.html",
            me=me,
            bag=bag,
            name=name,
            facts=facts,
            catalog=describe(bag.coffee_id),
            raw=store.coffee(bag.coffee_id) or {},
            best=store.best(everything),
            chart=views.target_svg(
                everything, 196, label="Every shot of this coffee by time and ratio", sequence=False
            )
            if everything
            else "",
            n_all=len(everything),
            tastes={s.taste or "none" for s in everything},  # the key shows only the marks on the chart
            shots=list(reversed(this_bag)),
            siblings=siblings,
            index=next((i for i, b in enumerate(siblings, 1) if b.bag_id == bag.bag_id), 1),
            grinder_of=lambda s: store.gear(me.user_id, s.grinder_id),
            days_off=((bag.finished_on or datetime.now(z).date()) - bag.roast_date).days
            if bag.roast_date
            else None,
            day=lambda d: views.day(d, z),
            date=lambda d: views.date(d, z),
        )

    @app.get("/shot/{shot_id}", response_class=HTMLResponse)
    def shot_page(
        request: Request, shot_id: str, me: Me, from_: str = Query("", alias="from")
    ) -> HTMLResponse:
        try:
            shot = store.shot(me.user_id, shot_id)
        except NotFound:
            raise HTTPException(404, "no such shot") from None
        bag = store.bag(me.user_id, shot.bag_id)
        name, _ = coffee_name(bag, shot.coffee_id, shot.coffee_freetext)
        z = tz(request)
        # Back to where it was opened from: History, else the bag's coffee page.
        back = ("/history", "History") if from_ == "history" or not bag else (f"/coffee/{bag.bag_id}", name)
        return page(request, "shot.html", me=me, shot=shot, bag=bag, name=name, back=back, grinder=store.gear(me.user_id, shot.grinder_id),
                    basket=store.gear(me.user_id, shot.basket_id), machine=store.gear(me.user_id, shot.machine_id),
                    when=f"{views.date(shot.brewed_at, z)}, {views.clock(shot.brewed_at, z)}")  # fmt: skip

    # The most recently viewed photos (least recently used out first), and ids recently found missing, so a bad id
    # or a long list of thumbnails doesn't mean a remote read per request.
    photo_cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
    photo_misses: OrderedDict[str, float] = OrderedDict()

    @app.get("/photos/{photo_id}")
    async def photo(photo_id: str, me: Me) -> Response:
        """A bag photo, to its owner only. Photos never change, so the browser keeps them (privately)."""
        if not SAFE_ID.fullmatch(photo_id):
            raise HTTPException(404, "not found")
        hit = photo_cache.get(photo_id)
        if hit is None:
            if time.monotonic() - photo_misses.get(photo_id, -PHOTO_MISS_TTL) < PHOTO_MISS_TTL:
                raise HTTPException(404, "not found")
            hit = await run_in_threadpool(photos.load, photo_lake, photo_id)
            if hit is None:
                photo_misses[photo_id] = time.monotonic()
                if len(photo_misses) > 1024:
                    photo_misses.popitem(last=False)
                raise HTTPException(404, "not found")
            photo_cache[photo_id] = hit
            if len(photo_cache) > 32:
                photo_cache.popitem(last=False)
        photo_cache.move_to_end(photo_id)
        if hit[0] != me.user_id:
            raise HTTPException(404, "not found")
        return Response(
            hit[1], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"}
        )

    def counts(user_id: str) -> dict[str, int]:
        return gear_counts(store.shots_of(user_id))

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, me: Me) -> HTMLResponse:
        return page(
            request,
            "setup.html",
            me=me,
            auth_name=getattr(auth, "name", ""),
            gear={k: store.equipment_of(me.user_id, k) for k in ("grinder", "machine", "basket")},
            counts=counts(me.user_id),
        )

    @app.get("/setup/gear/{equipment_id}", response_class=HTMLResponse)
    def gear_page(request: Request, equipment_id: str, me: Me, kind: str = "grinder") -> HTMLResponse:
        """Add (`new`) or change one grinder, machine or basket."""
        item = None if equipment_id == "new" else store.gear(me.user_id, equipment_id)
        if equipment_id != "new" and item is None:
            raise HTTPException(404, "no such equipment")
        if kind not in ("grinder", "machine", "basket"):
            kind = "grinder"
        kind = item.kind if item else kind
        others = [
            e for e in store.equipment_of(me.user_id, kind) if not item or e.equipment_id != item.equipment_id
        ]
        return page(request, "gear.html", me=me, item=item, kind=kind, others=others,
                    shots=counts(me.user_id).get(equipment_id, 0))  # fmt: skip

    @app.get("/log/gear", response_class=HTMLResponse)
    def pick_gear(
        request: Request,
        me: Me,
        grinder: str | None = None,
        basket: str | None = None,
        machine: str | None = None,
    ) -> HTMLResponse:
        """The chip on Log shot: which grinder, machine and basket this shot is on."""
        p = store.prefill(me.user_id, grinder, basket, machine)
        return page(
            request,
            "pick_gear.html",
            me=me,
            grinders=store.equipment_of(me.user_id, "grinder"),
            baskets=store.equipment_of(me.user_id, "basket"),
            machines=store.equipment_of(me.user_id, "machine"),
            grinder=p["grinder"],
            basket=p["basket"],
            machine=p["machine"],
        )

    @app.get("/bag/new", response_class=HTMLResponse)
    def new_bag(request: Request, me: Me) -> HTMLResponse:
        return page(request, "bag.html", me=me, reading=labels_client is not None or labels.available())

    # --- writes --------------------------------------------------------------------------------------

    def run(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except ValidationError as e:
            raise HTTPException(422, [err["msg"] for err in e.errors()]) from None
        except NotFound:
            raise HTTPException(404, "not found") from None
        except Invalid as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/shots")
    def post_shot(body: dict, me: Writer) -> dict:
        shot, written = run(store.save_shot, me.user_id, body)
        return {"shot_id": shot.shot_id, "written": written}

    @app.post("/api/shots/{shot_id}/retract")
    def retract(shot_id: str, me: Writer) -> dict:
        return {"shot_id": shot_id, "written": run(store.retract_shot, me.user_id, shot_id)}

    @app.post("/api/shots/{shot_id}/restore")
    def restore(shot_id: str, me: Writer) -> dict:
        return {"shot_id": shot_id, "written": run(store.restore_shot, me.user_id, shot_id)}

    @app.post("/api/equipment")
    def post_equipment(body: dict, me: Writer) -> dict:
        return {"equipment_id": run(store.save_equipment, me.user_id, body).equipment_id}

    photo_lake = photos_lake or Lake.photos()
    reads_today: dict[
        tuple[str, str], int
    ] = {}  # (user, date) -> label reads; in memory, so a restart resets it
    # photo_id -> (user, matched coffee_id, score, raw read, roast date): in memory, for the offline hand-off
    photo_reads: dict[str, tuple[str, str | None, int | None, str | None, str | None]] = {}
    last_reply: dict[str, tuple[str, dict]] = {}  # photo_id -> (owner, reply): a resend isn't read twice

    def describe(coffee_id: str | None) -> dict | None:
        c = store.coffee(coffee_id)
        if not c:
            return None
        notes = c.get("tasting_notes")
        return {
            "coffee_id": coffee_id,
            "title": c.get("title"),
            "roaster": " · ".join(x for x in (c.get("roaster_name"), c.get("roaster_state")) if x),
            "origin": ", ".join(
                x for x in (c.get("origin_country"), c.get("origin_region"), c.get("process")) if x
            ),
            "notes": ", ".join(str(n) for n in notes) if notes is not None and len(notes) else None,
            "price": f"${c['price_per_100g']:.2f} / 100 g" if c.get("price_per_100g") else None,
            "in_stock": c.get("in_stock"),
        }

    @app.post("/api/photos/{photo_id}")
    async def post_photo(photo_id: str, request: Request, me: Writer) -> dict:
        """A bag photo: checked, re-encoded without metadata, saved, then read by Claude and matched in Python.
        Reading needs ANTHROPIC_API_KEY and is capped per account per day; without either, the person types it."""
        if not SAFE_ID.fullmatch(photo_id):
            raise HTTPException(422, "bad photo id")
        if request.headers.get("content-type", "").split(";")[0] not in (
            "image/jpeg",
            "image/png",
            "image/webp",
        ):
            raise HTTPException(415, "Send the photo as JPEG, PNG or WebP.")
        try:
            # Decoding, resizing and encoding take a moment of CPU: off the event loop, like the save and the read.
            photo = await run_in_threadpool(photos.normalize, await request.body())
        except photos.BadPhoto as e:
            raise HTTPException(422, str(e)) from None
        if photo_id in last_reply:  # a resend from the outbox: the same reply, without a second read
            owner, reply = last_reply[photo_id]
            if owner != me.user_id:
                raise HTTPException(409, "That photo id is taken.")
            return reply
        key = (me.user_id, datetime.now(UTC).date().isoformat())
        if reads_today.get(key, 0) >= READS_PER_DAY:
            # Past the day's reads the photo isn't kept either, so uploads can't fill the dataset.
            return {"photo_id": None, "status": "limit", "label": None, "raw": None}
        reads_today[key] = reads_today.get(key, 0) + 1
        # The dataset commit (which can retry for a while) and the label read run side by side.
        saved, result = await asyncio.gather(
            run_in_threadpool(photos.save, photo_lake, photo_id, me.user_id, photo),
            run_in_threadpool(labels.read, photo.jpeg, None, labels_client),
        )
        if (
            saved is False
            and (await run_in_threadpool(photos.load, photo_lake, photo_id) or ("",))[0] != me.user_id
        ):
            raise HTTPException(409, "That photo id is taken.")  # someone else's, from before a restart
        photo_misses.pop(photo_id, None)
        out: dict[str, Any] = {
            "photo_id": photo_id,
            "status": result.status,
            "label": result.label,
            "raw": result.raw,
        }
        if result.status == "read" and result.label:
            out |= await run_in_threadpool(matched, result)
            # Kept so a bag that cites this photo can use the read, whichever of the two reaches the server first
            # (offline, the photo and the bag wait in the outbox together).
            coffee_id = (out["match"] or {}).get("coffee_id")
            photo_reads[photo_id] = (me.user_id, coffee_id, out["score"], result.raw, out["roast_date"])
            await run_in_threadpool(apply_read, me.user_id, photo_id)
        last_reply[photo_id] = (me.user_id, out)
        return out

    def apply_read(user_id: str, photo_id: str) -> None:
        """A bag saved before its photo was read (typed while offline) takes the read's match, if there was one
        and the typed text matched nothing."""
        hit = photo_reads.get(photo_id)
        if not hit or hit[0] != user_id or not hit[1]:
            return
        for bag in store.bags_of(user_id):
            if bag.photo_id == photo_id and not bag.coffee_id:
                update = {"coffee_id": hit[1], "match_score": hit[2], "read_json": bag.read_json or hit[3]}
                if not bag.roast_date and hit[4]:
                    update["roast_date"] = hit[4]
                store.save_bag(user_id, bag.model_dump(exclude={"user_id"}) | update)

    def matched(result: labels.Read) -> dict[str, Any]:
        """The catalog match for a read label, described for the screen."""
        label = result.label or {}
        coffee_id, candidates = shots.match_bag(label.get("roaster"), label.get("coffee"), lake)
        described = [(describe(m["coffee_id"]), m["score"]) for m in candidates]
        roast = result.roast_date()
        return {
            "match": describe(coffee_id),
            "score": candidates[0]["score"] if coffee_id and candidates else None,
            "candidates": [d | {"score": score} for d, score in described if d],
            "roast_date": roast.isoformat() if roast else None,
        }

    @app.post("/api/bags")
    def post_bag(body: dict, me: Writer) -> dict:
        # Only a bag typed before its photo was read (offline) takes the photo's match. A bag typed after seeing the
        # read chose not to use it, and keeps what was typed (#20-1).
        pending = bool(body.pop("read_pending", False))
        bag, matches = run(store.save_bag, me.user_id, body)
        if pending and bag.photo_id and not bag.coffee_id:
            apply_read(me.user_id, bag.photo_id)  # the photo got here first and was read
            bag = store.bag(me.user_id, bag.bag_id) or bag
        return {"bag_id": bag.bag_id, "coffee_id": bag.coffee_id, "matches": matches}

    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn app.main:app` builds the app on first use, so importing this module (tests) has no side effects.
    if name == "app":
        if logs.wanted():
            logs.setup()
        globals()["app"] = a = create_app()
        return a
    raise AttributeError(name)
