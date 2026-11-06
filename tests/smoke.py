"""Shared parts of the smoke suite (tests/test_smoke_*.py, `make smoke`, CI's `smoke` job): a guard that refuses
every connection off this machine, and a small lake made from the recorded storefront responses, written the way
ingest.run writes a weekly run."""

import os
import shutil
import socket
from datetime import UTC, date, datetime

import pytest

from coffeedata import ingest
from coffeedata.lake import Lake, new_batch_id

from .conftest import load_roasters, recorded_items

LOCAL = ("127.0.0.1", "::1", "localhost")
# Where a subprocess (dbt, uvicorn, node) would send HTTP: a closed local port, so a request fails at once.
BLACK_HOLE = "http://127.0.0.1:9"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
SECRETS = ("ANTHROPIC_API_KEY", "HF_TOKEN", "AUTH_PEPPER", "SESSION_SECRET", "OWNER_SETUP_CODE")


class NoNetwork(OSError):
    pass


def require(tool: str, available: bool, flag: str) -> None:
    """Skip a smoke test whose tool isn't installed, except where `flag` is set (CI's smoke job sets REQUIRE_DBT and
    REQUIRE_BROWSER): there a missing tool fails the test, so the check never passes green having run nothing."""
    if available:
        return
    if os.environ.get(flag):
        pytest.fail(f"{flag} is set, but {tool} isn't installed")
    pytest.skip(f"{tool} not installed")


def require_dbt() -> None:
    require("dbt", shutil.which("dbt") is not None, "REQUIRE_DBT")


def block_network(monkeypatch) -> None:
    """No real network: a connection from this process to anywhere but loopback raises NoNetwork, and the
    subprocesses it starts get proxies that go nowhere. No API key reaches the code under test."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def check(sock: socket.socket, address) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in LOCAL:
            raise NoNetwork(f"smoke tests make no outside connections (tried {address[0]})")

    def connect(self, address):
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    for name in PROXY_VARS:
        monkeypatch.setenv(name, BLACK_HOLE)
    monkeypatch.setenv("NO_PROXY", ",".join(LOCAL))
    monkeypatch.setenv("no_proxy", ",".join(LOCAL))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "False")  # dbt's telemetry is a real request
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)


def fixture_lake(lake: Lake, run_date: str) -> dict[str, int]:
    """Write the recorded storefronts (tests/fixtures) into `lake` as one weekly run: its run_manifests rows, then
    its fetches, snapshots and bodies from ingest.rows_for, as ingest.run writes them. Returns products per roaster."""
    roasters = load_roasters()
    items = recorded_items(roasters)
    rd = date.fromisoformat(run_date)
    fetched = datetime.combine(rd, datetime.min.time(), UTC).replace(hour=9, minute=17)
    stores = [roasters[rid] for rid in items]
    manifest = new_batch_id()
    lake.write_partition(
        "run_manifests", run_date, manifest, ingest.manifest_rows(stores, rd, manifest, mode="full")
    )
    batch = new_batch_id()
    results = [ingest.Result(roasters[rid], fetched, its) for rid, its in items.items()]
    for table, rows in ingest.rows_for(results, rd, batch, set()).items():
        lake.write_partition(table, run_date, batch, rows)
    return {rid: len(its) for rid, its in items.items()}
