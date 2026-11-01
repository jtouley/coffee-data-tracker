import functools
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pyarrow.parquet as pq
import respx

from coffeedata import ingest, pipelines, registry
from coffeedata.lake import Lake
from coffeedata.models import Roaster

from .conftest import load

CFG = pipelines.load_config()


def _states():
    return [
        registry.RoasterState(Roaster(roaster_id=h, name=h, host=h, platform="shopify"), "pending", None)
        for h in ("onyxcoffeelab.com", "broken.com")
    ]


def _mock(m, onyx_status=200):
    m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
    m.get("https://onyxcoffeelab.com/products.json").mock(
        return_value=httpx.Response(onyx_status, json=load("shopify_onyxcoffeelab.json"))
    )
    m.get("https://broken.com/products.json").mock(return_value=httpx.Response(403))


def _job(lake, root, run_date, onyx_status=200):
    with respx.mock(assert_all_called=False) as m:
        _mock(m, onyx_status)
        return pipelines._fetch(lake, root, CFG, "shopify", "shopify", "0", run_date, _states(), "j", 2, 25)


def _tables(lake):
    con = lake.connect()
    q = lambda t, cols: sorted(con.execute(f"select {cols} from {lake.scan(t)}").fetchall())  # noqa: E731
    return {
        "fetches": q("fetches", "roaster_id, status, products, coffees"),
        "snapshots": q("snapshots", "roaster_id, product_id, is_coffee, payload_hash"),
        "enrichments": q("enrichments", "roaster_id, product_id, body_hash, origin_country"),
        "bodies": q("bodies", "body_hash"),
        "run_manifests": q("run_manifests", "roaster_id, mode"),
    }


def test_merge_gives_dbt_the_rows_a_direct_ingest_writes(tmp_path, monkeypatch):
    direct = Lake(url=str(tmp_path / "direct"))
    with respx.mock(assert_all_called=False) as m:
        _mock(m)
        ingest.run(direct, _states(), "2026-11-08", workers=2, mode="full")
    lake = Lake(url=str(tmp_path / "lake"))
    _job(lake, tmp_path / "bucket", "2026-11-03")
    monkeypatch.setattr(pipelines, "roster", lambda lake, run_date: _states())
    out = pipelines.run_merge(lake, "2026-11-08", CFG, tmp_path / "bucket")
    assert (out["expected"], out["ok"], out["attempted"], out["never_fetched"]) == (2, 1, 2, 0)
    assert _tables(lake)["enrichments"] == []  # ELT: the load writes none
    enriched = pipelines.run_enrich(lake, "2026-11-08", CFG, tmp_path / "bucket")
    assert enriched["rules_rows"] > 0
    assert _tables(lake) == _tables(direct)  # post-load rules == what ingest used to write
    assert pipelines.run_enrich(lake, "2026-11-08", CFG, tmp_path / "bucket")["rules_rows"] == 0  # idempotent
    # One file per table for the week; every run_date is the weekly run's.
    con = lake.connect()
    assert con.execute(f"select distinct run_date from {lake.scan('snapshots')}").fetchall() == [
        (date(2026, 11, 8),)
    ]
    files = [p for p in (tmp_path / "lake").rglob("*.parquet") if "schema" not in p.name]
    assert Counter(p.relative_to(tmp_path / "lake").parts[0] for p in files) == {
        "fetches": 1,
        "snapshots": 1,
        "bodies": 1,
        "enrichments": 1,
        "run_manifests": 1,
    }


def test_merge_keeps_the_latest_success_over_a_later_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DEFERRED_SPREAD", 0.0)
    clock = {"t": 0.0}  # the limiter's Retry-After waits pass instantly
    sleep = lambda s: clock.update(t=clock["t"] + max(0.0, s))  # noqa: E731
    monkeypatch.setattr(
        ingest,
        "PlatformLimiter",
        functools.partial(ingest.PlatformLimiter, clock=lambda: clock["t"], sleep=sleep),
    )
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    # ingest.run's page backoffs (60/120/240 s) are scheduled on its own clock: run it on the fake one
    monkeypatch.setattr(ingest, "time", SimpleNamespace(monotonic=lambda: clock["t"], sleep=sleep))
    lake = Lake(url=str(tmp_path / "lake"))
    _job(lake, tmp_path / "bucket", "2026-11-03")
    _job(lake, tmp_path / "bucket", "2026-11-05", onyx_status=503)
    monkeypatch.setattr(pipelines, "roster", lambda lake, run_date: _states())
    pipelines.run_merge(lake, "2026-11-08", CFG, tmp_path / "bucket")
    assert {r[0]: r[1] for r in _tables(lake)["fetches"]} == {
        "onyxcoffeelab.com": "ok",
        "broken.com": "error",
    }


