"""The sign-up rules, as data, in one place (docs/app/signup-ux-spec.md §3).

The server checks with `check_username` and `check_password`; the page gets the same data as a JSON block
(`<script type="application/json" id="rules">`, see `for_page`) and `app.js` evaluates it the same way, so both sides
give the same verdicts in the same words. `tests/test_rules.py` holds them to that.

Each check returns every failing rule, not just the first, as `{"field", "rule", "message"}`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

USERNAME_MIN, USERNAME_MAX = 3, 32
MIN_PASSWORD, MAX_PASSWORD = 10, 256  # the owner's decision on the minimum (spec §2)
MIN_DISTINCT = 4
LABEL_MAX = 60  # an invite's "For" note
USERNAME_CHARS = "a-z0-9._-"  # a regex character class, the same in Python and JavaScript

# Long enough to pass the length rule, and still among the first guesses (NIST 800-63B asks for a check like this).
COMMON = frozenset(
    [
        "1234567890",
        "0123456789",
        "0987654321",
        "1111111111",
        "1234512345",
        "1q2w3e4r5t",
        "1qaz2wsx3edc",
        "123qweasdzxc",
        "qwertyuiop",
        "qwertyuiop1",
        "asdfghjkl1",
        "zxcvbnm123",
        "password12",
        "password123",
        "password1234",
        "passw0rd12",
        "p@ssword123",
        "iloveyou12",
        "iloveyou123",
        "letmein123",
        "welcome123",
        "sunshine12",
        "football12",
        "baseball12",
        "princess12",
        "starwars12",
        "trustno1234",
        "abc1234567",
        "abcdefghij",
        "aaaaaaaaaa",
        "1234554321",
        "1122334455",
        "qazwsxedcr",
        "monkey1234",
        "dragon1234",
        "master1234",
        "superman12",
        "batman1234",
        "michael123",
        "jennifer12",
        "changeme12",
        "administrator",
        "coffeecoffee",
        "espresso12",
        "espresso123",
        "registrycoffee",
        "coffeedata",
    ]
)

# id -> (field, the line shown under the field or None, the error message). The messages are the spec's §3 table,
# in the app's typography (’ for the apostrophe, as everywhere else on screen).
RULES: dict[str, tuple[str, str | None, str]] = {
    "u.length": ("username", f"{USERNAME_MIN} to {USERNAME_MAX} characters", f"Use {USERNAME_MIN} to {USERNAME_MAX} characters."),
    "u.chars": ("username", "Letters, digits, . - or _", "Use only letters, digits, . - or _."),
    "u.email": ("username", None, "A username isn’t an email address."),
    "u.taken": ("username", None, "That username is taken. Try another."),
    "p.length": ("password", f"{MIN_PASSWORD} to {MAX_PASSWORD} characters", f"Use at least {MIN_PASSWORD} characters."),
    "p.common": ("password", "Not a common password", "That’s one of the most common passwords. Pick another."),
    "p.username": ("password", "Doesn’t contain your username", "Leave your username out of the password."),
    "p.variety": ("password", f"At least {MIN_DISTINCT} different characters", f"Use at least {MIN_DISTINCT} different characters."),
    "terms": ("agree", None, "Tick the box to agree to the Terms and Privacy Policy before creating your account."),
    "i.label": ("for_label", None, f"Keep the note to {LABEL_MAX} characters."),
}  # fmt: skip
TOO_LONG = f"Use at most {MAX_PASSWORD} characters."  # p.length's other message
SUGGEST = "A username isn’t an email address. Try {suggestion}."  # u.email, when there's a suggestion


def error(rule: str, message: str | None = None) -> dict[str, str]:
    return {"field": RULES[rule][0], "rule": rule, "message": message or RULES[rule][2]}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


# What JavaScript's String.prototype.trim() removes, so both sides trim the same (Python's str.strip() also takes
# U+001C–U+001F and U+0085, and not U+FEFF).
TRIM = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def clean_username(name: str) -> str:
    """As stored: NFC, trimmed, lower-cased. Capitals are accepted (spec §2)."""
    return nfc(name).strip(TRIM).lower()


_ALLOWED = re.compile(f"[{USERNAME_CHARS}]")
_NOT_ALLOWED = re.compile(f"[^{USERNAME_CHARS}]")


def suggestion(name: str) -> str | None:
    """For a username with `@`: the part before it, lower-cased, without the characters that aren't allowed, cut to
    the maximum, and only when that passes."""
    if "@" not in name:
        return None
    s = _NOT_ALLOWED.sub("", clean_username(name).split("@")[0])[:USERNAME_MAX]
    return s if len(s) >= USERNAME_MIN else None


def check_username(name: str) -> list[dict[str, str]]:
    """Every rule `name` fails, as typed (it's cleaned first). Taken is checked by the caller."""
    u = clean_username(name)
    out = []
    if "@" in u:
        s = suggestion(u)
        out.append(error("u.email", SUGGEST.format(suggestion=s) if s else None))
    if not USERNAME_MIN <= len(u) <= USERNAME_MAX:
        out.append(error("u.length"))
    if any(not _ALLOWED.fullmatch(c) for c in u):
        out.append(error("u.chars"))
    return out


def check_password(password: str, username: str = "") -> list[dict[str, str]]:
    """Every rule the password fails. Any character counts, nothing is trimmed, and length is in code points of the
    NFC form, which is what gets hashed."""
    pw = nfc(password)
    name = clean_username(username)
    out = []
    if len(pw) < MIN_PASSWORD:
        out.append(error("p.length"))
    elif len(pw) > MAX_PASSWORD:
        out.append(error("p.length", TOO_LONG))
    folded = pw.lower()
    if folded in COMMON:
        out.append(error("p.common"))
    if name and name in folded:
        out.append(error("p.username"))
    if len(set(pw)) < MIN_DISTINCT:
        out.append(error("p.variety"))
    return out


def clean_label(label: object) -> str | None:
    """An invite's note: whitespace collapsed, control characters dropped, None when empty. Length is checked by
    `check_label`."""
    if label is None:
        return None
    text = "".join(c for c in str(label) if c.isspace() or unicodedata.category(c) != "Cc")
    text = " ".join(nfc(text).split())
    return text or None


def check_label(label: str | None) -> list[dict[str, str]]:
    return [error("i.label")] if label and len(label) > LABEL_MAX else []


def for_page() -> dict[str, Any]:
    """What the page script needs, as JSON."""
    return {
        "username": {"min": USERNAME_MIN, "max": USERNAME_MAX, "chars": USERNAME_CHARS},
        "password": {"min": MIN_PASSWORD, "max": MAX_PASSWORD, "distinct": MIN_DISTINCT},
        "label_max": LABEL_MAX,
        "rules": {k: {"field": f, "shown": shown, "message": m} for k, (f, shown, m) in RULES.items()},
        "too_long": TOO_LONG,
        "suggest": SUGGEST,
        "common": sorted(COMMON),
    }
