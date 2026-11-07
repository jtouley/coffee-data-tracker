"""Default deny: every registered route is either on auth.PUBLIC or refuses someone signed out, any failure while
working out who is asking counts as signed out, and nothing for a signed-in person can be kept by a cache."""

import re
import time

import pytest
from fastapi import WebSocket
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from app import photos
from app.accounts import Accounts
from app.auth import PUBLIC, LocalOwner, Sessions
from app.main import create_app
from coffeedata.lake import USER_TABLES, Lake
from tests.test_bags import jpeg

W = {"X-Requested-With": "test", "X-Expected-User": "owner"}
CODE = "K7QD-M2XF-9RPA-T4WE"
PW = "correct horse battery"
SECRET = "s" * 40


def accounts(users: Lake, secret: str = SECRET) -> Accounts:
    return Accounts(users, "pepper-" + "p" * 16, secret, CODE)


@pytest.fixture
def users(tmp_path) -> Lake:
    return Lake(str(tmp_path / "users"), tables=USER_TABLES)


@pytest.fixture
def photo_lake(tmp_path) -> Lake:
    return Lake(str(tmp_path / "photos"), tables=("photos",))


def make(lake, users, photo_lake, auth=None):
    return create_app(lake, auth or Sessions(accounts(users)), photos_lake=photo_lake)


def client(app) -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def sign_in(c: TestClient) -> None:
    body = {"agree": True, "setup_code": CODE, "username": "jtouley", "password": PW}
    assert c.post("/api/signin", json=body, headers=W).status_code == 200


def routes(app) -> list[tuple[str, str]]:
    out = []
    for r in app.routes:
        assert isinstance(r, APIRoute), f"{r!r}: only API routes are expected; check it against PUBLIC"
        out += [(m, r.path) for m in sorted(r.methods)]
    return out


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x1", path)


def refused(r) -> bool:
    return r.status_code == 401 or (r.status_code == 303 and r.headers["location"] == "/signin")


def test_every_route_is_public_by_name_or_refuses_anyone_signed_out(lake, users, photo_lake):
    app = make(lake, users, photo_lake)
    c = client(app)
    sign_in(client(app))  # an owner exists, so the screens have someone to show
    found = routes(app)
    assert set(found) >= PUBLIC, f"PUBLIC names routes that don't exist: {PUBLIC - set(found)}"
    # Opening a route to everyone is a decision: change this list with PUBLIC, in the same review.
    assert {p for _, p in PUBLIC} == {"/static/{path:path}", "/sw.js", "/manifest.webmanifest", "/robots.txt",
                                      "/health", "/healthz", "/offline", "/signin", "/api/signin", "/join",
                                      "/api/invites/check", "/api/join", "/reset", "/api/reset"}  # fmt: skip
    private = [(m, p) for m, p in found if (m, p) not in PUBLIC]
    assert len(private) > 30
    for method, path in private:
        for headers in ({}, {"Accept": "text/html"}, W | {"Content-Type": "application/json"}):
            r = c.request(
                method, concrete(path), headers=headers, content=b"{}" if method == "POST" else None
            )
            assert refused(r), (method, path, r.status_code)
            if method == "POST" or path.startswith("/api/"):
                assert r.status_code == 401, (method, path)
            assert r.headers["cache-control"] == "no-store, private"


def test_routes_added_later_are_private_too(lake, users, photo_lake):
    app = make(lake, users, photo_lake)
    app.add_api_route("/later", lambda: {"secret": 1})
    app.mount("/files", app=lambda scope, receive, send: None)
    c = client(app)
    assert c.get("/later").headers["location"] == "/signin"
    assert c.get("/files/anything").headers["location"] == "/signin"
    assert any(isinstance(r, Mount) for r in app.routes)
    sign_in(c)
    assert c.get("/later").json() == {"secret": 1}


