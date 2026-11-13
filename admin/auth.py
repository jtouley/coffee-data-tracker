"""Passkey-only sign-in for the admin (docs/design/admin-registry.md §5.3).

- **Enrollment, once.** While no passkey exists, the owner proves they hold the one-shot enrollment token (the
  ADMIN_ENROLL_TOKEN secret) and registers the first passkey. That registration, its recovery codes' hashes and the
  token's hash are one record, created only if it doesn't exist (`enroll/spent`), so enrollment closes for good the
  moment it succeeds, whatever the secret still holds.
- **The second passkey** is added from a session at most FRESH_S old, which in practice is the enrollment session.
  Any passkey after that needs a session and a recovery code.
- **Recovery:** a recovery code (each works once) registers a new passkey, retires every earlier passkey (recovery
  means they are lost) and signs out every session.
- **Sessions** last SESSION_S (12 hours) from sign-in and never renew. Each is a server-side record; the cookie
  carries its random id, signed with the session secret, so a cookie made under another secret is refused before
  the store is asked. Sign-out revokes the session server-side; "sign out everywhere" revokes all of them.

Everything fails closed: a store that can't be read means no session is trusted (AuthUnavailable, a 503).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .store import Store, StoreError

SESSION_S = 12 * 3600
FRESH_S = 10 * 60  # a session this young may add the second passkey
CEREMONY_S = 300  # a WebAuthn challenge's lifetime
RECOVERY_CODES = 10
MIN_SECRET = 32  # characters, for the session secret and the enrollment token
USER_ID = b"admin.registry.coffee owner"  # one user: the owner
CACHE_S = 30  # how long a checked session is trusted before revocations are read again
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class AuthError(Exception):
    """A sign-in step was refused. `message` is safe to show; it never says which secret check failed."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


class AuthUnavailable(Exception):
    """The sign-in state couldn't be read or written: fail closed (503)."""


def _now() -> float:
    return time.time()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def new_recovery_codes(n: int = RECOVERY_CODES) -> list[str]:
    """Crockford base32, 16 characters (80 bits) in four groups, like the app's invite codes."""
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(CROCKFORD) for _ in range(16))
        out.append("-".join(raw[i : i + 4] for i in range(0, 16, 4)))
    return out


def normalize_code(code: str) -> str:
    """As the app reads an invite code: any case, with or without dashes, O as 0 and I or L as 1."""
    c = "".join(ch for ch in code.upper() if ch.isalnum())
    c = c.replace("O", "0").replace("I", "1").replace("L", "1")
    return "-".join(c[i : i + 4] for i in range(0, len(c), 4))


@dataclass
class Credential:
    credential_id: str  # base64url
    public_key: str  # base64url
    sign_count: int
    created_at: str
    via: str  # enroll, session, recovery


@dataclass
class Session:
    key: str  # sha256 of the session id
    created_at: float
    expires_at: float
    credential_id: str


@dataclass
class Ceremony:
    challenge: bytes
    kind: str  # enroll, add, signin, recover
    expires_at: float
    extra: dict = field(default_factory=dict)


class Limiter:
    """At most `n` events per `window_s` per key, in memory (one instance)."""

    def __init__(self, n: int, window_s: float):
        self.n, self.window = n, window_s
        self.events: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def _live(self, key: str, now: float) -> deque[float]:
        q = self.events.setdefault(key, deque())
        while q and q[0] < now - self.window:
            q.popleft()
        if len(self.events) > 10_000:  # forget keys with nothing in the window
            for k in [k for k, v in self.events.items() if not v and k != key]:
                del self.events[k]
        return q

    def hit(self, key: str) -> bool:
        """Count one event; False (and not counted) if the key is already at its limit."""
        now = _now()
        with self.lock:
            q = self._live(key, now)
            if len(q) >= self.n:
                return False
            q.append(now)
            return True

    def full(self, key: str) -> bool:
        """Whether the key is at its limit, without counting anything."""
        with self.lock:
            return len(self._live(key, _now())) >= self.n


