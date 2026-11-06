"""coffeedata command line. Every command appends to the lake; `publish` ships staged files to a remote lake."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .lake import Lake, new_batch_id, remote_folders
from .ratelimit import SHOPIFY_RPS

# fct_shot and mart_bag are personal: they go to the private lake's marts/ only, never to the public export.
DBT_TARGET = Path("dbt/target")
SERVED_DB = Path("build/served.duckdb")  # the served release's tables, for the public export and the site
# The weekly fallback's crawl (both ingest passes) stops starting roasters this many minutes after the run starts, so
# enrichment, classify (which waits up to 45 minutes for Claude's batches), the build and the export still fit in the
# workflow's 240-minute limit (.github/workflows/weekly.yml). Roasters left unfetched wait for a re-run, which resumes.
WEEKLY_CRAWL_MIN = 150.0
# crawl-retry without a connector stops starting stores here, under the same 240-minute limit as the crawls.
CRAWL_RETRY_MIN = 225.0
MARTS = ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event", "fct_shot", "mart_bag"]


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _run_date(value: str) -> str:
    """argparse type for --run-date: a clean usage error instead of a traceback."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def _positive_float(value: str) -> float:
    """argparse type for a rate: a usage error up front, not a ValueError after the lake is opened."""
    try:
        x = float(value)
    except ValueError:
        x = 0.0
    if not x > 0:
        raise argparse.ArgumentTypeError(f"not a positive number: {value!r}")
    return x


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=1, default=str))


def cmd_init(args: argparse.Namespace) -> None:
    """Create the lake. For hf:// this creates the dataset repo (private unless --public), adds the dataset
    card, and publishes the empty table schemas. Safe to re-run."""
    lake = Lake()
    if not lake.remote:
        print(f"local lake ready at {lake.url}")
        return
    from huggingface_hub import HfApi

    repo_id = lake.url.removeprefix("hf://datasets/").split("@")[0]
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    url = api.create_repo(repo_id, repo_type="dataset", private=not args.public, exist_ok=True)
    card = Path("docs/DATASET_CARD.md")
    if card.exists():
        api.upload_file(
            path_or_fileobj=str(card),
            path_in_repo="README.md",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message="dataset card",
        )
    print(
        f"dataset {url} ready ({'public' if args.public else 'private'}); "
        f"published {lake.publish('init: table schemas', schemas=True)} schema files"
    )


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
    from . import discover, ingest, registry

    lake = Lake()
    states = registry.current(lake)
    cutoff = (date.fromisoformat(args.run_date) - timedelta(days=args.stale_days)).isoformat()
    todo = [
        s.roaster.roaster_id for s in states if s.status != "opted_out" and (s.last_probed or "") < cutoff
    ]
    if args.only:
        todo = [args.only]
    if args.sample:  # the same stable hash order ingest uses, so `weekly --sample` probes what it ingests
        todo = sorted(todo, key=ingest.sample_key)[: args.sample]
    rows = discover.probe(todo, workers=args.workers, run_date=args.run_date)
    lake.write_partition("probes", args.run_date, new_batch_id(), rows)
    ok = sum(r["status"] == "ok" for r in rows)
    print(f"probed {len(rows)} roasters ({ok} reachable)")


def cmd_ingest(args: argparse.Namespace) -> None:
    from . import ingest

    lake = Lake()
    states, mode = ingest.select_states(lake, args.run_date, args.only, args.sample, args.retry_failed)
    print(f"ingesting {len(states)} roasters for run {args.run_date}", file=sys.stderr)
    deadline_min = getattr(args, "deadline_min", None)
    _print(
        ingest.run(
            lake,
            states,
            args.run_date,
            workers=args.workers,
            mode=mode,
            shopify_rps=args.shopify_rps,
            **({} if deadline_min is None else {"deadline_s": 60 * deadline_min}),
        )
    )


def cmd_crawl(args: argparse.Namespace) -> None:
    from . import connectors

    out = connectors.crawl(
        Lake(),
        args.connector,
        args.run_date,
        args.deadline_min,
        rps=args.rps,
        part=args.slice,
        salt=args.salt,
        only=args.only,
    )
    _print(out)
    # A crawl stopped by its deadline is a finished job, not a failed one: it exits 0, and its job_manifests end row
    # says `deadline` with the count left (a resume slot or crawl-retry picks them up). A non-zero exit would make
    # Cloud Run start another attempt that re-asks the stores that already refused.
    if out.get("skipped_deadline"):
        print(
            f"crawl {args.connector}: deadline reached, {out['skipped_deadline']} roasters left",
            file=sys.stderr,
        )
    if out.get("partial"):  # ok fetches that read only part of a catalog (fetches.partial; RUNBOOK §5.3)
        every = out.get("partial_every_week") or []
        print(
            f"crawl {args.connector}: {out['partial']} catalogs read in part"
            + (f"; partial every week: {', '.join(every)}" if every else ""),
            file=sys.stderr,
        )


