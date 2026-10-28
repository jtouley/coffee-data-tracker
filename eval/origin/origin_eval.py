"""Origin-extraction evaluation on a local, read-only copy of the lake. See README.md for the protocol.

    python origin_eval.py build   <lake-dir> <data-dir>          the eval set: every coffee in marts/dim_coffee,
                                                                with its latest snapshot and description
    python origin_eval.py split   <data-dir>                    assigns train/validation/test by roaster
    python origin_eval.py predict <data-dir> <name>             runs the importable coffeedata's rules on every product
    python origin_eval.py compare <data-dir> <set> <a> <b> [--examples N]
                                                                how origins change from prediction a to b
    python origin_eval.py sample  <data-dir> <set> <pred>... <n> products to label: up to n per stratum
    python origin_eval.py show    <data-dir> <keys.json>        listings to label (title, tags, description)
    python origin_eval.py score   <data-dir> <set> <labels.json> <pred>...
                                                                agreement with the labels: Wilson 95% intervals
                                                                per stratum, stratified estimate for the set

The data dir holds the eval set, the split and the predictions; it stays out of git. Only train may be read
example by example while developing (compare --examples, show). The test set is scored once, with --final.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from collections import Counter
from pathlib import Path

SALT = "coffeedata-origin-eval-2026-10-28"
HERE = Path(__file__).parent


def bucket(roaster_id: str) -> str:
    """60% train, 20% validation, 20% test, by roaster, so one roaster's lots never sit on both sides."""
    n = int(hashlib.sha256(f"{SALT}:{roaster_id}".encode()).hexdigest()[:8], 16) % 100
    return "train" if n < 60 else "validation" if n < 80 else "test"


def build(lake: str, data: Path) -> None:
    import duckdb

    data.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect()
    c.execute(f"""
        copy (
            with latest as (
                select * from read_parquet('{lake}/snapshots/*/*.parquet', hive_partitioning=true, union_by_name=true)
                qualify row_number() over (partition by roaster_id, product_id order by run_date desc, fetched_at desc) = 1
            ),
            bodies as (select body_hash, body_text from read_parquet('{lake}/bodies/*/*.parquet', union_by_name=true))
            select d.coffee_id, d.roaster_id, l.title, l.tags, coalesce(b.body_text, '') as body_text
            from read_parquet('{lake}/marts/dim_coffee.parquet') d
            join latest l using (roaster_id, product_id)
            left join bodies b on b.body_hash = l.body_hash and l.body_hash <> ''
            order by d.coffee_id
        ) to '{data}/eval_set.parquet' (format parquet)
    """)
    print(c.execute(f"select count(*), count(distinct roaster_id) from '{data}/eval_set.parquet'").fetchone())


def rows(data: Path) -> list[dict]:
    import pyarrow.parquet as pq

    return pq.read_table(data / "eval_set.parquet").to_pylist()


def split(data: Path) -> None:
    sets = {r["coffee_id"]: bucket(r["roaster_id"]) for r in rows(data)}
    (data / "split.json").write_text(json.dumps(sets))
    roasters = Counter(bucket(r) for r in {r["roaster_id"] for r in rows(data)})
    print("products", dict(Counter(sets.values())), "roasters", dict(roasters))
    test = sorted(k for k, s in sets.items() if s == "test")
    (HERE / "test_keys.txt").write_text("\n".join(test) + "\n")


def predict(data: Path, name: str) -> None:
    import coffeedata
    from coffeedata.extract import rules
    from coffeedata.models import CatalogItem

    out = {}
    for r in rows(data):
        item = CatalogItem(
            roaster_id=r["roaster_id"],
            product_id=r["coffee_id"].split(":", 1)[1],
            handle="",
            url="",
            title=r["title"] or "",
            platform="",
            tags=list(r["tags"] or []),
            body_text=r["body_text"],
        )
        out[r["coffee_id"]] = rules(item).origin_country
    (data / f"pred_{name}.json").write_text(json.dumps(out))
    print(name, coffeedata.__file__, len(out), "with origin:", sum(v is not None for v in out.values()))


