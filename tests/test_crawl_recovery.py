"""Crawl recovery after the 2026-11-16 run (triage on notes/NEXT-STEPS.md, 2026-11-17), each case reproduced:

- D2: 66 roasters stuck `unreachable` were never probed again: probe_todo only took roasters probed more than
  --stale-days (28) ago, and the seed says every roaster was probed 2026-10-26 (todo 0, probes table empty).
- A: 36 generic stores whose robots.txt got no HTTP response were recorded only as `RobotsUnavailable <url>`: the
  fetch row now says which phase failed (fetches.transport_errors).
- 5: an unreachable robots.txt may be stood in for by the last one stored, at most 30 days old (RFC 9309 2.4).
- E: registry corrections (kaldi.com, galvestoncoffeeroasters.com) through the seed.

No test sends a request anywhere: every response comes from an httpx.MockTransport.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from coffeedata import cli, discover, http, ingest, registry
from coffeedata.http import Blocked, PoliteClient, RobotsUnavailable, SiteDisallowed
from coffeedata.lake import SCHEMAS
from coffeedata.models import Roaster

NOW = datetime(2026, 11, 23, 9, 17, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_sleep_no_egress(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "egress_ip", lambda: "203.0.113.7")  # never ask the echo service


def _state(rid, status, last, failed=0):
    return types.SimpleNamespace(
        status=status, last_probed=last, failed_probes=failed, roaster=types.SimpleNamespace(roaster_id=rid)
    )


# --- D2: unreachable roasters are probed again, on a backoff ---------------------------------------------------------


def test_an_unreachable_roaster_is_probed_again_within_the_week():
    """The 2026-11-16 case: an unreachable roaster whose last probe is the seed's (2026-10-26) was never asked
    again before 2026-11-30 (on main, probe_todo returns [] here)."""
    states = [
        _state("meanbeanroasters.com", "unreachable", "2026-11-02"),
        _state("ok.com", "active", "2026-10-26"),
    ]
    assert cli.probe_todo(states, "2026-11-16", 28) == ["meanbeanroasters.com"]
    assert cli.probe_todo(states, "2026-11-23", 28) == ["meanbeanroasters.com"]


def test_the_backoff_doubles_with_each_failed_probe_and_stops_at_stale_days():
    assert [cli.unreachable_backoff_days(n, 7, 28) for n in range(5)] == [7, 14, 28, 28, 28]
    s = _state("down.com", "unreachable", "2026-11-09", failed=1)  # one failed probe since: 14 days
    assert cli.probe_todo([s], "2026-11-16", 28) == []
    assert cli.probe_todo([s], "2026-11-23", 28) == ["down.com"]


def test_unreachable_roasters_due_come_after_new_ones_and_before_the_stale():
    states = [_state("stale.com", "active", "2026-09-01"), _state("down.com", "unreachable", "2026-11-02"),
              _state("new.com", "pending", None), _state("gone.com", "opted_out", None)]  # fmt: skip
    assert cli.probe_todo(states, "2026-11-16", 28) == ["new.com", "down.com", "stale.com"]


def _seed(tmp_path: Path, rows: list[dict]) -> Path:
    import csv

    p = tmp_path / "roasters.csv"
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=registry.SEED_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: "" for k in registry.SEED_FIELDS} | r)
    return p


def _fetch(rid, run_date, status, at, adapter="woocommerce", products=0, error=None):
    return {"run_date": date.fromisoformat(run_date), "batch_id": "b", "fetched_at": at, "roaster_id": rid,
            "adapter": adapter, "status": status, "error": error, "products": products, "coffees": None}  # fmt: skip


MEANBEAN = {"roaster_id": "meanbeanroasters.com", "name": "Mean Bean Roasters", "state": "IL", "sources": "overture",
            "opted_out": "false", "final_host": "www.meanbeancoffeeroasters.com", "platform": "http_404",
            "last_probed": "2026-11-02", "country": "US"}  # fmt: skip


def test_meanbean_unreachable_after_a_transient_404_is_reprobed_and_returns(lake, tmp_path, monkeypatch):
    """The named case (lake copy of run 2026-11-16): ok 10-26 and 10-28 (387 products); on 11-02 its host answered
    302, 302, then 404 on the Store API, and the seed refresh that day recorded platform http_404 from that probe;
    ok again on 11-09 (387 products, 154 pages); `unreachable` on 11-16 (a seed platform http_* is unreachable),
    so not crawled. Its robots.txt allows everyone. The re-probe asks it before the 11-23 crawl, and an ok probe
    puts it back in the crawl set."""
    seed = _seed(tmp_path, [MEANBEAN])
    lake.write_partition("fetches", "2026-11-09", "b1", [
        _fetch("meanbeanroasters.com", "2026-11-09", "ok", datetime(2026, 11, 9, 9, 20, tzinfo=UTC), products=387)])  # fmt: skip
    monkeypatch.setattr(registry, "SEED", seed)
    (before,) = registry.current(lake, seed)
    assert before.status == "unreachable"
    asked = []

    def probe_one(client, d):
        asked.append(d)
        return {
            "status": "ok",
            "final_host": "meanbeanroasters.com",
            "platform": "woocommerce",
            "http_status": 200,
        }

    monkeypatch.setattr(discover, "probe_one", probe_one)
    monkeypatch.setattr(
        registry, "current", lambda lk, s=seed, dropped=frozenset(): registry._current(lk, seed)
    )
    args = argparse.Namespace(stale_days=28, unreachable_days=7, only=None, sample=None, workers=1,
                              run_date="2026-11-23", deadline_min=None)  # fmt: skip
    cli.cmd_probe(args)
    assert asked == ["meanbeanroasters.com"]
    # the probe's row is stored in the lake (the probes table held only its schema on 2026-11-16)
    rows = [
        r
        for f in sorted((lake.root / "probes").glob("run_date=2*/*.parquet"))
        for r in pq.read_table(f).to_pylist()
    ]
    assert [(r["roaster_id"], r["status"], r["platform"]) for r in rows] == [
        ("meanbeanroasters.com", "ok", "woocommerce")
    ]
    (after,) = registry._current(lake, seed)
    assert (
        after.status not in ("unreachable", "opted_out", "alias") and after.roaster.adapter == "woocommerce"
    )
    assert ingest.crawlable(after, set())


def test_failed_probes_since_the_last_ok_one_are_counted(lake, tmp_path):
    seed = _seed(tmp_path, [MEANBEAN])

    def probe(at, status):
        return {"run_date": at.date(), "batch_id": "p", "probed_at": at, "roaster_id": "meanbeanroasters.com",
                "status": status, "http_status": None, "final_host": "", "platform": ""}  # fmt: skip

    lake.write_partition("probes", "2026-11-09", "p1", [
        probe(datetime(2026, 11, 9, tzinfo=UTC), "ok") | {"platform": "woocommerce"},
        probe(datetime(2026, 11, 16, tzinfo=UTC), "unreachable"),
        probe(datetime(2026, 11, 23, tzinfo=UTC), "http_404"),
        probe(datetime(2026, 11, 24, tzinfo=UTC), "robots_unavailable"),  # no answer: not counted
    ])  # fmt: skip
    (s,) = registry._current(lake, seed)
    assert s.failed_probes == 2


def test_kaldi_com_and_galveston_are_not_roasters():
    """kaldi.com is an espresso-equipment parts catalog (the roaster is kaldiscoffee.com); galvestoncoffeeroasters.com
    redirects to an unrelated site. Both are dropped through the seed's not_a_roaster rows."""
    assert registry.not_a_roaster_site("kaldi.com") and registry.not_a_roaster_site(
        "galvestoncoffeeroasters.com"
    )
    assert not registry.not_a_roaster_site("kaldiscoffee.com")