def cmd_merge(args: argparse.Namespace) -> None:
    from . import connectors

    _print(connectors.merge(Lake(), args.run_date))


def cmd_crawl_retry(args: argparse.Namespace) -> None:
    from . import ingest

    lake, run_date = Lake(), args.run_date
    if args.connector:  # design B: the connector's own partition, its job_manifests rows and the crash guard
        from . import connectors

        _print(
            connectors.crawl_retry(
                lake, args.connector, run_date, workers=args.workers, deadline_min=args.deadline_min
            )
        )
        return
    states, resume = ingest.select_parked(lake, run_date)
    print(f"resuming {len(states)} parked stores for run {args.run_date}", file=sys.stderr)
    _print(
        ingest.run(
            lake,
            states,
            run_date,
            workers=args.workers,
            mode="retry_failed",
            resume=resume,
            deadline_s=60 * (args.deadline_min or CRAWL_RETRY_MIN),
        )
    )


def cmd_fetch_health(args: argparse.Namespace) -> None:
    """The fetch-health check (fetchhealth.py): did each fetch return a catalog of about the right size. Exits
    non-zero on a systemic fetch problem not already found by an earlier check this week, which fails the Cloud Run
    execution and emails the owner once per finding; per-roaster problems only go on its list."""
    from . import fetchhealth

    out = fetchhealth.run(Lake(), args.run_date, force=args.force)
    _print(out)
    if out.get("alert"):
        # One structured line, so the execution's log names the problem at ERROR severity.
        print(
            json.dumps(
                {
                    "severity": "ERROR",
                    "message": "fetch-health: systemic fetch failure",
                    "systemic": out["systemic"],
                }
            )
        )
        raise SystemExit(f"fetch-health {out['run_date']}: systemic: {json.dumps(out['alert'])}")
    if out.get("systemic"):  # found again by a later pass's check: already alerted this week, logged only
        print(json.dumps({"severity": "WARNING", "message": "fetch-health: systemic, already alerted this week",
                          "systemic": out["systemic"]}))  # fmt: skip


def cmd_enrich_week(args: argparse.Namespace) -> None:
    from . import connectors

    _print(connectors.enrich_week(Lake(), args.run_date, args.limit, submit=args.submit))


def cmd_egress_check(args: argparse.Namespace) -> None:
    from . import control

    flags = control.egress_flags(Lake())
    _print({"flagged": flags})
    if flags:
        raise SystemExit(f"egress: {len(flags)} IP(s) refused on >8% of first contacts two runs in a row")


def cmd_enrich(args: argparse.Namespace) -> None:
    from . import enrich, extract

    if not extract.llm_enabled():
        print("no Anthropic credentials; rules-only enrichment")
        return
    lake = Lake()
    _print(
        {
            "collect": enrich.collect(lake, args.run_date),
            "enrich": enrich.run(lake, args.limit, args.run_date),
        }
    )


def cmd_classify(args: argparse.Namespace) -> None:
    """Claude's verdicts (verdict.py): collect finished batches, submit what's pending, optionally wait for it."""
    import time

    from . import verdict

    # Fails (non-zero, naming what is missing) unless rules only is explicitly allowed: a skipped classify once
    # left every release to the rules alone without anyone noticing.
    if not verdict.require(getattr(args, "allow_rules_only", False)):
        print(
            "rules only (explicitly allowed): no COFFEEDATA_VERDICT_MODEL or Anthropic credentials; nothing to do"
        )
        return
    lake = Lake()
    out = {"collect": verdict.collect(lake, args.run_date), "submit": verdict.submit(lake, args.limit)}
    deadline = time.monotonic() + args.wait * 60
    # Wait for this run's batches only; whatever is still running is collected by the next run.
    waiting = set(out["submit"].get("batch_ids", []))
    while waiting and time.monotonic() < deadline:
        time.sleep(60)
        got = verdict.collect(lake, args.run_date)
        out["collect_after_wait"] = got["collected"] + out.get("collect_after_wait", 0)
        waiting &= verdict.open_batch_ids(lake)
    _print(out)
    if err := out["submit"].get("error"):
        # A rejected key or an API outage leaves this week's new versions without verdicts. Fail like a missing
        # model or key, so the build job's execution fails and emails; the build still runs on the stored verdicts.
        raise SystemExit(
            f"Claude's verdict batch submit failed: {err}. This week's new product versions have no verdicts yet "
            "(docs/ingestion/RUNBOOK.md, verdicts)."
        )


