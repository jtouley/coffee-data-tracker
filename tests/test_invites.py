"""Invites, joining, reset codes and the owner's controls, over a local users lake; then equipment and switching
grinders from the Log shot chip."""

from datetime import timedelta

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app.accounts import AccountError, Accounts, code_hash
from app.auth import Sessions
from app.main import create_app
from coffeedata.lake import Lake

W = {"X-Requested-With": "test"}
SETUP = "SETUP-CODE-1234"
PW = "correct horse battery"


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    users = Lake(str(tmp_path / "users"), tables=("users", "invites"))
    return Accounts(users, "pepper-" + "p" * 16, "s" * 40, SETUP)


@pytest.fixture
def app(lake, accounts):
    return create_app(lake, Sessions(accounts))


def client(app) -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture
def owner(app) -> TestClient:
    c = client(app)
    r = c.post("/api/signin", json={"setup_code": SETUP, "username": "jtouley", "password": PW}, headers=W)
    assert r.status_code == 200
    return c


def invite(owner: TestClient, **body) -> dict:
    r = owner.post("/api/invites", json=body, headers=W)
    assert r.status_code == 200, r.text
    return r.json()


def join(app, code: str, username: str = "maya", password: str = "kettle orbit marble") -> TestClient:
    c = client(app)
    r = c.post("/api/join", json={"code": code, "username": username, "password": password}, headers=W)
    assert r.status_code == 200, r.text
    return c


def test_invite_join_and_sign_in(app, owner, accounts):
    r = invite(owner)
    assert len(r["code"]) == 19 and r["code"].count("-") == 3
    # Only the hash is stored, anywhere.
    rows = [
        row for f in accounts.lake.root.glob("invites/*__*.parquet") for row in pq.read_table(f).to_pylist()
    ]
    assert rows and all(r["code"] not in str(row) for row in rows)
    assert rows[0]["code_sha256"] == code_hash(r["code"])
    maya = join(app, r["code"].lower().replace("-", " "))  # typed loosely still works
    assert maya.get("/").status_code == 200
    again = client(app)
    assert (
        again.post(
            "/api/signin", json={"username": "maya", "password": "kettle orbit marble"}, headers=W
        ).status_code
        == 200
    )
    # Once only.
    r2 = client(app).post(
        "/api/join", json={"code": r["code"], "username": "sam", "password": "kettle orbit marble"}, headers=W
    )
    assert r2.status_code == 400 and "isn’t valid" in r2.json()["detail"]
    assert accounts.pending() == []


def test_join_page_needs_no_sign_in_and_never_sees_the_code(app, owner):
    c = client(app)
    page = c.get("/join")
    assert page.status_code == 200 and "jtouley invited you" in page.text
    code = invite(owner)["code"]
    assert c.post("/api/invites/check", json={"code": code}, headers=W).json()["kind"] == "join"
    assert c.post("/api/invites/check", json={"code": "AAAA-BBBB-CCCC-DDDD"}, headers=W).status_code == 400


def test_usernames_are_unique_and_rules_apply(app, owner):
    join(app, invite(owner)["code"], "maya")
    code = invite(owner)["code"]
    for name, pw, msg in (("maya", "kettle orbit marble", "taken"), ("JTOUley", "kettle orbit marble", "taken"),
                          ("x", "kettle orbit marble", "lower-case"), ("sam", "short", "at least")):  # fmt: skip
        r = client(app).post("/api/join", json={"code": code, "username": name, "password": pw}, headers=W)
        assert r.status_code == 400 and msg in r.json()["detail"], (name, r.text)
    join(app, code, "sam")  # the failures didn't use the code up


def test_expired_and_revoked_and_renewed(app, owner, accounts):
    a = invite(owner)
    inv = accounts.invites[a["invite_id"]][0]
    accounts.invites[a["invite_id"]] = (inv.__class__(**{**inv.__dict__, "expires_at": inv.expires_at - timedelta(days=8)}),
                                        accounts.invites[a["invite_id"]][1])  # fmt: skip
    assert client(app).post("/api/invites/check", json={"code": a["code"]}, headers=W).status_code == 400
    b = invite(owner)
    assert owner.post(f"/api/invites/{b['invite_id']}/revoke", headers=W).status_code == 200
    assert client(app).post("/api/invites/check", json={"code": b["code"]}, headers=W).status_code == 400
    c = invite(owner)
    new = owner.post(f"/api/invites/{c['invite_id']}/renew", headers=W).json()
    assert new["code"] != c["code"]
    assert client(app).post("/api/invites/check", json={"code": c["code"]}, headers=W).status_code == 400
    join(app, new["code"])


