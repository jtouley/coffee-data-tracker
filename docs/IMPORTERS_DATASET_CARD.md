# Green-importer offer lists (private)

**Backend-only: for internal use and a future roaster portal.** Never public. Nothing here enters the public release,
the `public_*` tables, the site or the public dataset, and dbt never reads it (`tests/test_importers.py` checks this).
Even in the roaster portal, only the normalized values may be shown, never the importer's wording.

Weekly snapshots of green-coffee importers' public offer lists, written by `coffeedata importer-snapshot`
(`src/coffeedata/importers.py`, run by `.github/workflows/importers.yml`): one pass a week, one public page per
importer, never logged in, robots.txt honored. No prices, page HTML or images.

## Layout

One immutable file per importer per week and layer; a week already stored is never fetched again.

| Path | What it holds |
|---|---|
| `raw/week=YYYY-MM-DD/importer=<name>.parquet` | Raw layer: each lot word for word, as the page had it that week: every cell of its row under the page's header, and its own page's URL, so everything derived can be derived again from it. Only cells about prices are left out |
| `offers/week=YYYY-MM-DD/importer=<name>.parquet` | The fields the page states (lot ID, name, origin, region, process, status, estimated arrival, warehouse, bags, bag size) and our normalized values derived from the raw layer (process type, varieties, altitude, harvest months, grower type and name, certifications) |

`week` is the Monday of the fetch's ISO week; `fetched_at` is when the page was fetched. The normalized values come
from deterministic rules (`src/coffeedata/importer_rules.py`, `rules_version`) that leave a value empty when the text
doesn't settle it. They haven't been evaluated yet.
