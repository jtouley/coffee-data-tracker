"""The origin spelling -> ISO 3166-1 census (README.md): build the blind packet, check the labelers' agreement, score.

    python origin_iso_eval.py census  <main.duckdb> <branch.duckdb> <out-dir>   # census.json + packet.json
    python origin_iso_eval.py agree   <out-dir>                                 # A vs B; writes packet_C.json
    python origin_iso_eval.py settle  <out-dir>                                 # A, B, C -> labels.json (in git)
    python origin_iso_eval.py score   <out-dir> [--seed <csv>]                  # the seed against labels.json

The databases are two `coffeedata build`s of one read-only copy of the lake: main's and the change's. Only
census.json, packet*.json and the labelers' files stay out of git (they quote product titles); labels.json holds
spellings, labels and product counts only. Scoring runs the build's own resolution (dbt/macros/origin_iso.sql),
rendered with Jinja over the seed, so it scores the code the build runs.
"""

import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "dbt" / "seeds" / "origin_country_iso.csv"
MACRO = ROOT / "dbt" / "macros" / "origin_iso.sql"
SALT = "coffeedata-origin-iso-2026-11-11"
NONE = {"MULTIPLE", "NOT_A_COUNTRY", "AMBIGUOUS"}  # no single country: the public column stays null
EXAMPLES = 5


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return float("nan"), 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, mid - half), min(1.0, mid + half)


def _captured(con, db: str) -> str:
    return f"""select origin_country as v from {db}.stg_products where origin_country is not null
               union select origin_country from {db}.stg_enrichments where method = 'llm' and origin_country is not null"""


