# Green-importer offer lists (private)

**Backend-only: for internal use and a future roaster portal.** Never public. Nothing here enters the public release,
the `public_*` tables, the site or the public dataset, and dbt never reads it (`tests/test_importers.py` checks this).
Even in the roaster portal, only the normalized values may be shown, never the importer's wording.

Weekly snapshots of green-coffee importers' public offer lists, written by `coffeedata importer-snapshot`
(`src/coffeedata/importers.py`, run by `.github/workflows/importers.yml`): one pass a week, one public page per
importer, never logged in, robots.txt honored.

## Layout

Raw only: one immutable file per importer per week, and a week already stored is never fetched again.

| Path | What it holds |
|---|---|
| `raw/week=YYYY-MM-DD/importer=<name>.parquet` | Each lot word for word, as the page had it that week: every cell of its row under the page's header (null when empty), and its own page's URL |

`week` is the Monday of the fetch's ISO week; `fetched_at` is when the page was fetched. Columns about prices are
left out by their header (price, cost, FOB, differential), and a price in any kept cell (a currency amount, an
amount per weight, a labelled price) fails the run. No page HTML or images either.

## Derived, never stored

The stated fields (lot ID, name, origin, region, process, status, estimated arrival, warehouse, bags, bag size) and
our normalized values (process type, varieties, altitude, harvest months, grower type and name, certifications
claimed and those the page marks absent) are
derived from this raw layer whenever they are read (`importers.offers()`), so a better rule re-derives every week.
The rules (`src/coffeedata/importer_rules.py`) leave a value empty when the text doesn't settle it, and haven't been
evaluated yet.