def test_wrong_codes_count_against_the_address(accounts):
    for _ in range(20):
        with pytest.raises(AccountError, match="isn’t valid"):
            accounts.lookup("AAAA-BBBB-CCCC-DDDD", "9.9.9.9", now=5)
    with pytest.raises(AccountError, match="Too many"):
        accounts.lookup("AAAA-BBBB-CCCC-DDDD", "9.9.9.9", now=6)


def test_reset_code_sets_a_password_and_signs_out_everywhere(app, owner, accounts):
    maya = join(app, invite(owner)["code"])
    maya_id = accounts.by_name("maya").user_id
    r = invite(owner, kind="reset", for_user_id=maya_id)
    assert client(app).get("/reset").status_code == 200
    # A join code can't reset, and a reset code can't join.
    assert client(app).post("/api/join", json={"code": r["code"], "username": "x2y", "password": "kettle orbit marble"},
                            headers=W).status_code == 400  # fmt: skip
    phone = client(app)
    assert (
        phone.post(
            "/api/reset", json={"code": r["code"], "password": "a brand new phrase"}, headers=W
        ).status_code
        == 200
    )
    assert phone.get("/").status_code == 200
    assert maya.get("/").status_code == 303  # the old session is gone
    assert (
        client(app)
        .post("/api/signin", json={"username": "maya", "password": "a brand new phrase"}, headers=W)
        .status_code
        == 200
    )
    assert (
        client(app)
        .post("/api/reset", json={"code": r["code"], "password": "another new phrase"}, headers=W)
        .status_code
        == 400
    )


def test_a_new_reset_code_replaces_the_old_one(app, owner, accounts):
    join(app, invite(owner)["code"])
    uid = accounts.by_name("maya").user_id
    first = invite(owner, kind="reset", for_user_id=uid)
    invite(owner, kind="reset", for_user_id=uid)
    assert (
        client(app)
        .post("/api/reset", json={"code": first["code"], "password": "a brand new phrase"}, headers=W)
        .status_code
        == 400
    )


def test_owner_resets_with_the_setup_code(app, owner):
    c = client(app)
    assert (
        c.post("/api/reset", json={"code": SETUP, "password": "owner new phrase"}, headers=W).status_code
        == 200
    )
    assert owner.get("/").status_code == 303
    assert (
        client(app)
        .post("/api/signin", json={"username": "jtouley", "password": "owner new phrase"}, headers=W)
        .status_code
        == 200
    )


def test_only_the_owner_invites_resets_or_turns_accounts_off(app, owner, accounts):
    maya = join(app, invite(owner)["code"])
    assert maya.get("/invite").status_code == 404
    assert maya.post("/api/invites", json={}, headers=W).status_code == 400
    owner_id = accounts.by_name("jtouley").user_id
    assert (
        maya.post("/api/invites", json={"kind": "reset", "for_user_id": owner_id}, headers=W).status_code
        == 400
    )
    assert maya.post(f"/api/people/{owner_id}", json={"disabled": True}, headers=W).status_code == 400
    assert owner.get("/invite").status_code == 200 and "maya" in owner.get("/invite").text


def test_turning_an_account_off_signs_it_out(app, owner, accounts):
    maya = join(app, invite(owner)["code"])
    uid = accounts.by_name("maya").user_id
    assert owner.post(f"/api/people/{uid}", json={"disabled": True}, headers=W).json()["disabled"] is True
    assert maya.get("/").status_code == 303
    r = client(app).post(
        "/api/signin", json={"username": "maya", "password": "kettle orbit marble"}, headers=W
    )
    assert r.status_code == 400
    owner.post(f"/api/people/{uid}", json={"disabled": False}, headers=W)
    r = client(app).post(
        "/api/signin", json={"username": "maya", "password": "kettle orbit marble"}, headers=W
    )
    assert r.status_code == 200


def test_invites_survive_a_restart(app, owner, accounts, lake):
    code = invite(owner)["code"]
    again = Accounts(accounts.lake, "pepper-" + "p" * 16, "s" * 40, SETUP)
    join(create_app(lake, Sessions(again)), code)


