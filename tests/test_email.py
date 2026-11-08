"""Email recovery, verification, the sealed `contacts` record and the marketing choice (docs/app/signup-ux-spec.md
§4, §5, §7.1, §12 "Email recovery" and "Consent"). Every message goes to a FakeMailer: nothing here calls out."""

import base64
import re
import statistics
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import mail
from app.accounts import Accounts, _now
from app.auth import Sessions
from app.contacts import EmailCrypto, EmailKeyError
from app.main import create_app
from coffeedata import erase
from coffeedata.lake import PHOTO_TABLES, USER_TABLES, Lake
from coffeedata.site import APP, LEGAL_UPDATED

W = {"X-Requested-With": "test", "X-Expected-User": "owner"}
SETUP, PW, MAYA_PW, NEW_PW = (
    "SETUP-CODE-1234",
    "correct horse battery",
    "kettle orbit marble",
    "lantern quarry velvet",
)
KEY = base64.b64encode(b"k" * 32).decode()
ROOT = Path(__file__).parents[1]
SAME = {"ok": True}


def new_accounts(root, key: str | None = KEY, legal: str | None = None) -> Accounts:
    return Accounts(
        Lake(str(root), tables=USER_TABLES),
        "pepper-" + "p" * 16,
        "s" * 40,
        SETUP,
        legal_version=legal,
        email_key=key,
    )


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    return new_accounts(tmp_path / "users")


@pytest.fixture
def outbox() -> mail.FakeMailer:
    return mail.FakeMailer()


@pytest.fixture
def app(lake, accounts, outbox):
    return create_app(lake, Sessions(accounts), mailer=outbox)


def client(app, **headers) -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False, headers=headers)


@pytest.fixture
def owner(app) -> TestClient:
    c = client(app)
    body = {"setup_code": SETUP, "username": "jtouley", "password": PW, "agree": True}
    assert c.post("/api/signin", json=body, headers=W).status_code == 200
    return c


def join(app, owner, username="maya", email="", marketing=True) -> TestClient:
    code = owner.post("/api/invites", json={}, headers=W).json()["code"]
    c = client(app)
    body = {"code": code, "username": username, "password": MAYA_PW, "agree": True, "marketing": marketing}
    r = c.post("/api/join", json=body | ({"email": email} if email else {}), headers=W)
    assert r.status_code == 200, r.text
    app.state.drain_mail()
    return c


def link_in(message: mail.Sent, path: str) -> str:
    m = re.search(rf"{re.escape(APP)}/{path}#([A-Za-z0-9_\-]+)", message.text)
    assert m, message.text
    return m.group(1)


def verified_member(app, owner, outbox, username="maya", email="maya@example.com") -> TestClient:
    c = join(app, owner, username, email)
    (sent,) = outbox.to(email)
    assert client(app).post("/api/verify", json={"token": link_in(sent, "verify")}, headers=W).json() == SAME
    outbox.sent.clear()
    return c


def forgot(app, who: str):
    r = client(app).post("/api/forgot", json={"account": who}, headers=W)
    app.state.drain_mail()
    return r


# --- asking for a reset link: the same answer, whatever happens ------------------------------------------------


NO_EMAIL = "This account has no verified email, so we can’t send a link. Ask the person who invited you for a reset link."


