"""Draw the train, validation and sealed test samples from a read-only copy of the lake.

  python build_sets.py <lake copy> <main's predictions.json> <data dir> [--exclude ids.json]

main's predictions ({item_key: [is_coffee, ...]} for every product, from predict_all.py) only assign the strata.
Deterministic: the same lake copy, predictions and salt give the same files. README.md has the rule.
It reads no label and prints only counts. It also writes manifest/<set>.tsv here: each drawn product's id and the
first 16 hex digits of its salted key hash, so make_blind.py can rebuild the labelers' files without predictions.
"""

import json
import sys
from pathlib import Path

import duckdb
from common import CUR, PREV, SETS, h, products, release, set_of, write_blind

# Products drawn per stratum and set (all of a stratum when it has fewer).
TAKE = {"kept": 100, "dropped_reclassified": 150, "dropped_other": 100, "moved_179": 40, "moved_182": 40}
WOO_FALLBACK = {"martinhenrycoffee.com"}  # the store #182 reads from its pages
FIRST_ID = {"train": 10000, "validation": 20000, "test": 30000}


def main() -> None:
    lake, pred_path, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    exclude = set(json.loads(Path(sys.argv[5]).read_text())) if "--exclude" in sys.argv else set()
    pred = json.loads(Path(pred_path).read_text())
    c = duckdb.connect()
    shopify_hosted = {r for (r,) in c.execute(
        f"select roaster_id from '{release(lake, CUR, 'dim_roaster')}' where platform = 'shopify?'").fetchall()}  # fmt: skip
    reclassified = {k for (k,) in c.execute(
        f"select coffee_id from '{release(lake, PREV, 'public_coffee')}' where coffee_id not in "
        f"(select coffee_id from '{release(lake, CUR, 'public_coffee')}') and roaster_id in "
        f"(select roaster_id from '{release(lake, CUR, 'public_roaster')}')").fetchall()}  # fmt: skip

    strata: dict[tuple[str, str], list[dict]] = {}
    roasters: dict[str, set[str]] = {s: set() for s in SETS}
    for r in products(lake):
        key = r["item_key"]
        if r["roaster_id"] in shopify_hosted and r["platform"] == "generic":
            stratum = "moved_179"
        elif r["roaster_id"] in WOO_FALLBACK:
            stratum = "moved_182"
        elif pred[key][0]:
            stratum = "kept"
        else:
            stratum = "dropped_reclassified" if key in reclassified else "dropped_other"
        r["stratum"] = stratum
        name = set_of(r["roaster_id"])
        roasters[name].add(r["roaster_id"])
        strata.setdefault((name, stratum), []).append(r)

    manifest = Path(__file__).parent / "manifest"
    manifest.mkdir(exist_ok=True)
    for name in SETS:
        population, items = {}, []
        for stratum, take in TAKE.items():
            members = strata.get((name, stratum), [])
            population[stratum] = len(members)
            eligible = sorted(
                (m for m in members if m["item_key"] not in exclude), key=lambda m: h(m["item_key"])
            )
            items += eligible[:take]
        # Ids in an order unrelated to the stratum, so a labeler can't read it from the position.
        items.sort(key=lambda m: h("order:" + m["item_key"]))
        for i, m in enumerate(items, 1):
            m["id"] = FIRST_ID[name] + i
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "sample.json").write_text(json.dumps(
            {"salt": h(""), "set": name, "population": population, "items": items}, ensure_ascii=False, default=str))  # fmt: skip
        write_blind(items, d / "blind.json")
        (manifest / f"{name}.tsv").write_text("".join(f"{m['id']}\t{h(m['item_key'])[:16]}\n" for m in items))
        drawn = {s: sum(m["stratum"] == s for m in items) for s in TAKE}
        print(f"{name:10} roasters={len(roasters[name])} population={population} drawn={drawn}")


if __name__ == "__main__":
    main()
