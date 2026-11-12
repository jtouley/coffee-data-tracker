"""Sign-up and invites on the server (spec §4, §7, §8, §9, §11): field-tagged errors, the idempotent join in one
commit, the required terms box and its record, re-acceptance when the terms change, named invites and their list,
owner reset links, the in-app browser banner, and the cold-start log line."""

import json
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts, _now
from app.auth import Sessions
from app.main import create_app
from coffeedata import erase
from coffeedata.lake import PHOTO_TABLES, USER_TABLES, Lake
from coffeedata.site import LEGAL_UPDATED

W = {"X-Requested-With": "test", "X-Expected-User": "owner"}
SETUP, PW, MAYA_PW = "SETUP-CODE-1234", "correct horse battery", "kettle orbit marble"
INSTAGRAM = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 390.0.0.27.105 (iPhone15,2; iOS 18_5; en_US; en; scale=3.00; 1179x2556; 739125451)"
FACEBOOK = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/480.0.0.40.104;FBBV/700000000;FBDV/iPhone15,2;FBMD/iPhone;FBSN/iOS;FBSV/18.5;FBSS/3;FBID/phone;FBLC/en_US;FBOP/5]"
FB_ANDROID = "Mozilla/5.0 (Linux; Android 15; Pixel 9 Build/AP4A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0.0.0 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/480.0.0.40.104;]"
SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
CHROME = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"


def new_accounts(root, legal: str | None = None) -> Accounts:
    return Accounts(
        Lake(str(root), tables=USER_TABLES), "pepper-" + "p" * 16, "s" * 40, SETUP, legal_version=legal
    )


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    return new_accounts(tmp_path / "users")


@pytest.fixture
def app(lake, accounts):
    return create_app(lake, Sessions(accounts))


def client(app, **headers) -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False, headers=headers)


@pytest.fixture
def owner(app) -> TestClient:
    c = client(app)
    body = {"setup_code": SETUP, "username": "jtouley", "password": PW, "agree": True}
    assert c.post("/api/signin", json=body, headers=W).status_code == 200
    return c


def invite(owner, **body) -> dict:
    r = owner.post("/api/invites", json=body, headers=W)
    assert r.status_code == 200, r.text
    return r.json()


def join_body(code, username="maya", password=MAYA_PW, agree=True) -> dict:
    return {"code": code, "username": username, "password": password, "agree": agree}


# --- errors that say which field they belong to ---------------------------------------------------------------


def test_errors_carry_their_field_and_only_those_fields(app, owner):
    code = invite(owner)["code"]
    r = client(app).post("/api/join", json=join_body(code, "maya", "short"), headers=W)
    assert r.status_code == 400
    assert r.json()["detail"] == [
        {"field": "password", "rule": "p.length", "message": "Use at least 10 characters."}
    ]
    r = owner.post("/api/password", json={"current": "nope", "new": "x"}, headers=W)
    assert r.json()["detail"] == [
        {"field": "current", "rule": "current", "message": "Your current password isn’t right."}
    ]
    r = owner.post("/api/password", json={"current": PW, "new": "jtouley-123456"}, headers=W)
    assert r.json()["detail"] == [
        {"field": "new", "rule": "p.username", "message": "Leave your username out of the password."}
    ]
    # Errors that belong to no field stay plain words.
    r = client(app).post("/api/signin", json={"username": "jtouley", "password": "wrong password"}, headers=W)
    assert isinstance(r.json()["detail"], str)


def test_capitals_and_dots_in_a_username(app, owner, accounts):
    code = invite(owner)["code"]
    assert client(app).post("/api/join", json=join_body(code, "  Maya.Lee "), headers=W).status_code == 200
    assert accounts.by_name("maya.lee") is not None
    assert accounts.sign_in("MAYA.LEE", MAYA_PW, "1.1.1.1").username == "maya.lee"


# --- the idempotent join ----------------------------------------------------------------------------------------