def test_forgot_takes_a_username_or_an_email_and_answers_alike_unless_there_is_no_email(
    app, owner, outbox, accounts
):
    verified_member(app, owner, outbox)
    join(app, owner, "sam", "sam@example.com")  # an email, not verified
    join(app, owner, "ana")  # no email at all
    outbox.sent.clear()
    # An account with a verified email and no account at all: one reply, by username or by email.
    replies = [forgot(app, w) for w in ("maya@example.com", "nobody@example.com", "nobody", "Maya")]
    assert {(r.status_code, r.text) for r in replies} == {(200, '{"ok":true}')}
    assert [m.to for m in outbox.sent] == ["maya@example.com", "maya@example.com"]
    sent = outbox.sent[0]
    assert (
        sent.subject == "Reset your registry.coffee password"
        and "maya" in sent.text
        and "30 minutes" in sent.text
    )
    # The owner's choice: an account with no verified email is told so, on the field.
    for who in ("sam", "ana", " Ana "):
        r = forgot(app, who)
        assert r.status_code == 400
        assert r.json()["detail"] == [{"field": "account", "rule": "f.no_email", "message": NO_EMAIL}]
    # An unverified address doesn't find its account (only a verified one does): the alike reply.
    assert forgot(app, "sam@example.com").json() == SAME
    assert len(outbox.sent) == 2
    # Capitals and spaces in what's typed don't matter.
    assert forgot(app, "  MAYA@Example.com ").json() == SAME and len(outbox.sent) == 3


def test_forgot_takes_the_same_time_whether_or_not_there_is_an_account(
    app, owner, outbox, accounts, monkeypatch
):
    verified_member(app, owner, outbox)
    # Make the work after the reply slow (the code's commit and the send): if it ran before the reply, it'd show.
    real = accounts.reset_link

    def slow(*a, **k):
        time.sleep(0.3)
        return real(*a, **k)

    monkeypatch.setattr(accounts, "reset_link", slow)
    c = client(app)

    def timed(who: str) -> float:
        t = time.perf_counter()
        assert c.post("/api/forgot", json={"account": who}, headers=W).json() == SAME
        return time.perf_counter() - t

    found = statistics.median(timed(w) for w in ["maya@example.com", "maya"] * 3)
    missing = statistics.median(timed(w) for w in ["nobody@example.com", "nobody"] * 3)
    app.state.drain_mail(30)
    assert abs(found - missing) < 0.05 and found < 0.25