def census(main_db: str, branch_db: str, out: Path) -> None:
    import duckdb

    con = duckdb.connect()
    con.execute(f"attach '{main_db}' as m (read_only)")
    con.execute(f"attach '{branch_db}' as b (read_only)")
    spellings = sorted(
        r[0] for r in con.execute(f"{_captured(con, 'm')} union {_captured(con, 'b')}").fetchall()
    )
    weight = dict(con.execute("select origin_country_raw, count(*) from b.dim_coffee group by 1").fetchall())
    weight_main = dict(con.execute("select origin_country, count(*) from m.dim_coffee group by 1").fetchall())
    # Example listings: the coffees carrying the spelling in the change's dim_coffee, else main's, else any product
    # version Claude's rows gave it (a spelling no current coffee carries). Title, roaster and Claude's region.
    ex: dict[str, list[dict]] = {}

    def add(v: str, title: str, roaster: str | None, region: str | None) -> None:
        got = ex.setdefault(v, [])
        if len(got) < EXAMPLES and all(x["title"] != title for x in got):
            got.append({"title": title, "roaster": roaster, "region": region})

    for db, col in (("b", "origin_country_raw"), ("m", "origin_country")):
        for row in con.execute(
            f"""select {col}, title, roaster_name, origin_region from {db}.dim_coffee where {col} is not null
                order by {col}, md5(coffee_id || '{SALT}')"""
        ).fetchall():
            add(*row)
    for v, title, region in con.execute(
        f"""select e.origin_country, s.title, e.origin_region
            from m.stg_enrichments e
            join (select roaster_id, product_id, title from m.stg_snapshots
                  qualify row_number() over (partition by roaster_id, product_id order by run_date desc) = 1) s
              using (roaster_id, product_id)
            where e.method = 'llm' and e.origin_country is not null
            order by 1, md5(e.roaster_id || e.product_id || '{SALT}')"""
    ).fetchall():
        add(v, title, None, region)
    order = sorted(spellings, key=lambda s: hashlib.sha256(f"{SALT}:{s}".encode()).hexdigest())
    items = [
        {"item": f"S{i:03d}", "spelling": s, "products": weight.get(s, 0), "products_main": weight_main.get(s, 0),
         "examples": ex.get(s, [])[:EXAMPLES]}
        for i, s in enumerate(order, 1)
    ]  # fmt: skip
    out.mkdir(parents=True, exist_ok=True)
    (out / "census.json").write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n")
    # The labelers' packet: spelling and examples only, no weights, no mapping, no kind.
    packet = [{"item": i["item"], "spelling": i["spelling"], "examples": i["examples"]} for i in items]
    (out / "packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=1) + "\n")
    print(
        f"{len(items)} spellings; {sum(i['products'] for i in items)} products in the change's dim_coffee carry one"
    )


def _labels(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text())
    out = {}
    for k, v in raw.items():
        lab = (v["label"] if isinstance(v, dict) else v).strip().upper()
        out[k] = lab
    return out


def _valid(lab: str, codes: set[str]) -> bool:
    return lab in NONE or lab in codes


def _codes() -> set[str]:
    import csv

    return {r["alpha2"] for r in csv.DictReader((ROOT / "dbt/seeds/iso_3166_1.csv").open(encoding="utf-8"))}


def agree(out: Path) -> None:
    items = {i["item"]: i for i in json.loads((out / "packet.json").read_text())}
    a, b = _labels(out / "labels_A.json"), _labels(out / "labels_B.json")
    codes = _codes()
    missing = [i for i in items if i not in a or i not in b]
    bad = [(i, x) for d in (a, b) for i, x in d.items() if not _valid(x, codes)]
    assert not missing and not bad, (missing, bad)
    same = [i for i in items if a[i] == b[i]]
    p, lo, hi = wilson(len(same), len(items))
    print(f"A and B agree on {len(same)} of {len(items)} spellings: {p:.1%} (95% {lo:.1%}-{hi:.1%})")
    diff = [items[i] for i in items if a[i] != b[i]]
    (out / "packet_C.json").write_text(json.dumps(diff, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(diff)} disagreements -> packet_C.json for the third labeler (blind to A and B)")


def settle(out: Path) -> None:
    census_ = {i["item"]: i for i in json.loads((out / "census.json").read_text())}
    a, b = _labels(out / "labels_A.json"), _labels(out / "labels_B.json")
    c = _labels(out / "labels_C.json") if (out / "labels_C.json").exists() else {}
    settled, unresolved = {}, []
    for i, it in census_.items():
        votes = [a[i], b[i]] + ([c[i]] if i in c else [])
        top = max(set(votes), key=votes.count)
        if votes.count(top) >= 2:
            settled[it["spelling"]] = {"label": top, "products": it["products"]}
        else:
            unresolved.append(it["spelling"])
            settled[it["spelling"]] = {"label": "UNRESOLVED", "products": it["products"]}
    dest = Path(__file__).parent / "labels.json"
    dest.write_text(json.dumps(dict(sorted(settled.items())), ensure_ascii=False, indent=1) + "\n")
    print(f"{len(settled)} settled -> {dest}; unresolved (three different labels): {unresolved}")


def predict(spellings: list[str], seed: Path = SEED) -> dict[str, str]:
    """What the build publishes for each spelling: an alpha-2 code, or MULTIPLE / NOT_A_COUNTRY / AMBIGUOUS /
    UNMAPPED. Renders dbt/macros/origin_iso.sql (plain Jinja) over the seed CSV."""
    import duckdb
    import jinja2

    env = jinja2.Environment()
    macro = env.from_string(MACRO.read_text()).module.origin_iso  # type: ignore[attr-defined]
    con = duckdb.connect()
    con.execute("create temp table census (raw_value varchar)")
    con.executemany("insert into census values (?)", [(s,) for s in spellings])
    seed_rel = f"read_csv('{seed}', header = true, all_varchar = true)"
    sql = str(macro("census", seed_rel))
    kinds = {"multiple": "MULTIPLE", "not_a_country": "NOT_A_COUNTRY", "ambiguous": "AMBIGUOUS",
             "unmapped": "UNMAPPED"}  # fmt: skip
    return {r[0]: (r[2] if r[1] == "country" else kinds[r[1]]) for r in con.execute(sql).fetchall()}


def score(labels: dict[str, dict], pred: dict[str, str]) -> dict:
    """Agreement over distinct spellings and weighted by products; wrong = the build publishes a country the label
    says is another country or no single country (confidently wrong). The three no-country labels count as one
    answer for agreement (all publish null); a null where the label is a country is a miss, not wrong."""
    res = {"n": 0, "agree": 0, "wrong": 0, "w": 0, "w_agree": 0, "w_wrong": 0, "misses": [], "wrongs": []}
    for s, lab in labels.items():
        want, got, w = lab["label"], pred.get(s, "UNMAPPED"), lab["products"]
        if want == "UNRESOLVED":
            continue
        ok = got == want or (got in NONE and want in NONE)
        wrong = got not in NONE | {"UNMAPPED"} and got != want
        res["n"] += 1
        res["w"] += w
        res["agree"] += ok
        res["w_agree"] += w * ok
        res["wrong"] += wrong
        res["w_wrong"] += w * wrong
        if not ok:
            res["wrongs" if wrong else "misses"].append((s, want, got, w))
    return res


def report(res: dict) -> None:
    for name, k, n in (("spellings", res["agree"], res["n"]), ("products", res["w_agree"], res["w"])):
        p, lo, hi = wilson(k, n)
        print(f"agreement over {name}: {k} of {n} = {p:.1%} (Wilson 95% {lo:.1%}-{hi:.1%})")
    for name, k, n in (("spellings", res["wrong"], res["n"]), ("products", res["w_wrong"], res["w"])):
        p, lo, hi = wilson(k, n)
        print(f"wrong mappings over {name}: {k} of {n} = {p:.1%} (Wilson 95% {lo:.1%}-{hi:.1%})")
    print("wrong:", res["wrongs"])
    print(
        "misses (no country published where the label names one, or a different no-country kind):",
        res["misses"],
    )


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "census":
        census(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    elif cmd == "agree":
        agree(Path(sys.argv[2]))
    elif cmd == "settle":
        settle(Path(sys.argv[2]))
    elif cmd == "score":
        seed = Path(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else SEED
        labels = json.loads((Path(__file__).parent / "labels.json").read_text())
        report(score(labels, predict(list(labels), seed)))
