"""Operational metadata kept in the lake and build records (never served): fetch diagnostics, run manifests,
payload hashes, and how older files without these columns still read."""

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import respx

from coffeedata import ingest, lineage
from coffeedata.adapters import shopify, woocommerce
from coffeedata.http import PoliteClient
from coffeedata.lake import GLOBS, SCHEMA_FILES, SCHEMAS, Lake
from coffeedata.models import Roaster, payload_hash
from coffeedata.registry import RoasterState
from coffeedata.text import scrub_surrogates

from .conftest import load
from .lakehelp import old_file

ONYX = Roaster(roaster_id="onyxcoffeelab.com", name="Onyx", host="onyxcoffeelab.com", platform="shopify")
SLOW = Roaster(roaster_id="slow.com", name="Slow", host="slow.com", platform="shopify")


def _rows(lake, table, where=""):
    cur = lake.connect().execute(f"select * from {lake.scan(table)} where roaster_id is not null {where}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_the_client_counts_each_threads_requests_retries_and_429s():
    with respx.mock() as m, PoliteClient(min_interval=0) as client:
        m.get("https://a.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://a.com/p").mock(
            side_effect=[httpx.Response(429, headers={"retry-after": "0"}), httpx.Response(200, json={})]
        )
        m.get("https://a.com/gone").mock(return_value=httpx.Response(404))
        with client.track() as stats:
            client.get("https://a.com/p")
            client.get("https://a.com/gone")
        client.get("https://a.com/gone")  # outside the block: not counted
    assert (stats.requests, stats.pages, stats.retries, stats.rate_limited) == (4, 1, 1, 1)
    assert dict(stats.statuses) == {404: 2, 429: 1, 200: 1}  # robots.txt's 404 is a request, not a page


def test_fetches_record_diagnostics_and_the_run_manifest_comes_first(lake, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://onyxcoffeelab.com/products.json").mock(
            return_value=httpx.Response(200, json=load("shopify_onyxcoffeelab.json"))
        )
        m.get("https://slow.com/products.json").mock(
            side_effect=[httpx.Response(429, headers={"retry-after": "0"})] * 3
        )
        ingest.run(lake, [RoasterState(ONYX, "active", None), SLOW], "2026-10-26", workers=2, mode="full")
    f = {r["roaster_id"]: r for r in _rows(lake, "fetches")}
    onyx, slow = f["onyxcoffeelab.com"], f["slow.com"]
    # Page 2 repeats page 1, which ends the catalog: robots.txt plus two pages.
    assert (onyx["requests"], onyx["pages"], onyx["retries"], onyx["rate_limited"]) == (3, 2, 0, 0)
    assert onyx["http_statuses"] == [{"status": 200, "responses": 2}, {"status": 404, "responses": 1}]
    assert (slow["status"], slow["requests"], slow["pages"], slow["retries"], slow["rate_limited"]) == (
        "error", 4, 0, 2, 3)  # fmt: skip
    assert all(r["duration_s"] >= 0 and r["code_version"] == "abc123" for r in f.values())

    m = sorted(_rows(lake, "run_manifests"), key=lambda r: r["roaster_id"])
    assert [(r["roaster_id"], r["status"], r["adapter"], r["mode"]) for r in m] == [
        ("onyxcoffeelab.com", "active", "shopify", "full"), ("slow.com", None, "shopify", "full")]  # fmt: skip
    assert m[0]["run_date"] == date(2026, 10, 26) and m[0]["code_version"] == "abc123"
    assert m[0]["started_at"] <= min(r["fetched_at"] for r in f.values())

    snaps = _rows(lake, "snapshots")
    assert snaps and all(re.fullmatch(r"[0-9a-f]{32}", s["payload_hash"]) for s in snaps)
    assert len({s["payload_hash"] for s in snaps}) == len(snaps)


def test_the_cli_records_each_roasters_status_and_the_mode(lake, monkeypatch):
    import argparse

    from coffeedata import cli, registry

    monkeypatch.setattr(registry, "current", lambda lake: [RoasterState(ONYX, "pending", None)])
    got = {}
    monkeypatch.setattr(
        ingest, "run", lambda lk, r, d, workers, mode, shopify_rps: got.update(r=r, m=mode) or {}
    )
    for flags, mode in (({"sample": 5}, "sample"), ({"only": ONYX.roaster_id}, "only"),
                        ({"retry_failed": True}, "retry_failed"), ({}, "full")):  # fmt: skip
        ns = argparse.Namespace(run_date="2026-10-26", only=None, sample=None, retry_failed=False, workers=1,
                                shopify_rps=2.0)  # fmt: skip
        cli.cmd_ingest(argparse.Namespace(**vars(ns) | flags))
        assert got["m"] == mode
    # The states themselves go to ingest.run, so each roaster's status can't come apart from the roaster.
    assert ingest.select_states(Lake(), "2026-10-26", sample=5) == (
        [RoasterState(ONYX, "pending", None)],
        "sample",
    )


def test_the_manifest_is_published_before_the_first_request(lake, monkeypatch):
    # On a remote lake, staged files reach the dataset only when published: a job killed before its first chunk
    # must still leave the manifest behind.
    events = []
    staged = lambda t: any(lake.root.glob(f"{t}/run_date=2026-10-26/*.parquet"))  # noqa: E731
    monkeypatch.setattr(lake, "publish", lambda *a, **k: events.append(
        ("publish", staged("run_manifests"), staged("fetches"))))  # fmt: skip
    monkeypatch.setattr(ingest, "ingest_one", lambda c, r: events.append(("fetch", r.roaster_id)) or ingest.Result(
        r, ingest.datetime.now(ingest.UTC).replace(microsecond=0), error="boom"))  # fmt: skip
    ingest.run(lake, [ONYX], "2026-10-26", workers=1, mode="full")
    assert events == [("publish", True, False), ("fetch", ONYX.roaster_id), ("publish", True, True)]
    # Without a mode (a caller other than the CLI), the manifest says so instead of claiming a full run.
    ingest.run(lake, [SLOW], "2026-11-02", workers=1)
    assert [r["mode"] for r in _rows(lake, "run_manifests", "and roaster_id = 'slow.com'")] == [None]


def test_a_lone_surrogate_from_a_store_is_ingested_not_fatal(lake):
    # json accepts a truncated emoji escape ("\\ud83d"), which can't be encoded as UTF-8: unscrubbed, it failed the
    # chunk's lake write and stopped the whole ingest. It is replaced with U+FFFD where the JSON is decoded.
    body = (b'{"products": [{"id": 1, "title": "Kenya \\ud83d", "handle": "kenya", "tags": ["\\udc00x"],'
            b' "body_html": "<p>Bright \\ud83d</p>", "product_type": "Coffee",'
            b' "variants": [{"id": 9, "title": "12 oz", "price": "20.00", "grams": 340, "available": true}]}]}')  # fmt: skip
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://onyxcoffeelab.com/products.json").mock(
            side_effect=[httpx.Response(200, content=body), httpx.Response(200, json={"products": []})]
        )
        out = ingest.run(lake, [ONYX], "2026-10-26", workers=1, mode="full")
    assert out["failures"] == [] and out["products"] == 1
    [fetch] = _rows(lake, "fetches")
    [snap] = _rows(lake, "snapshots")
    assert fetch["status"] == "ok" and snap["title"] == "Kenya \ufffd" and snap["tags"] == ["\ufffdx"]
    assert re.fullmatch(r"[0-9a-f]{32}", snap["payload_hash"])
    # Ordinary non-ASCII text is left as it is, and hashes exactly as plain UTF-8 did before.
    q = {"title": "Café Ñandú 咖啡 ☕", "tags": ["naïve"]}
    assert scrub_surrogates(q) == q
    raw = json.dumps(q, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert payload_hash(q) == hashlib.sha256(raw.encode()).hexdigest()[:32]


def test_git_sha_is_the_packages_checkout_not_the_cwds(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.delenv("GITHUB_SHA", raising=False)
    here = Path(lineage.__file__).resolve().parent
    mine = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                    "--allow-empty", "-m", "other repo"], check=True)  # fmt: skip
    monkeypatch.chdir(tmp_path)  # run from inside another repo
    assert lineage.git_sha() == (mine.stdout.strip() or None)


