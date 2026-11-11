"""The data-quality warnings #12 (near_duplicate_roasters) and #7 (price_per_100g_outliers) against eval/dq's labels
(eval/dq/README.md), run as dbt compiles them over the release eval/dq replayed.

The labels and the eval's sampling are in git; the catalog is private lake data: DQ_EVAL_DIR holds the served
dim_coffee and dim_roaster of release 20261103T203218-956abd (the 11-02 build), which eval/floor_inputs.py fetches
from the lake, read-only. The eval's own `items` code redraws its samples from them, and the test checks they are the
labelled items, so the floors score what was labelled. Only totals are printed.

Not floored here: #11 (per-adapter volume bands) scores metrics of five replayed builds that never existed in the lake
(the change added those metrics), and #4 (reconciliation) has 3 labelled train items, all one label, no validation
item; tests/test_dq_rules.py makes both fire on synthetic builds.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from .evalfloor import eval_dir
from .lakehelp import needs_dbt
from .smoke import require_dbt

pytestmark = [
    pytest.mark.private_eval,
    needs_dbt,
]
ROOT = Path(__file__).parent.parent
DQ = ROOT / "eval" / "dq"
sys.path.insert(0, str(DQ))

# Measured 2026-11-11 on main 0cfa4d9 (stratum-weighted as the eval scores them). Raise floors and lower ceilings
# when a rule improves; never move them the other way to pass.
FLOOR = {
    ("dupes", "train"): {"precision": 1.0, "recall": 1.0},
    ("dupes", "validation"): {"precision": 1.0, "recall": 1.0},
    ("price", "train"): {"precision": 0.857, "recall": 0.325},
    ("price", "validation"): {"precision": 0.957, "recall": 0.594},
}
CEILING = {  # dupes: distinct pairs flagged (count); price: weighted share of flags on right prices
    ("dupes", "train"): 0,
    ("dupes", "validation"): 0,
    ("price", "train"): 0.042,
    ("price", "validation"): 0.031,
}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, dict[str, set]]:
    """The eval's redrawn samples (its data dir) and both warnings' rows, from the SQL dbt compiles over the
    release's marts."""
    src = eval_dir("DQ_EVAL_DIR")
    require_dbt()  # module-scoped, so it runs before the tests' own dbt_installed check
    tmp = tmp_path_factory.mktemp("dq")
    db = tmp / "coffee.duckdb"
    r = subprocess.run(
        ["dbt", "compile", "--project-dir", "dbt", "--profiles-dir", "dbt", "--target-path", str(tmp / "target"),
         "--log-path", str(tmp / "logs"), "--select", "int_current_catalog near_duplicate_roasters price_per_100g_outliers"],
        cwd=ROOT, env=os.environ | {"COFFEEDATA_DB": str(db)}, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout[-3000:]
    compiled = tmp / "target" / "compiled" / "coffeedata"
    con = duckdb.connect(str(db))
    for t in ("dim_coffee", "dim_roaster"):
        con.execute(f"create table {t} as select * from read_parquet('{src / f'{t}.parquet'}')")
    sql = (compiled / "models" / "intermediate" / "int_current_catalog.sql").read_text()
    con.execute(f"create table int_current_catalog as {sql}")
    # the eval's items code writes label evidence too, which reads the latest snapshot: none is needed here
    con.execute("create table stg_snapshots (coffee_id varchar, run_date date, variants struct(title varchar, "
                "price_usd double, grams integer, available boolean)[])")  # fmt: skip
    flags = {
        "dupes": {f"{a}|{b}" for a, b in con.execute(
            f"select roaster_a, roaster_b from ({(compiled / 'tests' / 'near_duplicate_roasters.sql').read_text()})").fetchall()},
        "price": {c for (c,) in con.execute(
            f"select coffee_id from ({(compiled / 'tests' / 'price_per_100g_outliers.sql').read_text()})").fetchall()},
    }  # fmt: skip
    con.close()
    data = tmp / "data"
    import dupes_eval
    import price_eval

    dupes_eval.cmd_items(str(db), data)
    price_eval.cmd_items(str(db), data)
    return data, flags


def _scores(rule: str, name: str, data: Path, flags: set) -> dict[str, float]:
    from common import reference

    items = [i for i in json.loads((data / f"{rule}_items.json").read_text())]
    ref = reference(DQ / "labels", rule)
    labelled = {i["id"] for i in items if i["split"] != "test"}
    assert labelled == set(ref), (
        f"{rule}: the redrawn sample isn't the labelled one ({len(labelled)} vs {len(ref)})"
    )
    items = [i for i in items if i["split"] == name]
    bad, good = ("duplicate", "distinct") if rule == "dupes" else ("wrong", "right")
    if rule == "dupes":
        sizes = json.loads((data / "dupes_strata.json").read_text())
        every = json.loads((data / "dupes_items.json").read_text())
        w = {s: sizes[s] / max(1, sum(i["stratum"] == s for i in every)) for s in sizes}
        weight = {i["id"]: w[i["stratum"]] for i in items}
    else:
        sizes = json.loads((data / "price_strata.json").read_text())
        per: dict[int, int] = {}
        for i in items:
            per[i["stratum"]] = per.get(i["stratum"], 0) + 1
        weight = {i["id"]: sizes[f"{name}:{i['stratum']}"] / per[i["stratum"]] for i in items}
    flagged = [i["id"] for i in items if i["id"] in flags]
    wf = sum(weight[k] for k in flagged)
    tp = sum(weight[k] for k in flagged if ref[k] == bad)
    fp = sum(weight[k] for k in flagged if ref[k] == good)
    pos = sum(weight[i["id"]] for i in items if ref[i["id"]] == bad)
    return {
        "precision": tp / wf if wf else 1.0,
        "recall": tp / pos if pos else 1.0,
        "false_flags": sum(ref[k] == good for k in flagged) if rule == "dupes" else (fp / wf if wf else 0.0),
    }


@pytest.mark.parametrize("rule,name", sorted(FLOOR))
def test_dq_warning_accuracy_does_not_regress(rule, name, built):
    data, flags = built
    s = _scores(rule, name, data, flags[rule])
    for metric, floor in FLOOR[rule, name].items():
        assert s[metric] >= floor, f"{rule} {name} {metric}: {s[metric]:.3f} < {floor}"
    assert s["false_flags"] <= CEILING[rule, name], f"{rule} {name} false flags: {s['false_flags']:.3f}"
