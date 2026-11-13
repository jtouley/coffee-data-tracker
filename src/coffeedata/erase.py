"""Deleting one person's data from the datasets (`coffeedata delete-user`), for a deletion request.

Their shots, bags, equipment, coffee ratings and answers to their picks (main lake), photos (photo dataset), and
account and invites (users dataset) are removed, every version of each record: one commit per dataset on Hugging
Face, or the files themselves in a local lake. The private marts that carry their rows (fct_shot, mart_bag) are rewritten without them in the same commit,
so the app can't load them back from the export on its next start. The account must be turned off in the app
first, so the running app refuses its writes until it restarts without it. The main and photo datasets' version
history still has the old commits; the privacy policy says so. The users dataset's history is squashed afterwards,
so the account's email (sealed, app/contacts.py) can't be read from an old revision either.

`--only-recs` deletes just the "Your next bag" test's records (ratings and answers, and the rec marts' rows) and
keeps the account, signed in and untouched, for someone who asks to delete their answers. It first records an
erasure (`recs_erasures` in the users dataset: who, when, and which rating ids) and publishes it; the running app
checks it on every rating and answer it reads or writes, so nothing of theirs from before it can be undone, resent
or merged back, and they take part again only by joining the test again. Then it deletes every file of their
ratings and answers, listed after the erasure was published, so one the app wrote meanwhile goes too.

Every remote dataset's folders must be listable: with a missing or expired token a private dataset answers 404, and
every table would look empty.

A table with no files in a source (one never published there) has nothing to delete and is listed in the plan as
such. Any other failure to list a source stops the plan with an error, before anything is deleted: an erasure never
skips a table it couldn't read.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import lake as lake_mod
from .lake import Lake, no_files

log = logging.getLogger(__name__)

# Per dataset: (table, its key, columns that name the person). Every file of a record goes once any version of it
# names the person: an invite's first version has no `used_by`, and keeping it would make the invite usable again.
Tables = list[tuple[str, str, tuple[str, ...]]]
# rec_responses has no single id: every row names its user, so the user is the key.
MAIN: Tables = [("shots", "shot_id", ("user_id",)), ("bags", "bag_id", ("user_id",)),
                ("equipment", "equipment_id", ("user_id",)), ("ratings", "rating_id", ("user_id",)),
                ("rec_responses", "user_id", ("user_id",))]  # fmt: skip
PHOTOS: Tables = [("photos", "photo_id", ("user_id",))]
USERS: Tables = [("users", "user_id", ("user_id",)), ("invites", "invite_id", ("for_user_id", "used_by")),
                 ("consents", "user_id", ("user_id",)), ("contacts", "user_id", ("user_id",)),
                 ("recs_erasures", "user_id", ("user_id",))]  # fmt: skip
# The recommendation test's tables and marts: what --only-recs deletes.
RECS: Tables = [t for t in MAIN if t[0] in ("ratings", "rec_responses")]
RECS_MARTS: list[str] = []  # dbt/models/recs/ marts exported to marts/ with a user_id column (none yet)
# Private exports with a user_id column, rewritten without the person's rows. tests/test_privacy.py fails when an
# exported mart built from personal data isn't here.
MARTS = ["fct_shot", "mart_bag", *RECS_MARTS]


# How long --only-recs waits after publishing the erasure before it lists and deletes: longer than an upload the app
# started before it saw the erasure can take (Lake.publish: 5 tries, 2-16 s apart, plus the requests), and than the
# app's reuse of a read of the erasures (app/store.py ERASURES_TTL). Every upload after it drops their records.
SETTLE = 180.0
ERASURES = (
    0  # erasures made by this process: an app in the same process (tests) never reuses a read from before
)


class EraseError(ValueError):
    pass


@dataclass
class Plan:
    user_id: str
    username: str
    files: dict[str, list[str]] = field(default_factory=dict)  # dataset url -> paths within it
    marts: list[str] = field(default_factory=list)  # marts in the main lake that hold the person's rows
    absent: list[str] = field(default_factory=list)  # sources with no files at all: nothing to delete there


def find_user(users: Lake, username: str, force: bool = False, only_recs: bool = False) -> tuple[str, str]:
    name = username.strip().lower()
    for row in users.latest("users", "user_id"):
        if row["username"] == name:
            if row.get("role") == "owner":
                raise EraseError(
                    "That's the owner's account. The owner's data is deleted with the datasets themselves."
                )
            if (
                not row.get("disabled") and not force and not only_recs
            ):  # --only-recs: the erasure marker instead
                raise EraseError(
                    f"Turn {name}'s account off in the app first (Invite someone → {name} → Turn off account), so "
                    "the running app refuses their writes. Then run this again."
                )
            return row["user_id"], name
    raise EraseError(f"No account named {name}.")


def _rows(lake: Lake, src: str, sql: str, params: dict, absent: list[str], missing=no_files) -> list[tuple]:
    """The rows, or none when the source has no files (`missing`), which is recorded in `absent`. Any other failure
    is an EraseError: the plan stops before anything is deleted."""
    try:
        return lake.connect().execute(sql.format(src=src), params).fetchall()
    except Exception as e:  # noqa: BLE001 - duckdb.IOException, or the network
        if missing(e):
            absent.append(src)
            return []
        raise EraseError(
            f"Couldn't read {src} ({type(e).__name__}: {str(e)[:200]}). Nothing was deleted: run it again."
        ) from e


def _mart_missing(e: Exception) -> bool:
    """A mart that was never exported: no local file, or a 404 for the file on the dataset."""
    return no_files(e) or "404" in str(e)


def _files(lake: Lake, tables: Tables, user_id: str, absent: list[str]) -> list[str]:
    """Every file (path within the dataset) of every record that any version of names the person."""
    out: set[str] = set()
    for table, key, cols in tables:
        where = " or ".join(f"{c} = $id" for c in cols)
        for src in lake.sources(table):
            scan = "read_parquet('{src}', union_by_name = true, hive_partitioning = false, filename = true)"
            rows = _rows(
                lake,
                src,
                f"select distinct filename from {scan} where {key} in (select {key} from {scan} where {where})",
                {"id": user_id},
                absent,
            )
            out |= {_relative(lake, name) for (name,) in rows}
    return sorted(out)


def _relative(lake: Lake, name: str) -> str:
    if lake.remote and name.startswith(lake.url + "/"):
        return name[len(lake.url) + 1 :]
    return Path(name).resolve().relative_to(lake.root.resolve()).as_posix()


def _marts(lake: Lake, user_id: str, marts: list[str], absent: list[str]) -> list[str]:
    sql = "select 1 from read_parquet('{src}') where user_id = $id limit 1"
    return [
        m
        for m in marts
        for src in lake.mart_sources(m)
        if _rows(lake, src, sql, {"id": user_id}, absent, _mart_missing)
    ]


def plan(
    username: str, main: Lake, photos: Lake, users: Lake, force: bool = False, only_recs: bool = False
) -> Plan:
    # The reads go with HF_TOKEN alone (Lake.connect), so that's what is checked, not a cached CLI login (S12).
    for lake in (main, users) if only_recs else (main, photos, users):
        if lake.remote and not os.environ.get("HF_TOKEN"):
            raise EraseError(f"HF_TOKEN isn't set, so {lake.url} would read as empty. Nothing was deleted.")
        if lake.remote and lake_mod.remote_folders(lake.url) is None:
            raise EraseError(
                f"Couldn't list {lake.url}'s folders (check HF_TOKEN). Every table would look empty, so nothing was "
                "deleted."
            )
    user_id, name = find_user(users, username, force, only_recs)
    p = Plan(user_id, name)
    p.marts = _marts(main, user_id, RECS_MARTS if only_recs else MARTS, p.absent)
    scope = ((main, RECS),) if only_recs else ((main, MAIN), (photos, PHOTOS), (users, USERS))
    for lake, tables in scope:
        p.files[lake.url] = _files(lake, tables, user_id, p.absent)
    return p


def apply(p: Plan, main: Lake, photos: Lake, users: Lake) -> None:
    """One commit per dataset (or plain file changes in a local lake). Staged copies go too."""
    with tempfile.TemporaryDirectory() as tmp:
        rewritten: dict[str, Path] = {}
        for m in p.marts:  # the export without the person's rows
            (src,) = main.mart_sources(m)
            out = Path(tmp) / f"{m}.parquet"
            main.connect().execute(
                f"copy (select * from read_parquet('{src}') where user_id is distinct from $id) to '{out}'"
                " (format parquet, compression zstd)",
                {"id": p.user_id},
            )
            rewritten[f"marts/{m}.parquet"] = out
        for lake in (main, photos, users):
            paths = p.files.get(lake.url) or []
            marts = rewritten if lake is main else {}
            if not paths and not marts:
                continue
            for rel in paths:
                (lake.root / rel).unlink(missing_ok=True)  # a staged copy, or the file itself in a local lake
            if not lake.remote:
                for rel, f in marts.items():
                    (lake.root / rel).write_bytes(f.read_bytes())
            else:
                from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

                for rel in marts:
                    (lake.root / rel).unlink(missing_ok=True)  # a stale local copy would win over the new one
                repo_id = lake.url.removeprefix("hf://datasets/").split("@")[0]
                api = HfApi()
                existing = (
                    {f.path for f in api.get_paths_info(repo_id, paths, repo_type="dataset")}
                    if paths
                    else set()
                )
                ops: list = [CommitOperationDelete(path_in_repo=rel) for rel in paths if rel in existing]
                ops += [
                    CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(f)) for rel, f in marts.items()
                ]
                if ops:
                    api.create_commit(
                        repo_id=repo_id,
                        repo_type="dataset",
                        operations=ops,
                        commit_message=f"Delete the data of user {p.user_id} (deletion request)",
                    )
            log.info("deleted %d files from %s, rewrote %d marts", len(paths), lake.url, len(marts))
            if lake is users and paths:  # nothing to do in a local lake, which has no history
                users.squash(f"Squash history after deleting user {p.user_id}")


def erase_recs(
    p: Plan, main: Lake, photos: Lake, users: Lake, now: datetime | None = None, settle: float | None = None
) -> Plan:
    """--only-recs: record the erasure and publish it, wait SETTLE (an upload the running app started before it
    saw the erasure lands meanwhile; every one after drops their records), then plan again and delete every file of
    theirs, staged copies included. Returns the plan carried out."""
    global ERASURES
    rows = _rows(main, "", f"select distinct rating_id from {main.scan('ratings')} where user_id = $id",
                 {"id": p.user_id}, [])  # fmt: skip
    # After every record of theirs, even one stamped ahead of the clock (each record of a key sorts after the last,
    # app/store.py _commit), and after their latest consent, so an earlier joining never counts as a rejoin.
    times = [
        t
        for lake, table in ((main, "ratings"), (main, "rec_responses"), (users, "consents"))
        for (t,) in _rows(
            lake,
            "",
            f"select max(logged_at) from {lake.scan(table)} where user_id = $id",
            {"id": p.user_id},
            [],
        )  # fmt: skip
        if t is not None
    ]
    at = max([(now or datetime.now(UTC)).replace(microsecond=0), *(t + timedelta(seconds=1) for t in times)])
    marker = {"user_id": p.user_id, "logged_at": at, "rating_ids": sorted(r for (r,) in rows if r)}
    users.write("recs_erasures", f"recs_erasures/{p.user_id}__{at:%Y%m%dT%H%M%S}.parquet", [marker])
    users.publish(f"Erase the recommendation-test records of user {p.user_id} (deletion request)")
    ERASURES += 1
    wait = SETTLE if settle is None else settle
    if wait:
        log.info("erasure recorded; waiting %.0f s for uploads already under way", wait)
        time.sleep(wait)
    done = Plan(p.user_id, p.username)
    done.marts = _marts(main, p.user_id, RECS_MARTS, done.absent)
    done.files[main.url] = _files(main, RECS, p.user_id, done.absent)
    apply(done, main, photos, users)
    return done
