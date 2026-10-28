"""Score C0 (main) and C1 (the change) on one set's labeled sample. Prints totals and Wilson 95% intervals only.

Usage: python score.py <EVAL_DIR> <train|validation|test> [labels file, default labels_ref.json]

Precision and recall are weighted by stratum size (F, B, N in rules.stratum), since each stratum is sampled at a
different rate; the unweighted sample counts and their intervals are printed beside them. Stratum R (no rule flags
it and it doesn't look bulk) isn't sampled, so recall says nothing about wholesale listings there."""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rules import RULES, flag, stratum  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (float("nan"),) * 3
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, c - h, c + h


def measure(eval_dir: str, name: str, rule: str, labels: str = "labels_ref.json") -> dict:
    """Weighted precision and recall, and the sample counts behind them, for one rule on one set."""
    work = Path(eval_dir) / "work"
    pop = [r for r in json.loads(Path(work / "population.json").read_text()) if r["set"] == name]
    sample = set(json.loads(Path(work / f"labels_{name}.json").read_text()))  # the ids drawn for this set
    lab = {k: v for k, v in json.loads(Path(work / labels).read_text()).items() if k in sample}
    size = {k: sum(stratum(r) == k for r in pop) for k in "FBN"}
    est = {"tp": 0.0, "fp": 0.0, "fn": 0.0}
    raw = {"tp": 0, "fp": 0, "fn": 0, "unclear_flagged": 0}
    for k in "FBN":
        got = [r for r in pop if stratum(r) == k and r["coffee_id"] in lab]
        n = sum(lab[r["coffee_id"]] != "unclear" for r in got)
        for r in got:
            y, f = lab[r["coffee_id"]], flag(r, rule)
            if y == "unclear":
                raw["unclear_flagged"] += f
                continue
            key = "tp" if f and y == "wholesale" else "fp" if f else "fn" if y == "wholesale" else None
            if key:
                raw[key] += 1
                est[key] += size[k] / max(n, 1)
    return (
        raw
        | size
        | {
            "flags": sum(flag(r, rule) for r in pop),
            "listed": len(pop),
            "precision": est["tp"] / max(est["tp"] + est["fp"], 1e-9),
            "recall": est["tp"] / max(est["tp"] + est["fn"], 1e-9),
        }
    )


def main(eval_dir: str, name: str, labels: str = "labels_ref.json") -> None:
    for rule in RULES:
        m = measure(eval_dir, name, rule, labels)
        p = wilson(m["tp"], m["tp"] + m["fp"])
        r = wilson(m["tp"], m["tp"] + m["fn"])
        print(
            f"{name} {rule}: flags {m['flags']} of {m['listed']} listed;"
            f" weighted precision {m['precision']:.1%}, recall {m['recall']:.1%};"
            f" sample tp {m['tp']} fp {m['fp']} fn {m['fn']} (unclear: {m['unclear_flagged']} flagged),"
            f" precision {p[0]:.1%} [{p[1]:.1%}, {p[2]:.1%}], recall {r[0]:.1%} [{r[1]:.1%}, {r[2]:.1%}];"
            f" strata F/B/N {m['F']}/{m['B']}/{m['N']}"
        )


if __name__ == "__main__":
    main(*sys.argv[1:4])
