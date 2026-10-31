"""A weekly run that dies (crash, kill, job timeout) is continued under its own run_date, and the gate sees one run."""

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import date

import pytest

from coffeedata import cli, ingest, registry
from coffeedata.lake import new_batch_id
from coffeedata.models import CatalogItem, Roaster, Variant
from coffeedata.registry import RoasterState

from .test_dbt import build

RUN, NEXT_DAY = "2026-10-30", "2026-10-31"
N = 30


def roasters(n: int = N) -> list[Roaster]:
    return [
        Roaster(roaster_id=f"r{i:03d}.com", name=f"R{i}", host=f"r{i:03d}.com", platform="shopify")
        for i in range(n)
    ]


def fake_catalog(client, r: Roaster) -> list[CatalogItem]:
    return [
        CatalogItem(
            roaster_id=r.roaster_id,
            product_id=str(j),
            handle=f"h{j}",
            url=f"https://{r.host}/p/{j}",
            title=f"Ethiopia Guji Natural Coffee {j}",
            platform="shopify",
            product_type="Coffee",
            body_hash=f"{r.roaster_id}-{j}",
            body_text="Washed Ethiopian coffee, notes of jasmine.",
            variants=[Variant(variant_id=f"{j}-v", title="12 oz", price_usd=19.0, grams=340, available=True)],
        )
        for j in range(3)
    ]


def states(n: int = N) -> list[RoasterState]:
    return [RoasterState(r, "pending", None) for r in roasters(n)]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(ingest, "fetch_catalog", fake_catalog)
    monkeypatch.setattr(registry, "current", lambda lake: states())


def counts(lake, run_date=RUN) -> tuple[int, int]:
    """(fetch rows, distinct roasters) recorded for the run."""
    return (
        lake.connect()
        .execute(
            f"select count(*), count(distinct roaster_id) from {lake.scan('fetches')} where run_date = ?",
            [date.fromisoformat(run_date)],
        )
        .fetchone()
    )


# The child runs a normal ingest and is SIGKILLed (no cleanup, no flush: like a runner reaching its timeout) once
# 17 roasters have been fetched. CHUNK 5, so three chunks are already in the lake and the rest is lost.
CHILD = textwrap.dedent(
    """
    import os, signal, sys, threading, time
    sys.path.insert(0, sys.argv[2])
    from coffeedata import ingest, registry
    from coffeedata.lake import Lake
    from tests import test_resume as t
    ingest.CHUNK = 5
    n, lock = [0], threading.Lock()
    def dying(client, r):
        time.sleep(0.1)  # a fetch takes a while; the main thread flushes meanwhile
        with lock:
            n[0] += 1
            if n[0] > 17:
                os.kill(os.getpid(), signal.SIGKILL)
        return t.fake_catalog(client, r)
    ingest.fetch_catalog = dying
    registry.current = lambda lake: t.states()
    lake = Lake(sys.argv[1])
    todo, mode = ingest.select_states(lake, t.RUN)
    ingest.run(lake, todo, t.RUN, workers=1, mode=mode)
    """
)


@pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")
def test_killed_run_resumes_next_day_and_the_gate_sees_one_complete_run(lake, tmp_path, fake):
    root = os.path.dirname(os.path.dirname(__file__))
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(lake.root), root], capture_output=True, text=True
    )
    assert child.returncode == -9, child.stderr[-2000:]
    assert counts(lake) == (15, 15)  # three chunks survived the kill

    # The gate refuses the partial run on its own: nothing would be served from it.
    r = build(lake, tmp_path, check=False, as_of_run=RUN, today=RUN)
    assert r.returncode != 0 and "run_is_complete" in r.stdout

    # The next day, a re-run finds the open run and continues it, fetching only what it never tried.
    assert ingest.open_run(lake, NEXT_DAY) == RUN
    todo, mode = ingest.select_states(lake, RUN)
    assert len(todo) == N - 15 and mode == "full"
    out = ingest.run(lake, todo, RUN, workers=4, mode=mode)
    assert out["complete"] and out["fetched"] == N - 15
    assert counts(lake) == (N, N)  # one fetch per roaster: none fetched twice
    assert ingest.open_run(lake, NEXT_DAY) is None
    assert counts(lake, NEXT_DAY) == (0, 0)  # no second, partial run_date

    con = build(lake, tmp_path, as_of_run=RUN, today=NEXT_DAY)  # every gate test passes, unique_runs included
    rows = con.execute(
        "select count(*), count(*) filter (where attempted), count(*) filter (where catalog_ok), "
        "count(distinct run_date) from run_completeness"
    ).fetchone()
    assert rows == (N, N, N, 1)
    assert con.execute("select count(*), count(distinct roaster_id) from stg_runs").fetchone() == (N, N)
    dup = con.execute(
        "select count(*) - count(distinct (coffee_id, run_date)) from stg_snapshots"
    ).fetchone()[0]
    assert dup == 0 and con.execute("select count(*) from stg_snapshots").fetchone()[0] == 3 * N


