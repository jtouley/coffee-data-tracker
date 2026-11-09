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
    a = create_app(lake, Sessions(accounts), mailer=outbox)
    a.state.forgot_floor = 0.01  # the real 2 s floor has its own test
    return a


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
    # The owner's choice: an account with no verified email is told so, on the field, by username or by its
    # unconfirmed address alike (review L8).
    for who in ("sam", "ana", " Ana ", "sam@example.com"):
        r = forgot(app, who)
        assert r.status_code == 400, who
        assert r.json()["detail"] == [{"field": "account", "rule": "f.no_email", "message": NO_EMAIL}]
    assert len(outbox.sent) == 2
    # Capitals and spaces in what's typed don't matter.
    assert forgot(app, "  MAYA@Example.com ").json() == SAME and len(outbox.sent) == 3


def test_forgot_takes_the_same_time_whether_or_not_there_is_an_account(
    app, owner, outbox, accounts, monkeypatch
):
    """The link is sent inside the request (Cloud Run has no CPU after the reply, review H1), and every answer is
    held to the same floor, so a slow send (0.3 s here) doesn't show."""
    verified_member(app, owner, outbox)
    app.state.forgot_floor = 0.6
    real = outbox.send

    def slow(*a, **k):
        time.sleep(0.3)
        return real(*a, **k)

    monkeypatch.setattr(outbox, "send", slow)
    c = client(app)

    def timed(who: str) -> float:
        t = time.perf_counter()
        assert c.post("/api/forgot", json={"account": who}, headers=W).json() == SAME
        return time.perf_counter() - t

    # Three for maya (her hourly limit; a 4th would be refused, and answered at once), three for no one.
    found = statistics.median(timed(w) for w in ["maya@example.com", "maya", "Maya"])
    missing = statistics.median(timed(w) for w in ["nobody@example.com", "nobody", "nobody2"])
    assert abs(found - missing) < 0.05 and 0.6 <= missing < 0.75
    assert (
        len(outbox.to("maya@example.com")) == 3
    )  # sent before each reply (the per-account limit stops the rest)
    from app.main import FORGOT_FLOOR

    assert FORGOT_FLOOR == 2.0


