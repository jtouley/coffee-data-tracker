"""The admin (admin/): its queries over a small synthetic lake, the Catalog's per-build cache and filters, passkey
sign-in end to end with a software authenticator (real WebAuthn verification), the state store, the settings that
switch it off, every route refusing anonymous requests, and the lake never being written."""

import base64
import hashlib
import json
import os
from datetime import UTC, date, datetime

import cbor2
import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from admin import auth as auth_mod
from admin import queries
from admin.auth import Auth, AuthError, AuthUnavailable, normalize_code
from admin.catalog import Catalog, Filters
from admin.config import Settings, from_env
from admin.main import PUBLIC, Reader, create_app
from admin.store import FileStore, GcsStore, StoreError
from coffeedata import control, quality
from coffeedata.lake import SCHEMAS, Lake, ReadOnlyLake

W1, W2, W3 = "2026-10-19", "2026-10-26", "2026-11-02"
B1, B2 = "20261020T030000-aaaaaa", "20261027T030000-bbbbbb"
ORIGIN = "http://localhost:8001"
SECRET = "s" * 40
TOKEN = "enroll-token-0123456789-0123456789-abcdef"


def ts(d: str, h: int = 9) -> datetime:
    return datetime.fromisoformat(d).replace(hour=h, tzinfo=UTC)


def _ops(lake: Lake, run_date: str, build: str, table: str, rows: list[dict]) -> None:
    lake.write_ops(
        table, run_date, build, pa.Table.from_pylist([{"build_id": build} | r for r in rows], SCHEMAS[table])
    )


def _release(lake: Lake, run_date: str, build: str, table: str, rows: list[dict]) -> None:
    path = lake.root / "releases" / f"run_date={run_date}" / f"build={build}" / f"{table}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _fetch(rid, status, http=None, sg=None, cf=None, partial=False, at=None, run_date=W2):
    blocks = (
        [{"at": at or ts(run_date), "status": http, "host": rid, "sg_captcha": sg, "cf_mitigated": cf}]
        if http and http != 200
        else None
    )
    return {"run_date": date.fromisoformat(run_date), "batch_id": "b", "fetched_at": at or ts(run_date),
            "roaster_id": rid, "adapter": "shopify", "status": status, "error": None if status == "ok" else f"HTTP {http}",
            "products": 3, "http_statuses": [{"status": http, "responses": 2}] if http else [], "blocks": blocks,
            "partial": partial}  # fmt: skip


def build_lake(root) -> Lake:
    """Two weeks, two builds (the first passing and served, the second failing), ops tables for the second only, and
    a third week with fetches only."""
    lake = Lake(str(root))
    lake.write(
        "fetches", f"fetches/run_date={W1}/batch=a.parquet", [_fetch("good.example", "ok", 200, run_date=W1)]
    )
    lake.write("fetches", f"fetches/run_date={W2}/batch=a.parquet", [
        _fetch("good.example", "ok", 200),
        _fetch("sg.example", "error", 202, sg="1"),
        _fetch("sg.example", "error", 202, sg="1", at=ts(W2, 15)),
        _fetch("cf.example", "error", 403, cf="challenge"),
        _fetch("rl.example", "error", 429),
        _fetch("part.example", "ok", 200, partial=True),
        _fetch("q.example", "crashed"),
    ])  # fmt: skip
    lake.write(
        "fetches", f"fetches/run_date={W3}/batch=a.parquet", [_fetch("good.example", "ok", 200, run_date=W3)]
    )
    for week, conn, h, status in ((W2, "shopify", 9, "done"), (W2, "woocommerce", 9, "done"), (W2, "shopify", 15, "failed"),
                                  (W2, None, 20, "done")):  # fmt: skip
        control.write(lake, {"run_date": week, "job": "crawl" if conn else "merge", "connector": conn,
                             "attempt_id": f"a{h}{conn}", "started_at": ts(week, h), "ended_at": ts(week, h + 1),
                             "status": status, "todo": 10, "ok": 8, "errors": 2, "http_429": 1,
                             "detail": {"execution": f"exec-{h}"}})  # fmt: skip
    control.write(lake, {"run_date": W2, "job": "crawl", "connector": "generic", "attempt_id": "arm1",
                         "started_at": ts(W2, 10), "status": "done", "detail": {"arm": "b"}})  # fmt: skip
    builds = []
    for build, week, passed, built in ((B1, W1, True, ts(W1, 20)), (B2, W2, False, ts(W2, 20))):
        base = {"build_id": build, "built_at": built, "run_date": date.fromisoformat(week), "git_sha": "abc123",
                "ci_run_url": "https://github.com/x/y/actions/runs/1"}  # fmt: skip
        builds += [base | {"kind": "build", "unique_id": "build", "status": "pass" if passed else "fail"}]
        builds += [base | {"kind": "node", "unique_id": "test.a", "resource_type": "test", "status": "pass"},
                   base | {"kind": "node", "unique_id": "test.z_fails", "resource_type": "test",
                           "status": "pass" if passed else "fail", "failures": 0 if passed else 3},
                   base | {"kind": "node", "unique_id": "test.warns", "resource_type": "test", "status": "warn"},
                   base | {"kind": "node", "unique_id": "model.x", "resource_type": "model", "status": "success"},
                   base | {"kind": "metric", "unique_id": "listed_coffees", "value": 10.0 if passed else 12.0},
                   base | {"kind": "metric", "unique_id": "only_" + build[-1], "value": 1.0}]  # fmt: skip
        lake.write("builds", f"builds/run_date={week}/build={build}.parquet", builds[-7:])
    lake.write("serving_log", "serving_log/serve_id=1.parquet", [{"served_at": ts(W1, 21), "action": "serve",
               "build_id": B1, "run_date": date.fromisoformat(W1), "reason": "weekly", "actor": "ci",
               "ci_run_url": "javascript:alert(1)"}])  # fmt: skip
    (lake.root / "marts").mkdir(exist_ok=True)
    pq.write_table(pa.Table.from_pylist([{"build_id": B1, "run_date": date.fromisoformat(W1), "served_at": ts(W1, 21),
                                          "reason": "weekly"}]), lake.root / "marts" / "release.parquet")  # fmt: skip
    dim = [{"coffee_id": "c1", "roaster_id": "good.example", "title": "Guji", "origin_country": "Ethiopia",
            "process": "natural", "in_stock": True, "min_price_usd": 18.0, "price_per_100g": 5.3, "is_listed": True,
            "url": "https://good.example/p/1"},
           {"coffee_id": "c2", "roaster_id": "good.example", "title": "Huila", "origin_country": "Colombia",
            "process": "washed", "in_stock": False, "min_price_usd": 22.0, "price_per_100g": 6.5, "is_listed": True,
            "url": "javascript:alert(1)"}]  # fmt: skip
    _release(lake, W1, B1, "dim_coffee", dim)
    _release(lake, W2, B2, "dim_coffee", dim)
    _ops(lake, W2, B2, "product_decisions", [
        {"coffee_id": "c1", "roaster_id": "good.example", "product_id": "1", "title": "Guji 100%_off", "is_listed": True,
         "is_coffee": True, "classified_by": "verdict", "reason": "verdict:coffee", "url": "https://good.example/p/1"},
        {"coffee_id": "c2", "roaster_id": "good.example", "product_id": "2", "title": "Huila", "is_listed": False,
         "is_coffee": True, "classified_by": "rules", "reason": "rules:coffee"},
        {"coffee_id": "m1", "roaster_id": "other.example", "product_id": "3", "title": "Mug", "is_listed": True,
         "is_coffee": False, "classified_by": "rules", "reason": "excluded:merch"},
    ])  # fmt: skip
    _ops(lake, W2, B2, "release_changes", [
        {"table_name": "public_coffee", "item": "c2", "change": "changed", "reason": "delisted"},
        {"table_name": "public_coffee", "item": "c9", "change": "removed", "reason": "unexplained"},
        {"table_name": "public_price_event", "item": "e1", "change": "added", "reason": "new_run"},
    ])  # fmt: skip
    _ops(lake, W2, B2, "run_completeness", [
        {"run_date": date.fromisoformat(W2), "roaster_id": r, "attempted": a, "ok": ok, "quarantined": q,
         "partial": p, "challenge": c, "unavailable": False}
        for r, a, ok, q, p, c in (("good.example", True, True, False, False, None),
                                  ("sg.example", True, False, False, False, "siteground"),
                                  ("cf.example", True, False, False, False, "cloudflare"),
                                  ("rl.example", True, False, False, False, None),
                                  ("part.example", True, True, False, True, None),
                                  ("q.example", False, False, True, False, None),
                                  ("never.example", False, False, False, False, None))
    ])  # fmt: skip
    return Lake(str(root), read_only=True)


