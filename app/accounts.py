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

from . import rules
from .rules import clean_username, nfc
from .store import STARTUP_READS

log = logging.getLogger(__name__)

SESSION_DAYS = 90
RENEW_AFTER = timedelta(days=1)  # a cookie used after a day is reissued, so it lasts 90 days from last use
LOCKOUT = 15 * 60
MAX_PER_USERNAME, MAX_PER_IP = 5, 20  # per username from one address, per address
# Per username across all addresses: after BACKOFF_AFTER failures in the window, each further try waits 2, 4, 8 … up to
# BACKOFF_MAX seconds after the last failure. Usernames are visible to members, so a hard lock on the username alone
# would let anyone keep its owner out; this only slows a spread-out guesser down.
BACKOFF_AFTER, BACKOFF_MAX = 10, 30.0
# Accounts made before acceptance was recorded agreed to the terms in force then, by the "By joining you agree" line
# on the join page. They count as having accepted that version, so a later LEGAL_UPDATED asks them again.
TERMS_BEFORE_RECORDS = "November 2, 2026"

_hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)  # Argon2id, 64 MiB, 3 passes, 4 lanes
# Each hash or verify takes 64 MiB while it runs, and requests run in a thread pool of up to 40. A burst of sign-ins
# could take more memory than the 1 GiB instance has, so at most this many run at once; the rest wait their turn.
HASHES_AT_ONCE = 4
_hashing = threading.BoundedSemaphore(HASHES_AT_ONCE)


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
    for_label: str | None = None  # the owner's "For" note on a join link, shown only to the owner
    created_at: datetime | None = None
    revoked_at: datetime | None = None  # kept, so a later edit (a new note) doesn't move it

    def revoke(self) -> Invite:
        return replace(self, revoked=True, revoked_at=_now())

    def open(self, now: datetime) -> bool:
        return not self.used_by and not self.revoked and now < self.expires_at

    def status(self, now: datetime) -> str:
        if self.used_by:
            return "used"
        if self.revoked:
            return "revoked"
        return "open" if now < self.expires_at else "expired"


INVITE_DAYS = 7
JOIN_RETRY = timedelta(minutes=15)  # how long after a join link is used a retry of that join still signs in
OLD_AFTER_DAYS = 90  # expired and revoked links older than this fold away on the invite screen


@dataclass(frozen=True)
class InviteRow:
    """One line of the owner's invite list."""

    invite: Invite
    status: str  # open, used, expired or revoked
    when: datetime  # when that status began (for open: when it expires)
    used_by: str | None  # the username it made
    old: bool


_CODE_FIX = str.maketrans({"0": "O", "1": "I", "8": "B"})  # what people type for base32's missing digits


def new_code() -> str:
    """80 random bits as 16 base32 characters, in fours: K7QD-M2XF-9RPA-T4WE."""
    raw = base64.b32encode(secrets.token_bytes(10)).decode()
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def code_hash(code: str) -> str:
    clean = "".join(c for c in code.upper().translate(_CODE_FIX) if c.isalnum())
    return hashlib.sha256(clean.encode()).hexdigest()


class AccountError(ValueError):
    """Something to show the person, as is. `errors` tags it with the fields it belongs to ({"field", "rule",
    "message"}, from rules.py); without them it belongs to the whole form. `status` is the HTTP status to answer."""

    def __init__(self, message: str = "", errors: list[dict[str, str]] | None = None, status: int = 400):
        self.errors = errors or []
        self.status = status
        super().__init__(message or " ".join(e["message"] for e in self.errors))

    @property
    def detail(self) -> str | list[dict[str, str]]:
        return self.errors or str(self)


# Messages tagged with a field that aren't sign-up rules (rules.py holds those).
CODE_INVALID = "That code isn’t valid. It may have been used, replaced or expired."
CODE_USED = (
    "This invite has already been used. If you made an account with it, sign in. If not, ask whoever sent it for a "
    "new link."
)
MISMATCH = "That username and password don’t match. Check them, or ask the owner for a reset code."


