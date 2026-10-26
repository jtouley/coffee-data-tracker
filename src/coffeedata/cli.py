"""coffeedata command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def _discover(args: argparse.Namespace) -> None:
    from . import discover, registry

    found = discover.overture(args.release) if args.source == "overture" else discover.osm(args.states)
    rows, added = registry.merge(registry.load(), found)
    registry.save(rows)
    print(f"{args.source}: {len(found)} candidates, {added} new roasters, registry now {len(rows)}")


def _probe(args: argparse.Namespace) -> None:
    import random
    from datetime import date, timedelta

    from . import discover, registry

    rows = registry.load()
    cutoff = (date.today() - timedelta(days=args.stale_days)).isoformat()
    todo = [r for r in rows if (r.get("last_probed") or "") < cutoff or args.all]
    if args.only:
        todo = [r for r in rows if args.only in (r["slug"], r["domain"])]
    if args.sample:
        todo = random.sample(todo or rows, min(args.sample, len(todo or rows)))
    before = {r["slug"]: r.get("platform") for r in todo}
    discover.probe(todo, workers=args.workers)
    if not args.dry_run:
        registry.save(rows)
    same = sum(before[r["slug"]] == r.get("platform") for r in todo)
    print(f"probed {len(todo)}; platform unchanged for {same} ({same / max(len(todo), 1):.0%})")


def _ingest(args: argparse.Namespace) -> None:
    from . import ingest, registry

    rows = registry.load()
    shard = tuple(int(x) for x in args.shard.split("/")) if args.shard else None
    todo = ingest.select(rows, args.only, args.sample, shard)  # type: ignore[arg-type]
    print(f"ingesting {len(todo)} roasters", file=sys.stderr)
    cov = ingest.run(rows, todo, workers=args.workers, enrich_max=args.enrich_max)
    registry.save(rows)
    print(json.dumps({k: cov[k] for k in ("this_run", "coffees_this_run", "events_this_run",
                                          "llm_enriched_this_run")}, indent=1))


def _collect(args: argparse.Namespace) -> None:
    from . import extract, ingest

    if not extract.llm_enabled():
        print("no Anthropic credentials; skipping")
        return
    print(f"merged {ingest.collect_batches()} enrichments from finished batches")


def _shot(args: argparse.Namespace) -> None:
    from . import shots

    if args.action == "add":
        payload = json.loads(args.json)
        shot, matches = shots.log_shot(payload)
        print(json.dumps({"shot_id": shot.shot_id, "coffee_id": shot.coffee_id, "matches": matches,
                          "advice": shots.suggest_next(shot.coffee_id or shot.coffee_freetext or "")}, indent=1))
    elif args.action == "from-issue":
        body = Path(args.file).read_text() if args.file else sys.stdin.read()
        shot, matches = shots.log_shot(shots.parse_issue_body(body))
        print(shots.issue_reply(shot, matches))


def _add_roaster(args: argparse.Namespace) -> None:
    import re

    from . import discover, registry

    body = Path(args.file).read_text() if args.file else sys.stdin.read()
    fields = {m.group(1).strip().lower(): m.group(2).strip()
              for m in re.finditer(r"^###\s+(.+?)\s*\n(.*?)(?=^###\s|\Z)", body, re.S | re.M)}
    website = fields.get("website", "")
    domain = registry.normalize_domain(website)
    if "." not in domain:
        raise SystemExit(f"not a website: {website!r}")
    state = fields.get("state", "")
    rows, added = registry.merge(registry.load(), [{
        "domain": domain, "name": fields.get("name", domain), "source": "manual", "tagged_roastery": "true",
        "state": "" if state == "_No response_" else state.upper()[:2]}])
    row = next(r for r in rows if r["domain"] == domain)
    discover.probe([row], workers=1)
    registry.save(rows)
    verb = "Added" if added else "Already in the registry:"
    print(f"{verb} **{row['name']}** (`{row['slug']}`, {domain}). Platform: `{row.get('platform') or 'unknown'}`, "
          f"status: `{row['status']}`. Its catalog is picked up on the next daily run.")


def _site(args: argparse.Namespace) -> None:
    from . import site

    print(site.build(Path(args.db), Path(args.out)))


def _mcp(args: argparse.Namespace) -> None:
    from .mcp_server import mcp

    mcp.run()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="coffeedata")
    sub = p.add_subparsers(required=True)

    d = sub.add_parser("discover", help="add roasters from an open dataset to config/roasters.csv")
    d.add_argument("source", choices=["overture", "osm"])
    d.add_argument("--release", help="Overture release (default: latest)")
    d.add_argument("--states", nargs="*", help="OSM: limit to these state codes")
    d.set_defaults(fn=_discover)

    pr = sub.add_parser("probe", help="detect each roaster's storefront platform")
    pr.add_argument("--stale-days", type=int, default=7)
    pr.add_argument("--all", action="store_true")
    pr.add_argument("--only")
    pr.add_argument("--sample", type=int)
    pr.add_argument("--workers", type=int, default=16)
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(fn=_probe)

    i = sub.add_parser("ingest", help="fetch catalogs, store snapshots and observations, enrich")
    i.add_argument("--only", help='one roaster slug or domain, or "failed" to retry last run\'s failures')
    i.add_argument("--sample", type=int)
    i.add_argument("--shard", help="i/n, e.g. 0/4")
    i.add_argument("--workers", type=int, default=32)
    i.add_argument("--enrich-max", type=int, default=2000)
    i.set_defaults(fn=_ingest)

    c = sub.add_parser("collect-batches", help="merge finished Claude enrichment batches")
    c.set_defaults(fn=_collect)

    s = sub.add_parser("shot", help="log a shot")
    s.add_argument("action", choices=["add", "from-issue"])
    s.add_argument("--json", help="shot fields as JSON (add)")
    s.add_argument("--file", help="issue body file (from-issue); default stdin")
    s.set_defaults(fn=_shot)

    ar = sub.add_parser("add-roaster", help="add a roaster from an issue-form body")
    ar.add_argument("--file", help="issue body file; default stdin")
    ar.set_defaults(fn=_add_roaster)

    st = sub.add_parser("site", help="build the static site")
    st.add_argument("--db", default="build/coffee.duckdb")
    st.add_argument("--out", default="build/site")
    st.set_defaults(fn=_site)

    m = sub.add_parser("mcp", help="run the MCP server over stdio")
    m.set_defaults(fn=_mcp)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
