"""Evaluation of the discovery screen (src/coffeedata/screen.py). See README.md for the protocol.

    python screen_eval.py frame   <data-dir> <pool.parquet> <lake-dir>    the population, split and ranked
    python screen_eval.py screen  <data-dir> <set> <per-tag>               screen the first <per-tag> of each tag, live
    python screen_eval.py draw    <data-dir> <set> <per-stratum>           the items to label, and the stratum sizes
    python screen_eval.py predict <data-dir> <set> <rule> <min-coffees> <out.json>   rescore the screened evidence
    python screen_eval.py agree   <set>                                    two labels -> agreed + to settle
    python screen_eval.py score   <set> <pred.json> [<pred.json> ...]      train/validation: aggregates per rule
    python screen_eval.py choose  <data-dir>                               every rule x N on validation, aggregates
    python screen_eval.py final   <pred_main.json> <pred_branch.json>      the sealed test set, once: totals only

Nothing here prints an item of the validation or test set: only counts, rates and Wilson 95% intervals.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

D = Path(__file__).resolve().parent
SALT = "coffeedata-screen-eval-2026-11-07"
TAGS = ("coffee_shop", "cafe")
SETS = ("train", "validation", "test")
MIN_COFFEES = (1, 2, 3, 5)
# A labeler's kind for an out-of-scope candidate that an admission would make confidently wrong (README).
WRONG_KINDS = ("cafe", "reseller")


def h(*parts: str) -> int:
    return int(hashlib.sha256(":".join((SALT, *parts)).encode()).hexdigest()[:8], 16)


def group(domain: str) -> str:
    """The split's group: the registrable domain (the last two labels; three under a two-letter country code's
    co/com/org/net), so a business's subdomains never sit on both sides."""
    p = domain.split(".")
    if len(p) >= 3 and len(p[-1]) == 2 and p[-2] in ("co", "com", "org", "net"):
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def split_of(domain: str) -> str:
    n = h("split", group(domain)) % 100
    return "train" if n < 60 else "validation" if n < 80 else "test"


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float, float]:
    if not n:
        return (float("nan"), 0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, mid - half), min(1.0, mid + half)


def pct(t: tuple[float, float, float]) -> str:
    p, lo, hi = t
    return "n/a" if p != p else f"{p:.1%} [{lo:.1%}, {hi:.1%}]"


# --- frame, screen, draw --------------------------------------------------------------------------------------------


def frame(data: Path, pool: Path, lake: Path) -> None:
    import duckdb

    con = duckdb.connect()
    seed = D.parents[1] / "config" / "roasters.csv"
    known = {
        r[0]
        for r in con.execute(
            f"select roaster_id from read_csv('{seed}', all_varchar = true) union "
            f"select roaster_id from read_parquet('{lake}/registry/*/*.parquet', hive_partitioning = false)"
        ).fetchall()
    }
    rows = con.execute(
        f"select * from read_parquet('{pool}', hive_partitioning = false) order by roaster_id"
    ).fetchall()
    cols = [d[0] for d in con.description]
    out = []
    for r in rows:
        c = dict(zip(cols, r, strict=True))
        if c["roaster_id"] in known:
            continue
        c["split"] = split_of(c["roaster_id"])
        c["rank"] = h("rank", c["roaster_id"])
        out.append(c)
    (data / "frame.json").write_text(json.dumps(out, default=str))
    n = Counter((c["split"], c["tag"]) for c in out)
    print(f"pool {len(rows):,}; already roasters {len(rows) - len(out):,}; frame {len(out):,}")
    for s in SETS:
        print(s, {t: n[s, t] for t in TAGS})


def load_frame(data: Path) -> list[dict]:
    return json.loads((data / "frame.json").read_text())


def ranked(data: Path, set_name: str, tag: str) -> list[dict]:
    return sorted(
        (c for c in load_frame(data) if c["split"] == set_name and c["tag"] == tag), key=lambda c: c["rank"]
    )


def evidence_path(data: Path, set_name: str) -> Path:
    return data / f"evidence_{set_name}.jsonl.gz"


def load_evidence(data: Path, set_name: str) -> dict[str, dict]:
    p = evidence_path(data, set_name)
    if not p.exists():
        return {}
    with gzip.open(p, "rt") as f:
        return {e["roaster_id"]: e for e in map(json.loads, f)}


