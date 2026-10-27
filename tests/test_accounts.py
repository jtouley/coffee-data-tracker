"""Sign-in over a local users lake: owner bootstrap, passwords, lockouts, signed sessions and their epochs, CSRF,
per-user scoping with real accounts, and the headers and static files every response gets."""

import base64
import contextlib
import json
import re
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app.accounts import Account, AccountError, Accounts, check_password
from app.auth import COOKIE, Sessions
from app.main import create_app
from coffeedata import shots
from coffeedata.lake import Lake

W = {"X-Requested-With": "test"}
CODE = "K7QD-M2XF-9RPA-T4WE"
PW = "correct horse battery"


@pytest.fixture
def users(tmp_path) -> Lake:
    return Lake(str(tmp_path / "users"), tables=("users", "invites"))


def accounts(users: Lake) -> Accounts:
    return Accounts(users, "pepper-" + "p" * 16, "s" * 40, CODE)


def client(lake: Lake, users: Lake) -> TestClient:
    app = create_app(lake, Sessions(accounts(users)))
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def owner(c: TestClient) -> None:
    r = c.post("/api/signin", json={"setup_code": CODE, "username": "jtouley", "password": PW}, headers=W)
    assert r.status_code == 200, r.text


# --- owner bootstrap and sign-in -----------------------------------------------------------------------


def test_first_screen_is_owner_setup_and_needs_the_code(lake, users):
    c = client(lake, users)
    assert c.get("/").headers["location"] == "/signin"
    assert "Setup code" in c.get("/signin").text
    r = c.post("/api/signin", json={"setup_code": "nope", "username": "jtouley", "password": PW}, headers=W)
    assert r.status_code == 400 and "setup code" in r.json()["detail"]
    owner(c)
    assert c.get("/").status_code == 200  # signed in by creating the account
    # The code does nothing once an owner exists; the screen is plain sign-in.
    c2 = client(lake, users)
    assert "Setup code" not in c2.get("/signin").text
    r = c2.post("/api/signin", json={"setup_code": CODE, "username": "other", "password": PW}, headers=W)
    assert r.status_code == 400
    assert len(accounts(users).users) == 1


def test_owner_keeps_the_id_old_shots_belong_to(lake, users):
    c = client(lake, users)
    owner(c)
    (acct,) = (a for a, _ in accounts(users).users.values())
    assert acct.user_id == shots.owner_user_id() and acct.role == "owner"


def test_password_is_peppered_argon2id_in_its_own_dataset(lake, users):
    owner(client(lake, users))
    (f,) = (p for p in users.root.glob("users/*__*.parquet"))
    row = pq.read_table(f).to_pylist()[0]
    assert row["password_hash"].startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert PW not in json.dumps(row, default=str)
    assert not list(lake.root.glob("users/*")), "accounts never go to the main lake"
    # Without the pepper the hash doesn't verify: a copy of the dataset alone isn't enough.
    other = Accounts(users, "another-pepper-xxxxxxxx", "s" * 40, CODE)
    with pytest.raises(AccountError):
        other.sign_in("jtouley", PW, "1.2.3.4")


def test_sign_in_messages_dont_say_whether_the_username_exists(lake, users):
    owner(client(lake, users))
    c = client(lake, users)
    bad = c.post("/api/signin", json={"username": "jtouley", "password": "wrong password!"}, headers=W)
    unknown = c.post("/api/signin", json={"username": "nobody", "password": "wrong password!"}, headers=W)
    assert bad.status_code == unknown.status_code == 400
    assert bad.json() == unknown.json()
    ok = c.post("/api/signin", json={"username": " JTouley ", "password": PW}, headers=W)
    assert ok.status_code == 200 and c.get("/").status_code == 200