@pytest.fixture
def lake(tmp_path):
    return build_lake(tmp_path / "lake")


def run(lake, fn, *args):
    return fn(lake, lake.connect(), *args)


def tree(root) -> list:
    return sorted(
        (p.relative_to(root).as_posix(), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")
    )


# --- the read-only lake --------------------------------------------------------------------------------------


def test_a_read_only_lake_creates_nothing_and_refuses_every_write(tmp_path):
    root = tmp_path / "nothing-here"
    lake = Lake(str(root), read_only=True)
    assert not root.exists()  # no schema folders, unlike Lake() (ensure_schemas)
    tbl = pa.Table.from_pylist([], SCHEMAS["release_changes"])
    for call in (
        lambda: lake.write("fetches", "fetches/x.parquet", [{"roaster_id": "a"}]),
        lambda: lake.write_partition("fetches", W1, "b", [{"roaster_id": "a"}]),
        lambda: lake.write_mart("m", None, "select 1"),
        lambda: lake.write_release("t", W1, B1, None, "select 1"),
        lambda: lake.write_ops("release_changes", W1, B1, tbl),
        lambda: lake.publish("x"),
        lake.ensure_schemas,
    ):
        with pytest.raises(ReadOnlyLake):
            call()
    assert not root.exists()


def test_a_remote_read_only_lake_reads_the_remote_lake_only(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    writer = Lake("hf://datasets/o/lake", staging=staging)  # leaves schema files and a staged release behind
    (staging / "releases/run_date=x/build=B/ops").mkdir(parents=True)
    (staging / "releases/run_date=x/build=B/dim_coffee.parquet").write_bytes(b"")
    (staging / "releases/run_date=x/build=B/ops/release_changes.parquet").write_bytes(b"")
    reader = Lake("hf://datasets/o/lake", staging=staging, read_only=True)
    assert reader.sources("fetches") == ["hf://datasets/o/lake/fetches/*/*.parquet"]
    assert writer.sources("fetches")[0].startswith(str(staging))
    monkeypatch.setattr(quality, "_remote_releases", lambda url: ("releases/run_date=x/build=B/dim_coffee.parquet",
                                                                    "releases/run_date=x/build=B/ops/release_changes.parquet"))  # fmt: skip
    assert quality.release_files(reader, "B") == {
        "dim_coffee": "hf://datasets/o/lake/releases/run_date=x/build=B/dim_coffee.parquet"
    }
    assert quality.ops_files(reader, "B") == {
        "release_changes": "hf://datasets/o/lake/releases/run_date=x/build=B/ops/release_changes.parquet"
    }
    assert quality.release_files(writer, "B") == {
        "dim_coffee": str(staging / "releases/run_date=x/build=B/dim_coffee.parquet")
    }
    assert reader.mart_sources("release") == ["hf://datasets/o/lake/marts/release.parquet"]


def test_forget_listings_clears_both_caches(monkeypatch):
    from coffeedata import lake as lake_mod

    lake_mod._REMOTE_FOLDERS["hf://datasets/o/x"] = frozenset({"a"})
    quality.forget_listings()
    assert lake_mod._REMOTE_FOLDERS == {}


# --- queries ---------------------------------------------------------------------------------------------------


def test_weeks_and_builds(lake):
    assert run(lake, queries.weeks) == [W3, W2, W1]
    bs = run(lake, queries.builds)
    assert [(b["build_id"], b["passed"]) for b in bs] == [(B2, False), (B1, True)]
    assert run(lake, queries.builds, W1)[0]["build_id"] == B1
    assert run(lake, queries.week_build, W3) is None


def test_served_never_links_an_unsafe_url(lake):
    s = run(lake, queries.served)
    assert s["build_id"] == B1 and s["log"]["actor"] == "ci"
    assert s["log"]["ci_run_url"] is None  # javascript: is never a link
    assert s["folder"] == {"path": f"releases/run_date={W1}/build={B1}", "url": None}


def test_release_folder_links_the_dataset_on_a_remote_lake(tmp_path):
    remote = Lake("hf://datasets/jtouley/coffee-data", staging=tmp_path, read_only=True)
    assert queries.release_folder(remote, W1, B1)["url"] == (
        f"https://huggingface.co/datasets/jtouley/coffee-data/tree/main/releases/run_date={W1}/build={B1}"
    )


def test_week_lists_attempts_in_order_with_passes_stages_and_the_build_verdict(lake):
    w = run(lake, queries.week, W2)
    crawls = [(r["connector"], r["pass"], r["arm"]) for r in w["rows"] if r["job"] == "crawl"]
    assert crawls == [
        ("shopify", 1, False),
        ("woocommerce", 1, False),
        ("generic", None, True),
        ("shopify", 2, False),
    ]
    by = {c["connector"]: c for c in w["connectors"]}
    assert by["shopify"]["landed"] and by["shopify"]["passes"] == 2 and by["shopify"]["latest"] == "failed"
    assert not by["generic"]["landed"]  # an arm never counts as the week's crawl
    assert w["landed"] == 2
    assert w["stages"] == {"merge": "done", "enrich": "missing", "classify": "missing", "build": "failed"}
    assert w["rows"][0]["execution"] == "exec-9"
    assert run(lake, queries.week, W3)["rows"] == []


def test_failures_from_run_completeness(lake):
    f = run(lake, queries.failures, W2)
    assert f["counts_source"] == "run_completeness" and f["build_id"] == B2
    assert f["fetches"] == 7 and f["by_status"] == {"ok": 2, "error": 4, "crashed": 1}
    assert {h["http_status"]: (h["stores"], h["responses"]) for h in f["http"]} == {
        202: (1, 4),
        403: (1, 2),
        429: (1, 2),
    }
    c = f["counts"]
    assert (c["expected"], c["not_attempted"], c["failed"], c["siteground"], c["cloudflare"], c["partial"],
            c["quarantined"]) == (7, 2, 3, 1, 1, 1, 1)  # fmt: skip
    stores = {s["roaster_id"]: s for s in f["stores"]}
    assert set(stores) == {"sg.example", "cf.example", "rl.example", "part.example", "q.example"}
    assert stores["sg.example"]["attempts"] == 2 and stores["sg.example"]["http_statuses"] == [202]
    assert stores["sg.example"]["url"] == "https://sg.example/"
    assert stores["q.example"]["quarantined"] and stores["part.example"]["partial"]


def test_failures_without_ops_count_from_fetches_by_the_same_rule(lake):
    f = run(lake, queries.failures, W3)
    assert f["counts_source"] == "fetches" and f["stores"] == [] and f["fetches"] == 1


def test_failures_read_fetches_written_before_the_challenge_fields(tmp_path):
    Lake(str(tmp_path / "old"))  # its schema files, removed below for fetches
    old = pa.schema([f for f in SCHEMAS["fetches"] if f.name != "blocks"])
    for p in (tmp_path / "old").rglob("fetches/*/*.parquet"):
        p.unlink()  # no schema file: only files from before blocks existed
    path = tmp_path / "old" / f"fetches/run_date={W2}/batch=a.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{k: v for k, v in _fetch("x.example", "error", 429).items() if k != "blocks"}], old
        ),
        path,
    )
    ro = Lake(str(tmp_path / "old"), read_only=True)
    with pytest.raises(Exception):  # noqa: B017 - DuckDB can't bind a column no file has
        ro.connect().execute(f"select blocks from {ro.scan('fetches')}").fetchall()
    f = run(ro, queries.failures, W2)
    assert f["counts"]["siteground"] == 0 and f["stores"][0]["challenge"] is None


