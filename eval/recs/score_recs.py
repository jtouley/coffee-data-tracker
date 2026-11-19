"""Score the "Your Next Bag" experiment once, after the last window closes (eval/recs/README.md, "Scoring").

Prints only totals, Wilson 95% intervals, the sign tests and the pre-registered outcome. Nothing per user.

Usage:
    python eval/recs/score_recs.py SLATES.csv RESPONSES.csv [--exclude EXCLUDE.csv] [--later LATER.csv]
                                  [--outside OUTSIDE.csv] [--funnel FUNNEL.json] [--unsealed-dry-run]

The column contracts are in the README. Every input is read and checked before anything is printed.

The scoring run needs the seal: SHA256SUMS present and matching, config/recs.yml's seal fields filled in and the
README's placeholder gone. --unsealed-dry-run skips that check for a pilot or a rehearsal and never prints an
OUTCOME line.

Exit codes: 0 scored (or dry run), 2 bad input, 3 not sealed or the seal is broken.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CONFIG = REPO / "config" / "recs.yml"

ALPHA = 0.05
MIN_NON_TIED = 12  # fewer non-tied C-vs-P users than this: INCONCLUSIVE (README, "Outcomes")
GUARDRAIL_POINTS = 0.10  # C's "Not for me" rate may be at most 10 points above P's
LATER_WINDOW = (14, 35)  # days after T, both ends included (README, "Metrics")
# The pre-registered exclusion codes an exclusions file may use (README, "Exclusions"); the scorer itself applies
# not_sent and malformed_slate.
EXCLUDE_CODES = ("insider", "duplicate_account", "withdrawn")
SEAL_FIELDS = (("catalog_release",), ("popular", "source_chosen"))


class BadInput(ValueError):
    pass


def settings(path: Path = CONFIG) -> dict:
    """The slate shape and response coding, from the frozen config."""
    cfg = yaml.safe_load(path.read_text())
    return {
        "arms": tuple(cfg["slate"]["arms"]),
        "size": int(cfg["slate"]["size"]),
        "per_arm": int(cfg["slate"]["per_arm"]),
        "interest": sorted(int(v) for v in cfg["responses"]["interest"].values()),
        "raw": cfg,
    }


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


# --- strict parsing ---------------------------------------------------------------------------------------------


def read_csv(path: str, need: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            rows = list(reader)
    except OSError as e:
        raise BadInput(f"{path}: {e.strerror}") from None
    if missing := [c for c in need if c not in header]:
        raise BadInput(f"{path}: missing columns {missing}")
    return rows


def _int(v: str, allowed, what: str) -> int:
    s = str(v).strip()
    if not re.fullmatch(r"-?\d+", s):
        raise BadInput(f"{what} {v!r} is not an integer")
    x = int(s)
    if x not in allowed:
        raise BadInput(f"{what} {x} is not one of the allowed values")
    return x


def _float(v: str, what: str) -> float:
    try:
        x = float(str(v).strip())
    except ValueError:
        raise BadInput(f"{what} {v!r} is not a number") from None
    if not math.isfinite(x):
        raise BadInput(f"{what} {v!r} is not finite")
    return x


# --- the slates and responses -------------------------------------------------------------------------------------


def slate_record(rows, cfg) -> dict[str, tuple[str, dict[str, str]]]:
    """The slate record as first built (user -> (slate_id, coffee -> arm)); one slate per user."""
    rec: dict[str, tuple[str, dict[str, str]]] = {}
    for r in rows:
        arm = r["arm"].strip().upper()
        if arm not in cfg["arms"]:
            raise BadInput(f"slate arm {r['arm']!r} is not one of {cfg['arms']}")
        user, slate = r["user_id"], r["slate_id"]
        sid, items = rec.setdefault(user, (slate, {}))
        if sid != slate:
            raise BadInput("a user has more than one slate in the slate record")
        if r["coffee_id"] in items:
            raise BadInput("a coffee appears twice in one slate in the slate record")
        items[r["coffee_id"]] = arm
    return rec


def slates(record, responses, cfg, excluded=frozenset()) -> tuple[dict, Counter]:
    """Score only slates that were sent by the close and match the slate record; drop users whole.

    Returns kept slates (user -> [(coffee_id, arm, interest, picked)]) and the count of users dropped by code.
    """
    by_user: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in responses:
        if r["user_id"] not in record:
            raise BadInput("a response belongs to a user with no slate in the slate record")
        by_user[r["user_id"]].append(r)
    dropped: Counter = Counter()
    kept = {}
    for user, (slate_id, arms) in record.items():
        if user in excluded:
            continue
        rows = by_user.get(user, [])
        sent = {_int(r["sent"], (0, 1), "sent") for r in rows}
        if not rows or sent != {1}:  # never sent, sent after the close, or partly unsent
            dropped["not_sent"] += 1
            continue
        items = []
        ok = True
        for r in rows:
            if r["slate_id"] != slate_id or r["coffee_id"] not in arms:
                raise BadInput("a response doesn't match the slate record (slate_id or coffee)")
            if r["interest"].strip() == "":
                ok = False  # sent with an item unanswered: the app shouldn't allow it
                continue
            items.append(
                (
                    r["coffee_id"],
                    arms[r["coffee_id"]],
                    _int(r["interest"], cfg["interest"], "interest"),
                    _int(r["picked"], (0, 1), "picked"),
                )
            )
        per_arm = Counter(a for _, a, _, _ in items)
        ok = (
            ok
            and len(arms) == cfg["size"]
            and Counter(arms.values()) == Counter({a: cfg["per_arm"] for a in cfg["arms"]})
            and len(items) == cfg["size"]
            and len({c for c, _, _, _ in items}) == cfg["size"]
            and all(per_arm[a] == cfg["per_arm"] for a in cfg["arms"])
            and sum(p for _, _, _, p in items) == 1
        )
        if ok:
            kept[user] = items
        else:
            dropped["malformed_slate"] += 1
    return kept, dropped


def paired(kept: dict, a: str, b: str) -> dict:
    """Per user, arm a's summed interest against arm b's (each has per_arm items, so sums compare like means)."""
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


def _in_window(rows, kept, record, what: str, in_slate: bool):
    """Rows of kept users inside the later window, latest day per (user, coffee); refuses duplicates."""
    latest: dict[tuple[str, str], tuple[int, dict[str, str]]] = {}
    seen: set[tuple[str, str, int]] = set()
    for r in rows:
        user, coffee = r["user_id"], r["coffee_id"]
        day = _int(r["day"], range(0, 10_000), f"{what} day")
        _int(r["rating"], range(1, 6), f"{what} rating")
        if user not in record:
            raise BadInput(f"{what}: a user with no slate in the slate record")
        if (coffee in record[user][1]) != in_slate:
            raise BadInput(f"{what}: a coffee {'not ' if in_slate else ''}in the user's slate")
        if (user, coffee, day) in seen:
            raise BadInput(f"{what}: two ratings of one coffee by one user on the same day")
        seen.add((user, coffee, day))
        if user not in kept or not LATER_WINDOW[0] <= day <= LATER_WINDOW[1]:
            continue
        if (user, coffee) not in latest or latest[(user, coffee)][0] < day:
            latest[(user, coffee)] = (day, r)
    return [r for _, r in latest.values()]


def later_ratings(rows, kept, record, cfg) -> dict[str, list[int]]:
    rated: dict[str, list[int]] = {a: [] for a in cfg["arms"]}
    for r in _in_window(rows, kept, record, "later", in_slate=True):
        rated[record[r["user_id"]][1][r["coffee_id"]]].append(int(r["rating"]))
    return rated


def outside_ranking(rows, kept, record) -> dict:
    """Per user, the share of differently rated pairs each score orders correctly; C better, worse or tied."""
    for r in rows:
        _float(r["c_score"], "outside c_score")
        _float(r["p_score"], "outside p_score")
    by_user: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for r in _in_window(rows, kept, record, "outside", in_slate=False):
        by_user[r["user_id"]].append((int(r["rating"]), float(r["c_score"]), float(r["p_score"])))
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


def read_exclusions(rows) -> dict[str, str]:
    out = {}
    for r in rows:
        code = r["reason"].strip()
        if code not in EXCLUDE_CODES:
            raise BadInput(f"exclusion reason must be one of {EXCLUDE_CODES}")
        if r["user_id"] in out:
            raise BadInput("a user is listed twice in the exclusions")
        out[r["user_id"]] = code
    return out


def read_funnel(path: str) -> dict[str, int]:
    steps = ("invited", "rated_five", "answered", "rated_later")
    try:
        data = json.loads(Path(path).read_text())
    except OSError as e:
        raise BadInput(f"{path}: {e.strerror}") from None
    except json.JSONDecodeError:
        raise BadInput(f"{path}: not JSON") from None
    if not isinstance(data, dict) or set(data) != set(steps):
        raise BadInput(f"funnel must have exactly {steps}")
    if not all(isinstance(data[s], int) and data[s] >= 0 for s in steps):
        raise BadInput("funnel counts must be non-negative integers")
    return {s: data[s] for s in steps}


def score(slate_rows, responses, exclude=(), later=(), outside=(), cfg=None) -> dict:
    cfg = cfg or settings()
    record = slate_record(slate_rows, cfg)
    excluded = read_exclusions(exclude)
    if set(excluded) - set(record):
        raise BadInput("an excluded user has no slate in the slate record")
    kept, dropped = slates(record, responses, cfg, frozenset(excluded))
    dropped.update(Counter(excluded.values()))
    res: dict = {"users": len(kept), "slates": len(record), "dropped": dict(dropped), "arms": cfg["arms"]}
    res["cp"], res["cr"] = paired(kept, "C", "P"), paired(kept, "C", "R")
    items = [it for its in kept.values() for it in its]
    res["not_for_me"] = {
        a: (
            sum(1 for _, arm, i, _ in items if arm == a and i == 0),
            sum(1 for _, arm, _, _ in items if arm == a),
        )
        for a in cfg["arms"]
    }
    res["picks"] = {a: (sum(p for _, arm, _, p in items if arm == a), len(kept)) for a in cfg["arms"]}
    res["later"] = later_ratings(later, kept, record, cfg)
    res["outside"] = outside_ranking(outside, kept, record)
    res["outcome"] = outcome(res)
    return res


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
    if gap > GUARDRAIL_POINTS + 1e-12:
        return "INCONCLUSIVE", f"C's 'Not for me' rate is {gap:+.1%} against P's (limit +10 points)"
    if cp["p"] < ALPHA:
        return (
            "SUCCESS",
            f"{cp['wins']} of {cp['n']} favour C over P (need {cp['need']}); C ahead of R; guardrail met",
        )
    return (
        "PROMISING",
        f"{cp['wins']} of {cp['n']} favour C over P, under the {cp['need']} needed; C ahead of R",
    )


# --- the seal ---------------------------------------------------------------------------------------------------


def check_seal(sums: Path = HERE / "SHA256SUMS", config: Path = CONFIG, readme: Path = HERE / "README.md"):
    """(sealed, why). Sealed means: SHA256SUMS exists, every line is well formed and matches, the config's seal
    fields are filled in and the README's placeholder is gone."""
    if not sums.exists():
        return False, "not sealed: SHA256SUMS is missing"
    lines = [ln for ln in sums.read_text().splitlines() if ln.strip()]
    if not lines:
        return False, "not sealed: SHA256SUMS is empty"
    for line in lines:
        m = re.fullmatch(r"([0-9a-f]{64}) [ *](\S.*)", line)
        if not m:
            return False, "seal broken: a malformed line in SHA256SUMS"
        path = REPO / m.group(2)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != m.group(1):
            return False, f"seal broken: {m.group(2)} doesn't match SHA256SUMS"
    cfg = yaml.safe_load(config.read_text())
    for keys in SEAL_FIELDS:
        v = cfg
        for k in keys:
            v = (v or {}).get(k)
        if v in (None, ""):
            return False, f"not sealed: config {'.'.join(keys)} is not filled in"
    if "PLACEHOLDER" in readme.read_text():
        return False, "not sealed: the README's seal placeholder is still there"
    return True, "seal: every frozen file matches SHA256SUMS"


