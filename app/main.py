"""The shot-logging app. Run locally with `make app`; it reads and writes the lake in COFFEEDATA_LAKE (./lake).

Screens are server-rendered HTML with a little JavaScript (static/app.js) and no build step. Every write is a
JSON POST that the phone first puts in an IndexedDB outbox, with an id it generated, so a retry is safe.
"""

from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from coffeedata import shots
from coffeedata.lake import Lake

from . import views
from .auth import Auth, LocalOwner, User
from .store import NotFound, Store

HERE = Path(__file__).parent


def create_app(lake: Lake | None = None, auth: Auth | None = None) -> FastAPI:
    auth = auth or LocalOwner()
    lake = lake or Lake()
    # Records from before accounts (user_id null) belong to the owner.
    store = Store(lake, shots.owner_user_id())
    app = FastAPI(title="registry.coffee", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(g=views.g, ratio=views.ratio, shots_n=views.n_shots)

    def user(request: Request) -> User:
        u = auth.user(request)
        if u is None:
            raise HTTPException(401, "sign in first")
        return u

    def writer(request: Request, x_requested_with: Annotated[str | None, Header()] = None) -> User:
        # A cross-site form can't set this header; on top of SameSite cookies once real sessions exist.
        if not x_requested_with:
            raise HTTPException(403, "missing X-Requested-With")
        return user(request)

    Me = Annotated[User, Depends(user)]
    Writer = Annotated[User, Depends(writer)]

    def tz(request: Request) -> tzinfo:
        try:
            return ZoneInfo(request.cookies.get("tz") or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            return UTC

    def page(request: Request, template: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, template, ctx | {"now": datetime.now(tz(request))})

    def coffee_name(bag: Any, coffee_id: str | None, freetext: str | None) -> tuple[str, str]:
        """The serif name and the line under it."""
        c = store.coffee(coffee_id)
        if c:
            facts = ", ".join(x for x in (c["origin_country"], c["process"]) if x)
            return c["title"], " · ".join(x for x in (c["roaster_name"], facts) if x)
        return freetext or (bag and bag.coffee_freetext) or "Unnamed coffee", "Not matched to the catalog"

    # --- screens -------------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, me: Me) -> HTMLResponse:
        p = store.prefill(me.user_id)
        bag, last = p["bag"], p["last"]
        if not bag and not last:
            return page(request, "first_run.html", grinder=p["grinder"], basket=p["basket"])
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
            grinder_of=lambda s: store.gear(me.user_id, s.grinder_id),
            days_off=(datetime.now(z).date() - bag.roast_date).days if bag and bag.roast_date else None,
            day=lambda d: views.day(d, z),
            clock=lambda d: views.clock(d, z),
        )

    @app.get("/log", response_class=HTMLResponse, response_model=None)
    def log_shot(request: Request, me: Me) -> HTMLResponse | RedirectResponse:
        p = store.prefill(me.user_id)
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
        return page(request, "log.html", p=p, name=name, form=form)

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

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, me: Me) -> HTMLResponse:
        return page(
            request,
            "setup.html",
            me=me,
            auth_name=getattr(auth, "name", ""),
            grinders=store.equipment_of(me.user_id, "grinder"),
            baskets=store.equipment_of(me.user_id, "basket"),
            counts={
                e: sum(1 for s in store.shots_of(me.user_id) if e in (s.grinder_id, s.basket_id))
                for e in (r.item.equipment_id for r in store.equipment.values())
            },
        )

    @app.get("/bag/new", response_class=HTMLResponse)
    def new_bag(request: Request, me: Me) -> HTMLResponse:
        return page(request, "bag.html")

    # --- writes --------------------------------------------------------------------------------------

    def run(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except ValidationError as e:
            raise HTTPException(422, [err["msg"] for err in e.errors()]) from None
        except NotFound:
            raise HTTPException(404, "not found") from None

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