def cmd_eval_classifier(args: argparse.Namespace) -> None:
    """Score a verdict model on the labeled sets in tests/fixtures (synchronous calls), or write its predictions
    for a sealed set without seeing any labels."""
    from . import verdict

    if args.predict:
        rows = json.loads(Path(args.predict).read_text())
        usage: dict[str, int] = {}
        preds = verdict.judge(rows, args.model, usage=usage)
        Path(args.out).write_text(json.dumps({r["id"]: preds[i] for i, r in enumerate(rows)}))
        print(f"wrote {len(preds)} predictions to {args.out}; token usage {usage}")
        return
    labeled = [json.loads(line) for line in Path(args.labels).read_text().splitlines()]
    for name in args.sets.split(","):
        rows = [r for r in labeled if r["set"] == name]
        rows = [r | {"body_text": r["body"]} for r in rows]
        usage = {}
        preds = verdict.judge(rows, args.model, usage=usage)
        wrong = sum(preds[i] != (r["label"] == "coffee") for i, r in enumerate(rows))
        print(
            f"{name}: {1 - wrong / len(rows):.1%} ({wrong} wrong of {len(rows)}) with {args.model}; tokens {usage}"
        )


def dbt_build(
    lake: Lake,
    db: Path,
    extra_vars: dict | None = None,
    select: list[str] | None = None,
    target: Path | None = None,
    extra_roots: tuple[str, ...] = (),
    flags: tuple[str, ...] = (),
) -> tuple[bool, list[dict]]:
    """Build every model and seed, then run every test, so a failed check never stops a model from being built.
    Returns whether every step passed, and each step's run_results.json (dbt overwrites the file per command).
    dbt's unit tests check SQL logic over hand-written rows, not this lake's data, so they run in `make test`.
    select, target, extra_roots and flags are for `fetch-health`: only the nodes it names, its own target folder (the
    weekly build's artifacts are left alone), more lake roots to read (its copy of the unmerged crawl output) and
    more dbt flags."""
    from . import quality, registry, shots, verdict

    roots = [str(lake.root), *extra_roots] + ([lake.url] if lake.remote else [])
    folders = remote_folders(lake.url) if lake.remote else None
    db.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"COFFEEDATA_DB": str(db)}
    dbt_vars = json.dumps(
        {
            "lake_roots": roots,
            # Remote folders that exist: a table with none yet is read from staging alone (a 404 otherwise).
            "remote_folders": sorted(folders) if folders is not None else None,
            "max_failures": registry.MAX_FAILURES,
            "owner_user_id": shots.owner_user_id(),
            "verdict_model": verdict.model() or "",
            "verdict_definition": verdict.DEFINITION_VERSION,
            # verdicts_cover_catalog: fails a build with no verdict model, or too few listed coffees decided by
            # one, unless rules only is explicitly allowed (cmd_build's --allow-rules-only overrides this).
            "require_verdicts": not verdict.rules_only_allowed(),
            "coverage_claims": quality.coverage_claims(strict=False),  # never stops a build
        }
        | (extra_vars or {})
    )
    passed, results = True, []
    where = ["--target-path", str(target.resolve())] if target else []
    target = target or DBT_TARGET  # read here, not as a default: tests point DBT_TARGET elsewhere
    (target / "manifest.json").unlink(missing_ok=True)
    picked = ["--select", *select] if select else []
    for step in (
        ["build", *picked, "--exclude", "resource_type:test", "resource_type:unit_test"],
        ["test", *picked, "--exclude", "resource_type:unit_test"],
    ):
        # A step that crashes before writing its results must not be recorded with the previous step's.
        (target / "run_results.json").unlink(missing_ok=True)
        r = subprocess.run(
            [
                "dbt",
                *step,
                "--project-dir",
                "dbt",
                "--profiles-dir",
                "dbt",
                *where,
                *flags,
                "--vars",
                dbt_vars,
            ],
            env=env,
        )
        passed = passed and r.returncode == 0
        try:
            results.append(json.loads((target / "run_results.json").read_text()))
        except (OSError, ValueError):
            results.append({})
    return passed, results


