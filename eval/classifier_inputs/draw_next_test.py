"""Draw the next sealed test set, for the change after this one (issue #190), from the test roasters.

  python draw_next_test.py <lake copy> <main's predictions.json> <data dir>

Same roaster split as build_sets.py, so no train or validation roaster is in it. The products of the test set
scored on 2026-11-20 (manifest/test.tsv) are left out: that set is spent. A separate salt orders the draw. The
strata follow issue #190: products with no stored verdict that the rules drop get their own stratum.
Writes <data dir>/test_next/{sample,blind}.json and manifest/test_next.tsv. It reads no label and prints counts.
"""

import json
import sys
from pathlib import Path

import duckdb
from common import CUR, PREV, h, products, release, set_of, write_blind

NEXT = "next-test-1"
TAKE = {"kept": 100, "dropped_reclassified": 100, "dropped_no_verdict": 150, "dropped_other": 100}


def main() -> None:
    lake, pred_path, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]) / "test_next"
    pred = json.loads(
        Path(pred_path).read_text()
    )  # {item_key: [is_coffee, by, excluded, rules_only, verdict]}
    here = Path(__file__).parent
    spent = {line.split("\t")[1] for line in (here / "manifest" / "test.tsv").read_text().splitlines()}
    reclassified = {k for (k,) in duckdb.connect().execute(
        f"select coffee_id from '{release(lake, PREV, 'public_coffee')}' where coffee_id not in "
        f"(select coffee_id from '{release(lake, CUR, 'public_coffee')}') and roaster_id in "
        f"(select roaster_id from '{release(lake, CUR, 'public_roaster')}')").fetchall()}  # fmt: skip
    strata: dict[str, list[dict]] = {s: [] for s in TAKE}
    for r in products(lake):
        key = r["item_key"]
        if set_of(r["roaster_id"]) != "test" or h(key)[:16] in spent:
            continue
        is_coffee, _, excluded, _, verdict = pred[key]
        if is_coffee:
            stratum = "kept"
        elif key in reclassified:
            stratum = "dropped_reclassified"
        else:
            stratum = "dropped_no_verdict" if verdict is None and not excluded else "dropped_other"
        strata[stratum].append(r | {"stratum": stratum})
    items = []
    for stratum, take in TAKE.items():
        items += sorted(strata[stratum], key=lambda m: h(f"{NEXT}:{m['item_key']}"))[:take]
    items.sort(key=lambda m: h(f"{NEXT}:order:{m['item_key']}"))
    for i, m in enumerate(items, 1):
        m["id"] = 40000 + i
    out.mkdir(parents=True, exist_ok=True)
    population = {s: len(ms) for s, ms in strata.items()}
    (out / "sample.json").write_text(json.dumps(
        {"set": "test_next", "population": population, "items": items}, ensure_ascii=False, default=str))  # fmt: skip
    write_blind(items, out / "blind.json")
    (here / "manifest" / "test_next.tsv").write_text(
        "".join(f"{m['id']}\t{h(m['item_key'])[:16]}\n" for m in items)
    )
    print(
        f"test_next population={population} drawn={ {s: sum(m['stratum'] == s for m in items) for s in TAKE} }"
    )


if __name__ == "__main__":
    main()
