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

**Sample queries:**
```sql
-- Median retail price per 100 g by origin, washed vs natural
select origin_country, process, coffees, median_price_per_100g
from public_market_weekly where process in ('washed', 'natural') order by coffees desc;

-- Price increases in the last 4 weeks
select e.run_date, c.roaster_name, c.coffee_name, e.previous_price_usd, e.price_usd
from public_price_event e join public_coffee c using (coffee_id)
where e.event = 'price' and e.price_usd > e.previous_price_usd and e.run_date >= current_date - 28
order by e.run_date desc;

-- New Ethiopian naturals this month
select roaster_name, coffee_name, price_per_100g, source_url from public_coffee
where origin_country = 'Ethiopia' and process = 'natural' and first_seen >= date_trunc('month', current_date);
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