def cmd_build(args: argparse.Namespace) -> bool:
    """Build, check, and record the build either way. Only a build whose checks all pass replaces the current
    marts (what the site, the app, the public export and the MCP tools other than run_sql read); a failing one is still kept, with its results and a
    copy of its catalog marts, for `build-diff`. Returns whether the checks passed."""
    from . import quality

    lake = Lake()
    db = Path(args.db)
    if db.exists():
        db.unlink()  # derived: always rebuilt from the lake
    # The run the checks judge: the one named, or the latest in the lake, so a rebuild between weekly runs (the
    # site, the app) checks the last real run instead of an empty "today". Freshness is measured against today.
    run_date = getattr(args, "run_date", None) or quality.latest_run(lake) or _today()
    build_id = new_batch_id()  # every release table carries it as release_build_id
    extra: dict = {"as_of_run": run_date, "today": _today(), "build_id": build_id}
    served = (quality.served(lake) or {}).get("build_id")
    if (
        served
    ):  # the volume check compares with what customers get now, which after a rollback isn't the newest
        extra["baseline_build"] = served
    if getattr(args, "allow_rules_only", False):
        extra["require_verdicts"] = False
    if getattr(args, "accept_volume_change", False):
        extra["volume_tolerance"] = 1e9  # this build passes and becomes the next one's baseline
        # It skips the anomaly bands too, and records accepted_volume_change, so their history restarts at it.
        extra["accept_volume_change"] = True
    started = time.monotonic()
    passed, results = dbt_build(lake, db, extra)
    took = time.monotonic() - started
    try:  # the published coverage, against this build: only a report, so it never stops the build's record
        claim_lines = quality.coverage_claim_messages(db)
    except Exception as e:  # e.g. a partial coffee.duckdb after a crashed dbt step
        logging.warning("coverage claims not reported: %s", e)
        claim_lines = []
    for line in claim_lines:
        print(f"coverage claim: {line}", file=sys.stderr)
    try:  # the python_readers_agree_on_tags test fails the build on these; this shows which products
        parity_lines = quality.tag_parity_messages(db)
    except Exception as e:
        logging.warning("tag parity not reported: %s", e)
        parity_lines = []
    for line in parity_lines:
        print(f"tag parity: {line}", file=sys.stderr)
    try:  # the share verdicts_cover_catalog judges, so a failing (or narrowly passing) build shows it
        if verdict_line := quality.verdict_coverage_message(db):
            print(f"verdicts: {verdict_line}", file=sys.stderr)
    except Exception as e:
        logging.warning("verdict coverage not reported: %s", e)
    # A release that couldn't be written in full is recorded as failed, so it is never served or a baseline.
    passed = quality.record_build(
        lake, db, run_date, DBT_TARGET / "manifest.json", results, passed, build_id, took
    )
    _write_personal_marts(lake, db, quality.SERVED_MARTS)
    if not passed:
        print(
            f"build {build_id} failed its checks: its release is recorded, the served release is unchanged "
            "(`coffeedata build-diff` shows what it would have changed)",
            file=sys.stderr,
        )
        return False
    # The release read above is the one this replaces: serve doesn't read it again (a 429 there would leave a
    # passing release recorded but unserved).
    quality.serve(lake, build_id, f"build for {run_date}", SERVED_DB, previous=served)
    print(f"built {db}; serving release {build_id}")
    return True


def _write_personal_marts(lake: Lake, db: Path, catalog: tuple[str, ...]) -> None:
    """Shots and bags are personal, not part of a catalog release, so catalog checks don't hold them back: each is
    exported whenever its own model built."""
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        for m in (m for m in MARTS if m not in catalog):
            try:
                lake.write_mart(m, con, f"select * from {m}")
            except duckdb.Error as e:
                logging.warning("mart %s not exported: %s", m, e)
    finally:
        con.close()


def cmd_rollback(args: argparse.Namespace) -> None:
    """Serve an earlier passing release, then re-export the public tables and rebuild the site from it."""
    from . import quality

    lake = Lake()
    to = quality.rollback(lake, args.to, args.reason)
    cmd_publish(argparse.Namespace(message=f"rollback to release {to}"))
    _export_and_site(SERVED_DB, args.out)
    print(f"serving release {to}")


def _export_and_site(db: Path, out: str) -> None:
    """Everything customers see outside the dataset, from the served release."""
    cmd_export_public(argparse.Namespace(db=str(db), to="build/public"))
    if os.environ.get("PUBLIC_EXPORT_URL") and os.environ["PUBLIC_EXPORT_URL"] != "build/public":
        cmd_export_public(argparse.Namespace(db=str(db), to=os.environ["PUBLIC_EXPORT_URL"]))
    # Snowflake is loaded by its own workflow step after the site is built, so a Snowflake problem (such as a
    # contract change that needs `load-snowflake --migrate`) never holds back the site.
    cmd_site(argparse.Namespace(db=str(db), out=out, served=False))


def cmd_build_diff(args: argparse.Namespace) -> None:
    from . import quality

    _print(quality.build_diff(Lake(), args.old, args.new))


