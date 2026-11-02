"""Export the facts-only public tables (dbt models `public_*`) for release.

Destinations:
  * a local directory (default build/public)
  * hf://datasets/<owner>/<name>: a separate, public Hugging Face dataset (one commit per export)
  * s3://bucket/prefix or gs://bucket/prefix: object storage that Snowflake and Databricks load from.
    S3 uses DuckDB's credential chain (AWS env vars, profile or instance role); GCS uses HMAC keys from
    GCS_HMAC_KEY_ID / GCS_HMAC_SECRET.

Current tables are replaced each week (they are derived); history lives in `public_price_event`. A directory or
Hugging Face export writes `manifest.json`, which says which release it is (build id and run date), the contract
version and data licence, and each file's sha256 and row count, next to the licence text (LICENSE-DATA) when the
repository has it; an s3:// or gs:// export writes only the Parquet files. A Hugging Face export of a release build
also tags its commit `release-<run date>`, so a past release stays retrievable by name
(`hf://datasets/<owner>/<name>@release-2026-11-02/...`). A build without release ids (a plain `dbt build`) is never
tagged.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PUBLIC_TABLES = ["public_coffee", "public_roaster", "public_price_event", "public_market_weekly"]
# The order each file is written in, ending in the table's key so it is total: the same release always gives the
# same file, and readers of public_price_event can skip row groups by run_date.
ORDER = {
    "public_coffee": "coffee_id",
    "public_roaster": "roaster_id",
    "public_price_event": "run_date, coffee_id, variant_id, event",
    "public_market_weekly": "coffees desc, origin_country, process",  # the biggest markets first, as the model
}
CARD = Path("docs/PUBLIC_DATASET_CARD.md")
HF_DATA_DIR = "data"  # where the tables go in a Hugging Face export
# The public ODCS contract (the repository's copy; from the working directory when the package is installed elsewhere).
CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "public-catalog.odcs.yaml"
# The data licence (LICENSE-DATA at the repository root); the code is MIT (LICENSE). CDLA-Permissive-2.0 asks that
# the licence text travel with the data, so a directory or Hugging Face export carries the file when it exists.
LICENSE_FILE = Path(__file__).resolve().parents[2] / "LICENSE-DATA"
DATA_LICENSE = {
    "id": "CDLA-Permissive-2.0",
    "url": "https://github.com/jtouley/coffee-data-tracker/blob/main/LICENSE-DATA",
}


def contract(path: Path | None = None) -> dict:
    """The public contract, parsed."""
    import yaml

    path = path or (CONTRACT if CONTRACT.exists() else Path("contracts/public-catalog.odcs.yaml"))
    return yaml.safe_load(path.read_text())


def release(con) -> dict:
    """Which release a built database holds: its build id and run date (null in a plain `dbt build`), from the
    release columns every public table carries."""
    row = con.execute("select max(release_build_id), max(release_run_date) from public_roaster").fetchone()
    build_id, run_date = row if row else (None, None)
    return {"release_build_id": build_id, "release_run_date": str(run_date) if run_date else None}


def describe(con) -> dict:
    """What every copy of a release says about itself: the release ids, the contract and the data licence."""
    c = contract()
    return release(con) | {
        "contract": {"id": c["id"], "version": str(c["version"])},
        "license": DATA_LICENSE,
    }


def license_file() -> Path | None:
    """LICENSE-DATA (the repository's copy, else the working directory's), or None where there isn't one."""
    return next((p for p in (LICENSE_FILE, Path("LICENSE-DATA")) if p.is_file()), None)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hf_repo_id(url: str) -> str:
    """`hf://datasets/<owner>/<name>[@rev][/path]` -> `<owner>/<name>`."""
    return "/".join(url.removeprefix("hf://datasets/").split("@")[0].strip("/").split("/")[:2])