def _load(data: Path, name: str) -> dict:
    return json.loads((data / name).read_text())


def change(a: str | None, b: str | None) -> str:
    if a == b:
        return "unchanged"
    if a is None:
        return "none_to_country"
    return "country_to_none" if b is None else "country_to_other"


def _guard(set_name: str, final: bool) -> None:
    if set_name == "test" and not final:
        sys.exit("the test set is sealed: score it once, with --final, after the rules are frozen")


def compare(data: Path, set_name: str, a: str, b: str, examples: int, final: bool) -> None:
    _guard(set_name, final)
    if examples and set_name != "train":
        sys.exit("examples may only be read from train")
    sets, pa, pb = _load(data, "split.json"), _load(data, f"pred_{a}.json"), _load(data, f"pred_{b}.json")
    keys = [k for k, s in sets.items() if s == set_name]
    kinds = Counter(change(pa[k], pb[k]) for k in keys)
    print(f"{set_name}: {len(keys)} products;", dict(kinds))
    pairs = Counter((pa[k], pb[k]) for k in keys if pa[k] != pb[k])
    for (x, y), n in pairs.most_common(15):
        print(f"   {n:5}  {x} -> {y}")
    if examples:
        by = {r["coffee_id"]: r for r in rows(data)}
        rng = random.Random(SALT)
        for kind in ("none_to_country", "country_to_none", "country_to_other"):
            ks = [k for k in keys if change(pa[k], pb[k]) == kind]
            print(f"\n## {kind}: {len(ks)}")
            for k in rng.sample(ks, min(examples, len(ks))):
                r = by[k]
                print(f"- {k} | {r['title']!r} | {pa[k]} -> {pb[k]}\n    {r['body_text'][:300]!r}")


def stratum(preds: list[dict], k: str) -> str:
    """How the first and last prediction relate, and whether any prediction in between differs from the last."""
    kind = change(preds[0][k], preds[-1][k])
    return kind + ("+mid" if any(p[k] != preds[-1][k] for p in preds[1:-1]) else "")


def sample(data: Path, set_name: str, names: list[str], n: int) -> None:
    """Keys to label, chosen without reading them: up to n per stratum, by a seeded shuffle."""
    sets = _load(data, "split.json")
    preds = [_load(data, f"pred_{x}.json") for x in names]
    rng = random.Random(f"{SALT}:{set_name}")
    strata: dict[str, list[str]] = {}
    for k in sorted(k for k, s in sets.items() if s == set_name):
        strata.setdefault(stratum(preds, k), []).append(k)
    out = {st: rng.sample(ks, min(n, len(ks))) for st, ks in sorted(strata.items())}
    path = data / f"to_label_{set_name}.json"
    path.write_text(json.dumps(out, indent=1))
    print(path, {st: f"{len(v)}/{len(strata[st])}" for st, v in out.items()})


def show(data: Path, keys_file: Path) -> None:
    """The listings to label, shuffled and without any prediction, so labels can't follow one."""
    keys = [k for v in json.loads(keys_file.read_text()).values() for k in v]
    random.Random(SALT).shuffle(keys)
    by = {r["coffee_id"]: r for r in rows(data)}
    for k in keys:
        r = by[k]
        tags = ", ".join(t for t in (r["tags"] or []) if len(t) < 40)[:200]
        body = " ".join(r["body_text"].split())[:1200]
        print(f"### {k}\nTITLE: {r['title']}\nTAGS: {tags}\nBODY: {body}\n")


def wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def score(data: Path, set_name: str, labels_file: Path, names: list[str], final: bool) -> None:
    """Agreement with hand labels ("none": the listing names no single origin) and the wrong-country rate (a
    country that isn't the label; an empty origin is not wrong, it goes to Claude). Per stratum, and over the whole
    set by weighting each stratum by its size. Strata are by the first and last prediction (see stratum())."""
    _guard(set_name, final)
    sets = _load(data, "split.json")
    labels = {k: (None if v == "none" else v) for k, v in json.loads(labels_file.read_text()).items()}
    preds = {x: _load(data, f"pred_{x}.json") for x in names}
    plist = [preds[x] for x in names]
    keys = [k for k, s in sets.items() if s == set_name]
    size = Counter(stratum(plist, k) for k in keys)
    lab = [k for k in labels if sets.get(k) == set_name]
    print(f"{set_name}: {len(keys)} products, {len(lab)} labelled")
    for st in sorted(size):
        ks = [k for k in lab if stratum(plist, k) == st]
        print(f"  {st} ({size[st]} products, {len(ks)} labelled)")
        for x in names:
            right = sum(preds[x][k] == labels[k] for k in ks)
            wrong = sum(preds[x][k] is not None and preds[x][k] != labels[k] for k in ks)
            lo, hi = wilson(right, len(ks))
            wlo, whi = wilson(wrong, len(ks))
            print(
                f"     {x:6} agree {right:3}/{len(ks):<3} {right / max(1, len(ks)):6.1%} [{lo:5.1%}, {hi:5.1%}]   "
                f"wrong-country {wrong:3} {wrong / max(1, len(ks)):6.1%} [{wlo:5.1%}, {whi:5.1%}]"
            )
    for x in names:
        agree = wrong = var_a = var_w = 0.0
        for st, n in size.items():
            ks = [k for k in lab if stratum(plist, k) == st]
            if not ks:
                continue
            w = n / len(keys)
            pa = sum(preds[x][k] == labels[k] for k in ks) / len(ks)
            pw = sum(preds[x][k] is not None and preds[x][k] != labels[k] for k in ks) / len(ks)
            agree += w * pa
            wrong += w * pw
            # stratified variance, with a finite-population correction (some strata are labelled in full)
            fpc = (n - len(ks)) / max(1, n - 1)
            var_a += w * w * pa * (1 - pa) / len(ks) * fpc
            var_w += w * w * pw * (1 - pw) / len(ks) * fpc
        print(
            f"  whole set {x:6} agree {agree:6.1%} ± {1.96 * math.sqrt(var_a):.1%}   "
            f"wrong-country {wrong:6.1%} ± {1.96 * math.sqrt(var_w):.1%}"
        )
    # Paired: each rule set against the first, on the same products. Unchanged products add nothing, so this is
    # much tighter than the difference of the two whole-set estimates.
    base = preds[names[0]]
    for x in names[1:]:
        for what, hit in (
            ("agree", lambda p, k: p[k] == labels[k]),
            ("wrong-country", lambda p, k: p[k] is not None and p[k] != labels[k]),
        ):
            est = var = 0.0
            for st, n in size.items():
                ks = [k for k in lab if stratum(plist, k) == st]
                if not ks:
                    continue
                w = n / len(keys)
                d = [int(hit(preds[x], k)) - int(hit(base, k)) for k in ks]
                mean = sum(d) / len(d)
                sd2 = sum((v - mean) ** 2 for v in d) / max(1, len(d) - 1)
                est += w * mean
                var += w * w * sd2 / len(d) * (n - len(ks)) / max(1, n - 1)
            print(f"  paired {x} - {names[0]}: {what} {est:+.1%} ± {1.96 * math.sqrt(var):.1%}")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    final = "--final" in args
    args = [x for x in args if x != "--final"]
    if cmd == "build":
        build(args[0], Path(args[1]))
    elif cmd == "split":
        split(Path(args[0]))
    elif cmd == "predict":
        predict(Path(args[0]), args[1])
    elif cmd == "compare":
        n = int(args[args.index("--examples") + 1]) if "--examples" in args else 0
        compare(Path(args[0]), args[1], args[2], args[3], n, final)
    elif cmd == "sample":
        sample(Path(args[0]), args[1], args[2:-1], int(args[-1]))
    elif cmd == "show":
        show(Path(args[0]), Path(args[1]))
    elif cmd == "score":
        score(Path(args[0]), args[1], Path(args[2]), args[3:], final)