def test_deadline_flushes_what_finished_and_a_rerun_completes(lake, monkeypatch, fake):
    def slow(client, r):
        time.sleep(0.05)
        return fake_catalog(client, r)

    monkeypatch.setattr(ingest, "fetch_catalog", slow)
    todo, mode = ingest.select_states(lake, RUN)
    out = ingest.run(lake, todo, RUN, workers=2, mode=mode, deadline=time.monotonic() + 0.4)
    assert not out["complete"] and 0 < out["fetched"] < N
    assert counts(lake) == (out["fetched"], out["fetched"])  # in-flight fetches finished and were flushed too
    assert ingest.open_run(lake, RUN) == RUN
    todo, mode = ingest.select_states(lake, RUN)
    assert ingest.run(lake, todo, RUN, workers=4, mode=mode)["complete"]
    assert counts(lake) == (N, N)


def test_a_chunk_is_flushed_when_it_is_old_not_only_when_full(lake, monkeypatch, fake):
    def slow(client, r):
        time.sleep(0.03)
        return fake_catalog(client, r)

    monkeypatch.setattr(ingest, "fetch_catalog", slow)
    published = []
    monkeypatch.setattr(lake, "publish", lambda msg, **k: published.append(msg) or 0)
    ingest.run(lake, roasters(10), RUN, workers=1, mode="full", flush_every_s=0.1)
    assert sum("batch" in m for m in published) >= 3  # far fewer than CHUNK (200) roasters each


def manifest(lake, run_date: str, ids: list[str], mode: str = "full") -> None:
    rows = ingest.manifest_rows(
        [r for r in roasters() if r.roaster_id in ids], date.fromisoformat(run_date), "m", mode=mode
    )
    lake.write_partition("run_manifests", run_date, new_batch_id(), rows)


def test_open_run_is_only_a_recent_full_run_with_untried_roasters(lake, fake):
    assert ingest.open_run(lake, RUN) is None  # empty lake
    manifest(
        lake, "2026-10-20", ["r000.com"]
    )  # unfinished, but more than 6 days old: next week starts afresh
    assert ingest.open_run(lake, RUN) is None
    manifest(lake, "2026-10-29", ["r000.com", "r001.com"], mode="sample")  # a sample is never resumed
    assert ingest.open_run(lake, RUN) is None
    manifest(lake, "2026-10-29", ["r000.com", "r001.com"])
    assert ingest.open_run(lake, RUN) == "2026-10-29"
    ingest.run(
        lake, roasters(2), "2026-10-29", workers=2
    )  # a failed fetch counts as tried: the retry pass's job
    assert ingest.open_run(lake, RUN) is None


def weekly_args(**kw) -> argparse.Namespace:
    return argparse.Namespace(
        **(
            dict(
                run_date=None,
                sample=None,
                discover=False,
                enrich_limit=10,
                db="x",
                out="y",
                accept_volume_change=False,
                resume_only=False,
                ingest_deadline_minutes=None,
            )
            | kw
        )
    )


@pytest.fixture
def steps(monkeypatch):
    """cmd_weekly with every step stubbed: records which steps ran, for which run."""
    calls = []
    monkeypatch.setattr(cli, "_today", lambda: NEXT_DAY)
    for name in ("cmd_probe", "cmd_publish", "cmd_enrich", "cmd_classify", "cmd_discover"):
        monkeypatch.setattr(cli, name, lambda a, n=name: calls.append((n, getattr(a, "run_date", None))))
    monkeypatch.setattr(cli, "cmd_build", lambda a: calls.append(("cmd_build", a.run_date)) or True)
    monkeypatch.setattr(cli, "_export_and_site", lambda *a: None)
    return calls


def test_weekly_continues_the_open_run_and_stops_before_the_build_at_its_deadline(
    lake, monkeypatch, fake, steps
):
    manifest(lake, RUN, [r.roaster_id for r in roasters()])
    ran = []
    monkeypatch.setattr(
        cli, "cmd_ingest", lambda a: ran.append(a) or {"complete": False, "roasters": N, "fetched": 4}
    )
    with pytest.raises(SystemExit, match="continue this run"):
        cli.cmd_weekly(weekly_args(ingest_deadline_minutes=1))
    assert ran[0].run_date == RUN and ran[0].deadline is not None
    assert len(ran) == 1 and not any(n == "cmd_build" for n, _ in steps)


def test_weekly_resume_only_does_nothing_without_an_open_run(lake, fake, steps):
    cli.cmd_weekly(weekly_args(resume_only=True))
    assert steps == []
