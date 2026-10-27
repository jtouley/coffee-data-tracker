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
import uuid
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
MAX_PER_USERNAME, MAX_PER_IP = 5, 20  # per username from one address, per address
# Per username across all addresses: after BACKOFF_AFTER failures in the window, each further try waits 2, 4, 8 … up to
# BACKOFF_MAX seconds after the last failure. Usernames are visible to members, so a hard lock on the username alone
# would let anyone keep its owner out; this only slows a spread-out guesser down.
BACKOFF_AFTER, BACKOFF_MAX = 10, 30.0

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


@dataclass(frozen=True)
class Invite:
    """A join link, or a reset code for one account. Only the code's SHA-256 is kept."""

    invite_id: str
    code_sha256: str
    kind: str  # join or reset
    created_by: str
    expires_at: datetime
    for_user_id: str | None = None
    used_by: str | None = None
    used_at: datetime | None = None
    revoked: bool = False

    def open(self, now: datetime) -> bool:
        return not self.used_by and not self.revoked and now < self.expires_at


INVITE_DAYS = 7
_CODE_FIX = str.maketrans({"0": "O", "1": "I", "8": "B"})  # what people type for base32's missing digits


def new_code() -> str:
    """80 random bits as 16 base32 characters, in fours: K7QD-M2XF-9RPA-T4WE."""
    raw = base64.b32encode(secrets.token_bytes(10)).decode()
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def code_hash(code: str) -> str:
    clean = "".join(c for c in code.upper().translate(_CODE_FIX) if c.isalnum())
    return hashlib.sha256(clean.encode()).hexdigest()


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
    """Failed attempts per key within LOCKOUT seconds, in memory (a restart clears it).

    An attempt is reserved before the slow password check and released if it succeeds, under one lock, so a burst
    of parallel guesses can't all pass the check before any of them is counted."""

    SWEEP_AT = 10_000

    def __init__(self) -> None:
        self.fails: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self.fails[key]
        while q and q[0] <= now - LOCKOUT:
            q.popleft()
        return q

    def _sweep(self, now: float) -> None:
        if len(self.fails) > self.SWEEP_AT:
            for key in [k for k, q in self.fails.items() if not q or q[-1] <= now - LOCKOUT]:
                del self.fails[key]

    def reserve(self, limits: dict[str, int], now: float) -> bool:
        """Count one attempt against every key, unless any key is already at its limit."""
        with self.lock:
            self._sweep(now)
            if any(len(self._recent(k, now)) >= n for k, n in limits.items()):
                return False
            for k in limits:
                self.fails[k].append(now)
            return True

    def release(self, key: str, now: float) -> None:
        """Take back one reserved attempt (it succeeded)."""
        with self.lock:
            q = self.fails.get(key)
            if q and now in q:
                q.remove(now)

    def blocked(self, key: str, limit: int, now: float) -> bool:
        with self.lock:
            return len(self._recent(key, now)) >= limit

    def fail(self, key: str, now: float) -> None:
        with self.lock:
            self._sweep(now)
            self._recent(key, now).append(now)

    def reserve_backoff(self, key: str, after: int, cap: float, now: float) -> float:
        """Count one attempt against a key that backs off instead of locking: once it has `after` attempts in the
        window, each next one waits 2, 4, 8 … up to `cap` seconds after the last. Returns the wait (nothing counted),
        or 0 with the attempt counted, under one lock, so parallel tries can't skip the wait."""
        with self.lock:
            self._sweep(now)
            q = self._recent(key, now)
            if len(q) >= after:
                wait = q[-1] + min(2.0 ** (len(q) - after + 1), cap) - now
                if wait > 0:
                    return wait
            q.append(now)
            return 0.0

    def clear(self, key: str) -> None:
        with self.lock:
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
        self.invites: dict[str, tuple[Invite, datetime]] = {}
        for row in lake.latest("invites", "invite_id"):
            self.invites[row["invite_id"]] = (
                Invite(
                    invite_id=row["invite_id"],
                    code_sha256=row["code_sha256"],
                    kind=row["kind"],
                    created_by=row["created_by"],
                    expires_at=row["expires_at"],
                    for_user_id=row["for_user_id"],
                    used_by=row["used_by"],
                    used_at=row["used_at"],
                    revoked=bool(row["revoked"]),
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

    def _write_invite(self, inv: Invite) -> Invite:
        prev = self.invites.get(inv.invite_id)
        at = _now()
        if prev and at <= prev[1]:
            at = prev[1] + timedelta(seconds=1)
        row = {
            "invite_id": inv.invite_id,
            "logged_at": at,
            "code_sha256": inv.code_sha256,
            "kind": inv.kind,
            "for_user_id": inv.for_user_id,
            "created_by": inv.created_by,
            "expires_at": inv.expires_at,
            "used_by": inv.used_by,
            "used_at": inv.used_at,
            "revoked": inv.revoked,
        }
        self.lake.write("invites", f"invites/{inv.invite_id}__{at:%Y%m%dT%H%M%S}.parquet", [row])
        self.lake.publish(f"app: invites {inv.invite_id}")
        self.invites[inv.invite_id] = (inv, at)
        return inv

    def get(self, user_id: str) -> Account | None:
        hit = self.users.get(user_id)
        return hit[0] if hit else None

    def by_name(self, username: str) -> Account | None:
        username = username.strip().lower()
        return next((a for a, _ in list(self.users.values()) if a.username == username), None)

    @property
    def needs_owner(self) -> bool:
        return not self.users

    # --- sign-up and sign-in -------------------------------------------------------------------------

    def create_owner(
        self, setup_code: str, username: str, password: str, ip: str = "", now: float | None = None
    ) -> Account:
        """The first account, with OWNER_SETUP_CODE. Once anyone exists, the code does nothing. Wrong codes count
        against the address like failed sign-ins."""
        username = username.strip().lower()
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.needs_owner:
                raise AccountError("This app already has an owner. Sign in instead.")
            if not self.setup_code:
                raise AccountError("OWNER_SETUP_CODE isn’t set on the server.")
            key = f"ip:{ip}"
            if not self.limiter.reserve({key: MAX_PER_IP}, now):
                raise AccountError("Too many tries. Wait 15 minutes and try again.")
            if not hmac.compare_digest(setup_code.strip().encode(), self.setup_code.encode()):
                raise AccountError("That setup code isn’t right.")
            self.limiter.release(key, now)
            if not USERNAME.fullmatch(username):
                raise AccountError("Use 3 to 32 lower-case letters, digits, - or _.")
            check_password(password, username)
            # The owner keeps the id that shots from before accounts already belong to.
            acct = Account(shots.owner_user_id(), username, self._hash(password), role="owner")
            acct = self._write(acct)
            self._spend_setup_code(acct.user_id)
            return acct

    def _setup_code_spent(self) -> bool:
        sha = hashlib.sha256(b"setup\0" + (self.setup_code or "").strip().encode()).hexdigest()
        return any(i.kind == "setup" and i.code_sha256 == sha for i, _ in list(self.invites.values()))

    def _spend_setup_code(self, user_id: str) -> None:
        """Each OWNER_SETUP_CODE value works once (#16-3): its hash is kept as a used "setup" record. Using it
        again needs a new value in the secret."""
        sha = hashlib.sha256(b"setup\0" + (self.setup_code or "").strip().encode()).hexdigest()
        at = _now()
        self._write_invite(
            Invite(f"setup-{sha[:16]}", sha, "setup", user_id, at, used_by=user_id, used_at=at)
        )

    def sign_in(self, username: str, password: str, ip: str, now: float | None = None) -> Account:
        """The same message whether or not the username exists, and the same work."""
        now = time.monotonic() if now is None else now
        name = username.strip().lower()
        pkey, ikey, ukey = f"u:{name}|ip:{ip}", f"ip:{ip}", f"u:{name}"
        wait = self.limiter.reserve_backoff(ukey, BACKOFF_AFTER, BACKOFF_MAX, now)
        if wait:
            raise AccountError(
                f"Too many tries for this username. Wait {int(wait) + 1} seconds and try again."
            )
        if not self.limiter.reserve({pkey: MAX_PER_USERNAME, ikey: MAX_PER_IP}, now):
            self.limiter.release(ukey, now)
            raise AccountError("Too many tries. Wait 15 minutes and try again.")
        acct = self.by_name(name)
        ok = self._verify(acct.password_hash if acct else self._dummy, password) and acct is not None
        if not ok or acct is None or acct.disabled:
            raise AccountError("That username and password don’t match. Check both and try again.")
        self.limiter.clear(pkey)
        self.limiter.release(ikey, now)
        self.limiter.release(ukey, now)
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

    # --- invites and reset codes ---------------------------------------------------------------------

    def _owner(self, user_id: str) -> Account:
        acct = self.get(user_id)
        if acct is None or acct.role != "owner" or acct.disabled:
            raise AccountError("Only the owner can do that.")
        return acct

    def people(self) -> list[Account]:
        # A copy: another thread may add an account meanwhile (and callers may already hold self.lock).
        return sorted(
            (a for a, _ in list(self.users.values())), key=lambda a: (a.role != "owner", a.username)
        )

    def pending(self, now: datetime | None = None) -> list[Invite]:
        """Join links not used, revoked or expired, newest first."""
        now = now or _now()
        open_ = [(i, at) for i, at in list(self.invites.values()) if i.kind == "join" and i.open(now)]
        return [i for i, _ in sorted(open_, key=lambda x: x[1], reverse=True)]

    def invite(self, by: str, kind: str = "join", for_user_id: str | None = None) -> tuple[Invite, str]:
        """A new code, shown once. A new reset code for someone replaces any earlier one."""
        with self.lock:
            self._owner(by)
            return self._new_invite(by, kind, for_user_id)

    def _new_invite(self, by: str, kind: str, for_user_id: str | None) -> tuple[Invite, str]:
        if kind == "reset" and (for_user_id is None or self.get(for_user_id) is None):
            raise AccountError("No such account.")
        if kind == "reset":
            for old, _ in list(self.invites.values()):
                if old.kind == "reset" and old.for_user_id == for_user_id and old.open(_now()):
                    self._write_invite(replace(old, revoked=True))
        code = new_code()
        inv = Invite(
            invite_id=str(uuid.uuid4()),
            code_sha256=code_hash(code),
            kind=kind,
            created_by=by,
            expires_at=_now() + timedelta(days=INVITE_DAYS),
            for_user_id=for_user_id,
        )
        return self._write_invite(inv), code

    def revoke(self, by: str, invite_id: str) -> None:
        with self.lock:
            self._owner(by)
            hit = self.invites.get(invite_id)
            if hit is None:
                raise AccountError("No such invite.")
            if not hit[0].revoked:
                self._write_invite(replace(hit[0], revoked=True))

    def renew(self, by: str, invite_id: str) -> tuple[Invite, str]:
        """ "Send a new link": revoke the old code and make a new one of the same kind. Owner only, checked first,
        and only for a link still open, so the replies say nothing about which invite ids exist."""
        with self.lock:
            self._owner(by)
            hit = self.invites.get(invite_id)
            if hit is None or not hit[0].open(_now()):
                raise AccountError("That link can’t be renewed. Make a new one instead.")
            self._write_invite(replace(hit[0], revoked=True))
            return self._new_invite(by, hit[0].kind, hit[0].for_user_id)

    def lookup(self, code: str, ip: str, now: float | None = None) -> Invite:
        """The open invite for a code. Wrong codes count against the address like failed sign-ins."""
        now = time.monotonic() if now is None else now
        key = f"ip:{ip}"
        if self.limiter.blocked(key, MAX_PER_IP, now):
            raise AccountError("Too many tries. Wait 15 minutes and try again.")
        h = code_hash(code)
        hit = next((i for i, _ in list(self.invites.values()) if hmac.compare_digest(i.code_sha256, h)), None)
        if hit is None or not hit.open(_now()):
            self.limiter.fail(key, now)
            raise AccountError("That code isn’t valid. It may have been used, replaced or expired.")
        return hit

    def join(self, code: str, username: str, password: str, ip: str) -> Account:
        username = username.strip().lower()
        with self.lock:
            inv = self.lookup(code, ip)
            if inv.kind != "join":
                raise AccountError("That’s a reset code. Use it on the reset screen.")
            if not USERNAME.fullmatch(username):
                raise AccountError("Use 3 to 32 lower-case letters, digits, - or _.")
            if self.by_name(username):
                raise AccountError("That username is taken. Try another.")
            check_password(password, username)
            acct = self._write(
                Account(str(uuid.uuid4()), username, self._hash(password), invite_id=inv.invite_id)
            )
            self._write_invite(replace(inv, used_by=acct.user_id, used_at=_now()))
            return acct

    def reset(self, code: str, password: str, ip: str, now: float | None = None) -> Account:
        """A reset code sets a new password and signs out every device. The owner's own account can be reset
        with OWNER_SETUP_CODE. A blocked address can't try either; a wrong code counts against it (in lookup)."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.limiter.blocked(f"ip:{ip}", MAX_PER_IP, now):
                raise AccountError("Too many tries. Wait 15 minutes and try again.")
            if self.setup_code and hmac.compare_digest(code.strip().encode(), self.setup_code.encode()):
                if self._setup_code_spent():
                    raise AccountError(
                        "That setup code has been used. Put a new one in OWNER_SETUP_CODE to use it again."
                    )
                acct = next((a for a in self.people() if a.role == "owner"), None)
                if acct is None:
                    raise AccountError("There’s no owner yet. Create the account first.")
                inv = None
            else:
                inv = self.lookup(code, ip, now)
                acct = self.get(inv.for_user_id or "") if inv.kind == "reset" else None
                if acct is None:
                    raise AccountError("That’s an invite, not a reset code.")
                if acct.disabled:
                    raise AccountError("This account is turned off. Ask the owner to turn it back on.")
            check_password(password, acct.username)
            acct = self._write(
                replace(acct, password_hash=self._hash(password), session_epoch=acct.session_epoch + 1)
            )
            if inv:
                self._write_invite(replace(inv, used_by=acct.user_id, used_at=_now()))
            else:
                self._spend_setup_code(acct.user_id)
            self.limiter.clear(f"u:{acct.username}")
            return acct

    def set_disabled(self, by: str, user_id: str, disabled: bool) -> Account:
        """The owner turns an account off (or back on). Off signs it out everywhere."""
        with self.lock:
            self._owner(by)
            acct = self.get(user_id)
            if acct is None or acct.role == "owner":
                raise AccountError("No such account.")
            if disabled:  # an outstanding reset code would otherwise still change the password
                for inv, _ in list(self.invites.values()):
                    if inv.kind == "reset" and inv.for_user_id == user_id and inv.open(_now()):
                        self._write_invite(replace(inv, revoked=True))
            return self._write(replace(acct, disabled=disabled, session_epoch=acct.session_epoch + 1))

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