def test_the_limits_come_first_send_nothing_and_hide_the_no_email_answer(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    join(app, owner, "sam")
    for _ in range(3):
        assert forgot(app, "maya@example.com").json() == SAME
    assert forgot(app, "maya@example.com").json() == SAME  # the 4th in an hour for one account
    assert len(outbox.sent) == 3
    assert forgot(app, "maya").json() == SAME  # by username it's its own count
    assert len(outbox.sent) == 4
    # Past the limit, an account with no email gets the alike reply too, so the error can't be used to scan.
    assert [forgot(app, "sam").status_code for _ in range(4)] == [400, 400, 400, 200]
    # 10 an hour from one IP address, whatever it asks about: the 11th finds nothing, even for maya.
    now = time.monotonic()
    for i in range(10):
        assert accounts.forgot(f"n{i}", "203.0.113.9", now=now) == ("quiet", None)
    assert accounts.forgot("maya@example.com", "203.0.113.9", now=now) == ("quiet", None)
    answer, acct = accounts.forgot("maya@example.com", "203.0.113.9", now=now + 3601)  # an hour later
    assert answer == "send" and acct.username == "maya"


def test_the_daily_cap_holds_across_every_kind_of_email():
    clock = [1000.0]
    inner = mail.FakeMailer()
    capped = mail.Capped(inner, cap=3, clock=lambda: clock[0])
    for _ in range(5):
        mail.deliver(capped, "verify", "a@example.com", "u1", link="x")
    assert len(inner.sent) == 3
    clock[0] += 86401
    assert mail.deliver(capped, "changed", "a@example.com", "u1", username="a", app=APP)
    assert mail.DAILY_CAP == 90


def test_a_malformed_email_is_refused_without_a_lookup(app, owner):
    c = client(app)
    r = c.post("/api/forgot", json={"account": "maya@example"}, headers=W)
    assert r.status_code == 400
    assert r.json()["detail"] == [
        {
            "field": "account",
            "rule": "e.syntax",
            "message": "That doesn’t look like an email address. Check for typos.",
        }
    ]
    r = c.post("/api/forgot", json={"account": "  "}, headers=W)
    assert r.json()["detail"] == [
        {"field": "account", "rule": "f.required", "message": "Enter your username or email."}
    ]


def test_links_use_the_app_address_even_with_a_forged_host(app, owner, outbox):
    verified_member(app, owner, outbox)
    evil = TestClient(app, base_url="https://evil.example", follow_redirects=False)
    assert (
        evil.post("/api/forgot", json={"account": "maya"}, headers=W | {"Host": "evil.example"}).json()
        == SAME
    )
    app.state.drain_mail()
    (sent,) = outbox.sent
    assert (
        f"{APP}/reset#" in sent.text and f"{APP}/reset#" in sent.html and "evil" not in sent.text + sent.html
    )


# --- the reset code from email -----------------------------------------------------------------------------------


def test_an_emailed_code_lasts_30_minutes_works_once_and_revokes_the_others(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    maya = accounts.by_name("maya")
    by_owner = owner.post(
        "/api/invites", json={"kind": "reset", "for_user_id": maya.user_id}, headers=W
    ).json()
    forgot(app, "maya@example.com")
    code = link_in(outbox.sent[-1], "reset")
    inv = accounts._find(code)
    assert inv.created_by == "email" and inv.expires_at - inv.created_at == timedelta(minutes=30)
    assert accounts.invites[by_owner["invite_id"]][0].revoked  # a new request revokes the open one
    # Another request revokes this one too; the newest works.
    forgot(app, "maya@example.com")
    newest = link_in(outbox.sent[-1], "reset")
    assert accounts._find(code).revoked
    epoch = maya.session_epoch
    outbox.sent.clear()
    r = client(app).post("/api/reset", json={"code": newest, "password": NEW_PW}, headers=W)
    assert r.json() == {"ok": True, "signin": "maya"}
    assert "session" not in r.headers.get("set-cookie", "")  # the email path ends on sign-in (OWASP)
    assert accounts.by_name("maya").session_epoch == epoch + 1
    app.state.drain_mail()
    (notice,) = outbox.sent
    assert (
        notice.subject == "Your registry.coffee password was changed"
        and "every device was signed out" in notice.text
    )
    again = client(app).post(
        "/api/reset", json={"code": newest, "password": "another fine password"}, headers=W
    )
    assert again.status_code == 400  # once
    accounts.sign_in("maya", NEW_PW, "1.1.1.1")


def test_an_emailed_code_stops_working_after_30_minutes(app, owner, outbox, accounts):
    from dataclasses import replace

    verified_member(app, owner, outbox)
    forgot(app, "maya@example.com")
    code = link_in(outbox.sent[-1], "reset")
    inv = accounts._find(code)
    accounts.invites[inv.invite_id] = (replace(inv, expires_at=_now() - timedelta(seconds=1)), _now())
    assert (
        client(app).post("/api/reset", json={"code": code, "password": NEW_PW}, headers=W).status_code == 400
    )


def test_a_reset_revokes_the_other_open_codes_and_the_owner_link_still_signs_in(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    maya = accounts.by_name("maya")
    forgot(app, "maya@example.com")
    emailed = link_in(outbox.sent[-1], "reset")
    # The owner's link, made after: it revoked the emailed one; using it signs in and sends the notice.
    by_owner = owner.post(
        "/api/invites", json={"kind": "reset", "for_user_id": maya.user_id}, headers=W
    ).json()
    assert accounts._find(emailed).revoked
    outbox.sent.clear()
    r = client(app).post("/api/reset", json={"code": by_owner["code"], "password": NEW_PW}, headers=W)
    assert r.json() == {"ok": True} and "session=" in r.headers["set-cookie"]
    app.state.drain_mail()
    assert [m.subject for m in outbox.sent] == ["Your registry.coffee password was changed"]


def test_a_turned_off_account_gets_no_link(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    owner.post(f"/api/people/{accounts.by_name('maya').user_id}", json={"disabled": True}, headers=W)
    forgot(app, "maya@example.com")
    assert outbox.sent == []


# --- verifying an email --------------------------------------------------------------------------------------------


def test_the_verify_link_works_once_and_expires_after_24_hours(app, owner, outbox, accounts):
    from dataclasses import replace

    join(app, owner, "maya", "maya@example.com")
    (sent,) = outbox.sent
    assert sent.subject == "Confirm your email for registry.coffee" and "24 hours" in sent.text
    token = link_in(sent, "verify")
    contact = accounts.contacts[accounts.by_name("maya").user_id][0]
    assert contact.verify_expires - _now() <= timedelta(
        hours=24
    ) and contact.verify_expires - _now() > timedelta(hours=23)
    # An unverified email can't receive a reset.
    forgot(app, "maya@example.com")
    assert len(outbox.sent) == 1
    # Expired: refused, with the words that say what to do.
    uid = contact.user_id
    accounts.contacts[uid] = (replace(contact, verify_expires=_now() - timedelta(seconds=1)), _now())
    r = client(app).post("/api/verify", json={"token": token}, headers=W)
    assert r.status_code == 400 and r.json()["detail"][0]["message"].startswith("That link has expired")
    accounts.contacts[uid] = (contact, _now())
    assert client(app).post("/api/verify", json={"token": token}, headers=W).json() == SAME
    assert client(app).post("/api/verify", json={"token": token}, headers=W).status_code == 400  # once
    assert accounts.email(uid).verified


def test_an_email_verified_on_another_account_looks_the_same_and_sends_nothing(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    sam = join(app, owner, "sam", "MAYA@example.com")
    assert outbox.sent == []  # no verify link, and nothing to maya either
    view = accounts.email(accounts.by_name("sam").user_id)
    assert view.address == "MAYA@example.com" and not view.verified
    assert (
        sam.post("/api/email/resend", headers=W | {"X-Expected-User": accounts.by_name("sam").user_id}).json()
        == SAME
    )
    app.state.drain_mail()
    assert outbox.sent == []
    forgot(app, "maya@example.com")
    assert [m.to for m in outbox.sent] == ["maya@example.com"]  # still only maya's account


def test_send_again_is_limited_to_3_an_hour(app, owner, outbox, accounts):
    c = join(app, owner, "maya", "maya@example.com")
    me = W | {"X-Expected-User": accounts.by_name("maya").user_id}
    codes = [c.post("/api/email/resend", headers=me).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    app.state.drain_mail()
    assert len(outbox.sent) == 4  # the one on joining and three more
    tokens = [link_in(m, "verify") for m in outbox.sent]
    assert (
        client(app).post("/api/verify", json={"token": tokens[0]}, headers=W).status_code == 400
    )  # replaced
    assert client(app).post("/api/verify", json={"token": tokens[-1]}, headers=W).json() == SAME


# --- storage, change, removal and erasure -------------------------------------------------------------------------


def every_byte(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*.parquet"))


def test_no_plaintext_email_is_stored_and_the_keyed_hash_finds_it(app, owner, outbox, accounts):
    verified_member(app, owner, outbox, email="maya.lee@example.com")
    stored = every_byte(accounts.lake.root)
    assert b"maya.lee" not in stored and b"example.com" not in stored
    (row,) = accounts.lake.latest("contacts", "user_id")
    assert row["email_hmac"] == accounts.crypto.lookup("maya.lee@example.com")
    assert accounts._verified_with(row["email_hmac"]).user_id == accounts.by_name("maya").user_id
    # Sealed to the account: the same ciphertext under another user id doesn't open.
    with pytest.raises(Exception):  # noqa: B017 - cryptography's InvalidTag
        accounts.crypto.open(row["email_enc"], "someone-else")
    # It survives a restart.
    again = new_accounts(accounts.lake.root)
    assert (
        again.email(row["user_id"]).address == "maya.lee@example.com" and again.email(row["user_id"]).verified
    )


def test_change_and_remove_leave_no_file_of_the_old_record_and_squash(
    app, owner, outbox, accounts, monkeypatch
):
    squashed = []
    monkeypatch.setattr(accounts.lake, "squash", lambda message: squashed.append(message))
    c = verified_member(app, owner, outbox)
    uid = accounts.by_name("maya").user_id
    me = W | {"X-Expected-User": uid}
    folder = accounts.lake.root / "contacts"
    before = {p.name for p in folder.glob(f"{uid}__*.parquet")}
    assert len(before) == 2  # added, then verified
    r = c.post("/api/email", json={"email": "maya@new.example"}, headers=me)
    assert r.json() == {"email": "maya@new.example", "verified": False}
    after = {p.name for p in folder.glob(f"{uid}__*.parquet")}
    assert len(after) == 1 and not after & before and len(squashed) == 1
    app.state.drain_mail()
    assert [m.to for m in outbox.sent] == ["maya@new.example"]
    forgot(
        app, "maya@example.com"
    )  # the old address no longer finds anyone, and the new one isn't verified yet
    assert len(outbox.sent) == 1
    assert c.post("/api/email/remove", headers=me).json() == SAME
    assert not list(folder.glob(f"{uid}__*.parquet")) and len(squashed) == 2
    assert accounts.email(uid) is None and new_accounts(accounts.lake.root).email(uid) is None


def test_the_same_verified_address_again_changes_nothing(app, owner, outbox, accounts):
    c = verified_member(app, owner, outbox)
    me = W | {"X-Expected-User": accounts.by_name("maya").user_id}
    assert c.post("/api/email", json={"email": "Maya@Example.com"}, headers=me).json() == {
        "email": "maya@example.com",
        "verified": True,
    }
    app.state.drain_mail()
    assert outbox.sent == []


def test_delete_user_removes_the_contact_record_and_squashes(tmp_path, monkeypatch):
    accounts = new_accounts(tmp_path / "users")
    o = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    _, code = accounts.invite(o.user_id)
    maya, link = accounts.join_sending(code, "maya", MAYA_PW, "1.1.1.1", agree=True, email="maya@example.com")
    accounts.verify_email(link[1], "1.1.1.1")
    accounts.set_disabled(o.user_id, maya.user_id, True)
    main, photos = Lake(str(tmp_path / "lake")), Lake(str(tmp_path / "photos"), tables=PHOTO_TABLES)
    p = erase.plan("maya", main, photos, accounts.lake)
    mine = [f for f in p.files[accounts.lake.url] if f.startswith("contacts/")]
    assert len(mine) == 2
    squashed = []
    monkeypatch.setattr(accounts.lake, "squash", lambda message: squashed.append(message))
    erase.apply(p, main, photos, accounts.lake)
    assert not list((tmp_path / "users" / "contacts").glob(f"{maya.user_id}__*")) and squashed


def test_publishing_deletes_and_adds_in_one_commit(tmp_path, monkeypatch):
    """A remote lake: the old record's files go in the same commit as the new one, and squash drops history."""
    import huggingface_hub

    calls = []

    class Api:
        def __init__(self, *a, **k):
            pass

        def get_paths_info(self, repo_id, paths, repo_type):
            return [
                type("F", (), {"path": p})() for p in paths if p == "contacts/u1__20260101T000000.parquet"
            ]

        def list_repo_tree(self, repo_id, path_in_repo, repo_type):
            return [type("F", (), {"path": "contacts/u1__20260101T000000.parquet"})(),
                    type("F", (), {"path": "contacts/u2__20260101T000000.parquet"})()]  # fmt: skip

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            calls.append(sorted((type(o).__name__, o.path_in_repo) for o in operations))

        def super_squash_history(self, repo_id, repo_type, commit_message):
            calls.append(("squash", repo_id))

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr("coffeedata.lake.remote_folders", lambda url: None)
    lake = Lake("hf://datasets/owner/users", staging=str(tmp_path / "staging"), tables=USER_TABLES)
    assert lake.record_files("contacts", "u1") == ["contacts/u1__20260101T000000.parquet"]
    old = lake.record_files("contacts", "u1")  # listed before the new version is staged, as set_email does
    lake.write("contacts", "contacts/u1__20261108T000000.parquet", [{"user_id": "u1"}])
    lake.publish("change", delete=old)
    lake.squash("squash")
    adds = [c for c in calls[0] if c[0] == "CommitOperationAdd" and c[1].startswith("contacts/u1")]
    assert ("CommitOperationDelete", "contacts/u1__20260101T000000.parquet") in calls[0] and adds
    assert calls[-1] == ("squash", "owner/users")


# --- who sees an email ----------------------------------------------------------------------------------------------


def test_the_owner_sees_members_emails_and_members_see_only_their_own(app, owner, outbox, accounts):
    maya = verified_member(app, owner, outbox)
    join(app, owner, "sam", "sam@example.com")
    people = owner.get("/invite").text
    assert "maya@example.com · verified" in people and "sam@example.com · not verified yet" in people
    assert "Remove maya’s email" in people
    for path in ("/", "/setup", "/coffees", "/history"):
        page = maya.get(path, headers={"Accept": "text/html"}).text
        assert "sam@example.com" not in page, path
    assert "maya@example.com · verified" in maya.get("/setup").text
    assert maya.get("/invite").status_code == 404
    # Only the owner can remove someone else's email.
    sam_id = accounts.by_name("sam").user_id
    me = W | {"X-Expected-User": accounts.by_name("maya").user_id}
    assert maya.post(f"/api/people/{sam_id}/email/remove", headers=me).status_code == 400
    assert owner.post(f"/api/people/{sam_id}/email/remove", headers=W).json() == SAME
    assert accounts.email(sam_id) is None


def test_today_asks_for_an_email_until_not_now(app, owner, outbox, accounts):
    c = join(app, owner, "sam")
    assert "Add an email so you can reset your password yourself." in c.get("/").text
    c.cookies.set("nudge-email", "1")
    assert "Add an email so you can reset" not in c.get("/").text
    maya = join(app, owner, "maya", "maya@example.com")
    assert (
        "Add an email so you can reset" not in maya.get("/").text
    )  # has one (not verified yet: Setup says so)


# --- the marketing box (spec §7.1) -----------------------------------------------------------------------------------


def test_the_marketing_choice_is_recorded_with_its_time_and_version(app, owner, outbox, accounts):
    join(app, owner, "maya", "maya@example.com", marketing=True)
    join(app, owner, "sam", "sam@example.com", marketing=False)
    rows = {r["user_id"]: r for r in accounts.lake.latest("consents", "user_id")}
    maya, sam = rows[accounts.by_name("maya").user_id], rows[accounts.by_name("sam").user_id]
    assert maya["marketing"] is True and sam["marketing"] is False
    for r in (maya, sam):
        assert (
            r["marketing_version"] == LEGAL_UPDATED
            and r["marketing_at"] is not None
            and r["source"] == "signup"
        )
    assert rows[accounts.by_name("jtouley").user_id]["marketing"] is None  # owner setup doesn't offer it


def test_the_join_page_offers_email_and_a_ticked_marketing_box(app):
    page = client(app).get("/join").text
    assert (
        'type="email"' in page
        and 'autocomplete="email"' in page
        and "Optional. For resetting your password" in page
    )
    assert re.search(r'<input type="checkbox" id="marketing" name="marketing" checked>', page)
    assert page.index('id="username"') < page.index('name="email"') < page.index('id="password"')
    assert page.index('id="agree"') < page.index('id="marketing"')


def test_setup_changes_marketing_and_sends_go_only_to_verified_ticked_members(app, owner, outbox, accounts):
    maya = verified_member(app, owner, outbox)
    join(app, owner, "sam", "sam@example.com", marketing=True)  # not verified
    verified_member(app, owner, outbox, "ana", "ana@example.com")
    ana = accounts.by_name("ana").user_id
    assert sorted(a for _, a in accounts.marketing_recipients()) == ["ana@example.com", "maya@example.com"]
    me = W | {"X-Expected-User": accounts.by_name("maya").user_id}
    assert maya.post("/api/marketing", json={"marketing": False}, headers=me).json() == {"marketing": False}
    assert [a for _, a in accounts.marketing_recipients()] == ["ana@example.com"]
    latest = {r["user_id"]: r for r in accounts.lake.latest("consents", "user_id")}[
        accounts.by_name("maya").user_id
    ]
    assert (
        latest["source"] == "settings"
        and latest["marketing"] is False
        and latest["terms_version"] == LEGAL_UPDATED
    )
    accounts.set_disabled(accounts.by_name("jtouley").user_id, ana, True)
    assert accounts.marketing_recipients() == []


def test_reaccepting_new_terms_keeps_the_marketing_choice(tmp_path, lake):
    root = tmp_path / "users"
    first = new_accounts(root, legal="November 2, 2026")
    o = first.create_owner(SETUP, "jtouley", PW, agree=True)
    _, code = first.invite(o.user_id)
    maya = first.join(code, "maya", MAYA_PW, "1.1.1.1", agree=True, email="maya@example.com", marketing=True)
    later = new_accounts(root)
    assert later.needs_agree(maya.user_id)
    later.accept(maya.user_id)
    c = later.consents[maya.user_id][0]
    assert c.source == "reaccept" and c.terms_version == LEGAL_UPDATED and c.marketing is True


# --- without EMAIL_KEY, or without a way to send: hidden --------------------------------------------------------------


def test_without_a_key_the_email_features_are_hidden(tmp_path, lake):
    accounts = new_accounts(tmp_path / "users", key=None)
    app = create_app(lake, Sessions(accounts), mailer=mail.FakeMailer())
    c = client(app)
    page = c.get("/join").text
    assert 'name="email"' not in page and 'id="marketing"' not in page
    assert "No email on your account?" not in c.get("/forgot").text
    assert c.post("/api/forgot", json={"account": "a@example.com"}, headers=W).status_code == 404
    o = new_accounts(tmp_path / "users2")
    app2 = create_app(lake, Sessions(o), mailer=None)  # a key but nothing to send with
    assert 'name="email"' not in client(app2).get("/join").text


def test_a_bad_key_turns_email_off_without_stopping_the_app(monkeypatch, caplog):
    from app.auth import email_key

    with pytest.raises(EmailKeyError):
        EmailCrypto("not base64!")
    with pytest.raises(EmailKeyError):
        EmailCrypto(base64.b64encode(b"short").decode())
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("SPACE_ID", raising=False)
    monkeypatch.setenv("EMAIL_KEY", "nope")
    assert email_key() is None
    assert "nope" not in caplog.text
    monkeypatch.setenv("EMAIL_KEY", KEY)
    assert email_key() == KEY
    monkeypatch.setenv("K_SERVICE", "registry-coffee")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert email_key() is None  # hosted with no way to send
    assert mail.from_env(hosted=True) is None
    assert isinstance(mail.from_env(hosted=False), mail.Capped)  # a laptop: a FakeMailer, nothing leaves


@respx.mock
def test_resend_gets_one_call_with_the_message_and_no_tracking():
    route = respx.post(mail.RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    m = mail.ResendMailer("re_test", reply_to="help@example.com")
    m.send("a@example.com", *mail.render("verify", link=f"{APP}/verify#tok"))
    (call,) = route.calls
    assert call.request.headers["Authorization"] == "Bearer re_test"
    body = call.request.read().decode()
    assert '"to":["a@example.com"]' in body.replace(" ", "") and "hello@send.registry.coffee" in body
    assert '"reply_to":"help@example.com"' in body.replace(" ", "")
    respx.post(mail.RESEND_URL).mock(return_value=httpx.Response(422, json={"message": "a@example.com bad"}))
    with pytest.raises(RuntimeError) as e:
        m.send("a@example.com", "s", "t", "h")
    assert "a@example.com" not in str(e.value)


@pytest.mark.parametrize("kind", ["verify", "reset", "changed"])
def test_each_email_has_a_text_and_an_html_part_with_no_images_or_tracking(kind):
    subject, text, html = mail.render(kind, link=f"{APP}/x#tok", username="maya", app=APP)
    assert subject and text.strip() and html.strip()
    assert "<img" not in html and "<script" not in html and "http" not in html.replace(f"{APP}/x#tok", "")
    assert "Newsreader" in html and "#f6f5f2" in html and "#1b1714" in html
    assert ("#8a4b1d" in html) == (kind != "changed")  # one accent button, where there's something to do
    if kind != "changed":
        assert f"{APP}/x#tok" in text and f'href="{APP}/x#tok"' in html


def test_emails_and_links_never_reach_the_logs(app, owner, outbox, caplog):
    caplog.set_level("DEBUG")
    verified_member(app, owner, outbox)
    forgot(app, "maya@example.com")
    assert "maya@example.com" not in caplog.text and "#" not in "".join(
        r.getMessage() for r in caplog.records if r.name.startswith("app.mail")
    )


# --- headers, pages and the privacy policy ---------------------------------------------------------------------------


def test_recovery_pages_send_no_referrer(app):
    c = client(app)
    for path in ("/reset", "/forgot", "/verify"):
        assert c.get(path).headers["referrer-policy"] == "no-referrer", path
    assert c.get("/signin").headers["referrer-policy"] == "same-origin"


def test_forgot_and_verify_pages(app):
    c = client(app)
    page = c.get("/forgot").text
    for words in ("Reset your password", '<label class="flabel" for="account">Username or email</label>',
                  'autocomplete="username"', "Send reset link", 'data-busy="Sending…"',
                  "No email on your account? Ask the person who invited you for a reset link."):  # fmt: skip
        assert words in page
    assert "Checking your link…" in c.get("/verify").text


def test_the_privacy_policy_covers_email_and_marks_the_pending_passage():
    policy = (ROOT / "src/coffeedata/templates/privacy.html").read_text()
    for words in ("Your email address, if you add one", "<strong>Resend:</strong> sends the app's emails",
                  "To recommend coffees", "nudge-email", "We don't use third-party tracking or analytics",
                  "opted into marketing email"):  # fmt: skip
        assert words in policy
    # The insights passage waits on the owner's wording (task 0): marked in the page and in the template.
    assert policy.count('data-pending="owner-wording"') == 1 and "[Pending the owner's wording]" in policy
    assert LEGAL_UPDATED != "November 2, 2026"  # bumped, so every member accepts the new policy once


def test_rotating_the_email_key_reseals_everything_and_keeps_links_working(
    app, owner, outbox, accounts, monkeypatch
):
    from app import rekey

    verified_member(app, owner, outbox)
    join(app, owner, "sam", "sam@example.com")  # a verify link in flight
    sam_token = link_in(outbox.to("sam@example.com")[0], "verify")
    root = accounts.lake.root
    new_key = base64.b64encode(b"n" * 32).decode()
    monkeypatch.setenv("COFFEEDATA_USERS", str(root))
    monkeypatch.setenv("EMAIL_KEY", KEY)
    monkeypatch.setenv("EMAIL_KEY_NEW", new_key)
    before = {p.name for p in (root / "contacts").glob("*__*.parquet")}
    assert rekey.main([]) == 0
    assert not before & {p.name for p in (root / "contacts").glob("*__*.parquet")}  # old files gone
    after = new_accounts(root, key=new_key)
    assert after.email(after.by_name("maya").user_id).address == "maya@example.com"
    assert after.forgot("maya@example.com", "1.1.1.1")[0] == "send"  # the new keyed hash finds it
    after.verify_email(sam_token, "1.1.1.1")
    monkeypatch.setenv("EMAIL_KEY", new_key)
    assert rekey.main(["--check"]) == 0
    monkeypatch.setenv("EMAIL_KEY", KEY)
    assert rekey.main(["--check"]) == 1  # the old key opens nothing now
