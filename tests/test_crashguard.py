"""Crawl hardening: the poison-pill guard (crashguard), per-roaster containment, the response size cap, and a
Cloud Run retry that stays inside its execution's window."""

import gc
import threading
import time
import weakref
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from coffeedata import connectors, control, crashguard, http, ingest, registry
from coffeedata.lake import Lake
from coffeedata.models import CatalogItem, Roaster

WEEK = "2026-11-02"


class Killed(BaseException):
    """Stands in for an OOM kill or a segfault: nothing in the crawl catches it, so the attempt ends with no end row
    and its in-flight markers left on disk."""


def _stores(n: int = 6) -> list[Roaster]:
    return [
        Roaster(roaster_id=f"s{i}.com", name="s", host=f"s{i}.com", platform="custom") for i in range(n)
    ] + [Roaster(roaster_id="pill.com", name="p", host="pill.com", platform="custom")]


def _item(r: Roaster) -> CatalogItem:
    return CatalogItem(
        roaster_id=r.roaster_id,
        product_id="1",
        handle="h",
        url="u",
        title="Ethiopia Coffee",
        platform="generic",
    )


@pytest.fixture
def generic_week(tmp_path, monkeypatch):
    """A lake whose registry is 7 generic stores, one of which (pill.com) kills the process while it is fetched."""
    monkeypatch.setattr(connectors, "CONNECTORS", {"generic": connectors.CONNECTORS["generic"]})
    stores = _stores()
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(r, "active", None) for r in stores]
    )
    calls: list[str] = []
    pill = {"kills": True}

    def fetch(client, roaster, *a, **k):
        calls.append(roaster.roaster_id)
        if roaster.roaster_id == "pill.com" and pill["kills"]:
            time.sleep(0.05)  # the others are in flight with it
            raise Killed("signal 9")
        time.sleep(0.01)
        return [_item(roaster)]

    monkeypatch.setattr(ingest, "fetch_catalog", fetch)
    return Lake(str(tmp_path / "lake")), calls, pill


def _statuses(lake: Lake, connector: str | None = "generic") -> dict[str, list[str]]:
    """The week's fetch statuses per roaster: in the connector's partition (before the merge), or the lake's."""
    lk = connectors.PartitionLake(lake, connector, WEEK) if connector else lake
    q = f"select roaster_id, status from {lk.scan('fetches')} order by fetched_at"
    out: dict[str, list[str]] = {}
    for rid, status in lake.connect().execute(q).fetchall():
        out.setdefault(rid, []).append(status)
    return out


def _crawl(lake: Lake) -> dict:
    return connectors.crawl(lake, "generic", WEEK)


def test_a_roaster_that_kills_two_attempts_is_quarantined_and_the_resume_skips_it(generic_week):
    lake, calls, _ = generic_week
    part = connectors.partition(lake, "generic", WEEK)
    with pytest.raises(Killed):
        _crawl(lake)  # attempt 1 dies
    [dead1] = control.rows(lake, WEEK)
    assert dead1["status"] == "running"  # no end row: it died
    marked = {p.stem for p in crashguard.root(part).glob("attempt=*/*.json")}
    assert "pill.com" in marked  # in flight when it died

    calls.clear()
    with pytest.raises(Killed):
        _crawl(lake)  # attempt 2: pill.com is a suspect, fetched in the lane, and kills it again
    second = [r for r in control.rows(lake, WEEK) if r["attempt_id"] != dead1["attempt_id"]][0]
    assert (
        "pill.com" in control.detail_of(second)["suspects"] and control.detail_of(second)["quarantined"] == []
    )

    calls.clear()
    out = _crawl(lake)  # attempt 3: pill.com is quarantined, everything else is fetched
    assert "pill.com" not in calls
    assert out["status"] == "done" and out["quarantined"] == ["pill.com"]
    assert len(out["dead_attempts"]) == 2
    statuses = _statuses(lake)
    assert statuses["pill.com"] == ["crashed"]
    assert all("ok" in v for k, v in statuses.items() if k != "pill.com")
    # the dead attempts' folders stay for the week (the evidence); the finished one's is gone
    assert (
        sorted(d.name.removeprefix("attempt=") for d in crashguard.root(part).iterdir())
        == out["dead_attempts"]
    )

    [third] = [r for r in control.rows(lake, WEEK) if control.detail_of(r).get("quarantined")]
    assert (third["ok"], third["errors"]) == (6, 0)  # the quarantined roaster is neither: never fetched

    calls.clear()
    out = _crawl(lake)  # a later resume (15:17, 21:47) skips it: crashed is done for the week
    assert calls == [] and out["todo"] == 0 and _statuses(lake)["pill.com"] == ["crashed"]
    assert connectors.merge(lake, WEEK)["missing"] == []
    assert (
        _statuses(lake, None)["pill.com"] == ["crashed"] and not part.exists()
    )  # merged; the week's folders go


