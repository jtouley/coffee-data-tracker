"""Static site: one index.html (+ catalog.json) rendered from the dbt models in DuckDB. Shots are
private and never appear here; they are only in the app."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

# --- inline SVG charts (single series, role-based colors from CSS variables) -------------------------


def hbar_chart(rows: list[tuple[str, float, str]], unit: str, label: str, width: int = 460) -> str:
    """Horizontal bars: rows of (category, value, tooltip). Value labels at the bar end."""
    if not rows:
        return '<p class="empty">Not enough data yet.</p>'
    bar_h, gap, left, right = 18, 2 + 6, 120, 56
    height = len(rows) * (bar_h + gap) + 8
    vmax = max(v for _, v, _ in rows) or 1
    scale = (width - left - right) / vmax
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">']
    for i, (cat, v, tip) in enumerate(rows):
        y = 4 + i * (bar_h + gap)
        w = max(v * scale, 2)
        out.append(
            f'<g class="mark" tabindex="0" data-tip="{escape(tip)}">'
            f'<rect class="hit" x="0" y="{y - 4}" width="{width}" height="{bar_h + 8}"/>'
            f'<text class="cat" x="{left - 8}" y="{y + bar_h / 2}" text-anchor="end" dominant-baseline="central">{escape(cat)}</text>'
            # 4px rounded data end, square at the baseline.
            f'<path class="bar" d="M{left},{y} h{max(w - 4, 0):.1f} q4,0 4,4 v{bar_h - 8} q0,4 -4,4 h-{max(w - 4, 0):.1f} z"/>'
            f'<text class="val" x="{left + w + 6:.1f}" y="{y + bar_h / 2}" dominant-baseline="central">{escape(unit.format(v))}</text>'
            "</g>"
        )
    out.append(f'<line class="axis" x1="{left}" x2="{left}" y1="0" y2="{height}"/></svg>')
    return "".join(out)


# --- build -------------------------------------------------------------------------------------------


def _q(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _jsonable(v: object) -> object:
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return str(v)


def build(db: Path, out: Path) -> Path:
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    origin_price = _q(
        con,
        """
        select origin_country, count(*) as n, median(price_per_100g) as med
        from dim_coffee where in_stock and not is_blend and not is_chain and price_per_100g is not null
          and origin_country is not null
        group by 1 having count(*) >= 5 order by n desc limit 12""",
    )
    processes = _q(
        con,
        """
        select coalesce(process, 'not stated') as process, count(*) as n
        from dim_coffee where in_stock and not is_blend group by 1 order by n desc""",
    )
    statuses = _q(con, "select status, count(*) as n from dim_roaster group by 1 order by n desc")
    # On the first run everything is "new"; only count coffees that appeared after the catalog's first day.
    new = _q(con, "select * from mart_new_arrivals limit 25")
    totals = _q(
        con,
        """
        select count(*) filter (where is_listed) as coffees, count(*) filter (where in_stock) as in_stock,
               count(distinct roaster_id) as roasters from dim_coffee where is_listed""",
    )[0]
    catalog = _q(
        con,
        """
        select coffee_id, roaster_name, roaster_state, title, url, origin_country, process, roast_level,
               tasting_notes, price_per_100g, min_price_usd, in_stock, first_seen
        from dim_coffee where is_listed and not is_chain order by roaster_name, title""",
    )
    con.close()

    coverage = {"registry": {r["status"]: r["n"] for r in statuses}}
    charts = {
        "origin_price": hbar_chart(
            [
                (
                    r["origin_country"],
                    round(r["med"], 2),
                    f"{r['origin_country']}: ${r['med']:.2f} per 100 g (median of {r['n']} coffees)",
                )
                for r in sorted(origin_price, key=lambda r: -r["med"])
            ],
            "${:.2f}",
            "Median price per 100 g by origin",
        ),
        "process": hbar_chart(
            [(r["process"], r["n"], f"{r['process']}: {r['n']} coffees") for r in processes],
            "{:,.0f}",
            "Coffees in stock by process",
        ),
    }
    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    html = env.get_template("index.html").render(
        totals=totals,
        charts=charts,
        new=new,
        coverage=coverage,
        origin_price=origin_price,
        processes=processes,
        generated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html)
    cols = list(catalog[0]) if catalog else []
    (out / "catalog.json").write_text(
        json.dumps(
            {"columns": cols, "rows": [[_jsonable(r[c]) for c in cols] for r in catalog]},
            separators=(",", ":"),
        )
    )
    return out / "index.html"