def screen(data: Path, set_name: str, per_tag: int) -> None:
    """Screen the first per_tag candidates of each tag in the set's rank order, live, through screen.screen_one,
    keeping the evidence (the probe, the products read, the home page's text). Resumable: done ones are skipped."""
    import logging
    import time
    from concurrent.futures import ThreadPoolExecutor

    from coffeedata import registry
    from coffeedata import screen as sc

    logging.basicConfig(level=logging.ERROR)
    names = [r["name"] for r in registry.load_seed(D.parents[1] / "config" / "roasters.csv")]
    known = sc.KnownRoasters(names)
    done = load_evidence(data, set_name)
    todo = [c for t in TAGS for c in ranked(data, set_name, t)[:per_tag] if c["roaster_id"] not in done]

    def one(c: dict) -> dict:
        # A client of its own per candidate (each is a different host, so spacing and robots.txt are per candidate
        # anyway): behind an HTTPS proxy, a pool shared by thousands of hosts is exhausted by the connections a failed
        # tunnel (a TLS error) never releases.
        keep: dict = {}
        with sc.ScreenClient(min_interval=1.0, timeout=12, retries=2) as client:
            r = sc.screen_one(client, c["roaster_id"], c["name"], known, keep=keep)
        items = [
            {k: v for k, v in i.model_dump(mode="json").items() if k in ITEM_FIELDS}
            | {"body_text": i.body_text[:1500]}
            for i in keep.get("items", [])
        ]
        return {
            "roaster_id": c["roaster_id"],
            "result": r,
            "items": items,
            "home_text": keep.get("home_text", ""),
        }

    start = time.monotonic()
    with ThreadPoolExecutor(sc.WORKERS) as ex:
        for e in ex.map(one, todo):
            # one gzip member per record: a stopped run leaves every finished record readable
            with gzip.open(evidence_path(data, set_name), "at") as f:
                f.write(json.dumps(e, default=str) + "\n")
    secs = time.monotonic() - start
    ev = load_evidence(data, set_name)
    req = sum(e["result"]["requests"] or 0 for e in ev.values())
    print(
        f"{set_name}: screened {len(todo)} now ({secs / 60:.1f} min, {sc.WORKERS} workers), {len(ev)} in all; "
        f"{req / max(1, len(ev)):.1f} requests per candidate"
    )


ITEM_FIELDS = ("product_id", "title", "vendor", "product_type", "tags", "variants")


def items_of(e: dict) -> list:
    """The stored products as CatalogItems (body_text is excluded from a model's dump, so it is set back here)."""
    from coffeedata.models import CatalogItem

    out = []
    for i in e["items"]:
        item = CatalogItem(
            roaster_id=e["roaster_id"], handle="", url="", platform="", **{k: i[k] for k in ITEM_FIELDS}
        )
        item.body_text = i.get("body_text", "")
        out.append(item)
    return out


def found_coffee(e: dict) -> bool:
    """The stratum: the screen read at least one product the rules call coffee (rule-independent)."""
    return (e["result"].get("coffees") or 0) >= 1


