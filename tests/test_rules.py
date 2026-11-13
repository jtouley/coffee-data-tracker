"""The sign-up rules (app/rules.py, spec §3): every rule and its words, the edges of each, password managers'
passwords, NFC, and every failing field coming back at once. The page's checker is held to the same verdicts in
tests/test_signup_browser.py."""

import json
from pathlib import Path

import pytest

from app import rules
from app.accounts import AccountError, Accounts
from coffeedata.lake import USER_TABLES, Lake

PASSWORDS = json.loads((Path(__file__).parent / "fixtures" / "passwords" / "passwords.json").read_text())


def ids(errors: list[dict]) -> list[str]:
    return [e["rule"] for e in errors]


def test_every_rule_has_its_words():
    assert {k: v[2] for k, v in rules.RULES.items()} == {
        "u.length": "Use 3 to 32 characters.",
        "u.chars": "Use only letters, digits, . - or _.",
        "u.email": "A username isn’t an email address.",
        "u.taken": "That username is taken. Try another.",
        "p.length": "Use at least 10 characters.",
        "p.common": "That’s one of the most common passwords. Pick another.",
        "p.username": "Leave your username out of the password.",
        "p.variety": "Use at least 4 different characters.",
        "terms": "Tick the box to agree to the Terms and Privacy Policy before creating your account.",
        "i.label": "Keep the note to 60 characters.",
        "e.syntax": "That doesn’t look like an email address. Check for typos.",
        "e.required": "Enter your email address.",
        "f.required": "Enter your username or email.",
        "f.no_email": "This account has no verified email, so we can’t send a link. Ask the person who invited you for a "
        "reset link.",
    }
    shown = {k: v[1] for k, v in rules.RULES.items() if v[1]}
    assert shown == {
        "u.length": "3 to 32 characters",
        "u.chars": "Letters, digits, . - or _",
        "p.length": "10 to 256 characters",
        "p.common": "Not a common password",
        "p.username": "Doesn’t contain your username",
        "p.variety": "At least 4 different characters",
    }
    assert len(rules.COMMON) == 47


@pytest.mark.parametrize(
    ("name", "failed"),
    [
        ("maya", []),
        ("Maya", []),  # capitals are accepted and lower-cased
        ("first.last", []),  # dots are allowed now
        ("name.com", []),  # the tester's edit of name@gmail.com
        ("under_score-dash", []),
        ("ab", ["u.length"]),
        ("abc", []),
        ("a" * 32, []),
        ("a" * 33, ["u.length"]),
        ("maya!", ["u.chars"]),
        ("ma ya", ["u.chars"]),
        ("émile", ["u.chars"]),
        ("name@gmail.com", ["u.email", "u.chars"]),
        ("", ["u.length"]),
    ],
)
def test_usernames(name, failed):
    assert ids(rules.check_username(name)) == failed


def test_an_email_gets_a_suggestion_only_when_it_would_pass():
    assert rules.check_username("Maya.Lee+cafe@Gmail.com")[0]["message"] == (
        "A username isn’t an email address. Try maya.leecafe."
    )
    assert rules.suggestion("x@y") is None
    assert rules.check_username("x@y")[0]["message"] == "A username isn’t an email address."
    assert rules.suggestion("a" * 40 + "@b") == "a" * 32
    assert rules.suggestion("maya") is None


def test_usernames_are_stored_trimmed_and_lower_cased():
    assert rules.clean_username("  Maya.Lee ") == "maya.lee"
    assert rules.clean_username("\x1fmaya") == "\x1fmaya"  # what JavaScript's trim() keeps, kept here too


@pytest.mark.parametrize(
    ("password", "failed"),
    [
        ("a" * 3 + "bcdefg", ["p.length"]),  # 9
        ("abcdefghij", ["p.common"]),  # 10, and common
        ("kettle orb", []),  # 10
        ("x" * 255 + "y", ["p.variety"]),  # 256
        ("abcd" * 64 + "e", ["p.length"]),  # 257
        ("aaaaaaaaaa", ["p.common", "p.variety"]),  # every rule that fails, not just the first
        ("aaaaaaaaaaaa", ["p.variety"]),
        ("maya-espresso", ["p.username"]),
        ("MAYA espresso", ["p.username"]),  # whatever the case
        ("short", ["p.length"]),
    ],
)
def test_passwords(password, failed):
    assert ids(rules.check_password(password, "maya")) == failed


