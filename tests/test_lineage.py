"""Operational metadata kept in the lake and build records (never served): fetch diagnostics, run manifests,
payload hashes, and how older files without these columns still read."""

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
        ingest.run(lake, [ONYX, SLOW], "2026-10-26", workers=2, statuses={ONYX.roaster_id: "active"})
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
    from coffeedata.registry import RoasterState

    monkeypatch.setattr(registry, "current", lambda lake: [RoasterState(ONYX, "pending", None)])
    got = {}
    monkeypatch.setattr(
        ingest, "run", lambda lk, r, d, w, statuses, mode: got.update(s=statuses, m=mode) or {}
    )
    ns = argparse.Namespace(run_date="2026-10-26", only=None, sample=5, retry_failed=False, workers=1)
    cli.cmd_ingest(ns)
    assert got == {"s": {"onyxcoffeelab.com": "pending"}, "m": "sample"}


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
    # WooCommerce prices sizes from separate requests, so those are part of what was fetched.
    w = {"id": 5, "name": "Kenya", "variations": [{"id": 6, "attributes": []}]}
    assert woocommerce.parse_product(w, r, {"6": 20.0}).payload_hash != woocommerce.parse_product(
        w, r, {"6": 22.0}).payload_hash  # fmt: skip


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
