"""Deleting one person's data from the datasets (`coffeedata delete-user`), for a deletion request.

Their shots, bags and equipment (main lake), photos (photo dataset), and account and invites (users dataset) are
removed, every version of each record: one commit per dataset on Hugging Face, or the files themselves in a local
lake. The private marts that carry their rows (fct_shot, mart_bag) are rewritten without them in the same commit,
so the app can't load them back from the export on its next start. The account must be turned off in the app
first, so the running app refuses its writes until it restarts without it. The datasets' version history still
has the old commits; the privacy policy says so.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .lake import Lake, no_files

log = logging.getLogger(__name__)

# Per dataset: (table, its key, columns that name the person). Every file of a record goes once any version of it
# names the person: an invite's first version has no `used_by`, and keeping it would make the invite usable again.
Tables = list[tuple[str, str, tuple[str, ...]]]
MAIN: Tables = [("shots", "shot_id", ("user_id",)), ("bags", "bag_id", ("user_id",)),
                ("equipment", "equipment_id", ("user_id",))]  # fmt: skip
PHOTOS: Tables = [("photos", "photo_id", ("user_id",))]
USERS: Tables = [("users", "user_id", ("user_id",)), ("invites", "invite_id", ("for_user_id", "used_by"))]
MARTS = ["fct_shot", "mart_bag"]  # private exports with a user_id column, rewritten without the person's rows


class EraseError(ValueError):
    pass


@dataclass
class Plan:
    user_id: str
    username: str
    files: dict[str, list[str]] = field(default_factory=dict)  # dataset url -> paths within it
    marts: list[str] = field(default_factory=list)  # marts in the main lake that hold the person's rows


def find_user(users: Lake, username: str, force: bool = False) -> tuple[str, str]:
    name = username.strip().lower()
    for row in users.latest("users", "user_id"):
        if row["username"] == name:
            if row.get("role") == "owner":
                raise EraseError(
                    "That's the owner's account. The owner's data is deleted with the datasets themselves."
                )
            if not row.get("disabled") and not force:
                raise EraseError(
                    f"Turn {name}'s account off in the app first (Invite someone → {name} → Turn off account), so "
                    "the running app refuses their writes. Then run this again."
                )
            return row["user_id"], name
    raise EraseError(f"No account named {name}.")


def _rows(lake: Lake, src: str, sql: str, params: dict) -> list[tuple]:
    try:
        return lake.connect().execute(sql.format(src=src), params).fetchall()
    except Exception as e:  # noqa: BLE001 - duckdb.IOException: no files in this source
        if no_files(e):
            return []
        raise


def _files(lake: Lake, tables: Tables, user_id: str) -> list[str]:
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
            )
            out |= {_relative(lake, name) for (name,) in rows}
    return sorted(out)


def _relative(lake: Lake, name: str) -> str:
    if lake.remote and name.startswith(lake.url + "/"):
        return name[len(lake.url) + 1 :]
    return Path(name).resolve().relative_to(lake.root.resolve()).as_posix()


def _marts(lake: Lake, user_id: str) -> list[str]:
    return [
        m
        for m in MARTS
        for src in lake.mart_sources(m)
        if _rows(
            lake, src, "select 1 from read_parquet('{src}') where user_id = $id limit 1", {"id": user_id}
        )
    ]


def plan(username: str, main: Lake, photos: Lake, users: Lake, force: bool = False) -> Plan:
    user_id, name = find_user(users, username, force)
    p = Plan(user_id, name, marts=_marts(main, user_id))
    for lake, tables in ((main, MAIN), (photos, PHOTOS), (users, USERS)):
        p.files[lake.url] = _files(lake, tables, user_id)
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
