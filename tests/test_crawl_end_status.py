"""A crawl's end status is `deadline` only when its deadline stopped work: stores never started (skipped_deadline) or
catalogs the deadline cut off mid-way (stopped_deadline: a DeadlineReached behind the fetch). A catalog read in part
for another reason (a WooCommerce count that differs from X-WP-Total, a failed product page or sub-sitemap) ends the
crawl `done`, with detail.partial kept, and every reader of the status takes that `done` as a finished pass."""

from __future__ import annotations

import json

import pytest

from coffeedata import cli, connectors, control, http, ingest, registry
from coffeedata.http import DeadlineReached, PartialCatalog
from coffeedata.lake import Lake
from coffeedata.models import CatalogItem, Roaster

WEEK = "2026-11-23"


def _item(r: Roaster, pid: str = "1") -> CatalogItem:
    return CatalogItem(roaster_id=r.roaster_id, product_id=pid, handle="h", url="u", title="Ethiopia Coffee",
                       platform="woocommerce")  # fmt: skip


def _at_deadline(msg: str, items: list) -> PartialCatalog:
    """A PartialCatalog raised as the adapters raise it at the deadline: `from` the DeadlineReached."""
    try:
        raise DeadlineReached("the run's deadline passed before https://x/wp-json/wc/store/v1/products")
    except DeadlineReached as e:
        try:
            raise PartialCatalog(f"{e} ({msg})", items) from e
        except PartialCatalog as p:
            return p


# --- the rule --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("out", "status"),
    [
        ({"skipped_deadline": 3, "stopped_deadline": 0, "partial": 0}, "deadline"),  # skipped only
        ({"skipped_deadline": 0, "stopped_deadline": 1, "partial": 1}, "deadline"),  # a deadline partial
        ({"skipped_deadline": 0, "stopped_deadline": 0, "partial": 3}, "done"),  # partials for other reasons
        ({"skipped_deadline": 2, "stopped_deadline": 1, "partial": 4}, "deadline"),  # both
        ({"skipped_deadline": 0, "stopped_deadline": 0, "partial": 0}, "done"),
        ({"parked_at_deadline": 1, "partial": 2}, "deadline"),  # a blocked store parked at the deadline
        ({"not_retried_at_deadline": 1}, "deadline"),  # a store whose wait ends past the deadline
        ({"roasters": 0, "by_adapter": {}, "skipped_deadline": 0, "deferred": {}}, "done"),  # nothing to do
    ],
)
def test_the_end_status_is_deadline_only_when_the_deadline_stopped_work(out, status):
    assert connectors.end_status(out) == status


def test_only_a_deadline_among_the_causes_is_a_deadline_stop():
    r = Roaster(roaster_id="w.com", name="w", host="w.com", platform="woocommerce")
    assert ingest.stopped_by_deadline(_at_deadline("page 2", [_item(r)]))
    # Squarespace wraps the collection's PartialCatalog in a second one: the DeadlineReached is two causes down
    inner = _at_deadline("/shop page 3", [])
    try:
        raise PartialCatalog(str(inner), [_item(r)]) from inner
    except PartialCatalog as outer:
        assert ingest.stopped_by_deadline(outer)
    assert ingest.stopped_by_deadline(DeadlineReached("before the sitemap"))  # nothing read: an error
    # WooCommerce's X-WP-Total mismatch and generic's failed pages are raised on their own, with no cause
    assert not ingest.stopped_by_deadline(
        PartialCatalog("w.com: 10 products read, X-WP-Total says 44", [_item(r)])
    )
    assert not ingest.stopped_by_deadline(http.FetchError("HTTP 500 https://w.com/sitemap.xml", status=500))


# --- end to end: a crawl's end row, and every reader of it ------------------------------------------------------------


STORES = [Roaster(roaster_id=f"{n}.com", name=n, host=f"{n}.com", platform="woocommerce")
          for n in ("whole", "mismatch", "slow", "late")]  # fmt: skip


@pytest.fixture
def woo(tmp_path, monkeypatch):
    """A lake whose registry is 4 WooCommerce stores; `kinds` says how each store's fetch ends."""
    monkeypatch.setattr(connectors, "CONNECTORS", {"woocommerce": connectors.CONNECTORS["woocommerce"]})
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(r, "active", None) for r in STORES]
    )
    kinds: dict[str, str] = {}

    def fetch(client, roaster, *a, **k):
        kind = kinds.get(roaster.roaster_id, "whole")
        if kind == "mismatch":  # woocommerce.fetch's own check: never the clock
            raise PartialCatalog(f"{roaster.host}: 1 products read, X-WP-Total says 44", [_item(roaster)])
        if kind == "deadline_partial":
            raise _at_deadline("1 of 2+ products read", [_item(roaster)])
        if kind == "deadline_nothing":  # stopped before a product was read whole: an error row
            raise _at_deadline("0 of 0+ products read", [])
        return [_item(roaster), _item(roaster, "2")]

    monkeypatch.setattr(ingest, "fetch_catalog", fetch)
    return Lake(str(tmp_path / "lake")), kinds