def test_a_repeated_join_signs_in_and_anything_else_says_already_used(app, owner, accounts):
    code = invite(owner)["code"]
    first = client(app).post("/api/join", json=join_body(code), headers=W)
    assert first.status_code == 200
    # The reply was lost; the phone sends the same body again.
    again = client(app)
    r = again.post("/api/join", json=join_body(code), headers=W)
    assert r.status_code == 200 and "session=" in r.headers["set-cookie"]
    assert again.get("/").status_code == 200
    assert len([a for a in accounts.people() if a.username == "maya"]) == 1
    for body in (join_body(code, password="a different phrase"), join_body(code, username="sam")):
        r = client(app).post("/api/join", json=body, headers=W)
        assert r.status_code == 409
        assert r.json()["detail"] == [{
            "field": "code", "rule": "c.used",
            "message": "This invite has already been used. If you made an account with it, sign in. If not, ask "
                       "whoever sent it for a new link.",
        }]  # fmt: skip
    r = client(app).post("/api/invites/check", json={"code": code}, headers=W)
    assert r.status_code == 409 and r.json()["detail"][0]["rule"] == "c.used"


def test_a_retry_counts_against_the_sign_in_limits(app, owner, accounts):
    code = invite(owner)["code"]
    client(app).post("/api/join", json=join_body(code), headers=W)
    for _ in range(5):
        assert (
            client(app)
            .post("/api/join", json=join_body(code, password="wrong phrase!"), headers=W)
            .status_code
            == 409
        )
    # Past the limit even the right password gets the same reply as everyone else.
    r = client(app).post("/api/join", json=join_body(code), headers=W)
    assert r.status_code == 409 and r.json()["detail"][0]["rule"] == "c.used"
    assert accounts.limiter.blocked("u:maya|ip:testclient", 5, time.monotonic())


def test_a_retry_signs_in_only_soon_after_the_join(app, owner, accounts):
    code = invite(owner)["code"]
    client(app).post("/api/join", json=join_body(code), headers=W)
    inv = next(i for i, _ in accounts.invites.values() if i.kind == "join" and i.used_by)
    accounts.invites[inv.invite_id] = (replace(inv, used_at=inv.used_at - timedelta(minutes=16)), _now())
    r = client(app).post("/api/join", json=join_body(code), headers=W)
    assert r.status_code == 409 and r.json()["detail"][0]["rule"] == "c.used"


def test_a_used_code_does_the_same_work_whatever_the_username(app, owner, accounts, monkeypatch):
    code = invite(owner)["code"]
    client(app).post("/api/join", json=join_body(code), headers=W)
    checks = []
    real = accounts._verify
    monkeypatch.setattr(accounts, "_verify", lambda h, p: checks.append(p) or real(h, p))
    replies = []
    for name in ("maya", "sam", "nobody1"):
        r = client(app).post("/api/join", json=join_body(code, name, "wrong phrase!"), headers=W)
        replies.append((r.status_code, r.json()))
    assert len({json.dumps(x) for x in replies}) == 1  # one reply, word for word
    assert checks == ["wrong phrase!"] * 3  # one password check each


def test_a_bad_code_says_nothing_about_the_username(app, owner, accounts):
    a = invite(owner)
    client(app).post("/api/join", json=join_body(a["code"]), headers=W)  # maya exists now
    for name in ("maya", "nobody1"):
        r = client(app).post("/api/join", json=join_body("AAAA-BBBB-CCCC-DDDD", name, "x"), headers=W)
        assert r.json()["detail"] == [{"field": "code", "rule": "c.invalid",
                                       "message": "That code isn’t valid. It may have been used, replaced or expired."}]  # fmt: skip
    reset = invite(owner, kind="reset", for_user_id=accounts.by_name("maya").user_id)
    for name in ("maya", "nobody1"):
        r = client(app).post("/api/join", json=join_body(reset["code"], name), headers=W)
        assert [e["rule"] for e in r.json()["detail"]] == ["c.kind"]


