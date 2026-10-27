from datetime import date

import httpx
import respx

from coffeedata import ingest
from coffeedata.models import Roaster

from .conftest import load


def _roasters():
    return [
        Roaster(roaster_id="onyxcoffeelab.com", name="Onyx", host="onyxcoffeelab.com", platform="shopify"),
        Roaster(roaster_id="broken.com", name="Broken", host="broken.com", platform="shopify"),
    ]


def test_run_records_ok_and_failed_fetches(lake):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://onyxcoffeelab.com/products.json").mock(
            return_value=httpx.Response(200, json=load("shopify_onyxcoffeelab.json"))
        )
        m.get("https://broken.com/products.json").mock(return_value=httpx.Response(403))
        out = ingest.run(lake, _roasters(), "2026-10-26", workers=2)
    shopify = out["by_adapter"]["shopify"]
    assert (shopify["roasters"], shopify["errors"], shopify["with_coffee"]) == (2, 1, 1)
    assert 0 < shopify["coffees"] <= 12
    con = lake.connect()
    fetches = dict(con.execute(f"select roaster_id, status from {lake.scan('fetches')}").fetchall())
    assert fetches == {"onyxcoffeelab.com": "ok", "broken.com": "error"}
    # The failed roaster wrote no snapshot rows at all (review finding 1): nothing can look delisted.
    snap = dict(
        con.execute(f"select roaster_id, count(*) from {lake.scan('snapshots')} group by 1").fetchall()
    )
    assert snap == {"onyxcoffeelab.com": 12}
    n_bodies = con.execute(f"select count(*) from {lake.scan('bodies')}").fetchone()[0]
    n_enrich = con.execute(f"select count(*) from {lake.scan('enrichments')}").fetchone()[0]
    assert n_bodies > 0 and n_enrich == shopify["coffees"]


def test_odd_payload_is_one_roasters_error_not_the_runs(lake, monkeypatch):
    def boom(client, roaster):
        raise TypeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(ingest, "fetch_catalog", boom)
    out = ingest.run(lake, _roasters()[:1], "2026-10-26", workers=1)
    assert out["by_adapter"]["shopify"]["errors"] == 1 and "TypeError" in out["failures"][0]


def test_rerun_skips_done_and_does_not_reenrich(lake, monkeypatch):
    from coffeedata import registry
    from coffeedata.registry import RoasterState

    states = [RoasterState(r, "pending", None) for r in _roasters()]
    monkeypatch.setattr(registry, "current", lambda lake: states)
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://onyxcoffeelab.com/products.json").mock(
            return_value=httpx.Response(200, json=load("shopify_onyxcoffeelab.json"))
        )
        m.get("https://broken.com/products.json").mock(return_value=httpx.Response(403))
        ingest.run(lake, ingest.select(lake, "2026-10-26"), "2026-10-26", workers=2)
        todo = ingest.select(lake, "2026-10-26")
        assert [r.roaster_id for r in todo] == ["broken.com"]  # only the failure is retried
        assert [r.roaster_id for r in ingest.select(lake, "2026-10-26", retry_failed=True)] == ["broken.com"]
        ingest.run(lake, ingest.select(lake, "2026-11-02"), "2026-11-02", workers=2)  # next week: same bodies
    con = lake.connect()
    per_run = dict(
        con.execute(f"select run_date, count(*) from {lake.scan('snapshots')} group by 1").fetchall()
    )
    assert per_run == {date(2026, 10, 26): 12, date(2026, 11, 2): 12}
    dup = con.execute(
        f"select count(*) - count(distinct (roaster_id, product_id, body_hash)) "
        f"from {lake.scan('enrichments')}"
    ).fetchone()[0]
    assert dup == 0


def test_changed_rules_reenrich_unchanged_descriptions(lake, monkeypatch):
    """A rules fix must reach coffees whose description hasn't changed since they were last enriched."""
    from datetime import UTC, datetime

    from coffeedata import extract

    def week(run_date):
        with respx.mock(assert_all_called=False) as m:
            m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
            m.get("https://onyxcoffeelab.com/products.json").mock(
                return_value=httpx.Response(200, json=load("shopify_onyxcoffeelab.json"))
            )
            ingest.run(lake, _roasters()[:1], run_date, workers=1)

    def count():
        return lake.connect().execute(f"select count(*) from {lake.scan('enrichments')}").fetchone()[0]

    week("2026-10-26")
    first = count()
    monkeypatch.setattr(extract, "RULES_UPDATED", datetime(2999, 1, 1, tzinfo=UTC))
    week("2026-11-02")
    assert count() == 2 * first
