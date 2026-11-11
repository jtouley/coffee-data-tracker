"""Rebuild the private inputs of the accuracy floors from pinned, read-only revisions of the lake.

    python eval/floor_inputs.py <out-dir> [--github-env <file>]

The floors' labels are in git; the listings and catalogs they score are private lake data, so they aren't. This
reads them from the lake (`hf://datasets/jtouley/coffee-data`) at the exact revisions the evals were measured on,
rebuilds each eval's input the way its own script does, and checks every file against the sha256 recorded here, so a
floor never scores a different input than the one it was measured on. It only downloads (huggingface_hub
`snapshot_download`, with the HF_TOKEN already in the environment): nothing is written to the Hub, and the raw
download is deleted once the inputs are built.

It writes, under <out-dir>:
- wholesale/work/population.json   (eval/wholesale/population.py, lake at df57f62)    -> WHOLESALE_EVAL_DIR
- origin/listings.json             (the labelled train and validation listings of eval/origin, lake at df57f62;
                                    the sealed test listings are left out)              -> ORIGIN_EVAL_DIR
- match/lake/marts/dim_coffee.parquet  (eval/match's catalog, lake at df57f62)         -> MATCH_EVAL_DIR
- dq/dim_coffee.parquet, dq/dim_roaster.parquet  (release 20261103T203218-956abd, the build eval/dq replayed;
                                    lake at b3bf5e8)                                    -> DQ_EVAL_DIR

With --github-env, it appends those four variables to the file (GitHub Actions' $GITHUB_ENV). Any failure (no
token, a revision gone, a checksum that differs) exits non-zero: CI fails rather than skipping a floor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("floor_inputs")

HERE = Path(__file__).parent
LAKE_REPO = "jtouley/coffee-data"
# The lake when eval/wholesale, eval/origin and eval/match were measured (2026-10-28 run; dim_coffee 44,293 coffees).
LAKE_1028 = "df57f62dce023d6d1bf030620ed131751f979935"
# The lake serving release 20261103T203218-956abd, the 11-02 build eval/dq replayed (marts/release.parquet 6a84bfa4).
LAKE_1103 = "b3bf5e8c5c064ec335d184b9389518e85788fc75"
DOWNLOADS = {
    LAKE_1028: ["marts/dim_coffee.parquet", "snapshots/*", "bodies/*"],
    LAKE_1103: ["marts/dim_coffee.parquet", "marts/dim_roaster.parquet"],
}
SHA256 = {
    # eval/wholesale/SHA256SUMS pins the same file
    "wholesale/work/population.json": "b11b86ec9edfd6e69ab7ca48d7a58f0b383a023522be3f8c6864dc65cf939a2e",
    "origin/listings.json": "864a1f43d0023fa78ac6be11a0798ff482eb7909a41b08b85cc327aa37412eac",
    # eval/match/README.md pins the same file
    "match/lake/marts/dim_coffee.parquet": "a87c87420a78b3c5a75b83d9f63424361ff8c9131a915a87f4253ad1c46b4292",
    "dq/dim_coffee.parquet": "7ee066d63b1ba99a807a8809fd86894e896eb44b147c401d52f9e4609976eb4b",
    "dq/dim_roaster.parquet": "1cd6b700a68148b81e48b578de428e5dd16b2da0161d5b4c6fb8040ac459695b",
}
ENV = {
    "WHOLESALE_EVAL_DIR": "wholesale",
    "ORIGIN_EVAL_DIR": "origin",
    "MATCH_EVAL_DIR": "match",
    "DQ_EVAL_DIR": "dq",
}
ATTEMPTS = 4


def _transient(e: BaseException) -> bool:
    """Worth asking again: a network error, or the Hub answering 408, 429 or 5xx. A refused token, a missing repo or
    revision (404), or a bug in this script won't change on a retry."""
    import httpx
    from huggingface_hub.errors import HfHubHTTPError

    if isinstance(e, HfHubHTTPError):
        status = getattr(e.response, "status_code", None)
        return status in (408, 429) or (status is not None and status >= 500)
    return isinstance(e, httpx.TransportError | ConnectionError | TimeoutError)


