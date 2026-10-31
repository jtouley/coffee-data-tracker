"""Synthesize product-page fixtures from the recorded JSON fixtures (Shopify Dawn / WooCommerce layouts)."""

import html
import json
import sys
from pathlib import Path

FIX = Path(sys.argv[1])
OUT = FIX / "pages"
host = "counterculturecoffee.com"
ccc = json.loads((FIX / "shopify_counterculturecoffee.json").read_text())["products"]
# Spec lines a product page shows next to the description (invented for the parser; the description is real).
SPECS = {
    "finca-el-puente-honey": [
        ("Origin", "Marcala, Honduras"),
        ("Process", "Honey"),
        ("Variety", "Catuai, Lempira"),
        ("Tasting Notes", "Peach, Honey, Black Tea"),
    ],
    "worka-chelbessa": [
        ("Region", "Gedeb, Ethiopia"),
        ("Process", "Washed"),
        ("Variety", "Heirloom"),
        ("Elevation", "1,950 - 2,100 MASL"),
    ],
    "el-conacaste-java-variety": [
        ("Origin", "El Salvador"),
        ("Process", "Washed"),
        ("Variety", "Java"),
        ("Notes", "Brown Sugar, Toffee"),
    ],
}


def page(p, layout):
    url = f"https://{host}/products/{p['handle']}"
    offers = [
        {
            "@type": "Offer",
            "availability": "http://schema.org/" + ("InStock" if v["available"] else "OutOfStock"),
            "price": v["price"],
            "priceCurrency": "USD",
            "sku": v.get("sku") or "",
            "url": f"{url}?variant={v['id']}",
        }
        for v in p["variants"]
    ]
    ld = {
        "@context": "http://schema.org/",
        "@type": "Product",
        "name": p["title"],
        "url": url,
        "image": [f"https://{host}/cdn/shop/files/{p['handle']}.jpg"],
        "description": html.unescape(p["body_html"])[:200],
        "brand": {"@type": "Brand", "name": p["vendor"]},
        "offers": offers,
    }
    meta = {
        "product": {
            "id": p["id"],
            "gid": f"gid://shopify/Product/{p['id']}",
            "vendor": p["vendor"],
            "type": p["product_type"],
            "handle": p["handle"],
            "variants": [
                {
                    "id": v["id"],
                    "price": round(float(v["price"]) * 100),
                    "name": p["title"] + ("" if v["title"] == "Default Title" else " - " + v["title"]),
                    "public_title": None if v["title"] == "Default Title" else v["title"],
                    "sku": v.get("sku") or "",
                }
                for v in p["variants"]
            ],
            "remote": False,
        },
        "page": {"pageType": "product", "resourceType": "product", "resourceId": p["id"]},
    }
    theme = ""
    if layout == "theme_json":  # some themes embed the full /products/<handle>.js object
        full = {
            "id": p["id"],
            "title": p["title"],
            "handle": p["handle"],
            "description": p["body_html"],
            "published_at": p["published_at"],
            "vendor": p["vendor"],
            "type": p["product_type"],
            "tags": p["tags"],
            "price": round(float(p["variants"][0]["price"]) * 100),
            "available": any(v["available"] for v in p["variants"]),
            "variants": [
                {
                    "id": v["id"],
                    "title": v["title"],
                    "public_title": None if v["title"] == "Default Title" else v["title"],
                    "available": v["available"],
                    "price": round(float(v["price"]) * 100),
                    "grams": v["grams"],
                    "sku": v.get("sku"),
                }
                for v in p["variants"]
            ],
            "images": [f"//{host}/cdn/shop/files/{p['handle']}.jpg"],
        }
        theme = f'<script type="application/json" data-product-json id="ProductJson-main">{json.dumps(full)}</script>'
    specs = "".join(
        f'<div class="spec"><dt>{k}</dt><dd>{v}</dd></div>' for k, v in SPECS.get(p["handle"], [])
    )
    return f"""<!doctype html><html class="no-js" lang="en"><head><meta charset="utf-8">
<title>{html.escape(p["title"])} &ndash; Counter Culture Coffee</title>
<meta property="og:image" content="https://{host}/cdn/shop/files/{p["handle"]}.jpg">
<script>window.ShopifyAnalytics = window.ShopifyAnalytics || {{}};
var meta = {json.dumps(meta)};
for (var attr in meta) {{ window.ShopifyAnalytics.meta[attr] = meta[attr]; }}</script>
</head><body class="template-product">
<main id="MainContent"><product-info>
<img src="//{host}/cdn/shop/files/{p["handle"]}.jpg" alt="">
<h1 class="product__title">{html.escape(p["title"])}</h1>
<div class="price">${p["variants"][0]["price"]}</div>
<div class="product__description rte">{p["body_html"]}</div>
<dl class="product-specs">{specs}</dl>
{theme}
<script type="application/ld+json">{json.dumps(ld)}</script>
</product-info></main></body></html>
"""


