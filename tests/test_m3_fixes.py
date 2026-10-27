"""Regressions from the review of milestone 3 (#16)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.accounts import AccountError
from app.main import create_app

from .test_app import HeaderAuth
from .test_invites import SETUP, W, accounts, app, client, invite, join, owner  # noqa: F401 - fixtures


def test_a_blocked_address_cant_try_the_setup_code(accounts):  # noqa: F811
    accounts.create_owner(SETUP, "jtouley", "correct horse battery", ip="1.1.1.1", now=1)
    for _ in range(20):
        with pytest.raises(AccountError, match="isn’t valid"):
            accounts.lookup("AAAA-BBBB-CCCC-DDDD", "9.9.9.9", now=5)
    # The right code, from an address that's used up its tries, is refused before it's compared.
    with pytest.raises(AccountError, match="Too many"):
        accounts.reset(SETUP, "owner new phrase", "9.9.9.9", now=6)
    # A wrong code counts against the address.
    with pytest.raises(AccountError, match="isn’t valid"):
        accounts.reset("NOPE-NOPE-NOPE-NOPE", "owner new phrase", "8.8.8.8", now=6)
    assert len(accounts.limiter.fails["ip:8.8.8.8"]) == 1


@pytest.fixture
def c(lake):
    return TestClient(create_app(lake, HeaderAuth()))


def gear(c: TestClient, **body) -> None:
    r = c.post("/api/equipment", json=body, headers=W)
    assert r.status_code == 200, r.text


def test_editing_gear_keeps_what_the_form_leaves_out(c):
    g = {
        "equipment_id": "g1",
        "kind": "grinder",
        "name": "DF64",
        "scale_min": 0,
        "scale_max": 90,
        "scale_step": 0.5,
    }
    gear(c, **g, notes="SSP burrs", model="DF64 V2")
    # The grinder form has no notes and no model field.
    gear(c, **g | {"name": "DF64 at home"})
    item = c.app.state.store.gear("owner", "g1")
    assert (item.name, item.notes, item.model) == ("DF64 at home", "SSP burrs", "DF64 V2")
    # A field the form shows and the person clears is cleared.
    m = {"equipment_id": "m1", "kind": "machine", "name": "Bianca", "model": "Dual boiler"}
    gear(c, **m)
    gear(c, **m | {"model": None})
    assert c.app.state.store.gear("owner", "m1").model is None


def test_saving_retired_gear_doesnt_bring_it_back(c):
    b = {"equipment_id": "b1", "kind": "basket", "name": "Pullman", "basket_dose_g": 18}
    gear(c, **b)
    gear(c, **b, retired=True)
    gear(c, **b | {"name": "Pullman 18"})  # an old link to the form, saved again
    item = c.app.state.store.gear("owner", "b1")
    assert item.retired and item.name == "Pullman 18"
    assert "b1" not in {e.equipment_id for e in c.app.state.store.equipment_of("owner", "basket")}


def test_turning_an_account_off_voids_its_reset_codes(app, owner, accounts):  # noqa: F811
    join(app, invite(owner)["code"])
    uid = accounts.by_name("maya").user_id
    code = invite(owner, kind="reset", for_user_id=uid)["code"]
    assert owner.post(f"/api/people/{uid}", json={"disabled": True}, headers=W).status_code == 200
    r = client(app).post("/api/reset", json={"code": code, "password": "a brand new phrase"}, headers=W)
    assert r.status_code == 400
    # A code made after turning it off is refused with a reason.
    code = invite(owner, kind="reset", for_user_id=uid)["code"]
    r = client(app).post("/api/reset", json={"code": code, "password": "a brand new phrase"}, headers=W)
    assert r.status_code == 400 and "turned off" in r.json()["detail"]


def test_renew_says_nothing_about_invite_ids_and_skips_closed_ones(app, owner, accounts):  # noqa: F811
    maya = join(app, invite(owner)["code"])
    real = invite(owner)["invite_id"]
    # A member gets the same answer for a real id and a made-up one.
    a = maya.post(f"/api/invites/{real}/renew", headers=W)
    b = maya.post("/api/invites/nope/renew", headers=W)
    assert a.status_code == b.status_code == 400 and a.json() == b.json()
    # The owner can't renew a link that's been used or revoked.
    used = accounts.by_name("maya").invite_id
    assert owner.post(f"/api/invites/{used}/renew", headers=W).status_code == 400
    owner.post(f"/api/invites/{real}/revoke", headers=W)
    r = owner.post(f"/api/invites/{real}/renew", headers=W)
    assert r.status_code == 400 and "can’t be renewed" in r.json()["detail"]


def test_join_page_shows_expiry_in_the_viewers_time_zone(app, owner, accounts):  # noqa: F811
    a = invite(owner)
    inv, at = accounts.invites[a["invite_id"]]
    late = datetime(2030, 1, 1, 12, tzinfo=UTC)  # already January 2 in Kiritimati (UTC+14)
    accounts.invites[a["invite_id"]] = (inv.__class__(**{**inv.__dict__, "expires_at": late}), at)
    guest = client(app)
    guest.cookies.set("tz", "Pacific/Kiritimati")
    r = guest.post("/api/invites/check", json={"code": a["code"]}, headers=W)
    assert r.json()["expires"] == "January 2"