def test_only_fetches_a_quarantined_roaster_and_its_rows_dont_call_it_quarantined(generic_week, monkeypatch):
    """RUNBOOK §7's un-quarantine: `crawl --only` fetches the roaster, and its start row (the one a kill leaves)
    lists no quarantine, so the runbook's json_extract(detail, '$.quarantined') query doesn't name it."""
    lake, calls, pill = generic_week
    for _ in range(2):
        with pytest.raises(Killed):
            _crawl(lake)
    assert "pill.com" in _crawl(lake)["quarantined"]
    pill["kills"] = False  # fixed
    starts: list[dict] = []
    real_start = control.start

    def start(*a, **k):
        starts.append(k.get("detail") or {})
        return real_start(*a, **k)

    monkeypatch.setattr(control, "start", start)
    calls.clear()
    out = connectors.crawl(lake, "generic", WEEK, only="pill.com")
    assert calls == ["pill.com"] and out["status"] == "done"
    assert starts[0]["quarantined"] == [] and "pill.com" in starts[0]["suspects"]
    [end] = [r for r in control.rows(lake, WEEK) if control.detail_of(r).get("only") == "pill.com"]
    assert end["status"] == "done" and control.detail_of(end)["quarantined"] == []


def test_one_dead_attempt_only_makes_suspects_and_suspects_run_one_at_a_time(generic_week, monkeypatch):
    lake, calls, pill = generic_week
    with pytest.raises(Killed):
        _crawl(lake)
    pill["kills"] = False  # it was something else (memory piling up): the next attempt succeeds
    running, most = set(), [0]
    lock = threading.Lock()
    fetch = ingest.fetch_catalog
    health = crashguard.assess(
        crashguard.root(connectors.partition(lake, "generic", WEEK)), connectors._finished(lake, WEEK)
    )
    suspects = set(health.suspects)
    assert "pill.com" in suspects and not health.quarantine()

    def watched(client, roaster, *a, **k):
        with lock:
            running.add(roaster.roaster_id)
            most[0] = max(most[0], len(running & suspects))
        try:
            time.sleep(0.02)
            return fetch(client, roaster, *a, **k)
        finally:
            with lock:
                running.discard(roaster.roaster_id)

    monkeypatch.setattr(ingest, "fetch_catalog", watched)
    out = _crawl(lake)
    assert out["status"] == "done" and out["quarantined"] == [] and most[0] == 1
    assert all(v[-1] == "ok" for v in _statuses(lake).values())


def _lake_statuses(lake: Lake) -> dict[str, list[str]]:
    return _statuses(lake, None)


def test_an_exception_in_one_roasters_fetch_is_that_roasters_error(lake, monkeypatch):
    stores = _stores(4)

    def fetch(client, roaster, *a, **k):
        if roaster.roaster_id == "pill.com":
            raise RecursionError("maximum recursion depth exceeded while parsing")
        return [_item(roaster)]

    monkeypatch.setattr(ingest, "fetch_catalog", fetch)
    real = ingest.ingest_one

    def one(client, roaster, *a, **k):  # an error outside ingest_one's own try: still one roaster's
        if roaster.roaster_id == "s0.com":
            raise ValueError("bad progress")
        return real(client, roaster, *a, **k)

    monkeypatch.setattr(ingest, "ingest_one", one)
    out = ingest.run(lake, stores, WEEK, workers=4)
    assert out["by_adapter"]["generic"] == {"roasters": 5, "errors": 2, "with_products": 3, "partial": 0}
    st = _lake_statuses(lake)
    assert st["pill.com"] == ["error"] and st["s0.com"] == ["error"]
    assert st["s1.com"] == st["s2.com"] == st["s3.com"] == ["ok"]
    q = f"select error from {lake.scan('fetches')} where roaster_id = 'pill.com'"
    assert lake.connect().execute(q).fetchone()[0].startswith("RecursionError")


