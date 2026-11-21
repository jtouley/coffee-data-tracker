"""Draw the next sealed test set, for the change after this one (issue #190), from the test roasters.

  python draw_next_test.py <lake copy> <main's predictions.json> <data dir> --exclude <earlier.json>

Same roaster split as build_sets.py, so no train or validation roaster is in it. Left out of the draw, and of the
population it weights by: every product used in an earlier evaluation, which is the item keys in <earlier.json>
(the 1,796 of tests/fixtures/classifier_labels.jsonl and eval/sealed/sample.json) and every product of this
evaluation's train, validation and test samples (manifest/<set>.tsv). The full list is written, sorted, to
<data dir>/test_next/exclude.json, and its sha256 is printed for the README. A separate salt orders the draw. The
strata follow issue #190: products with no stored verdict that the rules drop get their own stratum.
Writes <data dir>/test_next/{sample,blind,exclude}.json and manifest/test_next.tsv. It reads no label and prints
counts and the hash only.
"""

import hashlib
import json
import sys
from pathlib import Path

import duckdb
from common import CUR, PREV, SETS, h, products, release, set_of, write_blind

NEXT = "next-test-1"
TAKE = {"kept": 100, "dropped_reclassified": 100, "dropped_no_verdict": 150, "dropped_other": 100}


def main() -> None:
    args = sys.argv[1:]
    if len(args) != 5 or args[3] != "--exclude":
        sys.exit(__doc__)
    lake, pred_path, out = args[0], args[1], Path(args[2]) / "test_next"
    earlier = set(json.loads(Path(args[4]).read_text()))
    pred = json.loads(
        Path(pred_path).read_text()
    )  # {item_key: [is_coffee, by, excluded, rules_only, verdict]}
    here = Path(__file__).parent
    used = {
        line.split("\t")[1]
        for name in SETS
        for line in (here / "manifest" / f"{name}.tsv").read_text().splitlines()
    }
    left_out: set[str] = set()
    reclassified = {k for (k,) in duckdb.connect().execute(
        f"select coffee_id from '{release(lake, PREV, 'public_coffee')}' where coffee_id not in "
        f"(select coffee_id from '{release(lake, CUR, 'public_coffee')}') and roaster_id in "
        f"(select roaster_id from '{release(lake, CUR, 'public_roaster')}')").fetchall()}  # fmt: skip
    strata: dict[str, list[dict]] = {s: [] for s in TAKE}
    for r in products(lake):
        key = r["item_key"]
        if key in earlier or h(key)[:16] in used:
            left_out.add(key)
            continue
        if set_of(r["roaster_id"]) != "test":
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
    found = sum(h(k)[:16] in used for k in left_out)
    if found != len(used):
        sys.exit(
            f"{found} of {len(used)} products of manifest/<set>.tsv found; the lake differs from the sample's copy"
        )
    left_out |= earlier
    exclude_bytes = json.dumps(sorted(left_out)).encode()
    (out / "exclude.json").write_bytes(exclude_bytes)
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
    print(
        f"excluded {len(left_out):,} products; exclude.json sha256 {hashlib.sha256(exclude_bytes).hexdigest()}"
    )


if __name__ == "__main__":
    main()