def test_one_publish_per_join_with_the_account_the_invite_and_the_acceptance(accounts, monkeypatch):
    owner = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    _, code = accounts.invite(owner.user_id)
    calls = []
    staged = {}

    def publish(message, *a, **k):
        calls.append(message)
        staged[message] = sorted(p.parent.name for p in accounts.lake.root.rglob("*__*.parquet"))
        return 0

    monkeypatch.setattr(accounts.lake, "publish", publish)
    acct = accounts.join(code, "maya", MAYA_PW, "1.1.1.1", agree=True)
    assert calls == [f"app: users {acct.user_id}"]
    assert {"consents", "invites", "users"} <= set(staged[calls[0]])


def test_a_failed_publish_leaves_the_code_open_and_no_account(accounts, monkeypatch):
    owner = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    inv, code = accounts.invite(owner.user_id)

    def fail(*a, **k):
        raise OSError("hub down")

    monkeypatch.setattr(accounts.lake, "publish", fail)
    with pytest.raises(OSError):
        accounts.join(code, "maya", MAYA_PW, "1.1.1.1", agree=True)
    assert accounts.by_name("maya") is None and accounts.invites[inv.invite_id][0].open(_now())


# --- the terms box and its record ------------------------------------------------------------------------------


def test_join_and_owner_setup_need_the_terms_box(lake, tmp_path):
    accounts = new_accounts(tmp_path / "u2")
    app = create_app(lake, Sessions(accounts))
    c = client(app)
    r = c.post("/api/signin", json={"setup_code": SETUP, "username": "jtouley", "password": PW}, headers=W)
    assert r.status_code == 400 and r.json()["detail"][-1]["rule"] == "terms"
    assert accounts.needs_owner
    assert c.post("/api/signin", json={"setup_code": SETUP, "username": "jtouley", "password": PW, "agree": True},
                  headers=W).status_code == 200  # fmt: skip
    code = invite(c)["code"]
    for agree in (False, "true", None):
        r = client(app).post("/api/join", json=join_body(code, agree=agree), headers=W)
        assert r.status_code == 400 and r.json()["detail"] == [{
            "field": "agree", "rule": "terms",
            "message": "Tick the box to agree to the Terms and Privacy Policy before creating your account.",
        }]  # fmt: skip
    assert accounts.by_name("maya") is None
    assert (
        client(app).post("/api/invites/check", json={"code": code}, headers=W).status_code == 200
    )  # still open


def test_acceptance_is_recorded_with_the_account(app, owner, accounts):
    code = invite(owner)["code"]
    client(app).post("/api/join", json=join_body(code), headers=W)
    rows = [r for f in accounts.lake.root.glob("consents/*__*.parquet") for r in pq.read_table(f).to_pylist()]
    maya = accounts.by_name("maya")
    mine = [r for r in rows if r["user_id"] == maya.user_id]
    assert len(mine) == 1
    assert mine[0]["terms_version"] == mine[0]["privacy_version"] == LEGAL_UPDATED
    assert mine[0]["source"] == "signup" and abs(mine[0]["terms_at"] - datetime.now(UTC)) < timedelta(
        minutes=1
    )
    assert {r["user_id"] for r in rows} == {maya.user_id, accounts.by_name("jtouley").user_id}


def test_the_join_page_has_the_box_unticked_and_links_that_open_on_click(app):
    page = client(app).get("/join").text
    assert '<input type="checkbox" id="agree" name="agree" required aria-describedby="agree-error">' in page
    assert page.count('target="_blank" rel="noopener"') == 2
    assert 'passwordrules="minlength: 10; maxlength: 256; allowed: unicode;"' in page
    assert 'autocomplete="new-password"' in page and 'autocomplete="username"' in page
    assert '<script type="application/json" id="rules">' in page
    assert 'aria-describedby="password-error password-h password-rules"' in page
    assert "By joining you agree" not in page


# --- re-acceptance --------------------------------------------------------------------------------------------


