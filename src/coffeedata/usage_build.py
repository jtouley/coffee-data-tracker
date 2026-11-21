"""The usage reports' build: the private dbt project dbt_usage/ over the private usage dataset, into its own
database file, build/usage.duckdb (design sections 0, 3-6; docs/usage/README.md).

Like the importers' derived view (importers.py), it is a project and a database of their own: the main build
(dbt/), the release, the public tables and their contracts, the site, the public export, Snowflake and the MCP
server never read either (tests/test_usage_dbt.py). The raw events and the pinned geo files are only read.

Before dbt runs, the geo files config/geo.lock names are found in the cache (build/geo/) or copied from the usage
dataset, and each one's sha256 is checked: a file that isn't the locked one stops the build. dbt then reads exactly
those files, and ops_usage_build records the lock. Same raw plus same lock gives the same rows.

Before any of that, `events_state` reads the dataset's event file names (never a file) for the newest stored day,
and `events_alert` turns it into the `prod:usage-events` triage alert: is the collector storing anything? A dataset
with no event at all is reported there, and the build is not attempted (there is nothing to build from).

The detector (dbt_usage/tests/search_text_looks_personal.sql) fails the build when any search looks like an email,
phone or card number. `detector_alert` turns its counts into a triage alert that says "N searches in week W matched
the email pattern" and nothing more: the issue is in a public repository, so it never holds the text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from . import geo
from .importers import MAIN_DB, PUBLIC_FOLDERS, SERVED_DB

DBT_PROJECT = "dbt_usage"
# The private database's one name and place, relative to the checkout; the project refuses any other
# (dbt_usage/macros/private_target.sql).
DB = "build/usage.duckdb"
# Where the locked geo files are kept between builds (not committed: build/ is ignored).
GEO_CACHE = "build/geo"
# Where dbt's output goes by default: never the console (usage_build.build).
DBT_LOG = "build/usage-dbt.log"
# The raw events under the usage dataset's root (design section 0: events/day=YYYY-MM-DD/batch=<id>.parquet).
EVENTS_GLOB = "events/*/*.parquet"
PATTERNS = ("email", "phone", "card")
DETECTOR = "search_text_looks_personal"
ALERT_KEY = "prod:usage-search-text"
DBT_WORK_SUFFIXES = ("__dbt_tmp", "__dbt_backup")

# The checkout: the project lives here, and DB and GEO_CACHE are relative to it (the tests point ROOT elsewhere).
ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / DBT_PROJECT


def dataset() -> str:
    """The usage dataset: COFFEEDATA_USAGE (a local folder, or hf://datasets/<owner>/<name>), else ./lake-usage."""
    return os.environ.get("COFFEEDATA_USAGE") or "lake-usage"


def sources(root: str) -> list[str]:
    """The raw event globs dbt reads: the dataset's, when it has any events (DuckDB fails on a glob with no match)."""
    if "://" in root:
        return [f"{root.rstrip('/')}/{EVENTS_GLOB}"]
    base = Path(root)
    return [str(base.resolve() / EVENTS_GLOB)] if any(base.glob(EVENTS_GLOB)) else []


EVENTS_KEY = "prod:usage-events"
# The newest stored event may be this many days old before the alert fires: dbt's own freshness warning
# (dbt_usage/tests/usage_events_are_fresh.sql; tests/test_usage_health.py ties the two). A warning only: a quiet
# few days on a small site is possible, and the quietest week isn't known until the collector has run for 8.
EVENTS_FRESH_DAYS = 3
_EVENT_FILE = re.compile(r"events/day=(\d{4}-\d{2}-\d{2})/[^/]+\.parquet")


class NotListed(Exception):
    """The usage dataset's files couldn't be listed, so nothing can be said about its events; the message says why
    and holds no token and no dataset name."""


def events_state(root: str | None = None) -> dict[str, Any]:
    """The usage dataset's stored event days, from its file names alone (events/day=<YYYY-MM-DD>/<file>.parquet;
    no file is opened): {"days": how many days have a file, "newest": the latest such day or None}. A local folder is
    globbed; an hf:// dataset is listed with HF_TOKEN. Raises NotListed when it can't be listed: no token, or a
    dataset the Hub calls "not found" (which it answers alike for a wrong name and a token that can't read it)."""
    root = root or dataset()
    if "://" not in root:
        base = Path(root)
        names = [p.relative_to(base).as_posix() for p in base.glob(EVENTS_GLOB)]
    else:
        token = os.environ.get("HF_TOKEN") or None
        if token is None:
            raise NotListed("no Hub token (HF_TOKEN): the usage dataset is private")
        from huggingface_hub import HfApi
        from huggingface_hub.errors import RepositoryNotFoundError

        from .importers import _hub

        repo = root.removeprefix("hf://datasets/").split("@")[0].strip("/")
        try:
            names = list(_hub(HfApi(token=token).list_repo_files, repo, repo_type="dataset"))
        except RepositoryNotFoundError:
            raise NotListed(
                'the Hub answered "not found": COFFEEDATA_USAGE names the wrong dataset, or the token can\'t read it'
            ) from None
    days = set()
    for name in names:
        if m := _EVENT_FILE.fullmatch(name):
            try:
                days.add(date.fromisoformat(m.group(1)))
            except ValueError:
                continue  # not a real date (a schema file's 0000-00-00): not a stored day
    return {"days": len(days), "newest": max(days).isoformat() if days else None}


def events_alert(state: dict[str, Any], today: str) -> dict:
    """The triage alert for the stored events: firing when the dataset holds no event at all, or its newest stored
    day is more than EVENTS_FRESH_DAYS before `today`. It says which, and carries a day and statuses only: never a
    count of events or visits (the repository is public)."""
    newest = state.get("newest")
    age = (date.fromisoformat(today) - date.fromisoformat(newest)).days if newest else None
    stale = age is not None and age > EVENTS_FRESH_DAYS
    if newest is None:
        summary = (
            "The usage dataset holds no event file: the collector has stored nothing. Either no page has sent it an "
            "event, or it refused or lost every one; its own log says which."
        )
    elif stale:
        summary = (
            f"The newest stored usage event is from {newest}, {age} days before this check (more than "
            f"{EVENTS_FRESH_DAYS}): the collector has stopped storing, or nobody has visited since."
        )
    else:
        summary = ""
    return {
        "key": EVENTS_KEY, "kind": "prod", "title": "usage: the collector isn't storing events",
        "severity": "warning", "firing": newest is None or stale, "summary": summary,
        "source": "src/coffeedata/usage_build.py",
        "checks": [
            {"name": "an event is stored", "status": "ok" if newest else "warning"},
            {"name": f"newest stored day within {EVENTS_FRESH_DAYS} days",
             "status": "warning" if newest is None or stale else "ok"},
        ],
        "context": {"newest stored day": newest or "none"},
        "steps": [
            "Read the collector's log in Cloud Logging (the `registry-coffee-usage` service; docs/app/RUNNING.md, "
            "Usage): its flush lines say whether requests arrived and why any were refused or lost. No request at "
            "all: check config/usage.toml's collector_url against the service's address, and the pages' CSP.",
            "Requests refused: the reason is in the line's fields (a foreign origin, no address). Events lost: the "
            "`usage_dropped` alert has emailed too; check the collector's Hub token.",
            "A quiet week needs nothing. This issue closes at the next check that finds an event stored within "
            f"{EVENTS_FRESH_DAYS} days (`gh workflow run usage-health` checks now).",
        ],
    }  # fmt: skip


def fetcher(root: str) -> Callable[[str, Path], None]:
    """How a locked geo file is copied into the cache: from a local dataset folder, or downloaded (read-only) from the
    Hub dataset."""
    if "://" not in root:

        def copy(name: str, dest: Path) -> None:
            src = Path(root) / name
            if not src.is_file():
                raise ValueError(f"{name} is not in the usage dataset at {root}")
            dest.write_bytes(src.read_bytes())

        return copy

    def download(name: str, dest: Path) -> None:
        from huggingface_hub import hf_hub_download

        repo = root.removeprefix("hf://datasets/").strip("/")
        got = hf_hub_download(
            repo_id=repo, filename=name, repo_type="dataset", local_dir=str(dest.parent / ".hub")
        )
        Path(got).replace(dest)

    return download


def models() -> set[str]:
    """The names of the usage project's models: the only tables its database may hold."""
    return {p.stem for p in (PROJECT / "models").rglob("*") if p.suffix in (".sql", ".py")}


def private_db() -> Path:
    """The private database's path, after checking that it isn't a link, a public build's database or inside a
    folder a public job uploads (ValueError otherwise)."""
    root = ROOT.resolve()
    db = root / DB
    for part in (Path(DB), *list(Path(DB).parents)[:-1]):
        if (root / part).is_symlink():
            raise ValueError(f"{root / part} is a link; the usage database is a file of its own, at {db}")
    bases = {root, Path.cwd().resolve()}
    named = (MAIN_DB, SERVED_DB, os.environ.get("COFFEEDATA_DB") or MAIN_DB)
    if db in {(b / p).resolve() for b in bases for p in named}:
        raise ValueError(f"{db} is a public build's database; the usage reports have their own")
    folders = [(b / f).resolve() for b in bases for f in PUBLIC_FOLDERS]
    if (export := os.environ.get("PUBLIC_EXPORT_URL")) and "://" not in export:
        folders.append(Path(export).resolve())
    if any(db.is_relative_to(f) for f in folders):
        raise ValueError(f"{db} is inside a folder a public job uploads")
    return db


def dbt_vars(
    root: str, lock: dict, local: dict[str, Path], catalog: dict[str, str] | None = None, today: str = ""
) -> dict:
    """The build's vars: the raw globs, the verified geo files and the lock, the served release's tables (optional)
    and the day of the build (for the freshness warning only)."""
    return {
        "usage_sources": sources(root),
        "geo_db": str(local[geo.DBIP].resolve()),
        "geo_cities": str(local[geo.CITIES].resolve()),
        "geo_lock": {
            k: {f: lock[k][f] for f in ("file", "release", "sha256")} for k in (geo.DBIP, geo.CITIES)
        },
        "catalog_roasters": (catalog or {}).get("dim_roaster", ""),
        "catalog_coffees": (catalog or {}).get("dim_coffee", ""),
        "today": today,
    }


def build(
    root: str | None = None,
    lock_path: Path = geo.LOCK,
    catalog: dict[str, str] | None = None,
    today: str = "",
    target: Path | None = None,
    log: Path | None = None,
    unit_tests: bool = False,
) -> bool:
    """Verify the locked geo files, then build dbt_usage/ into DB (replaced: derived, always rebuilt from raw).
    Refuses a database holding anything but this project's tables. Returns whether every model and test passed."""
    import duckdb

    root = root or dataset()
    lock = geo.read_lock(lock_path)
    local = geo.locate(
        lock, ROOT / GEO_CACHE, fetcher(root)
    )  # ValueError on a file that isn't the locked one
    db = private_db()
    if db.exists():
        with duckdb.connect(str(db), read_only=True) as con:
            tables = {
                t for (t,) in con.execute("select table_name from information_schema.tables").fetchall()
            }
        own = models()
        if foreign := {t for t in tables if t not in own and not any(
                t.endswith(s) and t[: -len(s)] in own for s in DBT_WORK_SUFFIXES)}:  # fmt: skip
            raise ValueError(
                f"{db} holds tables the usage project doesn't own ({', '.join(sorted(foreign))})"
            )
        db.unlink()
    db.with_name(db.name + ".wal").unlink(missing_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("COFFEEDATA_DB", "COFFEEDATA_IMPORTERS_DB")}
    env["COFFEEDATA_USAGE_DB"] = DB
    project = str(PROJECT.resolve())
    target = (target or PROJECT / "target").resolve()
    # dbt's output never goes to the console: a failing query's error can quote a value it read (a search), and CI's
    # log is public. It goes to a file under build/ (ignored, never uploaded) unless the caller names another.
    log = log or ROOT / DBT_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    variables = json.dumps(dbt_vars(root, lock, local, catalog, today))
    # Models first, then every test, as the main build does: a failing check (the detector above all) never stops a
    # report from being built. dbt's unit tests check the SQL over hand-written rows, so they run only when asked.
    # Each step's run_results.json is kept (run_results.<step>.json), so the counts cover both.
    units = [] if unit_tests else ["resource_type:unit_test"]
    steps = (("models", ["build", "--exclude", "resource_type:test", "resource_type:unit_test"]),
             ("tests", ["test", *(["--exclude", *units] if units else [])]))  # fmt: skip
    for name in (n for n, _ in steps):
        (target / f"run_results.{name}.json").unlink(missing_ok=True)
    passed = True
    for name, step in steps:
        (target / "run_results.json").unlink(missing_ok=True)
        cmd = ["dbt", *step, "--project-dir", project, "--profiles-dir", project,
               "--target-path", str(target), "--vars", variables]  # fmt: skip
        with log.open("a") as out:
            r = subprocess.run(cmd, env=env, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
        if (target / "run_results.json").exists():
            (target / "run_results.json").replace(target / f"run_results.{name}.json")
        passed = passed and r.returncode == 0
        if name == "models" and r.returncode != 0:
            # never leave a half-built database: the reports and the detector would read it as if it were whole
            db.unlink(missing_ok=True)
            db.with_name(db.name + ".wal").unlink(missing_ok=True)
            break
    return passed


def run_counts(target: Path) -> dict[str, dict[str, int]]:
    """How many of the build's nodes ended in each status, by kind (model, test, unit_test, ...), over both steps'
    run_results: counts only, never a node's message (a failing query's error can quote a value)."""
    counts: dict[str, dict[str, int]] = {}
    for name in ("models", "tests"):
        try:
            results = json.loads((target / f"run_results.{name}.json").read_text())["results"]
        except (OSError, ValueError, KeyError):
            continue
        for r in results:
            kind = str(r.get("unique_id", "")).split(".")[0] or "unknown"
            counts.setdefault(kind, {}).setdefault(str(r.get("status")), 0)
            counts[kind][str(r.get("status"))] += 1
    return counts


def detector_counts(db: Path, target: Path) -> list[tuple[str, str, int]]:
    """(week, pattern, searches) per week and pattern the detector matched, from its compiled SQL run against the
    built database: counts only, never a search's text (the query returns no text column)."""
    import duckdb

    compiled = next((target / "compiled").rglob(f"{DETECTOR}.sql"), None)
    if compiled is None:
        raise ValueError(f"no compiled {DETECTOR} in {target}")
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute(
            f"select cast(week as varchar), pattern, searches from ({compiled.read_text()})"
        ).fetchall()
    return sorted((str(w), str(p), int(n)) for w, p, n in rows)


def detector_alert(counts: list[tuple[str, str, int]] | None) -> dict:
    """The triage alert (coffeedata.triage prod) for the detector's counts: firing when any is above 0. Its checks
    are the three patterns, its summary "N searches in week W matched the <pattern> pattern"; no text, ever."""
    if counts is None:  # the models didn't build, so the detector never ran: say so, and keep the alert open
        return {
            "key": ALERT_KEY, "kind": "prod", "title": "usage: searches that look like personal data",
            "severity": "warning", "firing": True,
            "summary": "the usage build failed before the detector could run; nothing was measured",
            "source": f"{DBT_PROJECT}/tests/{DETECTOR}.sql",
            "checks": [{"name": "detector ran", "status": "warning"}],
            "steps": ["Read build/usage-dbt.log on the runner (never paste it anywhere public) and fix the build."],
        }  # fmt: skip
    lines = [f"{n} searches in week {w} matched the {p} pattern" for w, p, n in counts if n > 0]
    hit = {p for _w, p, n in counts if n > 0}
    return {
        "key": ALERT_KEY,
        "kind": "prod",
        "title": "usage: searches that look like personal data",
        "severity": "critical",
        "firing": bool(lines),
        "summary": "; ".join(lines) if lines else "no search matched a personal-data pattern",
        "source": f"{DBT_PROJECT}/tests/{DETECTOR}.sql",
        "checks": [{"name": f"{p} pattern", "status": "critical" if p in hit else "ok"} for p in PATTERNS],
        "steps": [
            "The searches are stored as typed (design section 3). Decide whether to scrub search text at collection; "
            "the counts above are all this issue will ever hold.",
            "Never paste a search's text here or anywhere public: read it, if needed, on a private copy of the usage "
            "dataset.",
        ],
    }