def download(
    revision: str,
    into: Path,
    get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """One pinned revision's files, read-only, retried with backoff on transient errors (network, 429, 5xx)."""
    if get is None:
        from huggingface_hub import snapshot_download as get
    for attempt in range(1, ATTEMPTS + 1):
        try:
            get(
                repo_id=LAKE_REPO,
                repo_type="dataset",
                revision=revision,
                allow_patterns=DOWNLOADS[revision],
                local_dir=str(into),
            )
            return into
        except Exception as e:
            if not _transient(e) or attempt == ATTEMPTS:
                raise
            wait = 2.0**attempt
            log.warning("download of %s failed (%s); retrying in %.0fs", revision[:7], type(e).__name__, wait)
            sleep(wait)
    raise AssertionError("unreachable")


def _origin_listings(lake: Path, out: Path) -> None:
    """eval/origin's eval set (origin_eval.build), kept to the labelled train and validation keys."""
    import duckdb

    keys = sorted(
        k
        for s in ("train", "validation")
        for k in json.loads((HERE / "origin" / f"labels_{s}.json").read_text())
    )
    con = duckdb.connect()
    con.execute("create table keys as select unnest(?::varchar[]) as coffee_id", [keys])
    rows = con.execute(f"""
        with latest as (
            select * from read_parquet('{lake}/snapshots/*/*.parquet', hive_partitioning=true, union_by_name=true)
            qualify row_number() over (partition by roaster_id, product_id order by run_date desc, fetched_at desc) = 1
        ),
        bodies as (select body_hash, body_text from read_parquet('{lake}/bodies/*/*.parquet', union_by_name=true))
        select d.coffee_id, d.roaster_id, l.title, l.tags, coalesce(b.body_text, '') as body_text
        from read_parquet('{lake}/marts/dim_coffee.parquet') d
        join keys using (coffee_id)
        join latest l using (roaster_id, product_id)
        left join bodies b on b.body_hash = l.body_hash and l.body_hash <> ''
        order by d.coffee_id
    """).fetchall()
    doc = {cid: {"roaster_id": rid, "title": t, "tags": list(tags or []), "body_text": body}
           for cid, rid, t, tags, body in rows}  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, sort_keys=True, ensure_ascii=False) + "\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(out: Path, get: Callable[..., Any] | None = None) -> dict[str, Path]:
    """Download, rebuild and check every input; returns {env var: folder}."""
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        raw = {rev: download(rev, Path(tmp) / rev[:7], get) for rev in DOWNLOADS}
        a, b = raw[LAKE_1028], raw[LAKE_1103]
        pop = out / "wholesale" / "work" / "population.json"
        pop.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, str(HERE / "wholesale" / "population.py"), str(a), str(pop)],
            check=True,
            cwd=HERE / "wholesale",
        )
        _origin_listings(a, out / "origin" / "listings.json")
        (out / "match" / "lake" / "marts").mkdir(parents=True, exist_ok=True)
        shutil.copy(
            a / "marts" / "dim_coffee.parquet", out / "match" / "lake" / "marts" / "dim_coffee.parquet"
        )
        (out / "dq").mkdir(parents=True, exist_ok=True)
        for t in ("dim_coffee", "dim_roaster"):
            shutil.copy(b / "marts" / f"{t}.parquet", out / "dq" / f"{t}.parquet")
    bad = [
        f"{rel}: {sha256(out / rel)} != {want}" for rel, want in SHA256.items() if sha256(out / rel) != want
    ]
    if bad:
        raise SystemExit("eval inputs differ from the measured ones:\n  " + "\n  ".join(bad))
    return {var: out / sub for var, sub in ENV.items()}


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log.setLevel(logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--github-env", type=Path, help="append VAR=path lines here ($GITHUB_ENV)")
    a = ap.parse_args()
    dirs = build(a.out.resolve())
    for var, path in dirs.items():
        print(f"{var}={path}")
    if a.github_env:
        with a.github_env.open("a") as f:
            f.writelines(f"{var}={path}\n" for var, path in dirs.items())


if __name__ == "__main__":
    main()
