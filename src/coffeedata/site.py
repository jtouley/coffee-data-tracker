"""Static site rendered from the dbt models in DuckDB: a launch page at the root (registry.coffee) that links out
to the catalog (catalog/index.html + catalog/catalog.json), the public Hugging Face dataset and the repo. Shots are
private and never appear here; they are only in the app.

Only the public roasters (dbt model `public_roaster`: not opted out, not a chain, and not known only from
OpenStreetMap) and their coffees appear, except in the roaster status counts and the registry total, which
count every roaster in dim_roaster. The landing page and the catalog carry a schema.org Dataset (JSON-LD, built
from the public contract) for search engines, and the site has a robots.txt and a sitemap.xml."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from . import public
from .discover import STATE_NAMES, US_STATES
from .http import USER_AGENT

# The 50 states; DC and Puerto Rico are named separately on the launch page.
STATES = [s for s in US_STATES if s not in ("DC", "PR")]
# The catalog's State filter and search: each code's name as shown ("District of Columbia"), and every name the search
# reads as a state, longest first so "west virginia" is read before "virginia".
STATE_TITLES = {
    code: " ".join(w if w == "of" else w.capitalize() for w in name.split())
    for name, code in STATE_NAMES.items()
    if name != "washington dc"
}
STATE_WORDS = sorted(STATE_NAMES.items(), key=lambda kv: (-len(kv[0]), kv[0]))
# Roaster statuses (dim_roaster.status) in reading order, with what each means to a visitor.
STATUS_LABELS = {
    "active": "Shop read, coffee found",
    "no_coffee": "Shop read, no coffee for sale",
    "no_catalog": "Shop read, nothing listed",
    "pending": "Not read yet",
    "alias": "Same shop as another roaster listed here",
    "unreachable": "Site down, or no shop we can read",
    "opted_out": "Opted out",
}

# Which roasters the site shows: the public roasters (public_roaster: not opted out, not a chain, not known only
# from OpenStreetMap). Chains are still crawled but left out of what's published: public_roaster decides that for
# every public table (public_market_weekly included) and so for the site; the is_chain filter below repeats it, so
# INCLUDE_CHAINS can no longer bring chains back on its own. Every count, chart, list and the catalog read
# site_roaster or site_coffee. Temporary views: the database itself stays read-only.
INCLUDE_CHAINS = False
SITE_VIEWS = (
    "create temp view site_roaster as select roaster_id from public_roaster"
    + (
        ""
        if INCLUDE_CHAINS
        else " where roaster_id not in (select roaster_id from dim_roaster where is_chain)"
    ),
    "create temp view site_coffee as select * from dim_coffee where roaster_id in (select roaster_id from site_roaster)",
)
# Every count and chart on the site uses one of two definitions over site_coffee, computed from this build's data
# (never written into a template or a page by hand; tests/test_stated_counts.py fails if one comes back):
# - LISTED, the current retail catalog: listed, not the roaster's archived back catalog, not wholesale. The headline
#   coffees, in stock and roasters, and the states. The same filter as int_current_catalog less wholesale, which
#   mart_build_profile's listed_coffees and roasters_with_coffee count.
# - SINGLE_ORIGIN: LISTED, not a blend, origin known. The origin count and all three charts (in stock by origin, by
#   process, and the median price by origin). Over the same roasters it selects public_market_weekly's coffees
#   (tests/test_dbt.py checks the two cell for cell).
LISTED = "is_listed and catalog_status <> 'archived' and not is_wholesale"
SINGLE_ORIGIN = f"{LISTED} and not is_blend and origin_country is not null"

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
APP = "https://app.registry.coffee"  # the shot-logging app; APP_URL overrides it for a fork or staging
# The legal pages' "Last updated" date: change it whenever privacy.html or terms.html changes, not on every build.
LEGAL_UPDATED = (
    "November 8, 2026"  # email, marketing and recommendations (spec §7.4); the owner sets the deploy day
)
# The crawler page's (bot.html) "Last updated" date: change it whenever that page changes.
BOT_UPDATED = "November 2, 2026"
FIRST_RUN = "2026-10-26"  # the first weekly run the public data covers (the start of temporalCoverage)
CREATOR = {"@type": "Person", "name": "Jason Touleyrou", "url": "https://github.com/jtouley"}
PAGES = ("", "catalog/", "privacy/", "terms/", "bot/")  # every page the site writes, for sitemap.xml


def links(public_export: str | None = None, site_url: str | None = None) -> dict[str, str | None]:
    """Where the launch page points. The dataset is the public export (`PUBLIC_EXPORT_URL`) when it's on Hugging
    Face; the lake itself is private by default, so it's never linked. Beta requests are an email to the support
    address: the static site collects nothing itself."""
    public_export = os.environ.get("PUBLIC_EXPORT_URL", "") if public_export is None else public_export
    site_url = (site_url or os.environ.get("SITE_URL") or "https://registry.coffee").rstrip("/")
    app_url = (os.environ.get("APP_URL") or APP).rstrip("/")
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
        "data_license": f"{REPO}/blob/main/licenses/CDLA-Permissive-2.0.txt",
        "marketplace": f"{REPO}/blob/main/marketplace/README.md",
        "mcp": f"{REPO}#logging-shots",
        "add_roaster": f"{REPO}/issues/new?template=add-roaster.yml",
        "signin": f"{app_url}/signin",
        "beta": f"mailto:{support_email()}?subject=registry.coffee%20beta%20invite",
    }


def support_email() -> str:
    return os.environ.get("SUPPORT_EMAIL") or "support@registry.coffee"


# The favicon and logo images (templates/static/, under templates/ so site.yml's deploy trigger covers them), served
# from the site root as they are.
STATIC = Path(__file__).parent / "templates" / "static"


def copy_static(out: Path) -> None:
    shutil.copytree(STATIC, out, dirs_exist_ok=True)


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


# --- findability: schema.org Dataset, robots.txt, sitemap.xml -------------------------------------------


def dataset_jsonld(release: dict, link: dict[str, str | None], contract: dict | None = None) -> dict:
    """The schema.org Dataset (Google Dataset Search's markup) for the public tables, from the public contract:
    name and description, the license, coverage, every contract column as a variable measured, and where to get
    the data (the site's catalog JSON; the Hugging Face dataset when the public export goes there). The release
    gives dateModified (its run date) and version (its build id); both are left out for a build with no release."""
    c = contract or public.contract()
    desc = c.get("description") or {}
    site = str(link["site"])
    variables: dict[str, dict] = {}
    for table in c["schema"]:
        for prop in table.get("properties") or []:
            if prop["name"] in variables:
                continue
            v = {"@type": "PropertyValue", "name": prop["name"]}
            if prop.get("description"):
                v["description"] = " ".join(str(prop["description"]).split())
            unit = {p.get("property"): p.get("value") for p in prop.get("customProperties") or []}.get("unit")
            if unit:
                v["unitText"] = str(unit)
            variables[prop["name"]] = v
    distribution = [
        {
            "@type": "DataDownload",
            "name": "catalog.json",
            "description": "The site's catalog: every listed retail coffee from the public roasters, as JSON "
            "(a subset of public_coffee's columns, plus roaster_city: the roaster's city from public_roaster).",
            "encodingFormat": "application/json",
            "contentUrl": f"{site}/catalog/catalog.json",
        }
    ]
    same_as = []
    if link.get("dataset"):
        base = str(link["dataset"])
        same_as.append(base)
        distribution += [
            {
                "@type": "DataDownload",
                "name": f"{t}.parquet",
                "encodingFormat": "application/vnd.apache.parquet",
                "contentUrl": f"{base}/resolve/main/{public.HF_DATA_DIR}/{t}.parquet",
            }
            for t in public.PUBLIC_TABLES
        ]
    doc: dict = {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "name": c["name"],
        "alternateName": "registry.coffee",
        "description": " ".join(
            " ".join(str(desc.get(k) or "") for k in ("purpose", "usage", "limitations")).split()
        ),
        "url": f"{site}/",
        "sameAs": same_as,
        "license": public.DATA_LICENSE["url"],
        "isAccessibleForFree": True,
        "creator": CREATOR,
        "keywords": [str(t) for t in c.get("tags") or []]
        or ["coffee", "specialty coffee", "coffee prices", "coffee roasters", "United States", "market data"],
        "temporalCoverage": f"{FIRST_RUN}/..",
        "spatialCoverage": {"@type": "Place", "name": "United States"},
        "isBasedOn": {
            "@type": "CreativeWork",
            "name": "Overture Maps Places",
            "url": "https://overturemaps.org/",
        },
        "variableMeasured": list(variables.values()),
        "distribution": distribution,
        "dateModified": release.get("release_run_date"),
        "version": release.get("release_build_id"),
    }
    return {k: v for k, v in doc.items() if v not in (None, [], "")}


def jsonld_script(doc: dict) -> Markup:
    """A `<script type="application/ld+json">` block. It is data, not script: the browser never runs it, so the
    CSP needn't (and with_csp doesn't) hash it. `<`, `>` and `&` are escaped so no text in it can close the tag."""
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Markup(f'<script type="application/ld+json">{text}</script>')


def robots_txt(site: str) -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {site}/sitemap.xml\n"


def sitemap_xml(site: str, lastmod: dict[str, str]) -> str:
    urls = "".join(
        f"  <url><loc>{escape(site + '/' + page)}</loc>"
        + (f"<lastmod>{lastmod[page]}</lastmod>" if page in lastmod else "")
        + "</url>\n"
        for page in PAGES
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n'
    )


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
    for view in SITE_VIEWS:
        con.execute(view)
    origin_price = _q(
        con,
        f"""
        select origin_country, count(*) as n, median(price_per_100g) as med
        from site_coffee where {SINGLE_ORIGIN} and in_stock and price_per_100g is not null
        group by 1 having count(*) >= 5 order by n desc limit 12""",
    )
    processes = _q(
        con,
        f"""
        select coalesce(process, 'not stated') as process, count(*) as n
        from site_coffee where {SINGLE_ORIGIN} and in_stock group by 1 order by n desc""",
    )
    statuses = _q(con, "select status, count(*) as n from dim_roaster group by 1 order by n desc")
    # On the first run everything is "new"; only count coffees that appeared after the catalog's first day.
    # The filter doesn't keep the mart's order, so it is restated (the model's own order).
    public_new = "select * from mart_new_arrivals where coffee_id in (select coffee_id from site_coffee)"
    new = _q(con, f"{public_new} order by first_seen desc, roaster_name, title, coffee_id limit 25")
    totals = _q(
        con,
        f"""
        select count(*) as coffees,
               count(*) filter (where in_stock) as in_stock,
               count(distinct roaster_id) as roasters
        from site_coffee where {LISTED}""",
    )[0]
    catalog = _q(
        con,
        """
        select c.coffee_id, c.roaster_name, c.roaster_state, c.title, c.url, c.origin_country, c.process,
               c.roast_level, c.tasting_notes, c.price_per_100g, c.min_price_usd, c.in_stock, c.catalog_status,
               c.first_seen,
               -- Added after main's columns, so readers that look columns up by position or name keep working:
               -- the stable roaster key, and the roaster's city as public_roaster stores it.
               c.roaster_id, r.city as roaster_city
        from site_coffee c join public_roaster r using (roaster_id)
        where c.is_listed and not c.is_wholesale order by c.roaster_name, c.title""",
    )
    # The states are the listed coffees' roasters' (the same roasters as totals.roasters); the origins are the
    # single-origin listings' countries.
    reach = (
        _q(
            con,
            """
        select count(distinct roaster_state) filter (where roaster_state in ({states})) as states,
               bool_or(roaster_state = 'DC') as has_dc, bool_or(roaster_state = 'PR') as has_pr
        from site_coffee where {listed}""".format(states=", ".join(f"'{s}'" for s in STATES), listed=LISTED),
        )[0]
        | _q(con, f"select count(distinct origin_country) as origins from site_coffee where {SINGLE_ORIGIN}")[
            0
        ]
    )
    origin_counts = _q(
        con,
        f"""
        select origin_country, count(*) as n from site_coffee where {SINGLE_ORIGIN} and in_stock
        group by 1 order by n desc, 1 limit 8""",
    )
    week = _q(
        con,
        """
        select count(*) filter (where event = 'price') as price_changes,
               count(*) filter (where event = 'removed') as removed
        -- A left join: a product no longer classed as coffee has left dim_coffee, but its events still count.
        from fct_price_event e left join dim_coffee c using (coffee_id)
        where not coalesce(c.is_wholesale, false) and e.roaster_id in (select roaster_id from site_roaster)
          and run_date > (select max(run_date) from fct_price_event) - interval 7 day""",
    )[0]
    for r in new + catalog:
        r["url"] = safe_url(r["url"])
    new_count = _q(con, f"select count(*) as n from ({public_new})")[0]["n"]
    last_fetch = _q(con, "select max(last_fetch) as t from dim_roaster")[0]["t"]
    release = public.describe(con)
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
            [(r["process"], r["n"], f"{r['process']}: {r['n']:,} coffees in stock") for r in processes],
            "{:,.0f}",
            "Single-origin coffees in stock by process",
        ),
        "origins": hbar_chart(
            [
                (r["origin_country"], r["n"], f"{r['origin_country']}: {r['n']:,} coffees in stock")
                for r in origin_counts
            ],
            "{:,.0f}",
            "Single-origin coffees in stock, by origin",
        ),
    }
    # "Updated" is when the data was last read, not when the pages were rendered: a template-only redeploy
    # (.github/workflows/site.yml) mustn't make the catalog look fresher than it is.
    generated = last_fetch.astimezone(UTC) if last_fetch else datetime.now(UTC)
    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    out.mkdir(parents=True, exist_ok=True)
    link = links()
    jsonld = jsonld_script(dataset_jsonld(release, link))
    landing = env.get_template("landing.html").render(
        totals=totals,
        registry=sum(coverage["registry"].values()),
        reach=reach,
        week=week | {"new": new_count},
        new=new[:6],
        charts=charts,
        links=link,
        public_tables=len(public.PUBLIC_TABLES),
        generated=f"{generated:%B} {generated.day}, {generated:%Y}",
        jsonld=jsonld,
    )
    (out / "index.html").write_text(with_csp(landing))
    copy_static(out)
    legal = {
        "links": links(),
        "updated": LEGAL_UPDATED,
        "contact": support_email(),
        "governing_state": os.environ.get("GOVERNING_STATE") or "Michigan",
    }
    for page in ("privacy", "terms"):
        (out / page).mkdir(exist_ok=True)
        (out / page / "index.html").write_text(with_csp(env.get_template(f"{page}.html").render(**legal)))
    # /bot: the page RegistryCoffeeBot's User-Agent links to (who we are, what we crawl, how to opt out)
    (out / "bot").mkdir(exist_ok=True)
    bot = env.get_template("bot.html").render(**legal | {"updated": BOT_UPDATED, "user_agent": USER_AGENT})
    (out / "bot" / "index.html").write_text(with_csp(bot))
    cat = out / "catalog"
    cat.mkdir(exist_ok=True)
    cols = list(catalog[0]) if catalog else []
    data = json.dumps(
        {
            # Which release this is and under what licence; the page's script reads only columns and rows.
            "release": {
                "release_build_id": release["release_build_id"],
                "release_run_date": release["release_run_date"],
                "contract": f"{release['contract']['id']}@{release['contract']['version']}",
                "license": release["license"]["id"],
                "license_url": release["license"]["url"],
            },
            "columns": cols,
            "rows": [[_jsonable(r[c]) for c in cols] for r in catalog],
        },
        separators=(",", ":"),
    )
    (cat / "catalog.json").write_text(data)
    html = env.get_template("index.html").render(
        links=link,
        jsonld=jsonld,
        totals=totals,
        status_rows=[(label, coverage["registry"].get(k, 0)) for k, label in STATUS_LABELS.items()],
        charts=charts,
        new=new,
        coverage=coverage,
        origin_price=origin_price,
        processes=processes,
        generated=generated.strftime("%Y-%m-%d %H:%M UTC"),
        # The page fetches catalog.json?b=<a hash of the file>: a new page never reads a cached older file, even
        # after a code-only redeploy of the same release. (Pages ignores the query, so a cached older page gets the
        # current file; that is safe because the file only ever gains columns.)
        data_key=hashlib.sha256(data.encode()).hexdigest()[:12],
        state_titles=STATE_TITLES,
        state_words=STATE_WORDS,
    )
    (cat / "index.html").write_text(with_csp(html))
    site = str(link["site"])
    (out / "robots.txt").write_text(robots_txt(site))
    day = f"{generated:%Y-%m-%d}"
    legal_day = f"{datetime.strptime(LEGAL_UPDATED, '%B %d, %Y'):%Y-%m-%d}"
    bot_day = f"{datetime.strptime(BOT_UPDATED, '%B %d, %Y'):%Y-%m-%d}"
    (out / "sitemap.xml").write_text(
        sitemap_xml(
            site, {"": day, "catalog/": day, "privacy/": legal_day, "terms/": legal_day, "bot/": bot_day}
        )
    )
    return out / "index.html"