def test_new_terms_are_accepted_on_the_next_sign_in(lake, tmp_path):
    root = tmp_path / "users"
    first = new_accounts(root, "November 2, 2026")
    owner = first.create_owner(SETUP, "jtouley", PW, agree=True)
    _, code = first.invite(owner.user_id)
    first.join(code, "maya", MAYA_PW, "1.1.1.1", agree=True)
    accounts = new_accounts(root, "December 1, 2026")  # LEGAL_UPDATED bumped, the app restarted
    app = create_app(lake, Sessions(accounts))
    c = client(app)
    assert c.post("/api/signin", json={"username": "maya", "password": MAYA_PW}, headers=W).status_code == 200
    assert c.get("/").headers["location"] == "/agree"
    assert c.get("/setup").headers["location"] == "/agree"
    page = c.get("/agree")
    assert page.status_code == 200 and "We’ve updated our Terms and Privacy Policy" in page.text
    assert "December 1, 2026" in page.text and 'data-signout="/api/signout"' in page.text
    r = c.post(
        "/api/bags", json={"bag_id": "b1"}, headers={**W, "X-Expected-User": accounts.by_name("maya").user_id}
    )
    assert r.status_code == 403  # the phone's outbox keeps it and sends it after Accept
    assert c.post("/api/agree", json={}, headers=W).json()["detail"][0]["rule"] == "terms"
    assert c.post("/api/agree", json={"agree": True}, headers=W).status_code == 200
    assert c.get("/").status_code == 200 and c.get("/agree").headers["location"] == "/"
    latest = new_accounts(root, "December 1, 2026").consents[accounts.by_name("maya").user_id][0]
    assert (latest.source, latest.terms_version) == ("reaccept", "December 1, 2026")
    # The owner too, and Sign out works from there.
    o = client(app)
    o.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W)
    assert o.get("/").headers["location"] == "/agree"
    assert o.post("/api/signout", headers=W).status_code == 200
    assert o.get("/").headers["location"] == "/signin"


def test_accounts_from_before_records_accepted_the_terms_then_in_force(tmp_path):
    accounts = new_accounts(tmp_path / "users", "November 2, 2026")
    owner = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    accounts.consents.clear()
    assert not accounts.needs_agree(owner.user_id)
    accounts.legal = "December 1, 2026"
    assert accounts.needs_agree(owner.user_id)


def test_delete_user_removes_the_acceptance_record_and_named_invites(tmp_path, monkeypatch):
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    accounts = new_accounts(tmp_path / "users")
    owner = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    inv, code = accounts.invite(owner.user_id, for_label="Maya from the café")
    maya = accounts.join(code, "maya", MAYA_PW, "1.1.1.1", agree=True)
    accounts.set_disabled(owner.user_id, maya.user_id, True)
    main, photos = Lake(str(tmp_path / "lake")), Lake(str(tmp_path / "photos"), tables=PHOTO_TABLES)
    p = erase.plan("maya", main, photos, accounts.lake)
    assert any(f.startswith("consents/") for f in p.files[accounts.lake.url])
    erase.apply(p, main, photos, accounts.lake)
    after = new_accounts(tmp_path / "users")
    assert maya.user_id not in after.consents and inv.invite_id not in after.invites
    assert owner.user_id in after.consents


# --- named invites ---------------------------------------------------------------------------------------------


def test_a_note_up_to_60_characters(app, owner):
    assert invite(owner, for_label="x" * 60)["for_label"] == "x" * 60
    r = owner.post("/api/invites", json={"for_label": "x" * 61}, headers=W)
    assert r.status_code == 400
    assert r.json()["detail"] == [
        {"field": "for_label", "rule": "i.label", "message": "Keep the note to 60 characters."}
    ]


def test_the_note_is_for_the_owner_only(app, owner, accounts):
    a = invite(owner, for_label="Maya from the café")
    assert "café" not in client(app).post("/api/invites/check", json={"code": a["code"]}, headers=W).text
    assert "café" not in client(app).get("/join").text
    maya = client(app)
    maya.post("/api/join", json=join_body(a["code"]), headers=W)
    b = invite(owner, for_label="Sam")
    assert (
        maya.post(f"/api/invites/{b['invite_id']}/label", json={"for_label": "mine"}, headers=W).status_code
        == 400
    )
    assert maya.post("/api/invites", json={"for_label": "x"}, headers=W).status_code == 400
    assert maya.get("/invite").status_code == 404
    assert accounts.invites[b["invite_id"]][0].for_label == "Sam"