def test_the_passed_rule_is_quality_builds_own(lake):
    assert {b["build_id"]: b["passed"] for b in run(lake, queries.builds)} == {
        b: ok for b, _, ok in quality.builds(lake)
    }
    assert queries.previous_passing(run(lake, queries.builds), B2) == B1


def test_failure_trend(lake):
    t = run(lake, queries.failure_trend)
    assert [(x["week"], x["stores"], x["failed"], x["stores_202"], x["stores_429"]) for x in t] == [
        (W3, 1, 0, 0, 0),
        (W2, 6, 4, 1, 1),
        (W1, 1, 0, 0, 0),
    ]


def test_gate_puts_failures_first_and_compares_with_the_previous_passing_build(lake):
    g = run(lake, queries.gate, B2)
    assert [t["unique_id"] for t in g["tests"]] == ["test.z_fails", "test.warns", "test.a"]
    assert g["previous"] == B1 and g["by_status"] == {"fail": 1, "warn": 1, "pass": 1}
    m = {x["metric"]: x for x in g["metrics"]}
    assert (m["listed_coffees"]["value"], m["listed_coffees"]["previous"], m["listed_coffees"]["change"]) == (
        12.0,
        10.0,
        2.0,
    )
    assert m["only_b"]["previous"] is None and m["only_a"]["value"] is None
    assert run(lake, queries.gate, B1)["previous"] is None


def test_changes_counts_unexplained_and_filters(lake):
    c = run(lake, queries.changes, B2)
    assert c["recorded"] and c["total"] == 3 and c["unexplained"] == 1
    assert c["rows"][0]["reason"] == "unexplained"  # unexplained first
    f = run(lake, queries.changes, B2, "public_coffee", "changed", "delisted")
    assert f["matching"] == 1 and f["rows"][0]["item"] == "c2"
    assert run(lake, queries.changes, B2, None, None, None, 1, 1)["rows"][0]["reason"] != "unexplained"
    assert run(lake, queries.changes, B1) == {
        "recorded": False,
        "build_id": B1,
        "source": ["release_changes"],
    }


def test_an_empty_lake_reads_as_no_rows(tmp_path):
    empty = Lake(str(tmp_path / "none"), read_only=True)
    assert (
        run(empty, queries.weeks) == []
        and run(empty, queries.builds) == []
        and run(empty, queries.served) is None
    )
    assert run(empty, queries.week, W1)["rows"] == [] and run(empty, queries.failure_trend) == []
    assert run(empty, queries.failures, W1)["fetches"] == 0
    assert not (tmp_path / "none").exists()


@pytest.mark.parametrize(
    ("url", "safe"),
    [("https://a.example/x", True), ("http://a.example/", False), ("javascript:alert(1)", False),
     ("https://a.example/\"onmouseover=x", False), (None, False)],
)  # fmt: skip
def test_only_https_links(url, safe):
    assert (queries.safe_url(url) is not None) is safe


def test_store_url_only_for_hostnames():
    assert queries.store_url("onyxcoffeelab.com") == "https://onyxcoffeelab.com/"
    assert queries.store_url("not a host") is None and queries.store_url("javascript:x") is None


# --- the Catalog ------------------------------------------------------------------------------------------------


def test_catalog_joins_decisions_and_dim_coffee_and_caches_once_per_build(lake, tmp_path):
    cache = tmp_path / "cache"
    cat = Catalog(lake, cache)
    r = cat.search(B2, Filters())
    assert r["total"] == 3 and r["meta"]["made_from"] == "product_decisions + dim_coffee"
    by = {x["coffee_id"]: x for x in r["rows"]}
    assert by["c1"]["origin_country"] == "Ethiopia" and by["c1"]["listed"] == "listed"
    assert (
        by["c2"]["listed"] == "removed"
        and by["m1"]["coffee"] == "not coffee"
        and by["m1"]["origin_country"] is None
    )
    assert by["c2"]["url"] is None  # never a javascript: link
    made = cat.path(B2).stat().st_ino
    cat.search(B2, Filters(q="guji"))
    assert cat.path(B2).stat().st_ino == made  # made once (a use only touches it)
    assert cat.path(B2).parent == cache and not cat.path(B2).is_relative_to(lake.root)


