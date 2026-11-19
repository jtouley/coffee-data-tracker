"""The "Your Next Bag" scorer (eval/recs/score_recs.py) on invented responses: thresholds, power, the outcome
boundaries, the input checks, the seal and what it prints. No real data; every user and coffee id is made up."""

import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "eval" / "recs"))
import score_recs as sr  # noqa: E402


def user(uid: str, c=(1, 1), p=(1, 1), r=(1, 1), pick="C", sent=1):
    """One slate (record rows) and its answers: two items per arm (2 want, 1 maybe, 0 not for me)."""
    slate, resp = [], []
    for arm, vals in (("C", c), ("P", p), ("R", r)):
        for j, v in enumerate(vals):
            coffee = f"{uid}-{arm}{j}"
            slate.append({"user_id": uid, "slate_id": f"s-{uid}", "coffee_id": coffee, "arm": arm})
            resp.append(
                {
                    "user_id": uid,
                    "slate_id": f"s-{uid}",
                    "coffee_id": coffee,
                    "interest": str(v),
                    "picked": "1" if arm == pick and j == 0 else "0",
                    "sent": str(sent),
                }
            )
    return slate, resp


def combine(*users):
    slate, resp = [], []
    for s, r in users:
        slate += s
        resp += r
    return slate, resp


def cohort(wins: int, losses: int = 0, ties: int = 0, **kw):
    """wins users favour C over P, losses favour P, ties have equal interest; R sits at maybe throughout."""
    us = [user(f"w{i}", c=(2, 2), p=(1, 1), **kw) for i in range(wins)]
    us += [user(f"l{i}", c=(1, 1), p=(2, 2), pick="P", **kw) for i in range(losses)]
    us += [user(f"t{i}", c=(2, 1), p=(1, 2), **kw) for i in range(ties)]
    return combine(*us)


def run(slate, resp, **kw):
    return sr.score(slate, resp, **kw)


def test_sign_test_thresholds_match_the_preregistration():
    table = {12: 10, 13: 11, 14: 12, 15: 12, 16: 13, 17: 13, 18: 14, 19: 15, 20: 15, 21: 16, 22: 17}
    assert {n: sr.threshold(n) for n in table} == table
    assert sr.sign_test(15, 20) == pytest.approx(0.0414, abs=1e-4)
    assert sr.sign_test(14, 20) == pytest.approx(0.1153, abs=1e-4)
    assert sr.threshold(5) is None  # 5 of 5 is p = 0.0625


@pytest.mark.parametrize(
    "n,expected",
    [
        (20, [0.13, 0.42, 0.62, 0.80, 0.93]),
        (15, [0.09, 0.30, 0.46, 0.65, 0.82]),
        (12, [0.08, 0.25, 0.39, 0.56, 0.74]),
    ],
)
def test_power_tables_with_ties(n, expected):
    assert [round(sr.power(n, s), 2) for s in (0.6, 0.7, 0.75, 0.8, 0.85)] == expected


def test_wilson_matches_the_readme():
    assert tuple(round(x, 2) for x in sr.wilson(15, 20)) == (0.53, 0.89)
    assert tuple(round(x, 2) for x in sr.wilson(7, 20)) == (0.18, 0.57)


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
    assert run(*cohort(wins, losses, ties))["outcome"][0] == expected


def test_c_not_ahead_of_random_fails_even_when_it_beats_popular():
    assert run(*cohort(20, r=(2, 2)))["outcome"] == ("FAIL", "C is not ahead of R")


def test_c_vs_r_all_tied_fails():
    us = [user(f"u{i}", c=(2, 2), p=(1, 1), r=(2, 2)) for i in range(20)]
    res = run(*combine(*us))
    assert res["cr"]["n"] == 0 and res["outcome"][0] == "FAIL"


def _guardrail_cohort(c_not: int):
    # P is (0, 1) for 4 users, so 4 of 40; a C (0, 2) user beside a P (1, 1) one ties and drops out of the primary
    return combine(
        *[
            user(f"g{i}", c=(0, 2) if i < c_not else (2, 2), p=(0, 1) if i < 4 else (1, 1), r=(0, 0))
            for i in range(20)
        ]
    )


def test_guardrail_boundary_at_ten_points():
    res = run(*_guardrail_cohort(8))  # C 8 of 40 (20%) against P 4 of 40 (10%): exactly +10 points passes
    assert res["not_for_me"]["C"] == (8, 40) and res["not_for_me"]["P"] == (4, 40)
    assert res["outcome"][0] == "SUCCESS"
    res = run(*_guardrail_cohort(9))  # 22.5% against 10%: over the limit
    assert res["not_for_me"]["C"] == (9, 40) and res["outcome"][0] == "INCONCLUSIVE"