def test_length_has_two_messages():
    assert rules.check_password("x1y2" * 65, "")[0]["message"] == "Use at most 256 characters."
    assert rules.check_password("x1y2", "")[0]["message"] == "Use at least 10 characters."


def test_password_managers_passwords_pass_from_ten_characters():
    assert len(PASSWORDS) >= 120
    kinds = {p["kind"] for p in PASSWORDS}
    assert kinds >= {"random", "memorable", "safari", "chrome", "phrase", "emoji"}
    seps = {p["separator"] for p in PASSWORDS if p["kind"] == "memorable"}
    assert seps == {"hyphen", "space", "period", "comma", "underscore", "digit", "digit_or_symbol", "none"}
    symbols = "".join(p["password"] for p in PASSWORDS if p["kind"] == "random" and p["symbols"])
    assert set(symbols) & set("!#%)*+,-.:=>?@]^_}~")
    for p in PASSWORDS:
        failed = ids(rules.check_password(p["password"], "maya"))
        if len(rules.nfc(p["password"])) >= 10:
            assert failed == [], p
        else:
            assert failed == ["p.length"], p


def test_length_counts_code_points_of_the_nfc_form():
    composed, decomposed = "é" * 10, "é" * 10
    assert ids(rules.check_password(composed + "abcd")) == []
    assert ids(rules.check_password(decomposed[:18])) == ["p.length", "p.variety"]  # 9 é once composed
    assert ids(rules.check_password("☕️ espresso 🫘 crema")) == []


def test_invite_notes():
    assert rules.clean_label("  Sam \n from\tthe  café ") == "Sam from the café"
    assert rules.clean_label("bell\x07 ") == "bell"
    assert rules.clean_label("👩‍💻 dev") == "👩‍💻 dev"  # the joiner in an emoji isn't a control character
    assert rules.clean_label("   ") is None
    assert rules.check_label("x" * 60) == []
    assert ids(rules.check_label("x" * 61)) == ["i.label"]


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    return Accounts(Lake(str(tmp_path / "users"), tables=USER_TABLES), "p" * 20, "s" * 40, "SETUP-1")


def test_composed_and_decomposed_passwords_are_the_same_password(accounts):
    accounts.create_owner("SETUP-1", "jtouley", "café crème brûlée", agree=True)
    assert accounts.sign_in("jtouley", "café crème brûlée", "1.1.1.1")
    assert accounts.sign_in("jtouley", "café crème brûlée", "1.1.1.1")


def test_a_hash_made_before_nfc_still_verifies(accounts):
    owner = accounts.create_owner("SETUP-1", "jtouley", "kettle orbit marble", agree=True)
    raw = "café crème brûlée"
    from app.accounts import _hasher

    old = _hasher.hash(accounts._peppered(raw))  # as the app hashed it before normalising
    from dataclasses import replace

    accounts._write(replace(owner, password_hash=old))
    assert accounts.sign_in("jtouley", raw, "1.1.1.1")


def test_every_failing_field_comes_back_at_once(accounts):
    owner = accounts.create_owner("SETUP-1", "jtouley", "kettle orbit marble", agree=True)
    _, code = accounts.invite(owner.user_id)
    with pytest.raises(AccountError) as e:
        accounts.join(code, "Maya@x", "maya", "1.1.1.1")
    assert [(x["field"], x["rule"]) for x in e.value.errors] == [
        ("username", "u.email"), ("username", "u.chars"),
        ("password", "p.length"), ("password", "p.variety"),
        ("agree", "terms"),
    ]  # fmt: skip
    with pytest.raises(AccountError) as e:
        accounts.join("AAAA-BBBB-CCCC-DDDD", "maya", "kettle orbit marble", "1.1.1.1", agree=True)
    assert [(x["field"], x["rule"]) for x in e.value.errors] == [("code", "c.invalid")]
