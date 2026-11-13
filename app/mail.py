"""Sending the app's emails (docs/app/signup-ux-spec.md §5): verify, reset and password-changed.

`Mailer` is the one interface: `send(to, subject, text, html)`. `ResendMailer` sends through Resend's HTTP API with
RESEND_API_KEY; `FakeMailer` keeps the messages in memory (tests, and local runs without a key), so nothing in the
tests or on a laptop ever reaches a real inbox. `Capped` holds every sender under the provider's free daily limit.

Logs never carry an email address or a link: only what kind of mail it was and the user id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from .limits import Windows

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates" / "email"
RESEND_URL = "https://api.resend.com/emails"
SENDER = "registry.coffee <hello@send.registry.coffee>"  # EMAIL_FROM overrides it
DAILY_CAP = 90  # under Resend's free 100 a day, with room for a manual test


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str, html: str) -> None:
        """Send one message, or raise."""
        ...


@dataclass(frozen=True)
class Sent:
    to: str
    subject: str
    text: str
    html: str


class FakeMailer:
    """Keeps every message instead of sending it. Never calls out."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.lock = threading.Lock()

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        with self.lock:
            self.sent.append(Sent(to, subject, text, html))

    def to(self, address: str) -> list[Sent]:
        with self.lock:
            return [m for m in self.sent if m.to == address]


class ResendMailer:
    """One POST per message to Resend's API (https://resend.com/docs/api-reference/emails/send-email), tried up to
    3 times on a 429, a 5xx or a network error, 0.5 and 1.5 s apart (with jitter). Every try carries the same
    Idempotency-Key, so Resend sends a message once however many tries reach it. Open and click tracking are off in
    the Resend domain's settings (RUNNING.md), so the HTML arrives as written.

    All the tries together fit in `deadline` (DEADLINE): no try's timeout runs past it, and no retry starts without
    at least MIN_TRY seconds left. Without it, three 5 s timeouts and the waits could hold one send for over 17 s."""

    TRIES, WAITS = 3, (0.5, 1.5)
    # One full 5 s try and a retry fit; Resend usually answers well inside a second, and a reset link is good for
    # 30 minutes, so someone whose send gave up can ask again. It bounds how long a stuck provider holds a thread.
    DEADLINE, MIN_TRY = 10.0, 1.0

    def __init__(
        self,
        api_key: str,
        sender: str = SENDER,
        reply_to: str | None = None,
        timeout: float = 5,
        sleep: Callable[[float], None] = time.sleep,
        deadline: float = DEADLINE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.api_key, self.sender, self.reply_to, self.timeout, self.sleep = (
            api_key,
            sender,
            reply_to,
            timeout,
            sleep,
        )
        self.deadline, self.clock = deadline, clock

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        body: dict[str, object] = {
            "from": self.sender,
            "to": [to],
            "subject": subject,
            "text": text,
            "html": html,
        }
        if self.reply_to:
            body["reply_to"] = self.reply_to
        headers = {"Authorization": f"Bearer {self.api_key}", "Idempotency-Key": str(uuid.uuid4())}
        end = self.clock() + self.deadline
        for attempt in range(self.TRIES):
            error: Exception
            try:
                timeout = max(self.MIN_TRY, min(self.timeout, end - self.clock()))
                r = httpx.post(RESEND_URL, json=body, headers=headers, timeout=timeout)
            except httpx.TransportError as e:
                error = e
            else:
                if r.status_code < 300:
                    return
                error = RuntimeError(f"Resend answered {r.status_code}")  # the body may echo the address
                if r.status_code != 429 and r.status_code < 500:
                    raise error
            if attempt == self.TRIES - 1:
                raise error
            wait = self.WAITS[attempt] * random.uniform(0.8, 1.2)
            if end - self.clock() - wait < self.MIN_TRY:  # no time left for another try: give up now
                raise error
            self.sleep(wait)


class Capped:
    """At most `cap` messages in any 24 hours, across every kind; past it, nothing is sent (and it says so in the
    log). A send that fails gives its place back. In memory (app/limits.py): each cold start counts afresh, so
    it guards against a runaway loop within an instance's life, and Resend's own daily quota is the hard stop."""

    def __init__(self, inner: Mailer, cap: int = DAILY_CAP, clock: Callable[[], float] = time.time):
        self.inner, self.cap, self.clock = inner, cap, clock
        self.window = Windows()

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        now = self.clock()
        if not self.window.take({"day": (self.cap, 86400)}, now):
            raise CapReached()
        try:
            self.inner.send(to, subject, text, html)
        except Exception:
            self.window.refund("day", now)
            raise


class CapReached(RuntimeError):
    """The day's sending limit is used up."""


_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
    keep_trailing_newline=True,
)
SUBJECTS = {
    "verify": "Confirm your email for registry.coffee",
    "reset": "Reset your registry.coffee password",
    "changed": "Your registry.coffee password was changed",
    "email_changed": "The email on your registry.coffee account changed",
}