def test_guardrail_is_checked_on_a_promising_result():
    # 14 of 20 favour C (promising on the primary), but C's "Not for me" rate is far above P's
    us = [user(f"w{i}", c=(0, 2), p=(1, 0), r=(0, 0)) for i in range(14)]
    us += [user(f"l{i}", c=(0, 0), p=(1, 1), r=(0, 0)) for i in range(6)]
    res = run(*combine(*us))
    assert res["cp"]["wins"] == 14 and res["cp"]["p"] >= 0.05
    assert res["outcome"][0] == "INCONCLUSIVE" and "Not for me" in res["outcome"][1]


def test_answered_but_not_sent_is_not_scored():
    slate, resp = cohort(15, 5)
    s2, r2 = user("unsent", c=(0, 0), p=(2, 2), pick="P", sent=0)  # every item answered, never sent
    s3, r3 = user("late", c=(0, 0), p=(2, 2), pick="P")
    for row in r3[:3]:
        row["sent"] = "0"  # partly unsent counts as not sent
    s4 = user("silent")[0]  # slate built, no answers at all
    res = run(slate + s2 + s3 + s4, resp + r2 + r3)
    assert res["users"] == 20 and res["dropped"] == {"not_sent": 3}
    assert res["outcome"][0] == "SUCCESS"


def test_malformed_and_excluded_users_are_counted_by_code():
    slate, resp = cohort(15, 5)
    s, r = user("missing")
    slate, resp = slate + s, resp + r[:5]  # five answers to a six-coffee slate
    s, r = user("twopicks")
    r[2]["picked"] = "1"
    slate, resp = slate + s, resp + r
    s, r = user("blank")
    r[0]["interest"] = ""
    slate, resp = slate + s, resp + r
    res = run(slate, resp, exclude=[{"user_id": "w0", "reason": "insider"}])
    assert res["users"] == 19
    assert res["dropped"] == {"malformed_slate": 3, "insider": 1}
    assert res["cp"]["wins"] == 14 and res["outcome"][0] == "PROMISING"  # 14 of 19 is under 15


@pytest.mark.parametrize("reason", ["pilot", "second account of w1", ""])
def test_free_text_exclusion_reasons_are_refused(reason):
    with pytest.raises(sr.BadInput):
        run(*cohort(15, 5), exclude=[{"user_id": "w0", "reason": reason}])


def test_responses_must_match_the_slate_record():
    slate, resp = cohort(15, 5)
    resp[0]["coffee_id"] = "not-in-slate"
    with pytest.raises(sr.BadInput):
        run(slate, resp)
    slate, resp = cohort(15, 5)
    with pytest.raises(sr.BadInput):
        run(slate[6:], resp)  # the first user's slate is missing from the record


@pytest.mark.parametrize(
    "field,value",
    [("interest", "3"), ("interest", "1.0"), ("picked", "1.0"), ("picked", "yes"), ("sent", "true")],
)
def test_values_are_parsed_strictly(field, value):
    slate, resp = cohort(15, 5)
    resp[0][field] = value
    with pytest.raises(sr.BadInput):
        run(slate, resp)


def test_unknown_slate_arm_is_refused():
    slate, resp = cohort(15, 5)
    slate[0]["arm"] = "Z"
    with pytest.raises(sr.BadInput):
        run(slate, resp)


def later_row(uid, coffee, rating, day):
    return {"user_id": uid, "coffee_id": coffee, "rating": str(rating), "day": str(day)}


def test_later_ratings_use_the_window_the_latest_rating_and_the_recorded_arm():
    later = [
        later_row("w1", "w1-C0", 2, 15),
        later_row("w1", "w1-C0", 5, 30),  # the latest in the window wins
        later_row("w2", "w2-C0", 3, 13),  # before the window
        later_row("w3", "w3-P0", 4, 14),  # first day of the window
        later_row("w4", "w4-R0", 1, 35),  # last day
        later_row("w5", "w5-R1", 1, 36),  # after it
    ]
    res = run(*cohort(15, 5), later=later)
    assert res["later"] == {"C": [5], "P": [4], "R": [1]}


@pytest.mark.parametrize(
    "row",
    [
        later_row("w1", "not-in-slate", 4, 20),
        later_row("w1", "w1-C0", 6, 20),
        later_row("w1", "w1-C0", 4, "x"),
        later_row("nobody", "w1-C0", 4, 20),
    ],
)
def test_bad_later_rows_are_refused(row):
    with pytest.raises(sr.BadInput):
        run(*cohort(15, 5), later=[row])


def test_duplicate_later_rows_are_refused():
    rows = [later_row("w1", "w1-C0", 4, 20), later_row("w1", "w1-C0", 2, 20)]
    with pytest.raises(sr.BadInput):
        run(*cohort(15, 5), later=rows)


def outside_row(uid, coffee, rating, day, c, p):
    return {**later_row(uid, coffee, rating, day), "c_score": str(c), "p_score": str(p)}


def test_outside_ranking():
    rows = [
        outside_row("w1", "a", 5, 20, 0.9, 1),
        outside_row("w1", "b", 2, 20, 0.1, 9),
        outside_row("w2", "c", 5, 5, 0.9, 1),
        outside_row("w2", "d", 1, 5, 0.1, 9),
    ]  # w2 is before the window
    assert run(*cohort(15, 5), outside=rows)["outside"] == {"better": 1, "worse": 0, "tied": 0}