def test_catalog_facets_count_without_their_own_filter_and_search_escapes_wildcards(lake, tmp_path):
    cat = Catalog(lake, tmp_path / "cache")
    r = cat.search(B2, Filters(values={"coffee": "coffee"}))
    assert r["matching"] == 2
    assert dict(r["facets"]["coffee"]) == {"coffee": 2, "not coffee": 1}  # its own filter left out
    assert dict(r["facets"]["roaster"]) == {"good.example": 2}
    assert cat.search(B2, Filters(q="100%_off"))["matching"] == 1
    assert cat.search(B2, Filters(q="G%off"))["matching"] == 0  # a literal %, not a wildcard
    assert cat.search(B2, Filters(price_min=20))["matching"] == 1
    assert cat.search(B2, Filters(values={"origin": "(none)"}))["matching"] == 1
    assert cat.search(B2, Filters(values={"stock": "in stock", "listed": "listed"}))["matching"] == 1


def test_catalog_of_a_build_without_ops_tables_is_its_coffees(lake, tmp_path):
    r = Catalog(lake, tmp_path / "c").search(B1, Filters())
    assert r["total"] == 2 and "dim_coffee only" in r["meta"]["made_from"]


def test_catalog_pruning_keeps_the_builds_in_use(lake, tmp_path, monkeypatch):
    import time as _time

    cat = Catalog(lake, tmp_path / "cache")
    cat.search(B2, Filters())
    for i in range(5):  # five other builds' files, all older than B2's last use
        f = tmp_path / "cache" / f"catalog-old{i}.duckdb"
        f.write_bytes(b"")
        os.utime(f, (_time.time() - 100 - i, _time.time() - 100 - i))
    cat.search(B2, Filters())  # used again: touched
    cat._prune(keep=cat.path(B1))
    assert cat.path(B2).exists()
    assert len(list((tmp_path / "cache").glob("catalog-*.duckdb"))) == 4


def test_catalog_refuses_a_bad_build_id(lake, tmp_path):
    with pytest.raises(ValueError):
        Catalog(lake, tmp_path).path("../../etc")


def test_filters_from_a_query_string():
    f = Filters.from_query(
        {"q": " x ", "roaster": "a", "nope": "z", "price_min": "abc", "price_max": "30", "page": "-3"}
    )
    assert (f.q, f.values, f.price_min, f.price_max, f.page) == ("x", {"roaster": "a"}, None, 30.0, 1)
    assert [k for k, _ in f.chips()] == ["q", "roaster", "price_max"]


# --- sign-in -------------------------------------------------------------------------------------------------


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class SoftKey:
    """A passkey in software: ES256, "none" attestation, user present and verified."""

    def __init__(self, rp_id: str = "localhost", origin: str = ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred = os.urandom(16)
        self.rp_id, self.origin, self.count = rp_id, origin, 0

    def _client(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}
        ).encode()

    def create(self, options: str) -> dict:
        o = json.loads(options)
        n = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + (0).to_bytes(4, "big") + bytes(16)
                + len(self.cred).to_bytes(2, "big") + self.cred + cose)  # fmt: skip
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})
        return {"id": b64u(self.cred), "rawId": b64u(self.cred), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(self._client("webauthn.create", o["challenge"])),
                             "attestationObject": b64u(att)}}  # fmt: skip

    def get(self, options: str) -> dict:
        o = json.loads(options)
        self.count += 1
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        client = self._client("webauthn.get", o["challenge"])
        sig = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred), "rawId": b64u(self.cred), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(client), "authenticatorData": b64u(data), "signature": b64u(sig),
                             "userHandle": b64u(auth_mod.USER_ID)}}  # fmt: skip


def new_auth(tmp_path, store=None) -> Auth:
    return Auth(store or FileStore(tmp_path / "state"), SECRET, TOKEN, "localhost", ORIGIN)


def enroll(auth: Auth, key: SoftKey) -> tuple[str, list[str]]:
    begin = auth.enroll_begin(TOKEN, "1.1.1.1")
    return auth.enroll_finish(begin["ceremony"], key.create(begin["options"]))


def signin(auth: Auth, key: SoftKey) -> str:
    begin = auth.signin_begin("1.1.1.1")
    return auth.signin_finish(begin["ceremony"], key.get(begin["options"]), "1.1.1.1")


def test_enrollment_works_once_with_the_token(tmp_path):
    a = new_auth(tmp_path)
    assert a.enroll_open()
    with pytest.raises(AuthError) as e:
        a.enroll_begin("wrong-token-wrong-token-wrong-token-xx", "1.1.1.1")
    assert e.value.status == 403
    cookie, codes = enroll(a, SoftKey())
    assert len(codes) == 10 and len(set(codes)) == 10 and a.session(cookie) is not None
    assert not a.enroll_open()
    with pytest.raises(AuthError, match="closed"):
        a.enroll_begin(TOKEN, "1.1.1.1")
    spent = json.loads((tmp_path / "state" / "enroll" / "spent").read_text())
    assert TOKEN not in json.dumps(spent) and not set(codes) & set(
        json.dumps(spent).split('"')
    )  # hashes only


def test_two_enrollments_in_flight_register_only_one(tmp_path):
    a = new_auth(tmp_path)
    k1, k2 = SoftKey(), SoftKey()
    b1, b2 = a.enroll_begin(TOKEN, "1.1.1.1"), a.enroll_begin(TOKEN, "1.1.1.1")
    a.enroll_finish(b1["ceremony"], k1.create(b1["options"]))
    with pytest.raises(AuthError, match="closed"):
        a.enroll_finish(b2["ceremony"], k2.create(b2["options"]))
    assert list(a.credentials()) == [b64u(k1.cred)]


def test_a_short_or_missing_token_never_opens_enrollment(tmp_path):
    for token in (None, "", "short"):
        a = Auth(FileStore(tmp_path / f"s{token}"), SECRET, token, "localhost", ORIGIN)
        assert not a.enroll_open()
        with pytest.raises(AuthError):
            a.enroll_begin(token or "", "1.1.1.1")


