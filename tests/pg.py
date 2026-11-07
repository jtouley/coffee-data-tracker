"""A throwaway local Postgres for the control-plane tests: initdb into a temporary folder and a postgres server on a
free port, with no password (trust, listening on 127.0.0.1 only). ADMIN_TEST_DATABASE_URL points the tests at an
existing server instead. Skipped where no Postgres server binaries are installed (CI's runner image has them;
REQUIRE_POSTGRES=1 turns the skip into a failure)."""

from __future__ import annotations

import glob
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def bindir() -> Path | None:
    for d in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if Path(d, "initdb").exists():
            return Path(d)
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """One server per test session (per xdist worker); `database()` makes a fresh, empty database for a test."""

    def __init__(self, root: Path):
        self.root, self.proc, self.url = root, None, os.environ.get("ADMIN_TEST_DATABASE_URL")
        self._n = 0

    def start(self) -> Server:
        if self.url:
            return self
        b = bindir()
        assert b, "no Postgres server binaries"
        # initdb and postgres refuse to run as root: in a root container they run as the postgres user, in a folder
        # of their own under the system's temporary directory (one it can reach).
        as_user = ["runuser", "-u", "postgres", "--"] if os.geteuid() == 0 else []
        if as_user:
            self.root = Path(tempfile.mkdtemp(prefix="admin-pg-"))
            shutil.chown(self.root, "postgres")
        self.root.mkdir(parents=True, exist_ok=True)
        data = self.root / "data"
        subprocess.run([*as_user, str(b / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust",
                        "--no-sync", "-E", "UTF8"], check=True, capture_output=True)  # fmt: skip
        port = _port()
        self.proc = subprocess.Popen(
            [*as_user, str(b / "postgres"), "-D", str(data), "-p", str(port), "-h", "127.0.0.1", "-k", str(self.root),
             "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self.url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        import pg8000.dbapi

        for _ in range(100):
            try:
                pg8000.dbapi.connect(
                    user="postgres", host="127.0.0.1", port=port, database="postgres"
                ).close()
                return self
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.1)
        raise RuntimeError("the test Postgres didn't start")

    def database(self) -> str:
        import pg8000.native

        assert self.url
        self._n += 1
        name = f"t{os.getpid()}_{self._n}"
        base = self.url.rsplit("/", 1)[0]
        u = __import__("urllib.parse").parse.urlsplit(self.url)
        con = pg8000.native.Connection(user=u.username or "postgres", host=u.hostname, port=u.port or 5432,
                                       database=u.path.strip("/"), password=u.password)  # fmt: skip
        con.run(f"create database {name}")
        con.close()
        return f"{base}/{name}"

    def stop(self) -> None:
        # SIGINT is Postgres's fast shutdown: it ends open client connections instead of waiting for them (SIGTERM,
        # the smart shutdown, waits for every client, so one leaked connection made teardown time out on CI).
        if self.proc:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
            shutil.rmtree(self.root, ignore_errors=True)
