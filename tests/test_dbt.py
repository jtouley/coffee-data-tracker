"""Build the dbt project offline from the recorded fixtures (the same data path the daily job uses)."""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from coffeedata import extract, store
from coffeedata.classify import is_coffee

from .conftest import FIX

ROOT = Path(__file__).parent.parent


@pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")
def test_dbt_build_on_fixtures(tmp_path, fixture_items):
    data = tmp_path / "data"
    for slug, items in fixture_items.items():
        coffees = [i for i in items if is_coffee(i)]
        store.save(slug, coffees, data, datetime(2026, 10, 20, tzinfo=UTC))
        extract.save_cache(slug, {i.product_id: extract.rules(i).model_dump() for i in coffees}, data / "enriched")
    first = next(i for i in fixture_items["onyxcoffeelab"] if is_coffee(i))
    shots = [
        {"shot_id": "a", "brewed_at": "2026-10-25T08:00:00Z", "coffee_id": first.coffee_id, "grinder_setting": 15,
         "dose_g": 18, "yield_g": 36, "time_s": 28, "rating": 4, "taste": "balanced"},
        {"shot_id": "b", "brewed_at": "2026-10-26T08:00:00Z", "coffee_freetext": "mystery bag", "grinder_setting": 12,
         "dose_g": 18, "yield_g": 40, "time_s": 24, "rating": 3, "roast_date": "2026-10-10"},
    ]
    (data / "shots.jsonl").write_text("".join(json.dumps(s) + "\n" for s in shots))
    db = tmp_path / "coffee.duckdb"
    env = os.environ | {"COFFEEDATA_DB": str(db)}
    vars_ = json.dumps({"data_dir": str(data), "config_dir": str(FIX)})
    r = subprocess.run(["dbt", "build", "--project-dir", "dbt", "--profiles-dir", "dbt", "--vars", vars_,
                        "--target-path", str(tmp_path / "target"), "--log-path", str(tmp_path / "logs")],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-3000:]
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) from dim_coffee").fetchone()[0] > 20
    assert con.execute("select count(*) from fct_shot").fetchone()[0] == 2
    assert con.execute("select brew_ratio from fct_shot where shot_id = 'a'").fetchone()[0] == 2.0
    assert con.execute("select count(*) from mart_dial_in where grain = 'coffee'").fetchone()[0] == 2
    assert con.execute("select count(*) from fct_price_event where event = 'new'").fetchone()[0] > 0
