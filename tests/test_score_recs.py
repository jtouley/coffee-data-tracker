"""The "Your Next Bag" scorer (eval/recs/score_recs.py) on invented responses: thresholds, power, the outcome
boundaries and what it prints. No real data; every user and coffee id here is made up."""

import csv
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "eval" / "recs"))
import score_recs as sr  # noqa: E402


def user(uid: str, c=(1, 1), p=(1, 1), r=(1, 1), pick="C") -> list[dict[str, str]]:
    """One answered slate: two items per arm with the given interests (2 want, 1 maybe, 0 not for me)."""
    rows = []
    for arm, vals in (("C", c), ("P", p), ("R", r)):
        for j, v in enumerate(vals):
            rows.append(
                {
                    "user_id": uid,
                    "coffee_id": f"{uid}-{arm}{j}",
                    "arm": arm,
                    "interest": str(v),
                    "picked": "1" if arm == pick and j == 0 else "0",
                }
            )
    return rows


def cohort(wins: int, losses: int = 0, ties: int = 0, **kw) -> list[dict[str, str]]:
    """wins users favour C over P, losses favour P, ties have equal interest; R sits at maybe throughout."""
    rows = []
    for i in range(wins):
        rows += user(f"w{i}", c=(2, 2), p=(1, 1), **kw)
    for i in range(losses):
        rows += user(f"l{i}", c=(1, 1), p=(2, 2), pick="P", **kw)
    for i in range(ties):
        rows += user(f"t{i}", c=(2, 1), p=(1, 2), **kw)
    return rows


def test_sign_test_thresholds_match_the_preregistration():
    assert sr.threshold(20) == 15 and sr.threshold(16) == 13 and sr.threshold(12) == 10
    assert sr.sign_test(15, 20) == pytest.approx(0.0414, abs=1e-4)
    assert sr.sign_test(14, 20) == pytest.approx(0.1153, abs=1e-4)
    assert sr.threshold(5) is None  # 5 of 5 is p = 0.0625


@pytest.mark.parametrize(
    "share,expected", [(0.6, 0.13), (0.7, 0.42), (0.75, 0.62), (0.8, 0.80), (0.85, 0.93)]
)
def test_power_table(share, expected):
    assert round(sr.power(20, share), 2) == expected


def test_wilson_matches_the_readme():
    lo, hi = sr.wilson(15, 20)
    assert (round(lo, 2), round(hi, 2)) == (0.53, 0.89)
    lo, hi = sr.wilson(7, 20)
    assert (round(lo, 2), round(hi, 2)) == (0.18, 0.57)


@pytest.mark.parametrize(
    "wins,losses,ties,expected",
    [
        (15, 5, 0, "SUCCESS"),  # exactly the 15 of 20 boundary
        (14, 6, 0, "PROMISING"),  # one short
        (13, 3, 4, "SUCCESS"),  # ties drop users: 13 of 16 needed
        (12, 4, 4, "PROMISING"),
        (10, 2, 8, "SUCCESS"),  # 10 of 12
        (9, 3, 8, "PROMISING"),
        (10, 10, 0, "FAIL"),  # C level with P
        (5, 15, 0, "FAIL"),
        (11, 0, 9, "INCONCLUSIVE"),  # 11 non-tied users, under the minimum of 12, though all favour C
    ],
)
def test_primary_boundaries(wins, losses, ties, expected):
    assert sr.score(cohort(wins, losses, ties))["outcome"][0] == expected


def test_c_not_ahead_of_random_fails_even_when_it_beats_popular():
    rows = cohort(20, r=(2, 2))  # R ties C for every winner
    assert sr.score(rows)["outcome"] == ("FAIL", "C is not ahead of R")