def draw(data: Path, set_name: str, per_stratum: int) -> None:
    """Strata: tag x found_coffee. The screened candidates of each tag are a random sample of that tag in the set (rank
    order), so each stratum's size in the set's population is estimated as the tag's size times the stratum's share
    of the screened. From each stratum the first per_stratum in rank order are drawn to label."""
    ev = load_evidence(data, set_name)
    fr = load_frame(data)
    pop = Counter(c["tag"] for c in fr if c["split"] == set_name)
    items, strata = [], {}
    for t in TAGS:
        screened = [c for c in ranked(data, set_name, t) if c["roaster_id"] in ev]
        for fc in (True, False):
            members = [c for c in screened if found_coffee(ev[c["roaster_id"]]) == fc]
            key = f"{t}|{'coffee' if fc else 'no_coffee'}"
            strata[key] = {
                "screened": len(members),
                "screened_tag": len(screened),
                "population": round(pop[t] * len(members) / max(1, len(screened)), 1),
            }
            for c in members[:per_stratum]:
                items.append(
                    {
                        "id": f"{set_name[:2]}-{h('id', c['roaster_id']) % 10**8:08d}",
                        "domain": c["roaster_id"],
                        "website": f"https://{c['roaster_id']}/",
                        "name": c["name"],
                        "city": c["city"],
                        "state": c["state"],
                        "overture_tag": t,
                        "stratum": key,
                    }
                )
            strata[key]["drawn"] = min(per_stratum, len(members))
    items.sort(key=lambda i: i["id"])  # shuffled with respect to stratum and screen outcome
    labeler_view = [{k: v for k, v in i.items() if k != "stratum"} for i in items]
    (D / f"items_{set_name}.json").write_text(json.dumps(labeler_view, ensure_ascii=False, indent=1) + "\n")
    (D / f"sample_{set_name}.json").write_text(
        json.dumps(
            {
                "set": set_name,
                "population_by_tag": dict(pop),
                "strata": strata,
                "stratum_of": {i["id"]: i["stratum"] for i in items},
                "domain_of": {i["id"]: i["domain"] for i in items},
            },
            indent=1,
        )
        + "\n"
    )
    print(
        f"{set_name}: {len(items)} items; strata "
        + json.dumps({k: (v["drawn"], v["population"]) for k, v in strata.items()})
    )


# --- predictions and scores -----------------------------------------------------------------------------------------


def predict(data: Path, set_name: str, rule: str, n: int, out: Path) -> None:
    """Each drawn item's admission under `rule` and `n`, from the stored evidence (main admits none: rule `main`)."""
    from coffeedata import registry
    from coffeedata import screen as sc

    sample = json.loads((D / f"sample_{set_name}.json").read_text())
    ev = load_evidence(data, set_name)
    names = [r["name"] for r in registry.load_seed(D.parents[1] / "config" / "roasters.csv")]
    known = sc.KnownRoasters(names)
    fr = {c["roaster_id"]: c for c in load_frame(data)}
    pred = {}
    for i, dom in sample["domain_of"].items():
        e = ev[dom]
        if rule == "main":
            pred[i] = False
        elif e["result"]["status"] != "ok":
            pred[i] = (
                False  # no answer from the site: rejected under every rule (nothing read decides as no_catalog)
            )
        else:
            pred[i] = sc.decide(items_of(e), e["home_text"], fr[dom]["name"], dom, known, rule, n)["admitted"]
    out.write_text(json.dumps(pred, sort_keys=True) + "\n")
    print(f"wrote {len(pred)} predictions ({rule}, N={n}) to {out}")


def labels(name: str) -> dict[str, dict]:
    return {r["id"]: r for r in json.loads((D / name).read_text())}


def agree(set_name: str) -> None:
    """labels_<set>_A.json and _B.json (each [{id, label, kind}]) -> agreed_<set>.json, and to_settle_<set>.json with
    the disagreements (the item and both labels, no screen result) for the third labeler."""
    a, b = labels(f"labels_{set_name}_A.json"), labels(f"labels_{set_name}_B.json")
    items = {i["id"]: i for i in json.loads((D / f"items_{set_name}.json").read_text())}
    same = {i: a[i] for i in a if i in b and a[i]["label"] == b[i]["label"] and a[i]["label"] != "unsure"}
    print(f"{set_name}: agreement {len(same)}/{len(items)} = {len(same) / len(items):.1%}")
    (D / f"agreed_{set_name}.json").write_text(json.dumps(same, indent=0, sort_keys=True) + "\n")
    dis = [items[i] | {"label_A": a.get(i), "label_B": b.get(i)} for i in items if i not in same]
    (D / f"to_settle_{set_name}.json").write_text(json.dumps(dis, ensure_ascii=False, indent=0) + "\n")
    print(f"to settle: {len(dis)}")


def truth(set_name: str) -> dict[str, dict]:
    t = {k: v for k, v in json.loads((D / f"agreed_{set_name}.json").read_text()).items()}
    settled = D / f"settled_{set_name}.json"
    if settled.exists():
        t |= {r["id"]: r for r in json.loads(settled.read_text())}
    return {k: v for k, v in t.items() if v["label"] in ("in_scope", "out_of_scope")}


