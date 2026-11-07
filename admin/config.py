"""The admin's settings, read once at start. A missing or weak setting never turns sign-in off: it is recorded in
`problems`, and the service then answers every request with 503 ("switched off") until it is fixed. No setting or
environment variable skips sign-in."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .auth import MIN_SECRET
from .store import FileStore, GcsStore, Store

SECRETS = ("ADMIN_SESSION_SECRET", "ADMIN_ENROLL_TOKEN", "HF_TOKEN")


def hosted() -> bool:
    """On Cloud Run (K_SERVICE is set on every service) or a Hugging Face Space (SPACE_ID)."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("SPACE_ID"))


@dataclass
class Settings:
    origin: str
    session_secret: str
    enroll_token: str | None
    lake_url: str
    cache_dir: Path
    store: Store | None
    hosted: bool = False
    proxy_hops: int = 0
    lake_timeout_s: float = 25.0
    problems: list[str] = field(default_factory=list)

    @property
    def rp_id(self) -> str:
        return urlsplit(self.origin).hostname or ""

    @property
    def secure_cookies(self) -> bool:
        """Secure cookies on any https origin, and always when hosted (an http origin is refused there)."""
        return self.hosted or self.origin.startswith("https://")

    def check(self) -> Settings:
        p = self.problems
        if len(self.session_secret) < MIN_SECRET:
            p.append(f"ADMIN_SESSION_SECRET is missing or shorter than {MIN_SECRET} characters")
        u = urlsplit(self.origin)
        if not (u.scheme in ("https", "http") and u.hostname and not u.path.strip("/") and not u.query):
            p.append("ADMIN_ORIGIN must be the admin's origin, like https://admin.registry.coffee")
        elif self.hosted and u.scheme != "https":
            p.append("ADMIN_ORIGIN must be https when hosted")
        elif u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1"):
            p.append("ADMIN_ORIGIN may be http only on localhost")
        if self.store is None:
            p.append("set ADMIN_STATE_BUCKET (hosted) or ADMIN_STATE_DIR (a laptop) for the sign-in state")
        elif self.hosted and not isinstance(self.store, GcsStore):
            # A folder in the container is lost on restart, and with it the record that enrollment was used.
            p.append("hosted, the sign-in state must be in ADMIN_STATE_BUCKET, not a local folder")
        return self


def clean(name: str) -> str | None:
    """A secret as Secret Manager hands it over, without the trailing newline `--data-file=-` keeps."""
    v = os.environ.get(name)
    return v.strip() if v is not None else None


def from_env() -> Settings:
    for name in SECRETS:  # every reader (huggingface_hub, DuckDB's secret) sees the clean value
        v = os.environ.get(name)
        if v is not None and v != v.strip():
            os.environ[name] = v.strip()
    bucket, folder = os.environ.get("ADMIN_STATE_BUCKET"), os.environ.get("ADMIN_STATE_DIR")
    store: Store | None = None
    problems: list[str] = []
    try:
        store = GcsStore(bucket) if bucket else FileStore(folder) if folder else None
    except ValueError:
        problems.append("ADMIN_STATE_BUCKET is not a bucket name")
    hops = os.environ.get("COFFEEDATA_PROXY_HOPS", "1" if hosted() else "0")
    return Settings(
        origin=(os.environ.get("ADMIN_ORIGIN") or "").rstrip("/"),
        session_secret=clean("ADMIN_SESSION_SECRET") or "",
        enroll_token=clean("ADMIN_ENROLL_TOKEN") or None,
        lake_url=os.environ.get("COFFEEDATA_LAKE") or "lake",
        cache_dir=Path(os.environ.get("ADMIN_CACHE_DIR") or "build/admin-cache"),
        store=store,
        hosted=hosted(),
        proxy_hops=int(hops) if hops.isdigit() else 0,
        problems=problems,
    ).check()