# --- A: a request with no HTTP response says why ---------------------------------------------------------------------


def _wrapped(exc_type, inner: BaseException):
    try:
        try:
            raise inner
        except BaseException as e:
            raise exc_type(str(inner)) from e
    except exc_type as outer:
        return outer


@pytest.mark.parametrize(
    "error, phase",
    [
        (
            _wrapped(httpx.ConnectError, ConnectionResetError(104, "Connection reset by peer")),
            "connection_reset",
        ),
        (
            _wrapped(httpx.ConnectError, ConnectionRefusedError(111, "Connection refused")),
            "connection_refused",
        ),
        (_wrapped(httpx.ConnectError, socket.gaierror(-2, "Name or service not known")), "dns"),
        (
            _wrapped(httpx.ConnectError, ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")),
            "tls",
        ),
        (httpx.ConnectError("[Errno -3] Temporary failure in name resolution"), "dns"),
        (httpx.ConnectTimeout("timed out"), "connect_timeout"),
        (httpx.ReadTimeout("timed out"), "read_timeout"),
        (httpx.RemoteProtocolError("Server disconnected without sending a response."), "protocol"),
    ],
)
def test_transport_phase(error, phase):
    assert http.transport_phase(error) == phase


def test_robots_txt_with_no_response_is_recorded_with_its_phase(lake):
    """The 2026-11-16 case: robots.txt failed fast three times and the row said only `RobotsUnavailable <url>`."""

    def handler(request):
        raise _wrapped(httpx.ConnectError, ConnectionResetError(104, "Connection reset by peer"))

    r = Roaster(roaster_id="batdorf.com", name="B", host="batdorf.com", platform="other")
    with PoliteClient(transport=httpx.MockTransport(handler), min_interval=0) as c:
        res = ingest.ingest_one(c, r)
    assert res.error and "RobotsUnavailable" in res.error
    recs = res.stats.transport_errors
    assert len(recs) == 3 and {(t["phase"], t["robots_txt"], t["path"]) for t in recs} == {
        ("connection_reset", True, "/robots.txt")
    }
    assert recs[0]["error_class"] == "ConnectError" and recs[0]["egress_ip"] == "203.0.113.7"
    rows = ingest.rows_for([res], date(2026, 11, 16), "b", set())
    path = lake.write_partition("fetches", "2026-11-16", "b", rows["fetches"])
    (row,) = pq.read_table(path).to_pylist()
    assert row["transport_errors"][0]["phase"] == "connection_reset" and row["robots"] is None


# --- 5: the last good robots.txt stands in for an unreachable one ----------------------------------------------------


def _copy(days: float, status=200, body="User-agent: *\nDisallow: /private\n"):
    return {"store.com": {"fetched_at": NOW - timedelta(days=days), "status": status, "body": body}}


def _client(handler, copies=None):
    return PoliteClient(
        transport=httpx.MockTransport(handler), min_interval=0, robots_copies=copies, clock=lambda: NOW
    )


def _down_robots(page_status=200):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(page_status, json={"products": []})

    return handler, seen


def test_a_29_day_copy_is_used_and_followed_exactly():
    handler, seen = _down_robots()
    with _client(handler, _copy(29)) as c, c.track() as stats:
        assert c.get("https://store.com/products.json").status_code == 200
        with pytest.raises(Blocked):
            c.get("https://store.com/private/x")  # the copy's own rule
    assert "/private/x" not in seen
    (rec,) = stats.robots
    assert rec["source"] == "cached" and rec["status"] == 200 and rec["body"] is None
    assert rec["age_s"] == pytest.approx(29 * 86400)


def test_a_31_day_copy_is_not_used():
    handler, seen = _down_robots()
    with _client(handler, _copy(31)) as c, pytest.raises(RobotsUnavailable):
        c.get("https://store.com/products.json")
    assert seen == ["/robots.txt"] * 3  # nothing but robots.txt was asked


def test_no_copy_behaves_as_before():
    handler, seen = _down_robots()
    with _client(handler, {}) as c, pytest.raises(RobotsUnavailable, match="phase connect_timeout"):
        c.get("https://store.com/products.json")
    assert set(seen) == {"/robots.txt"}


def test_a_copy_that_disallows_everything_is_honoured():
    handler, seen = _down_robots()
    with _client(handler, _copy(2, body="User-agent: *\nDisallow: /\n")) as c, pytest.raises(SiteDisallowed):
        c.get("https://store.com/products.json")
    assert set(seen) == {"/robots.txt"}


def test_a_4xx_copy_means_no_rules():
    handler, _ = _down_robots()
    with _client(handler, _copy(2, status=404, body=None)) as c:
        assert c.get("https://store.com/anything").status_code == 200


def test_a_403_copy_disallows_everything():
    handler, _ = _down_robots()
    with _client(handler, _copy(2, status=403, body=None)) as c, pytest.raises(Blocked):
        c.get("https://store.com/anything")


def test_a_500_uses_the_copy_but_a_pause_does_not():
    def handler_for(status, headers=None):
        def handler(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(status, headers=headers or {}, text="oops")
            return httpx.Response(200, json={})

        return handler

    with _client(handler_for(500), _copy(2)) as c:
        assert c.get("https://store.com/products.json").status_code == 200
    for status in (429, 503):  # a block is a pause: waited for, never read past with a copy
        with _client(handler_for(status), _copy(2)) as c, pytest.raises(RobotsUnavailable):
            c.get("https://store.com/products.json")


@pytest.mark.parametrize(
    "status, headers",
    [
        (403, {"cf-mitigated": "challenge"}),
        (202, {"sg-captcha": "challenge"}),
        (429, {"cf-mitigated": "challenge"}),
    ],
)
def test_a_challenge_never_uses_a_copy(status, headers):
    def handler(request):
        return httpx.Response(status, headers=headers, text="<html>challenge</html>")

    with _client(handler, _copy(1)) as c, c.track() as stats, pytest.raises(RobotsUnavailable) as e:
        c.get("https://store.com/products.json")
    assert e.value.challenge and stats.robots == []


def test_a_live_robots_txt_is_recorded_with_its_body_once_and_blocks_say_which_copy():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(429, headers={"retry-after": "1"})

    with _client(handler) as c:
        with c.track() as first, pytest.raises(httpx.HTTPStatusError):
            c.get("https://store.com/a")  # 429 on every try
        with c.track() as second, pytest.raises(httpx.HTTPStatusError):
            c.get("https://store.com/b")
    assert (
        second.robots[0]["source"] == "live" and second.robots[0]["body"] is None
    )  # the body is stored once
    assert first.robots[0]["source"] == "live" and first.robots[0]["body"] == "User-agent: *\nAllow: /\n"
    assert first.blocks and first.blocks[0]["robots"] == "live" and first.blocks[0]["robots_age_s"] == 0.0


def test_robots_copies_reads_the_newest_live_record_from_fetches_and_probes(lake):
    assert ingest.robots_copies(lake, "2026-11-23") == {}  # nothing stored yet: no copy, as before
    t1, t2 = datetime(2026, 11, 9, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC)

    def rec(at, status, body, source="live"):
        return {
            "host": "store.com",
            "source": source,
            "fetched_at": at,
            "age_s": 0.0,
            "status": status,
            "body": body,
        }

    lake.write_partition("fetches", "2026-11-09", "f", [
        _fetch("store.com", "2026-11-09", "ok", t1) | {"robots": [rec(t1, 200, "User-agent: *\nAllow: /\n")]}])  # fmt: skip
    lake.write_partition("probes", "2026-11-16", "p", [
        {"run_date": t2.date(), "batch_id": "p", "probed_at": t2, "roaster_id": "store.com", "status": "ok",
         "http_status": 200, "final_host": "store.com", "platform": "other",
         "robots": [rec(t2, 403, None), rec(t2 + timedelta(days=1), 200, "x", source="cached")]}])  # fmt: skip
    copies = ingest.robots_copies(lake, "2026-11-23")
    assert copies["store.com"]["status"] == 403  # the newest answer, the stricter one: never a cached record
    assert copies["store.com"]["fetched_at"] == t2


def test_the_fetches_and_probes_schemas_carry_the_new_columns():
    names = set(SCHEMAS["fetches"].names)
    assert {"transport_errors", "robots"} <= names
    assert "robots" in SCHEMAS["probes"].names


# --- review of #178 (2026-11-18): each finding reproduced ------------------------------------------------------------


def test_a_2xx_copy_without_its_body_is_never_used(lake):
    """Finding 1, the reviewer's repro: the only stored record for store.com is a live 200 without a body (written by a
    fetch that only went by the file, its reader's row never stored). Before, it read as no rules and
    GET /private was sent. Neither the query nor the client may treat it as a copy."""
    t = datetime(2026, 11, 16, 9, tzinfo=UTC)
    rec = {"host": "store.com", "source": "live", "fetched_at": t, "age_s": 0.0, "status": 200, "body": None}
    lake.write_partition(
        "fetches", "2026-11-16", "f", [_fetch("store.com", "2026-11-16", "ok", t) | {"robots": [rec]}]
    )
    assert ingest.robots_copies(lake, "2026-11-23") == {}
    handler, seen = _down_robots()
    copies = {"store.com": {"fetched_at": NOW - timedelta(days=7), "status": 200, "body": None}}
    with _client(handler, copies) as c, pytest.raises(RobotsUnavailable):
        c.get("https://store.com/private")
    assert set(seen) == {"/robots.txt"}


def _big_robots(cut_inside: str) -> str:
    """A robots.txt whose 500 KiB mark falls inside `cut_inside`, the last line of the file."""
    head = "User-agent: *\nDisallow: /private\n"
    filler_line = "# " + "x" * 98 + "\n"
    budget = http.ROBOTS_BODY_MAX_BYTES - len(head) - 8  # the mark lands 8 bytes into the last line
    filler = filler_line * (budget // len(filler_line))
    filler += "#" * (budget - len(filler) - 1) + "\n"
    return head + filler + cut_inside + "\n"


def test_live_rules_come_from_the_whole_file_and_only_the_stored_copy_is_cut():
    """Finding 2, the reviewer's repro: `Disallow: /private` then `Allow: /private/public-only`, with the 500 KiB
    mark inside the Allow line. Cut there, the line read `Allow: /private/` and /private/secret was allowed."""
    text = _big_robots("Allow: /private/public-only")
    assert len(text.encode()) > http.ROBOTS_BODY_MAX_BYTES

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=text)
        return httpx.Response(200, text="ok")

    with _client(handler) as c, c.track() as stats:
        with pytest.raises(Blocked):
            c.get("https://store.com/private/secret")
        assert c.get("https://store.com/private/public-only").status_code == 200  # the whole file's rules
    body = stats.robots[0]["body"]
    assert body.endswith("\n") and len(body.encode()) <= http.ROBOTS_BODY_MAX_BYTES
    assert "Allow: /private/" not in body  # the half line is never stored
    # the cut copy, used when the file is unreachable, is followed exactly: /private stays disallowed
    down, _ = _down_robots()
    copy = {"store.com": {"fetched_at": NOW - timedelta(days=1), "status": 200, "body": body}}
    with _client(down, copy) as c, pytest.raises(Blocked):
        c.get("https://store.com/private/secret")


def test_a_file_with_no_line_break_inside_the_limit_stores_no_copy():
    assert http.stored_body("x" * (http.ROBOTS_BODY_MAX_BYTES + 10)) is None
    assert http.stored_body("") == ""  # an empty 2xx (allow everything) is a real answer, kept


def test_at_the_same_second_a_403_beats_a_200(lake):
    """Finding 3: two jobs read the same host in the same second; the stricter answer is the copy."""
    t = datetime(2026, 11, 16, 9, tzinfo=UTC)

    def rec(status, body):
        return {
            "host": "store.com",
            "source": "live",
            "fetched_at": t,
            "age_s": 0.0,
            "status": status,
            "body": body,
        }

    lake.write_partition("fetches", "2026-11-16", "f", [
        _fetch("store.com", "2026-11-16", "ok", t) | {"robots": [rec(200, "User-agent: *\nAllow: /\n")]},
        _fetch("store.com", "2026-11-16", "error", t) | {"robots": [rec(403, None)]},
    ])  # fmt: skip
    assert ingest.robots_copies(lake, "2026-11-23")["store.com"]["status"] == 403


def test_copies_are_read_only_from_the_last_31_days(lake):
    """Finding 7: a bounded read. A run older than the copy's age limit is never scanned."""
    old, new = datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 11, 9, tzinfo=UTC)

    def rec(at, host):
        return {"host": host, "source": "live", "fetched_at": at, "age_s": 0.0, "status": 404, "body": None}

    lake.write_partition(
        "fetches",
        "2026-10-12",
        "a",
        [_fetch("old.com", "2026-10-12", "ok", old) | {"robots": [rec(old, "old.com")]}],
    )
    lake.write_partition(
        "fetches",
        "2026-11-09",
        "b",
        [_fetch("new.com", "2026-11-09", "ok", new) | {"robots": [rec(new, "new.com")]}],
    )
    assert set(ingest.robots_copies(lake, "2026-11-23")) == {"new.com"}


def test_the_backoff_counts_crawl_failures_after_an_ok_probe(lake, tmp_path):
    """Finding 6: a store whose home page answers the probe but whose catalog keeps failing or refusing us (kaldi.com's
    pattern) was due again a week after each ok probe; each failed crawl run since that probe now doubles the wait."""
    seed = _seed(tmp_path, [MEANBEAN | {"platform": "woocommerce", "last_probed": "2026-11-02"}])
    for day in ("2026-11-09", "2026-11-16", "2026-11-23"):
        at = datetime.fromisoformat(day).replace(hour=9, tzinfo=UTC)
        lake.write_partition(
            "fetches", day, f"b{day}", [_fetch("meanbeanroasters.com", day, "error", at, error="HTTP 429")]
        )
    (s,) = registry._current(lake, seed)
    assert (s.status, s.failed_probes, s.failed_runs) == ("unreachable", 0, 3)
    assert cli.probe_todo([s], "2026-11-23", 28) == []  # 28 days, not 7
    assert cli.probe_todo([s], "2026-11-30", 28) == ["meanbeanroasters.com"]
    st = _state("x.com", "unreachable", "2026-11-09")
    st.failed_runs = 1
    assert cli.probe_todo([st], "2026-11-16", 28) == [] and cli.probe_todo([st], "2026-11-23", 28) == [
        "x.com"
    ]


def test_a_block_record_finds_its_robots_copy_whatever_the_hosts_spelling():
    """Nit 10: robots_used was keyed as the URL spelled the host, the response's host is normalized."""

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<html>")

    with _client(handler) as c, c.track() as stats:
        c.get("https://Store.COM:443/a")
    assert stats.blocks and stats.blocks[0]["robots"] == "live"