def test_code_version_falls_back_to_the_package_version(monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(lineage, "git_sha", lambda: None)
    assert lineage.code_version() == f"coffeedata {lineage.package_version('coffeedata')}"


def test_ci_run_and_actor_come_from_github_actions(monkeypatch):
    for k, v in {"GITHUB_RUN_ID": "42", "GITHUB_SERVER_URL": "https://github.com",
                 "GITHUB_REPOSITORY": "me/repo", "GITHUB_ACTOR": "octocat"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    assert lineage.ci_run() == ("42", "https://github.com/me/repo/actions/runs/42")
    assert lineage.actor() == "octocat"
    monkeypatch.delenv("GITHUB_RUN_ID")
    monkeypatch.delenv("GITHUB_ACTOR")
    assert lineage.ci_run() == (None, None) and lineage.actor()  # the local user
    versions = json.loads(lineage.package_versions())
    assert {"python", "duckdb", "dbt-core", "pyarrow"} <= versions.keys()


def test_payload_hash_sees_any_change_but_not_key_order():
    r = Roaster(roaster_id="x.com", name="X", host="x.com", platform="shopify")
    p = {"id": 1, "title": "Kenya", "variants": [{"id": 9, "price": "20.00"}], "updated_at": "2026-10-01"}
    same = dict(reversed(list(p.items())))
    assert shopify.parse_product(p, r).payload_hash == shopify.parse_product(same, r).payload_hash
    assert payload_hash(p) == payload_hash(same)
    # A change nothing here parses (updated_at) is still a change in what the store sent.
    assert shopify.parse_product(p | {"updated_at": "2026-10-08"}, r).payload_hash != payload_hash(p)
    # WooCommerce prices sizes from separate requests, which depend on how the fetch went (a failed lookup, the
    # lookup budget running out): the hash is of the product's own payload, so a repeated fetch keeps it.
    w = {"id": 5, "name": "Kenya", "variations": [{"id": 6, "attributes": []}]}
    hashes = {
        woocommerce.parse_product(w, r, prices).payload_hash for prices in ({"6": 20.0}, {"6": None}, {})
    }
    assert hashes == {payload_hash(w)}
    assert woocommerce.parse_product(w | {"name": "Kenya AA"}, r, {}).payload_hash != payload_hash(w)


def test_new_tables_have_schema_files_globs_and_dbt_sources():
    macro = Path("dbt/macros/lake.sql").read_text()
    for t in ("run_manifests", "serving_log"):
        assert t in GLOBS and t in SCHEMA_FILES
        assert f"'{t}': '{GLOBS[t]}'" in macro  # dbt reads the same files as Lake.scan


def test_older_files_without_the_new_columns_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    root = tmp_path / "lake"
    new = {
        "fetches": ["pages", "requests", "http_statuses", "retries", "rate_limited", "duration_s",
                    "code_version"],
        "snapshots": ["payload_hash"],
        "builds": ["ci_run_id", "ci_run_url", "dbt_version", "package_versions", "duration_s"],
    }  # fmt: skip
    for (
        table,
        cols,
    ) in new.items():  # the lake as older code left it: stale schema files, files without columns
        old = pa.schema([f for f in SCHEMAS[table] if f.name not in cols])
        (root / SCHEMA_FILES[table]).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(old.empty_table(), root / SCHEMA_FILES[table])
    old_file(root, "fetches", "fetches/run_date=2026-10-19/batch=old.parquet", new["fetches"],
             {"run_date": date(2026, 10, 19), "roaster_id": "a.com", "status": "ok", "products": 1})  # fmt: skip
    lake = Lake()  # refreshes the stale schema files in place
    for table in new:
        assert pq.read_schema(root / SCHEMA_FILES[table]).names == SCHEMAS[table].names
    lake.write_partition("fetches", "2026-10-26", "new", [
        {"run_date": date(2026, 10, 26), "roaster_id": "a.com", "status": "ok", "requests": 3,
         "http_statuses": [{"status": 200, "responses": 3}]}])  # fmt: skip
    got = sorted((r["run_date"], r["requests"], r["http_statuses"]) for r in _rows(lake, "fetches"))
    assert got == [
        (date(2026, 10, 19), None, None),
        (date(2026, 10, 26), 3, [{"status": 200, "responses": 3}]),
    ]