for p in ccc:
    (OUT / f"ccc_{p['handle']}.html").write_text(
        page(p, "theme_json" if p["handle"] == "worka-chelbessa" else "dawn")
    )
(OUT / "ccc_sitemap.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://{host}/sitemap_products_1.xml?from=7610173063245&amp;to=15422020026445</loc></sitemap>
<sitemap><loc>https://{host}/sitemap_pages_1.xml</loc></sitemap>
<sitemap><loc>https://{host}/sitemap_collections_1.xml</loc></sitemap>
<sitemap><loc>https://{host}/sitemap_blogs_1.xml</loc></sitemap>
</sitemapindex>
""")
urls = "".join(
    f"""<url><loc>https://{host}/products/{p["handle"]}</loc><lastmod>{p["updated_at"]}</lastmod><changefreq>daily</changefreq>
<image:image><image:loc>https://cdn.shopify.com/s/files/{p["handle"]}.jpg</image:loc><image:title>{html.escape(p["title"])}</image:title></image:image></url>
"""
    for p in ccc
)
(OUT / "ccc_sitemap_products_1.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
<url><loc>https://{host}/</loc><changefreq>daily</changefreq></url>
{urls}</urlset>
""")
# WooCommerce: a simple product from the recorded Store API fixture, and a variable product.
woo = json.loads((FIX / "woocommerce_anaheimcoffee.json").read_text())
p = woo[0]
ld = {
    "@context": "https://schema.org/",
    "@graph": [
        {"@type": "BreadcrumbList", "itemListElement": []},
        {
            "@type": "Product",
            "@id": p["permalink"] + "#product",
            "name": html.unescape(p["name"]),
            "url": p["permalink"],
            "description": "A long description.",
            "sku": p.get("sku") or "",
            "offers": [
                {
                    "@type": "Offer",
                    "price": f"{int(p['prices']['price']) / 100:.2f}",
                    "priceCurrency": "USD",
                    "availability": "http://schema.org/InStock",
                    "url": p["permalink"],
                }
            ],
        },
    ],
}
(OUT / "woo_simple.html").write_text(f"""<!DOCTYPE html><html lang="en-US"><head>
<link rel='shortlink' href='https://anaheimcoffee.com/?p={p["id"]}' />
<script type="application/ld+json">{json.dumps(ld)}</script></head>
<body class="product-template-default single single-product postid-{p["id"]} woocommerce">
<h1 class="product_title entry-title">{p["name"]}</h1>
<div class="woocommerce-product-details__short-description"><p>Origin: Chiapas, Mexico</p><p>Roast: Medium</p><p>We roast this one with care, and it is lovely.</p></div>
</body></html>
""")
variations = [
    {
        "attributes": {"attribute_pa_size": "12 oz"},
        "display_price": 18,
        "is_in_stock": True,
        "variation_id": 901,
    },
    {
        "attributes": {"attribute_pa_size": "5 lb"},
        "display_price": 70,
        "is_in_stock": False,
        "variation_id": 902,
    },
]
ld2 = {
    "@context": "https://schema.org/",
    "@type": "Product",
    "name": "Ethiopia Guji Natural",
    "sku": "ETH-GUJI",
    "offers": [
        {
            "@type": "AggregateOffer",
            "lowPrice": "18.00",
            "highPrice": "70.00",
            "offerCount": 2,
            "priceCurrency": "USD",
            "availability": "http://schema.org/InStock",
        }
    ],
}
(OUT / "woo_variable.html").write_text(f"""<!DOCTYPE html><html lang="en-US"><head>
<script type="application/ld+json">{json.dumps(ld2)}</script></head>
<body class="product-template-default single single-product postid-900 woocommerce">
<h1 class="product_title entry-title">Ethiopia Guji Natural</h1>
<form class="variations_form cart" data-product_id="900" data-product_variations="{html.escape(json.dumps(variations))}"></form>
<table class="woocommerce-product-attributes shop_attributes"><tr><th>Process</th><td><p>Natural</p></td></tr><tr><th>Tasting Notes</th><td><p>Blueberry, Jasmine</p></td></tr></table>
</body></html>
""")
(
    OUT / "challenge.html"
).write_text("""<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>
<body><noscript>Enable JavaScript and cookies to continue</noscript>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>
""")
