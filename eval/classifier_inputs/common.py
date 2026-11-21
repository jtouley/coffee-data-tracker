"""Shared by the scripts here: the split rule, the lake read and the labelers' view of a product."""

import hashlib
import json
import textwrap
from pathlib import Path

import duckdb

SALT = "classifier-inputs-2026-11-20"
# Roasters named in issue #190's examples: always train, never validation or test.
TRAIN_ONLY = {
    "moongoat.com", "drinkcoffeedostuff.com", "kaldiscoffee.com", "coffeekinginc.com", "mlcrc.com",
    "javamania.com", "jbccoffeeroasters.com",
}  # fmt: skip
PREV, CUR = "2026-11-09", "2026-11-16"  # the two releases the dropped_reclassified stratum compares
SETS = ("train", "validation", "test")
LAKE_FILES = [
    "snapshots/*",
    "bodies/*",
    "verdicts/*",
    f"releases/run_date={PREV}/*",
    f"releases/run_date={CUR}/*",
]


def h(text: str) -> str:
    return hashlib.sha256(f"{SALT}:{text}".encode()).hexdigest()


def set_of(roaster_id: str) -> str:
    """train 60%, validation 20%, test 20%, by roaster: every product of a roaster is in one set."""
    if roaster_id in TRAIN_ONLY:
        return "train"
    bucket = int(h(roaster_id)[:8], 16) % 100
    return "train" if bucket < 60 else "validation" if bucket < 80 else "test"


# What a labeler's copy holds: the products and the roasters' names, without the stored verdicts.
LABELER_FILES = ["snapshots/*", "bodies/*", f"releases/run_date={CUR}/*/dim_roaster.parquet"]


def copy_lake(repo: str, dest: str, files: list[str] = LAKE_FILES) -> None:
    """A local copy of the tables the evaluation reads. Reads only; make the folder read-only afterwards."""
    from huggingface_hub import snapshot_download

    snapshot_download(repo, repo_type="dataset", local_dir=dest, allow_patterns=files)


def release(lake: str, run_date: str, table: str) -> str:
    (path,) = Path(lake, "releases", f"run_date={run_date}").glob(f"build=*/{table}.parquet")
    return str(path)


def products(lake: str) -> list[dict]:
    """One row per product (roaster_id:product_id): its latest stored version, with its description."""
    c = duckdb.connect()
    cur = c.execute(f"""
        with s as (
          select roaster_id, product_id, roaster_id || ':' || product_id as item_key, platform, title, product_type,
                 tags, body_hash, list_transform(variants, lambda v: v.grams) as grams,
                 md5(to_json(struct_pack(s.title, s.product_type, s.tags, s.body_hash,
                                         grams := list_transform(s.variants, lambda v: v.grams)))) as input_hash
          from read_parquet('{lake}/snapshots/*/*.parquet', union_by_name=true, hive_partitioning=true) s
          where run_date between '2026-10-26' and '{CUR}'
          qualify row_number() over (partition by roaster_id, product_id
                                     order by run_date desc, fetched_at desc, batch_id desc) = 1),
        b as (select body_hash, any_value(body_text) as body_text
              from read_parquet('{lake}/bodies/*/*.parquet', union_by_name=true) group by 1)
        select s.*, b.body_text, coalesce(r.roaster_name, s.roaster_id) as roaster_name
        from s left join b using (body_hash)
        left join '{release(lake, CUR, "dim_roaster")}' r using (roaster_id)""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, t, strict=True)) for t in cur.fetchall()]


def blind(m: dict) -> dict:
    """What a labeler sees: no stratum, no prediction, no verdict, no key."""
    return {"id": m["id"], "roaster_name": m["roaster_name"], "title": m["title"],
            "product_type": m["product_type"], "tags": m["tags"], "grams": m["grams"],
            "body": (m["body_text"] or "")[:1500]}  # fmt: skip


def blind_text(m: dict) -> str:
    """The same, as lines a person or a model reads top to bottom (long values wrapped)."""
    b = blind(m)
    fields = {"id": b["id"], "roaster": b["roaster_name"], "title": b["title"], "product_type": b["product_type"],
              "tags": ", ".join(b["tags"] or []), "bag sizes in grams": ", ".join(str(g) for g in b["grams"] or [] if g),
              "description": " ".join(b["body"].split())}  # fmt: skip
    return "\n".join(textwrap.fill(f"{k}: {v}", 1000, subsequent_indent="  ") for k, v in fields.items())


def write_blind(items: list[dict], path: Path) -> None:
    path.write_text(json.dumps([blind(m) for m in items], ensure_ascii=False))
