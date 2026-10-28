"""Static site rendered from the dbt models in DuckDB: a launch page at the root (registry.coffee) that links out
to the catalog (catalog/index.html + catalog/catalog.json), the public Hugging Face dataset and the repo. Shots are
private and never appear here; they are only in the app."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from . import public
from .discover import US_STATES

# The 50 states; DC and Puerto Rico are named separately on the launch page.
STATES = [s for s in US_STATES if s not in ("DC", "PR")]
# Roaster statuses (dim_roaster.status) in reading order, with what each means to a visitor.
STATUS_LABELS = {
    "active": "Shop read, coffee found",
    "no_coffee": "Shop read, no coffee for sale",
    "no_catalog": "Shop read, nothing listed",
    "pending": "Not read yet",
    "unreachable": "Site down, or no shop we can read",
    "opted_out": "Opted out",
}

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


# --- links ---------------------------------------------------------------------------------------------

REPO = "https://github.com/jtouley/coffee-data-tracker"
# The legal pages' "Last updated" date: change it whenever privacy.html or terms.html changes, not on every build.
LEGAL_UPDATED = "October 27, 2026"


def links(public_export: str | None = None, site_url: str | None = None) -> dict[str, str | None]:
    """Where the launch page points. The dataset is the public export (`PUBLIC_EXPORT_URL`) when it's on Hugging
    Face; the lake itself is private by default, so it's never linked."""
    public_export = os.environ.get("PUBLIC_EXPORT_URL", "") if public_export is None else public_export
    site_url = (site_url or os.environ.get("SITE_URL") or "https://registry.coffee").rstrip("/")
    dataset = hf_path = None
    if public_export.startswith("hf://datasets/"):
        repo_id = public.hf_repo_id(public_export)
        dataset = f"https://huggingface.co/datasets/{repo_id}"
        hf_path = f"hf://datasets/{repo_id}/{public.HF_DATA_DIR}"
    return {
        "site": site_url,
        "catalog": "catalog/",
        "dataset": dataset,
        "hf_path": hf_path,
        "repo": REPO,
        "marketplace": f"{REPO}/blob/main/marketplace/README.md",
        "mcp": f"{REPO}#logging-shots",
        "add_roaster": f"{REPO}/issues/new?template=add-roaster.yml",
    }


# --- hardening ------------------------------------------------------------------------------------------


def safe_url(url: object) -> str | None:
    """Product links come from roasters' feeds; only plain http(s) links reach the page (no `javascript:`)."""
    return url if isinstance(url, str) and re.fullmatch(r"https?://[^\s\"<>]+", url, re.I) else None


def with_csp(html: str) -> str:
    """Add a Content-Security-Policy that allows the page's own inline scripts (by hash) and nothing third-party."""
    hashes = (
        " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(js.encode()).digest()).decode() + "'"
            for js in re.findall(r"<script>(.*?)</script>", html, re.S)
        )
        or "'none'"
    )
    csp = (
        f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'"
    )
    meta = (
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="referrer" content="strict-origin-when-cross-origin">'
    )
    if '<meta charset="utf-8">' not in html:
        raise ValueError('the page has no <meta charset="utf-8"> to put the Content-Security-Policy after')
    return html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n' + meta, 1)


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
        from dim_coffee where in_stock and not is_wholesale and not is_blend and not is_chain and price_per_100g is not null
          and origin_country is not null
        group by 1 having count(*) >= 5 order by n desc limit 12""",
    )
    processes = _q(
        con,
        """
        select coalesce(process, 'not stated') as process, count(*) as n
        from dim_coffee where in_stock and not is_wholesale and not is_blend group by 1 order by n desc""",
    )
    statuses = _q(con, "select status, count(*) as n from dim_roaster group by 1 order by n desc")
    # On the first run everything is "new"; only count coffees that appeared after the catalog's first day.
    new = _q(con, "select * from mart_new_arrivals limit 25")
    totals = _q(
        con,
        """
        select count(*) as coffees,
               count(*) filter (where in_stock) as in_stock,
               count(distinct roaster_id) as roasters
        from dim_coffee where is_listed and catalog_status <> 'archived' and not is_wholesale""",
    )[0]
    catalog = _q(
        con,
        """
        select coffee_id, roaster_name, roaster_state, title, url, origin_country, process, roast_level,
               tasting_notes, price_per_100g, min_price_usd, in_stock, catalog_status, first_seen
        from dim_coffee where is_listed and not is_wholesale and not is_chain order by roaster_name, title""",
    )
    reach = _q(
        con,
        """
        select count(distinct origin_country) as origins,
               count(distinct roaster_state) filter (where roaster_state in ({states})) as states,
               bool_or(roaster_state = 'DC') as has_dc, bool_or(roaster_state = 'PR') as has_pr
        from dim_coffee where is_listed and catalog_status <> 'archived' and not is_wholesale and not is_chain""".format(
            states=", ".join(f"'{s}'" for s in STATES)
        ),
    )[0]
    origin_counts = _q(
        con,
        """
        select origin_country, count(*) as n from dim_coffee
        where in_stock and not is_wholesale and not is_blend and not is_chain and origin_country is not null
        group by 1 order by n desc, 1 limit 8""",
    )
    week = _q(
        con,
        """
        select count(*) filter (where event = 'price') as price_changes,
               count(*) filter (where event = 'removed') as removed
        -- A left join: a product no longer classed as coffee has left dim_coffee, but its events still count.
        from fct_price_event left join dim_coffee using (coffee_id)
        where not coalesce(is_wholesale, false)
          and run_date > (select max(run_date) from fct_price_event) - interval 7 day""",
    )[0]
    for r in new + catalog:
        r["url"] = safe_url(r["url"])
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
        public_tables=len(public.PUBLIC_TABLES),
        generated=f"{generated:%B} {generated.day}, {generated:%Y}",
    )
    (out / "index.html").write_text(with_csp(landing))
    legal = {
        "links": links(),
        "updated": LEGAL_UPDATED,
        "contact": os.environ.get("SUPPORT_EMAIL") or "support@registry.coffee",
        "governing_state": os.environ.get("GOVERNING_STATE") or "Michigan",
    }
    for page in ("privacy", "terms"):
        (out / page).mkdir(exist_ok=True)
        (out / page / "index.html").write_text(with_csp(env.get_template(f"{page}.html").render(**legal)))
    cat = out / "catalog"
    cat.mkdir(exist_ok=True)
    html = env.get_template("index.html").render(
        totals=totals,
        status_rows=[(label, coverage["registry"].get(k, 0)) for k, label in STATUS_LABELS.items()],
        charts=charts,
        new=new,
        coverage=coverage,
        origin_price=origin_price,
        processes=processes,
        generated=generated.strftime("%Y-%m-%d %H:%M UTC"),
    )
    (cat / "index.html").write_text(with_csp(html))
    cols = list(catalog[0]) if catalog else []
    (cat / "catalog.json").write_text(
        json.dumps(
            {"columns": cols, "rows": [[_jsonable(r[c]) for c in cols] for r in catalog]},
            separators=(",", ":"),
        )
    )
    return out / "index.html"