def test_notes_survive_a_restart_and_a_renew_and_can_be_edited_and_cleared(app, owner, accounts, tmp_path):
    a = invite(owner, for_label="  Sam \n at the  market ")
    assert a["for_label"] == "Sam at the market"
    renewed = owner.post(f"/api/invites/{a['invite_id']}/renew", headers=W).json()
    assert renewed["for_label"] == "Sam at the market"
    assert new_accounts(tmp_path / "users").invites[renewed["invite_id"]][0].for_label == "Sam at the market"
    r = owner.post(
        f"/api/invites/{renewed['invite_id']}/label", json={"for_label": "Sam, café on 5th"}, headers=W
    )
    assert r.json() == {"invite_id": renewed["invite_id"], "for_label": "Sam, café on 5th"}
    assert new_accounts(tmp_path / "users").invites[renewed["invite_id"]][0].for_label == "Sam, café on 5th"
    owner.post(f"/api/invites/{renewed['invite_id']}/label", json={"for_label": ""}, headers=W)
    assert new_accounts(tmp_path / "users").invites[renewed["invite_id"]][0].for_label is None


def test_the_list_shows_every_status_with_its_dates(app, owner, accounts):
    used = invite(owner, for_label="Maya")
    client(app).post("/api/join", json=join_body(used["code"]), headers=W)
    revoked = invite(owner, for_label="Ana")
    owner.post(f"/api/invites/{revoked['invite_id']}/revoke", headers=W)
    expired = invite(owner, for_label="Lee")
    inv = accounts.invites[expired["invite_id"]][0]
    accounts._write_invite(replace(inv, expires_at=_now() - timedelta(days=1)))
    invite(owner, for_label="Sam")
    rows = {r.invite.for_label: r for r in accounts.invite_log()}
    assert {k: r.status for k, r in rows.items()} == {
        "Maya": "used",
        "Ana": "revoked",
        "Lee": "expired",
        "Sam": "open",
    }
    assert rows["Maya"].used_by == "maya"
    page = owner.get("/invite").text
    for line in ("Used by maya on", "Revoked ", "Expired ", "Made ", "· expires "):
        assert line in page
    assert page.count("Edit note") == 4 and page.count("data-renew=") == 1


def test_old_links_fold_away(app, owner, accounts):
    a = invite(owner, for_label="Long ago")
    inv = accounts.invites[a["invite_id"]][0]
    accounts._write_invite(
        replace(inv, expires_at=_now() - timedelta(days=100), created_at=_now() - timedelta(days=107))
    )
    page = owner.get("/invite").text
    assert "Older links (1)" in page


def test_invite_files_from_before_notes_still_load(tmp_path):
    root = tmp_path / "users"
    accounts = new_accounts(root)
    accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    old = pa.schema([("invite_id", pa.string()), ("logged_at", pa.timestamp("s", tz="UTC")),
                     ("code_sha256", pa.string()), ("kind", pa.string()), ("for_user_id", pa.string()),
                     ("created_by", pa.string()), ("expires_at", pa.timestamp("s", tz="UTC")),
                     ("used_by", pa.string()), ("used_at", pa.timestamp("s", tz="UTC")), ("revoked", pa.bool_())])  # fmt: skip
    expires = datetime(2026, 11, 10, tzinfo=UTC)
    row = {"invite_id": "old1", "logged_at": datetime(2026, 11, 3, tzinfo=UTC), "code_sha256": "x" * 64,
           "kind": "join", "for_user_id": None, "created_by": "owner", "expires_at": expires, "used_by": None,
           "used_at": None, "revoked": False}  # fmt: skip
    pq.write_table(
        pa.Table.from_pylist([row], schema=old), root / "invites" / "old1__20261103T000000.parquet"
    )
    inv = new_accounts(root).invites["old1"][0]
    assert inv.for_label is None and inv.created_at == expires - timedelta(days=7)