def export(db: Path, dest: str = "build/public") -> dict:
    import duckdb

    con = duckdb.connect(str(db), read_only=True)

    def scalar(sql: str):
        row = con.execute(sql).fetchone()
        return row[0] if row else None

    counts = {t: scalar(f"select count(*) from {t}") for t in PUBLIC_TABLES}
    as_of = scalar("select max(last_seen) from public_coffee")
    manifest = describe(con) | {
        "as_of_run": str(as_of) if as_of else None,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tables": counts,
    }

    if dest.startswith(("s3://", "gs://")):
        _configure_object_store(con, dest)
        for t in PUBLIC_TABLES:
            con.execute(
                f"COPY (select * from {t} order by {ORDER[t]}) TO '{dest.rstrip('/')}/{t}.parquet' "
                "(FORMAT parquet, COMPRESSION zstd)"
            )
        return manifest | {"dest": dest}

    local = Path("build/public") if dest.startswith("hf://") else Path(dest)
    local.mkdir(parents=True, exist_ok=True)
    for t in PUBLIC_TABLES:
        con.execute(
            f"COPY (select * from {t} order by {ORDER[t]}) TO '{local / (t + '.parquet')}' "
            "(FORMAT parquet, COMPRESSION zstd)"
        )
    con.close()
    manifest["files"] = {
        f"{t}.parquet": {"sha256": _sha256(local / f"{t}.parquet"), "rows": counts[t]} for t in PUBLIC_TABLES
    }
    (local / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    if lic := license_file():
        (local / "LICENSE-DATA").write_bytes(lic.read_bytes())
    else:
        (local / "LICENSE-DATA").unlink(missing_ok=True)  # not left over from an export that had one
    if dest.startswith("hf://"):
        manifest["tag"] = _upload_hf(local, dest, manifest)
    return manifest | {"dest": dest}


def _configure_object_store(con, dest: str) -> None:
    con.execute("INSTALL httpfs; LOAD httpfs;")
    if dest.startswith("s3://"):
        con.execute("INSTALL aws; LOAD aws;")
        con.execute("CREATE OR REPLACE SECRET pub (TYPE s3, PROVIDER credential_chain)")
    else:
        con.execute(
            "CREATE OR REPLACE SECRET pub (TYPE gcs, KEY_ID ?, SECRET ?)",
            [os.environ["GCS_HMAC_KEY_ID"], os.environ["GCS_HMAC_SECRET"]],
        )


def release_tag(manifest: dict) -> str | None:
    """The Hugging Face tag for a release: `release-<run date>` (one release is served per weekly run). Only a release
    build has one: without a release_build_id and release_run_date (a plain `dbt build`) there is no tag."""
    run = manifest.get("release_run_date")
    return f"release-{str(run)[:10]}" if run and manifest.get("release_build_id") else None


def _tagged_build(api, repo_id: str, tag: str) -> str | None:
    """The release_build_id in manifest.json at `tag`, or None when the tagged commit has no manifest or no build id."""
    from huggingface_hub.errors import RemoteEntryNotFoundError

    try:
        path = api.hf_hub_download(repo_id, "manifest.json", repo_type="dataset", revision=tag)
    except RemoteEntryNotFoundError:  # only a commit without the file; any other failure is raised
        return None
    return json.loads(Path(path).read_text()).get("release_build_id")


def _upload_hf(local: Path, dest: str, manifest: dict) -> str | None:
    """One commit with the files, the manifest and the card, then the release tag on it. Returns the tag.

    The tag names the release served for that run date, so the last export of a run wins: when `release-<run date>`
    already exists for another build (a corrected build of the same run), it is moved to this commit. A re-export of
    the same build leaves it where it is (its files are the same). The commit the tag left keeps its build id in its
    message and manifest."""
    from huggingface_hub import CommitOperationAdd, HfApi

    repo_id = hf_repo_id(dest)
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    api.create_repo(repo_id, repo_type="dataset", private=False, exist_ok=True)
    ops = [
        CommitOperationAdd(path_in_repo=f"{HF_DATA_DIR}/{p.name}", path_or_fileobj=str(p))
        for p in sorted(local.glob("*.parquet"))
    ]
    ops.append(CommitOperationAdd(path_in_repo="manifest.json", path_or_fileobj=str(local / "manifest.json")))
    if CARD.exists():
        ops.append(CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=str(CARD)))
    if (local / "LICENSE-DATA").is_file():
        ops.append(
            CommitOperationAdd(path_in_repo="LICENSE-DATA", path_or_fileobj=str(local / "LICENSE-DATA"))
        )
    build = manifest.get("release_build_id")
    commit = api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=ops,
        commit_message=f"weekly public export (run {manifest['as_of_run']}"
        + (f", build {build})" if build else ")"),
    )
    tag = release_tag(manifest)
    if tag is None:
        return None
    if tag in {t.name for t in api.list_repo_refs(repo_id, repo_type="dataset").tags}:
        tagged = _tagged_build(api, repo_id, tag)
        if tagged == build:
            print(f"{repo_id}: {tag} is already on build {build}; left as it is", file=sys.stderr)
            return tag
        print(f"{repo_id}: moving {tag} from build {tagged} to build {build}", file=sys.stderr)
        api.delete_tag(repo_id, tag=tag, repo_type="dataset")
    api.create_tag(
        repo_id,
        tag=tag,
        tag_message=f"release {build}",
        revision=getattr(commit, "oid", None),
        repo_type="dataset",
        exist_ok=True,
    )
    return tag