def test_partition_is_one_file_and_a_rerun_skips_what_it_fetched(tmp_path):
    lake = Lake(url=str(tmp_path / "lake"))
    out, outcomes = _job(lake, tmp_path / "bucket", "2026-11-03")
    part = tmp_path / "bucket/raw/connector=shopify/run_date=2026-11-03/shard=0"
    assert [p.name for p in part.rglob("*.parquet")] == ["data.parquet"]
    assert {r["outcome"] for r in outcomes.rows()} == {"ok", "error"}
    again, _ = _job(lake, tmp_path / "bucket", "2026-11-03")
    assert (again["resumed_skipped"], again["todo"]) == (1, 1)
    assert [p.name for p in part.rglob("*.parquet")] == ["data.parquet"]
    kinds = Counter(r["kind"] for r in pq.read_table(part / "data.parquet").to_pylist())
    assert kinds["fetches"] == 3  # onyx once, broken twice


def test_shards_are_stable_and_even():
    assert pipelines.shard_of("onyxcoffeelab.com") == pipelines.shard_of("onyxcoffeelab.com")
    sizes = Counter(pipelines.shard_of(f"store{i}.com") for i in range(7000))
    assert len(sizes) == 7 and max(sizes.values()) < 1.1 * 1000


def _control(root, rid, outcome, when, job="shopify"):
    row = {k: None for k in pipelines.CONTROL.names} | {
        "job_id": f"{job}-{rid}-{when:%H%M}",
        "job": job,
        "connector": "shopify",
        "shard": "0",
        "run_date": when.date(),
        "ended_at": when,
        "roaster_id": rid,
        "outcome": outcome,
        "fetched_at": when,
    }
    pipelines.write_control(root, str(when.date()), row["job_id"], [row])


def test_refused_only_after_two_jobs_hours_apart_and_reset_by_a_success(tmp_path):
    t = datetime(2026, 11, 2, 3, tzinfo=UTC)
    _control(tmp_path, "a.com", "refused_attempt", t)
    _control(tmp_path, "a.com", "refused_attempt", t + timedelta(minutes=20), "sweep")
    _control(tmp_path, "b.com", "refused_attempt", t)
    _control(tmp_path, "b.com", "refused_attempt", t + timedelta(hours=6), "sweep")
    _control(tmp_path, "c.com", "refused_attempt", t)
    _control(tmp_path, "c.com", "ok", t + timedelta(hours=6), "sweep")
    _control(tmp_path, "c.com", "refused_attempt", t + timedelta(hours=7), "sweep")
    state = pipelines.store_state(tmp_path, t + timedelta(hours=8))
    assert {rid: s.refused(CFG) for rid, s in state.items()} == {
        "a.com": False,
        "b.com": True,
        "c.com": False,
    }


def test_a_store_parked_mid_catalog_is_parked_and_its_progress_found_by_the_sweep(tmp_path):
    from coffeedata.http import RequestStats

    r = Roaster(roaster_id="big.com", name="Big", host="big.com", platform="shopify")
    res = ingest.Result(
        r, datetime(2026, 11, 2, 3, tzinfo=UTC), error="parked: page 2 still blocked after 3 backoffs"
    )
    res.stats = RequestStats(requests=5, rate_limited=4)
    out = pipelines.Outcomes("j", "shopify", "shopify", "0", "2026-11-02")
    out(res)
    assert [row["outcome"] for row in out.rows()] == ["parked"]
    item = load("shopify_onyxcoffeelab.json")["products"][0]
    from coffeedata.adapters.shopify import parse_product

    ingest.save_parked(
        SimpleNamespace(root=tmp_path),
        "2026-11-02",
        "big.com",
        {"page": 2, "items": [parse_product(item, r)]},
    )
    got = pipelines.parked_progress(tmp_path, date(2026, 11, 1))
    assert got["big.com"]["page"] == 2 and len(got["big.com"]["items"]) == 1


def _all_429_run(tmp_path, monkeypatch, quiet):
    clock = {"t": 0.0}
    sleep = lambda s: clock.update(t=clock["t"] + max(0.0, s))  # noqa: E731
    monkeypatch.setattr(
        ingest,
        "PlatformLimiter",
        functools.partial(ingest.PlatformLimiter, clock=lambda: clock["t"], sleep=sleep),
    )
    monkeypatch.setattr(ingest, "time", SimpleNamespace(monotonic=lambda: clock["t"], sleep=sleep))
    monkeypatch.setattr(ingest, "DEFERRED_SPREAD", 0.0)
    hosts = [f"s{i}.example" for i in range(10)]
    lake = Lake(url=str(tmp_path / f"lake{quiet}"))
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        m.get(url__regex=r"https://[^/]+/products.json").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "60"})
        )
        roasters = [Roaster(roaster_id=h, name=h, host=h, platform="shopify") for h in hosts]
        out = ingest.run(
            lake, roasters, "2026-11-02", workers=10, shopify_rps=1000, quiet_hosts=hosts if quiet else ()
        )
    return out["shopify_limiter"]["cooldowns"]


def test_a_sweeps_known_blockers_never_trip_the_platform_wide_pause(tmp_path, monkeypatch):
    # the real condensed week's sweep, 2026-10-31: "5 of 10 stores blocked within 30s: platform-wide"
    assert _all_429_run(tmp_path, monkeypatch, quiet=False) > 0
    assert _all_429_run(tmp_path, monkeypatch, quiet=True) == 0
