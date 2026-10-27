"""Accounts: invite-only sign-up, username and password, signed session cookies (PLAN, "Accounts and sign-in").

Users and invites live in their own private dataset (`Lake.users()`), one file per change, latest wins, loaded
into memory at start. Passwords are HMAC-ed with a pepper (AUTH_PEPPER) and then hashed with Argon2id. There's no
session table: the cookie is a signed token carrying the user's id and `session_epoch`, and bumping the epoch
signs that user out everywhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY

from coffeedata import shots
from coffeedata.lake import Lake

log = logging.getLogger(__name__)

USERNAME = re.compile(r"[a-z0-9_-]{3,32}")
MIN_PASSWORD, MAX_PASSWORD = 10, 256
SESSION_DAYS = 90
RENEW_AFTER = timedelta(days=1)  # a cookie used after a day is reissued, so it lasts 90 days from last use
LOCKOUT = 15 * 60
MAX_PER_USERNAME, MAX_PER_IP = 5, 20

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

_hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)  # Argon2id, 64 MiB, 3 passes, 4 lanes


@dataclass(frozen=True)
class Account:
    user_id: str
    username: str
    password_hash: str
    role: str = "member"
    session_epoch: int = 0
    display_name: str | None = None
    invite_id: str | None = None
    disabled: bool = False


class AccountError(ValueError):
    """Something to show the person, as is."""


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def check_password(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Use at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"Use at most {MAX_PASSWORD} characters.")
    folded = password.lower()
    if folded in COMMON or (username and username in folded) or len(set(password)) < 4:
        raise AccountError("That password is too easy to guess. Try a few unrelated words.")


class Limiter:
    """Failed sign-ins per key within LOCKOUT seconds, in memory (a restart clears it)."""

    def __init__(self) -> None:
        self.fails: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self.fails[key]
        while q and q[0] <= now - LOCKOUT:
            q.popleft()
        return q

    def blocked(self, key: str, limit: int, now: float) -> bool:
        return len(self._recent(key, now)) >= limit

    def fail(self, key: str, now: float) -> None:
        self._recent(key, now).append(now)

    def clear(self, key: str) -> None:
        self.fails.pop(key, None)


class Accounts:
    def __init__(self, lake: Lake, pepper: str, session_secret: str, setup_code: str | None):
        if len(pepper) < 16 or len(session_secret) < 32:
            raise RuntimeError("AUTH_PEPPER (16+ characters) and SESSION_SECRET (32+) must be set")
        self.lake = lake
        self.pepper = pepper.encode()
        self.key = hashlib.sha256(b"session\0" + session_secret.encode()).digest()
        self.setup_code = setup_code or None
        self.lock = threading.Lock()
        self.limiter = Limiter()
        self.users: dict[str, tuple[Account, datetime]] = {}
        for row in lake.latest("users", "user_id"):
            self.users[row["user_id"]] = (
                Account(
                    user_id=row["user_id"],
                    username=row["username"],
                    password_hash=row["password_hash"],
                    role=row["role"] or "member",
                    session_epoch=row["session_epoch"] or 0,
                    display_name=row["display_name"],
                    invite_id=row["invite_id"],
                    disabled=bool(row["disabled"]),
                ),
                row["logged_at"],
            )
        # A hash to verify against when the username doesn't exist, so a miss takes as long as a wrong password.
        self._dummy = _hasher.hash(self._peppered(secrets.token_urlsafe(16)))

    # --- passwords -----------------------------------------------------------------------------------

    def _peppered(self, password: str) -> str:
        return hmac.new(self.pepper, password.encode(), hashlib.sha256).hexdigest()

    def _hash(self, password: str) -> str:
        return _hasher.hash(self._peppered(password))

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return _hasher.verify(password_hash, self._peppered(password))
        except (VerificationError, InvalidHashError):
            return False

    # --- records -------------------------------------------------------------------------------------

    def _write(self, acct: Account) -> Account:
        """One users file per change, published before the cache changes."""
        prev = self.users.get(acct.user_id)
        at = _now()
        if prev and at <= prev[1]:
            at = prev[1] + timedelta(seconds=1)
        row = {
            "user_id": acct.user_id,
            "logged_at": at,
            "username": acct.username,
            "display_name": acct.display_name,
            "password_hash": acct.password_hash,
            "role": acct.role,
            "session_epoch": acct.session_epoch,
            "invite_id": acct.invite_id,
            "disabled": acct.disabled,
        }
        self.lake.write("users", f"users/{acct.user_id}__{at:%Y%m%dT%H%M%S}.parquet", [row])
        self.lake.publish(f"app: users {acct.user_id}")
        self.users[acct.user_id] = (acct, at)
        return acct

    def get(self, user_id: str) -> Account | None:
        hit = self.users.get(user_id)
        return hit[0] if hit else None

    def by_name(self, username: str) -> Account | None:
        username = username.strip().lower()
        return next((a for a, _ in self.users.values() if a.username == username), None)

    @property
    def needs_owner(self) -> bool:
        return not self.users

    # --- sign-up and sign-in -------------------------------------------------------------------------

    def create_owner(self, setup_code: str, username: str, password: str) -> Account:
        """The first account, with OWNER_SETUP_CODE. Once anyone exists, the code does nothing."""
        username = username.strip().lower()
        with self.lock:
            if not self.needs_owner:
                raise AccountError("This app already has an owner. Sign in instead.")
            if not self.setup_code:
                raise AccountError("OWNER_SETUP_CODE isn’t set on the server.")
            if not hmac.compare_digest(setup_code.strip().encode(), self.setup_code.encode()):
                raise AccountError("That setup code isn’t right.")
            if not USERNAME.fullmatch(username):
                raise AccountError("Use 3 to 32 lower-case letters, digits, - or _.")
            check_password(password, username)
            # The owner keeps the id that shots from before accounts already belong to.
            acct = Account(shots.owner_user_id(), username, self._hash(password), role="owner")
            return self._write(acct)

    def sign_in(self, username: str, password: str, ip: str, now: float | None = None) -> Account:
        """The same message whether or not the username exists, and the same work."""
        now = time.monotonic() if now is None else now
        name = username.strip().lower()
        ukey, ikey = f"u:{name}", f"ip:{ip}"
        if self.limiter.blocked(ukey, MAX_PER_USERNAME, now) or self.limiter.blocked(ikey, MAX_PER_IP, now):
            raise AccountError("Too many tries. Wait 15 minutes and try again.")
        acct = self.by_name(name)
        ok = self._verify(acct.password_hash if acct else self._dummy, password) and acct is not None
        if not ok or acct is None or acct.disabled:
            self.limiter.fail(ukey, now)
            self.limiter.fail(ikey, now)
            raise AccountError("That username and password don’t match. Check both and try again.")
        self.limiter.clear(ukey)
        if _hasher.check_needs_rehash(acct.password_hash):
            with self.lock:
                acct = self._write(replace(acct, password_hash=self._hash(password)))
        return acct

    def change_password(self, user_id: str, current: str, new: str) -> Account:
        with self.lock:
            acct = self.get(user_id)
            if acct is None or not self._verify(acct.password_hash, current):
                raise AccountError("Your current password isn’t right.")
            check_password(new, acct.username)
            return self._write(
                replace(acct, password_hash=self._hash(new), session_epoch=acct.session_epoch + 1)
            )

    def sign_out_everywhere(self, user_id: str) -> Account:
        with self.lock:
            acct = self.get(user_id)
            if acct is None:
                raise AccountError("No such account.")
            return self._write(replace(acct, session_epoch=acct.session_epoch + 1))

    # --- sessions ------------------------------------------------------------------------------------

    def issue(self, acct: Account, now: float | None = None) -> str:
        body = _b64(
            json.dumps(
                {"u": acct.user_id, "e": acct.session_epoch, "t": int(time.time() if now is None else now)},
                separators=(",", ":"),
            ).encode()
        )
        return f"{body}.{_b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())}"

    def session(self, token: str | None, now: float | None = None) -> tuple[Account, bool] | None:
        """The account a cookie belongs to and whether it's due for renewal, or None."""
        if not token or token.count(".") != 1 or len(token) > 512:
            return None
        body, sig = token.split(".")
        want = hmac.new(self.key, body.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_unb64(sig), want):
                return None
            data = json.loads(_unb64(body))
            user_id, epoch, issued = str(data["u"]), int(data["e"]), int(data["t"])
        except (ValueError, KeyError, TypeError):
            return None
        now = time.time() if now is None else now
        age = now - issued
        if age < -60 or age > SESSION_DAYS * 86400:
            return None
        acct = self.get(user_id)
        if acct is None or acct.disabled or acct.session_epoch != epoch:
            return None
        return acct, age > RENEW_AFTER.total_seconds()
