# Listing copy (draft)

Use for the public Hugging Face dataset, the Snowflake Marketplace and the Databricks Marketplace. Keep the
three in sync. The Snowflake listing's attribute boxes are typed by hand from this file, so this file is the
source of truth for them.

<!-- coverage-claims: every build checks these against its own mart_build_profile (`coverage_claims_*` dbt tests).
One claim per line, `metric: text`: the text must appear in the copy below, and its number is the stated value.
Change a number in the copy and here together, then retype it in the Snowflake listing.
listed_coffees counts the coffees listed for sale in this build (not archived, not wholesale), not PUBLIC_COFFEE's
rows (every coffee ever seen); roasters_with_coffee counts the roasters with at least one of them, not PUBLIC_ROASTER.
listed_coffees: about 38,000 coffees listed for sale
roasters_with_coffee: 1,800 US specialty roasters with coffee for sale
Not checked: "about half of reachable US roasters" (no build metric counts reachable roasters).
-->

**Title:** US Specialty Coffee Market: Weekly Prices, Origins and Availability

**Short description:**
Weekly prices, stock and attributes for about 38,000 coffees listed for sale, from
1,800 US specialty roasters with coffee for sale. Each coffee keeps its price and listing history.

**Description:**
What US specialty roasters sell online, collected every week from their own storefronts. Each coffee has origin, process, varietal, roast level, tasting notes, bag sizes, list price and price
per 100 g, plus whether it's listed and in stock. A change log records every price change, stock change,
delisting and relisting week by week. It can be used to follow
retail prices by origin and process, see what competitors carry and how often it changes, or track how coffees
are described.

**Tables:**
- `PUBLIC_COFFEE`: one row per coffee ever seen
- `PUBLIC_PRICE_EVENT`: weekly change log
- `PUBLIC_MARKET_WEEKLY`: origin × process summary
- `PUBLIC_ROASTER`: roasters covered

**Refresh:** weekly (data collected Mondays, published Tuesdays).
**Geography:** United States. **History:** from 2026-10-26.

**Changed column names on Snowflake (breaking for quoted lower-case names):** the share's views now have upper-case
column names. A query that quotes a lower-case name now fails with `invalid identifier`. Before:
`select "release_run_date" from SHARE.COFFEE`. Now: `select release_run_date from SHARE.COFFEE` (unquoted names
work in any case, or quote the upper-case name, `"RELEASE_RUN_DATE"`).

**Sample queries** (Snowflake: the share's views `SHARE.COFFEE`, `SHARE.PRICE_EVENT`, `SHARE.MARKET_WEEKLY` and
`SHARE.ROASTER`, in the database the consumer mounts the share as; their column names are upper case and need no
quotes. Databricks: the shared tables `market.coffee`, `market.price_event`, `market.market_weekly` and
`market.roaster`, in the catalog the consumer creates from the share. Hugging Face: the `public_*` table names):
```sql
-- 1. Median retail price per 100 g by origin, washed vs natural
select ORIGIN_COUNTRY, PROCESS, COFFEES, MEDIAN_PRICE_PER_100G
from SHARE.MARKET_WEEKLY where PROCESS in ('washed', 'natural') order by COFFEES desc;

-- 2. New Ethiopian naturals this month
select ROASTER_NAME, COFFEE_NAME, PRICE_PER_100G, SOURCE_URL from SHARE.COFFEE
where ORIGIN_COUNTRY = 'Ethiopia' and PROCESS = 'natural' and FIRST_SEEN >= date_trunc('month', current_date);

-- 3. Price increases in the last 4 weeks
select e.RUN_DATE, c.ROASTER_NAME, c.COFFEE_NAME, e.PREVIOUS_PRICE_USD, e.PRICE_USD
from SHARE.PRICE_EVENT e join SHARE.COFFEE c using (COFFEE_ID)
where e.PRICE_USD > e.PREVIOUS_PRICE_USD and e.RUN_DATE >= current_date - 28 and e.EVENT = 'price'
order by e.RUN_DATE desc;

-- 4. Origins with the most coffees for sale, and the median of their processes' median prices
select ORIGIN_COUNTRY, sum(COFFEES) as COFFEES_LISTED, median(MEDIAN_PRICE_PER_100G) as median_of_medians_per_100g
from SHARE.MARKET_WEEKLY group by ORIGIN_COUNTRY order by COFFEES_LISTED desc limit 20;
```

**Limitations** (more in the dataset card):
- Coverage is roasters with machine-readable storefronts (about half of reachable US roasters).
- Attributes are extracted, by rules or LLM, and can be wrong.
- Prices are list prices.
- No descriptions or images.

**Pricing:** free.
**License:** CDLA-Permissive-2.0. Full text: https://github.com/jtouley/coffee-data-tracker/blob/main/licenses/CDLA-Permissive-2.0.txt.
Attribution: https://github.com/jtouley/coffee-data-tracker/blob/main/docs/ATTRIBUTION.md.
**Support contact:** _your email_.