def test_websockets_need_a_session_too(lake, users, photo_lake):
    app = make(lake, users, photo_lake)

    async def echo(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("hello")
        await ws.close()

    app.add_api_websocket_route("/ws", echo)
    c = client(app)
    with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect("/ws"):
        pass
    assert closed.value.code == 1008
    sign_in(c)
    cookie = {
        "cookie": f"session={c.cookies['session']}"
    }  # the test client's ws:// wouldn't send a Secure one
    with c.websocket_connect("/ws", headers=cookie) as ws:
        assert ws.receive_text() == "hello"


def test_requests_let_through_are_logged_with_their_user(lake, users, photo_lake, caplog):
    app = make(lake, users, photo_lake)
    app.add_api_route("/later", lambda: {"ok": True})  # no Me dependency: only the middleware knows the user
    c = client(app)
    sign_in(c)
    caplog.set_level("INFO", logger="app.access")
    c.get("/later")
    line = next(r for r in caplog.records if r.name == "app.access" and r.fields["route"] == "/later")
    assert line.fields["user_id"] == "owner"


def test_public_routes_answer_signed_out(lake, users, photo_lake):
    c = client(make(lake, users, photo_lake))
    for path in ("/health", "/healthz", "/signin", "/join", "/reset", "/offline", "/sw.js", "/robots.txt",
                 "/manifest.webmanifest"):  # fmt: skip
        assert c.get(path).status_code == 200, path
        assert c.head(path).status_code in (200, 405), path
    css = re.search(r'href="(/static/app\.[0-9a-f]{10}\.css)"', c.get("/signin").text).group(1)
    assert c.get(css).headers["cache-control"] == "public, max-age=31536000, immutable"


def test_a_signed_out_upload_is_refused_before_its_body_is_read(lake, users, photo_lake):
    c = client(make(lake, users, photo_lake))
    r = c.post(
        "/api/photos/p1", content=b"x" * (photos.MAX_UPLOAD + 1), headers=W | {"Content-Type": "image/jpeg"}
    )
    assert r.status_code == 401


class Broken(Sessions):
    """A sign-in whose lookup fails, as an unreadable secret or a failed read would."""

    broken = False

    def user(self, request):
        if self.broken:
            raise OSError("lake read failed")
        return super().user(request)


def test_a_failing_session_lookup_fails_closed(lake, users, photo_lake, caplog):
    auth = Broken(accounts(users))
    app = make(lake, users, photo_lake, auth)
    c = client(app)
    sign_in(c)
    assert c.get("/").status_code == 200
    auth.broken = True
    for path in ("/", "/setup", "/invite", "/photos/p1", "/coffees"):
        assert c.get(path).headers["location"] == "/signin", path
    assert c.get("/api/coffees/search?q=x").status_code == 401
    assert c.post("/api/shots", json={}, headers=W).status_code == 401
    r = c.get("/signin")  # the way back in still opens, and says why
    assert r.status_code == 200 and "couldn’t check your sign-in just now" in r.text
    assert "set-cookie" not in r.headers  # kept: it may be fine once the check works again
    assert c.get("/health").json() == {"ok": True}
    assert "lake read failed" in caplog.text and "Traceback" in caplog.text  # logged, not shown


def test_a_failing_terms_check_shows_only_the_agree_screen(lake, users, photo_lake, monkeypatch):
    acc = accounts(users)
    c = client(make(lake, users, photo_lake, Sessions(acc)))
    sign_in(c)

    def boom(user_id):
        raise ValueError("bad consent record")

    monkeypatch.setattr(acc, "needs_agree", boom)
    assert c.get("/").headers["location"] == "/agree"
    assert c.get("/agree").status_code == 200  # no loop back to /
    assert c.post("/api/shots", json={}, headers=W).status_code == 403


def test_expired_and_foreign_sessions_go_to_sign_in(lake, users, photo_lake):
    acc = accounts(users)
    app = make(lake, users, photo_lake, Sessions(acc))
    sign_in(client(app))
    owner = acc.by_name("jtouley")
    old = acc.issue(owner, now=time.time() - 91 * 86400)
    other = accounts(users, secret="t" * 40).issue(owner)  # signed with another (a rotated) secret
    for token in (old, other, "garbage", "a.b"):
        c = client(app)
        c.cookies.set("session", token, domain="testserver.local")  # where the jar keeps the server's cookies
        assert c.get("/").headers["location"] == "/signin"
        assert c.post("/api/shots", json={}, headers=W).status_code == 401
        r = c.get("/signin")
        assert r.status_code == 200 and "You’ve been signed out. Sign in again" in r.text
        assert "set-cookie" not in r.headers  # left alone: signing in replaces it
    assert "signed out" not in client(app).get("/signin").text  # never signed in: no notice


def test_any_response_carrying_a_session_cookie_is_private(lake, users, photo_lake):
    acc = accounts(users)
    app = make(lake, users, photo_lake, Sessions(acc))
    c = client(app)
    body = {"agree": True, "setup_code": CODE, "username": "jtouley", "password": PW}
    r = c.post("/api/signin", json=body, headers=W)  # a public route that signs someone in
    assert "session=" in r.headers["set-cookie"] and r.headers["cache-control"] == "no-store, private"
    # A cookie due for renewal, on a public route that looks at it (/signin sends the signed-in home).
    due = client(app)
    due.cookies.set("session", acc.issue(acc.by_name("jtouley"), now=time.time() - 2 * 86400))
    r = due.get("/signin")
    assert r.headers["location"] == "/" and "session=" in r.headers["set-cookie"]
    assert r.headers["cache-control"] == "no-store, private" and "Cookie" in r.headers["vary"]


def test_photos_and_signed_in_pages_are_never_stored(lake, users, photo_lake):
    c = client(make(lake, users, photo_lake))
    sign_in(c)
    photos.save(photo_lake, "p1", "owner", photos.normalize(jpeg()))
    for path in ("/photos/p1", "/", "/setup", "/invite", "/api/coffees/search?q=x", "/photos/missing"):
        r = c.get(path)
        assert r.headers["cache-control"] == "no-store, private", path
        assert "cookie" in r.headers["vary"].lower(), path
        assert "etag" not in r.headers and "last-modified" not in r.headers, path
    r = c.get("/photos/p1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (
        c.get("/photos/p1", headers={"Accept-Encoding": "gzip"}).headers["vary"].lower().count("cookie") == 1
    )


def test_the_stub_must_be_asked_for(monkeypatch, tmp_path):
    for k in ("SESSION_SECRET", "AUTH_PEPPER", "K_SERVICE", "SPACE_ID", "COFFEEDATA_LOCAL_OWNER"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        LocalOwner()
    c = TestClient(create_app(lake=Lake(str(tmp_path / "lake"))))
    for path in ("/", "/health", "/photos/p1"):
        r = c.get(path)
        assert r.status_code == 503 and "SESSION_SECRET" in r.text, path
    monkeypatch.setenv("COFFEEDATA_LOCAL_OWNER", "1")
    assert TestClient(create_app(lake=Lake(str(tmp_path / "lake")))).get("/setup").status_code == 200


def test_insecure_cookies_are_refused_when_hosted(monkeypatch, tmp_path):
    monkeypatch.setenv("K_SERVICE", "registry-coffee")
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("AUTH_PEPPER", "p" * 20)
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("COFFEEDATA_INSECURE_COOKIES", "1")
    monkeypatch.setenv("COFFEEDATA_USERS", str(tmp_path / "users"))
    r = TestClient(create_app(lake=Lake(str(tmp_path / "lake")))).get("/signin")
    assert r.status_code == 503 and "COFFEEDATA_INSECURE_COOKIES" in r.text
    monkeypatch.setenv("COFFEEDATA_INSECURE_COOKIES", "0")  # only "1" turns it on, so only "1" is refused
    c = TestClient(create_app(lake=Lake(str(tmp_path / "lake"))), base_url="https://testserver")
    assert c.get("/signin").status_code == 200
