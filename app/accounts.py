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
from .contacts import RESET_MINUTES, VERIFY_HOURS, Contact, EmailCrypto, new_token, token_hash
from .limits import Windows
from .rules import clean_email, clean_username, nfc
from .store import STARTUP_READS

log = logging.getLogger(__name__)

SESSION_DAYS = 90
RENEW_AFTER = timedelta(days=1)  # a cookie used after a day is reissued, so it lasts 90 days from last use
LOCKOUT = 15 * 60
MAX_PER_USERNAME, MAX_PER_IP = 5, 20  # per username from one address, per address
REAUTH_TRIES = 10  # wrong current passwords per account in LOCKOUT, before changing the email or the password
# Per username across all addresses: after BACKOFF_AFTER failures in the window, each further try waits 2, 4, 8 … up to
# BACKOFF_MAX seconds after the last failure. Usernames are visible to members, so a hard lock on the username alone
# would let anyone keep its owner out; this only slows a spread-out guesser down.
BACKOFF_AFTER, BACKOFF_MAX = 10, 30.0
# Accounts made before acceptance was recorded agreed to the terms in force then, by the "By joining you agree" line
# on the join page. They count as having accepted that version, so a later LEGAL_UPDATED asks them again.
TERMS_BEFORE_RECORDS = "November 2, 2026"
# The wording of the "Your next bag" consent the person agreed to. Change it whenever that consent's words change,
# so each joining is recorded against the words it was given with.
EXPERIMENT_VERSION = "your-next-bag-1"

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
VERIFY_EXPIRED = "That link has expired or was already used. Send a new one from Setup."
EMAIL_TOKEN = "E-"  # an emailed reset link's token starts with this; owner codes and the setup code never do
EMAIL_LINK_USED = (
    "That link no longer works. It may be over 30 minutes old, already used or replaced by a newer one. Ask for "
    "a new one under “Forgot your password?”."
)
TOO_MANY_LINKS = "You’ve asked for 3 links in the last hour. Try again in an hour."
# Email recovery's limits (spec §4): per address (by its keyed hash) and per IP, per hour; and verify links per
# account per hour. Counted before any lookup, whether or not an account matches.
FORGOT_PER_EMAIL, FORGOT_PER_IP, LINKS_PER_ACCOUNT, HOUR = 3, 10, 3, 3600.0
NO_EMAIL_PER_HOUR = (
    20  # "no verified email" answers an hour, from everyone: the most a scan can confirm per hour
)


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
    """Which Terms and Privacy Policy an account accepted, and when, and its marketing-email choice (spec §7).
    Latest wins; each row carries the other half forward."""

    user_id: str
    terms_version: str
    privacy_version: str
    terms_at: datetime
    source: str  # signup, reaccept or settings
    marketing: bool | None = None  # None: never offered
    marketing_at: datetime | None = None
    marketing_version: str | None = None
    insights: bool | None = None  # counted in roaster insights; None: never changed, which counts as on
    insights_at: datetime | None = None
    insights_version: str | None = None
    experiment: bool | None = None  # joined the "Your next bag" test; None: never asked
    experiment_at: datetime | None = None
    experiment_version: str | None = None


_KEEP = object()  # "leave the marketing choice as it was"


