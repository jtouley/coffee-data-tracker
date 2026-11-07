"""Draw every set's labeling sample from work/population.json: up to K[set][stratum] products per stratum, in a
seeded order over a coffee_id-sorted population (so a draw repeats), and write:

- samples_<set>.txt: the drawn coffee_ids (committed; the listings' text is the stores', so it stays out of git);
- preds_main.json and preds_guard.json: each arm's answer for every drawn product, written before any label exists
  (main removes every product here; the guard keeps the held ones as coffee);
- work/items_<set>.json: what the labelers read, blind (no stratum, no prediction, no verdict), shuffled.

Usage: python sample.py <EVAL_DIR>.
"""

import json
import random
import sys
from pathlib import Path

K = {"train": {"D": 100, "N": 40}, "validation": {"D": 100, "N": 40}, "test": {"D": 150, "N": 50}}
HERE = Path(__file__).parent


def main(eval_dir: str) -> None:
    work = Path(eval_dir) / "work"
    pop = sorted(json.loads((work / "population.json").read_text()), key=lambda r: r["coffee_id"])
    main_pred, guard_pred = {}, {}
    for name, ks in K.items():
        drawn = []
        for s, k in ks.items():
            group = [r for r in pop if r["set"] == name and r["stratum"] == s]
            random.Random(f"{name}-{s}").shuffle(group)
            drawn += group[:k]
        random.Random(name).shuffle(drawn)
        (HERE / f"samples_{name}.txt").write_text("".join(r["coffee_id"] + "\n" for r in drawn))
        items = [{"id": i, "coffee_id": r["coffee_id"], "roaster_name": r["roaster_name"], "title": r["title"],
                  "product_type": r["product_type"], "tags": r["tags"], "grams": r["grams"], "body": r["body"]}
                 for i, r in enumerate(drawn)]  # fmt: skip
        (work / f"items_{name}.json").write_text(json.dumps(items, indent=1, ensure_ascii=False))
        for r in drawn:
            main_pred[r["coffee_id"]] = "not_coffee"
            guard_pred[r["coffee_id"]] = "coffee" if r["held"] else "not_coffee"
        print(name, {s: sum(r["stratum"] == s for r in drawn) for s in ks})
    (HERE / "preds_main.json").write_text(json.dumps(main_pred, sort_keys=True, indent=0))
    (HERE / "preds_guard.json").write_text(json.dumps(guard_pred, sort_keys=True, indent=0))


if __name__ == "__main__":
    main(sys.argv[1])
