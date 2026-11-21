"""Process-extraction evaluation on a local, read-only copy of the lake. See README.md for the protocol.

    python process_eval.py build   <lake-dir> <data-dir>     every coffee in marts/dim_coffee with its latest
                                                             snapshot (title, tags, variant titles) and description
    python process_eval.py split   <data-dir>                train/validation/test by a salted hash of the roaster
    python process_eval.py predict <data-dir> <name>         the importable coffeedata's rules on every product
    python process_eval.py compare <data-dir> <set> <a> <b> [--examples N]
                                                             how processes change from a to b (examples: train only)
    python process_eval.py diag    <data-dir> <n>            the diagnosis sample: n per platform, train only, of the
                                                             single-origin coffees in stock with no process
    python process_eval.py sample  <data-dir> <set> <a> <b> <n>
                                                             up to n per stratum of the listed catalog, unread
    python process_eval.py show    <data-dir> <keys.json>    the labelling view, shuffled, no prediction shown
    python process_eval.py score   <data-dir> <set> <labels.json> <a> <b> [--strata a,b] [--final for test]
    python process_eval.py packet  <data-dir> <set> <out.txt>   the blind labelling packet for a set's sample
    python process_eval.py settle  <A.json> <B.json> <out.json> the keys A and B disagree on, for the third labeller
    python process_eval.py merge   <A.json> <B.json> <C.json> <out.json>
                                                             the reference: A where A and B agree, else C

The data dir holds the eval set, the split and the predictions; it stays out of git. Only train may be read
example by example while developing. The test set is scored once, with --final, and prints only totals.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path

SALT = "coffeedata-process-eval-2026-11-21"
HERE = Path(__file__).parent
# The labels (GUIDE.md). "none" and "multiple" both mean the right answer is no process (left to Claude).
LABELS = ("washed", "natural", "honey", "anaerobic", "other", "none", "multiple")
# The population the site's chart and the catalog read: the listed retail catalog (site.LISTED), chains left out.
LISTED = "is_listed and catalog_status <> 'archived' and not is_wholesale and not coalesce(is_chain, false)"

# The labelling view shows every passage that could state a process: deliberately much wider than any rule, so a
# label never depends on a rule's vocabulary. Words, not categories.
VOCAB = re.compile(
    r"process|washed|washing|wash\b|natural|honey|anaerob|carbonic|macerat|ferment|lactic|yeast|koji|"
    r"pulped|semi|hulled|giling|basah|dry[- ]?process|sun[- ]?dried|dried|raised bed|patio|wet[- ]?process|"
    r"lavado|natural|mel\b|miel|infus|thermal|experimental|cherry|mucilage|depulp|oxygen|tank|"
    r"processo|proceso|traitement|aufbereitung",
    re.I,
)


def bucket(roaster_id: str) -> str:
    """60% train, 20% validation, 20% test, by roaster, so one roaster's products never sit on both sides."""
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
            bodies as (select body_hash, any_value(body_text) as body_text
                       from read_parquet('{lake}/bodies/*/*.parquet', union_by_name=true) group by 1),
            llm as (
                select roaster_id, product_id, bool_or(process is not null) as any_llm_process
                from read_parquet('{lake}/enrichments/**/*.parquet', hive_partitioning=true, union_by_name=true)
                where method = 'llm' group by all
            )
            select d.coffee_id, d.roaster_id, d.platform, d.title, l.tags, l.product_type,
                   list_transform(l.variants, lambda v: v.title) as variant_titles,
                   list_transform(l.variants, lambda v: v.grams) as grams,
                   coalesce(b.body_text, '') as body_text,
                   ({LISTED}) as listed, d.in_stock, d.is_blend, d.origin_country, d.process as dim_process,
                   d.enrichment_method, coalesce(llm.any_llm_process, false) as any_llm_process
            from read_parquet('{lake}/marts/dim_coffee.parquet') d
            join latest l using (roaster_id, product_id)
            left join bodies b on b.body_hash = l.body_hash and l.body_hash <> ''
            left join llm using (roaster_id, product_id)
            order by d.coffee_id
        ) to '{data}/eval_set.parquet' (format parquet)
    """)
    print(
        c.execute(
            f"select count(*), count(distinct roaster_id), count(*) filter (where listed) "
            f"from '{data}/eval_set.parquet'"
        ).fetchone()
    )


def rows(data: Path) -> list[dict]:
    import pyarrow.parquet as pq

    return pq.read_table(data / "eval_set.parquet").to_pylist()


def split(data: Path) -> None:
    rs = rows(data)
    sets = {r["coffee_id"]: bucket(r["roaster_id"]) for r in rs}
    (data / "split.json").write_text(json.dumps(sets))
    roasters = Counter(bucket(r) for r in {r["roaster_id"] for r in rs})
    print("products", dict(Counter(sets.values())), "roasters", dict(roasters))
    print("listed", dict(Counter(sets[r["coffee_id"]] for r in rs if r["listed"])))
    test = sorted(k for k, s in sets.items() if s == "test")
    # JSON, not one key a line: a few keys end in a space, which a whitespace hook would strip.
    (HERE / "test_keys.json").write_text(json.dumps(test, indent=0) + "\n")


def item(r: dict):
    from coffeedata.models import CatalogItem, Variant

    return CatalogItem(
        roaster_id=r["roaster_id"],
        product_id=r["coffee_id"].split(":", 1)[1],
        handle="",
        url="",
        title=r["title"] or "",
        platform="",
        product_type=r["product_type"] or "",
        tags=list(r["tags"] or []),
        variants=[Variant(variant_id=str(i), grams=g) for i, g in enumerate(r["grams"] or [])],
        body_text=r["body_text"],
    )


def predict(data: Path, name: str) -> None:
    import coffeedata
    from coffeedata.extract import rules

    out = {r["coffee_id"]: rules(item(r)).process for r in rows(data)}
    (data / f"pred_{name}.json").write_text(json.dumps(out, sort_keys=True))
    print(name, coffeedata.__file__, len(out), "with process:", sum(v is not None for v in out.values()))


def _load(data: Path, name: str) -> dict:
    return json.loads((data / name).read_text())


def change(a: str | None, b: str | None) -> str:
    if a == b:
        return "same_none" if a is None else "same_process"
    if a is None:
        return "none_to_process"
    return "process_to_none" if b is None else "process_to_other"


def _guard(set_name: str, final: bool) -> None:
    if set_name == "test" and not final:
        sys.exit("the test set is sealed: score it once, with --final, after the rules are frozen")


def _listed(data: Path) -> set[str]:
    return {r["coffee_id"] for r in rows(data) if r["listed"]}


def compare(data: Path, set_name: str, a: str, b: str, examples: int, final: bool) -> None:
    _guard(set_name, final)
    if examples and set_name != "train":
        sys.exit("examples may only be read from train")
    sets, pa, pb = _load(data, "split.json"), _load(data, f"pred_{a}.json"), _load(data, f"pred_{b}.json")
    listed = _listed(data)
    keys = [k for k, s in sets.items() if s == set_name and k in listed]
    print(f"{set_name}: {len(keys)} listed products;", dict(Counter(change(pa[k], pb[k]) for k in keys)))
    print("  a:", dict(Counter(pa[k] for k in keys)), "\n  b:", dict(Counter(pb[k] for k in keys)))
    pairs = Counter((pa[k], pb[k]) for k in keys if pa[k] != pb[k])
    for (x, y), n in pairs.most_common(20):
        print(f"   {n:5}  {x} -> {y}")
    if examples:
        by = {r["coffee_id"]: r for r in rows(data)}
        rng = random.Random(SALT)
        for kind in ("none_to_process", "process_to_none", "process_to_other"):
            ks = [k for k in keys if change(pa[k], pb[k]) == kind]
            print(f"\n## {kind}: {len(ks)}")
            for k in rng.sample(ks, min(examples, len(ks))):
                print(f"- {k} {pa[k]} -> {pb[k]}")
                print(view(by[k], width=90))


def _order(key: str, tag: str) -> str:
    return hashlib.sha256(f"{SALT}:{tag}:{key}".encode()).hexdigest()


def diag(data: Path, n: int) -> None:
    """The diagnosis sample: the chart's "not stated" coffees (single-origin, listed, in stock, no process in
    dim_coffee), train roasters only, n per platform, first by a salted hash of the product key."""
    sets = _load(data, "split.json")
    pool: dict[str, list[str]] = {}
    for r in rows(data):
        if (
            r["listed"]
            and r["in_stock"]
            and not r["is_blend"]
            and r["origin_country"]
            and not r["dim_process"]
            and sets[r["coffee_id"]] == "train"
        ):
            pool.setdefault(r["platform"], []).append(r["coffee_id"])
    out = {p: sorted(ks, key=lambda k: _order(k, "diag"))[:n] for p, ks in sorted(pool.items())}
    (data / "diag_keys.json").write_text(json.dumps(out, indent=1))
    print({p: f"{len(v)}/{len(pool[p])}" for p, v in out.items()})


def sample(data: Path, set_name: str, a: str, b: str, n: int) -> None:
    """Keys to label, chosen without reading them: up to n per stratum of the listed catalog, by a salted hash."""
    sets, pa, pb = _load(data, "split.json"), _load(data, f"pred_{a}.json"), _load(data, f"pred_{b}.json")
    listed = _listed(data)
    strata: dict[str, list[str]] = {}
    for k in sorted(k for k, s in sets.items() if s == set_name and k in listed):
        strata.setdefault(change(pa[k], pb[k]), []).append(k)
    out = {st: sorted(ks, key=lambda k: _order(k, set_name))[:n] for st, ks in sorted(strata.items())}
    path = data / f"to_label_{set_name}.json"
    path.write_text(json.dumps(out, indent=1))
    print(path, {st: f"{len(v)}/{len(strata[st])}" for st, v in out.items()})


def view(r: dict, width: int = 160) -> str:
    """Title, tags, variant titles, the top of the description, and every passage that could state a process."""
    body = " ".join((r["body_text"] or "").split())
    tags = ", ".join(t for t in (r["tags"] or []) if len(t) < 60)[:400]
    variants = sorted({v for v in (r["variant_titles"] or []) if v and v.lower() != "default title"})
    hits, last = [], -1
    for m in VOCAB.finditer(body):
        if m.start() < 500 or m.start() < last:
            continue
        s, e = max(0, m.start() - width), min(len(body), m.end() + width)
        hits.append(body[s:e])
        last = e
    lines = [
        f"TITLE: {r['title']}",
        f"TAGS: {tags}",
        f"VARIANTS: {' | '.join(variants)[:300]}",
        f"BODY[:500]: {body[:500]}",
    ]
    lines += [f"  ...{h}..." for h in hits[:8]]
    return "\n".join(lines)


def show(data: Path, keys_file: Path) -> None:
    """The listings to label, shuffled and without any prediction, so labels can't follow one."""
    raw = json.loads(keys_file.read_text())
    keys = sorted({k for v in raw.values() for k in v}) if isinstance(raw, dict) else sorted(raw)
    keys.sort(key=lambda k: _order(k, "show"))
    by = {r["coffee_id"]: r for r in rows(data)}
    for k in keys:
        print(f"### {k}\n{view(by[k])}\n")


def wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def right(p: str | None, label: str) -> bool:
    return p == label or (p is None and label in ("none", "multiple"))


def wrong(p: str | None, label: str) -> bool:
    """Confidently wrong: a process that isn't the label (an empty process is never wrong; it goes to Claude)."""
    return p is not None and p != label


def found(p: str | None, label: str) -> bool:
    """Coverage: the listing states one process and the rules give it."""
    return label not in ("none", "multiple") and p == label


def score(
    data: Path, set_name: str, labels_file: Path, names: list[str], final: bool, strata: list[str]
) -> None:
    """Agreement, confidently wrong and coverage per stratum (Wilson 95%), then over the set weighted by stratum
    size, then the paired difference (last prediction minus first). The strata are those the labelled sample was
    drawn by (`strata`: the two predictions given to `sample`). With --final (test) only the totals are printed."""
    _guard(set_name, final)
    sets = _load(data, "split.json")
    listed = _listed(data)
    labels = json.loads(labels_file.read_text())
    assert set(labels.values()) <= set(LABELS), set(labels.values()) - set(LABELS)
    preds = {x: _load(data, f"pred_{x}.json") for x in names}
    a, b = preds[names[0]], preds[names[-1]]
    sa, sb = (_load(data, f"pred_{x}.json") for x in strata)
    keys = [k for k, s in sets.items() if s == set_name and k in listed]
    st_of = {k: change(sa[k], sb[k]) for k in keys}
    size = Counter(st_of.values())
    lab = [k for k in labels if sets.get(k) == set_name and k in listed]
    print(
        f"{set_name}: {len(keys)} listed products, {len(lab)} labelled; label mix",
        dict(Counter(labels[k] for k in lab)),
    )
    metrics = (("agree", right), ("confidently wrong", wrong), ("coverage", found))
    if not final:
        for st in sorted(size):
            ks = [k for k in lab if st_of[k] == st]
            print(f"  {st} ({size[st]} products, {len(ks)} labelled)")
            for x in names:
                cells = []
                for what, f in metrics:
                    hit = sum(f(preds[x][k], labels[k]) for k in ks)
                    lo, hi = wilson(hit, len(ks))
                    cells.append(f"{what} {hit:3}/{len(ks):<3} [{lo:5.1%}, {hi:5.1%}]")
                print(f"     {x:6} " + "   ".join(cells))
    for x in names:
        out = []
        for what, f in metrics:
            est = var = 0.0
            hits = 0
            for st, n in size.items():
                ks = [k for k in lab if st_of[k] == st]
                if not ks:
                    continue
                w = n / len(keys)
                h = sum(f(preds[x][k], labels[k]) for k in ks)
                hits += h
                p = h / len(ks)
                est += w * p
                var += w * w * p * (1 - p) / len(ks) * (n - len(ks)) / max(1, n - 1)
            out.append(f"{what} {est:6.1%} ± {1.96 * math.sqrt(var):.1%} ({hits}/{len(lab)} labelled)")
        print(f"  whole set {x:6} " + "   ".join(out))
    for what, f in metrics:
        est = var = 0.0
        for st, n in size.items():
            ks = [k for k in lab if st_of[k] == st]
            if not ks:
                continue
            w = n / len(keys)
            d = [int(f(b[k], labels[k])) - int(f(a[k], labels[k])) for k in ks]
            mean = sum(d) / len(d)
            sd2 = sum((v - mean) ** 2 for v in d) / max(1, len(d) - 1)
            est += w * mean
            var += w * w * sd2 / len(d) * (n - len(ks)) / max(1, n - 1)
        print(f"  paired {names[-1]} - {names[0]}: {what} {est:+.1%} ± {1.96 * math.sqrt(var):.1%}")


