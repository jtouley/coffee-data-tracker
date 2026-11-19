"""/api/forgot's reply never waits for the reset email (app/main.py post_forgot, mail.later), and a send's tries fit
in one deadline (mail.ResendMailer). Like PR #161's floor test, timing is proved with the handler's clock and sleep
replaced, not with wall-clock bounds. Every message goes to a stand-in mailer: nothing here calls out."""

import asyncio
import base64
import logging
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app.main as app_main
from app import mail
from app.accounts import Accounts
from app.auth import Sessions
from app.main import create_app
from coffeedata.lake import USER_TABLES, Lake
from tests.conftest import wait_for_sends

W = {"X-Requested-With": "test", "X-Expected-User": "owner"}
SETUP, PW, MAYA_PW = "SETUP-CODE-1234", "correct horse battery", "kettle orbit marble"
KEY = base64.b64encode(b"k" * 32).decode()
SAME = {"ok": True}
FLOOR = 2.0
# How long a held send waits to be let go. Only so a broken fix fails instead of hanging: with the fix, the test
# lets it go as soon as the reply is back, so a slow runner can't reach this.
GUARD = 60.0


class Held:
    """A mailer whose send waits until the test lets it go, then "takes" 30 s on the handler's clock."""

    def __init__(self, clock: list[float]) -> None:
        self.inner, self.clock = mail.FakeMailer(), clock
        self.go, self.done = threading.Event(), threading.Event()

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        self.go.wait(GUARD)
        self.clock[0] += 30.0
        self.inner.send(to, subject, text, html)
        self.done.set()


class Failing:
    def send(self, to: str, subject: str, text: str, html: str) -> None:
        raise RuntimeError("provider down")


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    return Accounts(
        Lake(str(tmp_path / "users"), tables=USER_TABLES),
        "pepper-" + "p" * 16,
        "s" * 40,
        SETUP,
        email_key=KEY,
    )


def client(app) -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def verify(app, outbox: mail.FakeMailer, address: str) -> None:
    (sent,) = outbox.to(address)
    token = sent.text.split("/verify#", 1)[1].split()[0]
    assert client(app).post("/api/verify", json={"token": token}, headers=W).status_code == 200


def people(app, accounts, outbox: mail.FakeMailer) -> None:
    """The owner and maya (each with a verified address), and sam (an address not yet verified)."""
    owner = client(app)
    body = {"setup_code": SETUP, "username": "jtouley", "password": PW, "agree": True}
    assert owner.post("/api/signin", json=body, headers=W).status_code == 200
    assert (
        owner.post("/api/email", json={"email": "owner@example.com", "current": PW}, headers=W).status_code
        == 200
    )
    verify(app, outbox, "owner@example.com")
    for name, email in (("maya", "maya@example.com"), ("sam", "sam@example.com")):
        code = owner.post("/api/invites", json={}, headers=W).json()["code"]
        body = {"code": code, "username": name, "password": MAYA_PW, "agree": True, "email": email}
        assert client(app).post("/api/join", json=body, headers=W).status_code == 200
    verify(app, outbox, "maya@example.com")
    assert accounts.verified_email(accounts.by_name("jtouley").user_id) == "owner@example.com"
    outbox.sent.clear()


# Every kind of input that gets past the limits, with how long its lookup "takes" on the handler's clock.
INPUTS = {
    "maya": (0.05, 200),  # a verified email: a link is sent
    "maya@example.com": (0.3, 200),  # the same, by address
    "nobody": (0.12, 200),  # no account
    "nobody@example.com": (0.2, 200),
    "jtouley": (0.07, 200),  # the owner never resets by email
    "sam": (0.15, 400),  # no verified email: told so on the form
}