def test_a_quarantined_roaster_has_no_request_and_counts_apart(lake, monkeypatch):
    stores = _stores(2)
    monkeypatch.setattr(ingest, "fetch_catalog", lambda client, r, *a, **k: [_item(r)])
    out = ingest.run(lake, stores, WEEK, workers=2, quarantine={"pill.com": "crashed: in flight when 2 died"})
    assert out["quarantined"] == ["pill.com"]
    assert out["by_adapter"]["generic"]["quarantined"] == 1
    assert out["by_adapter"]["generic"]["errors"] == 0  # not an error: it was never fetched
    assert connectors._ok_errors(out) == (2, 0)
    assert out["first_contact"]["stores"] == 2  # never asked: no first contact
    assert _lake_statuses(lake)["pill.com"] == ["crashed"]
    done, _ = ingest._done_and_tried(lake, WEEK)
    assert "pill.com" in done


def test_a_leftover_folder_of_a_finished_attempt_is_not_dead(tmp_path):
    root = tmp_path / "_inflight"
    crashguard.Guard(root, "a1").start("x.com")  # killed between its end row and its cleanup
    crashguard.Guard(root, "a2").start("x.com")
    health = crashguard.assess(root, finished={"a1"})
    assert (
        health.dead == ["a2"] and health.suspects == {"x.com": ["a2"]} and not (root / "attempt=a1").exists()
    )


def test_markers_need_no_metadata_calls(tmp_path, monkeypatch):
    """/mnt/staging is GCSFuse: no chmod. The guard only creates, writes and deletes files."""
    from .test_connectors import _no_fuse_metadata_calls

    _no_fuse_metadata_calls(monkeypatch)
    g = crashguard.Guard(tmp_path / "_inflight", "a1")
    g.start("x.com")
    assert (g.dir / "x.com.json").exists()
    g.end("x.com")
    g.close()
    assert not g.dir.exists()


def test_a_retried_task_attempt_stays_inside_its_executions_window(tmp_path, monkeypatch):
    lake = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "crawl-generic-abc12")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "0")
    first = control.start(lake, "crawl", WEEK, "generic", 10, detail=control.execution())
    assert (
        connectors._window_left(lake, "crawl", "generic", WEEK, 225) == 225
    )  # the first attempt: full window
    first_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=100)
    control.write(lake, first | {"started_at": first_at})  # it started 100 min ago, then died
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    assert connectors._window_left(lake, "crawl", "generic", WEEK, 225) == pytest.approx(125, abs=1)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "crawl-generic-other")  # another execution: its own window
    assert connectors._window_left(lake, "crawl", "generic", WEEK, 225) == 225


def test_crawl_retry_resumes_only_its_connectors_parked_stores_with_control_rows(tmp_path, monkeypatch):
    lake = Lake(str(tmp_path / "lake"))
    shop = Roaster(roaster_id="shop.com", name="s", host="shop.com", platform="shopify")
    other = Roaster(roaster_id="other.com", name="o", host="other.com", platform="custom")
    monkeypatch.setattr(
        registry,
        "current",
        lambda lake, *a, **k: [registry.RoasterState(r, "active", None) for r in (shop, other)],
    )
    plake = connectors.PartitionLake(lake, "shopify", WEEK)
    for rid in ("shop.com", "other.com"):
        ingest.save_parked(plake, WEEK, rid, {"page": 2, "items": []})
    seen: list[tuple[str, dict | None]] = []

    def fetch(client, roaster, resume=None, known=None):
        seen.append((roaster.roaster_id, resume))
        return [_item(roaster)]

    monkeypatch.setattr(ingest, "fetch_catalog", fetch)
    out = connectors.crawl_retry(lake, "shopify", WEEK)
    assert seen == [("shop.com", {"page": 2, "items": []})] and out["roasters"] == 1
    [row] = control.rows(lake, WEEK)
    assert (row["job"], row["connector"], row["status"]) == ("crawl-retry", "shopify", "done")
    assert control.landed(lake, WEEK) == set()  # a retry pass never counts as the week's crawl