@pytest.mark.parametrize(
    "row",
    [
        outside_row("w1", "a", 5, 20, "nan", 1),
        outside_row("w1", "a", 5, 20, 0.5, "inf"),
        outside_row("w1", "a", 5, 20, "high", 1),
        outside_row("w1", "w1-C0", 5, 20, 0.5, 1),  # a slate coffee isn't outside the slate
    ],
)
def test_bad_outside_rows_are_refused(row):
    with pytest.raises(sr.BadInput):
        run(*cohort(15, 5), outside=[row])


def _write(path: Path, rows) -> str:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return str(path)


@pytest.fixture
def sealed(monkeypatch):
    monkeypatch.setattr(sr, "check_seal", lambda: (True, "seal: every frozen file matches SHA256SUMS"))


def test_cli_prints_only_totals(tmp_path, capsys, sealed):
    slate, resp = cohort(15, 5)
    args = [_write(tmp_path / "s.csv", slate), _write(tmp_path / "r.csv", resp)]
    assert sr.main(args) == 0
    out = capsys.readouterr().out
    assert "OUTCOME: SUCCESS" in out and "15 of 20, 75% (95% 53%-89%)" in out
    assert not any(r["user_id"] in out or r["coffee_id"] in out or r["slate_id"] in out for r in slate)


def test_cli_refuses_to_score_without_the_seal(tmp_path, capsys):
    slate, resp = cohort(15, 5)
    args = [_write(tmp_path / "s.csv", slate), _write(tmp_path / "r.csv", resp)]
    assert sr.main(args) == 3  # this branch is unsealed: no SHA256SUMS yet
    assert "OUTCOME" not in capsys.readouterr().out


def test_cli_dry_run_never_prints_an_outcome(tmp_path, capsys):
    slate, resp = cohort(15, 5)
    args = [_write(tmp_path / "s.csv", slate), _write(tmp_path / "r.csv", resp), "--unsealed-dry-run"]
    assert sr.main(args) == 0
    out = capsys.readouterr().out
    assert "OUTCOME" not in out and "DRY RUN" in out


@pytest.mark.parametrize(
    "funnel",
    [
        "{not json",
        json.dumps({"invited": 30}),
        json.dumps({"invited": 30, "rated_five": 24, "answered": -1, "rated_later": 9}),
    ],
)
def test_cli_bad_input_prints_nothing(tmp_path, capsys, sealed, funnel):
    slate, resp = cohort(15, 5)
    (tmp_path / "f.json").write_text(funnel)
    args = [
        _write(tmp_path / "s.csv", slate),
        _write(tmp_path / "r.csv", resp),
        "--funnel",
        str(tmp_path / "f.json"),
    ]
    assert sr.main(args) == 2
    assert capsys.readouterr().out == ""


def test_cli_missing_file_is_bad_input(tmp_path, capsys, sealed):
    slate, _ = cohort(15, 5)
    assert sr.main([_write(tmp_path / "s.csv", slate), str(tmp_path / "absent.csv")]) == 2
    assert capsys.readouterr().out == ""


def test_seal_check(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "REPO", tmp_path)
    frozen = tmp_path / "frozen.txt"
    frozen.write_text("spec\n")
    sums, config, readme = tmp_path / "SHA256SUMS", tmp_path / "recs.yml", tmp_path / "README.md"
    config.write_text("catalog_release: r1\npopular: {source_chosen: weeks_listed}\n")
    readme.write_text("sealed spec\n")

    def check():
        return sr.check_seal(sums, config, readme)

    assert check()[0] is False  # no SHA256SUMS: not sealed
    sums.write_text(f"{hashlib.sha256(b'spec' + bytes([10])).hexdigest()}  frozen.txt\n")
    assert check() == (True, "seal: every frozen file matches SHA256SUMS")
    frozen.write_text("changed\n")
    assert check()[0] is False
    frozen.write_text("spec\n")
    sums.write_text("not-a-valid-line\n")
    assert check() == (False, "seal broken: a malformed line in SHA256SUMS")
    sums.write_text(f"{hashlib.sha256(b'spec' + bytes([10])).hexdigest()}  frozen.txt\n")
    config.write_text("catalog_release: null\npopular: {source_chosen: weeks_listed}\n")
    assert check()[0] is False
    config.write_text("catalog_release: r1\npopular: {source_chosen: weeks_listed}\n")
    readme.write_text("> **PLACEHOLDER: filled in by the seal\n")
    assert check()[0] is False


def test_this_branch_is_not_sealed():
    """Until PR 7, the real check must say "not sealed", so no OUTCOME can come from this tree."""
    assert sr.check_seal()[0] is False


def test_scorer_reads_the_frozen_config():
    cfg = sr.settings()
    assert cfg["arms"] == ("C", "P", "R") and cfg["size"] == 6 and cfg["per_arm"] == 2
    assert cfg["interest"] == [0, 1, 2]
