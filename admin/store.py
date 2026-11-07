"""Where the admin keeps its sign-in state: passkeys, sessions, recovery codes and their use. Append-only: every
change is a new record, created only if its key is new, and nothing is ever replaced or deleted. That makes the
one-shot enrollment and each recovery code's single use atomic (a second create of the same key fails), and it lets
the hosted bucket's IAM grant create and read but not overwrite or delete.

Two stores: `GcsStore`, a Cloud Storage bucket (hosted; `ifGenerationMatch=0` makes a create atomic), and
`FileStore`, a local folder (tests and a laptop; `O_EXCL`). The lake is never written: this is not lake data.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

KEY = re.compile(r"^[a-z][a-z0-9-]*(/[A-Za-z0-9_.=-]+)+$")


class StoreError(RuntimeError):
    """The store couldn't be read or written. Sign-in fails closed on it: no session is trusted or made."""


def check_key(key: str) -> str:
    if not KEY.match(key) or ".." in key:
        raise ValueError(f"bad store key: {key!r}")
    return key


class Store(Protocol):
    def create(self, key: str, record: dict) -> bool:
        """Write `record` at `key` if nothing is there yet. False if something is (it is never replaced)."""
        ...

    def get(self, key: str) -> dict | None:
        """The record at `key`, or None if there is none."""
        ...

    def list(self, prefix: str) -> list[str]:
        """Every key under `prefix` (a folder, ending in /), sorted."""
        ...


class FileStore:
    """One JSON file per record under `root`. Created with O_EXCL, so a key is written once."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def create(self, key: str, record: dict) -> bool:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        except OSError as e:
            raise StoreError(f"couldn't create {key}: {type(e).__name__}") from e
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        return True

    def get(self, key: str) -> dict | None:
        try:
            return json.loads(self._path(key).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise StoreError(f"couldn't read {key}: {type(e).__name__}") from e

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix.rstrip("/")
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())


METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
GCS = "https://storage.googleapis.com"


class GcsStore:
    """Records as objects in a Cloud Storage bucket, through its JSON API with the service account's own token from
    the metadata server (no key file). A create sends ifGenerationMatch=0: Cloud Storage refuses it (412) if the
    object exists, so it is atomic across instances and revisions."""

    def __init__(self, bucket: str, client: httpx.Client | None = None, timeout: float = 10.0):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
            raise ValueError("bad bucket name")
        self.bucket = bucket
        self.http = client or httpx.Client(timeout=timeout)
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _auth(self) -> dict[str, str]:
        with self._lock:
            if self._token is None or self._token[1] < time.time() + 60:
                try:
                    r = self.http.get(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
                    r.raise_for_status()
                    body = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    raise StoreError(f"no token from the metadata server: {type(e).__name__}") from e
                self._token = (body["access_token"], time.time() + int(body.get("expires_in", 300)))
            return {"Authorization": f"Bearer {self._token[0]}"}

    def _call(self, method: str, url: str, headers: dict | None = None, **kw) -> httpx.Response:
        try:
            return self.http.request(method, url, headers=self._auth() | (headers or {}), **kw)
        except httpx.HTTPError as e:
            raise StoreError(f"{method} to the state bucket failed: {type(e).__name__}") from e

    def create(self, key: str, record: dict) -> bool:
        r = self._call(
            "POST",
            f"{GCS}/upload/storage/v1/b/{self.bucket}/o",
            params={"uploadType": "media", "name": check_key(key), "ifGenerationMatch": "0"},
            content=json.dumps(record, sort_keys=True).encode(),
            headers={"Content-Type": "application/json"},
        )
        if r.status_code == 412:
            return False
        if r.status_code != 200:
            raise StoreError(f"create {key}: HTTP {r.status_code}")
        return True

    def get(self, key: str) -> dict | None:
        r = self._call("GET", f"{GCS}/storage/v1/b/{self.bucket}/o/{quote(check_key(key), safe='')}",
                       params={"alt": "media"})  # fmt: skip
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise StoreError(f"read {key}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise StoreError(f"read {key}: not JSON") from e

    def list(self, prefix: str) -> list[str]:
        out: list[str] = []
        token = None
        while True:
            params = {"prefix": prefix, "fields": "items(name),nextPageToken"}
            if token:
                params["pageToken"] = token
            r = self._call("GET", f"{GCS}/storage/v1/b/{self.bucket}/o", params=params)
            if r.status_code != 200:
                raise StoreError(f"list {prefix}: HTTP {r.status_code}")
            body = r.json()
            out += [i["name"] for i in body.get("items", [])]
            token = body.get("nextPageToken")
            if not token:
                return sorted(out)
