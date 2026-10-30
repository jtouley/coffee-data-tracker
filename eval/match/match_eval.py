"""Typed-text matching evaluation (`shots.match_coffee`) on a local, read-only copy of the lake. See README.md.

    python match_eval.py split   <lake-dir> <data-dir>                       train/validation/test, grouped by roaster
    python match_eval.py predict <lake-dir> <data-dir> <set> <name> [--out F] runs the importable coffeedata
    python match_eval.py score   <lake-dir> <data-dir> <set> <pred>... [--examples N]
                                                                             right / abstain / confidently wrong,
                                                                             Wilson 95% intervals

<lake-dir> holds marts/dim_coffee.parquet. The data dir holds the split and train/validation predictions; it
stays out of git. --examples is for train only. Test is scored once, from the sealed files in this directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

import duckdb

SALT = "coffeedata-match-eval-2026-10-30"
PER_SET = 1000  # items drawn from each set, by hash; two queries each
FORMS = ("title", "roaster+title")
HERE = Path(__file__).parent


def bucket(key: str) -> int:
    return int(hashlib.sha256(f"{SALT}:{key}".encode()).hexdigest()[:8], 16) % 100


def norm(s: str | None) -> str:
    return " ".join(re.findall(r"\w+", (s or "").casefold()))


def catalog(lake_dir: str) -> dict[str, tuple[str, str, str]]:
    """coffee_id -> (roaster_id, roaster_name, title), for every catalog coffee."""
    rows = duckdb.execute(
        "select coffee_id, coalesce(roaster_id, roaster_name), roaster_name, title"
        f" from '{lake_dir}/marts/dim_coffee.parquet'"
    ).fetchall()
    return {cid: (rid, r or "", t or "") for cid, rid, r, t in rows}


def query(form: str, roaster: str, title: str) -> str:
    return title if form == "title" else f"{roaster} {title}"


def split(lake_dir: str, data: Path) -> None:
    cat = catalog(lake_dir)
    sets: dict[str, list[str]] = defaultdict(list)
    for cid, (rid, _, title) in cat.items():
        if not title.strip():
            continue  # nothing to type
        n = bucket(rid)
        sets["train" if n < 60 else "validation" if n < 80 else "test"].append(cid)
    out = {name: sorted(keys, key=bucket)[:PER_SET] for name, keys in sets.items()}
    data.mkdir(parents=True, exist_ok=True)
    (data / "split.json").write_text(json.dumps(out, indent=1))
    (HERE / "test_keys.txt").write_text("\n".join(out["test"]) + "\n")
    for name, keys in sets.items():
        roasters = len({cat[k][0] for k in keys})
        print(f"{name}: {len(keys)} coffees, {roasters} roasters; {len(out[name])} drawn")


_lake = None


def _predict(task: tuple[str, str]) -> tuple[str, str | None, float]:
    from coffeedata import shots

    t = time.perf_counter()
    got = shots.match_coffee(task[1], _lake)[0]
    return task[0], got, time.perf_counter() - t


def predict(lake_dir: str, data: Path, name: str, set_: str, out: Path | None) -> None:
    global _lake
    from coffeedata import shots
    from coffeedata.lake import Lake

    _lake = Lake(lake_dir)
    shots._index(_lake)  # load once, before the workers fork
    cat = catalog(lake_dir)
    keys = json.loads((data / "split.json").read_text())[set_]
    tasks = [(f"{k}|{f}", query(f, cat[k][1], cat[k][2])) for k in keys for f in FORMS]
    with Pool() as pool:
        results = pool.map(_predict, tasks, chunksize=8)
    src = Path(shots.__file__).read_bytes()
    doc = {
        "name": name,
        "shots_py_sha256": hashlib.sha256(src).hexdigest(),
        "sec_per_call": round(sum(r[2] for r in results) / len(results), 3),
        "pred": {key: got for key, got, _ in results},
    }
    path = out or data / f"pred_{name}_{set_}.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"{path}: {len(results)} queries, {doc['sec_per_call']} s/call")


def outcome(cat: dict, ambiguous: set[str], key: str, got: str | None) -> str:
    """right, abstain or wrong (a confident prediction that isn't right). See README, "Reference"."""
    cid, form = key.split("|")
    _, roaster, title = cat[cid]
    if form == "title" and norm(title) in ambiguous:
        return "right" if got is None else "wrong"
    if got is None:
        return "abstain"
    _, r, t = cat[got]
    return "right" if (norm(r), norm(t)) == (norm(roaster), norm(title)) else "wrong"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    mid, half = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return mid - half, mid + half


def score(lake_dir: str, data: Path, set_: str, preds: list[str], examples: int) -> None:
    if examples and set_ != "train":
        sys.exit("examples are shown for train only")
    cat = catalog(lake_dir)
    roasters_by_title = defaultdict(set)
    for _, r, t in cat.values():
        if norm(t):
            roasters_by_title[norm(t)].add(norm(r))
    ambiguous = {t for t, rs in roasters_by_title.items() if len(rs) > 1}
    folder = HERE if set_ == "test" else data
    docs = [json.loads((folder / f"pred_{p}_{set_}.json").read_text()) for p in preds]
    for doc in docs:
        tally = Counter()
        for key, got in doc["pred"].items():
            o = outcome(cat, ambiguous, key, got)
            tally[key.split("|")[1], o] += 1
            tally["all", o] += 1
        print(f"\n{set_} / {doc['name']}  ({doc['sec_per_call']} s/call)")
        for form in (*FORMS, "all"):
            n = sum(tally[form, o] for o in ("right", "abstain", "wrong"))
            cells = []
            for o in ("right", "abstain", "wrong"):
                lo, hi = wilson(tally[form, o], n)
                cells.append(f"{o} {tally[form, o]}/{n} {tally[form, o] / n:.1%} [{lo:.1%}, {hi:.1%}]")
            print(f"  {form:14} " + "  ".join(cells))
    if len(docs) == 2:
        a, b = (d["pred"] for d in docs)
        changed = [k for k in a if a[k] != b[k]]
        print(f"\noutputs changed {docs[0]['name']} -> {docs[1]['name']}: {len(changed)}/{len(a)}")
        for k in changed[:examples]:
            cid, form = k.split("|")
            show = lambda x: x and " | ".join(cat[x][1:])  # noqa: E731
            print(f"  {form:14} {' | '.join(cat[cid][1:])!r}: {show(a[k])!r} -> {show(b[k])!r}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("split", "predict", "score"):
        p = sub.add_parser(cmd)
        p.add_argument("lake_dir")
        p.add_argument("data", type=Path)
        if cmd != "split":
            p.add_argument("set", choices=("train", "validation", "test"))
    sub.choices["predict"].add_argument("name")
    sub.choices["predict"].add_argument("--out", type=Path)
    sub.choices["score"].add_argument("preds", nargs="+")
    sub.choices["score"].add_argument("--examples", type=int, default=0)
    a = ap.parse_args()
    if a.cmd == "split":
        split(a.lake_dir, a.data)
    elif a.cmd == "predict":
        predict(a.lake_dir, a.data, a.name, a.set, a.out)
    else:
        score(a.lake_dir, a.data, a.set, a.preds, a.examples)