def test_sign_in_with_each_passkey_and_the_counter_moves_on(tmp_path):
    a = new_auth(tmp_path)
    k1, k2 = SoftKey(), SoftKey()
    cookie, _ = enroll(a, k1)
    s = a.session(cookie)
    assert a.add_rule(s) == "fresh"
    begin = a.add_begin(s, None, "1.1.1.1")
    a.add_finish(s, begin["ceremony"], k2.create(begin["options"]))
    assert len(a.credentials()) == 2 and a.add_rule(s) == "code"
    assert a.session(signin(a, k1)) and a.session(signin(a, k2)) and a.session(signin(a, k1))
    assert a.credentials()[b64u(k1.cred)].sign_count == 2
    k1.count = 0  # a cloned authenticator replays an old counter
    with pytest.raises(AuthError):
        signin(a, k1)


def test_an_unknown_passkey_or_a_bad_signature_is_refused(tmp_path):
    a = new_auth(tmp_path)
    enroll(a, SoftKey())
    with pytest.raises(AuthError, match="isn’t registered|isn't registered"):
        signin(a, SoftKey())
    with pytest.raises(AuthError, match="expired"):
        a.signin_finish("not-a-ceremony", {}, "1.1.1.1")
    k = SoftKey()
    enroll(new_auth(tmp_path / "b"), k)
    begin = a.signin_begin("1.1.1.1")
    with pytest.raises(AuthError):
        a.signin_finish(begin["ceremony"], k.get(begin["options"]), "1.1.1.1")
    with pytest.raises(AuthError, match="expired"):  # a ceremony is used once, even when it failed
        a.signin_finish(begin["ceremony"], k.get(begin["options"]), "1.1.1.1")


def test_a_third_passkey_needs_a_recovery_code_which_is_then_used_up(tmp_path):
    a = new_auth(tmp_path)
    k1 = SoftKey()
    cookie, codes = enroll(a, k1)
    s = a.session(cookie)
    b = a.add_begin(s, None, "1.1.1.1")
    a.add_finish(s, b["ceremony"], SoftKey().create(b["options"]))
    with pytest.raises(AuthError, match="recovery code"):
        a.add_begin(s, None, "1.1.1.1")
    b = a.add_begin(s, codes[1].lower().replace("-", ""), "1.1.1.1")  # any case, with or without dashes
    a.add_finish(s, b["ceremony"], SoftKey().create(b["options"]))
    assert len(a.credentials()) == 3
    with pytest.raises(AuthError, match="used"):
        a.add_begin(s, codes[1], "1.1.1.1")


def test_a_stale_session_cannot_add_the_second_passkey(tmp_path, monkeypatch):
    a = new_auth(tmp_path)
    cookie, _ = enroll(a, SoftKey())
    s = a.session(cookie)
    monkeypatch.setattr(auth_mod, "_now", lambda: s.created_at + auth_mod.FRESH_S + 1)
    assert a.add_rule(s) == "stale"
    with pytest.raises(AuthError, match="Sign in again"):
        a.add_begin(s, None, "1.1.1.1")


def test_recovery_registers_a_passkey_signs_out_everywhere_and_works_once(tmp_path):
    a = new_auth(tmp_path)
    old, codes = enroll(a, SoftKey())
    b = a.recover_begin(codes[0], "1.1.1.1")
    new = a.recover_finish(b["ceremony"], SoftKey().create(b["options"]))
    assert a.session(new) is not None and a.session(old) is None
    assert len(a.credentials()) == 1  # the new passkey; the lost one is retired
    with pytest.raises(AuthError, match="used"):
        a.recover_begin(codes[0], "1.1.1.1")
    with pytest.raises(AuthError):
        a.recover_begin("0000-0000-0000-0000", "1.1.1.1")


def test_recovery_retires_every_earlier_passkey(tmp_path):
    a = new_auth(tmp_path)
    lost1, lost2, new = SoftKey(), SoftKey(), SoftKey()
    cookie, codes = enroll(a, lost1)
    s = a.session(cookie)
    b = a.add_begin(s, None, "1.1.1.1")
    a.add_finish(s, b["ceremony"], lost2.create(b["options"]))
    b = a.recover_begin(codes[0], "1.1.1.1")
    fresh = a.recover_finish(b["ceremony"], new.create(b["options"]))
    assert list(a.credentials()) == [b64u(new.cred)]
    for lost in (lost1, lost2):
        with pytest.raises(AuthError, match="isn't registered"):
            signin(a, lost)
    assert a.session(signin(a, new)) is not None
    assert a.add_rule(a.session(fresh)) == "fresh" and a.recovery_left() == 9  # the second passkey, again


def test_the_add_rule_is_checked_again_when_the_ceremony_finishes(tmp_path):
    a = new_auth(tmp_path)
    cookie, _ = enroll(a, SoftKey())
    s = a.session(cookie)
    b1, b2 = (
        a.add_begin(s, None, "1.1.1.1"),
        a.add_begin(s, None, "1.1.1.1"),
    )  # both while there's one passkey
    a.add_finish(s, b1["ceremony"], SoftKey().create(b1["options"]))
    with pytest.raises(AuthError, match="needs a recovery code"):
        a.add_finish(s, b2["ceremony"], SoftKey().create(b2["options"]))
    assert len(a.credentials()) == 2


def test_a_malformed_credential_is_refused_not_an_error(tmp_path):
    a = new_auth(tmp_path)
    k = SoftKey()
    enroll(a, k)
    begin = a.signin_begin("1.1.1.1")
    bad = k.get(begin["options"])
    del bad["response"]["signature"]
    with pytest.raises(AuthError):
        a.signin_finish(begin["ceremony"], bad, "1.1.1.1")
    s = a.session(a.start_session(b64u(k.cred)))
    b = a.add_begin(s, None, "1.1.1.1")
    with pytest.raises(AuthError):
        a.add_finish(s, b["ceremony"], {"id": "x", "response": {}})


def test_a_flood_of_ceremonies_evicts_the_oldest_instead_of_refusing(tmp_path):
    a = new_auth(tmp_path)
    for _ in range(1005):
        a._ceremony("signin")
    assert len(a.ceremonies) == 1000
    k = SoftKey()
    enroll(a, k)
    assert a.session(signin(a, k)) is not None


def test_recovery_before_enrollment_is_refused(tmp_path):
    with pytest.raises(AuthError, match="Nothing to recover"):
        new_auth(tmp_path).recover_begin("AAAA-AAAA-AAAA-AAAA", "1.1.1.1")


def test_sessions_last_twelve_hours_and_never_renew(tmp_path, monkeypatch):
    a = new_auth(tmp_path)
    cookie = a.start_session("c")
    s = a.session(cookie)
    assert s.expires_at - s.created_at == 12 * 3600
    monkeypatch.setattr(auth_mod, "_now", lambda: s.created_at + 12 * 3600 + 1)
    assert a.session(cookie) is None


