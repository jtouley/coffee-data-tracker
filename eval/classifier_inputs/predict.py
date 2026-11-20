"""Run the installed coffeedata classifier over a sample: products.decide with the stored verdicts, as the build does.

  python predict.py <sample.json> <lake copy> <out.json>

Run it from a checkout's own environment; it prints which coffeedata it imported (check it is that checkout's)
and the git commit of that checkout. It prints counts only.
out: {id: {"is_coffee": bool, "by": "claude"|"rules", "excluded": bool, "rules_only": bool}}
  excluded    the rules exclude the product outright (classify.excluded), whatever Claude said
  rules_only  what the rules alone answer (classify.is_coffee): the product's fate if its stored verdict stops
              joining, as it does when a change gives it a new input_hash
"""

import html
import json
import subprocess
import sys
from pathlib import Path

import duckdb

import coffeedata
from coffeedata import classify, products, verdict

sample, lake, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
verdicts = dict(duckdb.connect().execute(
    "select input_hash, label from read_parquet(?, union_by_name=true) where definition = ? "
    "qualify row_number() over (partition by input_hash order by decided_at desc, label = 'coffee' desc, label) = 1",
    [f"{lake}/verdicts/*/*.parquet", verdict.DEFINITION_VERSION]).fetchall())  # fmt: skip
res = {}
for m in json.loads(sample.read_text())["items"]:
    # dbt decodes HTML entities in tags (stg_snapshots); input_hash stays over the raw tags.
    tags = [html.unescape(t) if t and "&" in t else t for t in (m["tags"] or [])]
    it = products.item(m["title"], m["product_type"], tags, m["grams"], m["body_hash"], m["body_text"])
    ok, by = products.decide(it, verdicts.get(m["input_hash"]))
    res[m["id"]] = {"is_coffee": ok, "by": by, "excluded": bool(classify.excluded(it)),
                    "rules_only": bool(classify.is_coffee(it))}  # fmt: skip
out.write_text(json.dumps(res))
root = Path(coffeedata.__file__).parents[2]
sha = subprocess.run(
    ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
).stdout.strip()
print(
    f"coffeedata {coffeedata.__file__} @ {sha[:12]}; definition {verdict.DEFINITION_VERSION}; "
    f"{len(res)} products, {sum(v['is_coffee'] for v in res.values())} coffee, "
    f"{sum(v['by'] == 'claude' for v in res.values())} decided by a stored verdict"
)