class Auth:
    def __init__(
        self,
        store: Store,
        session_secret: str,
        enroll_token: str | None,
        rp_id: str,
        origin: str,
        rp_name: str = "admin.registry.coffee",
    ):
        if len(session_secret) < MIN_SECRET:
            raise ValueError("the session secret is too short")
        self.store, self.rp_id, self.origin, self.rp_name = store, rp_id, origin, rp_name
        self._secret = hashlib.sha256(b"admin-session\0" + session_secret.encode()).digest()
        # Only a token long enough to be a real secret can enroll; a short or missing one leaves enrollment shut.
        self._enroll_hash = sha256(enroll_token) if enroll_token and len(enroll_token) >= MIN_SECRET else None
        self.ceremonies: dict[str, Ceremony] = {}
        self.sessions: dict[str, tuple[Session, float]] = {}  # key -> (session, checked_at)
        self.lock = threading.Lock()
        self.add_lock = threading.Lock()  # one passkey registration decided at a time
        self.begin_limit = Limiter(30, 900)  # ceremonies started per client IP
        self.fail_limit = Limiter(10, 900)  # refused tokens, codes and assertions per client IP
        # Refused enrollment tokens and recovery codes from anywhere. Never passkey sign-ins: anyone can send those,
        # and counting them here would let a stranger lock the owner's own passkey out.
        self.global_fail = Limiter(50, 900)

    # --- state --------------------------------------------------------------------------------------------

    def _read(self, fn, *args):
        try:
            return fn(*args)
        except StoreError as e:
            raise AuthUnavailable(str(e)) from e

    def enrollment(self) -> dict | None:
        return self._read(self.store.get, "enroll/spent")

    def credentials(self) -> dict[str, Credential]:
        out: dict[str, Credential] = {}
        spent = self.enrollment()
        if spent:
            c = spent["credential"]
            out[c["credential_id"]] = Credential(**c)
        for key in self._read(self.store.list, "credentials/"):
            rec = self._read(self.store.get, key)
            if rec:
                out[rec["credential_id"]] = Credential(**rec)
        for key in self._read(self.store.list, "retired/"):  # passkeys a recovery retired
            out.pop(key.rsplit("/", 1)[1], None)
        for cid in out:  # the highest counter any use recorded (names: uses/<id>/<count>-<time>-<rand>)
            uses = self._read(self.store.list, f"uses/{cid}/")
            if uses:
                out[cid].sign_count = max(int(u.rsplit("/", 1)[1].split("-")[0]) for u in uses)
        return out

    def enroll_open(self) -> bool:
        """Enrollment is open only before the first passkey, and only with a real token configured."""
        return self._enroll_hash is not None and self.enrollment() is None

    def _recovery_hashes(self) -> set[str]:
        """The recovery codes made at enrollment (hashes). Issuing a new set comes with the write API."""
        spent = self.enrollment()
        return set(spent["recovery"]["hashes"]) if spent else set()

    def recovery_left(self) -> int:
        used = {k.rsplit("/", 1)[1] for k in self._read(self.store.list, "recovery-used/")}
        return len(self._recovery_hashes() - used)

    def _code_ok(self, code: str) -> str | None:
        h = sha256(normalize_code(code))
        if h in self._recovery_hashes() and self._read(self.store.get, f"recovery-used/{h}") is None:
            return h
        return None

    # --- ceremonies ---------------------------------------------------------------------------------------

    def _ceremony(self, kind: str, extra: dict | None = None) -> tuple[str, bytes]:
        cid, challenge = secrets.token_urlsafe(24), secrets.token_bytes(32)
        now = _now()
        with self.lock:
            for k in [k for k, c in self.ceremonies.items() if c.expires_at < now]:
                del self.ceremonies[k]
            while len(self.ceremonies) >= 1000:  # a flood evicts the oldest, never refuses everyone
                del self.ceremonies[min(self.ceremonies, key=lambda k: self.ceremonies[k].expires_at)]
            self.ceremonies[cid] = Ceremony(challenge, kind, now + CEREMONY_S, extra or {})
        return cid, challenge

    def _take(self, ceremony_id: str, kind: str) -> Ceremony:
        with self.lock:
            c = self.ceremonies.pop(ceremony_id or "", None)
        if c is None or c.kind != kind or c.expires_at < _now():
            raise AuthError("That sign-in step expired. Start again.")
        return c

    def _limited(self, ip: str) -> None:
        if not self.begin_limit.hit(ip):
            raise AuthError("Too many attempts. Try again in 15 minutes.", 429)

    def _not_blocked(self, ip: str, secret: bool = True) -> None:
        """Refuse before anything is checked once this IP failed too often, or (for a token or recovery code,
        `secret`) once everyone did. A passkey sign-in (secret=False) is limited per IP only."""
        if self.fail_limit.full(ip) or (secret and self.global_fail.full("*")):
            raise AuthError("Too many attempts. Try again in 15 minutes.", 429)

    def _failed(self, ip: str, secret: bool = True) -> None:
        if not self.fail_limit.hit(ip) or (secret and not self.global_fail.hit("*")):
            raise AuthError("Too many attempts. Try again in 15 minutes.", 429)

    def _registration(self, kind: str, extra: dict | None = None) -> dict:
        cid, challenge = self._ceremony(kind, extra)
        # Recovery retires every passkey registered now, so it excludes none: the device holding one of them may
        # register its new passkey.
        held = [] if kind == "recover" else list(self.credentials())
        exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c)) for c in held]
        opts = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self.rp_name,
            user_id=USER_ID,
            user_name="owner",
            user_display_name="registry.coffee owner",
            challenge=challenge,
            exclude_credentials=exclude,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return {"ceremony": cid, "options": options_to_json(opts)}

    def _verify_registration(self, c: Ceremony, credential: dict, via: str) -> Credential:
        if not isinstance(credential, dict):  # a number, a boolean, a list: a bad request, not an error
            raise AuthError("The passkey couldn't be registered. Try again.")
        try:
            v = verify_registration_response(
                credential=credential,
                expected_challenge=c.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, KeyError, TypeError) as e:
            raise AuthError("The passkey couldn't be registered. Try again.") from e
        return Credential(
            credential_id=bytes_to_base64url(v.credential_id),
            public_key=bytes_to_base64url(v.credential_public_key),
            sign_count=v.sign_count,
            created_at=_iso(_now()),
            via=via,
        )

    # Enrollment: the first passkey ------------------------------------------------------------------------

    def enroll_begin(self, token: str, ip: str) -> dict:
        self._limited(ip)
        self._not_blocked(ip)
        if not self.enroll_open():
            raise AuthError("Enrollment is closed: a passkey is already registered.", 403)
        assert self._enroll_hash is not None
        if not hmac.compare_digest(sha256(token or ""), self._enroll_hash):
            self._failed(ip)
            raise AuthError("That isn't the enrollment token.", 403)
        return self._registration("enroll")

    def enroll_finish(self, ceremony: str, credential: dict) -> tuple[str, list[str]]:
        """The first passkey, its recovery codes and the spent token as one record. Returns (cookie, codes)."""
        c = self._take(ceremony, "enroll")
        cred = self._verify_registration(c, credential, "enroll")
        codes = new_recovery_codes()
        record = {
            "spent_at": _iso(_now()),
            "token_sha256": self._enroll_hash,
            "credential": cred.__dict__,
            "recovery": {"created_at": _iso(_now()), "hashes": [sha256(normalize_code(x)) for x in codes]},
        }
        if not self._read(self.store.create, "enroll/spent", record):
            raise AuthError("Enrollment is closed: a passkey is already registered.", 403)
        return self.start_session(cred.credential_id), codes

    # More passkeys ----------------------------------------------------------------------------------------

    def add_rule(self, session: Session) -> str:
        """What adding a passkey needs now: 'fresh' (this session, if young enough), 'code', or 'stale'."""
        if len(self.credentials()) >= 2:
            return "code"
        return "fresh" if _now() - session.created_at <= FRESH_S else "stale"

    def add_begin(self, session: Session, code: str | None, ip: str) -> dict:
        self._limited(ip)
        self._not_blocked(ip)
        rule = self.add_rule(session)
        if rule == "stale":
            raise AuthError("Sign in again, then add the passkey within 10 minutes.", 403)
        extra = {}
        if rule == "code":
            h = self._code_ok(code or "")
            if h is None:
                self._failed(ip)
                raise AuthError("That recovery code isn't valid, or it was used.", 403)
            extra["code"] = h
        return self._registration("add", extra | {"session": session.key})

    def add_finish(self, session: Session, ceremony: str, credential: dict) -> Credential:
        c = self._take(ceremony, "add")
        if c.extra.get("session") != session.key:
            raise AuthError("That sign-in step expired. Start again.")
        cred = self._verify_registration(c, credential, "session")
        with self.add_lock:  # the rule again, now: another registration may have finished meanwhile
            rule = self.add_rule(session)
            if rule == "stale":
                raise AuthError("Sign in again, then add the passkey within 10 minutes.", 403)
            h = c.extra.get("code")
            if rule == "code" and not h:
                raise AuthError("Two passkeys are registered now: another needs a recovery code.", 403)
            if h and not self._read(
                self.store.create, f"recovery-used/{h}", {"used_at": _iso(_now()), "for": "add"}
            ):
                raise AuthError("That recovery code was just used.", 403)
            self._read(self.store.create, f"credentials/{cred.credential_id}", cred.__dict__)
        return cred

    # Recovery ---------------------------------------------------------------------------------------------

    def recover_begin(self, code: str, ip: str) -> dict:
        self._limited(ip)
        self._not_blocked(ip)
        if self.enrollment() is None:
            raise AuthError("Nothing to recover: no passkey is registered yet.", 403)
        h = self._code_ok(code or "")
        if h is None:
            self._failed(ip)
            raise AuthError("That recovery code isn't valid, or it was used.", 403)
        return self._registration("recover", {"code": h})

    def recover_finish(self, ceremony: str, credential: dict) -> str:
        c = self._take(ceremony, "recover")
        cred = self._verify_registration(c, credential, "recovery")
        h = c.extra["code"]
        if not self._read(
            self.store.create, f"recovery-used/{h}", {"used_at": _iso(_now()), "for": "recover"}
        ):
            raise AuthError("That recovery code was just used.", 403)
        with self.add_lock:
            earlier = list(self.credentials())
            self._read(self.store.create, f"credentials/{cred.credential_id}", cred.__dict__)
            for cid in earlier:  # recovery means they are lost: none of them signs in again
                self._read(self.store.create, f"retired/{cid}", {"at": _iso(_now()), "by": "recover"})
        self.revoke_all()
        return self.start_session(cred.credential_id)

    # Sign-in ----------------------------------------------------------------------------------------------

    def signin_begin(self, ip: str) -> dict:
        self._limited(ip)
        cid, challenge = self._ceremony("signin")
        # A discoverable credential: no list of ids is handed to whoever asks.
        opts = generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge, user_verification=UserVerificationRequirement.REQUIRED
        )
        return {"ceremony": cid, "options": options_to_json(opts)}

    def signin_finish(self, ceremony: str, credential: dict, ip: str) -> str:
        self._not_blocked(ip, secret=False)
        c = self._take(ceremony, "signin")
        raw = credential.get("rawId") if isinstance(credential, dict) else None
        cred = self.credentials().get(raw) if isinstance(raw, str) else None
        if cred is None:
            self._failed(ip, secret=False)
            raise AuthError(
                "That passkey isn't registered here. Use your other passkey, or a recovery code.", 403
            )
        try:
            v = verify_authentication_response(
                credential=credential,
                expected_challenge=c.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                credential_public_key=base64url_to_bytes(cred.public_key),
                credential_current_sign_count=cred.sign_count,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, KeyError, TypeError) as e:
            self._failed(ip, secret=False)
            raise AuthError("The passkey didn't sign in. Try again.", 403) from e
        stamp = f"{v.new_sign_count:012d}-{int(_now())}-{secrets.token_hex(4)}"
        self._read(self.store.create, f"uses/{cred.credential_id}/{stamp}", {"at": _iso(_now())})
        return self.start_session(cred.credential_id)

    # Sessions ---------------------------------------------------------------------------------------------

    def _sign(self, sid: str) -> str:
        return (
            base64.urlsafe_b64encode(hmac.new(self._secret, sid.encode(), "sha256").digest())
            .decode()
            .rstrip("=")
        )

    def start_session(self, credential_id: str) -> str:
        sid = secrets.token_urlsafe(32)
        now = _now()
        s = Session(sha256(sid), now, now + SESSION_S, credential_id)
        rec = {"created_at": s.created_at, "expires_at": s.expires_at, "credential_id": credential_id}
        self._read(self.store.create, f"sessions/{s.key}", rec)
        with self.lock:
            self.sessions[s.key] = (s, now)
        return f"{sid}.{self._sign(sid)}"

    def _revoked_all_after(self) -> float:
        marks = self._read(self.store.list, "revoke-all/")
        return max((float(k.rsplit("/", 1)[1].split("-")[0]) for k in marks), default=0.0)

    def session(self, cookie: str | None) -> Session | None:
        """The live session a cookie names, or None. Raises AuthUnavailable when the store can't say."""
        if not cookie or cookie.count(".") != 1:
            return None
        sid, sig = cookie.split(".")
        if not hmac.compare_digest(sig, self._sign(sid)):
            return None  # not signed with this service's secret
        key, now = sha256(sid), _now()
        with self.lock:
            hit = self.sessions.get(key)
        if hit and hit[0].expires_at > now and now - hit[1] < CACHE_S:
            return hit[0]
        rec = self._read(self.store.get, f"sessions/{key}")
        if not rec:
            return None
        s = Session(key, float(rec["created_at"]), float(rec["expires_at"]), rec["credential_id"])
        if s.expires_at <= now or self._read(self.store.get, f"revoked/{key}") is not None:
            self._forget(key)
            return None
        if s.created_at <= self._revoked_all_after():
            self._forget(key)
            return None
        with self.lock:
            self.sessions[key] = (s, now)
        return s

    def _forget(self, key: str) -> None:
        with self.lock:
            self.sessions.pop(key, None)

    def revoke(self, session: Session) -> None:
        self._read(self.store.create, f"revoked/{session.key}", {"at": _iso(_now())})
        self._forget(session.key)

    def revoke_all(self) -> None:
        now = _now()
        self._read(self.store.create, f"revoke-all/{now:.6f}-{secrets.token_hex(4)}", {"at": _iso(now)})
        with self.lock:
            self.sessions.clear()
