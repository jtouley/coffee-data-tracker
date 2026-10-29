"""Build the eval population from a lake: every listed coffee in its dim_coffee export, with its latest snapshot's
title, product type, tags and bag sizes, the rule parts (title, product_type, tag) that flag it under C0, and its
split. Strata come from C0, the rule the samples were drawn under. Usage: python population.py <lake folder> <EVAL_DIR>/work/population.json"""

import hashlib
import json
import sys
from pathlib import Path

import duckdb
from rules import C0_TAG, C0_TEXT

SALT = "wholesale-eval-2026-10-28"


def split(roaster: str) -> str:
    """Grouped by roaster: sha256(SALT + roaster_id) as a fraction, <0.6 train, <0.8 validation, else test."""
    h = int(hashlib.sha256((SALT + roaster).encode()).hexdigest()[:12], 16) / 16**12
    return "train" if h < 0.6 else "validation" if h < 0.8 else "test"


def main(lake: str, out: str) -> None:
    rows = (
        duckdb.connect()
        .execute(f"""
    with s as (select * from read_parquet('{lake}/snapshots/**/*.parquet', union_by_name=true, hive_partitioning=false)
               qualify row_number() over (partition by roaster_id, product_id order by fetched_at desc) = 1)
    select c.coffee_id, c.roaster_id, s.title, coalesce(s.product_type, ''), coalesce(s.tags, []),
           list_transform(s.variants, lambda v: v.grams)
    from '{lake}/marts/dim_coffee.parquet' c
    join s on s.roaster_id = c.roaster_id and c.coffee_id = s.roaster_id || ':' || s.product_id
    where c.is_listed
    order by c.coffee_id""")  # a fixed order, so the seeded draws in sample.py repeat
        .fetchall()
    )
    pop = []
    for cid, rid, title, ptype, tags, grams in rows:
        parts = [p for p, hit in (("title", C0_TEXT.search(title or "")),
                                  ("product_type", C0_TEXT.search(ptype)),
                                  ("tag", any(C0_TAG.fullmatch(t.strip()) for t in tags))) if hit]  # fmt: skip
        pop.append({"coffee_id": cid, "roaster_id": rid, "title": title, "product_type": ptype, "tags": tags,
                    "grams": [g for g in grams if g], "parts": parts, "flagged": bool(parts), "set": split(rid)})  # fmt: skip
    Path(out).write_text(json.dumps(pop))


if __name__ == "__main__":
    main(*sys.argv[1:3])
