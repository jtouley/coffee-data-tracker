# Green-importer offer lists (private)

**Backend-only: for internal use and a future roaster portal.** Never public. Nothing here enters the public release,
the `public_*` tables, the site or the public dataset, and dbt never reads it (`tests/test_importers.py` checks this).
Even in the roaster portal, only the normalized values may be shown, never the importer's wording.

Weekly snapshots of green-coffee importers' public offer lists, written by `coffeedata importer-snapshot`
(`src/coffeedata/importers.py`, run by `.github/workflows/importers.yml`): one pass a week, one public page per
importer (Royal Coffee: its product listing, 250 products a page, spaced), never logged in, robots.txt honored.

## Layout

Raw: one immutable file per importer per week, and a week already stored is never fetched again. Derived: one file,
the latest build's offers, replaced at each build. Nothing else is ever written here.

| Path | What it holds |
|---|---|
| `raw/week=YYYY-MM-DD/importer=<name>.parquet` | Each lot word for word, as the page had it that week: every cell of its row under the page's header (null when empty), and its own page's URL |
| `raw/week=YYYY-MM-DD/importer=royal_coffee.parquet` | Royal Coffee (a Shopify store's public product listing): each product word for word under its field name (`id`, `title`, `handle`, `vendor`, `product_type`, `tags`, `published_at`, `body_text` from `body_html`, and `options` and `variants` as JSON without prices or images), and its product page's URL. A field that mentions a price is left out of that product. Fetched only by the scheduled workflow's first attempt, never by an AI agent |
| `derived/importer_offers.parquet` | The latest build's `importer_offers` (below): every stored lot of every week with its stated fields and our normalized values, derived from `raw/` by the private dbt project. Replaced at each weekly build that passes; never an input to anything |

`week` is the Monday of the fetch's ISO week; `fetched_at` is when the page was fetched. Columns about prices are
left out by their header (price, cost, FOB, differential), and a price in any kept cell (a currency amount, an
amount per weight, a labelled price) fails the run. No page HTML or images either.

## Derived

The stated fields (lot ID, name, origin, region, process, status, estimated arrival, warehouse, bags, bag size; for
InterAmerican also the grade and screen its product names state) and our normalized values (process type,
varieties, altitude, harvest months, grower type and name, certifications claimed and those the page marks absent)
are derived from this raw layer by a private dbt project (`dbt_importers/`, `coffeedata importer-build`) into a
database file of its own, each build from the whole raw history, so a better rule re-derives every week. After each
weekly snapshot that passes, the workflow keeps the latest build's offers here, in `derived/importer_offers.parquet`, replacing
the one before (the dataset's history keeps earlier versions); a build that fails leaves it as it was. No build reads
it back, and nothing public reads it or the database file. The rules (SQL models over the vocabulary in
`src/coffeedata/importer_rules.py`) leave a value empty when the text doesn't settle it, and haven't been evaluated
yet: nothing should rely on the derived values.
