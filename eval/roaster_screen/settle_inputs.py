"""Build blind settle inputs from #130's stored screen evidence: the 7 item fields, home_text (first 2,000
characters) and up to 40 products (title, vendor, type, tags, sizes). Nothing from the screen's `result` and no
label from any labeler. Usage: python -I settle_inputs.py <evidence-dir> <eval-dir> <out-dir>"""

import ast
import gzip
import json
import sys
from pathlib import Path

HOME_CHARS, MAX_PRODUCTS = 2000, 40
FIELDS = ("id", "domain", "website", "name", "city", "state", "overture_tag")
evidence_dir, eval_dir, out = (Path(a) for a in sys.argv[1:4])
out.mkdir(parents=True, exist_ok=False)


def listish(v):
    """A stored list, or a list's repr; any other string (a comma-separated tag string) is split on commas."""
    if isinstance(v, str):
        if v.lstrip().startswith("["):
            return ast.literal_eval(v)
        return [t.strip() for t in v.split(",") if t.strip()]
    return v or []


def size(v: dict) -> dict:
    return {"title": v.get("title"), "grams": v.get("grams")}


for s in ("train", "validation"):
    waiting = json.loads((eval_dir / f"to_settle_{s}.json").read_text())
    items = {x["id"]: x for x in json.loads((eval_dir / f"items_{s}.json").read_text())}
    ev = {}
    with gzip.open(evidence_dir / f"evidence_{s}.jsonl.gz", "rt") as f:
        for line in f:
            r = json.loads(line)
            ev[r["roaster_id"]] = r
    rows = []
    # in id order: no order signal from the files they came from
    for w in sorted(waiting, key=lambda x: x["id"]):
        item = items[w["id"]]
        e = ev.get(item["domain"], {})
        products = [
            {"title": p.get("title"), "vendor": p.get("vendor"), "product_type": p.get("product_type"),
             "tags": listish(p.get("tags")), "sizes": [size(v) for v in listish(p.get("variants"))]}
            for p in (e.get("items") or [])[:MAX_PRODUCTS]
        ]  # fmt: skip
        rows.append({k: item[k] for k in FIELDS} | {
            "home_text": (e.get("home_text") or "")[:HOME_CHARS],
            "products_shown": len(products),
            "products_stored": len(e.get("items") or []),
            "products": products,
        })  # fmt: skip
    (out / f"settle_items_{s}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    print(s, len(rows))
