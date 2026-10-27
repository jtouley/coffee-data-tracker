# Listing copy (draft)

Use for the public Hugging Face dataset, the Snowflake Marketplace and the Databricks Marketplace. Keep the
three in sync.

**Title:** US Specialty Coffee Market: Weekly Prices, Origins and Availability

**Short description:**
Weekly prices, stock and attributes for about 45,000 coffees from 1,800 US specialty roasters, with price and
listing history.

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
**License:** CDLA-Permissive-2.0 (see the dataset card for attribution).
**Support contact:** _your email_.