def cmd_override(args: argparse.Namespace) -> None:
    from . import quality

    path = quality.add_override(Lake(), args.roaster, args.run_date, args.action, args.reason, args.batch)
    print(
        f"recorded {args.action} for {args.roaster} {args.run_date} ({path.name}); the next build applies it"
    )


def cmd_publish(args: argparse.Namespace) -> None:
    lake = Lake()
    print(
        f"published {lake.publish(args.message)} files to {lake.url}"
        if lake.remote
        else "local lake; nothing to publish"
    )


def cmd_export_public(args: argparse.Namespace) -> None:
    from . import public

    _print(public.export(Path(args.db), args.to))


def cmd_load_snowflake(args: argparse.Namespace) -> None:
    from . import snowflake_load

    _print(
        snowflake_load.load(
            Path(args.source),
            migrate=getattr(args, "migrate", False),
            dry_run=getattr(args, "dry_run", False),
        )
    )


def cmd_weekly(args: argparse.Namespace) -> None:
    """The whole weekly run, in order. Safe to re-run: each step skips what this run already has."""
    from . import verdict

    run = args.run_date
    rules_only = getattr(args, "allow_rules_only", False)
    # Before hours of crawling: the run's classify and build would fail without verdicts.
    verdict.require(rules_only)
    crawl_until = time.monotonic() + 60 * WEEKLY_CRAWL_MIN  # both ingest passes stop starting roasters here
    crawl_left = lambda: max(0.0, (crawl_until - time.monotonic()) / 60)  # noqa: E731
    if date.fromisoformat(run).day <= 7 or args.discover:
        lake = Lake()
        month = run[:7]
        done = {
            r[0]
            for r in lake.connect()
            .execute(
                f"select distinct split_part(source, '-', 1) from {lake.scan('registry')} "
                f"where strftime(added_at, '%Y-%m') = ?",
                [month],
            )
            .fetchall()
        }
        for src in ("overture", "osm"):
            if src in done and not args.discover:
                continue  # once a month; re-running the week doesn't re-record it
            try:
                cmd_discover(argparse.Namespace(source=src, release=None, states=None))
            except Exception as e:  # noqa: BLE001 - discovery is best-effort; the catalog run must still happen
                logging.warning("discover %s failed: %s", src, e)
    # Publish after every step that records new data, so a later failure (build, export, Snowflake, site) or a
    # job timeout never loses what was already fetched or paid for. Re-running the week resumes from the lake.
    cmd_probe(argparse.Namespace(stale_days=28, only=None, sample=args.sample, workers=16, run_date=run))
    cmd_publish(argparse.Namespace(message=f"weekly run {run}: discover and probe"))
    cmd_ingest(
        argparse.Namespace(
            run_date=run,
            only=None,
            sample=args.sample,
            retry_failed=False,
            workers=32,
            shopify_rps=SHOPIFY_RPS,
            deadline_min=crawl_left(),
        )
    )
    cmd_publish(argparse.Namespace(message=f"weekly run {run}: catalogs"))
    cmd_ingest(
        argparse.Namespace(
            run_date=run,
            only=None,
            sample=None,
            retry_failed=True,
            workers=16,
            shopify_rps=SHOPIFY_RPS,
            deadline_min=crawl_left(),
        )
    )
    cmd_publish(argparse.Namespace(message=f"weekly run {run}: retries"))
    cmd_enrich(argparse.Namespace(limit=args.enrich_limit, run_date=run))
    cmd_publish(argparse.Namespace(message=f"weekly run {run}: enrichment"))
    classify_failed = None
    try:  # as in the build job: a failed classify fails the run, but the build still runs on the stored verdicts
        cmd_classify(argparse.Namespace(limit=100_000, run_date=run, wait=45, allow_rules_only=rules_only))
    except SystemExit as e:
        classify_failed = str(e.code)
        print(f"classify failed: {classify_failed}", file=sys.stderr)
    cmd_publish(argparse.Namespace(message=f"weekly run {run}: verdicts"))
    # The checks gate publishing, not the run: the build is recorded and published either way (its results and a
    # copy of its catalog marts), but only a passing build replaces the current marts, public tables and site.
    accept = getattr(args, "accept_volume_change", False)
    passed = (
        cmd_build(
            argparse.Namespace(
                db=args.db, run_date=run, accept_volume_change=accept, allow_rules_only=rules_only
            )
        )
        is not False
    )
    cmd_publish(
        argparse.Namespace(message=f"weekly run {run}: {'marts' if passed else 'failed build record'}")
    )
    if not passed:
        raise SystemExit(f"weekly run {run}: the build failed its data checks; nothing derived was published")
    # Facts-only tables and the site come from the served release (this build's, since it passed).
    _export_and_site(SERVED_DB, args.out)
    if classify_failed:
        raise SystemExit(f"weekly run {run}: classify failed; the build ran on the verdicts already stored")