def test_the_limits_come_first_send_nothing_and_hide_the_no_email_answer(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    join(app, owner, "sam")
    for _ in range(3):
        assert forgot(app, "maya@example.com").json() == SAME
    assert forgot(app, "maya@example.com").json() == SAME  # the 4th in an hour for one account
    assert len(outbox.sent) == 3
    # By username it's the same account, and the same 3 an hour (review L1).
    assert forgot(app, "maya").json() == SAME and len(outbox.sent) == 3
    # Past the limit, an account with no email gets the alike reply too.
    assert [forgot(app, "sam").status_code for _ in range(4)] == [400, 400, 400, 200]
    # 10 an hour from one IP address, whatever it asks about: the 11th finds nothing, even for maya.
    now = time.monotonic()
    for i in range(10):
        assert accounts.forgot(f"n{i}", "203.0.113.9", now=now) == ("quiet", None)
    assert accounts.forgot("maya@example.com", "203.0.113.9", now=now) == ("limited", None)
    answer, acct = accounts.forgot("maya@example.com", "203.0.113.9", now=now + 3601)  # an hour later
    assert answer == "send" and acct.username == "maya"


def test_no_email_answers_are_capped_across_everyone(app, owner, outbox, accounts):
    """A scan names many accounts, so the per-account limit never applies; this caps how many it can confirm an
    hour (review M5)."""
    from app.accounts import NO_EMAIL_PER_HOUR

    now = time.monotonic()
    names = [f"m{i:02d}" for i in range(NO_EMAIL_PER_HOUR + 2)]
    for n in names:
        join(app, owner, n)
    answers = [accounts.forgot(n, f"10.0.0.{i}", now=now)[0] for i, n in enumerate(names)]
    assert answers.count("no_email") == NO_EMAIL_PER_HOUR and answers[-2:] == ["limited", "limited"]


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
    (sent,) = outbox.sent
    assert (
        f"{APP}/reset#" in sent.text and f"{APP}/reset#" in sent.html and "evil" not in sent.text + sent.html
    )


# --- the reset code from email -----------------------------------------------------------------------------------


def test_an_emailed_link_lasts_30_minutes_works_once_and_a_newer_one_replaces_it(
    app, owner, outbox, accounts
):
    verified_member(app, owner, outbox)
    maya = accounts.by_name("maya")
    commits = len(list((accounts.lake.root / "invites").glob("*.parquet")))
    forgot(app, "maya@example.com")
    first = link_in(outbox.sent[-1], "reset")
    assert (
        first.startswith("E-") and len(list((accounts.lake.root / "invites").glob("*.parquet"))) == commits
    )  # no write
    forgot(app, "maya")
    newest = link_in(outbox.sent[-1], "reset")
    epoch = maya.session_epoch
    outbox.sent.clear()
    old = client(app).post("/api/reset", json={"code": first, "password": NEW_PW}, headers=W)
    assert old.status_code == 400 and old.json()["detail"][0]["message"].startswith(
        "That link no longer works."
    )
    r = client(app).post("/api/reset", json={"code": newest, "password": NEW_PW}, headers=W)
    assert r.json() == {"ok": True, "signin": "maya"}
    assert "session" not in r.headers.get("set-cookie", "")  # the email path ends on sign-in (OWASP)
    assert accounts.by_name("maya").session_epoch == epoch + 1
    (notice,) = outbox.sent
    assert (
        notice.subject == "Your registry.coffee password was changed"
        and "every device was signed out" in notice.text
    )
    again = client(app).post(
        "/api/reset", json={"code": newest, "password": "another fine password"}, headers=W
    )
    assert again.status_code == 400  # once: the password it was signed over has changed
    accounts.sign_in("maya", NEW_PW, "1.1.1.1")
    # A forged or altered token is refused like a wrong code.
    tampered = newest[:-2] + ("AA" if not newest.endswith("AA") else "BB")
    assert (
        client(app).post("/api/reset", json={"code": tampered, "password": NEW_PW}, headers=W).status_code
        == 400
    )


def test_an_emailed_link_stops_working_after_30_minutes(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    maya = accounts.by_name("maya")
    token, _ = accounts.email_reset_token(maya, now=time.time() - 30 * 60 - 1)
    assert accounts._from_email_token(token) is None
    fresh, _ = accounts.email_reset_token(maya)
    assert accounts._from_email_token(fresh).user_id == maya.user_id
    # A turned-off account's link stops working too.
    accounts.set_disabled(accounts.by_name("jtouley").user_id, maya.user_id, True)
    assert accounts._from_email_token(fresh) is None


def test_forgot_never_touches_the_owners_reset_link_but_a_reset_ends_every_other_way_in(
    app, owner, outbox, accounts
):
    """Review M4: an anonymous /forgot doesn't revoke the owner's link (nor costs a commit). A reset, either way,
    ends the other: the owner's link revokes nothing it doesn't need to, but changes the password the emailed link
    was signed over; an emailed reset revokes the owner's open link."""
    verified_member(app, owner, outbox)
    maya = accounts.by_name("maya")
    by_owner = owner.post(
        "/api/invites", json={"kind": "reset", "for_user_id": maya.user_id}, headers=W
    ).json()
    for _ in range(3):
        forgot(app, "maya")
    assert not accounts.invites[by_owner["invite_id"]][0].revoked
    emailed = link_in(outbox.sent[-1], "reset")
    outbox.sent.clear()
    r = client(app).post("/api/reset", json={"code": by_owner["code"], "password": NEW_PW}, headers=W)
    assert (
        r.json() == {"ok": True} and "session=" in r.headers["set-cookie"]
    )  # the owner's link still signs in
    assert [m.subject for m in outbox.sent] == ["Your registry.coffee password was changed"]
    assert (
        client(app)
        .post("/api/reset", json={"code": emailed, "password": "x" + NEW_PW}, headers=W)
        .status_code
        == 400
    )
    # The other way round: an emailed reset revokes the owner's open link.
    second = owner.post("/api/invites", json={"kind": "reset", "for_user_id": maya.user_id}, headers=W).json()
    token, _ = accounts.email_reset_token(accounts.by_name("maya"))
    assert (
        client(app)
        .post("/api/reset", json={"code": token, "password": "third " + NEW_PW}, headers=W)
        .json()["ok"]
    )
    assert accounts.invites[second["invite_id"]][0].revoked


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
    assert outbox.sent == []
    forgot(app, "maya@example.com")
    assert [m.to for m in outbox.sent] == ["maya@example.com"]  # still only maya's account


def test_send_again_is_limited_to_3_an_hour(app, owner, outbox, accounts):
    c = join(app, owner, "maya", "maya@example.com")
    me = W | {"X-Expected-User": accounts.by_name("maya").user_id}
    codes = [c.post("/api/email/resend", headers=me).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
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


def test_change_and_remove_need_the_password_tell_the_old_address_and_leave_no_file(
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
    # Without the current password, nothing changes (review M3).
    for current in ("", "wrong password!"):
        r = c.post("/api/email", json={"email": "maya@new.example", "current": current}, headers=me)
        assert r.status_code == 400 and r.json()["detail"][0]["field"] == "current"
    assert {p.name for p in folder.glob(f"{uid}__*.parquet")} == before and outbox.sent == []
    r = c.post("/api/email", json={"email": "maya@new.example", "current": MAYA_PW}, headers=me)
    assert r.json() == {"email": "maya@new.example", "verified": False}
    after = {p.name for p in folder.glob(f"{uid}__*.parquet")}
    assert len(after) == 1 and not after & before and len(squashed) == 1
    # The new address gets its link; the old one hears of the change, without the new address.
    assert sorted(m.to for m in outbox.sent) == ["maya@example.com", "maya@new.example"]
    (told,) = outbox.to("maya@example.com")
    assert told.subject == "The email on your registry.coffee account changed" and "maya@new" not in told.text
    outbox.sent.clear()
    forgot(app, "maya@example.com")  # the old address no longer finds anyone
    assert outbox.sent == []
    assert c.post("/api/email/remove", json={"current": "nope"}, headers=me).status_code == 400
    assert c.post("/api/email/remove", json={"current": MAYA_PW}, headers=me).json() == SAME
    assert not list(folder.glob(f"{uid}__*.parquet")) and len(squashed) == 2
    assert outbox.sent == []  # the new address was never verified, so nobody to tell
    assert accounts.email(uid) is None and new_accounts(accounts.lake.root).email(uid) is None


def test_the_same_verified_address_again_changes_nothing(app, owner, outbox, accounts):
    c = verified_member(app, owner, outbox)
    me = W | {"X-Expected-User": accounts.by_name("maya").user_id}
    r = c.post("/api/email", json={"email": "Maya@Example.com", "current": MAYA_PW}, headers=me)
    assert r.json() == {"email": "maya@example.com", "verified": True}
    assert outbox.sent == []


def test_delete_user_removes_the_contact_record_and_squashes(tmp_path, monkeypatch):
    accounts = new_accounts(tmp_path / "users")
    o = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    _, code = accounts.invite(o.user_id)
    maya, link = accounts.join_sending(code, "maya", MAYA_PW, "1.1.1.1", agree=True, email="maya@example.com")
    accounts.verify_email(link[1], "1.1.1.1")
    accounts.set_insights(maya.user_id, False)  # the insights choice goes with the account too
    accounts.set_disabled(o.user_id, maya.user_id, True)
    main, photos = Lake(str(tmp_path / "lake")), Lake(str(tmp_path / "photos"), tables=PHOTO_TABLES)
    p = erase.plan("maya", main, photos, accounts.lake)
    mine = [f for f in p.files[accounts.lake.url] if f.startswith("contacts/")]
    assert len(mine) == 2
    squashed = []
    monkeypatch.setattr(accounts.lake, "squash", lambda message: squashed.append(message))
    erase.apply(p, main, photos, accounts.lake)
    assert not list((tmp_path / "users" / "contacts").glob(f"{maya.user_id}__*")) and squashed
    assert not list((tmp_path / "users" / "consents").glob(f"{maya.user_id}__*"))


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
    # The no-email path is only the inline error (owner, 2026-11-08), not a line under the button.
    assert "No email on your account?" not in page
    assert page.index("We’ll email you a link") < page.index("data-sent") < page.index('id="forgot"')
    for words in ("Reset your password", '<label class="flabel" for="account">Username or email</label>',
                  'autocomplete="username"', "Send reset link", 'data-busy="Sending…"',
                  'data-sent role="status" hidden'):  # fmt: skip
        assert words in page
    assert "Checking your link…" in c.get("/verify").text


def test_the_privacy_policy_covers_email_and_roaster_insights():
    policy = " ".join((ROOT / "src/coffeedata/templates/privacy.html").read_text().split())
    for words in ("Your email address, if you add one", "<strong>Resend:</strong> sends the app's emails",
                  "To recommend coffees", "nudge-email", "We don't use third-party tracking or analytics",
                  "Resend receives it to deliver each email"):  # fmt: skip
        assert words in policy
    # The pre-ticked box, said as it is (review M8).
    assert "unless you untick that box when you sign up" in policy and "if you tick that box" not in policy
    # Insights: in the future tense, nothing said to exist that doesn't (review H3, M7).
    assert "<strong>Insights for roasters.</strong> We may in future sell" in policy
    assert "No report has been sold yet. Before we release any report, we will make sure that:" in policy
    for promise in ("at least 11 members", "could be worked out from the others is", "record of every report",
                    "agreed in writing not to try to identify anyone"):  # fmt: skip
        assert promise in policy
    assert "Insights we sell" not in policy and "Include my shots and bags in roaster insights" in policy
    assert "Your personal data isn't sold," in policy  # the summary
    setup = (ROOT / "app/templates/setup.html").read_text()
    assert "Include my shots and bags in roaster insights" in setup and "sold to roasters" not in setup
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


# --- roaster insights: the setting the privacy policy names --------------------------------------------------------


def test_shots_count_in_roaster_insights_until_turned_off_and_the_choice_is_recorded(
    app, owner, outbox, accounts
):
    maya = join(app, owner, "maya", "maya@example.com", marketing=True)
    uid = accounts.by_name("maya").user_id
    me = W | {"X-Expected-User": uid}
    assert accounts.insights(uid) is True and accounts.insights_excluded() == set()  # on by default
    page = maya.get("/setup").text
    assert re.search(
        r'<input type="checkbox" id="insights" data-insights aria-describedby="insights-h insights-s" checked>',
        page,
    )
    assert maya.post("/api/insights", json={"insights": False}, headers=me).json() == {"insights": False}
    assert accounts.insights_excluded() == {uid}
    row = {r["user_id"]: r for r in accounts.lake.latest("consents", "user_id")}[uid]
    assert (
        row["insights"] is False
        and row["insights_version"] == LEGAL_UPDATED
        and row["insights_at"] is not None
    )
    assert row["source"] == "settings" and row["marketing"] is True and row["terms_version"] == LEGAL_UPDATED
    assert (
        'id="insights" data-insights aria-describedby="insights-h insights-s">' in maya.get("/setup").text
    )  # unticked
    # It survives a restart, a marketing change and re-acceptance of newer terms.
    maya.post("/api/marketing", json={"marketing": False}, headers=me)
    later = new_accounts(accounts.lake.root, legal="December 31, 2026")
    later.accept(uid)
    assert later.insights(uid) is False and later.consents[uid][0].source == "reaccept"
    assert maya.post("/api/insights", json={"insights": "no"}, headers=me).status_code == 422
    assert client(app).post("/api/insights", json={"insights": True}, headers=W).status_code == 401


def test_the_insights_setting_is_there_without_email(tmp_path, lake):
    accounts = new_accounts(tmp_path / "users", key=None)
    app = create_app(lake, Sessions(accounts), mailer=None)
    c = client(app)
    c.post(
        "/api/signin",
        json={"setup_code": SETUP, "username": "jtouley", "password": PW, "agree": True},
        headers=W,
    )
    page = c.get("/setup").text
    assert "Include my shots and bags in roaster insights" in page and 'id="marketing"' not in page
    assert c.post("/api/insights", json={"insights": False}, headers=W).json() == {"insights": False}


# --- the review's fix round (PR #134) -----------------------------------------------------------------------------


def test_a_wrong_but_well_formed_key_reads_as_no_email_instead_of_failing(tmp_path, lake, caplog):
    """Review M1: an email sealed under another key (say, added mid-rotation) is no email, logged by user id."""
    accounts = new_accounts(tmp_path / "users")
    o = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    _, code = accounts.invite(o.user_id)
    maya, link = accounts.join_sending(code, "maya", MAYA_PW, "1.1.1.1", agree=True, email="maya@example.com")
    accounts.verify_email(link[1], "1.1.1.1")
    other = new_accounts(tmp_path / "users", key=base64.b64encode(b"z" * 32).decode())
    app = create_app(lake, Sessions(other), mailer=mail.FakeMailer())
    owner = client(app)
    owner.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W)
    assert owner.get("/invite").status_code == 200
    assert other.email(maya.user_id) is None and other.unreadable() == [maya.user_id]
    assert "maya@example.com" not in caplog.text
    assert any(getattr(r, "fields", {}).get("user_id") == maya.user_id for r in caplog.records)
    r = client(app).post("/api/forgot", json={"account": "maya"}, headers=W)
    assert r.status_code == 400 and r.json()["detail"][0]["rule"] == "f.no_email"  # as an account without one


def test_joining_replies_the_same_whether_or_not_a_link_went(app, owner, outbox, accounts):
    """Review M2: the reply no longer says whether the address is verified on another account."""
    verified_member(app, owner, outbox)
    codes = [owner.post("/api/invites", json={}, headers=W).json()["code"] for _ in range(2)]
    bodies = [
        {
            "code": codes[0],
            "username": "sam",
            "password": MAYA_PW,
            "agree": True,
            "email": "MAYA@example.com",
        },
        {"code": codes[1], "username": "ana", "password": MAYA_PW, "agree": True, "email": "ana@example.com"},
    ]
    replies = [client(app).post("/api/join", json=b, headers=W) for b in bodies]
    assert [r.json() for r in replies] == [{"ok": True}, {"ok": True}]
    assert [m.to for m in outbox.sent] == ["ana@example.com"]


def test_the_owner_removing_a_members_email_tells_that_address(app, owner, outbox, accounts):
    verified_member(app, owner, outbox)
    uid = accounts.by_name("maya").user_id
    assert owner.post(f"/api/people/{uid}/email/remove", headers=W).json() == SAME
    (told,) = outbox.sent
    assert told.to == "maya@example.com" and "The owner of registry.coffee removed this email" in told.text


def test_a_delete_that_fails_waits_for_the_next_publish(tmp_path, monkeypatch):
    """Review M6: the delete list is kept in staging until a commit has removed the files."""
    import huggingface_hub

    from coffeedata.lake import PENDING_DELETES

    commits, fail = [], [True]

    class Api:
        def __init__(self, *a, **k):
            pass

        def get_paths_info(self, repo_id, paths, repo_type):
            return [type("F", (), {"path": p})() for p in paths if p.startswith("contacts/u1__2026")]

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            if fail[0]:
                raise RuntimeError("HF is down")
            commits.append(sorted((type(o).__name__, o.path_in_repo) for o in operations))

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr("coffeedata.lake.remote_folders", lambda url: None)
    monkeypatch.setattr("coffeedata.lake.time.sleep", lambda s: None)
    lake = Lake("hf://datasets/owner/users", staging=str(tmp_path / "staging"), tables=USER_TABLES)
    with pytest.raises(RuntimeError):
        lake.publish("remove", delete=["contacts/u1__20260101T000000.parquet"])
    assert (tmp_path / "staging" / PENDING_DELETES).exists()
    fail[0] = False
    lake.write("users", "users/u2__20261108T000000.parquet", [{"user_id": "u2"}])
    lake.publish("something else")
    assert ("CommitOperationDelete", "contacts/u1__20260101T000000.parquet") in commits[-1]
    assert not (tmp_path / "staging" / PENDING_DELETES).exists()


@respx.mock
def test_resend_is_retried_on_429_5xx_and_timeouts_with_one_idempotency_key():
    """Review M9."""
    waits = []
    route = respx.post(mail.RESEND_URL).mock(
        side_effect=[httpx.Response(503), httpx.ConnectTimeout("slow"), httpx.Response(200, json={"id": "x"})]
    )
    m = mail.ResendMailer("re_test", sleep=waits.append)
    m.send("a@example.com", "s", "t", "h")
    keys = {c.request.headers["Idempotency-Key"] for c in route.calls}
    assert len(route.calls) == 3 and len(keys) == 1 and len(waits) == 2
    respx.post(mail.RESEND_URL).mock(return_value=httpx.Response(422))
    with pytest.raises(RuntimeError):
        mail.ResendMailer("re_test", sleep=waits.append).send("a@example.com", "s", "t", "h")  # not retried
    respx.post(mail.RESEND_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RuntimeError):
        mail.ResendMailer("re_test", sleep=lambda s: None).send("a@example.com", "s", "t", "h")


def test_a_failed_send_gives_its_place_in_the_daily_cap_back():
    """Review L4."""

    class Down:
        def send(self, *a):
            raise RuntimeError("down")

    capped = mail.Capped(Down(), cap=1)
    for _ in range(3):
        assert mail.deliver(capped, "verify", "a@example.com", "u1", link="x") is False
    ok = mail.FakeMailer()
    capped.inner = ok
    assert mail.deliver(capped, "verify", "a@example.com", "u1", link="x") is True


def test_marketing_has_no_time_or_version_where_it_was_never_offered(tmp_path):
    """Review L2: owner setup, and an app without email, don't offer the box."""
    accounts = new_accounts(tmp_path / "users", key=None)
    o = accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    _, code = accounts.invite(o.user_id)
    maya = accounts.join(code, "maya", MAYA_PW, "1.1.1.1", agree=True, marketing=True)
    rows = {r["user_id"]: r for r in accounts.lake.latest("consents", "user_id")}
    for uid in (o.user_id, maya.user_id):
        assert (rows[uid]["marketing"], rows[uid]["marketing_at"], rows[uid]["marketing_version"]) == (
            None,
            None,
            None,
        )


def test_unwired_email_forms_post_and_change_nothing(app, owner, outbox, accounts):
    """Like #133's sign-in forms: without app.js they post, and what was typed is dropped, unread."""
    verified_member(app, owner, outbox)
    c = client(app)
    page = c.get("/forgot").text
    assert re.search(r'<form id="forgot"[^>]* method="post"', page)
    r = c.post("/forgot", data={"account": "maya"})
    assert r.status_code == 400 and "no link was sent" in r.text and outbox.sent == []
    setup = owner.get("/setup").text
    assert re.search(r'<form id="email-form"[^>]* method="post" action="/setup\?form=email"', setup)
    r = owner.post("/setup?form=email", data={"email": "x@example.com", "current": PW})
    assert r.status_code == 400 and "your email wasn’t changed" in r.text and PW not in r.text


def test_turning_email_off_never_destroys_its_secrets():
    """Review H2: the two email secrets are always in local.secrets; var.email only mounts them."""
    tf = ROOT / "src/terraform"
    provider, run = (tf / "provider.tf").read_text(), (tf / "cloud_run.tf").read_text()
    assert 'email_secrets = ["EMAIL_KEY", "RESEND_API_KEY"]' in provider
    secrets_line = next(line for line in provider.splitlines() if line.strip().startswith("secrets "))
    assert "local.email_secrets" in secrets_line and "var.email" not in secrets_line
    assert "if var.email || !contains(local.email_secrets, k)" in run


# --- the final review (PR #134) -----------------------------------------------------------------------------------


def test_a_burst_of_forgot_requests_doesnt_hold_up_the_rest_of_the_app(lake, tmp_path, monkeypatch):
    """Final review N1: the floor is an async wait, so it holds no worker thread. 60 requests that all get past
    the limits wait 1.5 s each, at once; /healthz meanwhile answers as quickly as when idle."""
    import concurrent.futures

    import app.accounts
    from tests.test_app_browser import _serve

    monkeypatch.setattr(
        app.accounts, "FORGOT_PER_IP", 10_000
    )  # one test client, one address: let them all in
    accounts = new_accounts(tmp_path / "users")
    accounts.create_owner(SETUP, "jtouley", PW, agree=True)
    a = create_app(lake, Sessions(accounts, secure=False), mailer=mail.FakeMailer())
    a.state.forgot_floor = 1.5
    server, thread, port = _serve(a)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client() as c:
            started = time.perf_counter()
            assert c.get(f"{base}/healthz").status_code == 200
            idle = time.perf_counter() - started

        def ask(i: int) -> float:
            with httpx.Client(timeout=30) as c:
                t = time.perf_counter()
                r = c.post(f"{base}/api/forgot", json={"account": f"nobody{i}"}, headers=W)
                assert r.json() == SAME
                return time.perf_counter() - t

        with concurrent.futures.ThreadPoolExecutor(60) as pool:
            burst = [pool.submit(ask, i) for i in range(60)]
            time.sleep(0.4)  # the burst is waiting out its floor now
            with httpx.Client() as c:
                t = time.perf_counter()
                assert c.get(f"{base}/healthz").status_code == 200
                busy = time.perf_counter() - t
            waits = [f.result() for f in burst]
    finally:
        server.should_exit = True
        thread.join()
    assert busy < max(0.3, idle * 5), (idle, busy)
    assert (
        min(waits) >= 1.5 and max(waits) < 4.0
    )  # every answer held to the floor, none queued behind the others


def test_an_answer_refused_by_a_limit_comes_back_at_once(app, owner, outbox, accounts):
    app.state.forgot_floor = 1.0
    for _ in range(3):
        forgot(app, "maya")
    t = time.perf_counter()
    assert forgot(app, "maya").json() == SAME  # the 4th: limited, same words
    assert time.perf_counter() - t < 0.5


def test_changing_or_removing_the_email_ends_an_outstanding_link(app, owner, outbox, accounts):
    """Final review N2: the link is bound to the verified email it was sent to."""
    c = verified_member(app, owner, outbox)
    uid = accounts.by_name("maya").user_id
    me = W | {"X-Expected-User": uid}
    forgot(app, "maya")
    link = link_in(outbox.sent[-1], "reset")
    assert accounts._from_email_token(link) is not None
    c.post("/api/email", json={"email": "maya@new.example", "current": MAYA_PW}, headers=me)
    assert accounts._from_email_token(link) is None
    r = client(app).post("/api/reset", json={"code": link, "password": NEW_PW}, headers=W)
    assert r.status_code == 400
    # Removing it does the same: confirm the new address, get a link, remove the email.
    token = link_in(outbox.to("maya@new.example")[-1], "verify")
    client(app).post("/api/verify", json={"token": token}, headers=W)
    forgot(app, "maya@new.example")
    link = link_in(outbox.to("maya@new.example")[-1], "reset")
    assert accounts._from_email_token(link) is not None
    c.post("/api/email/remove", json={"current": MAYA_PW}, headers=me)
    assert accounts._from_email_token(link) is None


def test_a_failed_send_doesnt_end_the_previous_link(app, owner, outbox, accounts, monkeypatch):
    """Final review N2 (related): "a newer link replaces the older one" only once the newer one has gone."""
    verified_member(app, owner, outbox)
    forgot(app, "maya")
    first = link_in(outbox.sent[-1], "reset")

    def down(*a):
        raise RuntimeError("Resend is down")

    monkeypatch.setattr(outbox, "send", down)
    assert forgot(app, "maya").json() == SAME  # nothing went
    assert accounts._from_email_token(first) is not None


def test_wrong_current_passwords_are_counted(app, owner, outbox, accounts):
    """Final review N3: past 10 wrong ones in 15 minutes, email and password changes refuse before checking."""
    from app.accounts import REAUTH_TRIES

    c = verified_member(app, owner, outbox)
    me = W | {"X-Expected-User": accounts.by_name("maya").user_id}
    for _ in range(REAUTH_TRIES):
        r = c.post("/api/email", json={"email": "x@example.com", "current": "guess"}, headers=me)
        assert r.status_code == 400
    for path, body in (("/api/email", {"email": "x@example.com", "current": MAYA_PW}),
                       ("/api/email/remove", {"current": MAYA_PW}),
                       ("/api/password", {"current": MAYA_PW, "new": "a brand new kettle"})):  # fmt: skip
        r = c.post(path, json=body, headers=me)
        assert r.status_code == 429 and "Too many wrong passwords" in r.text, path


def test_a_change_made_while_listing_still_deletes_every_old_file(app, owner, outbox, accounts, monkeypatch):
    """Final review N4: if the record changes between listing (outside the lock) and committing, it's listed
    again under the lock."""
    c = verified_member(app, owner, outbox)
    uid = accounts.by_name("maya").user_id
    me = W | {"X-Expected-User": uid}
    real = accounts.lake.record_files
    calls = []

    def listing(table, key=None):
        calls.append(key)
        if len(calls) == 1:  # a second change lands right after the first listing
            accounts.set_email(uid, "maya@other.example", MAYA_PW)
        return real(table, key)

    monkeypatch.setattr(accounts.lake, "record_files", listing)
    c.post("/api/email/remove", json={"current": MAYA_PW}, headers=me)
    monkeypatch.setattr(accounts.lake, "record_files", real)
    assert not list((accounts.lake.root / "contacts").glob(f"{uid}__*.parquet"))
    assert len(calls) >= 2


def test_the_reset_field_keeps_an_emailed_tokens_case(app):
    """Final review N6."""
    page = client(app).get("/reset").text
    assert re.search(r'<input class="input code" id="code"[^>]* autocapitalize="none"', page)
    assert ".input.code[data-token] { text-transform: none; }" in (ROOT / "app/static/app.css").read_text()


def test_refused_requests_add_no_keys():
    """Final review N7."""
    from app.limits import Windows

    w = Windows()
    assert w.take({"ip": (1, 3600)}, 0.0)
    for i in range(50):
        assert not w.take({"ip": (1, 3600), f"name:{i}": (3, 3600)}, 1.0)
    assert set(w.hits) == {"ip"}


def test_the_copy_names_who_sees_an_email_and_explains_insights(app, owner):
    """Final review N10."""
    assert "Only the owner, and our email provider, see it." in client(app).get("/join").text
    setup = owner.get("/setup").text
    assert "Totals across at least 11 members. None sold yet." in setup
    assert re.search(
        r"Totals across at least 11 members\. None sold yet\. <a href=\"[^\"]*/privacy/\">Privacy Policy</a>",
        setup,
    )


def test_start_up_deletes_contact_files_left_from_a_changed_email(app, owner, outbox, accounts):
    """Final review N11: a change's delete that never landed (the instance stopped) is done at the next start."""
    verified_member(app, owner, outbox)
    uid = accounts.by_name("maya").user_id
    folder = accounts.lake.root / "contacts"
    # A leftover file from an older email, as if its delete had been lost.
    accounts.lake.write("contacts", f"contacts/{uid}__20260101T000000.parquet",
                        [{"user_id": uid, "email_hmac": "old-hash", "email_enc": "x", "logged_at": None}])  # fmt: skip
    assert len(list(folder.glob(f"{uid}__*.parquet"))) == 3
    again = new_accounts(accounts.lake.root)
    assert not (folder / f"{uid}__20260101T000000.parquet").exists()
    assert len(list(folder.glob(f"{uid}__*.parquet"))) == 2  # the current record's two files stay
    assert again.email(uid).address == "maya@example.com" and again.sweep_contacts() == []


def test_the_owner_never_resets_by_email(app, owner, outbox, accounts):
    """Owner decision (final review N9): the owner's account recovers with OWNER_SETUP_CODE only. /forgot answers
    for it like any ineligible account (same reply, same floor) and sends nothing; a signed link for it wouldn't
    work anyway; Setup and the emails call the owner's address what it is, for notices."""
    me = accounts.by_name("jtouley")
    page = owner.get("/setup").text
    assert "Your password is reset with the setup code, never by email." in page
    assert "For resetting your password yourself" not in page
    assert owner.post("/api/email", json={"email": "owner@example.com", "current": PW}, headers=W).json()[
        "email"
    ]
    (confirm,) = outbox.sent
    assert "for account notices" in confirm.text and "reset your password" not in confirm.text
    r = client(app).post("/api/verify", json={"token": link_in(confirm, "verify")}, headers=W)
    assert r.json() == {"ok": True, "notices_only": True}
    assert accounts.verified_email(me.user_id) == "owner@example.com"
    outbox.sent.clear()
    app.state.forgot_floor = 0.4
    timings = {}
    for who in ("jtouley", "owner@example.com", "nobody-at-all"):
        t = time.perf_counter()
        r = forgot(app, who)
        timings[who] = time.perf_counter() - t
        assert (r.status_code, r.json()) == (200, SAME), who
    assert outbox.sent == []
    assert all(0.4 <= t < 0.6 for t in timings.values()), timings  # held to the same floor as anyone's
    assert accounts.forgot("jtouley", "9.9.9.9") == ("quiet", None)
    assert accounts.reset_link(me.user_id) is None
    token, _ = accounts.email_reset_token(me)
    assert (
        client(app).post("/api/reset", json={"code": token, "password": NEW_PW}, headers=W).status_code == 400
    )
    assert "Add an email so you can reset" not in owner.get("/").text  # no nudge for the owner either