def test_member_data_is_their_own(app, owner):
    maya = join(app, invite(owner)["code"])
    for c, gid in ((owner, "g-own"), (maya, "g-maya")):
        r = c.post("/api/equipment", json={"equipment_id": gid, "kind": "grinder", "name": gid, "scale_min": 0,
                                           "scale_max": 50, "scale_step": 1}, headers=W)  # fmt: skip
        assert r.status_code == 200
    assert "g-maya" not in owner.get("/setup").text and "g-own" not in maya.get("/setup").text
    assert maya.get("/setup/gear/g-own", headers={"Accept": "text/html"}).status_code == 404


# --- equipment and the Log shot chip -------------------------------------------------------------------


def gear(c, eid, kind="grinder", **kw) -> None:
    body = {"equipment_id": eid, "kind": kind, "name": eid} | kw
    if kind == "grinder":
        body = {"scale_min": 0, "scale_max": 90, "scale_step": 0.5, "finer_is_lower": True} | body
    if kind == "basket":
        body = {"basket_dose_g": 18} | body
    r = c.post("/api/equipment", json=body, headers=W)
    assert r.status_code == 200, r.text


def test_one_default_per_kind_and_kind_is_fixed(owner, app):
    gear(owner, "df64", is_default=True)
    gear(owner, "niche", scale_max=50, scale_step=1, is_default=True)
    store = app.state.store
    defaults = [e.equipment_id for e in store.equipment_of("owner", "grinder") if e.is_default]
    assert defaults == ["niche"]
    r = owner.post(
        "/api/equipment",
        json={"equipment_id": "niche", "kind": "basket", "name": "x", "basket_dose_g": 18},
        headers=W,
    )
    assert r.status_code == 422 and "kind" in r.json()["detail"]
    gear(owner, "niche", scale_max=50, scale_step=1, retired=True)
    assert [e.equipment_id for e in store.equipment_of("owner", "grinder")] == ["df64"]


def test_setup_lists_gear_and_the_gear_pages(owner):
    gear(owner, "df64")
    gear(owner, "bianca", kind="machine", model="Dual boiler")
    gear(owner, "pullman", kind="basket")
    page = owner.get("/setup").text
    for s in (
        "df64",
        "bianca",
        "pullman",
        "Dual boiler",
        "/setup/gear/df64",
        "Auto follows",
        "Invite someone",
    ):
        assert s in page
    assert "Dial from" in owner.get("/setup/gear/df64").text
    assert "Nominal dose" in owner.get("/setup/gear/new?kind=basket").text
    assert owner.get("/setup/gear/nope", headers={"Accept": "text/html"}).status_code == 404


def test_switching_grinders_prefills_that_grinders_last_grind(owner, app):
    gear(owner, "df64", is_default=True)
    gear(owner, "niche", scale_max=50, scale_step=1)
    gear(owner, "pullman", kind="basket")
    gear(owner, "bianca", kind="machine")
    owner.post("/api/bags", json={"bag_id": "b1", "coffee_freetext": "Geometry"}, headers=W)
    base = {"bag_id": "b1", "basket_id": "pullman", "dose_g": 18, "yield_g": 36, "time_s": 28, "rating": 4}
    for i, (g, s) in enumerate((("niche", 22), ("df64", 11), ("df64", 11.5))):
        r = owner.post("/api/shots", json=base | {"shot_id": f"s{i}", "brewed_at": f"2026-10-2{i}T07:00:00Z",
                                                   "grinder_id": g, "grinder_setting": s}, headers=W)  # fmt: skip
        assert r.status_code == 200, r.text
    store = app.state.store
    assert store.prefill("owner")["grinder_setting"] == 11.5
    p = store.prefill("owner", "niche")
    assert p["grinder"].equipment_id == "niche" and p["grinder_setting"] == 22
    assert p["machine"].equipment_id == "bianca"
    html = owner.get("/log?grinder=niche").text
    assert (
        '"grinder_id": "niche"' in html
        and '"grinder_setting": 22' in html
        and '"machine_id": "bianca"' in html
    )
    pick = owner.get("/log/gear?grinder=niche&basket=pullman").text
    assert 'value="niche" checked' in pick
    # Someone else's grinder id is ignored.
    assert store.prefill("owner", "not-mine")["grinder"].equipment_id == "df64"


def test_theme_cookie_sets_the_page_theme(owner):
    owner.cookies.set("theme", "dark")
    assert '<html lang="en" data-theme="dark">' in owner.get("/setup").text
    owner.cookies.set("theme", "auto")
    assert '<html lang="en">' in owner.get("/setup").text
