"""coffeedata command line. Every command appends to the lake; `publish` ships staged files to a remote lake."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .lake import Lake, new_batch_id

MARTS = ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event"]


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=1, default=str))


def cmd_init(args: argparse.Namespace) -> None:
    lake = Lake()
    print(f"lake ready at {lake.url}" + (f"; published {lake.publish('init')} schema files" if lake.remote else ""))


def cmd_discover(args: argparse.Namespace) -> None:
    from . import discover

    lake = Lake()
    found = discover.overture(args.release) if args.source == "overture" else discover.osm(args.states)
    now = datetime.now(UTC).replace(microsecond=0)
    rows = [r | {"added_at": now, "opted_out": False} for r in found]
    key = (args.release or "latest") if args.source == "overture" else _today()
    lake.write("registry", f"registry/source={args.source}/{key}-{new_batch_id()}.parquet", rows)
    print(f"{args.source}: {len(rows)} roasters recorded (new ones are picked up by the next probe)")


def cmd_probe(args: argparse.Namespace) -> None:
    import random

    from . import discover, registry

    lake = Lake()
    states = registry.current(lake)
    cutoff = (date.fromisoformat(args.run_date) - timedelta(days=args.stale_days)).isoformat()
    todo = [s.roaster.roaster_id for s in states if s.status != "opted_out" and (s.last_probed or "") < cutoff]
    if args.only:
        todo = [args.only]
    if args.sample:
        todo = random.sample(todo, min(args.sample, len(todo)))
    rows = discover.probe(todo, workers=args.workers, run_date=args.run_date)
    lake.write_partition("probes", args.run_date, new_batch_id(), rows)
    ok = sum(r["status"] == "ok" for r in rows)
    print(f"probed {len(rows)} roasters ({ok} reachable)")


def cmd_ingest(args: argparse.Namespace) -> None:
    from . import ingest

    lake = Lake()
    todo = ingest.select(lake, args.run_date, args.only, args.sample, args.retry_failed)
    print(f"ingesting {len(todo)} roasters for run {args.run_date}", file=sys.stderr)
    _print(ingest.run(lake, todo, args.run_date, workers=args.workers))


def cmd_enrich(args: argparse.Namespace) -> None:
    from . import enrich, extract

    if not extract.llm_enabled():
        print("no Anthropic credentials; rules-only enrichment")
        return
    lake = Lake()
    _print({"collect": enrich.collect(lake, args.run_date), "enrich": enrich.run(lake, args.limit, args.run_date)})


def dbt_build(lake: Lake, db: Path) -> None:
    roots = [str(lake.root)] + ([lake.url] if lake.remote else [])
    db.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"COFFEEDATA_DB": str(db)}
    subprocess.run(["dbt", "build", "--project-dir", "dbt", "--profiles-dir", "dbt",
                    "--vars", json.dumps({"lake_roots": roots})], check=True, env=env)


def cmd_build(args: argparse.Namespace) -> None:
    import duckdb

    lake = Lake()
    db = Path(args.db)
    if db.exists():
        db.unlink()  # derived: always rebuilt from the lake
    dbt_build(lake, db)
    con = duckdb.connect(str(db), read_only=True)
    for m in MARTS:
        lake.write_mart(m, con, f"select * from {m}")
    con.close()
    print(f"built {db}; exported {', '.join(MARTS)} to {lake.root / 'marts'}")


def cmd_publish(args: argparse.Namespace) -> None:
    lake = Lake()
    print(f"published {lake.publish(args.message)} files to {lake.url}" if lake.remote
          else "local lake; nothing to publish")


def cmd_weekly(args: argparse.Namespace) -> None:
    """The whole weekly run, in order. Safe to re-run: each step skips what this run already has."""
    run = args.run_date
    if date.fromisoformat(run).day <= 7 or args.discover:
        lake = Lake()
        month = run[:7]
        done = {r[0] for r in lake.connect().execute(
            f"select distinct split_part(source, '-', 1) from {lake.scan('registry')} "
            f"where strftime(added_at, '%Y-%m') = ?", [month]).fetchall()}
        for src in ("overture", "osm"):
            if src in done and not args.discover:
                continue  # once a month; re-running the week doesn't re-record it
            try:
                cmd_discover(argparse.Namespace(source=src, release=None, states=None))
            except Exception as e:  # noqa: BLE001 - discovery is best-effort; the catalog run must still happen
                logging.warning("discover %s failed: %s", src, e)
    cmd_probe(argparse.Namespace(stale_days=28, only=None, sample=args.sample, workers=16, run_date=run))
    cmd_ingest(argparse.Namespace(run_date=run, only=None, sample=args.sample, retry_failed=False, workers=32))
    cmd_ingest(argparse.Namespace(run_date=run, only=None, sample=None, retry_failed=True, workers=16))
    cmd_enrich(argparse.Namespace(limit=args.enrich_limit, run_date=run))
    cmd_build(argparse.Namespace(db=args.db))
    cmd_publish(argparse.Namespace(message=f"weekly run {run}"))
    cmd_site(argparse.Namespace(db=args.db, out=args.out))


def cmd_shot(args: argparse.Namespace) -> None:
    from . import shots

    lake = Lake()
    if args.action == "add":
        shot, matches = shots.log_shot(json.loads(args.json), lake)
    else:
        body = Path(args.file).read_text() if args.file else sys.stdin.read()
        shot, matches = shots.log_shot(shots.parse_issue_body(body), lake)
    lake.publish(f"shot {shot.shot_id}")
    print(shots.issue_reply(shot, matches, lake))


def cmd_add_roaster(args: argparse.Namespace) -> None:
    from . import discover, registry

    body = Path(args.file).read_text() if args.file else sys.stdin.read()
    fields = {m.group(1).strip().lower(): m.group(2).strip()
              for m in re.finditer(r"^###\s+(.+?)\s*\n(.*?)(?=^###\s|\Z)", body, re.S | re.M)}
    domain = registry.normalize_domain(fields.get("website", ""))
    if not registry.is_storefront_host(domain):
        raise SystemExit(f"not a roaster website: {fields.get('website')!r}")
    state = fields.get("state", "")
    opted_out = bool(re.search(r"-\s*\[x\]", fields.get("opt out", ""), re.I))
    lake = Lake()
    now = datetime.now(UTC).replace(microsecond=0)
    lake.write("registry", f"registry/source=manual/{domain}-{new_batch_id()}.parquet", [{
        "added_at": now, "source": "manual", "roaster_id": domain, "name": fields.get("name") or domain,
        "state": "" if state == "_No response_" else state.upper()[:2], "tagged_roastery": True,
        "opted_out": opted_out}])
    if opted_out:
        lake.publish(f"opt out {domain}")
        print(f"`{domain}` is opted out and won't be fetched again.")
        return
    rows = discover.probe([domain], workers=1)
    lake.write_partition("probes", _today(), new_batch_id(), rows)
    lake.publish(f"add roaster {domain}")
    p = rows[0]
    print(f"Recorded **{fields.get('name') or domain}** (`{domain}`). Probe: `{p['status']}`, platform "
          f"`{p['platform'] or 'unknown'}`. Its catalog is fetched in the next weekly run.")


def cmd_site(args: argparse.Namespace) -> None:
    from . import site

    print(site.build(Path(args.db), Path(args.out)))


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import mcp

    mcp.run()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="coffeedata", description="Lake: $COFFEEDATA_LAKE (default ./lake)")
    sub = p.add_subparsers(required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "create the lake's schema files (and publish them to a remote lake)")
    d = add("discover", cmd_discover, "record roasters from an open dataset")
    d.add_argument("source", choices=["overture", "osm"])
    d.add_argument("--release", help="Overture release (default: latest)")
    d.add_argument("--states", nargs="*", help="OSM: limit to these state codes")

    pr = add("probe", cmd_probe, "detect storefront platforms for roasters not probed recently")
    pr.add_argument("--stale-days", type=int, default=28)
    pr.add_argument("--only")
    pr.add_argument("--sample", type=int)
    pr.add_argument("--workers", type=int, default=16)
    pr.add_argument("--run-date", default=_today())

    i = add("ingest", cmd_ingest, "fetch catalogs for this run (skips roasters already fetched in it)")
    i.add_argument("--run-date", default=_today())
    i.add_argument("--only", help="one roaster_id (domain)")
    i.add_argument("--sample", type=int)
    i.add_argument("--retry-failed", action="store_true", help="only roasters whose fetch failed in this run")
    i.add_argument("--workers", type=int, default=32)

    e = add("enrich", cmd_enrich, "Claude enrichment of pending coffees (collects finished batches first)")
    e.add_argument("--limit", type=int, default=2000)
    e.add_argument("--run-date", default=_today())

    b = add("build", cmd_build, "dbt build from the lake, then export marts")
    b.add_argument("--db", default="build/coffee.duckdb")

    pb = add("publish", cmd_publish, "upload staged lake files to the remote lake")
    pb.add_argument("--message", default=f"publish {_today()}")

    w = add("weekly", cmd_weekly, "the full weekly run")
    w.add_argument("--run-date", default=_today())
    w.add_argument("--sample", type=int, help="only N roasters (for trying it out)")
    w.add_argument("--discover", action="store_true", help="run discovery even if it isn't the first week")
    w.add_argument("--enrich-limit", type=int, default=2000)
    w.add_argument("--db", default="build/coffee.duckdb")
    w.add_argument("--out", default="build/site")

    s = add("shot", cmd_shot, "log a shot")
    s.add_argument("action", choices=["add", "from-issue"])
    s.add_argument("--json", help="shot fields as JSON (add)")
    s.add_argument("--file", help="issue body file (from-issue); default stdin")

    ar = add("add-roaster", cmd_add_roaster, "add (or opt out) a roaster from an issue-form body")
    ar.add_argument("--file", help="issue body file; default stdin")

    st = add("site", cmd_site, "build the static site")
    st.add_argument("--db", default="build/coffee.duckdb")
    st.add_argument("--out", default="build/site")

    add("mcp", cmd_mcp, "run the MCP server over stdio")

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