def metrics(set_name: str, pred: dict[str, bool]) -> dict:
    """Stratum-weighted precision of admission, recall, and confidently-wrong admits (an admitted café or reseller),
    each with a Wilson 95% interval on the weighted proportion's effective size."""
    sample = json.loads((D / f"sample_{set_name}.json").read_text())
    t = truth(set_name)
    w_adm = w_tp = w_pos = w_wrong = 0.0
    n_adm = n_tp = n_pos = n_wrong = 0
    for key, st in sample["strata"].items():
        ids = [i for i, s in sample["stratum_of"].items() if s == key and i in t]
        if not ids:
            continue
        w = st["population"] / len(ids)  # each labeled item stands for this many candidates
        for i in ids:
            pos, adm = t[i]["label"] == "in_scope", bool(pred[i])
            wrong = adm and not pos and t[i].get("kind") in WRONG_KINDS
            w_adm, w_tp, w_pos, w_wrong = (
                w_adm + w * adm,
                w_tp + w * (adm and pos),
                w_pos + w * pos,
                w_wrong + w * wrong,
            )
            n_adm, n_tp, n_pos, n_wrong = n_adm + adm, n_tp + (adm and pos), n_pos + pos, n_wrong + wrong
    # The interval of a weighted ratio, on the unweighted counts' size (an approximation stated in README.md).
    prec = (w_tp / w_adm if w_adm else float("nan"), *wilson(n_tp, n_adm)[1:])
    rec = (w_tp / w_pos if w_pos else float("nan"), *wilson(n_tp, n_pos)[1:])
    wrong = (w_wrong / w_adm if w_adm else float("nan"), *wilson(n_wrong, n_adm)[1:])
    beta2 = 0.25  # F0.5: precision weighs more (README)
    f = (1 + beta2) * prec[0] * rec[0] / (beta2 * prec[0] + rec[0]) if w_tp else 0.0
    return {"labeled": len(t), "admitted": n_adm, "in_scope": n_pos, "precision": prec, "recall": rec,
            "confidently_wrong": wrong, "wrong_n": n_wrong, "f05": f}  # fmt: skip


def show(name: str, m: dict) -> None:
    print(
        f"{name:28} labeled {m['labeled']}, admitted {m['admitted']}, in scope {m['in_scope']}; "
        f"precision {pct(m['precision'])}; recall {pct(m['recall'])}; "
        f"confidently wrong {m['wrong_n']} = {pct(m['confidently_wrong'])} of admitted; F0.5 {m['f05']:.3f}"
    )


def score(set_name: str, preds: list[str]) -> None:
    if set_name == "test":
        sys.exit("the test set is scored once, with `final`")
    for p in preds:
        show(Path(p).stem, metrics(set_name, json.loads(Path(p).read_text())))


def choose(data: Path) -> None:
    """Every rule x N on validation, from aggregates; the choice is the highest F0.5, ties to the stricter rule."""
    from coffeedata import screen as sc

    best = None
    for rule in sc.RULES:
        for n in MIN_COFFEES:
            out = data / f"pred_validation_{rule}_{n}.json"
            predict(data, "validation", rule, n, out)
            m = metrics("validation", json.loads(out.read_text()))
            show(f"{rule} N={n}", m)
            if best is None or m["f05"] > best[0]:
                best = (m["f05"], rule, n)
    print(f"chosen: {best}")


def final(main_pred: str, branch_pred: str) -> None:
    """The sealed test set, scored once: totals and intervals only."""
    for p in (main_pred, branch_pred):
        show(Path(p).stem, metrics("test", json.loads(Path(p).read_text())))


if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    if cmd == "frame":
        frame(Path(a[0]), Path(a[1]), Path(a[2]))
    elif cmd == "screen":
        screen(Path(a[0]), a[1], int(a[2]))
    elif cmd == "draw":
        draw(Path(a[0]), a[1], int(a[2]))
    elif cmd == "predict":
        predict(Path(a[0]), a[1], a[2], int(a[3]), Path(a[4]))
    elif cmd == "agree":
        agree(a[0])
    elif cmd == "score":
        score(a[0], a[1:])
    elif cmd == "choose":
        choose(Path(a[0]))
    elif cmd == "final":
        final(a[0], a[1])
    else:
        sys.exit(__doc__)