def test_lockout_after_five_failures_for_a_username_from_one_address_and_twenty_for_an_address(users):
    a = accounts(users)
    a.create_owner(CODE, "jtouley", PW)
    for _ in range(5):
        with pytest.raises(AccountError, match="match"):
            a.sign_in("jtouley", "wrong password!", "1.1.1.1", now=100)
    with pytest.raises(AccountError, match="Too many"):
        a.sign_in("jtouley", PW, "1.1.1.1", now=101)  # even the right password, from that address
    # The owner, elsewhere, isn't locked out by someone else's guesses (#15-5, #16-4).
    assert a.sign_in("jtouley", PW, "2.2.2.2", now=101).username == "jtouley"
    assert a.sign_in("jtouley", PW, "1.1.1.1", now=100 + 15 * 60 + 1).username == "jtouley"
    for i in range(20):
        with pytest.raises(AccountError, match="match"):
            a.sign_in(f"guess{i}", "wrong password!", "3.3.3.3", now=2000)
    with pytest.raises(AccountError, match="Too many"):
        a.sign_in("jtouley", PW, "3.3.3.3", now=2001)


def test_password_rules():
    for pw in ("short", "1234567890", "aaaaaaaaaaaa", "jtouley-espresso"):
        with pytest.raises(AccountError):
            check_password(pw, "jtouley")
    check_password("kettle orbit marble", "jtouley")


def test_owner_setup_validates_username(users):
    a = accounts(users)
    with pytest.raises(AccountError, match="lower-case"):
        a.create_owner(CODE, "J Touley!", PW)


# --- sessions ------------------------------------------------------------------------------------------


def test_cookie_flags(lake, users):
    c = client(lake, users)
    r = c.post("/api/signin", json={"setup_code": CODE, "username": "jtouley", "password": PW}, headers=W)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=7776000"):
        assert flag in cookie
    assert "Domain" not in cookie  # host-only


def test_tampered_or_expired_cookie_is_signed_out(lake, users):
    c = client(lake, users)
    owner(c)
    token = c.cookies[COOKIE]
    body, sig = token.split(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=="))
    forged = base64.urlsafe_b64encode(json.dumps(data | {"u": "someone"}).encode()).rstrip(b"=").decode()
    a = accounts(users)
    assert a.session(f"{forged}.{sig}") is None
    assert a.session(token) is not None
    assert a.session(token, now=data["t"] + 91 * 86400) is None
    assert a.session("garbage") is None and a.session(None) is None


def test_cookie_renews_after_a_day(lake, users):
    a = accounts(users)
    acct = a.create_owner(CODE, "jtouley", PW)
    old = a.issue(acct, now=datetime.now(UTC).timestamp() - 2 * 86400)
    c = TestClient(create_app(lake, Sessions(a)), base_url="https://testserver")
    c.cookies.set(COOKIE, old)
    r = c.get("/setup")
    assert r.status_code == 200 and f"{COOKIE}=" in r.headers.get("set-cookie", "")
    fresh = a.issue(acct)
    c.cookies.set(COOKIE, fresh)
    assert "set-cookie" not in c.get("/setup").headers


def test_sign_out_everywhere_and_password_change_end_other_sessions(lake, users):
    a = accounts(users)
    app = create_app(lake, Sessions(a))
    phone, laptop = (TestClient(app, base_url="https://testserver", follow_redirects=False) for _ in "ab")
    phone.post("/api/signin", json={"setup_code": CODE, "username": "jtouley", "password": PW}, headers=W)
    laptop.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W)
    new = "a different long password"
    r = phone.post("/api/password", json={"current": "wrong one here", "new": new}, headers=W)
    assert r.status_code == 400
    assert phone.post("/api/password", json={"current": PW, "new": new}, headers=W).status_code == 200
    assert phone.get("/setup").status_code == 200  # this phone got a new cookie
    assert laptop.get("/setup").status_code == 303  # the other one is signed out
    assert (
        laptop.post("/api/signin", json={"username": "jtouley", "password": new}, headers=W).status_code
        == 200
    )
    assert phone.post("/api/signout-everywhere", headers=W).status_code == 200
    assert phone.get("/").status_code == 303 and laptop.get("/").status_code == 303


def test_sign_out_clears_the_cookie(lake, users):
    c = client(lake, users)
    owner(c)
    r = c.post("/api/signout", headers=W)
    assert r.status_code == 200 and 'session=""' in r.headers["set-cookie"]
    assert c.get("/").status_code == 303


