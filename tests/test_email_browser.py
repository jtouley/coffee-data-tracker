"""Email recovery in a real browser (tests/browser/email_flow.js): "Forgot your password?" on sign-in, /forgot by
username and by email (sent, no verified email, empty), the emailed reset link ending on sign-in, joining with an
email and the once-only nudge, verify links (done and expired), Today's add-email prompt, Setup's email row and
marketing box, and the owner's People screen. Each state at 390 px and 1280 px, light and dark, with axe.

The app sends to a FakeMailer; the flow reads it through a test-only route added here, never part of the app.
Skipped without Node and Playwright, except in CI (REQUIRE_BROWSER=1). EMAIL_SCREENSHOTS=<dir> keeps the
screenshots."""

import base64
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from app import mail
from app.accounts import Accounts
from app.auth import Sessions
from app.main import create_app
from coffeedata.lake import USER_TABLES, Lake
from tests.test_app_browser import BROWSER, _playwright, _serve

pytestmark = pytest.mark.skipif(
    not _playwright() and not os.environ.get("REQUIRE_BROWSER"), reason="Node with Playwright not installed"
)
PEPPER, SECRET, SETUP = "browser-test-pepper", "browser-test-session-secret-0123456789", "SETUP-CODE-1234"
KEY = base64.b64encode(b"e" * 32).decode()
OWNER, MAYA, SAM = (
    ("jtouley", "kettle orbit marble"),
    ("maya", "lantern quarry velvet"),
    ("sam", "copper meadow tide"),
)


def _app(root: Path):
    accounts = Accounts(Lake(str(root / "users"), tables=USER_TABLES), PEPPER, SECRET, SETUP, email_key=KEY)
    owner = accounts.create_owner(SETUP, *OWNER, agree=True)
    accounts.set_email(owner.user_id, "owner@example.com", OWNER[1])
    _, code = accounts.invite(owner.user_id, for_label="Maya")
    _, link = accounts.join_sending(
        code, *MAYA, "127.0.0.1", agree=True, email="maya@example.com", marketing=True
    )
    accounts.verify_email(link[1], "127.0.0.1")
    _, code = accounts.invite(owner.user_id, for_label="Sam")
    accounts.join(code, *SAM, "127.0.0.1", agree=True)  # no email
    outbox = mail.FakeMailer()
    app = create_app(Lake(str(root / "lake")), Sessions(accounts, secure=False), mailer=outbox)

    def sent() -> JSONResponse:  # test-only: what the app sent (inside each request), newest last
        return JSONResponse([{"to": m.to, "subject": m.subject, "text": m.text} for m in outbox.sent])

    app.add_api_route("/__test/outbox", sent, methods=["GET"])
    from app.auth import PUBLIC

    assert ("GET", "/__test/outbox") not in PUBLIC  # the flow reads it signed in as the owner
    return app


def test_email_recovery_in_a_browser(tmp_path):
    server = _serve(_app(tmp_path))
    out = os.environ.get("EMAIL_SCREENSHOTS") or "-"
    if out != "-":
        Path(out).mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["node", str(BROWSER / "email_flow.js"), f"http://127.0.0.1:{server[2]}", out],
            capture_output=True, text=True, timeout=600, cwd=BROWSER,
        )  # fmt: skip
    finally:
        server[0].should_exit = True
        server[1].join()
    assert r.returncode == 0, r.stderr[-4000:]
