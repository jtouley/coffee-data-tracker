"""Members' email addresses, kept so that only the app can read them (docs/app/signup-ux-spec.md §5, "Storage").

Each account has at most one email, in the users dataset's `contacts` table, one file per change, latest wins:
- `email_hmac`: HMAC-SHA256 of the address, lower-cased, under a key derived from EMAIL_KEY. Forgot-password finds
  the account with it without decrypting anything. Keyed, because addresses are guessable: a plain hash of one is
  a lookup table away from the address (OWASP Password Storage, "Peppering").
- `email_enc`: the address, AES-256-GCM under a second key derived from EMAIL_KEY, with the user id as associated
  data, so a row copied onto another account doesn't decrypt.
Anyone who reads the dataset, or its history, sees only these. Removing or changing an email deletes every file of
the record (Accounts.remove_email) and squashes the dataset's history.

EMAIL_KEY is 32 random bytes, base64 (`openssl rand -base64 32`). Without it the email features are off.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERIFY_HOURS = 24  # NIST 800-63B §4.2.1.2's limit for a code sent by email
RESET_MINUTES = 30  # the owner asked for 15–30 minutes; NIST allows up to 24 h for email


class EmailKeyError(ValueError):
    """EMAIL_KEY isn't 32 bytes of base64."""


def _derive(master: bytes, label: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"registry.coffee email " + label
    ).derive(master)


class EmailCrypto:
    def __init__(self, key_b64: str):
        try:
            master = base64.b64decode(key_b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise EmailKeyError("EMAIL_KEY isn’t valid base64") from None
        if len(master) != 32:
            raise EmailKeyError("EMAIL_KEY must be 32 random bytes, base64 (openssl rand -base64 32)")
        self._lookup = _derive(master, b"lookup")
        self._aead = AESGCM(_derive(master, b"encrypt"))

    def lookup(self, email: str) -> str:
        """The keyed hash an address is found by. Lower-cased whole: people type their address's capitals freely,
        and the mail providers members use ignore them."""
        return hmac.new(self._lookup, email.lower().encode(), hashlib.sha256).hexdigest()

    def seal(self, email: str, user_id: str) -> str:
        nonce = secrets.token_bytes(12)
        return base64.b64encode(nonce + self._aead.encrypt(nonce, email.encode(), user_id.encode())).decode()

    def open(self, sealed: str, user_id: str) -> str:
        raw = base64.b64decode(sealed)
        return self._aead.decrypt(raw[:12], raw[12:], user_id.encode()).decode()


def new_token() -> str:
    """A verify link's token: 32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


@dataclass(frozen=True)
class Contact:
    """One account's email, as stored (sealed). `verified_at` is set once its link was opened."""

    user_id: str
    email_hmac: str
    email_enc: str
    verified_at: datetime | None = None
    verify_sha256: str | None = None
    verify_expires: datetime | None = None