def _end(lake: Lake, job: str = "crawl") -> tuple[dict, dict]:
    [row] = [r for r in control.rows(lake, WEEK) if r["job"] == job]
    return row, json.loads(row["detail"])


def test_a_crawl_whose_only_partial_is_an_x_wp_total_mismatch_ends_done_and_lands(woo):
    lake, kinds = woo
    kinds["mismatch.com"] = "mismatch"
    out = connectors.crawl(lake, "woocommerce", WEEK)
    row, detail = _end(lake)
    assert (row["status"], row["skipped"], out["status"]) == ("done", 0, "done")
    assert (detail["partial"], detail["stopped_deadline"], detail["partial_every_week"]) == (1, 0, [])
    assert detail["parked_at_deadline"] == detail["not_retried_at_deadline"] == 0
    # the readers of the status: the merge's landed set, the crash guard's finished attempts, the admin's landed
    assert control.landed(lake, WEEK) == {"woocommerce"}
    assert row["attempt_id"] in connectors._finished(lake, WEEK)
    from admin import queries

    assert row["status"] in queries.LANDED
    # the partial store isn't done: the resume slot asks it again, and only it
    assert connectors.crawl(lake, "woocommerce", WEEK)["todo"] == 1


@pytest.mark.parametrize("kind", ["deadline_partial", "deadline_nothing"])
def test_a_catalog_the_deadline_stopped_ends_the_crawl_deadline(woo, kind):
    lake, kinds = woo
    kinds |= {"mismatch.com": "mismatch", "slow.com": kind}
    connectors.crawl(lake, "woocommerce", WEEK)
    row, detail = _end(lake)
    assert (row["status"], row["skipped"], detail["stopped_deadline"]) == ("deadline", 0, 1)
    assert detail["partial"] == (
        2 if kind == "deadline_partial" else 1
    )  # a fetch with nothing read is an error
    assert control.landed(lake, WEEK) == {"woocommerce"}  # a deadline still lands, as before


def test_stores_left_unstarted_end_the_crawl_deadline(woo):
    lake, _ = woo
    out = connectors.crawl(
        lake, "woocommerce", WEEK, deadline_min=1e-9
    )  # the window is over before any start
    row, detail = _end(lake)
    assert (row["status"], row["skipped"], out["skipped_deadline"]) == ("deadline", 4, 4)
    assert detail["partial"] == detail["stopped_deadline"] == 0


def test_crawl_retry_ends_done_when_a_resumed_store_stops_partial_for_another_reason(tmp_path, monkeypatch):
    lake = Lake(str(tmp_path / "lake"))
    shop = Roaster(roaster_id="shop.com", name="s", host="shop.com", platform="shopify")
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(shop, "active", None)]
    )
    ingest.save_parked(
        connectors.PartitionLake(lake, "shopify", WEEK), WEEK, "shop.com", {"page": 2, "items": []}
    )

    def fetch(client, roaster, resume=None, known=None):
        raise PartialCatalog("1 product page failed", [_item(roaster)])

    monkeypatch.setattr(ingest, "fetch_catalog", fetch)
    connectors.crawl_retry(lake, "shopify", WEEK)
    row, detail = _end(lake, "crawl-retry")
    assert (row["status"], detail["partial"], detail["stopped_deadline"]) == ("done", 1, 0)


def test_the_cli_says_what_the_deadline_stopped_apart_from_partials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    out = {
        "status": "done",
        "skipped_deadline": 0,
        "partial": 3,
        "stopped_deadline": 0,
        "partial_every_week": [],
    }
    monkeypatch.setattr(connectors, "crawl", lambda *a, **k: out)
    cli.main(["crawl", "--connector", "woocommerce", "--run-date", WEEK])
    err = capsys.readouterr().err
    assert "3 catalogs read in part" in err and "deadline" not in err
    out |= {"status": "deadline", "stopped_deadline": 1}
    cli.main(["crawl", "--connector", "woocommerce", "--run-date", WEEK])
    err = capsys.readouterr().err
    assert "deadline reached, 1 fetches stopped mid-catalog" in err and "3 catalogs read in part" in err
    # a deadline stop with nothing read is an error, not a partial: the deadline line still says why
    out |= {"partial": 0, "stopped_deadline": 0, "parked_at_deadline": 2, "not_retried_at_deadline": 1}
    cli.main(["crawl", "--connector", "woocommerce", "--run-date", WEEK])
    err = capsys.readouterr().err
    assert "deadline reached, 2 blocked stores parked for crawl-retry, 1 stores not retried" in err
    assert "read in part" not in err


def test_a_cause_chain_that_loops_ends_instead_of_hanging():
    """An exception that is its own cause (some mocks make one) is walked once, so a crawl worker can't hang on it."""
    e = RuntimeError("loop")
    e.__cause__ = e
    assert list(http.causes(e)) == [e]
    assert ingest.stopped_by_deadline(e) is False
