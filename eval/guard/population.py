"""The guard's population: every product the classifier removed from the served release in one build (rule
verdict_flip, direction removed), with what a labeler reads and the guard's stratum, split by roaster.

Usage: python population.py <coffee.duckdb> <EVAL_DIR>. The duckdb is a build of the lake copy named in README.md,
with the served release set to the one before the change being measured (rules/verify in README). Writes
<EVAL_DIR>/work/population.json, sorted by coffee_id, so the same build gives the same file.

Strata: D, doubted (a guard.doubted_by flag: the guard keeps it), and N, not doubted (the guard removes it, as main
does). Split: sha256(SALT + roaster_id) read as a fraction: below 0.6 train, below 0.8 validation, else test, so one
roaster's products never sit on both sides.
"""

import hashlib
import json
import sys
from pathlib import Path

import duckdb

SALT = "guard-eval-2026-11-07-b"


def split(roaster_id: str) -> str:
    x = int(hashlib.sha256((SALT + roaster_id).encode()).hexdigest()[:15], 16) / 16**15
    return "train" if x < 0.6 else "validation" if x < 0.8 else "test"


def main(db: str, eval_dir: str) -> None:
    con = duckdb.connect(db, read_only=True)
    rows = con.execute(
        """
        select f.coffee_id, c.roaster_id, coalesce(r.roaster_name, c.roaster_id) as roaster_name, c.title,
               coalesce(c.product_type, '') as product_type, coalesce(c.tags, []) as tags,
               list_transform(c.variants, lambda v: v.grams) as grams, coalesce(i.body_text, '') as body,
               d.doubts > 0 as doubted, d.held
        from flag_verdict_flip f
        join int_catalog_candidates c using (coffee_id)
        join int_catalog_decided d using (coffee_id)
        left join stg_product_inputs i using (input_hash)
        left join stg_registry r on r.roaster_id = c.roaster_id
        where f.direction = 'removed'
        order by f.coffee_id
        """
    ).fetchall()
    cols = ("coffee_id", "roaster_id", "roaster_name", "title", "product_type", "tags", "grams", "body", "doubted",
            "held")  # fmt: skip
    out = []
    for r in rows:
        d = dict(zip(cols, r, strict=True))
        d["stratum"] = "D" if d.pop("doubted") else "N"
        d["set"] = split(d["roaster_id"])
        out.append(d)
    work = Path(eval_dir) / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "population.json").write_text(json.dumps(out, sort_keys=True, indent=0))
    counts: dict = {}
    for d in out:
        counts[(d["set"], d["stratum"])] = counts.get((d["set"], d["stratum"]), 0) + 1
    print(json.dumps({f"{s}/{t}": n for (s, t), n in sorted(counts.items())}))


if __name__ == "__main__":
    main(*sys.argv[1:3])
