"""The usage reports' build: the private dbt project dbt_usage/ over the private usage dataset, into its own
database file, build/usage.duckdb (design sections 0, 3-6; docs/usage/README.md).

Like the importers' derived view (importers.py), it is a project and a database of their own: the main build
(dbt/), the release, the public tables and their contracts, the site, the public export, Snowflake and the MCP
server never read either (tests/test_usage_dbt.py). The raw events and the pinned geo files are only read.

Before dbt runs, the geo files config/geo.lock names are found in the cache (build/geo/) or copied from the usage
dataset, and each one's sha256 is checked: a file that isn't the locked one stops the build. dbt then reads exactly
those files, and ops_usage_build records the lock. Same raw plus same lock gives the same rows.

The detector (dbt_usage/tests/search_text_looks_personal.sql) fails the build when any search looks like an email,
phone or card number. `detector_alert` turns its counts into a triage alert that says "N searches in week W matched
the email pattern" and nothing more: the issue is in a public repository, so it never holds the text.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import geo
from .importers import MAIN_DB, PUBLIC_FOLDERS, SERVED_DB

DBT_PROJECT = "dbt_usage"
# The private database's one name and place, relative to the checkout; the project refuses any other
# (dbt_usage/macros/private_target.sql).
DB = "build/usage.duckdb"
# Where the locked geo files are kept between builds (not committed: build/ is ignored).
GEO_CACHE = "build/geo"
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
    where = ["--target-path", str(target.resolve())] if target else []
    variables = json.dumps(dbt_vars(root, lock, local, catalog, today))
    # Models first, then every test, as the main build does: a failing check (the detector above all) never stops a
    # report from being built. dbt's unit tests check the SQL over hand-written rows, so they run only when asked.
    units = [] if unit_tests else ["resource_type:unit_test"]
    steps = (["build", "--exclude", "resource_type:test", "resource_type:unit_test"],
             ["test", *(["--exclude", *units] if units else [])])  # fmt: skip
    passed = True
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("")
    for step in steps:
        cmd = ["dbt", *step, "--project-dir", project, "--profiles-dir", project, *where, "--vars", variables]
        if log is None:
            r = subprocess.run(cmd, env=env, cwd=ROOT)
        else:
            with log.open("a") as out:
                r = subprocess.run(cmd, env=env, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
        passed = passed and r.returncode == 0
    return passed


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


def detector_alert(counts: list[tuple[str, str, int]]) -> dict:
    """The triage alert (coffeedata.triage prod) for the detector's counts: firing when any is above 0. Its checks
    are the three patterns, its summary "N searches in week W matched the <pattern> pattern"; no text, ever."""
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
