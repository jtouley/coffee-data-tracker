"""Deleting one person's data from the datasets (`coffeedata delete-user`), for a deletion request.

Their shots, bags and equipment (main lake), photos (photo dataset), and account and invites (users dataset) are
removed: one commit per dataset on Hugging Face, or the files themselves in a local lake. The datasets' version
history still has the old commits; the privacy policy says so. The weekly export (marts/fct_shot.parquet) drops
their shots at the next weekly run. The running app keeps what it loaded in memory until it restarts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .lake import Lake

log = logging.getLogger(__name__)

# Per dataset: (table, columns that name the person).
Tables = list[tuple[str, tuple[str, ...]]]
MAIN: Tables = [("shots", ("user_id",)), ("bags", ("user_id",)), ("equipment", ("user_id",))]
PHOTOS: Tables = [("photos", ("user_id",))]
USERS: Tables = [("users", ("user_id",)), ("invites", ("for_user_id", "used_by"))]


class EraseError(ValueError):
    pass


@dataclass
class Plan:
    user_id: str
    username: str
    files: dict[str, list[str]] = field(default_factory=dict)  # dataset url -> paths within it


def find_user(users: Lake, username: str) -> tuple[str, str]:
    name = username.strip().lower()
    for row in users.latest("users", "user_id"):
        if row["username"] == name:
            if row.get("role") == "owner":
                raise EraseError(
                    "That's the owner's account. The owner's data is deleted with the datasets themselves."
                )
            return row["user_id"], name
    raise EraseError(f"No account named {name}.")


def _files(lake: Lake, tables: Tables, user_id: str) -> list[str]:
    """Files (paths within the dataset) holding any row that names the person."""
    con = lake.connect()
    out: set[str] = set()
    for table, cols in tables:
        where = " or ".join(f"{c} = $id" for c in cols)
        for src in lake.sources(table):
            try:
                rows = con.execute(
                    f"select distinct filename from read_parquet('{src}', union_by_name = true,"
                    f" hive_partitioning = false, filename = true) where {where}",
                    {"id": user_id},
                ).fetchall()
            except Exception as e:  # noqa: BLE001 - duckdb.IOException: no files in this source
                if "No files found" not in str(e):
                    raise
                continue
            for (name,) in rows:
                out.add(_relative(lake, name))
    return sorted(out)


def _relative(lake: Lake, name: str) -> str:
    if lake.remote and name.startswith(lake.url + "/"):
        return name[len(lake.url) + 1 :]
    return Path(name).resolve().relative_to(lake.root.resolve()).as_posix()


def plan(username: str, main: Lake, photos: Lake, users: Lake) -> Plan:
    user_id, name = find_user(users, username)
    p = Plan(user_id, name)
    for lake, tables in ((main, MAIN), (photos, PHOTOS), (users, USERS)):
        p.files[lake.url] = _files(lake, tables, user_id)
    return p


def apply(p: Plan, main: Lake, photos: Lake, users: Lake) -> None:
    """One commit per dataset (or plain deletes in a local lake). Staged copies go too."""
    for lake in (main, photos, users):
        paths = p.files.get(lake.url) or []
        if not paths:
            continue
        for rel in paths:
            (lake.root / rel).unlink(missing_ok=True)  # a staged copy, or the file itself in a local lake
        if lake.remote:
            from huggingface_hub import CommitOperationDelete, HfApi

            repo_id = lake.url.removeprefix("hf://datasets/").split("@")[0]
            api = HfApi()
            existing = {f.path for f in api.get_paths_info(repo_id, paths, repo_type="dataset")}
            ops = [CommitOperationDelete(path_in_repo=rel) for rel in paths if rel in existing]
            if ops:
                api.create_commit(
                    repo_id=repo_id,
                    repo_type="dataset",
                    operations=ops,
                    commit_message=f"Delete the data of user {p.user_id} (deletion request)",
                )
        log.info("deleted %d files from %s", len(paths), lake.url)