def field_error(field: str, rule: str, message: str, status: int = 400) -> AccountError:
    return AccountError(errors=[{"field": field, "rule": rule, "message": message}], status=status)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def check_password(password: str, username: str = "") -> None:
    """Raises with every rule the password fails (rules.check_password)."""
    failed = rules.check_password(password, username)
    if failed:
        raise AccountError(errors=failed)


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


@dataclass(frozen=True)
class Consent:
    """Which Terms and Privacy Policy an account accepted, and when (spec §7). Latest wins."""

    user_id: str
    terms_version: str
    privacy_version: str
    terms_at: datetime
    source: str  # signup or reaccept


def legal_date(version: str) -> datetime:
    return datetime.strptime(version, "%B %d, %Y")


class Accounts:
    def __init__(
        self,
        lake: Lake,
        pepper: str,
        session_secret: str,
        setup_code: str | None,
        legal_version: str | None = None,
    ):
        if len(pepper) < 16 or len(session_secret) < 32:
            raise RuntimeError("AUTH_PEPPER (16+ characters) and SESSION_SECRET (32+) must be set")
        self.lake = lake
        self.pepper = pepper.encode()
        self.key = hashlib.sha256(b"session\0" + session_secret.encode()).digest()
        self.setup_code = setup_code or None
        if legal_version is None:
            from coffeedata.site import LEGAL_UPDATED

            legal_version = LEGAL_UPDATED
        self.legal = legal_version  # the Terms' and the Privacy Policy's "Last updated" (they share one date)
        self.lock = threading.Lock()
        self.limiter = Limiter()
        self.users: dict[str, tuple[Account, datetime]] = {}
        for row in lake.latest("users", "user_id", attempts=STARTUP_READS):
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
        for row in lake.latest("invites", "invite_id", attempts=STARTUP_READS):
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
                    # Files from before revoked_at was kept: the record that revoked it is the latest one.
                    revoked_at=row.get("revoked_at") or (row["logged_at"] if row["revoked"] else None),
                    for_label=row.get("for_label"),
                    # Files from before created_at was kept: invites have always lasted INVITE_DAYS.
                    created_at=row.get("created_at") or row["expires_at"] - timedelta(days=INVITE_DAYS),
                ),
                row["logged_at"],
            )
        self.consents: dict[str, tuple[Consent, datetime]] = {}
        if "consents" in lake.tables:
            for row in lake.latest("consents", "user_id", attempts=STARTUP_READS):
                self.consents[row["user_id"]] = (
                    Consent(row["user_id"], row["terms_version"], row["privacy_version"], row["terms_at"],
                            row["source"]),
                    row["logged_at"],
                )  # fmt: skip
        # A hash to verify against when the username doesn't exist, so a miss takes as long as a wrong password.
        self._dummy = self._hash(secrets.token_urlsafe(16))

    # --- passwords -----------------------------------------------------------------------------------

    def _peppered(self, password: str) -> str:
        return hmac.new(self.pepper, password.encode(), hashlib.sha256).hexdigest()

    def _hash(self, password: str) -> str:
        """Of the NFC form (NIST 800-63B §3.1.1.2), so a composed and a decomposed é are the same password."""
        with _hashing:
            return _hasher.hash(self._peppered(nfc(password)))

    def _verify(self, password_hash: str, password: str) -> bool:
        """NFC first, then the string as typed, so a hash made before passwords were normalised still matches."""
        tries = [nfc(password)] + ([password] if nfc(password) != password else [])
        for pw in tries:
            try:
                with _hashing:
                    if _hasher.verify(password_hash, self._peppered(pw)):
                        return True
            except (VerificationError, InvalidHashError):
                continue
        return False

    # --- records -------------------------------------------------------------------------------------

    @staticmethod
    def _next_at(prev: tuple[object, datetime] | None) -> datetime:
        at = _now()
        return prev[1] + timedelta(seconds=1) if prev and at <= prev[1] else at

    def _stage(self, acct: Account) -> tuple[Account, datetime]:
        """One users file per change. Staged only: `_commit` publishes and then updates the cache."""
        at = self._next_at(self.users.get(acct.user_id))
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
        return acct, at

    def _stage_invite(self, inv: Invite) -> tuple[Invite, datetime]:
        at = self._next_at(self.invites.get(inv.invite_id))
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
            "revoked_at": inv.revoked_at,
            "for_label": inv.for_label,
            "created_at": inv.created_at,
        }
        self.lake.write("invites", f"invites/{inv.invite_id}__{at:%Y%m%dT%H%M%S}.parquet", [row])
        return inv, at

    def _stage_consent(self, user_id: str, source: str) -> tuple[Consent, datetime]:
        at = self._next_at(self.consents.get(user_id))
        c = Consent(user_id, self.legal, self.legal, at, source)
        row = {
            "user_id": user_id,
            "logged_at": at,
            "terms_version": c.terms_version,
            "privacy_version": c.privacy_version,
            "terms_at": c.terms_at,
            "source": source,
        }
        self.lake.write("consents", f"consents/{user_id}__{at:%Y%m%dT%H%M%S}.parquet", [row])
        return c, at

    def _commit(
        self,
        message: str,
        users: tuple[tuple[Account, datetime], ...] = (),
        invites: tuple[tuple[Invite, datetime], ...] = (),
        consents: tuple[tuple[Consent, datetime], ...] = (),
    ) -> None:
        """Publish what was staged in one commit, then let the cache see it, so the records of one change (an
        account, its invite marked used and its acceptance) are sent together. If the publish fails they stay
        staged, unseen by the cache, and go with the next publish (Lake.publish); the cache sees them after the
        next restart."""
        self.lake.publish(message)
        for acct, at in users:
            self.users[acct.user_id] = (acct, at)
        for inv, at in invites:
            self.invites[inv.invite_id] = (inv, at)
        for c, at in consents:
            self.consents[c.user_id] = (c, at)

    def _write(self, acct: Account) -> Account:
        """One users file per change, published before the cache changes."""
        self._commit(f"app: users {acct.user_id}", users=(self._stage(acct),))
        return acct

    def _write_invite(self, inv: Invite) -> Invite:
        self._commit(f"app: invites {inv.invite_id}", invites=(self._stage_invite(inv),))
        return inv

    def get(self, user_id: str) -> Account | None:
        hit = self.users.get(user_id)
        return hit[0] if hit else None

    def by_name(self, username: str) -> Account | None:
        username = clean_username(username)
        return next((a for a, _ in list(self.users.values()) if a.username == username), None)

    @property
    def needs_owner(self) -> bool:
        return not self.users

    # --- sign-up and sign-in -------------------------------------------------------------------------

    def _new_account_errors(self, username: str, password: str, agree: bool) -> list[dict[str, str]]:
        """Every rule a new account's details fail, all at once (spec §3)."""
        errors = rules.check_username(username)
        if not errors and self.by_name(username):
            errors.append(rules.error("u.taken"))
        errors += rules.check_password(password, username)
        if not agree:
            errors.append(rules.error("terms"))
        return errors

    def create_owner(
        self,
        setup_code: str,
        username: str,
        password: str,
        ip: str = "",
        now: float | None = None,
        agree: bool = False,
    ) -> Account:
        """The first account, with OWNER_SETUP_CODE. Once anyone exists, the code does nothing. Wrong codes count
        against the address like failed sign-ins."""
        username = clean_username(username)
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.needs_owner:
                raise AccountError("This app already has an owner. Sign in instead.")
            if not self.setup_code:
                raise AccountError("OWNER_SETUP_CODE isn’t set on the server.")
            key = f"ip:{ip}"
            if not self.limiter.reserve({key: MAX_PER_IP}, now):
                raise AccountError("Too many tries. Wait 15 minutes and try again.")
            errors = []
            if hmac.compare_digest(setup_code.strip().encode(), self.setup_code.encode()):
                self.limiter.release(key, now)
            else:
                errors += field_error("setup_code", "s.wrong", "That setup code isn’t right.").errors
            errors += self._new_account_errors(username, password, agree)
            if errors:
                raise AccountError(errors=errors)
            # The owner keeps the id that shots from before accounts already belong to.
            acct = Account(shots.owner_user_id(), username, self._hash(password), role="owner")
            self._commit(
                f"app: users {acct.user_id}",
                users=(self._stage(acct),),
                invites=(self._stage_invite(self._setup_invite(acct.user_id)),),
                consents=(self._stage_consent(acct.user_id, "signup"),),
            )
            return acct

    def _setup_sha(self) -> str:
        return hashlib.sha256(b"setup\0" + (self.setup_code or "").strip().encode()).hexdigest()

    def _setup_code_spent(self) -> bool:
        sha = self._setup_sha()
        return any(i.kind == "setup" and i.code_sha256 == sha for i, _ in list(self.invites.values()))

    def _setup_invite(self, user_id: str) -> Invite:
        """Each OWNER_SETUP_CODE value works once (#16-3): its hash is kept as a used "setup" record. Using it
        again needs a new value in the secret."""
        sha, at = self._setup_sha(), _now()
        return Invite(
            f"setup-{sha[:16]}", sha, "setup", user_id, at, used_by=user_id, used_at=at, created_at=at
        )

    def sign_in(self, username: str, password: str, ip: str, now: float | None = None) -> Account:
        """The same message whether or not the username exists, and the same work."""
        now = time.monotonic() if now is None else now
        name = clean_username(username)
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
            raise AccountError(MISMATCH)
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
                raise field_error("current", "current", "Your current password isn’t right.")
            failed = rules.check_password(new, acct.username)
            if failed:  # the field is "new" on this form
                raise AccountError(errors=[e | {"field": "new"} for e in failed])
            return self._write(
                replace(acct, password_hash=self._hash(new), session_epoch=acct.session_epoch + 1)
            )

    def sign_out_everywhere(self, user_id: str) -> Account:
        with self.lock:
            acct = self.get(user_id)
            if acct is None:
                raise AccountError("No such account.")
            return self._write(replace(acct, session_epoch=acct.session_epoch + 1))

    # --- the Terms and Privacy Policy --------------------------------------------------------------------

    def needs_agree(self, user_id: str) -> bool:
        """Whether the account last accepted an older Terms and Privacy Policy than the current ones."""
        hit = self.consents.get(user_id)
        version = hit[0].terms_version if hit else TERMS_BEFORE_RECORDS
        return legal_date(version) < legal_date(self.legal)

    def accept(self, user_id: str) -> None:
        with self.lock:
            if self.get(user_id) is None:
                raise AccountError("No such account.")
            self._commit(f"app: consents {user_id}", consents=(self._stage_consent(user_id, "reaccept"),))

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

    def invite_log(self, now: datetime | None = None) -> list[InviteRow]:
        """Every join link, newest first, with its status and the date that status began (spec §8)."""
        now = now or _now()
        out = []
        for inv, at in list(self.invites.values()):
            if inv.kind != "join":
                continue
            status = inv.status(now)
            when = {"used": inv.used_at, "revoked": inv.revoked_at or at, "expired": inv.expires_at}.get(
                status
            ) or inv.expires_at
            user = self.get(inv.used_by) if inv.used_by else None
            out.append(InviteRow(inv, status, when, user.username if user else None,
                                 status in ("expired", "revoked") and now - when > timedelta(days=OLD_AFTER_DAYS)))  # fmt: skip
        return sorted(out, key=lambda r: r.invite.created_at or r.invite.expires_at, reverse=True)

    def invite(
        self, by: str, kind: str = "join", for_user_id: str | None = None, for_label: object = None
    ) -> tuple[Invite, str]:
        """A new code, shown once. A new reset code for someone replaces any earlier one. Join links can carry a
        note for the owner; reset codes already name their member."""
        with self.lock:
            self._owner(by)
            return self._new_invite(by, kind, for_user_id, self._label(for_label) if kind == "join" else None)

    @staticmethod
    def _label(label: object) -> str | None:
        clean = rules.clean_label(label)
        failed = rules.check_label(clean)
        if failed:
            raise AccountError(errors=failed)
        return clean

    def _new_invite(
        self,
        by: str,
        kind: str,
        for_user_id: str | None,
        for_label: str | None = None,
        *staged: tuple[Invite, datetime],
    ) -> tuple[Invite, str]:
        """The new code and anything it replaces go in one commit."""
        if kind == "reset" and (for_user_id is None or self.get(for_user_id) is None):
            raise AccountError("No such account.")
        if kind == "reset":
            done = {i.invite_id for i, _ in staged}  # renew has already revoked the one it replaces
            for old, _ in list(self.invites.values()):
                if (
                    old.kind == "reset"
                    and old.for_user_id == for_user_id
                    and old.open(_now())
                    and old.invite_id not in done
                ):
                    staged += (self._stage_invite(old.revoke()),)
        code, now = new_code(), _now()
        inv = Invite(
            invite_id=str(uuid.uuid4()),
            code_sha256=code_hash(code),
            kind=kind,
            created_by=by,
            expires_at=now + timedelta(days=INVITE_DAYS),
            for_user_id=for_user_id,
            for_label=for_label,
            created_at=now,
        )
        self._commit(f"app: invites {inv.invite_id}", invites=(*staged, self._stage_invite(inv)))
        return inv, code

    def revoke(self, by: str, invite_id: str) -> None:
        with self.lock:
            self._owner(by)
            hit = self.invites.get(invite_id)
            if hit is None:
                raise AccountError("No such invite.")
            if not hit[0].revoked:
                self._write_invite(hit[0].revoke())

    def set_label(self, by: str, invite_id: str, label: object) -> Invite:
        """Edit or clear (empty) a join link's note. A new version of the invite; latest wins."""
        with self.lock:
            self._owner(by)
            hit = self.invites.get(invite_id)
            if hit is None or hit[0].kind != "join":
                raise AccountError("No such invite.")
            return self._write_invite(replace(hit[0], for_label=self._label(label)))

    def renew(self, by: str, invite_id: str) -> tuple[Invite, str]:
        """ "Send a new link": revoke the old code and make a new one of the same kind, keeping its note. Owner
        only, checked first, and only for a link still open, so the replies say nothing about which invite ids
        exist."""
        with self.lock:
            self._owner(by)
            hit = self.invites.get(invite_id)
            if hit is None or not hit[0].open(_now()):
                raise AccountError("That link can’t be renewed. Make a new one instead.")
            old = hit[0]
            revoked = self._stage_invite(old.revoke())
            return self._new_invite(by, old.kind, old.for_user_id, old.for_label, revoked)

    def _find(self, code: str) -> Invite | None:
        h = code_hash(code)
        return next(
            (i for i, _ in list(self.invites.values()) if hmac.compare_digest(i.code_sha256, h)), None
        )

    def lookup(self, code: str, ip: str, now: float | None = None) -> Invite:
        """The open invite for a code. Wrong codes count against the address like failed sign-ins. A join link
        already used says so (409, `c.used`), so whoever made an account with it knows to sign in."""
        now = time.monotonic() if now is None else now
        key = f"ip:{ip}"
        if self.limiter.blocked(key, MAX_PER_IP, now):
            raise AccountError("Too many tries. Wait 15 minutes and try again.")
        hit = self._find(code)
        if hit is None or not hit.open(_now()):
            self.limiter.fail(key, now)
            if hit and hit.kind == "join" and hit.used_by:
                raise field_error("code", "c.used", CODE_USED, 409)
            raise field_error("code", "c.invalid", CODE_INVALID)
        return hit

    def join(self, code: str, username: str, password: str, ip: str, agree: bool = False) -> Account:
        """A new account from a join link, written with the invite marked used and its acceptance of the Terms in
        one commit. Idempotent (spec §11): a retry with the same code, username and password, after a reply that
        never reached the phone, signs that account in instead of failing."""
        username = clean_username(username)
        if self.limiter.blocked(f"ip:{ip}", MAX_PER_IP, time.monotonic()):
            raise AccountError("Too many tries. Wait 15 minutes and try again.")
        with self.lock:
            hit = self._find(code)
            if not (hit and hit.kind == "join" and hit.used_by):
                return self._join(code, username, password, ip, agree)
        return self._join_again(hit, username, password, ip)

    def _join(self, code: str, username: str, password: str, ip: str, agree: bool) -> Account:
        # The code first, and only the code: without a valid join code nothing is said about the username (whether
        # it's taken) or the rest (OWASP: generic replies on account creation too).
        inv = self.lookup(code, ip)
        if inv.kind != "join":
            raise field_error("code", "c.kind", "That’s a reset code. Use it on the reset screen.")
        errors = self._new_account_errors(username, password, agree)
        if errors:
            raise AccountError(errors=errors)
        acct = Account(str(uuid.uuid4()), username, self._hash(password), invite_id=inv.invite_id)
        self._commit(
            f"app: users {acct.user_id}",
            users=(self._stage(acct),),
            invites=(self._stage_invite(replace(inv, used_by=acct.user_id, used_at=_now())),),
            consents=(self._stage_consent(acct.user_id, "signup"),),
        )
        return acct

    def _join_again(self, inv: Invite, username: str, password: str, ip: str) -> Account:
        """A used join link: within JOIN_RETRY of its use (a reply lost on the way back), the account it made, if
        the username and password are that account's. As strict as sign-in and counted by its limits. Anything
        else, a wrong password and too many tries included, gets the one "already used" reply, after the same
        work (one password check), so neither the words nor the time say which username the link made."""
        acct = self.get(inv.used_by or "")
        recent = inv.used_at is not None and _now() - inv.used_at <= JOIN_RETRY
        if recent and acct is not None and acct.username == username:
            try:
                signed = self.sign_in(username, password, ip)
                if signed.user_id == acct.user_id:
                    return signed
            except AccountError:
                pass
        else:
            self._verify(self._dummy, password)
            self.limiter.fail(f"ip:{ip}", time.monotonic())
        raise field_error("code", "c.used", CODE_USED, 409)

    def reset(self, code: str, password: str, ip: str, now: float | None = None) -> Account:
        """A reset code sets a new password and signs out every device. The owner's own account can be reset
        with OWNER_SETUP_CODE. A blocked address can't try either; a wrong code counts against it (in lookup)."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.limiter.blocked(f"ip:{ip}", MAX_PER_IP, now):
                raise AccountError("Too many tries. Wait 15 minutes and try again.")
            if self.setup_code and hmac.compare_digest(code.strip().encode(), self.setup_code.encode()):
                if self._setup_code_spent():
                    raise field_error(
                        "code",
                        "c.spent",
                        "That setup code has been used. Put a new one in OWNER_SETUP_CODE to use it again.",
                    )
                acct = next((a for a in self.people() if a.role == "owner"), None)
                if acct is None:
                    raise AccountError("There’s no owner yet. Create the account first.")
                inv = None
            else:
                inv = self.lookup(code, ip, now)
                acct = self.get(inv.for_user_id or "") if inv.kind == "reset" else None
                if acct is None:
                    raise field_error("code", "c.kind", "That’s an invite, not a reset code.")
                if acct.disabled:
                    raise AccountError("This account is turned off. Ask the owner to turn it back on.")
            check_password(password, acct.username)
            acct = replace(acct, password_hash=self._hash(password), session_epoch=acct.session_epoch + 1)
            used = (
                replace(inv, used_by=acct.user_id, used_at=_now())
                if inv
                else self._setup_invite(acct.user_id)
            )
            self._commit(
                f"app: users {acct.user_id}", users=(self._stage(acct),), invites=(self._stage_invite(used),)
            )
            self.limiter.clear(f"u:{acct.username}")
            return acct

    def set_disabled(self, by: str, user_id: str, disabled: bool) -> Account:
        """The owner turns an account off (or back on). Off signs it out everywhere."""
        with self.lock:
            self._owner(by)
            acct = self.get(user_id)
            if acct is None or acct.role == "owner":
                raise AccountError("No such account.")
            staged: tuple[tuple[Invite, datetime], ...] = ()
            if disabled:  # an outstanding reset code would otherwise still change the password
                staged = tuple(
                    self._stage_invite(inv.revoke())
                    for inv, _ in list(self.invites.values())
                    if inv.kind == "reset" and inv.for_user_id == user_id and inv.open(_now())
                )
            acct = replace(acct, disabled=disabled, session_epoch=acct.session_epoch + 1)
            self._commit(f"app: users {acct.user_id}", users=(self._stage(acct),), invites=staged)
            return acct

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