@dataclass(frozen=True)
class EmailView:
    """An account's email as its owner (or the app's owner) sees it."""

    address: str
    verified: bool


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
        email_key: str | None = None,
    ):
        if len(pepper) < 16 or len(session_secret) < 32:
            raise RuntimeError("AUTH_PEPPER (16+ characters) and SESSION_SECRET (32+) must be set")
        self.lake = lake
        self.pepper = pepper.encode()
        self.key = hashlib.sha256(b"session\0" + session_secret.encode()).digest()
        # Emailed reset links are signed, not stored (see email_reset_token), under a key of their own.
        self.reset_key = hashlib.sha256(b"reset\0" + session_secret.encode()).digest()
        self.reset_issued: dict[str, int] = {}  # user id -> when the newest emailed link was made (in memory)
        self.setup_code = setup_code or None
        if legal_version is None:
            from coffeedata.site import LEGAL_UPDATED

            legal_version = LEGAL_UPDATED
        self.legal = legal_version  # the Terms' and the Privacy Policy's "Last updated" (they share one date)
        self.lock = threading.Lock()
        self.limiter = Limiter()
        self.windows = Windows()
        # Email features need EMAIL_KEY; without it they're hidden and nothing about email is stored.
        self.crypto = EmailCrypto(email_key) if email_key else None
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
                            row["source"], row.get("marketing"), row.get("marketing_at"),
                            row.get("marketing_version"), row.get("insights"), row.get("insights_at"),
                            row.get("insights_version"), row.get("experiment"), row.get("experiment_at"),
                            row.get("experiment_version")),
                    row["logged_at"],
                )  # fmt: skip
        self.contacts: dict[str, tuple[Contact, datetime]] = {}
        if "contacts" in lake.tables:
            for row in lake.latest("contacts", "user_id", attempts=STARTUP_READS):
                self.contacts[row["user_id"]] = (
                    Contact(row["user_id"], row["email_hmac"], row["email_enc"], row["verified_at"],
                            row["verify_sha256"], row["verify_expires"]),
                    row["logged_at"],
                )  # fmt: skip
        try:
            self.sweep_contacts()
        except Exception:  # noqa: BLE001 - a start-up tidy-up; the app works without it
            log.exception("sweeping stale contact files failed; the next start tries again")
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

    def _stage_consent(
        self,
        user_id: str,
        source: str,
        marketing: object = _KEEP,
        insights: object = _KEEP,
        experiment: object = _KEEP,
        after: datetime | None = None,
    ) -> tuple[Consent, datetime]:
        """A new acceptance (signup, reaccept) records the current Terms; a choice changed in Setup (`settings`)
        keeps the acceptance it had. The marketing and roaster-insights choices are carried forward unless given."""
        prev = self.consents.get(user_id)
        at = self._next_at(prev)
        if after and at <= after:  # joining the test again after an erasure is recorded after it
            at = after + timedelta(seconds=1)
        if source == "settings":
            # Only a choice changes. An account with no record yet accepted the terms of its day by
            # joining (TERMS_BEFORE_RECORDS), and this mustn't claim it accepted anything newer.
            old = TERMS_BEFORE_RECORDS
            c = (
                replace(prev[0], source=source)
                if prev
                else Consent(user_id, old, old, legal_date(old).replace(tzinfo=UTC), source)
            )
        else:
            c = Consent(user_id, self.legal, self.legal, at, source)
            if prev:
                c = replace(c, marketing=prev[0].marketing, marketing_at=prev[0].marketing_at,
                            marketing_version=prev[0].marketing_version, insights=prev[0].insights,
                            insights_at=prev[0].insights_at, insights_version=prev[0].insights_version,
                            experiment=prev[0].experiment, experiment_at=prev[0].experiment_at,
                            experiment_version=prev[0].experiment_version)  # fmt: skip
        if marketing is not _KEEP:  # None: never offered, so no time or version either (review L2)
            offered = isinstance(marketing, bool)
            c = replace(
                c,
                marketing=marketing if isinstance(marketing, bool) else None,
                marketing_at=at if offered else None,
                marketing_version=self.legal if offered else None,
            )
        if insights is not _KEEP:
            c = replace(c, insights=bool(insights), insights_at=at, insights_version=self.legal)
        if experiment is not _KEEP:
            c = replace(
                c, experiment=bool(experiment), experiment_at=at, experiment_version=EXPERIMENT_VERSION
            )
        row = {
            "user_id": user_id,
            "logged_at": at,
            "terms_version": c.terms_version,
            "privacy_version": c.privacy_version,
            "terms_at": c.terms_at,
            "source": source,
            "marketing": c.marketing,
            "marketing_at": c.marketing_at,
            "marketing_version": c.marketing_version,
            "insights": c.insights,
            "insights_at": c.insights_at,
            "insights_version": c.insights_version,
            "experiment": c.experiment,
            "experiment_at": c.experiment_at,
            "experiment_version": c.experiment_version,
        }
        self.lake.write("consents", f"consents/{user_id}__{at:%Y%m%dT%H%M%S}.parquet", [row])
        return c, at

    def _stage_contact(self, c: Contact) -> tuple[Contact, datetime]:
        at = self._next_at(self.contacts.get(c.user_id))
        row = {
            "user_id": c.user_id,
            "logged_at": at,
            "email_hmac": c.email_hmac,
            "email_enc": c.email_enc,
            "verified_at": c.verified_at,
            "verify_sha256": c.verify_sha256,
            "verify_expires": c.verify_expires,
        }
        self.lake.write("contacts", f"contacts/{c.user_id}__{at:%Y%m%dT%H%M%S}.parquet", [row])
        return c, at

    def _commit(
        self,
        message: str,
        users: tuple[tuple[Account, datetime], ...] = (),
        invites: tuple[tuple[Invite, datetime], ...] = (),
        consents: tuple[tuple[Consent, datetime], ...] = (),
        contacts: tuple[tuple[Contact, datetime], ...] = (),
        delete: list[str] | None = None,
        forget: str | None = None,
    ) -> None:
        """Publish what was staged in one commit, then let the cache see it, so the records of one change (an
        account, its invite marked used and its acceptance) are sent together. If the publish fails they stay
        staged, unseen by the cache, and go with the next publish (Lake.publish); the cache sees them after the
        next restart. `delete` removes files in the same commit, and `forget` drops that account's email from the
        cache (it was removed, not replaced)."""
        self.lake.publish(message, delete=delete or ())
        if forget:
            self.contacts.pop(forget, None)
        for contact, cat in contacts:
            self.contacts[contact.user_id] = (contact, cat)
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

    def _new_account_errors(
        self, username: str, password: str, agree: bool, email: str = ""
    ) -> list[dict[str, str]]:
        """Every rule a new account's details fail, all at once (spec §3). The email is optional."""
        errors = rules.check_username(username)
        if not errors and self.by_name(username):
            errors.append(rules.error("u.taken"))
        if self.crypto:
            errors += rules.check_email(email)
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
                consents=(self._stage_consent(acct.user_id, "signup", None),),
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
        acct = self._reauth(user_id, current)  # counted, like the email changes (final review N3)
        with self.lock:
            acct = self.get(user_id) or acct
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
        ttl: timedelta = timedelta(days=INVITE_DAYS),
    ) -> tuple[Invite, str]:
        """The new code and anything it replaces go in one commit."""
        if kind == "reset" and (for_user_id is None or self.get(for_user_id) is None):
            raise AccountError("No such account.")
        if kind == "reset":
            done = {i.invite_id for i, _ in staged}  # renew has already revoked the one it replaces
            staged += self._revoke_resets(for_user_id or "", done)
        code, now = new_code(), _now()
        inv = Invite(
            invite_id=str(uuid.uuid4()),
            code_sha256=code_hash(code),
            kind=kind,
            created_by=by,
            expires_at=now + ttl,
            for_user_id=for_user_id,
            for_label=for_label,
            created_at=now,
        )
        self._commit(f"app: invites {inv.invite_id}", invites=(*staged, self._stage_invite(inv)))
        return inv, code

    def _revoke_resets(
        self, user_id: str, keep: set[str] | None = None
    ) -> tuple[tuple[Invite, datetime], ...]:
        """Stage a revocation of every open reset code for one account, except those in `keep`."""
        keep = keep or set()
        return tuple(
            self._stage_invite(o.revoke())
            for o, _ in list(self.invites.values())
            if o.kind == "reset" and o.for_user_id == user_id and o.open(_now()) and o.invite_id not in keep
        )

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

    def join(
        self,
        code: str,
        username: str,
        password: str,
        ip: str,
        agree: bool = False,
        email: str = "",
        marketing: bool | None = None,
    ) -> Account:
        return self.join_sending(code, username, password, ip, agree, email, marketing)[0]

    def join_sending(
        self,
        code: str,
        username: str,
        password: str,
        ip: str,
        agree: bool = False,
        email: str = "",
        marketing: bool | None = None,
    ) -> tuple[Account, tuple[str, str] | None]:
        """A new account from a join link, written with the invite marked used, its acceptance of the Terms (and
        marketing choice) and its email, if given, in one commit. Idempotent (spec §11): a retry with the same
        code, username and password, after a reply that never reached the phone, signs that account in instead of
        failing. Also returns the (address, token) of a verify link to send, if there is one."""
        username = clean_username(username)
        if self.limiter.blocked(f"ip:{ip}", MAX_PER_IP, time.monotonic()):
            raise AccountError("Too many tries. Wait 15 minutes and try again.")
        with self.lock:
            hit = self._find(code)
            if not (hit and hit.kind == "join" and hit.used_by):
                return self._join(code, username, password, ip, agree, email, marketing)
        return self._join_again(hit, username, password, ip), None

    def _join(
        self,
        code: str,
        username: str,
        password: str,
        ip: str,
        agree: bool,
        email: str,
        marketing: bool | None,
    ) -> tuple[Account, tuple[str, str] | None]:
        # The code first, and only the code: without a valid join code nothing is said about the username (whether
        # it's taken) or the rest (OWASP: generic replies on account creation too).
        inv = self.lookup(code, ip)
        if inv.kind != "join":
            raise field_error("code", "c.kind", "That’s a reset code. Use it on the reset screen.")
        errors = self._new_account_errors(username, password, agree, email)
        if errors:
            raise AccountError(errors=errors)
        acct = Account(str(uuid.uuid4()), username, self._hash(password), invite_id=inv.invite_id)
        send = None
        contacts: tuple[tuple[Contact, datetime], ...] = ()
        if self.crypto and clean_email(email):
            contact, send = self._new_contact(acct.user_id, clean_email(email))
            contacts = (self._stage_contact(contact),)
        offered = marketing if self.crypto else None  # the box shows only where email does
        self._commit(
            f"app: users {acct.user_id}",
            users=(self._stage(acct),),
            invites=(self._stage_invite(replace(inv, used_by=acct.user_id, used_at=_now())),),
            consents=(self._stage_consent(acct.user_id, "signup", offered),),
            contacts=contacts,
        )
        return acct, send

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
        return self.reset_by(code, password, ip, now)[0]

    def reset_by(self, code: str, password: str, ip: str, now: float | None = None) -> tuple[Account, bool]:
        """A reset code sets a new password, signs out every device and revokes any other open reset code for the
        account. The owner's own account can be reset with OWNER_SETUP_CODE. A blocked address can't try either; a
        wrong code counts against it (in lookup). Also says whether the code came by email (spec §4: that path
        ends on sign-in rather than signing in)."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.limiter.blocked(f"ip:{ip}", MAX_PER_IP, now):
                raise AccountError("Too many tries. Wait 15 minutes and try again.")
            emailed = code.strip().startswith(EMAIL_TOKEN)
            inv = None
            if emailed:
                acct = self._from_email_token(code)
                if acct is None:
                    self.limiter.fail(f"ip:{ip}", now)
                    raise field_error("code", "c.invalid", EMAIL_LINK_USED)
            elif self.setup_code and hmac.compare_digest(code.strip().encode(), self.setup_code.encode()):
                if self._setup_code_spent():
                    raise field_error(
                        "code",
                        "c.spent",
                        "That setup code has been used. Put a new one in OWNER_SETUP_CODE to use it again.",
                    )
                acct = next((a for a in self.people() if a.role == "owner"), None)
                if acct is None:
                    raise AccountError("There’s no owner yet. Create the account first.")
            else:
                inv = self.lookup(code, ip, now)
                acct = self.get(inv.for_user_id or "") if inv.kind == "reset" else None
                if acct is None:
                    raise field_error("code", "c.kind", "That’s an invite, not a reset code.")
                if acct.disabled:
                    raise AccountError("This account is turned off. Ask the owner to turn it back on.")
            check_password(password, acct.username)
            acct = replace(acct, password_hash=self._hash(password), session_epoch=acct.session_epoch + 1)
            # A reset revokes every other way in: the owner's open reset links, and (the password hash they're
            # signed over has changed) every emailed link.
            used: tuple[tuple[Invite, datetime], ...] = ()
            if inv:
                used = (self._stage_invite(replace(inv, used_by=acct.user_id, used_at=_now())),)
            elif not emailed:
                used = (self._stage_invite(self._setup_invite(acct.user_id)),)
            others = self._revoke_resets(acct.user_id, {inv.invite_id} if inv else set())
            self._commit(f"app: users {acct.user_id}", users=(self._stage(acct),), invites=(*used, *others))
            self.limiter.clear(f"u:{acct.username}")
            self.reset_issued.pop(acct.user_id, None)
            return acct, emailed

    def set_disabled(self, by: str, user_id: str, disabled: bool) -> Account:
        """The owner turns an account off (or back on). Off signs it out everywhere."""
        with self.lock:
            self._owner(by)
            acct = self.get(user_id)
            if acct is None or acct.role == "owner":
                raise AccountError("No such account.")
            staged: tuple[tuple[Invite, datetime], ...] = ()
            if disabled:  # an outstanding reset code would otherwise still change the password
                staged = self._revoke_resets(user_id)
            acct = replace(acct, disabled=disabled, session_epoch=acct.session_epoch + 1)
            self._commit(f"app: users {acct.user_id}", users=(self._stage(acct),), invites=staged)
            return acct

    # --- email: verification, recovery and the marketing choice (spec §4, §5, §7.1) ---------------------

    @property
    def email_on(self) -> bool:
        return self.crypto is not None

    def _crypto(self) -> EmailCrypto:
        if self.crypto is None:
            raise AccountError("Email isn’t set up on this app.", status=404)
        return self.crypto

    def _verified_with(self, email_hmac: str, but: str | None = None) -> Contact | None:
        return next(
            (c for c, _ in list(self.contacts.values())
             if c.verified_at and c.user_id != but and hmac.compare_digest(c.email_hmac, email_hmac)),
            None,
        )  # fmt: skip

    def _new_contact(self, user_id: str, email: str) -> tuple[Contact, tuple[str, str] | None]:
        """An unverified email with a fresh verify link, and the (address, token) to send. An address already
        verified on another account is kept unverified with no link, and nothing is sent: the reply looks the same
        and that account's owner hears nothing (no enumeration)."""
        crypto = self._crypto()
        h = crypto.lookup(email)
        sealed = crypto.seal(email, user_id)
        if self._verified_with(h, but=user_id):
            return Contact(user_id, h, sealed), None
        token = new_token()
        expires = _now() + timedelta(hours=VERIFY_HOURS)
        return Contact(user_id, h, sealed, None, token_hash(token), expires), (email, token)

    def email(self, user_id: str) -> EmailView | None:
        """The account's email, decrypted, for that person or the app's owner only (callers check who asks). One
        that won't open with this EMAIL_KEY (sealed under another key: RUNNING.md, "Rotating keys") counts as no
        email, logged by user id, rather than failing the page (review M1)."""
        hit = self.contacts.get(user_id)
        if hit is None or self.crypto is None:
            return None
        try:
            address = self.crypto.open(hit[0].email_enc, user_id)
        except Exception:  # noqa: BLE001 - cryptography's InvalidTag, or a damaged row
            log.error("an email doesn't open with this EMAIL_KEY", extra={"fields": {"user_id": user_id}})
            return None
        return EmailView(address, hit[0].verified_at is not None)

    def verified_email(self, user_id: str) -> str | None:
        view = self.email(user_id)
        return view.address if view and view.verified else None

    def _drop_squash(self, user_id: str, tries: int = 3) -> None:
        """After an email's files are deleted: drop the old commits too, so it can't be read from history. Tried
        3 times, 2 and 4 s apart; if it still fails it's logged at ERROR, which the "errors" alert emails, and the
        next removal's squash covers it (review M6). With encryption it's defence in depth, so the person isn't
        shown an error."""
        for attempt in range(tries):
            try:
                self.lake.squash(f"app: squash after contacts {user_id}")
                return
            except Exception:  # noqa: BLE001 - the deletion itself has happened
                if attempt == tries - 1:
                    log.exception("squashing the users dataset's history failed; it still holds old contacts")
                else:
                    time.sleep(2 ** (attempt + 1))

    def _reauth(self, user_id: str, password: str, now: float | None = None) -> Account:
        """The current password, for a change that would hand the account to whoever made it (OWASP Authentication:
        re-authenticate before a sensitive change; review M3). Wrong ones are counted: past REAUTH_TRIES in 15
        minutes, it refuses before checking, so a stolen session can't guess at hash speed (final review N3)."""
        now = time.monotonic() if now is None else now
        key = f"reauth:u:{user_id}"
        if self.limiter.blocked(key, REAUTH_TRIES, now):
            raise field_error(
                "current", "current", "Too many wrong passwords. Wait 15 minutes and try again.", 429
            )
        acct = self.get(user_id)
        if acct is None or not self._verify(acct.password_hash, password):
            self.limiter.fail(key, now)
            raise field_error("current", "current", "Your current password isn’t right.")
        return acct

    def set_email(
        self, user_id: str, email: str, password: str, now: float | None = None
    ) -> tuple[tuple[str, str] | None, str | None]:
        """Add or change the account's email, with the current password. A change deletes every file of the old
        record in the same commit, and isn't used until the new address is verified. Returns the verify link to
        send, if any, and the previous verified address, which is told of the change. Counts against the account's
        3 links an hour."""
        self._crypto()
        failed = rules.check_email(email, required=True)
        if failed:
            raise AccountError(errors=failed)
        address = clean_email(email)
        now = time.monotonic() if now is None else now
        self._reauth(user_id, password)
        # Listed before the lock (a remote listing takes a moment, and sign-ins wait on the lock; review L5), then
        # listed again under it if the record changed meanwhile, so no version is left behind (final review N4).
        seen = self.contacts.get(user_id)
        old = self.lake.record_files("contacts", user_id) if seen else []
        with self.lock:
            if self.contacts.get(user_id) is not seen:
                old = self.lake.record_files("contacts", user_id)
            current = self.email(user_id)
            if current and current.verified and current.address.lower() == address.lower():
                return None, None  # nothing changes
            if not self.windows.take({f"verify:u:{user_id}": (LINKS_PER_ACCOUNT, HOUR)}, now):
                raise field_error("email", "e.limit", TOO_MANY_LINKS, 429)
            contact, send = self._new_contact(user_id, address)
            self._commit(f"app: contacts {user_id}", contacts=(self._stage_contact(contact),), delete=old)
        if old:
            self._drop_squash(user_id)
        return send, current.address if current and current.verified else None

    def resend_verify(self, user_id: str, now: float | None = None) -> tuple[str, str] | None:
        """ "Send again": a new verify link for an email not verified yet (the old link stops working)."""
        crypto = self._crypto()
        now = time.monotonic() if now is None else now
        with self.lock:
            hit = self.contacts.get(user_id)
            if hit is None or hit[0].verified_at:
                raise AccountError("There’s no email waiting to be verified.")
            if not self.windows.take({f"verify:u:{user_id}": (LINKS_PER_ACCOUNT, HOUR)}, now):
                raise AccountError(TOO_MANY_LINKS, status=429)
            address = crypto.open(hit[0].email_enc, user_id)
            contact, send = self._new_contact(user_id, address)
            self._commit(f"app: contacts {user_id}", contacts=(self._stage_contact(contact),))
        return send

    def verify_email(self, token: str, ip: str, now: float | None = None) -> Account:
        """Open a verify link. Works once, within VERIFY_HOURS. A wrong or old token counts against the address
        like a wrong code, and gets one message whatever the reason."""
        self._crypto()
        now = time.monotonic() if now is None else now
        key = f"ip:{ip}"
        expired = field_error("token", "v.expired", VERIFY_EXPIRED)
        with self.lock:
            if self.limiter.blocked(key, MAX_PER_IP, now):
                raise AccountError("Too many tries. Wait 15 minutes and try again.")
            h = token_hash(token) if token.strip() else ""
            hit = next(
                (c for c, _ in list(self.contacts.values())
                 if h and c.verify_sha256 and hmac.compare_digest(c.verify_sha256, h)),
                None,
            )  # fmt: skip
            acct = self.get(hit.user_id) if hit else None
            if hit is None or acct is None or not hit.verify_expires or _now() >= hit.verify_expires:
                self.limiter.fail(key, now)
                raise expired
            if self._verified_with(hit.email_hmac, but=hit.user_id):  # verified elsewhere meanwhile
                raise expired
            done = replace(hit, verified_at=_now(), verify_sha256=None, verify_expires=None)
            self._commit(f"app: contacts {hit.user_id}", contacts=(self._stage_contact(done),))
            return acct

    def remove_email(self, by: str, user_id: str, password: str | None = None) -> str | None:
        """The person themselves, with their current password, or the app's owner. Every file of the record goes,
        in one commit, then the dataset's history is squashed. Returns the address if it was verified, to be told
        of the removal (review M3)."""
        if by != user_id:
            self._owner(by)
        else:
            self._reauth(user_id, password or "")
        seen = self.contacts.get(user_id)
        if seen is None:
            return None
        current = self.email(user_id)
        files = self.lake.record_files("contacts", user_id)  # outside the lock (review L5) …
        with self.lock:
            if (
                self.contacts.get(user_id) is not seen
            ):  # … and again if it changed meanwhile (final review N4)
                files = self.lake.record_files("contacts", user_id)
            self._commit(f"app: contacts {user_id} removed", delete=files, forget=user_id)
        self._drop_squash(user_id)
        return current.address if current and current.verified else None

    def forgot(self, who: str, ip: str, now: float | None = None) -> tuple[str, Account | None]:
        """ "Forgot your password?" with a username or an email (owner decision, 2026-11-08). The answer:
        - "send": the account has a verified email; the caller sends a link (`reset_link`);
        - "no_email": the account exists and has no verified email (none, or one not confirmed yet), said on the
          form so the person knows to ask whoever invited them. That tells the asker the account exists: the
          owner's trade-off for an invite-only app, against OWASP's uniform reply;
        - "quiet": no such account, or a turned-off one. The same reply as "send", and nothing goes;
        - "limited": past a limit. The same words again, answered at once (main.post_forgot), and nothing goes.

        Limits (review M5: what they do and don't do). Before any lookup: 3 an hour per name or address asked
        about, and 10 an hour per IP. After it: 3 an hour per account, however it was named (review L1), and at
        most NO_EMAIL_PER_HOUR "no_email" answers an hour in all. They're in memory, so each cold start counts
        afresh: they stop a quick run of guesses from one place, and cap how many accounts a scan can confirm per
        instance-hour, but not a slow scan spread over many addresses and many hours."""
        now = time.monotonic() if now is None else now
        text = nfc(who).strip(rules.TRIM)
        if not text:
            return "quiet", None
        by_email = "@" in text
        if by_email:
            if self.crypto is None or rules.check_email(text):
                return "quiet", None
            target = f"forgot:e:{self.crypto.lookup(clean_email(text))}"
        else:
            target = f"forgot:u:{clean_username(text)}"
        if not self.windows.take(
            {target: (FORGOT_PER_EMAIL, HOUR), f"forgot:ip:{ip}": (FORGOT_PER_IP, HOUR)}, now
        ):
            return "limited", None
        if by_email:
            assert self.crypto is not None
            h = self.crypto.lookup(clean_email(text))
            # A verified address finds its account. An address not confirmed yet finds its account too (review
            # L8), so it gets the same "no verified email" answer as the username would.
            hit = self._verified_with(h) or next(
                (c for c, _ in list(self.contacts.values()) if hmac.compare_digest(c.email_hmac, h)), None
            )
            acct = self.get(hit.user_id) if hit else None
        else:
            acct = self.by_name(text)
        # The owner's account recovers with OWNER_SETUP_CODE only, never by email (owner decision, final review
        # N9): like a turned-off account, the same reply as anyone's, and nothing is sent.
        if acct is None or acct.disabled or acct.role == "owner":
            return "quiet", None
        if not self.windows.take({f"forgot:a:{acct.user_id}": (FORGOT_PER_EMAIL, HOUR)}, now):
            return "limited", None
        if self.verified_email(acct.user_id):
            return "send", acct
        if not self.windows.take({"forgot:no_email": (NO_EMAIL_PER_HOUR, HOUR)}, now):
            return "limited", None
        return "no_email", acct

    def reset_link(self, user_id: str) -> tuple[str, Account, str, int] | None:
        """A 30-minute reset link for an account with a verified email: the (address, account, token) to send.
        Nothing is written: the token is signed (email_reset_token), so asking costs no commit, and an anonymous
        request never touches the owner's reset links (review M4)."""
        acct = self.get(user_id)
        address = self.verified_email(user_id)
        if acct is None or acct.disabled or acct.role == "owner" or address is None:
            return None
        token, issued = self.email_reset_token(acct)
        return address, acct, token, issued

    # --- emailed reset links: signed, not stored ------------------------------------------------------

    def _tag(self, user_id: str) -> bytes:
        return hmac.new(self.reset_key, b"tag\0" + user_id.encode(), hashlib.sha256).digest()[:8]

    def _token_mac(self, acct: Account, tag: bytes, issued: int) -> bytes:
        """Over the account's password hash and session epoch (a reset ends every link) and its verified email's
        lookup hash (changing or removing the email ends them too, even across a restart: final review N2)."""
        hit = self.contacts.get(acct.user_id)
        email = hit[0].email_hmac if hit and hit[0].verified_at else ""
        body = (
            tag
            + issued.to_bytes(6, "big")
            + acct.password_hash.encode()
            + str(acct.session_epoch).encode()
            + b"\0"
            + email.encode()
        )
        return hmac.new(self.reset_key, b"mac\0" + body, hashlib.sha256).digest()[:16]

    def email_reset_token(self, acct: Account, now: float | None = None) -> tuple[str, int]:
        """A reset link's token: which account (an 8-byte keyed tag, not the id), when it was made, and a 128-bit
        MAC over those and the account's current password hash. It lasts RESET_MINUTES; it works once, because
        using it changes the password hash; and a newer one replaces it (reset_issued: in memory, so after a
        restart an older link works again until its 30 minutes are up)."""
        # In milliseconds, and always after the last one, so two links asked for in one second still order.
        ms = int((time.time() if now is None else now) * 1000)
        issued = max(ms, self.reset_issued.get(acct.user_id, 0) + 1)
        tag = self._tag(acct.user_id)
        return EMAIL_TOKEN + _b64(
            tag + issued.to_bytes(6, "big") + self._token_mac(acct, tag, issued)
        ), issued

    def link_sent(self, user_id: str, issued: int) -> None:
        """A link went out: older ones stop working. Only once it's sent (final review N2), so a failed send
        doesn't end the member's previous link with nothing to replace it."""
        self.reset_issued[user_id] = max(issued, self.reset_issued.get(user_id, 0))

    def _from_email_token(self, token: str, now: float | None = None) -> Account | None:
        try:
            raw = _unb64(token.strip()[len(EMAIL_TOKEN) :])
        except ValueError:
            return None
        if len(raw) != 30:
            return None
        tag, issued, mac = raw[:8], int.from_bytes(raw[8:14], "big"), raw[14:]
        now_ms = (time.time() if now is None else now) * 1000
        if not issued <= now_ms + 60_000 or now_ms - issued > RESET_MINUTES * 60_000:
            return None
        acct = next((a for a in self.people() if hmac.compare_digest(self._tag(a.user_id), tag)), None)
        if (
            acct is None
            or acct.disabled
            or acct.role == "owner"
            or issued < self.reset_issued.get(acct.user_id, 0)
        ):
            return None
        return acct if hmac.compare_digest(self._token_mac(acct, tag, issued), mac) else None

    def rekey(self, new_key: str) -> int:
        """Rotate EMAIL_KEY (docs/app/RUNNING.md, "Email"): every email re-sealed and re-hashed under the new key,
        written with the old files deleted in one commit, then the history squashed so nothing sealed under the old
        key stays readable. Verify links in flight keep working. Returns how many were re-sealed."""
        old, new = self._crypto(), EmailCrypto(new_key)
        delete = self.lake.record_files("contacts")  # one listing for all of them (review L5)
        with self.lock:
            fresh = []
            for uid, (c, _) in list(self.contacts.items()):
                address = old.open(c.email_enc, uid)
                fresh.append(replace(c, email_hmac=new.lookup(address), email_enc=new.seal(address, uid)))
            self.crypto = new
            staged = tuple(self._stage_contact(c) for c in fresh)
            if staged or delete:
                self._commit("app: contacts re-sealed under a new EMAIL_KEY", contacts=staged, delete=delete)
        if delete:
            self._drop_squash("rekey")
        return len(fresh)

    def sweep_contacts(self) -> list[str]:
        """At start-up: delete any contact file left from an older email of a member (a different lookup hash
        from the current record's), then squash. A change normally deletes those in its own commit; this catches
        one whose delete didn't land before the instance stopped, since pending deletes only live in staging
        (final review N11). Returns the files deleted."""
        if "contacts" not in self.lake.tables or not self.contacts:
            return []
        stale = sorted(
            r["file"]
            for r in self.lake.file_rows("contacts", ("user_id", "email_hmac"))
            if r["user_id"] in self.contacts and r["email_hmac"] != self.contacts[r["user_id"]][0].email_hmac
        )
        if stale:
            self.lake.publish("app: delete contact files left from changed emails", delete=stale)
            self._drop_squash("sweep")
            log.warning(
                "deleted contact files left from changed emails", extra={"fields": {"files": len(stale)}}
            )
        return stale

    def unreadable(self) -> list[str]:
        """User ids whose email doesn't open with the current key (should be none)."""
        crypto = self._crypto()
        out = []
        for uid, (c, _) in list(self.contacts.items()):
            try:
                crypto.open(c.email_enc, uid)
            except Exception:  # noqa: BLE001 - cryptography's InvalidTag
                out.append(uid)
        return out

    def marketing(self, user_id: str) -> bool | None:
        hit = self.consents.get(user_id)
        return hit[0].marketing if hit else None

    def set_marketing(self, user_id: str, on: bool) -> None:
        """The marketing-email box in Setup: recorded with when, and the Privacy Policy version in force."""
        with self.lock:
            if self.get(user_id) is None:
                raise AccountError("No such account.")
            self._commit(f"app: consents {user_id}", consents=(self._stage_consent(user_id, "settings", on),))

    def insights(self, user_id: str) -> bool:
        """Whether this member's shots may be counted in roaster insights: on unless they turned it off."""
        hit = self.consents.get(user_id)
        return not (hit and hit[0].insights is False)

    def set_insights(self, user_id: str, on: bool) -> None:
        """ "Include my shots in roaster insights" in Setup: recorded with when, and the Privacy Policy version."""
        with self.lock:
            if self.get(user_id) is None:
                raise AccountError("No such account.")
            staged = self._stage_consent(user_id, "settings", insights=on)
            self._commit(f"app: consents {user_id}", consents=(staged,))

    def experiment(self, user_id: str) -> bool:
        """Whether this member is in the "Your next bag" test: only once they've joined it, and not after leaving."""
        hit = self.consents.get(user_id)
        return bool(hit and hit[0].experiment is True)

    def experiment_since(self, user_id: str) -> datetime | None:
        """When this member last joined the test, while they're in it; None otherwise."""
        hit = self.consents.get(user_id)
        return hit[0].experiment_at if hit and hit[0].experiment is True else None

    def set_experiment(self, user_id: str, on: bool, after: datetime | None = None) -> None:
        """Joining or leaving the test: recorded with when, and the wording of the consent (EXPERIMENT_VERSION).
        `after`: the person's latest erasure of their test records, which a joining must come after."""
        with self.lock:
            if self.get(user_id) is None:
                raise AccountError("No such account.")
            staged = self._stage_consent(user_id, "settings", experiment=on, after=after)
            self._commit(f"app: consents {user_id}", consents=(staged,))

    def insights_excluded(self) -> set[str]:
        """User ids whose shots the roaster-insights export must leave out, read when it runs. There's no export
        job yet: when there is, it reads this (or the latest `consents` row per user, `insights = false`)."""
        return {uid for uid, (c, _) in list(self.consents.items()) if c.insights is False}

    def marketing_recipients(self) -> list[tuple[str, str]]:
        """(user_id, address) of everyone a marketing email may go to, read at the time of sending: the box ticked,
        a verified email, and the account on."""
        out = []
        for acct in self.people():
            if acct.disabled or self.marketing(acct.user_id) is not True:
                continue
            address = self.verified_email(acct.user_id)
            if address:
                out.append((acct.user_id, address))
        return out

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
