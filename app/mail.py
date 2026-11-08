"""Sending the app's emails (docs/app/signup-ux-spec.md §5): verify, reset and password-changed.

`Mailer` is the one interface: `send(to, subject, text, html)`. `ResendMailer` sends through Resend's HTTP API with
RESEND_API_KEY; `FakeMailer` keeps the messages in memory (tests, and local runs without a key), so nothing in the
tests or on a laptop ever reaches a real inbox. `Capped` holds every sender under the provider's free daily limit.

Logs never carry an email address or a link: only what kind of mail it was and the user id.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

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
    """One POST per message to Resend's API (https://resend.com/docs/api-reference/emails/send-email). Open and
    click tracking are off in the Resend domain's settings (RUNNING.md), so the HTML arrives as written."""

    def __init__(self, api_key: str, sender: str = SENDER, reply_to: str | None = None, timeout: float = 10):
        self.api_key, self.sender, self.reply_to, self.timeout = api_key, sender, reply_to, timeout

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
        r = httpx.post(
            RESEND_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout
        )
        if r.status_code >= 300:
            raise RuntimeError(
                f"Resend answered {r.status_code}"
            )  # the body may echo the address: not logged


class Capped:
    """At most `cap` messages in any 24 hours, across every kind; past it, nothing is sent (and it says so in the
    log). In memory: the app runs as one instance, and a restart only makes the cap more lenient for a day."""

    def __init__(self, inner: Mailer, cap: int = DAILY_CAP, clock: Callable[[], float] = time.time):
        self.inner, self.cap, self.clock = inner, cap, clock
        self.times: deque[float] = deque()
        self.lock = threading.Lock()

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        now = self.clock()
        with self.lock:
            while self.times and self.times[0] <= now - 86400:
                self.times.popleft()
            if len(self.times) >= self.cap:
                raise CapReached()
            self.times.append(now)
        self.inner.send(to, subject, text, html)


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
}


def render(kind: str, **ctx: object) -> tuple[str, str, str]:
    """Subject, plain text and HTML for one kind of email."""
    ctx = {"subject": SUBJECTS[kind]} | ctx
    return (
        SUBJECTS[kind],
        _env.get_template(f"{kind}.txt").render(ctx),
        _env.get_template(f"{kind}.html").render(ctx),
    )


def deliver(mailer: Mailer | None, kind: str, to: str, user_id: str, **ctx: object) -> bool:
    """Render and send, never raising: a failed send is logged (kind and user id only) and the caller carries on.
    Callers run this after the reply has gone, so neither the outcome nor its timing reaches the person asking."""
    if mailer is None:
        return False
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
    log.info("email sent", extra={"fields": {"kind": kind, "user_id": user_id}})
    return True


def from_env(hosted: bool) -> Mailer | None:
    """Resend when RESEND_API_KEY is set; on a laptop without one, a FakeMailer (nothing leaves the machine); hosted
    without one, nothing, and the email features stay hidden."""
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if key:
        sender = os.environ.get("EMAIL_FROM") or SENDER
        return Capped(ResendMailer(key, sender, os.environ.get("SUPPORT_EMAIL") or None))
    return None if hosted else Capped(FakeMailer())