# --- the report -------------------------------------------------------------------------------------------------


def _pct(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k} of {n}, {k / n:.0%} (95% {lo:.0%}-{hi:.0%})" if n else "0 of 0"


def report(res: dict, funnel: dict | None = None, dry_run: bool = False) -> str:
    arms = res["arms"]
    out = [f"slates built: {res['slates']}; scored users: {res['users']}"]
    out += [f"  dropped, {code}: {n}" for code, n in sorted(res["dropped"].items())]
    for name, key in (("C vs P (primary)", "cp"), ("C vs R (sanity)", "cr")):
        t = res[key]
        out.append(
            f"{name}: C ahead for {_pct(t['wins'], t['n'])} non-tied; behind {t['losses']}, tied {t['ties']};"
            f" sign test p = {t['p']:.4f}; needed {t['need'] if t['need'] is not None else 'n/a'}"
        )
    out.append(
        "'Not for me' rate (guardrail): " + "; ".join(f"{a} {_pct(*res['not_for_me'][a])}" for a in arms)
    )
    out.append(
        "'Buy next' pick share (chance 33%): " + "; ".join(f"{a} {_pct(*res['picks'][a])}" for a in arms)
    )
    for a in arms:
        r = res["later"][a]
        if r:
            share = _pct(sum(x >= 4 for x in r), len(r))
            out.append(f"later rating, {a}: n {len(r)}, mean {sum(r) / len(r):.2f}, rated 4+ {share}")
        else:
            out.append(f"later rating, {a}: none")
    o = res["outside"]
    out.append(
        f"later ratings outside the slate: C ranks them better for {_pct(o['better'], o['better'] + o['worse'])}"
        f" non-tied users; tied {o['tied']} (descriptive)"
    )
    if funnel:
        out.append("completion: " + " -> ".join(f"{s} {n}" for s, n in funnel.items()))
    if dry_run:
        out.append("DRY RUN, UNSEALED: no outcome is decided from this run")
    else:
        out.append(f"OUTCOME: {res['outcome'][0]} ({res['outcome'][1]})")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("slates")
    ap.add_argument("responses")
    ap.add_argument("--exclude")
    ap.add_argument("--later")
    ap.add_argument("--outside")
    ap.add_argument("--funnel")
    ap.add_argument(
        "--unsealed-dry-run", action="store_true", help="pilot or rehearsal: no seal, no OUTCOME line"
    )
    a = ap.parse_args(argv)
    sealed, seal = check_seal()
    if not sealed and not a.unsealed_dry_run:
        print(f"{seal}; refusing to score (use --unsealed-dry-run for a pilot or rehearsal)", file=sys.stderr)
        return 3
    try:  # read and check every input before printing anything
        res = score(
            read_csv(a.slates, ("user_id", "slate_id", "coffee_id", "arm")),
            read_csv(a.responses, ("user_id", "slate_id", "coffee_id", "interest", "picked", "sent")),
            read_csv(a.exclude, ("user_id", "reason")) if a.exclude else (),
            read_csv(a.later, ("user_id", "coffee_id", "rating", "day")) if a.later else (),
            read_csv(a.outside, ("user_id", "coffee_id", "rating", "day", "c_score", "p_score"))
            if a.outside
            else (),
        )
        funnel = read_funnel(a.funnel) if a.funnel else None
    except BadInput as e:
        print(f"bad input: {e}", file=sys.stderr)
        return 2
    print("UNSEALED DRY RUN: " + seal if a.unsealed_dry_run else seal)
    print(report(res, funnel, dry_run=a.unsealed_dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