def test_sign_out_and_sign_out_everywhere_revoke_server_side(tmp_path):
    a = new_auth(tmp_path)
    c1, c2 = a.start_session("c"), a.start_session("c")
    a.revoke(a.session(c1))
    assert a.session(c1) is None and a.session(c2) is not None
    other = new_auth(tmp_path)  # another instance, reading the same store, sees it too
    assert other.session(c1) is None
    a.revoke_all()
    assert a.session(c2) is None and new_auth(tmp_path).session(c2) is None


def test_a_cookie_from_another_secret_or_tampered_is_refused(tmp_path):
    a = new_auth(tmp_path)
    cookie = a.start_session("c")
    b = Auth(FileStore(tmp_path / "state"), "t" * 40, TOKEN, "localhost", ORIGIN)
    assert b.session(cookie) is None
    sid, sig = cookie.split(".")
    # A changed first character, never the one already there. Replacing the last one with A left the cookie as it
    # was whenever the signature already ended in A (1 run in 16: its last character's low bits are padding).
    other = "B" if sig.startswith("A") else "A"
    for bad in (f"{sid}.{other}{sig[1:]}", f"{sid}x.{sig}", sid, "", None, "a.b.c"):
        assert a.session(bad) is None


def test_a_session_secret_too_short_is_refused():
    with pytest.raises(ValueError):
        Auth(FileStore("/nonexistent"), "short", TOKEN, "localhost", ORIGIN)


def test_an_unreadable_store_fails_closed(tmp_path):
    class Broken:
        def create(self, *a):
            raise StoreError("down")

        get = list = create

    a = new_auth(tmp_path, store=Broken())
    with pytest.raises(AuthUnavailable):
        a.enroll_open()
    good = new_auth(tmp_path)
    cookie = good.start_session("c")
    with pytest.raises(AuthUnavailable):
        new_auth(tmp_path, store=Broken()).session(cookie)


def test_failed_attempts_are_limited_before_anything_is_checked(tmp_path):
    a = new_auth(tmp_path)
    for _ in range(10):
        with pytest.raises(AuthError) as e:
            a.enroll_begin("x" * 40, "9.9.9.9")
        assert e.value.status == 403
    with pytest.raises(AuthError) as e:
        a.enroll_begin(TOKEN, "9.9.9.9")  # the right token, but nothing is checked any more from there
    assert e.value.status == 429
    assert a.enroll_begin(TOKEN, "8.8.8.8")["ceremony"]  # another address still can


def test_a_full_global_counter_never_blocks_the_owners_passkey(tmp_path):
    a = new_auth(tmp_path)
    owner = SoftKey()
    enroll(a, owner)
    for i in range(60):  # strangers' passkey sign-ins, from many addresses: never counted globally
        b = a.signin_begin(f"10.0.{i}.1")
        with pytest.raises(AuthError):
            a.signin_finish(b["ceremony"], SoftKey().get(b["options"]), f"10.0.{i}.1")
    assert not a.global_fail.full("*")
    for i in range(50):  # wrong recovery codes from many addresses fill the global counter
        with pytest.raises(AuthError):
            a.recover_begin("0000-0000-0000-0000", f"10.1.{i}.1")
    assert a.global_fail.full("*")
    with pytest.raises(AuthError) as e:  # recovery codes and tokens are now refused from anywhere
        a.recover_begin("0000-0000-0000-0000", "10.2.0.1")
    assert e.value.status == 429
    assert a.session(signin(a, owner)) is not None  # the owner's passkey still signs in


def test_passkey_sign_in_keeps_its_per_address_limit(tmp_path):
    a = new_auth(tmp_path)
    enroll(a, SoftKey())
    for _ in range(10):
        with pytest.raises(AuthError) as e:
            b = a.signin_begin("9.9.9.9")
            a.signin_finish(b["ceremony"], SoftKey().get(b["options"]), "9.9.9.9")
        assert e.value.status == 403
    b = a.signin_begin("9.9.9.9")
    with pytest.raises(AuthError) as e:
        a.signin_finish(b["ceremony"], SoftKey().get(b["options"]), "9.9.9.9")
    assert e.value.status == 429


def test_recovery_excludes_no_passkey_but_adding_excludes_the_registered_ones(tmp_path):
    a = new_auth(tmp_path)
    k = SoftKey()
    cookie, codes = enroll(a, k)
    assert json.loads(a.recover_begin(codes[0], "1.1.1.1")["options"]).get("excludeCredentials", []) == []
    add = json.loads(a.add_begin(a.session(cookie), None, "1.1.1.1")["options"])
    assert [c["id"] for c in add["excludeCredentials"]] == [b64u(k.cred)]


def test_recovery_codes_read_like_invite_codes():
    assert normalize_code("abcd efgh ijkl mnop") == "ABCD-EFGH-1JK1-MN0P"


# --- the store ---------------------------------------------------------------------------------------------------


def test_file_store_creates_each_key_once(tmp_path):
    s = FileStore(tmp_path)
    assert s.create("sessions/abc", {"a": 1}) and not s.create("sessions/abc", {"a": 2})
    assert s.get("sessions/abc") == {"a": 1} and s.get("sessions/none") is None
    assert s.list("sessions/") == ["sessions/abc"] and s.list("nothing/") == []
    for bad in ("../x", "/etc/passwd", "sessions/../../x", "Sessions/x"):
        with pytest.raises(ValueError):
            s.create(bad, {})


@respx.mock
def test_gcs_store_creates_atomically_with_its_own_token():
    respx.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
    up = respx.post("https://storage.googleapis.com/upload/storage/v1/b/bkt/o").mock(
        side_effect=[httpx.Response(200, json={}), httpx.Response(412)]
    )
    s = GcsStore("bkt")
    assert s.create("enroll/spent", {"a": 1}) is True
    assert s.create("enroll/spent", {"a": 2}) is False
    req = up.calls[0].request
    assert req.url.params["ifGenerationMatch"] == "0" and req.headers["Authorization"] == "Bearer tok"
    respx.get("https://storage.googleapis.com/storage/v1/b/bkt/o/enroll%2Fspent").mock(
        return_value=httpx.Response(200, json={"a": 1})
    )
    respx.get("https://storage.googleapis.com/storage/v1/b/bkt/o/enroll%2Fnone").mock(
        return_value=httpx.Response(404)
    )
    assert s.get("enroll/spent") == {"a": 1} and s.get("enroll/none") is None
    respx.get("https://storage.googleapis.com/storage/v1/b/bkt/o").mock(side_effect=[
        httpx.Response(200, json={"items": [{"name": "uses/b"}], "nextPageToken": "p"}),
        httpx.Response(200, json={"items": [{"name": "uses/a"}]})])  # fmt: skip
    assert s.list("uses/") == ["uses/a", "uses/b"]