def test_accounts_survive_a_restart(lake, users):
    owner(client(lake, users))
    c = client(lake, users)  # a new process: loaded from the users lake
    assert c.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W).status_code == 200


def test_disabled_account_cannot_sign_in_and_its_cookie_stops_working(lake, users):
    a = accounts(users)
    a.create_owner(CODE, "jtouley", PW)
    member = a._write(Account("u2", "maria", a._hash(PW)))
    token = a.issue(member)
    a._write(Account("u2", "maria", member.password_hash, disabled=True))
    assert a.session(token) is None
    with pytest.raises(AccountError):
        a.sign_in("maria", PW, "1.1.1.1")


# --- CSRF and scoping ----------------------------------------------------------------------------------


def test_writes_need_the_header_and_a_same_origin_request(lake, users):
    c = client(lake, users)
    owner(c)
    body = {"equipment_id": "b1", "kind": "basket", "name": "Pullman", "basket_dose_g": 18}
    assert c.post("/api/equipment", json=body).status_code == 403
    cross = W | {"Origin": "https://evil.example"}
    assert c.post("/api/equipment", json=body, headers=cross).status_code == 403
    assert (
        c.post("/api/equipment", json=body, headers=W | {"Sec-Fetch-Site": "cross-site"}).status_code == 403
    )
    same = W | {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}
    assert c.post("/api/equipment", json=body, headers=same).status_code == 200
    # Signing in is a write too (login CSRF).
    r = c.post(
        "/api/signin",
        json={"username": "jtouley", "password": PW},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_signed_out_api_is_401_and_screens_redirect(lake, users):
    c = client(lake, users)
    for path in ("/", "/log", "/setup", "/bag/new", "/saved/x"):
        assert c.get(path).headers["location"] == "/signin"
    assert c.post("/api/shots", json={}, headers=W).status_code == 401


def test_two_real_accounts_cant_see_each_others_shots(lake, users):
    a = accounts(users)
    app = create_app(lake, Sessions(a))
    own = TestClient(app, base_url="https://testserver", follow_redirects=False)
    own.post("/api/signin", json={"setup_code": CODE, "username": "jtouley", "password": PW}, headers=W)
    a._write(Account("u2", "maria", a._hash(PW)))
    other = TestClient(app, base_url="https://testserver", follow_redirects=False)
    other.post("/api/signin", json={"username": "maria", "password": PW}, headers=W)
    own.post(
        "/api/equipment",
        json={"equipment_id": "b1", "kind": "basket", "name": "P", "basket_dose_g": 18},
        headers=W,
    )
    shot = {"shot_id": "s1", "brewed_at": "2026-10-27T07:00:00Z", "coffee_freetext": "Geometry",
            "dose_g": 18, "yield_g": 36, "time_s": 28, "rating": 4, "grinder_setting": 11}  # fmt: skip
    assert own.post("/api/shots", json=shot, headers=W).status_code == 200
    assert own.get("/saved/s1").status_code == 200
    assert other.get("/saved/s1", headers={"Accept": "text/html"}).status_code == 404
    assert other.post("/api/shots/s1/retract", headers=W).status_code == 404
    assert other.post("/api/shots", json=shot | {"rating": 1}, headers=W).status_code == 404
    assert other.post("/api/equipment", json={"equipment_id": "b1", "kind": "basket", "name": "Mine", "basket_dose_g": 20},
                      headers=W).status_code == 404  # fmt: skip
    # The user comes from the cookie; a user_id in the body is ignored.
    r = other.post("/api/shots", json=shot | {"shot_id": "s2", "user_id": shots.owner_user_id()}, headers=W)
    assert r.status_code == 200
    assert app.state.store.shot("u2", "s2").user_id == "u2"


# --- headers, static files, the shell ------------------------------------------------------------------


def test_security_headers(lake, users):
    c = client(lake, users)
    r = c.get("/signin")
    h = r.headers
    assert "script-src 'self'" in h["content-security-policy"]
    assert "unsafe-inline" not in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["strict-transport-security"].startswith("max-age=")
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin"
    assert "camera=()" in h["permissions-policy"]
    owner(c)
    assert "camera=(self)" in c.get("/bag/new").headers["permissions-policy"]
    assert "camera=()" in c.get("/setup").headers["permissions-policy"]
    assert c.post("/api/signout", headers=W).headers["cache-control"] == "no-store"


def test_no_inline_script_or_style(lake, users):
    c = client(lake, users)
    owner(c)
    for path in ("/signin", "/", "/setup", "/bag/new", "/offline"):
        html = c.get(path).text
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag or 'type="application/json"' in tag, (path, tag)
        assert " style=" not in html, path


def test_static_files_are_hashed_compressed_and_cached_for_a_year(lake, users):
    c = client(lake, users)
    html = c.get("/signin").text
    css = re.search(r'href="(/static/app\.[0-9a-f]{10}\.css)"', html).group(1)
    r = c.get(css, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert re.search(r"url\(/static/fonts/archivo\.[0-9a-f]{10}\.woff2\)", r.text)
    assert 'rel="preload"' in html and "font/woff2" in html
    assert c.get("/static/app.css").status_code == 404
    assert c.get("/static/../main.py").status_code == 404


def test_manifest_and_service_worker(lake, users):
    c = client(lake, users)
    m = c.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and m["start_url"] == "/"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i["purpose"] == "maskable" for i in m["icons"])
    for i in m["icons"]:
        assert c.get(i["src"]).headers["content-type"] == "image/png"
    sw = c.get("/sw.js")
    assert sw.headers["cache-control"] == "no-cache" and "__SHELL__" not in sw.text
    for url in json.loads(re.search(r"const SHELL = (\[.*?\]);", sw.text).group(1)):
        assert c.get(url).status_code == 200, url


def test_large_bodies_are_refused(lake, users):
    c = client(lake, users)
    r = c.post("/api/signin", content=b"x" * (65 * 1024), headers=W | {"Content-Type": "application/json"})
    assert r.status_code == 413


def test_error_pages_are_screens(lake, users):
    c = client(lake, users)
    owner(c)
    r = c.get("/saved/nope", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "Nothing here" in r.text


# --- the cache starts from the fct_shot export ---------------------------------------------------------


def test_cache_starts_from_fct_shot_plus_newer_files(lake, users, monkeypatch):
    from app.store import Store

    old = datetime(2026, 10, 1, tzinfo=UTC)
    base = {"brewed_at": old, "dose_g": 18.0, "yield_g": 36.0, "time_s": 28.0, "rating": 4,
            "coffee_freetext": "Geometry", "user_id": None, "grinder_setting": 11.0}  # fmt: skip
    # The export knows s1 and s2, as of `old`.
    lake.root.joinpath("marts").mkdir(parents=True, exist_ok=True)
    con = lake.connect()
    con.execute("create table t (shot_id varchar, logged_at timestamptz, brewed_at timestamptz, dose_g double,"
                " yield_g double, time_s double, rating int, coffee_freetext varchar, user_id varchar,"
                " grinder_setting double, brew_ratio double, coffee_id varchar, roast_date date,"
                " shot_coffee_id varchar, shot_coffee_freetext varchar, shot_roast_date date)")  # fmt: skip
    for sid in ("s1", "s2"):
        # fct_shot fills coffee_id and roast_date from the bag; the shot's own values are the shot_* columns.
        con.execute("insert into t values (?, ?, ?, 18, 36, 28, 4, 'Geometry', 'owner', 11, 2.0, 'bag:coffee',"
                    " '2026-09-20', null, 'Geometry', null)", [sid, old, old])  # fmt: skip
    lake.write_mart("fct_shot", con, "select * from t")
    # An old file the export already covers is never read; newer files are: s2 retracted, s3 new.
    lake.write("shots", f"shots/s1__{old - timedelta(days=2):%Y%m%dT%H%M%S}.parquet",
               [base | {"shot_id": "s1", "logged_at": old - timedelta(days=2), "time_s": 99.0}])  # fmt: skip
    # Made before the export but uploaded after it (#15-1): not in the export, and still found.
    early = old - timedelta(hours=1)
    lake.write("shots", f"shots/s4__{early:%Y%m%dT%H%M%S}.parquet",
               [base | {"shot_id": "s4", "logged_at": early, "time_s": 30.0}])  # fmt: skip
    later = old + timedelta(hours=1)
    lake.write("shots", f"shots/s2__{later:%Y%m%dT%H%M%S}.parquet",
               [base | {"shot_id": "s2", "logged_at": later, "deleted": True}])  # fmt: skip
    lake.write("shots", f"shots/s3__{later:%Y%m%dT%H%M%S}.parquet",
               [base | {"shot_id": "s3", "logged_at": later, "time_s": 31.0}])  # fmt: skip
    read: list[list[str]] = []
    real = lake.files_after
    monkeypatch.setattr(lake, "files_after", lambda t, a: read.append(real(t, a)) or read[-1])
    store = Store(lake, "owner")
    assert sorted(s.shot_id for s in store.shots_of("owner")) == ["s1", "s3", "s4"]
    assert store.shot("owner", "s1").time_s == 28  # from the export, not the older file
    # The shot as it was written, without the bag's values fct_shot filled in.
    assert store.shot("owner", "s1").coffee_id is None and store.shot("owner", "s1").roast_date is None
    assert [p.rsplit("/", 1)[-1] for p in read[0]] == sorted(
        [
            f"s2__{later:%Y%m%dT%H%M%S}.parquet",
            f"s3__{later:%Y%m%dT%H%M%S}.parquet",
            f"s4__{early:%Y%m%dT%H%M%S}.parquet",
        ]
    )
    # A failed listing (a 429 from the Hub) falls back to reading every file instead of failing to start (#15-7).
    monkeypatch.setattr(lake, "files_after", lambda t, a: (_ for _ in ()).throw(OSError("429")))
    assert sorted(s.shot_id for s in Store(lake, "owner").shots_of("owner")) == ["s1", "s3", "s4"]


def test_cache_reads_every_file_without_an_export(lake):
    from app.store import Store

    t = datetime(2026, 10, 1, tzinfo=UTC)
    lake.write("shots", f"shots/s1__{t:%Y%m%dT%H%M%S}.parquet",
               [{"shot_id": "s1", "logged_at": t, "brewed_at": t, "dose_g": 18.0, "yield_g": 36.0,
                 "time_s": 28.0, "rating": 4, "coffee_freetext": "x", "grinder_setting": 11.0}])  # fmt: skip
    assert [s.shot_id for s in Store(lake, "owner").shots_of("owner")] == ["s1"]


def test_a_space_without_its_secrets_says_which_are_missing(lake, monkeypatch):
    monkeypatch.setenv("SPACE_ID", "jtouley/coffee-data-app")
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    for k in ("AUTH_PEPPER", "OWNER_SETUP_CODE"):
        monkeypatch.delenv(k, raising=False)
    r = TestClient(create_app(lake)).get("/")
    assert r.status_code == 503
    assert "deploy again: AUTH_PEPPER." in r.text and "s" * 40 not in r.text  # OWNER_SETUP_CODE is optional
    assert r.headers["x-content-type-options"] == "nosniff"


# --- regressions from the review of #15 ----------------------------------------------------------------


def _aged(lake, users, days: float = 2) -> TestClient:
    """Signed in with a cookie old enough to be renewed on the next request."""
    a = accounts(users)
    acct = a.by_name("jtouley") or a.create_owner(CODE, "jtouley", PW)
    c = TestClient(create_app(lake, Sessions(a)), base_url="https://testserver", follow_redirects=False)
    c.cookies.set(COOKIE, a.issue(acct, now=datetime.now(UTC).timestamp() - days * 86400))
    return c


@pytest.mark.parametrize("path", ["/api/signout", "/api/signout-everywhere"])
def test_sign_out_sticks_when_the_cookie_was_due_for_renewal(lake, users, path):
    c = _aged(lake, users)
    r = c.post(path, headers=W)
    assert r.status_code == 200
    sessions = [v for v in r.headers.get_list("set-cookie") if v.startswith(f"{COOKIE}=")]
    assert len(sessions) == 1 and sessions[0].startswith(f'{COOKIE}=""')  # cleared, not renewed after


def test_password_change_keeps_this_phone_signed_in_with_an_old_cookie(lake, users):
    c = _aged(lake, users)
    r = c.post("/api/password", json={"current": PW, "new": "a different long password"}, headers=W)
    assert r.status_code == 200
    assert c.get("/setup").status_code == 200


def test_parallel_guesses_are_counted_before_the_slow_check(users, monkeypatch):
    import threading

    a = accounts(users)
    a.create_owner(CODE, "jtouley", PW)
    checked = []
    real = a._verify

    def slow(h, p):
        checked.append(p)
        threading.Event().wait(0.05)
        return real(h, p)

    monkeypatch.setattr(a, "_verify", slow)

    def guess(i):
        with contextlib.suppress(AccountError):
            a.sign_in("jtouley", f"guess number {i}", f"10.0.0.{i}", now=500)

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checked) == 10  # the username's backoff starts after 10, not 40


def test_setup_code_guesses_are_limited(users):
    a = accounts(users)
    for i in range(20):
        with pytest.raises(AccountError, match="isn’t right"):
            a.create_owner(f"guess-{i}", "jtouley", PW, ip="7.7.7.7", now=10)
    with pytest.raises(AccountError, match="Too many"):
        a.create_owner(CODE, "jtouley", PW, ip="7.7.7.7", now=11)
    assert a.create_owner(CODE, "jtouley", PW, ip="8.8.8.8", now=11).role == "owner"


def test_limiter_forgets_old_keys(monkeypatch):
    from app.accounts import Limiter

    lim = Limiter()
    monkeypatch.setattr(Limiter, "SWEEP_AT", 100)
    for i in range(150):
        lim.fail(f"ip:{i}", now=0)
    lim.fail("ip:new", now=10_000)
    assert len(lim.fails) <= 2


def test_client_address_comes_from_the_proxy_not_the_client():
    from starlette.requests import Request

    from app.web import client_ip

    req = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")],
            "client": ("10.0.0.2", 1),
        }
    )
    assert client_ip(req, 1) == "203.0.113.9"  # the entry the proxy appended, not the spoofed one
    assert client_ip(req, 0) == "10.0.0.2"
    bare = Request({"type": "http", "headers": [], "client": ("10.0.0.2", 1)})
    assert client_ip(bare, 1) == "10.0.0.2"


