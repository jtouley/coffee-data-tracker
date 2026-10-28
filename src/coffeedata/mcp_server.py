"""MCP server: log shots, search the catalog, get dial-in advice, and query the models from Claude."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import shots
from .lake import Lake

DB = Path(os.environ.get("COFFEEDATA_DB", "build/coffee.duckdb"))
_columns_cache: dict[str, tuple[float, list[str]]] = {}


def _mart_columns(con, src: str) -> list[str]:
    """The exported dim_coffee's columns: a remote read each, so asked once an hour (they change only with the
    contract), not on every search."""
    import time

    cached = _columns_cache.get(src)
    if cached and time.monotonic() - cached[0] < shots.INDEX_TTL_S:
        return cached[1]
    cols = [r[0] for r in con.execute(f"describe select * from read_parquet([{src}])").fetchall()]
    _columns_cache[src] = (time.monotonic(), cols)
    return cols


mcp = MCPServer("coffee-data")


@mcp.tool()
def log_shot(
    coffee: str,
    dose_g: float,
    yield_g: float,
    time_s: float,
    grinder_setting: float,
    rating: int,
    taste: str | None = None,
    is_milk_drink: bool = False,
    notes: str | None = None,
    water_temp_c: float | None = None,
    roast_date: str | None = None,
    grinder_name: str | None = None,
) -> str:
    """Log an espresso shot. `coffee` is free text ("Onyx Geometry") or a coffee_id ("onyxcoffeelab.com:123");
    taste is sour, balanced or bitter; grinder_name is a grinder from the app's Setup ("DF64 V2"). Returns the
    matched catalog coffee and advice for the next shot."""
    payload = {k: v for k, v in locals().items() if v is not None} | {"source": "mcp"}
    is_id = ":" in coffee and " " not in coffee
    payload["coffee_id" if is_id else "coffee_freetext"] = payload.pop("coffee")
    lake = Lake()
    shot, matches = shots.log_shot(payload, lake)
    lake.publish(f"shot {shot.shot_id}")
    return shots.issue_reply(shot, matches, lake)


@mcp.tool()
def search_coffees(
    query: str = "",
    roaster: str | None = None,
    process: str | None = None,
    origin: str | None = None,
    in_stock_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """Search the current roaster catalog by free text, roaster domain, process (washed/natural/honey/anaerobic)
    or origin country. Uses the same weekly dim_coffee export as the site, so stock status matches it."""
    lake = Lake()
    src = ", ".join(f"'{s}'" for s in lake.mart_sources("dim_coffee"))
    where, params = ["is_listed"], []
    if in_stock_only:
        where.append("in_stock")
    if roaster:
        where.append("roaster_id ilike ?")
        params.append(f"%{roaster}%")
    if process:
        where.append("process = ?")
        params.append(process.lower())
    if origin:
        where.append("origin_country ilike ?")
        params.append(f"%{origin}%")
    con = lake.connect()
    # catalog_status says whether a listed coffee can be bought now or is the roaster's back catalog. A dim_coffee
    # exported before it existed has no such column, so it reads as unknown rather than failing the search.
    has_status = "catalog_status" in _mart_columns(con, src)
    status = "catalog_status" if has_status else "null::varchar as catalog_status"
    cur = con.execute(
        f"""select coffee_id, roaster_name, title, url, origin_country as origin, process, tasting_notes as notes,
                   in_stock, {status}, price_per_100g from read_parquet([{src}]) where {" and ".join(where)}""",
        params,
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    if query:
        from rapidfuzz import fuzz

        scored = [
            (fuzz.WRatio(query, f"{r['roaster_name']} {r['title']} {' '.join(r['notes'] or [])}"), r)
            for r in rows
        ]
        rows = [r for sc, r in sorted(scored, key=lambda x: -x[0]) if sc >= 60]
    return rows[:limit]


@mcp.tool()
def shot_history(coffee: str) -> list[dict]:
    """All logged shots for a coffee (coffee_id or free text), oldest first."""
    return [json.loads(s.model_dump_json(exclude_none=True)) for s in shots.history(coffee, Lake())]


@mcp.tool()
def suggest_next(coffee: str) -> str:
    """Dial-in advice for the next shot of this coffee, based on the last one logged."""
    return shots.suggest_next(coffee, Lake())


_SELECT = re.compile(r"^\s*(with|select)\b", re.I)


def check_sql(sql: str) -> str:
    stmt = sql.strip().rstrip(";").strip()
    if ";" in stmt or not _SELECT.match(stmt):
        raise ValueError("only a single SELECT (or WITH … SELECT) statement is allowed")
    return stmt


@mcp.tool()
def run_sql(sql: str, max_rows: int = 200) -> dict:
    """Read-only SQL over the dbt models in DuckDB: dim_roaster, dim_coffee, dim_equipment, fct_price_event,
    fct_shot, mart_dial_in, mart_market, mart_new_arrivals. Run `make build` first to refresh. This reads the latest
    local build (COFFEEDATA_DB), whether or not it passed its checks; the other tools read the served marts."""
    import duckdb

    stmt = check_sql(sql)
    # Read-only, no file/network access (read_text, read_csv, httpfs, ATTACH …), and the setting is locked
    # so the query can't turn it back on. Only the dbt models in the database are reachable.
    con = duckdb.connect(
        str(DB), read_only=True, config={"enable_external_access": False, "lock_configuration": True}
    )
    try:
        cur = con.execute(stmt)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows)
    finally:
        con.close()
    return {
        "columns": cols,
        "rows": [
            list(map(lambda v: v if isinstance(v, (int, float, str, bool, type(None))) else str(v), r))
            for r in rows
        ],
    }


if __name__ == "__main__":
    mcp.run()