# --- owner reset links -----------------------------------------------------------------------------------------


def test_a_reset_link_can_be_renewed(app, owner, accounts):
    a = invite(owner)
    client(app).post("/api/join", json=join_body(a["code"]), headers=W)
    first = invite(owner, kind="reset", for_user_id=accounts.by_name("maya").user_id)
    r = owner.post(f"/api/invites/{first['invite_id']}/renew", headers=W)
    assert r.status_code == 200 and r.json()["kind"] == "reset"
    assert accounts.invites[first["invite_id"]][0].revoked
    assert accounts.invites[r.json()["invite_id"]][0].open(_now())
    staged = sorted(p.name for p in accounts.lake.root.glob(f"invites/{first['invite_id']}__*.parquet"))
    assert len(staged) == 2  # made, then revoked once
    assert client(app).post("/api/reset", json={"code": r.json()["code"], "password": "a brand new phrase"},
                            headers=W).status_code == 200  # fmt: skip


def test_editing_a_note_keeps_the_revoked_date(app, owner, accounts):
    a = invite(owner, for_label="Ana")
    owner.post(f"/api/invites/{a['invite_id']}/revoke", headers=W)
    inv = accounts.invites[a["invite_id"]][0]
    long_ago = _now() - timedelta(days=120)
    accounts._write_invite(replace(inv, revoked_at=long_ago))
    owner.post(f"/api/invites/{a['invite_id']}/label", json={"for_label": "Ana, edited"}, headers=W)
    row = next(
        r for r in new_accounts(accounts.lake.root).invite_log() if r.invite.invite_id == a["invite_id"]
    )
    assert row.when == long_ago and row.old


def test_edit_note_opens_with_the_note(app, owner):
    invite(owner, for_label="Sam from the café")
    page = owner.get("/invite").text
    assert 'data-note value="Sam from the café"' in page


def test_reset_links(app, owner, accounts):
    a = invite(owner)
    client(app).post("/api/join", json=join_body(a["code"]), headers=W)
    page = owner.get("/invite").text
    assert "Make a reset link for maya" in page and "Tap again: maya’s old reset link stops working" in page
    maya_id = accounts.by_name("maya").user_id
    first = invite(owner, kind="reset", for_user_id=maya_id)
    second = invite(owner, kind="reset", for_user_id=maya_id, for_label="ignored")
    assert second["for_label"] is None  # reset links already name their member
    assert accounts.invites[first["invite_id"]][0].revoked  # the earlier one stops working
    assert "Ask the person who invited you for a reset link" in client(app).get("/reset").text
    signin = client(app).get("/signin").text
    # "Forgot your password?" goes to /forgot (spec §4), which keeps the way back for accounts without an email.
    assert '<a class="btn text" href="/forgot">Forgot your password?</a>' in signin
    assert (
        "No email on your account? Ask the person who invited you" not in client(app).get("/forgot").text
    )  # email off
    assert "Ask the person who invited you for a reset link" in client(app).get("/forgot").text


# --- in-app browsers --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ua", "name"),
    [
        (INSTAGRAM, "Instagram"),
        (FACEBOOK, "Facebook"),
        (FB_ANDROID, "Facebook"),
        (SAFARI, None),
        (CHROME, None),
    ],
)
def test_the_in_app_browser_banner(app, ua, name):
    for path in ("/join", "/reset"):
        page = client(app, **{"User-Agent": ua}).get(path).text
        if name:
            assert f"<strong>Opening from {name}?</strong>" in page and "Open in external browser" in page
            assert "data-copy-link" in page
        else:
            assert "data-in-app" not in page


# --- cold start ------------------------------------------------------------------------------------------------


def test_startup_time_is_logged(lake, accounts, caplog):
    with caplog.at_level(logging.INFO, logger="app.events"):
        create_app(lake, Sessions(accounts))
    ready = [r for r in caplog.records if r.getMessage() == "ready"]
    assert ready and ready[0].fields["seconds"] >= 0  # type: ignore[attr-defined]
