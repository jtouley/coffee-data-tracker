"""Static site rendered from the dbt models in DuckDB: a launch page at the root (registry.coffee) that links out
to the catalog app (app/index.html + app/catalog.json), the public Hugging Face dataset and the repo."""

from __future__ import annotations

import json
import os
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


def scatter_chart(points: list[tuple[float, float, str]], width: int = 460, height: int = 240) -> str:
    """Brew ratio (x) vs rating (y)."""
    if len(points) < 2:
        return '<p class="empty">Log a few shots to see how ratio relates to your ratings.</p>'
    left, bottom, top, right = 40, 32, 12, 16
    xs = [p[0] for p in points]
    x0, x1 = min(1.0, min(xs)), max(3.5, max(xs))
    sx = lambda x: left + (x - x0) / (x1 - x0) * (width - left - right)  # noqa: E731
    sy = lambda y: top + (5.5 - y) / 5 * (height - top - bottom)  # noqa: E731
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Rating by brew ratio">']
    for r in range(1, 6):
        out.append(
            f'<line class="grid" x1="{left}" x2="{width - right}" y1="{sy(r):.1f}" y2="{sy(r):.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{sy(r):.1f}" text-anchor="end" dominant-baseline="central">{r}</text>'
        )
    t = x0
    while t <= x1 + 1e-9:
        out.append(
            f'<text class="tick" x="{sx(t):.1f}" y="{height - 10}" text-anchor="middle">1:{t:.1f}</text>'
        )
        t += 0.5
    for x, y, tip in points:
        out.append(
            f'<g class="mark" tabindex="0" data-tip="{escape(tip)}"><circle class="hit" cx="{sx(x):.1f}" '
            f'cy="{sy(y):.1f}" r="12"/><circle class="dot" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5"/></g>'
        )
    out.append("</svg>")
    return "".join(out)


# --- links ---------------------------------------------------------------------------------------------

REPO = "https://github.com/jtouley/coffee-data-tracker"


def links(public_export: str | None = None, site_url: str | None = None) -> dict[str, str | None]:
    """Where the launch page points. The dataset is the public export (`PUBLIC_EXPORT_URL`) when it's on Hugging
    Face; the lake itself is private by default, so it's never linked."""
    public_export = os.environ.get("PUBLIC_EXPORT_URL", "") if public_export is None else public_export
    site_url = (site_url or os.environ.get("SITE_URL") or "https://registry.coffee").rstrip("/")
    dataset = hf_path = None
    if public_export.startswith("hf://datasets/"):
        repo_id = public_export.removeprefix("hf://datasets/").split("@")[0].strip("/")
        dataset = f"https://huggingface.co/datasets/{repo_id}"
        hf_path = f"hf://datasets/{repo_id}/data"
    return {
        "site": site_url,
        "app": "app/",
        "dataset": dataset,
        "hf_path": hf_path,
        "repo": REPO,
        "marketplace": f"{REPO}/blob/main/marketplace/README.md",
        "mcp": f"{REPO}#logging-shots",
        "add_roaster": f"{REPO}/issues/new?template=add-roaster.yml",
    }


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
    shots = _q(
        con,
        """
        select brew_ratio, rating, coalesce(coffee_title, coffee_freetext, '?') as coffee, time_s, brewed_at
        from fct_shot order by brewed_at desc""",
    )
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
    reach = _q(
        con,
        """
        select count(distinct origin_country) as origins, count(distinct roaster_state) as states
        from dim_coffee where is_listed and not is_chain""",
    )[0]
    origin_counts = _q(
        con,
        """
        select origin_country, count(*) as n from dim_coffee
        where in_stock and not is_blend and not is_chain and origin_country is not null
        group by 1 order by n desc, 1 limit 8""",
    )
    week = _q(
        con,
        """
        select count(*) filter (where event = 'price') as price_changes,
               count(*) filter (where event = 'removed') as removed
        from fct_price_event where run_date > (select max(run_date) from fct_price_event) - interval 7 day""",
    )[0]
    new_count = _q(con, "select count(*) as n from mart_new_arrivals")[0]["n"]
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
        "shots": scatter_chart(
            [
                (
                    r["brew_ratio"],
                    r["rating"],
                    f"{r['coffee']}: 1:{r['brew_ratio']:.1f}, {r['time_s']:g} s, rated {r['rating']}/5",
                )
                for r in shots
                if r["brew_ratio"]
            ]
        ),
        "origins": hbar_chart(
            [
                (r["origin_country"], r["n"], f"{r['origin_country']}: {r['n']} coffees in stock")
                for r in origin_counts
            ],
            "{:,.0f}",
            "Single-origin coffees in stock, by origin",
        ),
    }
    generated = datetime.now(UTC)
    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    out.mkdir(parents=True, exist_ok=True)
    landing = env.get_template("landing.html").render(
        totals=totals,
        registry=sum(coverage["registry"].values()),
        reach=reach,
        week=week | {"new": new_count},
        new=new[:6],
        charts=charts,
        links=links(),
        generated=generated.strftime("%B %-d, %Y"),
    )
    (out / "index.html").write_text(landing)
    app = out / "app"
    app.mkdir(exist_ok=True)
    html = env.get_template("index.html").render(
        totals=totals,
        charts=charts,
        new=new,
        shots=shots[:15],
        coverage=coverage,
        origin_price=origin_price,
        processes=processes,
        generated=generated.strftime("%Y-%m-%d %H:%M UTC"),
    )
    (app / "index.html").write_text(html)
    cols = list(catalog[0]) if catalog else []
    (app / "catalog.json").write_text(
        json.dumps(
            {"columns": cols, "rows": [[_jsonable(r[c]) for c in cols] for r in catalog]},
            separators=(",", ":"),
        )
    )
    return out / "index.html"
