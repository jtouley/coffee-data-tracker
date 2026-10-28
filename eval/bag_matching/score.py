"""Bag matching against the person's own confirmed bags: how often the matcher picks the bag's coffee_id.

Usage: python score.py <lake folder> <dim_coffee parquet> [--final]

Each bag in the lake's `bags` table with a coffee_id is a case: its typed text (match_coffee) and, when it has a
photo read, the read label (match_bag). Split by sha256(SALT + the true coffee's roaster) into train 60%,
validation 20% and test 20%, so one roaster's bags never sit on both sides. Prints counts and, per set, matches
with Wilson 95% intervals. The test set is scored only with --final, once, after a matching change is frozen.

Use the dim_coffee export the app reads: the matcher's 300-candidate prefilter breaks ties by row order, so a
rebuilt export with the same rows in another order can score differently.
"""

import hashlib
import json
import math
import sys
import tempfile

import duckdb

from coffeedata import shots
from coffeedata.lake import Lake

SALT = "bag-matching-eval-2026-10-28"
MIN_PER_SET = 30  # below this a set's score isn't printed: too few bags to say anything


def split(roaster: str) -> str:
    h = int(hashlib.sha256((SALT + roaster).encode()).hexdigest()[:12], 16) / 16**12
    return "train" if h < 0.6 else "validation" if h < 0.8 else "test"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def main(lake_dir: str, export: str, final: bool = False) -> None:
    lake = Lake(tempfile.mkdtemp())
    lake.write_mart("dim_coffee", duckdb.connect(), f"select * from '{export}'")
    bags = (
        duckdb.connect()
        .execute(f"""
        select coffee_id, coffee_freetext, read_json, split_part(coffee_id, ':', 1) as roaster
        from read_parquet('{lake_dir}/bags/*.parquet', union_by_name = true)
        qualify row_number() over (partition by bag_id order by logged_at desc) = 1
        """)
        .fetchall()
    )
    bags = [b for b in bags if b[0]]
    print(f"bags with a coffee_id: {len(bags)}")
    for name in ("train", "validation", "test"):
        cases = [b for b in bags if split(b[3]) == name]
        hits = n = 0
        for cid, text, read, _ in cases:
            if text:
                n += 1
                hits += shots.match_coffee(text, lake)[0] == cid
            if read:
                label = json.loads(read)
                n += 1
                hits += shots.match_bag(label.get("roaster"), label.get("coffee"), lake)[0] == cid
        if name == "test" and not final:
            print(f"test: {len(cases)} bags (sealed: scored only with --final)")
        elif n < MIN_PER_SET:
            print(f"{name}: {len(cases)} bags, {n} inputs (fewer than {MIN_PER_SET}: not scored)")
        else:
            lo, hi = wilson(hits, n)
            print(f"{name}: {hits}/{n} inputs matched, {hits / n:.1%} [{lo:.1%}, {hi:.1%}]")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--final" in sys.argv)