def test_guardrail_boundary_at_ten_points():
    # P is (0, 1) for 4 users, so 4 of 40; a C (0, 2) user beside a P (1, 1) one ties and drops out of the primary
    rows = []
    for i in range(20):  # C: 8 of 40 not for me (20%); P: 4 of 40 (10%): exactly +10 points passes
        rows += user(f"g{i}", c=(0, 2) if i < 8 else (2, 2), p=(0, 1) if i < 4 else (1, 1), r=(0, 0))
    res = sr.score(rows)
    assert res["not_for_me"]["C"] == (8, 40) and res["not_for_me"]["P"] == (4, 40)
    assert res["outcome"][0] == "SUCCESS"

    rows = []
    for i in range(20):  # C: 9 of 40 (22.5%) against P's 10%: over the limit
        rows += user(f"g{i}", c=(0, 2) if i < 9 else (2, 2), p=(0, 1) if i < 4 else (1, 1), r=(0, 0))
    res = sr.score(rows)
    assert res["not_for_me"]["C"] == (9, 40)
    assert res["outcome"][0] == "INCONCLUSIVE"


def test_malformed_and_excluded_users_are_dropped_whole():
    rows = cohort(15, 5)
    rows += user("missing")[:5]  # five items, not six
    two_picks = user("twopicks")
    two_picks[2]["picked"] = "1"
    rows += two_picks
    unanswered = user("unanswered")
    unanswered[0]["interest"] = ""
    rows += unanswered
    res = sr.score(rows, exclude=[{"user_id": "w0", "reason": "pilot"}])
    assert res["users"] == 19
    assert res["dropped"] == {"incomplete or malformed slate": 3, "pilot": 1}
    assert res["cp"]["wins"] == 14 and res["outcome"][0] == "PROMISING"  # 14 of 19 is under 15


def test_secondary_metrics():
    rows = cohort(15, 5)
    later = [
        {"user_id": "w1", "coffee_id": "x", "arm": "C", "rating": "5"},
        {"user_id": "w2", "coffee_id": "y", "arm": "C", "rating": "3"},
        {"user_id": "nobody", "coffee_id": "z", "arm": "P", "rating": "1"},
    ]  # not a scored user
    outside = [
        {"user_id": "w1", "coffee_id": a, "rating": rt, "c_score": cs, "p_score": ps}
        for a, rt, cs, ps in (("a", "5", "0.9", "1"), ("b", "2", "0.1", "9"))
    ]
    res = sr.score(rows, later=later, outside=outside)
    assert res["picks"]["C"] == (15, 20) and res["picks"]["P"] == (5, 20)
    assert res["later"]["C"] == [5, 3] and res["later"]["P"] == []
    assert res["outside"] == {"better": 1, "worse": 0, "tied": 0}


def test_bad_values_are_refused():
    rows = user("u")
    rows[0]["interest"] = "3"
    with pytest.raises(sr.BadInput):
        sr.score(rows)
    rows = user("u")
    rows[0]["arm"] = "X"
    with pytest.raises(sr.BadInput):
        sr.score(rows)


def _write(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def test_cli_prints_only_totals(tmp_path, capsys):
    rows = cohort(15, 5)
    assert sr.main([str(_write(tmp_path / "r.csv", rows))]) == 0
    out = capsys.readouterr().out
    assert "OUTCOME: SUCCESS" in out and "15 of 20, 75% (95% 53%-89%)" in out
    assert not any(r["user_id"] in out or r["coffee_id"] in out for r in rows)


def test_cli_rejects_bad_input(tmp_path, capsys):
    rows = user("u")
    rows[0]["interest"] = "7"
    assert sr.main([str(_write(tmp_path / "r.csv", rows))]) == 2


def test_seal_check(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "REPO", tmp_path)
    (tmp_path / "frozen.txt").write_text("spec\n")
    sums = tmp_path / "SHA256SUMS"
    assert sr.check_seal(sums)[0]  # no seal yet: allowed, but says it isn't the scoring run
    sums.write_text(f"{hashlib.sha256(b'spec\n').hexdigest()}  frozen.txt\n")
    assert sr.check_seal(sums) == (True, "seal: every frozen file matches SHA256SUMS")
    (tmp_path / "frozen.txt").write_text("changed\n")
    assert sr.check_seal(sums)[0] is False