@respx.mock
def test_gcs_store_retries_a_read_once_but_never_a_create():
    respx.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
    read = respx.get("https://storage.googleapis.com/storage/v1/b/bkt/o/enroll%2Fspent").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"a": 1})]
    )
    up = respx.post("https://storage.googleapis.com/upload/storage/v1/b/bkt/o").mock(
        return_value=httpx.Response(503)
    )
    s = GcsStore("bkt")
    assert s.get("enroll/spent") == {"a": 1} and read.call_count == 2
    with pytest.raises(StoreError):
        s.create("enroll/spent", {})
    assert up.call_count == 1


@respx.mock
def test_gcs_store_errors_are_store_errors():
    respx.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    ).mock(return_value=httpx.Response(500))
    with pytest.raises(StoreError):
        GcsStore("bkt").get("enroll/spent")


# --- settings ----------------------------------------------------------------------------------------------------


def test_settings_problems_switch_the_service_off(tmp_path, monkeypatch):
    def s(**kw):
        base = {"origin": ORIGIN, "session_secret": SECRET, "enroll_token": TOKEN, "lake_url": str(tmp_path),
                "cache_dir": tmp_path, "store": FileStore(tmp_path)}  # fmt: skip
        return Settings(**(base | kw)).check().problems

    assert s() == []
    assert s(session_secret="short") and s(session_secret="")
    assert s(origin="") and s(origin="https://a.example/path") and s(origin="http://admin.example")
    assert s(store=None)
    assert s(hosted=True, origin="http://localhost:8001")
    assert s(hosted=True, origin="https://admin.registry.coffee")  # a local folder, hosted
    assert s(hosted=True, origin="https://admin.registry.coffee", store=GcsStore("bkt")) == []
    for k in ("K_SERVICE", "SPACE_ID", "ADMIN_STATE_BUCKET", "ADMIN_STATE_DIR", "ADMIN_SESSION_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ADMIN_ORIGIN", ORIGIN + "/")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", SECRET + "\n")
    monkeypatch.setenv("ADMIN_STATE_DIR", str(tmp_path / "st"))
    env = from_env()
    assert (
        env.problems == []
        and env.session_secret == SECRET
        and env.origin == ORIGIN
        and not env.secure_cookies
    )
    monkeypatch.setenv("K_SERVICE", "admin")
    assert from_env().problems  # hosted with a local folder: off


# --- the app: every route needs a session, and fails closed --------------------------------------------------------


def _app(tmp_path, lake_dir, **kw):
    st = Settings(origin=ORIGIN, session_secret=kw.pop("secret", SECRET), enroll_token=TOKEN, lake_url=str(lake_dir),
                  cache_dir=tmp_path / "cache", store=FileStore(tmp_path / "state")).check()  # fmt: skip
    return create_app(st, **kw)


def _routes(app):
    for r in app.routes:
        for m in sorted(getattr(r, "methods", None) or []):
            if m != "HEAD":
                yield m, r.path