def cmd_shot(args: argparse.Namespace) -> None:
    from . import shots

    lake = Lake()
    if args.action == "add":
        shot, matches = shots.log_shot(json.loads(args.json), lake)
    else:
        body = Path(args.file).read_text() if args.file else sys.stdin.read()
        shot, matches = shots.log_shot(shots.parse_issue_body(body) | {"source": "issue"}, lake)
    lake.publish(f"shot {shot.shot_id}")
    print(shots.issue_reply(shot, matches, lake))


def cmd_add_roaster(args: argparse.Namespace) -> None:
    from . import discover, issueform, registry

    body = Path(args.file).read_text() if args.file else sys.stdin.read()
    fields = issueform.parse(body)
    domain = registry.normalize_domain(fields.get("website", ""))
    if not registry.is_storefront_host(domain):
        raise SystemExit(f"not a roaster website: {fields.get('website')!r}")
    try:
        state = discover.state_code(fields.get("state", ""))
    except ValueError as e:
        raise SystemExit(str(e)) from None
    opted_out = issueform.checked(fields.get("opt out", ""))
    lake = Lake()
    now = datetime.now(UTC).replace(microsecond=0)
    lake.write(
        "registry",
        f"registry/source=manual/{domain}-{new_batch_id()}.parquet",
        [
            {
                "added_at": now,
                "source": "manual",
                "roaster_id": domain,
                "name": fields.get("name") or domain,
                "state": state,
                "tagged_roastery": True,
                "opted_out": opted_out,
            }
        ],
    )
    if opted_out:
        lake.publish(f"opt out {domain}")
        print(f"`{domain}` is opted out and won't be fetched again.")
        return
    rows = discover.probe([domain], workers=1)
    lake.write_partition("probes", _today(), new_batch_id(), rows)
    lake.publish(f"add roaster {domain}")
    p = rows[0]
    print(
        f"Recorded **{fields.get('name') or domain}** (`{domain}`). Probe: `{p['status']}`, platform "
        f"`{p['platform'] or 'unknown'}`. Its catalog is fetched in the next weekly run."
    )


def cmd_site(args: argparse.Namespace) -> None:
    from . import site

    # The release customers get now, read from the lake; nothing is written to it.
    if args.served:
        from . import quality

        release = quality.served(Lake())
        if release is None and args.skip_if_unserved:
            print("nothing is served yet: the site isn't built", file=sys.stderr)
            return
        if release is None:
            raise SystemExit(
                "nothing is served yet: the site is built from the served release, which the first weekly run "
                "whose checks pass creates (or `coffeedata rollback --to <build>`)"
            )
        quality.materialize(Lake(), release["build_id"], Path(args.db))
    print(site.build(Path(args.db), Path(args.out)))


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import mcp

    mcp.run()


