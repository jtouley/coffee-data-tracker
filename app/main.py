"""The shot-logging app. Run locally with `make app`; it reads and writes the lake in COFFEEDATA_LAKE (./lake).

Screens are server-rendered HTML with a little JavaScript (static/app.js) and no build step. Every write is a
JSON POST that the phone first puts in an IndexedDB outbox, with an id it generated, so a retry is safe.
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
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

from . import labels, photos, views, web
from .accounts import AccountError
from .auth import COOKIE, Auth, Sessions, User, from_env, missing_secrets
from .store import Invalid, NotFound, Store

HERE = Path(__file__).parent
MAX_BODY = 64 * 1024  # JSON writes are a few hundred bytes
READS_PER_DAY = 10  # label reads per account per day (PLAN, "Cost")
log = logging.getLogger(__name__)


class SignIn(Exception):
    """A screen asked for while signed out: go to the sign-in screen."""


def not_configured(missing: list[str]) -> FastAPI:
    """What a Space shows until its sign-in secrets are set: their names, never their values."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    text = (
        "This app isn't set up yet. Add these secrets in the Space's settings, then restart it: "
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
        missing = missing_secrets()
        if missing:
            log.error("not configured; missing secrets: %s", ", ".join(missing))
            return not_configured(missing)
        auth = from_env()
    lake = lake or Lake()
    # Records from before accounts (user_id null) belong to the owner.
    store = Store(lake, shots.owner_user_id())
    sessions = auth if isinstance(auth, Sessions) else None
    app = FastAPI(title="registry.coffee", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    assets = web.Assets()
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(g=views.g, ratio=views.ratio, shots_n=views.n_shots, asset=assets)
    # Added innermost first: the body limit, then compression, then (outermost) the headers every response gets.
    app.add_middleware(web.LimitBody, default=MAX_BODY, limits={"/api/photos/": photos.MAX_UPLOAD})
    app.add_middleware(GZipMiddleware, minimum_size=500)

    async def on_error(request: Request) -> Response:
        log.exception("%s %s failed", request.method, request.url.path)  # no headers or body: no tokens
        if wants_html(request):
            return page(request, "error.html", status=500, code=500)
        return JSONResponse({"detail": "Something went wrong on the server."}, 500)

    app.middleware("http")(web.middleware(sessions.set_cookie if sessions else None, COOKIE, on_error))  # type: ignore[arg-type]
    hops = int(os.environ.get("COFFEEDATA_PROXY_HOPS") or 0)

    def ip(request: Request) -> str:
        return web.client_ip(request, hops)

    def user(request: Request) -> User:
        u = auth.user(request)
        if u is None:
            raise HTTPException(401, "sign in first")
        return u

    def screen_user(request: Request) -> User:
        u = auth.user(request)
        if u is None:
            raise SignIn()
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
            {"me": None, "theme": request.cookies.get("theme", "auto")}
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
        return page(request, "signin.html", owner_setup=s.accounts.needs_owner, owner=owner_name(s))

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

    def owner_name(s: Sessions) -> str:
        return next((a.username for a in s.accounts.people() if a.role == "owner"), "the owner")

    @app.get("/join", response_class=HTMLResponse)
    def join_page(request: Request) -> HTMLResponse:
        # The code is after # in the link, so it never reaches the server or its logs; the page's script reads it.
        s = accounts_only()
        return page(request, "join.html", owner=owner_name(s))

    @app.get("/reset", response_class=HTMLResponse)
    def reset_page(request: Request) -> HTMLResponse:
        s = accounts_only()
        return page(request, "reset.html", owner=owner_name(s))

    @app.post("/api/invites/check", dependencies=[Depends(anyone)])
    def check_invite(body: dict, request: Request) -> dict:
        s = accounts_only()
        try:
            inv = s.accounts.lookup(str(body.get("code") or ""), ip(request))
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        return {"kind": inv.kind, "expires": f"{inv.expires_at:%B} {inv.expires_at.day}"}

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
        for r in store.shots.values():
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
    ) -> HTMLResponse | RedirectResponse:
        p = store.prefill(me.user_id, grinder, basket, machine)
        if not p["grinder"] or not p["basket"]:
            return RedirectResponse("/setup", 303)
        if not p["bag"] and not p["last"]:
            return RedirectResponse("/bag/new", 303)
        name, _ = coffee_name(p["bag"], p["coffee_id"], p["coffee_freetext"])
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
        return page(request, "log.html", me=me, p=p, name=name, form=form)

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
            theme=request.cookies.get("theme", "auto"),
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

    @app.post("/api/equipment")
    def post_equipment(body: dict, me: Writer) -> dict:
        return {"equipment_id": run(store.save_equipment, me.user_id, body).equipment_id}

    photo_lake = photos_lake or Lake.photos()
    reads_today: dict[
        tuple[str, str], int
    ] = {}  # (user, date) -> label reads; in memory, so a restart resets it

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
        key = (me.user_id, datetime.now(UTC).date().isoformat())
        if reads_today.get(key, 0) >= READS_PER_DAY:
            # Past the day's reads the photo isn't kept either, so uploads can't fill the dataset.
            return {"photo_id": None, "status": "limit", "label": None, "raw": None}
        reads_today[key] = reads_today.get(key, 0) + 1
        # The dataset commit (which can retry for a while) and the label read run side by side.
        _, result = await asyncio.gather(
            run_in_threadpool(photos.save, photo_lake, photo_id, me.user_id, photo),
            run_in_threadpool(labels.read, photo.jpeg, None, labels_client),
        )
        out: dict[str, Any] = {
            "photo_id": photo_id,
            "status": result.status,
            "label": result.label,
            "raw": result.raw,
        }
        if result.status == "read" and result.label:
            out |= await run_in_threadpool(matched, result)
        return out

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
        bag, matches = run(store.save_bag, me.user_id, body)
        return {"bag_id": bag.bag_id, "coffee_id": bag.coffee_id, "matches": matches}

    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn app.main:app` builds the app on first use, so importing this module (tests) has no side effects.
    if name == "app":
        globals()["app"] = a = create_app()
        return a
    raise AttributeError(name)
