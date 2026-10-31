# Product-page fixtures for `tests/test_pages.py`

Synthesized, not recorded: no store was crawled to make them. `make_fixtures.py` builds them from the recorded JSON
fixtures one level up, so the page path can be checked against the JSON adapter on the same products.

- `ccc_*.html`, `ccc_sitemap*.xml`: Counter Culture's 12 products from `shopify_counterculturecoffee.json`, in the
  layout of Shopify's Dawn theme (the analytics `var meta`, JSON-LD with one offer per variant). `worka-chelbessa`
  also embeds a theme product JSON. Descriptions are the real ones; the spec lines (Origin, Process...) on three
  pages are invented to exercise the fact-line parser.
- `woo_simple.html`: the first product of `woocommerce_anaheimcoffee.json`, WooCommerce layout. `woo_variable.html`:
  a made-up variable product with a variation form.
- `challenge.html`: a bot-challenge interstitial.

Regenerate: `python3 -I tests/fixtures/pages/make_fixtures.py tests/fixtures`.