# --- the response size cap and the response <-> stream cycle (crawl-generic's OOM, 2026-11-02) ---------------------


def _client(handler, **kw) -> http.PoliteClient:
    def route(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else handler(req)

    return http.PoliteClient(min_interval=0, transport=httpx.MockTransport(route), **kw)


def test_a_response_over_the_cap_fails_without_being_held(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    sent = {"n": 0}

    def streamed(req):  # no content-length: counted as it is read
        def body():
            for _ in range(64):
                sent["n"] += 1
                yield b"x" * 1024

        return httpx.Response(200, content=body())

    c = _client(streamed, max_response_bytes=10 * 1024)
    with pytest.raises(http.ResponseTooLarge):
        c.get("https://a.com/products/big")
    assert sent["n"] <= 11  # it stopped reading at the cap, and never retried
    said = _client(lambda req: httpx.Response(200, headers={"content-length": str(1 << 30)}, content=b""))
    with pytest.raises(http.ResponseTooLarge, match="1073741824 bytes"):
        said.get("https://a.com/products/huge")
    ok = _client(lambda req: httpx.Response(200, json={"products": []}), max_response_bytes=10 * 1024)
    assert ok.fetch_json("https://a.com/products.json") == {"products": []}


def test_a_page_over_the_cap_is_that_roasters_error(lake, monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(
                200, text=f"<urlset><loc>https://{req.url.host}/products/coffee-1</loc></urlset>"
            )
        size = 1 << 20 if req.url.host == "pill.com" else 100
        return httpx.Response(200, text="<html>" + "y" * size + "</html>")

    out = ingest.run(
        lake,
        _stores(1),
        WEEK,
        workers=2,
        client_kw={
            "transport": httpx.MockTransport(handler),
            "min_interval": 0,
            "max_response_bytes": 64 * 1024,
        },
    )
    assert any(f.startswith("pill.com: ResponseTooLarge") for f in out["failures"])
    assert _lake_statuses(lake)["s0.com"] == ["ok"]


def test_a_read_response_is_freed_without_waiting_for_the_garbage_collector():
    """An httpx response refers to itself through its stream; PoliteClient breaks that cycle once the body is read,
    so 32 workers' pages don't pile up between full collections."""
    c = _client(lambda req: httpx.Response(200, text="x" * 100_000))
    gc.disable()
    try:
        r = c.get("https://a.com/p")
        assert len(r.text) == 100_000
        w = weakref.ref(r)
        del r
        assert w() is None
    finally:
        gc.enable()


def test_sitemap_reads_every_url_with_no_count_limit():
    """The old limit of 5,000 URLs is gone: only the byte cap (SITEMAP_MAX_BYTES) bounds a sitemap."""
    from coffeedata import sitemap

    body = (
        "<urlset>"
        + "".join(f"<url><loc>https://a.com/products/c{i}</loc></url>" for i in range(20_000))
        + "</urlset>"
    )
    c = _client(lambda req: httpx.Response(200, text=body))
    urls = sitemap.sitemap_urls(c, "a.com")
    assert urls == [f"https://a.com/products/c{i}" for i in range(20_000)]


def test_a_sitemap_over_the_response_cap_but_within_the_protocols_50_mb_is_read(monkeypatch, caplog):
    """Sitemap requests get their own cap (SITEMAP_MAX_BYTES, above the protocol's 50 MB); a product page still
    gets the client's. Past SITEMAP_MAX_BYTES a sitemap is out of spec: generic's fetch fails (its only list of
    product pages), Squarespace's falls back to the common shop paths and says why."""
    from coffeedata import sitemap
    from coffeedata.adapters import generic, squarespace

    assert sitemap.SITEMAP_MAX_BYTES >= 50 * 2**20 > http.MAX_RESPONSE_BYTES  # 32 MiB by default
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(sitemap, "SITEMAP_MAX_BYTES", 64 * 1024)
    body = (
        "<urlset>"
        + "".join(f"<url><loc>https://a.com/products/c{i}</loc></url>" for i in range(500))
        + "</urlset>"
    )
    assert 8 * 1024 < len(body) < 64 * 1024

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=body)
        return httpx.Response(200, text="y" * 9 * 1024)

    c = _client(handler, max_response_bytes=8 * 1024)
    assert len(sitemap.sitemap_urls(c, "a.com")) == 500
    assert c.max_response_bytes == 8 * 1024  # the sitemap's cap was a view's, not the client's
    with pytest.raises(http.ResponseTooLarge):
        c.get("https://a.com/products/c0")
    roomy = _client(handler, max_response_bytes=1 << 20)  # a client cap above the sitemap's is never lowered
    assert roomy.with_max_response(max(roomy.max_response_bytes, sitemap.SITEMAP_MAX_BYTES)) is roomy

    monkeypatch.setattr(sitemap, "SITEMAP_MAX_BYTES", 8 * 1024)  # now the sitemap is over its cap
    r = Roaster(roaster_id="a.com", name="a", host="a.com", platform="custom")
    with pytest.raises(http.ResponseTooLarge):
        generic.fetch(c, r)
    tried: list[str] = []

    def shop(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=body)
        tried.append(req.url.path)
        return httpx.Response(404)

    with caplog.at_level("WARNING"):
        assert squarespace.fetch(_client(shop, max_response_bytes=8 * 1024), r) == []
    assert tried == squarespace.COMMON_SHOP_PATHS
    assert "a.com: sitemap unreadable" in caplog.text and "response over" in caplog.text


def test_an_oversized_robots_txt_is_remembered_and_counted(monkeypatch):
    """A robots.txt over the cap (a catch-all page at that path) is a RobotsUnavailable remembered for
    ROBOTS_RETRY_AFTER, so the host's later fetches don't download it again; it is counted in RequestStats."""
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    asked = {"robots": 0}

    def route(req):
        if req.url.path == "/robots.txt":
            asked["robots"] += 1
            return httpx.Response(200, text="z" * 20 * 1024)
        return httpx.Response(200, text="ok")

    c = http.PoliteClient(min_interval=0, transport=httpx.MockTransport(route), max_response_bytes=8 * 1024)
    with c.track() as stats, pytest.raises(http.RobotsUnavailable, match="too large"):
        c.get("https://a.com/p1")
    assert stats.requests == 1 and stats.statuses == {200: 1} and stats.pages == 0  # robots.txt: not a page
    with pytest.raises(http.RobotsUnavailable, match="recently"):
        c.get("https://a.com/p2")
    assert asked["robots"] == 1


def test_a_response_over_the_cap_is_counted_with_its_status(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    c = _client(lambda req: httpx.Response(200, text="x" * 20 * 1024), max_response_bytes=8 * 1024)
    with c.track() as stats, pytest.raises(http.ResponseTooLarge) as e:
        c.get("https://a.com/products/big")
    assert e.value.status == 200 and not ingest.blocked(e.value)
    assert stats.requests == 2 and stats.statuses == {200: 1, 404: 1}  # robots.txt (404), then the page


# --- the Cloud Run shape (src/terraform/ingestion.tf; terraform validate runs where terraform is installed) ----------


def _tf() -> str:
    from pathlib import Path

    return (Path(__file__).parents[1] / "src/terraform/ingestion.tf").read_text()


def test_shopify_woocommerce_and_generic_crawls_never_retry_in_place():
    import re

    jobs = dict(re.findall(r'^\s+"([a-z-]+)"\s+=\s+\{ command = .*retries = (\d) \}$', _tf(), re.M))
    assert {j: int(n) for j, n in jobs.items() if j.startswith("crawl") or j == "fetch-health"} == {
        "crawl-shopify": 0, "crawl-shopify-default-egress": 0, "crawl-retry": 0, "crawl-squarespace": 1,
        "crawl-woocommerce": 0, "crawl-square-online": 1, "crawl-generic": 0,
        "fetch-health": 0,  # a retry would find the check done and exit 0, hiding its alert
    }  # fmt: skip
    assert "max_retries = each.value.retries" in _tf()
    assert re.search(
        r'"crawl-generic" += \{.*memory = "8Gi"', _tf()
    )  # the owner's hand-set 8Gi, kept by an apply
    assert "lookup(var.ingest_job_memory, each.key, each.value.memory)" in _tf()


def test_every_connector_has_a_third_pass_in_the_evening():
    import re

    slots = re.findall(r'^\s+"([a-z-]+)"\s+= \{ job = "([a-z-]+)", cron = "([^"]+)" \}', _tf(), re.M)
    evening = {job for _, job, cron in slots if cron == "47 21 * * 1"}
    assert evening == {
        "crawl-retry",
        "crawl-squarespace",
        "crawl-woocommerce",
        "crawl-square-online",
        "crawl-generic",
    }
    # Shopify's evening pass is crawl-retry, on its NAT; the others run their own crawl job, on that job's egress
    assert re.search(
        r'"crawl-retry"\s+= \{ command = \["coffeedata"\], args = \["crawl-retry", "--connector", "shopify"\], egress = "shopify"',
        _tf(),
    )
    assert set(connectors.CONNECTORS) == {"shopify", "squarespace", "woocommerce", "square_online", "generic"}


def _block(kind: str, name: str) -> str:
    """One top-level block of ingestion.tf, e.g. _block("resource", 'google_compute_router_nat" "shopify_egress')."""
    tf = _tf()
    start = tf.index(f'{kind} "{name}" {{')
    return tf[start : tf.index("\n}\n", start) + 2]


def _job_egress() -> dict[str, str]:
    """Each job in local.ingest_jobs => its egress, from the one-line and the multi-line entries alike."""
    import re

    jobs = _tf()[_tf().index("ingest_jobs = {") : _tf().index("ingest_schedule = {")]
    return dict(
        re.findall(
            r'^\s+"([a-z-]+)"\s+=\s+\{(?:[^{}]|\[[^]]*\])*?egress\s+=\s+"([a-z_-]+)"', jobs, re.M | re.S
        )
    )


def test_each_job_leaves_through_the_egress_the_owner_chose():
    """Shopify's crawls on Shopify's NAT IP, WooCommerce's and generic's (every pass: resume and evening retry run the
    same jobs) on a second NAT IP, everything else on default egress."""
    import re

    assert _job_egress() == {
        "discover": "default", "probe": "default",
        "crawl-shopify": "shopify", "crawl-shopify-default-egress": "default", "crawl-retry": "shopify",
        "crawl-squarespace": "default", "crawl-woocommerce": "woo_generic", "crawl-square-online": "default",
        "crawl-generic": "woo_generic", "merge": "default", "enrich-week": "default", "build": "default",
        "fetch-health": "default",  # it reads the lake and crawls no store
        "publish": "default",  # the admin's Publish and Roll back: serves a build, crawls no store
    }  # fmt: skip
    slots = re.findall(r'^\s+"([a-z-]+)"\s+= \{ job = "([a-z-]+)", cron = "[^"]+" \}', _tf(), re.M)
    woo_generic = {entry for entry, job in slots if _job_egress()[job] == "woo_generic"}
    assert woo_generic == {
        f"crawl-{c}{p}" for c in ("woocommerce", "generic") for p in ("", "-resume", "-retry")
    }
    assert (
        "nat " not in _tf()[_tf().index("ingest_jobs = {") : _tf().index("ingest_schedule = {")]
    )  # no old flag
    job = _block("resource", 'google_cloud_run_v2_job" "ingest')
    assert 'for_each = each.value.egress == "default" ? [] : [local.egress_nats[each.value.egress]]' in job
    assert "subnetwork = vpc_access.value.subnet" in job
    assert (
        'value = each.value.egress == "default" ? var.egress_echo_url : local.egress_nats[each.value.egress].ip'
        in job
    )
    assert "contains(keys(local.egress_nats), each.value.egress)" in job  # a misspelt egress fails at plan
    assert "google_compute_router_nat.woo_generic_egress" in job  # jobs wait for their NAT
    nats = _tf()[_tf().index("egress_nats = {") :]
    nats = nats[: nats.index("\n  }\n")]
    assert re.search(
        r'"shopify"\s+= \{ subnet = google_compute_subnetwork\.shopify_egress\.id, ip = google_compute_address\.shopify_egress\.address \}',
        nats,
    )
    assert re.search(
        r'"woo_generic" = \{ subnet = google_compute_subnetwork\.woo_generic_egress\.id, ip = google_compute_address\.woo_generic_egress\.address \}',
        nats,
    )


def test_shopifys_egress_is_untouched_and_the_second_nat_is_separate():
    """Shopify's subnet, IP and NAT keep their addresses, names and settings (nothing existing is replaced); the
    WooCommerce/generic gateway has its own subnet and IP, on the same router, with the same port settings."""
    import ipaddress
    import re

    def default(var: str) -> str:
        return re.search(r'default\s+=\s+"([^"]+)"', _block("variable", var)).group(1)

    shopify_net = ipaddress.ip_network(default("ingest_subnet_cidr"))
    woo_net = ipaddress.ip_network(default("woo_generic_subnet_cidr"))
    assert str(shopify_net) == "10.8.0.0/26" and woo_net.prefixlen <= 26 and not shopify_net.overlaps(woo_net)

    for which, cidr in (
        ("shopify_egress", "ingest_subnet_cidr"),
        ("woo_generic_egress", "woo_generic_subnet_cidr"),
    ):
        dash = which.replace("_", "-")
        subnet = _block("resource", f'google_compute_subnetwork" "{which}')
        assert f'name          = "${{var.service}}-{dash}"' in subnet and f"var.{cidr}" in subnet
        assert "network       = google_compute_network.ingest.id" in subnet
        address = _block("resource", f'google_compute_address" "{which}')
        assert (
            f'name         = "${{var.service}}-{dash}"' in address and 'address_type = "EXTERNAL"' in address
        )
        nat = _block("resource", f'google_compute_router_nat" "{which}')
        assert f'name                               = "${{var.service}}-{dash}"' in nat
        assert "router                             = google_compute_router.ingest.name" in nat  # one router
        assert f"nat_ips                            = [google_compute_address.{which}.self_link]" in nat
        assert re.findall(r"name\s+= (google_compute_subnetwork\.\w+)\.id", nat) == [
            f"google_compute_subnetwork.{which}"
        ]
        for setting in (
            'nat_ip_allocate_option             = "MANUAL_ONLY"',
            'source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"',
            "enable_dynamic_port_allocation      = true",
            "enable_endpoint_independent_mapping = false",
            "min_ports_per_vm                    = 1024",
            "max_ports_per_vm                    = 8192",
            'source_ip_ranges_to_nat = ["ALL_IP_RANGES"]',
            'filter = "ERRORS_ONLY"',
        ):
            assert setting in nat, (which, setting)
    assert _tf().count('resource "google_compute_router" ') == 1
    # a /26 or larger, and never overlapping Shopify's subnet: checked at plan, before anything is created
    assert "<= 26" in _block("variable", "woo_generic_subnet_cidr")
    assert "overlaps ingest_subnet_cidr" in _block(
        "resource", 'google_compute_subnetwork" "woo_generic_egress'
    )
    assert "precondition" not in _block("resource", 'google_compute_subnetwork" "shopify_egress')
    assert _tf().count('resource "google_compute_network" ') == 1
    assert _tf().count('resource "google_compute_router_nat" ') == 2


def test_both_egress_ips_are_outputs():
    assert "google_compute_address.shopify_egress.address" in _block("output", "shopify_egress_ip")
    assert "google_compute_address.woo_generic_egress.address" in _block("output", "woo_generic_egress_ip")


def test_job_memory_overrides_are_range_checked_and_must_name_a_job():
    tf = _tf()
    assert "mib >= 1024 && mib <= 8192" in tf  # 1Gi to 8Gi: "16Gi" fails at plan, not at apply
    assert (
        "length(setsubtract(keys(var.ingest_job_memory), keys(local.ingest_jobs))) == 0" in tf
    )  # a misspelt key


def test_the_schedule_alert_names_each_entrys_job_correctly():
    """The email tells the owner which job to run by hand: the entry's name without `-resume` or `-retry`, except
    `crawl-retry`, whose job is itself. That rule, applied to every Scheduler entry, gives its real job."""
    import re
    from pathlib import Path

    alert = (Path(__file__).parents[1] / "src/terraform/alerts.tf").read_text()
    assert "except for `${var.service}-crawl-retry`, whose job is `crawl-retry` itself" in alert
    slots = re.findall(r'^\s+"([a-z-]+)"\s+= \{ job = "([a-z-]+)", cron = "[^"]+" \}', _tf(), re.M)
    assert len(slots) >= 20
    for entry, job in slots:
        named = entry if entry == "crawl-retry" else re.sub(r"-(resume|retry)$", "", entry)
        assert named == job, entry