def packet(data: Path, set_name: str, out: Path) -> None:
    """GUIDE.md and the sample's listings, shuffled, with no prediction and no other labeller's answer."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        show(data, data / f"to_label_{set_name}.json")
    out.write_text((HERE / "GUIDE.md").read_text() + "\n\n# Listings\n\n" + buf.getvalue())
    print(out, buf.getvalue().count("### "), "listings")


def settle(a_file: Path, b_file: Path, out: Path) -> None:
    a, b = json.loads(a_file.read_text()), json.loads(b_file.read_text())
    assert a.keys() == b.keys(), "A and B labelled different listings"
    keys = sorted(k for k in a if a[k] != b[k])
    out.write_text(json.dumps(keys, indent=1) + "\n")
    print(f"{len(a) - len(keys)}/{len(a)} agree; {len(keys)} for the third labeller -> {out}")


def merge(a_file: Path, b_file: Path, c_file: Path, out: Path) -> None:
    a, b, c = (json.loads(f.read_text()) for f in (a_file, b_file, c_file))
    ref = {k: a[k] if a[k] == b[k] else c[k] for k in sorted(a)}
    assert set(ref.values()) <= set(LABELS)
    out.write_text(json.dumps(ref, indent=0, sort_keys=True) + "\n")
    print(out, len(ref))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    final = "--final" in args
    args = [x for x in args if x != "--final"]
    strata = ["main", "v5"]
    if "--strata" in args:
        i = args.index("--strata")
        strata = args[i + 1].split(",")
        del args[i : i + 2]
    if cmd == "build":
        build(args[0], Path(args[1]))
    elif cmd == "split":
        split(Path(args[0]))
    elif cmd == "predict":
        predict(Path(args[0]), args[1])
    elif cmd == "compare":
        n = int(args[args.index("--examples") + 1]) if "--examples" in args else 0
        compare(Path(args[0]), args[1], args[2], args[3], n, final)
    elif cmd == "diag":
        diag(Path(args[0]), int(args[1]))
    elif cmd == "sample":
        sample(Path(args[0]), args[1], args[2], args[3], int(args[4]))
    elif cmd == "show":
        show(Path(args[0]), Path(args[1]))
    elif cmd == "score":
        score(Path(args[0]), args[1], Path(args[2]), args[3:], final, strata)
    elif cmd == "packet":
        packet(Path(args[0]), args[1], Path(args[2]))
    elif cmd == "settle":
        settle(Path(args[0]), Path(args[1]), Path(args[2]))
    elif cmd == "merge":
        merge(Path(args[0]), Path(args[1]), Path(args[2]), Path(args[3]))