def test_the_reply_takes_the_floor_and_never_waits_for_the_send(lake, accounts, monkeypatch):
    clock = [1000.0]
    held = Held(clock)
    app = create_app(lake, Sessions(accounts), mailer=held)
    held.go.set()  # the setup's verify emails go straight through
    people(app, accounts, held.inner)
    app.state.forgot_floor = FLOOR
    steps: list[tuple] = []
    real_forgot = accounts.forgot

    def timed_forgot(who, ip):
        answer = real_forgot(who, ip)
        steps.append(("forgot", answer[0]))
        clock[0] += INPUTS[who][0]
        return answer

    async def sleep(seconds):
        steps.append(("sleep", round(seconds, 9)))
        await asyncio.sleep(0)

    # One event loop for every request (the client's `with`), so a send can outlive its reply.
    with client(app) as c, monkeypatch.context() as m:
        m.setattr(accounts, "forgot", timed_forgot)
        m.setattr(app_main, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        m.setattr(app_main, "asyncio", SimpleNamespace(sleep=sleep))
        for who, (work, status) in INPUTS.items():
            steps.clear()
            held.go.clear(), held.done.clear()
            before = len(held.inner.sent)
            r = c.post("/api/forgot", json={"account": who}, headers=W)
            assert r.status_code == status, who
            # Every input: the lookup, then one sleep for exactly the rest of the floor, then the reply.
            assert [s[0] for s in steps] == ["forgot", "sleep"], who
            assert steps[1] == ("sleep", round(FLOOR - work, 9)), who
            # The reply is back while the send is still held: it never waited for it.
            assert not held.done.is_set() and len(held.inner.sent) == before, who
            held.go.set()
            wait_for_sends()
            sends = len(held.inner.sent) - before
            assert sends == (1 if who.startswith("maya") else 0), who
    assert [m.to for m in held.inner.sent] == ["maya@example.com", "maya@example.com"]
    assert "/reset#" in held.inner.sent[0].text


def test_a_failed_send_is_logged_and_the_reply_is_the_same(lake, accounts, caplog):
    outbox = mail.FakeMailer()
    people(create_app(lake, Sessions(accounts), mailer=outbox), accounts, outbox)
    # The same accounts, and a mailer that raises.
    failing = create_app(lake, Sessions(accounts), mailer=Failing())
    failing.state.forgot_floor = 0.01
    with caplog.at_level(logging.INFO), client(failing) as c:
        assert c.post("/api/forgot", json={"account": "maya"}, headers=W).json() == SAME
        wait_for_sends()
    assert [r.getMessage() for r in caplog.records if r.name == "app.mail"] == ["email not sent"]


def test_an_error_before_the_send_is_logged_not_lost(lake, accounts, caplog, monkeypatch):
    outbox = mail.FakeMailer()
    app = create_app(lake, Sessions(accounts), mailer=outbox)
    people(app, accounts, outbox)
    app.state.forgot_floor = 0.01

    def broken(user_id):
        raise OSError("lake unavailable")

    monkeypatch.setattr(accounts, "reset_link", broken)
    with caplog.at_level(logging.INFO), client(app) as c:
        assert c.post("/api/forgot", json={"account": "maya"}, headers=W).json() == SAME
        wait_for_sends()
    (record,) = [r for r in caplog.records if r.name == "app.mail"]
    assert (
        record.getMessage() == "email work failed" and record.levelno >= logging.WARNING
    )  # the alert reads it
    assert record.fields == {"kind": "reset", "user_id": accounts.by_name("maya").user_id, "error": "OSError"}
    assert outbox.sent == []


@respx.mock
def test_a_send_gives_up_at_its_deadline_across_tries():
    """Three tries that each time out would take over 17 s; all of them fit in the deadline instead."""
    clock, waits, timeouts = [0.0], [], []

    def slow(request):
        timeouts.append(request.extensions["timeout"]["read"])
        clock[0] += timeouts[-1]  # each try runs to its timeout
        raise httpx.ReadTimeout("slow", request=request)

    respx.post(mail.RESEND_URL).mock(side_effect=slow)

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    m = mail.ResendMailer("re_test", sleep=sleep, clock=lambda: clock[0])
    with pytest.raises(httpx.ReadTimeout):
        m.send("a@example.com", "s", "t", "h")
    assert clock[0] <= mail.ResendMailer.DEADLINE
    assert timeouts[0] == 5 and len(timeouts) == 2 and timeouts[1] < 5  # the retry gets only what's left
    assert len(waits) == 1


@respx.mock
def test_a_quick_failure_still_gets_all_its_tries():
    clock = [0.0]
    route = respx.post(mail.RESEND_URL).mock(return_value=httpx.Response(503))
    m = mail.ResendMailer(
        "re_test", sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0]
    )
    with pytest.raises(RuntimeError, match="503"):
        m.send("a@example.com", "s", "t", "h")
    assert route.call_count == 3
    route.mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"id": "x"})])
    m.send("a@example.com", "s", "t", "h")


class Trickle(httpx.AsyncBaseTransport):
    """A provider that answers 200 and then sends its body one byte every 50 ms, for 5 s."""

    def __init__(self) -> None:
        self.bytes = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(100):
                    transport.bytes += 1
                    yield b" "
                    await asyncio.sleep(0.05)

        return httpx.Response(200, stream=Body())


def test_a_reply_that_trickles_in_is_cut_off_at_the_deadline():
    """Each byte arrives well inside any read timeout, so only a wall-clock bound on the whole try stops it."""
    trickle = Trickle()
    m = mail.ResendMailer("re_test", deadline=0.5, transport=trickle)
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        m.send("a@example.com", "s", "t", "h")
    # Cut off after about 10 of its 100 bytes (no retry: under MIN_TRY was left). The time is only a loose check;
    # the byte count is the proof.
    assert trickle.bytes < 40, trickle.bytes
    assert time.monotonic() - started < 4.0


def test_sends_past_the_cap_are_dropped_and_logged(lake, accounts, caplog, monkeypatch):
    held = Held([0.0])
    app = create_app(lake, Sessions(accounts), mailer=held)
    held.go.set()
    people(app, accounts, held.inner)
    held.go.clear()
    app.state.forgot_floor = 0.01
    monkeypatch.setattr(mail, "IN_FLIGHT", 1)
    with caplog.at_level(logging.INFO), client(app) as c:
        assert c.post("/api/forgot", json={"account": "maya"}, headers=W).json() == SAME  # held, in flight
        assert c.post("/api/forgot", json={"account": "maya"}, headers=W).json() == SAME  # the same reply
        held.go.set()
        wait_for_sends()
    assert len(held.inner.sent) == 1
    dropped = [r for r in caplog.records if r.name == "app.mail" and "too many" in r.getMessage()]
    assert len(dropped) == 1 and dropped[0].levelno >= logging.WARNING