WRITE = {"X-Requested-With": "fetch", "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}


def test_every_route_but_the_public_ones_refuses_anonymous_requests(tmp_path, lake):
    app = _app(tmp_path, lake.root)
    c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    routes = set(_routes(app))
    assert routes >= PUBLIC  # every public entry is a real route
    assert {r for r in routes if r in PUBLIC or r[1].startswith("/static/")} == PUBLIC | {
        ("GET", "/static/{rest:path}")
    }
    checked = 0
    for method, path in routes:
        if (method, path) in PUBLIC or path.startswith("/static/"):
            continue
        url = path.replace("{rest:path}", "x")
        r = c.request(method, url, headers=WRITE, json={})
        if method == "GET" and not path.startswith(("/api/", "/part/")):
            assert r.status_code == 303 and r.headers["location"].startswith("/signin"), (method, path)
        else:
            assert r.status_code == 401, (method, path, r.status_code)
        checked += 1
    assert checked >= 15
    for path in ("/no-such-page", "/part/nothing", "/api/nothing"):  # unknown paths too
        assert c.get(path).status_code in (303, 401)
    assert c.post("/api/nothing", headers=WRITE).status_code == 401


def test_a_misconfigured_service_answers_503_to_everything(tmp_path, lake):
    app = _app(tmp_path, lake.root, secret="tiny-secret-value")
    c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    for method, path in _routes(app):
        if path.startswith("/static/"):
            continue
        r = c.request(method, path.replace("{rest:path}", "x"), headers=WRITE, json={})
        assert r.status_code == 503, (method, path)
    assert "tiny-secret-value" not in c.get("/week").text and "switched off" in c.get("/week").text


def test_writes_need_the_same_origin_markers(tmp_path, lake):
    c = TestClient(_app(tmp_path, lake.root), base_url=ORIGIN)
    assert c.post("/api/auth/signin/begin", json={}).status_code == 403
    assert (
        c.post(
            "/api/auth/signin/begin", json={}, headers=WRITE | {"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/auth/signin/begin", json={}, headers=WRITE | {"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert c.post("/api/auth/signin/begin", json={}, headers=WRITE).status_code == 200


def test_sign_in_through_the_api_sets_a_strict_session_cookie(tmp_path, lake):
    app = _app(tmp_path, lake.root)
    c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    assert c.get("/signin").headers["location"] == "/enroll"  # no passkey yet
    key = SoftKey()
    b = c.post("/api/auth/enroll/begin", json={"token": TOKEN}, headers=WRITE).json()
    r = c.post(
        "/api/auth/enroll/finish",
        json={"ceremony": b["ceremony"], "credential": key.create(b["options"])},
        headers=WRITE,
    )
    assert r.status_code == 200 and len(r.json()["codes"]) == 10
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie and "max-age=43200" in set_cookie
    assert c.get("/part/release").status_code == 200
    c.cookies.clear()
    assert c.get("/part/release").status_code == 401
    b = c.post("/api/auth/signin/begin", json={}, headers=WRITE).json()
    r = c.post("/api/auth/signin/finish", headers=WRITE,
               json={"ceremony": b["ceremony"], "credential": key.get(b["options"]), "next": "//evil.example/x"})  # fmt: skip
    assert r.json()["next"] == "/week"  # never another origin
    assert c.get("/part/release").status_code == 200
    assert c.post("/api/auth/signout", headers=WRITE).status_code == 200
    assert c.get("/part/release").status_code == 401


@pytest.mark.parametrize("bad", [5, True, ["x"], "text", 1.5])
def test_a_credential_that_isnt_an_object_is_a_bad_request(tmp_path, lake, bad):
    app = _app(tmp_path, lake.root)
    c = TestClient(app, base_url=ORIGIN)
    b = c.post("/api/auth/enroll/begin", json={"token": TOKEN}, headers=WRITE).json()
    assert c.post("/api/auth/enroll/finish", json={"ceremony": b["ceremony"], "credential": bad},
                  headers=WRITE).status_code == 400  # fmt: skip
    key = SoftKey()
    b = c.post("/api/auth/enroll/begin", json={"token": TOKEN}, headers=WRITE).json()
    r = c.post("/api/auth/enroll/finish", json={"ceremony": b["ceremony"], "credential": key.create(b["options"])},
               headers=WRITE)  # fmt: skip
    codes = r.json()["codes"]
    b = c.post("/api/auth/add/begin", json={"code": None}, headers=WRITE).json()
    assert c.post("/api/auth/add/finish", json={"ceremony": b["ceremony"], "credential": bad},
                  headers=WRITE).status_code == 400  # fmt: skip
    b = c.post("/api/auth/recover/begin", json={"code": codes[0]}, headers=WRITE).json()
    assert c.post("/api/auth/recover/finish", json={"ceremony": b["ceremony"], "credential": bad},
                  headers=WRITE).status_code == 400  # fmt: skip


def test_the_secure_cookie_is_host_only_on_https(tmp_path, lake):
    st = Settings(origin="https://admin.registry.coffee", session_secret=SECRET, enroll_token=TOKEN,
                  lake_url=str(lake.root), cache_dir=tmp_path, store=FileStore(tmp_path / "s")).check()  # fmt: skip
    c = TestClient(create_app(st), base_url="https://admin.registry.coffee")
    key = SoftKey("admin.registry.coffee", "https://admin.registry.coffee")
    h = WRITE | {"Origin": "https://admin.registry.coffee"}
    b = c.post("/api/auth/enroll/begin", json={"token": TOKEN}, headers=h).json()
    r = c.post(
        "/api/auth/enroll/finish",
        json={"ceremony": b["ceremony"], "credential": key.create(b["options"])},
        headers=h,
    )
    assert r.headers["set-cookie"].startswith("__Host-admin=") and "secure" in r.headers["set-cookie"].lower()


def test_pages_read_the_lake_and_never_write_it(tmp_path, lake):
    before = tree(lake.root)
    app = _app(tmp_path, lake.root)
    c = TestClient(app, base_url=ORIGIN)
    c.cookies.set("admin_session", app.state.auth.start_session("c"))
    for url in ("/week", "/jobs", "/release", "/catalog", "/log", "/part/week", f"/part/week?week={W2}",
                f"/part/release?build={B2}", "/part/release", f"/part/release?build={B2}&reason=unexplained",
                f"/part/release?build={B1}", f"/part/catalog?build={B2}&q=guji&coffee=coffee", "/part/catalog",
                "/part/jobs", "/part/log", "/account"):  # fmt: skip
        r = c.get(url)
        assert r.status_code == 200, url
        h = r.headers
        assert h["cache-control"] == "no-store" and "frame-ancestors 'none'" in h["content-security-policy"]
    assert tree(lake.root) == before
    assert list((tmp_path / "cache").glob("catalog-*.duckdb"))  # the cache is outside the lake


def test_pages_say_what_they_read(tmp_path, lake):
    app = _app(tmp_path, lake.root)
    c = TestClient(app, base_url=ORIGIN)
    c.cookies.set("admin_session", app.state.auth.start_session("c"))
    week = c.get(f"/part/week?week={W2}").text
    assert "2 of 5 connectors landed" in week and f"Serving week {W1}; week {W3} has data" in week
    release = c.get(f"/part/release?build={B2}").text  # Gate and Changes, one page
    assert 'class="unexplained"' in release and "3 rows change" in release
    assert "1 failing and 1 warning tests of 3" in release and "1 passing test<" in release
    assert "javascript:" not in c.get(f"/part/catalog?build={B2}").text + c.get("/part/week").text
    b1 = c.get(f"/part/release?build={B1}").text
    assert "No changes" in b1 or "Not recorded" in b1
    assert (
        "Actions are off" in release
    )  # no database, Cloud Run or snapshot store here: writes off, said once
    empty_week = c.get(f"/part/week?week={W3}").text
    assert 'data-state="empty"' in empty_week


def test_a_slow_or_broken_lake_shows_the_error_state(tmp_path, lake):
    import time

    import duckdb

    def broken():
        raise duckdb.IOException("HTTP 503 from hf://datasets/secret/path")

    app = _app(tmp_path, lake.root, reader=Reader(lake, 5, before=broken))
    c = TestClient(app, base_url=ORIGIN)
    c.cookies.set("admin_session", app.state.auth.start_session("c"))
    r = c.get("/part/week")
    assert (
        r.status_code == 502
        and "data-retry" in r.text
        and "HTTP 503" in r.text
        and "secret/path" not in r.text
    )
    slow = _app(tmp_path / "slow", lake.root, reader=Reader(lake, 0.2, before=lambda: time.sleep(1)))
    c = TestClient(slow, base_url=ORIGIN)
    c.cookies.set("admin_session", slow.state.auth.start_session("c"))
    r = c.get("/part/week")
    assert r.status_code == 504 and "answer in 0.2 s" in r.text


def test_one_deadline_covers_all_the_reads_of_a_part(tmp_path, lake):
    import time

    slow = _app(tmp_path, lake.root, reader=Reader(lake, 0.6, before=lambda: time.sleep(0.25)))
    c = TestClient(slow, base_url=ORIGIN)
    c.cookies.set("admin_session", slow.state.auth.start_session("c"))
    started = time.monotonic()
    r = c.get(f"/part/week?week={W2}")  # several reads of 0.25 s each: over the part's 0.6 s
    assert r.status_code == 504 and time.monotonic() - started < 1.5


def test_a_sign_in_with_a_next_that_isnt_text_still_lands_on_the_admin(tmp_path, lake):
    app = _app(tmp_path, lake.root)
    c = TestClient(app, base_url=ORIGIN)
    key = SoftKey()
    b = c.post("/api/auth/enroll/begin", json={"token": TOKEN}, headers=WRITE).json()
    c.post(
        "/api/auth/enroll/finish",
        json={"ceremony": b["ceremony"], "credential": key.create(b["options"])},
        headers=WRITE,
    )
    b = c.post("/api/auth/signin/begin", json={}, headers=WRITE).json()
    r = c.post("/api/auth/signin/finish", headers=WRITE,
               json={"ceremony": b["ceremony"], "credential": key.get(b["options"]), "next": ["/x"]})  # fmt: skip
    assert r.status_code == 200 and r.json()["next"] == "/week"


def test_the_admin_image_runs_the_admin(tmp_path):
    from pathlib import Path

    docker = (Path(__file__).parents[1] / "app" / "Dockerfile").read_text()
    assert "COPY --chown=user admin ./admin" in docker
