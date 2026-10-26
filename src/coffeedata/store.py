"""Git-scraping storage: a deterministic per-roaster snapshot plus change-only observation rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .models import CatalogItem

DATA = Path("data")


def _dump(obj: object) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def snapshot_path(slug: str, root: Path = DATA) -> Path:
    return root / "catalog" / f"{slug}.json"


def load_snapshot(slug: str, root: Path = DATA) -> list[dict]:
    p = snapshot_path(slug, root)
    return json.loads(p.read_text()) if p.exists() else []


def diff(prev: list[dict], cur: list[dict], observed_at: str) -> list[dict]:
    """Observation events between two snapshots: new, price, availability, removed (per variant)."""

    def by_variant(rows: list[dict]) -> dict[tuple[str, str], tuple[dict, dict]]:
        return {(p["product_id"], v["variant_id"]): (p, v) for p in rows for v in p["variants"]}

    a, b = by_variant(prev), by_variant(cur)
    events = []

    def row(p: dict, v: dict, event: str) -> dict:
        return {"observed_at": observed_at, "roaster_slug": p["roaster_slug"], "product_id": p["product_id"],
                "variant_id": v["variant_id"], "event": event, "price_usd": v.get("price_usd"),
                "available": v.get("available")}

    for key in sorted(b):
        p, v = b[key]
        if key not in a:
            events.append(row(p, v, "new"))
            continue
        _, old = a[key]
        if old.get("price_usd") != v.get("price_usd"):
            events.append(row(p, v, "price"))
        elif old.get("available") != v.get("available"):
            events.append(row(p, v, "availability"))
    for key in sorted(set(a) - set(b)):
        p, v = a[key]
        events.append(row(p, v, "removed") | {"price_usd": None, "available": False})
    return events


def save(slug: str, items: list[CatalogItem], root: Path = DATA, now: datetime | None = None) -> list[dict]:
    """Write the snapshot and append observation events. Returns the events written."""
    now = now or datetime.now(UTC)
    prev = load_snapshot(slug, root)
    first_seen = {p["product_id"]: p.get("first_seen") for p in prev}
    today = now.date().isoformat()
    cur = []
    for it in sorted(items, key=lambda i: i.product_id):
        d = it.model_dump()
        d["variants"] = sorted(d["variants"], key=lambda v: v["variant_id"])
        d["first_seen"] = first_seen.get(it.product_id) or today
        cur.append(d)
    events = diff(prev, cur, now.isoformat(timespec="seconds"))
    path = snapshot_path(slug, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if cur or prev:
        path.write_text(_dump(cur))
    if events:
        obs = root / "observations" / f"{now:%Y}" / f"{now:%m}.jsonl"
        obs.parent.mkdir(parents=True, exist_ok=True)
        with obs.open("a") as f:
            for e in events:
                f.write(json.dumps(e, sort_keys=True) + "\n")
    return events
