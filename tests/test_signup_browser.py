"""Sign-up and invites in a real browser (tests/browser/signup_flow.js, spec §12): the rule list as you type, the
"@" suggestion, the terms box, double taps, a slow server, a lost reply, "already used", the in-app browser banner,
named invites, reset links, re-acceptance of new terms, and widths from 320 to 430 px, with axe in light and dark.
Then the page's rule checker against rules.py (tests/browser/rules_parity.js). Skipped without Node and Playwright,
except in CI (REQUIRE_BROWSER=1). SIGNUP_SCREENSHOTS=<dir> keeps the screenshots."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.auth import Sessions
from app.main import create_app
from app.rules import check_password, check_username
from coffeedata.lake import Lake
from tests import signup_seed
from tests.test_app_browser import BROWSER, _playwright, _serve

pytestmark = pytest.mark.skipif(
    not _playwright() and not os.environ.get("REQUIRE_BROWSER"), reason="Node with Playwright not installed"
)
PEPPER, SECRET = "browser-test-pepper", "browser-test-session-secret-0123456789"
PASSWORDS = Path(__file__).parent / "fixtures" / "passwords" / "passwords.json"


def _app(root: Path, legal: str | None = None, publish_delay: float = 0):
    signup_seed.seed(root / "users", PEPPER, SECRET)
    accounts = signup_seed.accounts(root / "users", PEPPER, SECRET, legal)
    if publish_delay:  # a Hugging Face commit that takes a while
        real = accounts.lake.publish

        def slow(*a, **k):
            time.sleep(publish_delay)
            return real(*a, **k)

        accounts.lake.publish = slow  # type: ignore[method-assign]
    return create_app(Lake(str(root / "lake")), Sessions(accounts, secure=False))


def test_signup_in_a_browser(tmp_path):
    apps = [
        _app(tmp_path / "plain"),
        _app(tmp_path / "slow", publish_delay=6),
        _app(tmp_path / "bumped", legal="December 1, 2026"),
    ]
    servers = [_serve(a) for a in apps]
    out = os.environ.get("SIGNUP_SCREENSHOTS") or "-"
    if out != "-":
        Path(out).mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["node", str(BROWSER / "signup_flow.js"), *(f"http://127.0.0.1:{p}" for _, _, p in servers), out,
             str(PASSWORDS)],
            capture_output=True, text=True, timeout=600, cwd=BROWSER,
        )  # fmt: skip
    finally:
        for server, thread, _ in servers:
            server.should_exit = True
            thread.join()
    assert r.returncode == 0, r.stderr[-4000:]


USERNAMES = ["maya", "Maya", "  maya  ", "first.last", "name.com", "name@gmail.com", "Maya.Lee@Gmail.com", "ab", "abc",
             "a" * 32, "a" * 33, "@@", "x@y", "émile", "émile", "maya!", "ma ya", "﻿maya", " maya",
             "\x1fmaya", "Kelvin", "İstanbul", "", "under_score-dash", "a@" + "b" * 40, "😀😀😀"]  # fmt: skip


def test_the_page_and_the_server_give_the_same_verdicts_in_the_same_words(tmp_path):
    passwords = [p["password"] for p in json.loads(PASSWORDS.read_text())]
    passwords += ["short", "1234567890", "Password123", "aaaaaaaaaaaa", "maya-espresso", "x" * 256, "x" * 257,
                  "abcabcabcab", "é" * 5, "café crème", "MAYA is here and fine"]  # fmt: skip
    cases = {"usernames": USERNAMES, "passwords": [[p, u] for p in passwords for u in ("maya", "")]}
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    server = _serve(_app(tmp_path / "parity"))
    try:
        r = subprocess.run(
            ["node", str(BROWSER / "rules_parity.js"), f"http://127.0.0.1:{server[2]}", str(tmp_path / "cases.json")],
            capture_output=True, text=True, timeout=120, cwd=BROWSER,
        )  # fmt: skip
    finally:
        server[0].should_exit = True
        server[1].join()
    assert r.returncode == 0, r.stderr[-3000:]
    page = json.loads(r.stdout)
    assert page["usernames"] == [check_username(u) for u in USERNAMES]
    assert page["passwords"] == [check_password(p, u) for p, u in cases["passwords"]]
