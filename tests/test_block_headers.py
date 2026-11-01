"""Block-header logging: every 403/429/430/503 lands in its fetch row (raw.fetches.blocks) with the headers that tell
a rate limit from an edge challenge, and the egress IP; each job's manifest row counts first-contact refusals per IP."""

import httpx
import pytest
import respx

from coffeedata import control, http, ingest
from coffeedata.models import Roaster
from tests.test_ratelimit import FAST, fake_time

FIELDS = {
    "status",
    "server",
    "cf_mitigated",
    "content_type",
    "retry_after",
    "cf_ray",
    "x_request_id",
    "egress_ip",
}


def _r(host: str) -> Roaster:
    return Roaster(roaster_id=host, name="R", host=host, platform="shopify")


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        yield m


@pytest.fixture(autouse=True)
def fixed_egress(monkeypatch):
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "203.0.113.7")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)


def test_every_block_status_is_recorded_with_its_headers_and_egress_ip(lake, mock, monkeypatch):
    fake_time(monkeypatch)
    limited = {"server": "cloudflare", "content-type": "application/json", "retry-after": "60",
               "cf-ray": "8f1a2b3c4d5e6f70-SJC", "x-request-id": "req-429"}  # fmt: skip
    challenge = {"server": "cloudflare", "cf-mitigated": "challenge", "content-type": "text/html",
                 "cf-ray": "8f1a2b3c4d5e6f71-SJC"}  # fmt: skip
    mock.get("https://slow.com/products.json").mock(return_value=httpx.Response(429, headers=limited))
    mock.get("https://edge.com/products.json").mock(
        return_value=httpx.Response(503, headers=challenge, text="<html>Verifying your connection</html>")
    )
    mock.get("https://shut.com/products.json").mock(
        return_value=httpx.Response(403, headers={"server": "nginx"})
    )
    mock.get("https://wall.com/products.json").mock(return_value=httpx.Response(430))
    mock.get("https://fine.com/products.json").mock(side_effect=[httpx.Response(200, json={"products": []})])
    out = ingest.run(lake, [_r(h) for h in ("slow.com", "edge.com", "shut.com", "wall.com", "fine.com")],
                     "2026-11-02", workers=1, shopify_rps=1000, client_kw=FAST)  # fmt: skip

    con = lake.connect()
    rows = con.execute(
        f"select roaster_id, blocks from {lake.scan('fetches')} order by fetched_at"
    ).fetchall()
    blocks: dict[str, list[dict]] = {}
    for rid, bs in rows:
        blocks.setdefault(rid, []).extend(bs or [])
    assert not blocks["fine.com"]  # a 200 records nothing
    for rid, status in (("slow.com", 429), ("edge.com", 503), ("shut.com", 403), ("wall.com", 430)):
        assert blocks[rid], rid
        b = blocks[rid][0]
        assert set(b) >= FIELDS and b["status"] == status and b["egress_ip"] == "203.0.113.7"
        assert b["host"] == rid and b["path"] == "/products.json" and b["at"] is not None
    first = blocks["slow.com"][0]
    assert (first["server"], first["content_type"], first["retry_after"], first["cf_ray"], first["x_request_id"]) == (
        "cloudflare", "application/json", "60", "8f1a2b3c4d5e6f70-SJC", "req-429")  # fmt: skip
    edge = blocks["edge.com"][0]
    assert (edge["cf_mitigated"], edge["content_type"], edge["retry_after"]) == (
        "challenge",
        "text/html",
        None,
    )
    assert all(len(bs) <= http.MAX_BLOCKS_PER_FETCH for _, bs in rows if bs)

    # per egress IP: the stores first contacted from it, and those that refused that first contact
    fc = out["first_contact"]
    assert fc["by_ip"] == {"203.0.113.7": {"stores": 5, "refused": fc["refused"]}} and fc["refused"] >= 2
    job = control.finish(
        lake, control.start(lake, "crawl", "2026-11-02", "shopify", 5), "done", first_contact=fc
    )
    assert job["egress_ips"] == ["203.0.113.7"]
    assert job["refusals"] == [{"ip": "203.0.113.7", "stores": 5, "refused": fc["refused"]}]


def test_egress_ip_from_an_echo_endpoint_is_reused_for_a_minute(monkeypatch):
    monkeypatch.delenv("COFFEEDATA_EGRESS_IP")
    monkeypatch.setenv("COFFEEDATA_EGRESS_ECHO_URL", "https://echo.example/ip")
    monkeypatch.setattr(http, "_egress", {"ip": None, "at": None})
    clock = [100.0]
    monkeypatch.setattr(http.time, "monotonic", lambda: clock[0])
    with respx.mock() as m:
        echo = m.get("https://echo.example/ip").mock(
            side_effect=[httpx.Response(200, text="198.51.100.4\n"), httpx.Response(200, text="198.51.100.9")]
        )
        assert http.egress_ip() == "198.51.100.4"
        clock[0] += 30
        assert http.egress_ip() == "198.51.100.4" and echo.call_count == 1
        clock[0] += http.EGRESS_TTL
        assert http.egress_ip() == "198.51.100.9" and echo.call_count == 2


def test_no_egress_configured_is_unknown_and_never_looked_up(monkeypatch):
    monkeypatch.delenv("COFFEEDATA_EGRESS_IP")
    monkeypatch.delenv("COFFEEDATA_EGRESS_ECHO_URL", raising=False)
    with respx.mock() as m:
        assert http.egress_ip() is None
        assert not m.calls
