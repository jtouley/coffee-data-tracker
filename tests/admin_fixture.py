"""The admin's test lake: the recorded storefronts (tests/smoke.py's fixture lake) over three weeks, built twice
through `coffeedata build`, so every admin page reads real builds, releases and ops tables (#124):

- weeks 1 and 2: the five recorded stores plus six stores that fail in each way the Failures page counts (a
  SiteGround challenge, a Cloudflare challenge, a 429, a bare 202, a partial catalog, a quarantined store), the
  control rows of every pass and stage, and a passing build that is served;
- week 3 (the current week): fetches only, so the served release is a week behind (the stale state) and the Week page
  has no job rows for it yet (the empty state).

Needs dbt. Writes only under `root`.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from coffeedata import cli, connectors, control, ingest
from coffeedata.lake import Lake, new_batch_id
from coffeedata.models import Roaster

from .conftest import load_roasters, recorded_items
from .lakehelp import product
from .smoke import fixture_lake

FAILING = {
    "sg-challenge.example": ("woocommerce", "error", 202, {"sg_captcha": "1"}),
    "cf-challenge.example": ("generic", "error", 403, {"cf_mitigated": "challenge"}),
    "rate-limited.example": ("shopify", "error", 429, {}),
    "bare-202.example": ("woocommerce", "error", 202, {}),
    "partial.example": ("squarespace", "ok", 200, {}),
    "quarantined.example": ("generic", "crashed", None, {}),
}


def _at(day: date, hour: int, minute: int = 17) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC).replace(hour=hour, minute=minute)


def _failures(lake: Lake, week: str, which: tuple[str, ...]) -> None:
    d = date.fromisoformat(week)
    failing = {r: v for r, v in FAILING.items() if r in which}
    stores = [Roaster(roaster_id=r, name=r, host=r, platform=a) for r, (a, *_rest) in failing.items()]
    mid = new_batch_id()
    lake.write_partition("run_manifests", week, mid, ingest.manifest_rows(stores, d, mid, mode="full"))
    rows, snaps = [], []
    b = new_batch_id()
    for rid, (adapter, status, http, block) in failing.items():
        blocks = None
        if http and http != 200:
            blocks = [{"at": _at(d, 9), "status": http, "host": rid, "path": "/products.json", "server": "edge",
                       "cf_mitigated": block.get("cf_mitigated"), "sg_captcha": block.get("sg_captcha"),
                       "egress_ip": "203.0.113.7"}]  # fmt: skip
        rows.append({
            "run_date": d, "batch_id": b, "fetched_at": _at(d, 9), "roaster_id": rid, "adapter": adapter,
            "status": status, "error": None if status == "ok" else f"HTTP {http}" if http else "quarantined",
            "products": 2 if status == "ok" else 0, "coffees": 2 if status == "ok" else 0, "pages": 1,
            "requests": 3, "http_statuses": [{"status": http, "responses": 3}] if http else [],
            "rate_limited": 3 if http == 429 else 0, "blocks": blocks, "partial": status == "ok",
        })  # fmt: skip
        if status == "ok":
            for p in (product(f"{rid}-1"), product(f"{rid}-2")):
                snaps.append(p | {"run_date": d, "batch_id": b, "fetched_at": _at(d, 9), "roaster_id": rid,
                                  "url": f"https://{rid}/products/{p['product_id']}"})  # fmt: skip
    lake.write_partition("fetches", week, b, rows)
    lake.write_partition("snapshots", week, b, snaps)


def _onyx_changes(lake: Lake, week: str) -> None:
    """Onyx fetched again later in the week, with its first coffee $2 dearer and its second gone: rows the release
    changes against the served one (release_changes)."""
    d = date.fromisoformat(week)
    roasters = load_roasters()
    items = recorded_items(roasters)["onyxcoffeelab.com"]
    dearer = items[0].model_copy(update={"variants": [v.model_copy(update={"price_usd": (v.price_usd or 0) + 2})
                                                      for v in items[0].variants]})  # fmt: skip
    b = new_batch_id()
    results = [ingest.Result(roasters["onyxcoffeelab.com"], _at(d, 15), [dearer, *items[2:]])]
    for table, rows in ingest.rows_for(results, d, b, set()).items():
        lake.write_partition(table, week, b, rows)


def _jobs(lake: Lake, week: str, retry_failed: bool) -> None:
    d = date.fromisoformat(week)

    def row(job, connector, start_h, status, **counts):
        control.write(lake, {
            "run_date": week, "job": job, "connector": connector, "attempt_id": new_batch_id(),
            "started_at": _at(d, start_h), "ended_at": _at(d, start_h + 1) if status != "running" else None,
            "status": status, "code_version": "test", "egress_ips": ["203.0.113.7"],
            "detail": {"execution": f"{job}-{connector or 'all'}-{start_h}"}, **counts,
        })  # fmt: skip

    for name in connectors.CONNECTORS:
        row("crawl", name, 9, "done", todo=40, ok=36, errors=4, parked=0, skipped=0, http_429=2, http_430=0)
    row("crawl", "woocommerce", 15, "failed" if retry_failed else "done", todo=4, ok=1, errors=3, http_429=1)
    row("crawl-retry", "shopify", 21, "deadline", todo=2, ok=1, errors=1, http_429=0)
    for job, h in (("merge", 3), ("enrich", 4), ("classify", 5)):
        control.write(lake, {"run_date": week, "job": job, "attempt_id": new_batch_id(),
                             "started_at": _at(d + timedelta(days=1), h), "ended_at": _at(d + timedelta(days=1), h, 40),
                             "status": "done", "code_version": "test"})  # fmt: skip


def build_admin_lake(root: Path, today: date | None = None) -> dict:
    """Write and build the lake under root/lake (dbt artifacts under root/dbt). Returns the weeks and build ids."""
    root = Path(root).resolve()  # dbt runs from dbt/: its target and log paths must be absolute
    today = today or datetime.now(UTC).date()
    w3 = connectors.week_of(today.isoformat())
    w2 = (date.fromisoformat(w3) - timedelta(days=7)).isoformat()
    w1 = (date.fromisoformat(w3) - timedelta(days=14)).isoformat()
    lake_dir = root / "lake"
    old = {
        k: os.environ.get(k)
        for k in ("COFFEEDATA_LAKE", "COFFEEDATA_ALLOW_RULES_ONLY", "DBT_TARGET_PATH", "DBT_LOG_PATH")
    }
    saved = (cli.DBT_TARGET, cli.SERVED_DB, cli._today)
    os.environ |= {"COFFEEDATA_LAKE": str(lake_dir), "COFFEEDATA_ALLOW_RULES_ONLY": "1",
                   "DBT_TARGET_PATH": str(root / "dbt-target"), "DBT_LOG_PATH": str(root / "dbt-logs")}  # fmt: skip
    cli.DBT_TARGET, cli.SERVED_DB = root / "dbt-target", root / "served.duckdb"
    try:
        lake = Lake(str(lake_dir))
        built = {}
        # Week 1 passes the gate (its one extra store is partial, which counts as ok); week 2 fails run_ok_rate (min 90%).
        week1 = ("partial.example",)
        for week, retry_failed, which in ((w1, False, week1), (w2, True, tuple(FAILING))):
            fixture_lake(lake, week)
            _failures(lake, week, which)
            if week == w2:
                _onyx_changes(lake, week)
            _jobs(lake, week, retry_failed)
            cli._today = lambda w=week: (date.fromisoformat(w) + timedelta(days=1)).isoformat()
            try:
                cli.main(["build", "--db", str(root / f"coffee-{week}.duckdb"), "--run-date", week])
            except SystemExit as e:  # a build that fails its gate exits 1; it is recorded all the same
                if e.code not in (0, 1, None):
                    raise
            built[week] = [r for r in control.rows(lake, week) if r["job"] == "build"][-1]
        fixture_lake(lake, w3)  # this week: fetched, not yet built
    finally:
        cli.DBT_TARGET, cli.SERVED_DB, cli._today = saved
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return {
        "lake": lake_dir,
        "weeks": [w1, w2, w3],
        "builds": {w: control.detail_of(r)["build_id"] for w, r in built.items()},
    }


def flag_served_build(lake_dir: Path, week: str, build: str) -> None:
    """Give a copy of the built lake's served build the flags the Queue's states need, on real products of it: two
    drink mixes from two stores ready to remove on the same evidence, and a classifier removal that pasted text
    doubts. The fixture's stores raise no such flags of themselves (dbt's flag_* models are tested in
    tests/test_flags.py); this replaces the build's ops file in a test copy only."""
    import json

    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from coffeedata import quality
    from coffeedata.lake import SCHEMAS

    lake = Lake(str(lake_dir), read_only=True)
    ops = quality.ops_files(lake, build)
    picks = (
        duckdb.connect()
        .execute(
            f"select coffee_id, roaster_id, input_hash, title from '{ops['product_decisions']}' where is_coffee "
            "qualify row_number() over (partition by roaster_id order by coffee_id) = 1 order by roaster_id limit 3"
        )
        .fetchall()
    )
    (a, b, c) = picks
    base = {"build_id": build, "decision": None, "coverage_problem": False, "restores": 0, "verdict_label": "coffee",
            "classified_by": "rules", "reason": "coffee_words", "tags": ["drink mix"], "bag_sizes_g": [283],
            "prices_usd": [14.0], "description": "A cocoa drink mix. Stir two spoons into hot milk."}  # fmt: skip

    def row(p, rule, family, polarity, direction, evidence, key, eh, **kw):
        return base | {"rule": rule, "family": family, "polarity": polarity, "direction": direction,
                       "coffee_id": p[0], "roaster_id": p[1], "input_hash": p[2], "title": p[3],
                       "product_type": "Drink Mix", "evidence": json.dumps(evidence), "evidence_key": key,
                       "evidence_hash": eh, "url": f"https://{p[1]}/products/x", "public_rows": 4} | kw  # fmt: skip

    ready = {"classifier_is_coffee": True, "was_public": True, "held": False, "ready_flags": True,
             "supports_families": 2, "doubts": 0}  # fmt: skip
    doubted = {"classifier_is_coffee": False, "was_public": True, "held": False, "ready_flags": False,
               "supports_families": 0, "doubts": 1, "restores": 1, "public_rows": 2, "product_type": ""}  # fmt: skip
    rows = [
        row(a, "verdict_flip", "classifier", "supports", "added", {"verdict": "coffee"}, "added:coffee", "mix", **ready),
        row(a, "non_coffee_type", "listing", "supports", "", {"product_type": "Drink Mix"}, "drink mix", "mix", **ready),
        row(b, "verdict_flip", "classifier", "supports", "added", {"verdict": "coffee"}, "added:coffee", "mix", **ready),
        row(b, "non_coffee_type", "listing", "supports", "", {"product_type": "Drink Mix"}, "drink mix", "mix", **ready),
        row(c, "verdict_flip", "classifier", "restore", "removed", {"verdict": "not_coffee"}, "removed", "pasted",
            **doubted),
        row(c, "shared_body", "catalog", "doubts", "", {"products": 4, "text": "this sample pack includes three bags"},
            "p1", "pasted", **doubted),
    ]  # fmt: skip
    pq.write_table(pa.Table.from_pylist(rows, SCHEMAS["mart_flags"]), ops["mart_flags"])


if __name__ == "__main__":  # python -m tests.admin_fixture <root>: for the Playwright run and screenshots
    import json
    import sys

    print(json.dumps(build_admin_lake(Path(sys.argv[1])), default=str))
