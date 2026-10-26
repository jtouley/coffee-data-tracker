"""Build small synthetic lakes for tests: a few roasters, a few weekly runs, chosen failures."""

from datetime import UTC, date, datetime

from coffeedata.lake import Lake

_n = [0]


def batch() -> str:
    _n[0] += 1
    return f"t{_n[0]:04d}"


def at(d: str, hour: int = 12) -> datetime:
    return datetime.fromisoformat(d).replace(hour=hour, tzinfo=UTC)


def product(pid: str, price: float | None = 20.0, available: bool = True, coffee: bool = True, title: str = "") -> dict:
    return {"product_id": pid, "handle": f"h{pid}", "url": f"https://x/{pid}", "title": title or f"Coffee {pid}",
            "platform": "shopify", "tags": [], "body_hash": f"b{pid}", "is_coffee": coffee,
            "variants": [{"variant_id": f"{pid}-v", "title": "12 oz", "price_usd": price, "grams": 340,
                          "available": available}]}


def run(lake: Lake, run_date: str, roaster: str, products: list[dict] | None, hour: int = 12) -> str:
    """Record one fetch attempt: products=None means the fetch failed."""
    b = batch()
    fetched = at(run_date, hour)
    lake.write_partition("fetches", run_date, b, [{
        "run_date": date.fromisoformat(run_date), "batch_id": b, "fetched_at": fetched, "roaster_id": roaster,
        "adapter": "shopify", "status": "error" if products is None else "ok", "error": "boom" if products is None else None,
        "products": len(products or []), "coffees": sum(p["is_coffee"] for p in products or [])}])
    if products:
        lake.write_partition("snapshots", run_date, b, [
            p | {"run_date": date.fromisoformat(run_date), "batch_id": b, "fetched_at": fetched, "roaster_id": roaster}
            for p in products])
    return b


def probe(lake: Lake, run_date: str, roaster: str, status: str = "ok", platform: str = "shopify") -> None:
    b = batch()
    lake.write_partition("probes", run_date, b, [{
        "run_date": date.fromisoformat(run_date), "batch_id": b, "probed_at": at(run_date, 6), "roaster_id": roaster,
        "status": status, "final_host": roaster if status == "ok" else "", "platform": platform if status == "ok" else ""}])
