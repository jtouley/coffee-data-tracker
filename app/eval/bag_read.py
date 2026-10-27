"""Compare Claude Opus 5.5 and Claude Haiku 5.5 at reading bag labels (PLAN, "Where the bag photo is read").

The set is every bag confirmed in the app with a photo: the person either accepted the match card or picked the
coffee, so the bag's coffee_id and roast date are the right answers. Each model reads each photo three times at
low effort with the same prompt and schema as the app. Scored per field (roaster, coffee name and roast date, exact
after normalizing case and spacing) and end to end: did the Python matcher land on the bag's coffee_id? Also:
median latency, cost per read, and whether the three runs agreed.

Rule: use Haiku if it gets as many end-to-end matches right as Opus (or misses only bags Opus also misses) and its
median latency is lower. Otherwise Opus. Re-run once there are 20 bags, and whenever the prompt or models change.

    uv run python -m app.eval.bag_read                 # the lake in COFFEEDATA_LAKE, photos in COFFEEDATA_PHOTOS
    uv run python -m app.eval.bag_read --runs 1 --limit 5 --out build/bag_read.md

Needs ANTHROPIC_API_KEY. Every run spends real money: about a cent a read with Opus.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app import labels
from app.photos import load as load_photo
from coffeedata import shots
from coffeedata.lake import Lake


@dataclass
class Case:
    bag_id: str
    coffee_id: str
    roaster: str
    title: str
    roast_date: str | None
    jpeg: bytes


@dataclass
class Score:
    model: str
    reads: int = 0
    matched: int = 0
    roaster_ok: int = 0
    coffee_ok: int = 0
    date_ok: int = 0
    dated: int = 0
    agreed: int = 0
    failed: int = 0
    latencies: list[float] = field(default_factory=list)
    cost: float = 0.0
    missed: set[str] = field(default_factory=set)  # bag_ids with any end-to-end miss


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def same_name(read: str | None, truth: str) -> bool:
    """Exact after normalizing, or the read is the truth without a suffix like 'Roasting Co.' (or the reverse)."""
    a, b = norm(read), norm(truth)
    return bool(a) and (a == b or a.startswith(b + " ") or b.startswith(a + " "))


def coffee_right(read: str | None, title: str) -> bool:
    """The coffee's name was read if it matches the title, or every word read is in the title: catalog titles
    often add the origin or process ("Hambela Alaka" vs "Ethiopia Hambela Alaka Washed")."""
    words = norm(read).split()
    return same_name(read, title) or (bool(words) and set(words) <= set(norm(title).split()))


def cases(lake: Lake, photos: Lake, limit: int | None = None) -> list[Case]:
    con = lake.connect()
    src = ", ".join(f"'{s}'" for s in lake.mart_sources("dim_coffee"))
    catalog = {
        cid: (r or "", t or "")
        for cid, r, t in con.execute(
            f"select coffee_id, roaster_name, title from read_parquet([{src}])"
        ).fetchall()
    }
    bags = [
        b
        for b in lake.latest("bags", "bag_id")
        if b["photo_id"] and b["coffee_id"] in catalog and not b["deleted"]
    ]
    out = []
    for b in bags[:limit]:
        # Staging and the dataset are tried one at a time: a photo is in one or the other, never both.
        hit = load_photo(photos, b["photo_id"])
        if hit is None:
            continue  # the photo is missing from the dataset: skip the bag
        img = hit[1]
        roaster, title = catalog[b["coffee_id"]]
        rd = b["roast_date"].isoformat() if b["roast_date"] else None
        out.append(Case(b["bag_id"], b["coffee_id"], roaster, title, rd, img))
    return out


def evaluate(
    cases_: list[Case], models: list[str], runs: int, reader: Callable = labels.read, lake: Lake | None = None
) -> dict[str, Score]:
    scores = {m: Score(m) for m in models}
    for case in cases_:
        for m in models:
            sc, seen = scores[m], []
            for _ in range(runs):
                r = reader(case.jpeg, m)
                sc.reads += 1
                sc.cost += r.cost_usd
                if r.latency_s and r.status == "read":  # a timeout says nothing about a normal read's speed
                    sc.latencies.append(r.latency_s)
                label = r.label or {}
                seen.append((norm(label.get("roaster")), norm(label.get("coffee")), label.get("roast_date")))
                if r.status != "read":
                    sc.failed += 1
                    sc.missed.add(case.bag_id)
                    continue
                coffee_id, _ = shots.match_bag(label.get("roaster"), label.get("coffee"), lake)
                sc.matched += coffee_id == case.coffee_id
                if coffee_id != case.coffee_id:
                    sc.missed.add(case.bag_id)
                sc.roaster_ok += same_name(label.get("roaster"), case.roaster)
                sc.coffee_ok += coffee_right(label.get("coffee"), case.title)
                if case.roast_date:
                    sc.dated += 1
                    sc.date_ok += label.get("roast_date") == case.roast_date
            sc.agreed += len(set(seen)) == 1
    return scores


def decide(scores: dict[str, Score]) -> str:
    opus, haiku = scores.get(labels.OPUS), scores.get(labels.HAIKU)
    if not opus or not haiku or not opus.latencies or not haiku.latencies:
        return labels.OPUS
    as_good = haiku.matched >= opus.matched or haiku.missed <= opus.missed
    faster = statistics.median(haiku.latencies) < statistics.median(opus.latencies)
    return labels.HAIKU if as_good and faster else labels.OPUS


def report(scores: dict[str, Score], n_cases: int, runs: int) -> str:
    def pct(a: int, b: int) -> str:
        return f"{a}/{b}" if b else "–"

    lines = [
        f"Bag label reads: {n_cases} bags × {runs} runs each.",
        "",
        "| Model | End to end | Roaster | Coffee | Roast date | Runs agreed | Failed | Median s | Cost / read |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m, s in scores.items():
        med = f"{statistics.median(s.latencies):.1f}" if s.latencies else "–"
        per = f"${s.cost / s.reads:.4f}" if s.reads else "–"
        lines.append(
            f"| {m} | {pct(s.matched, s.reads)} | {pct(s.roaster_ok, s.reads)} | {pct(s.coffee_ok, s.reads)} | "
            f"{pct(s.date_ok, s.dated)} | {pct(s.agreed, n_cases)} | {s.failed} | {med} | {per} |"
        )
    pick = decide(scores)
    lines += [
        "",
        f"Default by the rule: **{pick}**."
        + ("" if n_cases >= 20 else f" Only {n_cases} bags; re-run at 20."),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--models", nargs="+", default=[labels.OPUS, labels.HAIKU])
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if not labels.available():
        print("ANTHROPIC_API_KEY isn't set.", file=sys.stderr)
        return 2
    lake = Lake()
    found = cases(lake, Lake.photos(), args.limit)
    if not found:
        print("No confirmed bags with photos yet.", file=sys.stderr)
        return 1
    text = report(evaluate(found, args.models, args.runs, lake=lake), len(found), args.runs)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
