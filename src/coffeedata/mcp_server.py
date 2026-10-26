"""MCP server: log shots, search the catalog, get dial-in advice, and query the models from Claude."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import shots

DB = Path(os.environ.get("COFFEEDATA_DB", "build/coffee.duckdb"))
mcp = MCPServer("coffee-data")


def _catalog_rows() -> list[dict]:
    rows = []
    for p in sorted(Path("data/catalog").glob("*.json")):
        enriched_p = Path("data/enriched") / p.name
        enriched = json.loads(enriched_p.read_text()) if enriched_p.exists() else {}
        for it in json.loads(p.read_text()):
            e = enriched.get(it["product_id"], {})
            prices = [v["price_usd"] for v in it["variants"] if v.get("price_usd")]
            rows.append({
                "coffee_id": f"{it['roaster_slug']}:{it['handle']}", "title": it["title"], "url": it["url"],
                "roaster": it["roaster_slug"], "origin": e.get("origin_country"), "process": e.get("process"),
                "notes": e.get("tasting_notes", []), "in_stock": any(v.get("available") for v in it["variants"]),
                "min_price_usd": min(prices) if prices else None,
            })
    return rows


@mcp.tool()
def log_shot(coffee: str, dose_g: float, yield_g: float, time_s: float, grinder_setting: float,
             rating: int, taste: str | None = None, is_milk_drink: bool = False, notes: str | None = None,
             water_temp_c: float | None = None, roast_date: str | None = None) -> str:
    """Log an espresso shot. `coffee` is free text ("Onyx Geometry") or a coffee_id ("onyxcoffeelab:geometry");
    taste is sour, balanced or bitter. Returns the matched catalog coffee and advice for the next shot."""
    payload = {k: v for k, v in locals().items() if v is not None}
    payload["coffee_id" if ":" in coffee and " " not in coffee else "coffee_freetext"] = payload.pop("coffee")
    shot, matches = shots.log_shot(payload, shots.SHOTS)
    return shots.issue_reply(shot, matches, shots.SHOTS)


@mcp.tool()
def search_coffees(query: str = "", roaster: str | None = None, process: str | None = None,
                   origin: str | None = None, in_stock_only: bool = True, limit: int = 20) -> list[dict]:
    """Search the roaster catalog by free text, roaster slug, process (washed/natural/honey/anaerobic) or origin."""
    rows = _catalog_rows()
    if roaster:
        rows = [r for r in rows if roaster.lower() in r["roaster"]]
    if process:
        rows = [r for r in rows if r["process"] == process.lower()]
    if origin:
        rows = [r for r in rows if r["origin"] and origin.lower() in r["origin"].lower()]
    if in_stock_only:
        rows = [r for r in rows if r["in_stock"]]
    if query:
        from rapidfuzz import fuzz

        scored = [(fuzz.WRatio(query, f"{r['roaster']} {r['title']} {' '.join(r['notes'])}"), r) for r in rows]
        rows = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s >= 60]
    return rows[:limit]


@mcp.tool()
def shot_history(coffee: str) -> list[dict]:
    """All logged shots for a coffee (coffee_id or free text), oldest first."""
    return [json.loads(s.model_dump_json(exclude_none=True)) for s in shots.history(coffee, shots.SHOTS)]


@mcp.tool()
def suggest_next(coffee: str) -> str:
    """Dial-in advice for the next shot of this coffee, based on the last one logged."""
    return shots.suggest_next(coffee, shots.SHOTS)


_SELECT = re.compile(r"^\s*(with|select)\b", re.I)


def check_sql(sql: str) -> str:
    stmt = sql.strip().rstrip(";").strip()
    if ";" in stmt or not _SELECT.match(stmt):
        raise ValueError("only a single SELECT (or WITH … SELECT) statement is allowed")
    return stmt


@mcp.tool()
def run_sql(sql: str, max_rows: int = 200) -> dict:
    """Read-only SQL over the dbt models in DuckDB: dim_roaster, dim_coffee, fct_price_event, fct_shot,
    mart_dial_in, mart_market. Run `make build` first to refresh."""
    import duckdb

    stmt = check_sql(sql)
    con = duckdb.connect(str(DB), read_only=True)
    try:
        cur = con.execute(stmt)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows)
    finally:
        con.close()
    return {"columns": cols, "rows": [list(map(lambda v: v if isinstance(v, (int, float, str, bool, type(None)))
                                               else str(v), r)) for r in rows]}


if __name__ == "__main__":
    mcp.run()