def render(kind: str, **ctx: object) -> tuple[str, str, str]:
    """Subject, plain text and HTML for one kind of email."""
    ctx = {"subject": SUBJECTS[kind]} | ctx
    return (
        SUBJECTS[kind],
        _env.get_template(f"{kind}.txt").render(ctx),
        _env.get_template(f"{kind}.html").render(ctx),
    )


class _Timing:
    """How long a send usually takes (a moving average), so a request that deliberately sends nothing can wait about
    as long (pad), and its reply's timing doesn't say which it was."""

    def __init__(self, start: float = 0.4):
        self.typical, self.lock = start, threading.Lock()

    def add(self, seconds: float) -> None:
        with self.lock:
            self.typical = 0.8 * self.typical + 0.2 * min(seconds, 5.0)


timing = _Timing()


def pad() -> None:
    """Wait about as long as a send takes, give or take a fifth."""
    time.sleep(timing.typical * random.uniform(0.8, 1.2))


def deliver(mailer: Mailer | None, kind: str, to: str, user_id: str, **ctx: object) -> bool:
    """Render and send, never raising: a failed send is logged (kind and user id only) and the caller carries on.
    Called inside the request (Cloud Run gives no CPU after the reply), so its time is part of the reply's, unless
    the request starts it with `later`."""
    if mailer is None:
        return False
    started = time.monotonic()
    try:
        mailer.send(to, *render(kind, **ctx))
    except CapReached:
        log.warning(
            "email not sent: the daily limit is reached", extra={"fields": {"kind": kind, "user_id": user_id}}
        )
        return False
    except Exception:  # noqa: BLE001 - a mail outage mustn't break the request that asked for it
        log.exception("email not sent", extra={"fields": {"kind": kind, "user_id": user_id}})
        return False
    timing.add(time.monotonic() - started)
    log.info("email sent", extra={"fields": {"kind": kind, "user_id": user_id}})
    return True


_running: set[asyncio.Task[None]] = set()  # every `later` still going: the loop keeps only a weak reference


def later(kind: str, user_id: str, work: Callable[[str], None]) -> None:
    """Start `work(user_id)` (which sends `kind` of email) in the thread pool and return at once, so the reply that
    asked for it never waits for it, and its timing can't depend on how the send goes. It starts now, not after the
    reply: on Cloud Run (CPU only while a request is open) it runs while the request is still held open, and only
    a send slower than that runs on after the reply. Nothing it raises reaches the caller; it is logged here."""
    task = asyncio.get_running_loop().create_task(run_in_threadpool(work, user_id))
    _running.add(task)
    task.add_done_callback(lambda t: _finished(t, kind, user_id))


def _finished(task: asyncio.Task[None], kind: str, user_id: str) -> None:
    _running.discard(task)
    fields = {"fields": {"kind": kind, "user_id": user_id}}
    if task.cancelled():
        log.warning("email work cancelled", extra=fields)
    elif (e := task.exception()) is not None:
        log.error("email not sent", exc_info=e, extra=fields)


def from_env(hosted: bool) -> Mailer | None:
    """Resend when RESEND_API_KEY is set; on a laptop without one, a FakeMailer (nothing leaves the machine); hosted
    without one, nothing, and the email features stay hidden."""
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if key:
        sender = os.environ.get("EMAIL_FROM") or SENDER
        return Capped(ResendMailer(key, sender, os.environ.get("SUPPORT_EMAIL") or None))
    return None if hosted else Capped(FakeMailer())
