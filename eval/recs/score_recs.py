"""Score the "Your Next Bag" experiment once, after the last window closes (eval/recs/README.md).

Prints only totals, Wilson 95% intervals, the sign tests and the pre-registered outcome. Nothing per user.

Usage:
    python eval/recs/score_recs.py RESPONSES.csv [--exclude EXCLUDE.csv] [--later LATER.csv]
                                  [--outside OUTSIDE.csv] [--funnel FUNNEL.json]

RESPONSES.csv  one row per slate item: user_id, coffee_id, arm (C, P or R), interest (2 "Want to try", 1 "Maybe",
               0 "Not for me"), picked (1 for the user's one "I'd buy this next" pick, else 0)
EXCLUDE.csv    user_id, reason: the exclusions decided before unblinding (README, "Exclusions")
LATER.csv      user_id, coffee_id, arm, rating (1-5): slate coffees drunk and rated in the later window
OUTSIDE.csv    user_id, coffee_id, rating, c_score, p_score: coffees rated later outside the slate, with the frozen
               content score and popularity score (higher is more popular) each arm would have given them
FUNNEL.json    {"invited": n, "rated_five": n, "answered": n, "rated_later": n}

Exit codes: 0 scored, 2 bad input, 3 the frozen files don't match SHA256SUMS.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ARMS = ("C", "P", "R")
SLATE_SIZE, PER_ARM = 6, 2
ALPHA = 0.05
MIN_NON_TIED = 12  # fewer non-tied C-vs-P users than this: INCONCLUSIVE (README, "Outcomes")
GUARDRAIL_POINTS = 0.10  # C's "Not for me" rate may be at most 10 points above P's
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


class BadInput(ValueError):
    pass


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval of k successes in n."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def sign_test(k: int, n: int) -> float:
    """Exact two-sided sign test: k of n non-tied users favour the first arm, under a fair coin."""
    if n == 0:
        return 1.0
    hi = sum(math.comb(n, i) for i in range(k, n + 1)) / 2**n
    lo = sum(math.comb(n, i) for i in range(0, k + 1)) / 2**n
    return min(1.0, 2 * min(hi, lo))


def threshold(n: int) -> int | None:
    """The fewest of n non-tied users who must favour C for the two-sided sign test to reach p < ALPHA."""
    for k in range(n // 2 + 1, n + 1):
        if sign_test(k, n) < ALPHA:
            return k
    return None


def power(n: int, share: float) -> float:
    """The chance the primary passes with n non-tied users when a true `share` of them prefer C (exact binomial)."""
    k = threshold(n)
    if k is None:
        return 0.0
    return sum(math.comb(n, i) * share**i * (1 - share) ** (n - i) for i in range(k, n + 1))


def _read_csv(path: str, need: tuple[str, ...]) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if rows and (missing := [c for c in need if c not in rows[0]]):
        raise BadInput(f"{path}: missing columns {missing}")
    return rows


def _int(v: str, allowed: range, what: str) -> int:
    try:
        x = int(str(v).strip())
    except ValueError:
        raise BadInput(f"{what} {v!r} is not an integer") from None
    if x not in allowed:
        raise BadInput(f"{what} {x} is outside {allowed.start}..{allowed.stop - 1}")
    return x


def slates(rows: list[dict[str, str]]) -> tuple[dict[str, list[tuple[str, str, int, int]]], Counter]:
    """Group the responses by user, keeping only complete, well-formed answered slates.

    Returns the kept slates (user -> [(coffee_id, arm, interest, picked)]) and a count of users dropped by reason.
    A user is dropped whole, never item by item.
    """
    by_user: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
    bad: set[str] = set()
    for r in rows:
        arm = r["arm"].strip().upper()
        if arm not in ARMS:
            raise BadInput(f"arm {r['arm']!r} is not one of {ARMS}")
        interest, picked = r["interest"].strip(), r["picked"].strip().lower()
        if interest == "":  # unanswered item: the slate wasn't sent
            bad.add(r["user_id"])
            continue
        by_user[r["user_id"]].append(
            (
                r["coffee_id"],
                arm,
                _int(interest, range(3), "interest"),
                1 if picked in ("1", "true", "t", "yes") else 0,
            )
        )
    dropped: Counter = Counter()
    kept = {}
    for user, items in by_user.items():
        arms = Counter(a for _, a, _, _ in items)
        ok = (
            user not in bad
            and len(items) == SLATE_SIZE
            and len({c for c, _, _, _ in items}) == SLATE_SIZE
            and all(arms[a] == PER_ARM for a in ARMS)
            and sum(p for _, _, _, p in items) == 1
        )
        if ok:
            kept[user] = items
        else:
            dropped["incomplete or malformed slate"] += 1
    dropped["incomplete or malformed slate"] += len(bad - set(by_user))
    return kept, dropped


def paired(kept: dict, a: str, b: str) -> dict:
    """Per user, arm a's summed interest against arm b's (each has PER_ARM items, so sums compare like means)."""
    wins = losses = ties = 0
    for items in kept.values():
        sa = sum(i for _, arm, i, _ in items if arm == a)
        sb = sum(i for _, arm, i, _ in items if arm == b)
        wins += sa > sb
        losses += sa < sb
        ties += sa == sb
    n = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "n": n,
        "p": sign_test(wins, n),
        "need": threshold(n),
    }


def score(rows, exclude=(), later=(), outside=()) -> dict:
    excluded = {r["user_id"]: r["reason"].strip() or "unspecified" for r in exclude}
    kept, dropped = slates([r for r in rows if r["user_id"] not in excluded])
    present = {r["user_id"] for r in rows}
    dropped.update(Counter(reason for user, reason in excluded.items() if user in present))
    res: dict = {"users": len(kept), "dropped": dict(dropped)}
    res["cp"], res["cr"] = paired(kept, "C", "P"), paired(kept, "C", "R")

    items = [it for its in kept.values() for it in its]
    res["not_for_me"] = {
        a: (
            sum(1 for _, arm, i, _ in items if arm == a and i == 0),
            sum(1 for _, arm, _, _ in items if arm == a),
        )
        for a in ARMS
    }
    res["picks"] = {a: (sum(p for _, arm, _, p in items if arm == a), len(kept)) for a in ARMS}

    rated: dict[str, list[int]] = defaultdict(list)
    for r in later:
        if r["user_id"] in kept:
            rated[r["arm"].strip().upper()].append(_int(r["rating"], range(1, 6), "rating"))
    res["later"] = {a: rated.get(a, []) for a in ARMS}

    res["outside"] = _outside([r for r in outside if r["user_id"] in kept])
    res["outcome"] = outcome(res)
    return res


def _outside(rows) -> dict:
    """Per user, the share of differently-rated pairs each score orders correctly; C better, worse or tied."""
    by_user: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for r in rows:
        by_user[r["user_id"]].append(
            (_int(r["rating"], range(1, 6), "rating"), float(r["c_score"]), float(r["p_score"]))
        )
    better = worse = tied = 0
    for its in by_user.values():
        pairs = [(x, y) for i, x in enumerate(its) for y in its[i + 1 :] if x[0] != y[0]]
        if not pairs:
            continue

        def right(col, pairs=pairs):
            return sum(((x[col] - y[col]) * (x[0] - y[0]) > 0) + 0.5 * (x[col] == y[col]) for x, y in pairs)

        c, p = right(1), right(2)
        better += c > p
        worse += c < p
        tied += c == p
    return {"better": better, "worse": worse, "tied": tied}


def outcome(res: dict) -> tuple[str, str]:
    """The pre-registered decision (README, "Outcomes"), checked in this order."""
    cp, cr = res["cp"], res["cr"]
    (kc, nc), (kp, np_) = res["not_for_me"]["C"], res["not_for_me"]["P"]
    gap = (kc / nc if nc else 0.0) - (kp / np_ if np_ else 0.0)
    if cp["n"] < MIN_NON_TIED:
        return "INCONCLUSIVE", f"{cp['n']} non-tied C-vs-P users, fewer than {MIN_NON_TIED}"
    if cp["wins"] <= cp["losses"]:
        return "FAIL", "C is not ahead of P"
    if cr["wins"] <= cr["losses"]:
        return "FAIL", "C is not ahead of R"
    primary = cp["p"] < ALPHA
    if primary and gap > GUARDRAIL_POINTS + 1e-12:
        return (
            "INCONCLUSIVE",
            f"primary met but C's 'Not for me' rate is {gap:+.1%} against P's (limit +10 points)",
        )
    if primary:
        return (
            "SUCCESS",
            f"{cp['wins']} of {cp['n']} favour C over P (need {cp['need']}); C ahead of R; guardrail met",
        )
    return (
        "PROMISING",
        f"{cp['wins']} of {cp['n']} favour C over P, under the {cp['need']} needed; C ahead of R",
    )


def check_seal(sums: Path = HERE / "SHA256SUMS") -> tuple[bool, str]:
    """Whether every frozen file still matches the seal. Paths in SHA256SUMS are relative to the repo root."""
    if not sums.exists():
        return True, "seal: none yet (SHA256SUMS is written at the seal, PR 7); this is not the scoring run"
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        path = REPO / name.lstrip("*")
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return False, f"seal broken: {name} doesn't match SHA256SUMS"
    return True, "seal: every frozen file matches SHA256SUMS"


def _pct(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k} of {n}, {k / n:.0%} (95% {lo:.0%}-{hi:.0%})" if n else "0 of 0"


def report(res: dict, funnel: dict | None = None) -> str:
    out = [f"scored users: {res['users']}"]
    out += [f"  dropped, {why}: {n}" for why, n in sorted(res["dropped"].items())]
    for name, key in (("C vs P (primary)", "cp"), ("C vs R (sanity)", "cr")):
        t = res[key]
        out.append(
            f"{name}: C ahead for {_pct(t['wins'], t['n'])} non-tied; behind {t['losses']}, tied {t['ties']};"
            f" sign test p = {t['p']:.4f}; needed {t['need'] if t['need'] is not None else 'n/a'}"
        )
    out.append(
        "'Not for me' rate (guardrail): " + "; ".join(f"{a} {_pct(*res['not_for_me'][a])}" for a in ARMS)
    )
    out.append(
        "'Buy next' pick share (chance 33%): " + "; ".join(f"{a} {_pct(*res['picks'][a])}" for a in ARMS)
    )
    for a in ARMS:
        r = res["later"][a]
        if r:
            out.append(
                f"later rating, {a}: n {len(r)}, mean {sum(r) / len(r):.2f}, rated 4+ {_pct(sum(x >= 4 for x in r), len(r))}"
            )
        else:
            out.append(f"later rating, {a}: none")
    o = res["outside"]
    out.append(
        f"later ratings outside the slate: C ranks them better for {_pct(o['better'], o['better'] + o['worse'])}"
        f" non-tied users; tied {o['tied']} (descriptive)"
    )
    if funnel:
        steps = ("invited", "rated_five", "answered", "rated_later")
        out.append("completion: " + " -> ".join(f"{s} {funnel.get(s, 0)}" for s in steps))
    out.append(f"OUTCOME: {res['outcome'][0]} ({res['outcome'][1]})")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("responses")
    ap.add_argument("--exclude")
    ap.add_argument("--later")
    ap.add_argument("--outside")
    ap.add_argument("--funnel")
    a = ap.parse_args(argv)
    sealed, seal = check_seal()
    if not sealed:
        print(seal, file=sys.stderr)
        return 3
    try:
        res = score(
            _read_csv(a.responses, ("user_id", "coffee_id", "arm", "interest", "picked")),
            _read_csv(a.exclude, ("user_id", "reason")) if a.exclude else (),
            _read_csv(a.later, ("user_id", "coffee_id", "arm", "rating")) if a.later else (),
            _read_csv(a.outside, ("user_id", "coffee_id", "rating", "c_score", "p_score"))
            if a.outside
            else (),
        )
    except BadInput as e:
        print(f"bad input: {e}", file=sys.stderr)
        return 2
    print(seal)
    print(report(res, json.loads(Path(a.funnel).read_text()) if a.funnel else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