def test_body_limit_counts_chunked_bodies_and_rejects_bad_lengths(lake, users):
    c = client(lake, users)

    def chunks():
        for _ in range(70):
            yield b"x" * 1024

    r = c.post("/api/signin", content=chunks(), headers=W | {"Content-Type": "application/json"})
    assert r.status_code == 413 and r.headers["x-content-type-options"] == "nosniff"
    r = c.post(
        "/api/signin",
        content=b"{}",
        headers=W | {"Content-Type": "application/json", "Content-Length": "abc"},
    )
    assert r.status_code == 400


def test_server_errors_get_the_security_headers(lake, users):
    app = create_app(lake, Sessions(accounts(users)))

    def boom() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/boom", boom)
    c = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    r = c.get("/boom", headers={"Accept": "text/html"})
    assert r.status_code == 500 and "Something went wrong" in r.text
    assert r.headers["x-content-type-options"] == "nosniff" and "content-security-policy" in r.headers
    assert c.get("/boom").json() == {"detail": "Something went wrong on the server."}


def test_guesses_spread_over_addresses_back_off_per_username(users):
    from app.accounts import BACKOFF_AFTER

    a = accounts(users)
    a.create_owner(CODE, "jtouley", PW)
    for i in range(BACKOFF_AFTER):
        with pytest.raises(AccountError, match="match"):
            a.sign_in("jtouley", "wrong password!", f"9.9.9.{i}", now=100)
    with pytest.raises(AccountError, match="Wait 3 seconds"):
        a.sign_in("jtouley", PW, "1.2.3.4", now=100)  # the owner waits a moment, never 15 minutes
    assert a.sign_in("jtouley", PW, "1.2.3.4", now=103).username == "jtouley"