def cmd_delete_user(args: argparse.Namespace) -> None:
    from . import erase

    main, photos, users = Lake(), Lake.photos(), Lake.users()
    try:
        p = erase.plan(args.username, main, photos, users, force=args.force)
    except erase.EraseError as e:
        sys.exit(str(e))
    print(f"{p.username} ({p.user_id}):")
    for url, paths in p.files.items():
        print(f"  {url}: {len(paths)} files")
        for rel in paths:
            print(f"    {rel}")
    for m in p.marts:
        print(f"  marts/{m}.parquet: rewritten without their rows")
    if args.dry_run:
        print("Dry run: nothing deleted.")
        return
    erase.apply(p, main, photos, users)
    print("Deleted. Deploy again (or restart the service) so the app forgets the turned-off account.")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="coffeedata", description="Lake: $COFFEEDATA_LAKE (default ./lake)")
    sub = p.add_subparsers(required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    ini = add("init", cmd_init, "create the lake (for hf://: the dataset repo, card and table schemas)")
    ini.add_argument("--public", action="store_true", help="make a new HF dataset public (default private)")
    d = add("discover", cmd_discover, "record roasters from an open dataset")
    d.add_argument("source", choices=["overture", "osm"])
    d.add_argument("--release", help="Overture release (default: latest)")
    d.add_argument("--states", nargs="*", help="OSM: limit to these state codes")

    pr = add("probe", cmd_probe, "detect storefront platforms for roasters not probed recently")
    pr.add_argument("--stale-days", type=int, default=28)
    pr.add_argument("--only")
    pr.add_argument("--sample", type=int)
    pr.add_argument("--workers", type=int, default=16)
    pr.add_argument("--run-date", type=_run_date, default=_today())

    i = add("ingest", cmd_ingest, "fetch catalogs for this run (skips roasters already fetched in it)")
    i.add_argument("--run-date", type=_run_date, default=_today())
    i.add_argument("--only", help="one roaster_id (domain)")
    i.add_argument("--sample", type=int)
    i.add_argument("--retry-failed", action="store_true", help="only roasters whose fetch failed in this run")
    i.add_argument("--workers", type=int, default=32)
    i.add_argument(
        "--deadline-min", type=_positive_float, help="stop starting roasters after this many minutes"
    )
    i.add_argument(
        "--shopify-rps",
        type=_positive_float,
        default=SHOPIFY_RPS,
        help="requests/s across all Shopify stores together (default %(default)s; halves itself on 429s)",
    )

    from .connectors import CONNECTORS

    cr = add("crawl", cmd_crawl, "one connector's weekly job (or its resume) into its raw partition")
    cr.add_argument("--connector", required=True, choices=sorted(CONNECTORS))
    cr.add_argument("--run-date", type=_run_date, default=_today(), help="any day of the week to crawl")
    cr.add_argument(
        "--deadline-min", type=_positive_float, help="stop starting roasters after this many minutes"
    )
    cr.add_argument("--rps", type=_positive_float, help="override the connector's request budget (req/s)")
    cr.add_argument("--slice", help="crawl one slice k/n of a salted-hash split of its roasters, e.g. 0/3")
    cr.add_argument("--salt", default="arm", help="the split's salt (record it with the results)")
    cr.add_argument(
        "--only", help="fetch this one roaster_id, even if done or quarantined this week (RUNBOOK section 7)"
    )
    mg = add("merge", cmd_merge, "union the week's connector partitions into the run dbt reads")
    mg.add_argument("--run-date", type=_run_date, default=_today(), help="any day of the week to merge")
    rr = add("crawl-retry", cmd_crawl_retry, "resume the run's parked stores from their blocked page")
    rr.add_argument("--run-date", type=_run_date, default=_today())
    rr.add_argument("--workers", type=int, default=8)
    rr.add_argument("--connector", choices=sorted(CONNECTORS), help="design B: that connector's partition")
    rr.add_argument(
        "--deadline-min",
        type=_positive_float,
        help=f"stop starting stores after this many minutes (default: the connector's, else {CRAWL_RETRY_MIN:.0f})",
    )

    dq = add(
        "fetch-health",
        cmd_fetch_health,
        "fetch-health check: did each fetch return a catalog of about the right size (gate rules, unmerged week)",
    )
    dq.add_argument("--run-date", type=_run_date, default=_today(), help="any day of the week to check")
    dq.add_argument(
        "--force", action="store_true", help="check now, even while a crawl runs or nothing new has landed"
    )

    ew = add(
        "enrich-week", cmd_enrich_week, "post-load enrichment: Claude candidates from raw, after the merge"
    )
    ew.add_argument("--run-date", type=_run_date, default=_today())
    ew.add_argument("--limit", type=int, default=2000)
    ew.add_argument("--submit", action="store_true", help="send to Claude (spends); without it, only count")
    add(
        "egress-check", cmd_egress_check, "flag egress IPs refused on >8% of first contacts two runs in a row"
    )

    c = add("classify", cmd_classify, "Claude's coffee-or-not verdicts (collects finished batches first)")
    c.add_argument("--limit", type=int, default=100_000)
    c.add_argument("--run-date", default=None)
    c.add_argument("--wait", type=int, default=0, help="minutes to wait for the new batch")
    c.add_argument(
        "--allow-rules-only",
        action="store_true",
        help="exit 0 without a verdict model or credentials (local and dev runs; also COFFEEDATA_ALLOW_RULES_ONLY=1)",
    )
    ev = add("eval-classifier", cmd_eval_classifier, "score a verdict model on the labeled sets")
    ev.add_argument("--model", required=True)
    ev.add_argument("--labels", default="tests/fixtures/classifier_labels.jsonl")
    ev.add_argument("--sets", default="train,validation")
    ev.add_argument("--predict", help="JSON list of products to predict (a sealed set); writes --out")
    ev.add_argument("--out", default="predictions.json")
    e = add("enrich", cmd_enrich, "Claude enrichment of pending coffees (collects finished batches first)")
    e.add_argument("--limit", type=int, default=2000)
    e.add_argument("--run-date", type=_run_date, default=_today())

    b = add("build", cmd_build, "dbt build from the lake, then export marts")
    b.add_argument("--db", default="build/coffee.duckdb")
    b.add_argument(
        "--run-date",
        type=_run_date,
        help="the run this build checks (default: the latest in the lake). A run older than the latest in the "
        "lake fails public_coffee_is_fresh, so this can't re-release an earlier week: to serve one again, "
        "`coffeedata rollback` to its release, or build the latest week",
    )
    b.add_argument(
        "--accept-volume-change",
        action="store_true",
        help="let one build through the volume check and the anomaly bands (it becomes the next baseline, and "
        "the bands' history starts again at it); the fixed floors still apply",
    )
    b.add_argument(
        "--allow-rules-only",
        action="store_true",
        help="turn off the verdict gate (verdicts_cover_catalog) for a deliberate rules-only build (local and dev "
        "runs; also COFFEEDATA_ALLOW_RULES_ONLY=1)",
    )

    bd = add(
        "build-diff", cmd_build_diff, "what changed between two builds (default: the last two that passed)"
    )
    bd.add_argument("--old")
    bd.add_argument("--new")

    rb = add(
        "rollback", cmd_rollback, "serve an earlier passing release (default: the one before the current)"
    )
    rb.add_argument(
        "--to", help="build id of the release to serve, or 'served' to re-export the served one (no crawl)"
    )
    rb.add_argument("--reason", default="", help="recorded in marts/release.parquet")
    rb.add_argument("--out", default="build/site")

    ov = add("override", cmd_override, "accept, reject or pin one roaster-week's fetch for the history")
    ov.add_argument("--roaster", required=True)
    ov.add_argument("--run-date", type=_run_date, required=True)
    ov.add_argument("--action", choices=["accept", "reject", "use_batch"], required=True)
    ov.add_argument("--batch", help="the batch to count (use_batch)")
    ov.add_argument("--reason", required=True)

    pb = add("publish", cmd_publish, "upload staged lake files to the remote lake")
    pb.add_argument("--message", default=f"publish {_today()}")

    ep = add(
        "export-public", cmd_export_public, "export the facts-only public tables (dir, hf://, s3://, gs://)"
    )
    ep.add_argument(
        "--db", default=str(SERVED_DB), help="default: the served release, never an unchecked build"
    )
    ep.add_argument("--to", default=os.environ.get("PUBLIC_EXPORT_URL", "build/public"))

    sf = add(
        "load-snowflake", cmd_load_snowflake, "load an export-public folder into Snowflake (internal stage)"
    )
    sf.add_argument("--from", dest="source", default="build/public")
    sf.add_argument(
        "--migrate",
        action="store_true",
        help="after a public contract change: recreate changed tables and their share views, then load",
    )
    sf.add_argument(
        "--dry-run",
        action="store_true",
        help="change nothing: print the checks' reads and the statements it would run",
    )

    w = add("weekly", cmd_weekly, "the full weekly run")
    w.add_argument("--run-date", type=_run_date, default=_today())
    w.add_argument("--sample", type=int, help="only N roasters (for trying it out)")
    w.add_argument("--discover", action="store_true", help="run discovery even if it isn't the first week")
    w.add_argument("--enrich-limit", type=int, default=2000)
    w.add_argument("--db", default="build/coffee.duckdb")
    w.add_argument("--out", default="build/site")
    w.add_argument("--accept-volume-change", action="store_true", help="see build --accept-volume-change")
    w.add_argument(
        "--allow-rules-only", action="store_true", help="see classify and build --allow-rules-only"
    )

    s = add("shot", cmd_shot, "log a shot")
    s.add_argument("action", choices=["add", "from-issue"])
    s.add_argument("--json", help="shot fields as JSON (add)")
    s.add_argument("--file", help="issue body file (from-issue); default stdin")

    du = add("delete-user", cmd_delete_user, "delete one person's shots, bags, gear, photos and account")
    du.add_argument("username")
    du.add_argument("--dry-run", action="store_true", help="list the files, delete nothing")
    du.add_argument(
        "--force", action="store_true", help="even if the account isn't turned off (the app isn't running)"
    )

    ar = add("add-roaster", cmd_add_roaster, "add (or opt out) a roaster from an issue-form body")
    ar.add_argument("--file", help="issue body file; default stdin")

    st = add("site", cmd_site, "build the static site")
    st.add_argument(
        "--db", default=str(SERVED_DB), help="default: the served release, never an unchecked build"
    )
    st.add_argument("--out", default="build/site")
    st.add_argument(
        "--served",
        action="store_true",
        help="first fetch the served release from the lake into --db (read-only)",
    )
    st.add_argument(
        "--skip-if-unserved",
        action="store_true",
        help="with --served: build nothing and exit 0 when no release is served yet (the site workflow)",
    )

    add("mcp", cmd_mcp, "run the MCP server over stdio")

    args = p.parse_args(argv)
    if args.fn(args) is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
