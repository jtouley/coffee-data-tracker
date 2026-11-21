import csv
import json
import os
from pathlib import Path

import pytest

from coffeedata.adapters import shopify, squarespace, woocommerce
from coffeedata.lake import Lake
from coffeedata.models import Roaster

# The accuracy floors' report and annotations (tests/evalfloor.py, docs/TRIAGE.md).
from .evalfloor import (  # noqa: F401
    pytest_itemcollected,
    pytest_runtest_call,
    pytest_runtest_logreport,
    pytest_sessionfinish,
    pytest_terminal_summary,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def _rules_only(monkeypatch):
    """Tests build synthetic lakes without Claude, so they take the explicit rules-only opt-out
    (verdict.rules_only_allowed). Tests of the verdict guard delete it."""
    monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", "1")


REPO_SERVED_DB = Path(__file__).resolve().parents[1] / "build" / "served.duckdb"


def _stat(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_mtime_ns


@pytest.fixture(autouse=True)
def _no_repo_served_db():
    """No test may write the checkout's build/served.duckdb (quality.serve's cwd-relative default): every xdist
    worker shares that one file, so two tests serving at once fight over its DuckDB lock. A test serves into its own
    tmp_path (cli.SERVED_DB, or `db=`). A file that was already there, from a local build, is left alone."""
    before = _stat(REPO_SERVED_DB)
    yield
    if _stat(REPO_SERVED_DB) != before:
        pytest.fail(
            f"{REPO_SERVED_DB} changed during this test (or one running beside it in another xdist worker): "
            "serve into tmp_path (cli.SERVED_DB, or db=) instead"
        )


def load_roasters() -> dict[str, Roaster]:
    """The recorded stores (fixtures/roasters.csv), keyed by roaster id."""
    with (FIX / "roasters.csv").open() as f:
        return {
            r["roaster_id"]: Roaster(
                roaster_id=r["roaster_id"], name=r["name"], host=r["final_host"], platform=r["platform"]
            )
            for r in csv.DictReader(f)
        }


@pytest.fixture
def roasters() -> dict[str, Roaster]:
    return load_roasters()


@pytest.fixture
def lake(tmp_path, monkeypatch) -> Lake:
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    return Lake()


def recorded_items(roasters: dict[str, Roaster]) -> dict:
    """CatalogItems parsed from every recorded storefront response, keyed by roaster id."""
    out = {}
    for rid in ("counterculturecoffee.com", "onyxcoffeelab.com", "intelligentsia.com"):
        name = rid.removesuffix(".com")
        out[rid] = [shopify.parse_product(p, roasters[rid]) for p in load(f"shopify_{name}.json")["products"]]
    out["anaheimcoffee.com"] = [
        woocommerce.parse_product(p, roasters["anaheimcoffee.com"])
        for p in load("woocommerce_anaheimcoffee.json")
    ]
    out["vividcoffee.com"] = [
        squarespace.parse_item(i, roasters["vividcoffee.com"])
        for i in load("squarespace_vividcoffee.json")["items"]
    ]
    return out


@pytest.fixture
def fixture_items(roasters):
    return recorded_items(roasters)


@pytest.fixture
def dbt_installed():
    """For tests that build with dbt (lakehelp.needs_dbt): skip without dbt, fail instead under REQUIRE_DBT=1."""
    from .smoke import require_dbt

    require_dbt()


@pytest.fixture
def no_network(monkeypatch):
    """The smoke suite's guard (tests/smoke.py): no connection off this machine, and no API keys."""
    from .smoke import block_network

    block_network(monkeypatch)


@pytest.fixture(autouse=True, scope="session")
def _own_dbt_target(tmp_path_factory):
    """Under pytest-xdist (`pytest -n`), each worker gets its own dbt target and log folders: a `coffeedata build`
    in two workers at once would otherwise overwrite each other's run_results.json in the checkout's dbt/target."""
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        yield
        return
    from coffeedata import cli

    target = tmp_path_factory.getbasetemp() / "dbt-target"
    mp = pytest.MonkeyPatch()
    mp.setenv("DBT_TARGET_PATH", str(target))
    mp.setenv("DBT_LOG_PATH", str(tmp_path_factory.getbasetemp() / "dbt-logs"))
    mp.setattr(cli, "DBT_TARGET", target)
    yield
    mp.undo()


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    """A local Postgres for the admin's control plane (tests/pg.py): one per test session and xdist worker."""
    from .pg import Server, bindir

    if not (bindir() or os.environ.get("ADMIN_TEST_DATABASE_URL")):
        if os.environ.get("REQUIRE_POSTGRES"):
            pytest.fail("REQUIRE_POSTGRES is set but no Postgres server binaries are installed")
        pytest.skip("no Postgres server binaries (initdb) installed")
    server = Server(tmp_path_factory.mktemp("pg")).start()
    yield server
    server.stop()


@pytest.fixture
def pg_url(pg_server, monkeypatch):
    """A fresh, empty database on the test Postgres. Every ControlPlane the test makes is closed at teardown: an
    open pool's idle connections otherwise last until the worker ends, and enough tests in one worker exhaust the
    server's max_connections ("too many clients already")."""
    from admin.controlplane import ControlPlane

    made: list[ControlPlane] = []
    init = ControlPlane.__init__

    def tracked(self, *a, **kw):
        init(self, *a, **kw)
        made.append(self)

    monkeypatch.setattr(ControlPlane, "__init__", tracked)
    yield pg_server.database()
    for cp in made:
        cp.close()


def wait_for_sends() -> None:
    """Until every send app/mail.py's `later` started has finished and been logged. /api/forgot's reply doesn't wait
    for its email, so a test that checks the outbox after it waits here first (a wait, not a timing bound)."""
    import time

    from app import mail

    for _ in range(3000):
        if not mail._running:
            return
        time.sleep(0.01)
    raise AssertionError("a send never finished")
