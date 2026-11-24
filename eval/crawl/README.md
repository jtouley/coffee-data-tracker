# Crawl output: how much of each catalog is read

The crawl's output fields are each roaster's **listed products** (which products a fetch read) and their **variant
prices** (`snapshots.variants[].price_usd`). They have no labeled evaluation yet: whether a product is listed, or
what a store charges, is read from the store, not judged. This README keeps the running total CLAUDE.md step 9 asks
for, so the next change to these fields knows how far it is from the last full look.

## Running total (CLAUDE.md step 9)

Products whose crawl output changed since the last full evaluation of these fields (there has been none): **at
least 13,300**: 11,750 from the caps' removal, 37 from Lone Oak's registry move (#98), 1,445 from #99's fix round,
34 from the currency change and 34 from the capped `per_page` rule (below). Add each later change's count here.
This total is far over step 9's 50, so the next change to these fields needs the full process, or the owner's
decision to waive it.

| Change | Products changed (estimate) | Evaluation |
|---|---|---|
| Remove crawl data caps; bulk WooCommerce variation prices; mark early stops partial (2026-11-02) | ≥ 11,750 | Steps 2–7 waived by the owner (below) |
| Lone Oak Coffee (`loneoakcoffee.com`) moved from Square Online to Shopify in the registry (#98, 2026-11-02): its catalog is read for the first time | 37 | Hand-checked (#98's PR body); under step 9's 50 on its own |
| #99 fix round: WooCommerce readable size names (variant `title`, and `grams` where the name has a weight) | ≈ 1,391 | Steps 2–7 waived by the owner (below) |
| #99 fix round: volunteercoffee.com read in smaller pages after a timeout (it failed every week) | 54 | The WooCommerce review's probe (54 products); a fetch fix, not a rule |
| #99 fix round: each WooCommerce size's own `available` (`is_in_stock`), where prices are read in bulk | unknown | Not measurable from the lake (below) |
| #99 fix round: no null price stored when a one-by-one price lookup fails (the fetch fails or is deferred) | in the ≤ 36 of the first row | — |
| #99 fix round: vendor sites and `gmail.com` never crawled (`registry.current`); martincoffee, piccoffee, meanbean registry fixes | 0 | Hand-checked: none had products |
| Each price's currency stored (raw 1.6.0); public tables USD prices and US roasters only; 7 non-US roasters corrected in the registry (2026-11-04) | 34 now; more at the first crawl with raw 1.6.0 (unknown) | Steps 2–7 waived by the owner (2026-11-04, #107); 21 hand-checked, 13 in other fields' sealed test sets (count only); below |
| WooCommerce null size prices traced to the old lookup cap and measured at the first uncapped crawl (2026-11-20): tests and a measurement only | 0 | Measured below; no crawl or dbt code changes |
| A WooCommerce store that caps `per_page` (a short first page while every count header it sends says there is more) is read at its own page size (2026-11-23) | 34 (estimate: one store, 10 of 44 read each week) | A fetch fix, not a rule; unit tests (`tests/test_woo_capped_per_page.py`); confirmed by the owner's one-store Test crawl after deploy (below) |
| unrivaledcoffee.com's seed platform `other` -> `shopify?` (2026-11-24): the probe's marker answer for its Shopify storefront; its connector stays generic | 0 | Hand-checked (the PR lists the 71 audited stores); no product's output changes |

## 2026-11-02: every data cap removed

The owner's decision, verbatim: "remove the woocommerce cap and all caps, we don't have enough data to know what caps
are." The owner made it explicitly, so **CLAUDE.md steps 2–7 (split, labels, sealed test score) were waived for this
change.** It is measured here instead, and the count above stays on the books until these fields get a full
evaluation.

**How it was measured.** On the read-only local lake copy (runs 2026-10-26 to 2026-11-02), with DuckDB over
`fetches` and `snapshots`. No request was sent to any store. Nothing was written to the remote lake. The figures
come from the latest ok fetch per roaster on 2026-11-02 unless a row says otherwise. Who judged: Claude, from the
code and these counts; no product was labeled.

| Cap removed | Roasters at it | Products whose output changes |
|---|---|---|
| WooCommerce `MAX_VARIATION_LOOKUPS = 150` | 48 of 201 ok fetches (`pages ≥ 151`) | **1,628** products with a size left unpriced, now priced. 23,230 sizes needed a lookup at those 48 stores; 150 each were asked for. |
| WooCommerce `MAX_PAGES = 20` (of 100) | 1 (`villageofartisans.com`: exactly 2,000 products, ended without an error) | its products past 2,000: unknown until it is read |
| WooCommerce lookups that failed (SiteGround's 202, 429s), stored as a null price in an ok fetch | `mozzaroasters.com`; others at the uncapped stores | ≤ 36 products: a bulk page that fails now fails the fetch (retried) instead of wiping those prices |
| generic `MAX_PAGES = 80` | 11 at the cap (dq-audit 2026-11-02); 14 fetches with ≥ 81 pages | **≥ 122**: products these roasters showed in some week but not in their latest. The real number past 80 is unknown (never read). |
| generic `GIVE_UP_AFTER = 10` (stops after 10 pages without Product markup, as an empty catalog) | 39 ok fetches with 0 products and ≥ 10 pages | unknown, likely few: those sites rarely use the markup |
| Shopify `MAX_PAGES = 60` (of 250) | 1 (`kaldi.com`: error "still more products after 60 pages" on 10-28 and 11-02) | **≥ 10,000**: its catalog is over 15,000 products; its last ok fetch (10-26, at the old cap of 20) had 5,000 |
| Square Online `MAX_PAGES = 20` (of 60) | none (largest: 985 products) | 0 now |
| Squarespace `MAX_PAGES = 10` and 6 shop paths | none visible (largest: 159 products) | 0 now (a collection's raw item count isn't stored, so a store at the page cap can't be ruled out) |
| sitemap: 5,000 URLs, 5 sub-sitemaps | not recorded per fetch | unknown; feeds the generic and Squarespace rows above |

**Total: at least 1,628 + 122 + 10,000 ≈ 11,750 products**, far over step 9's 50. The unknowns add to it.

New products from a fuller read are new rows for the classifier, origin, wholesale and match rules: those rules
don't change, so this doesn't count against their own running totals (`eval/wholesale`, `eval/match`).

### Request counts, WooCommerce (2026-11-02, the 201 ok fetches)

Reasoned from the code over the lake's catalogs (no request was sent). "Needs a price" is a product with more than
one size and no single listing price; that rule reproduces the old lookup count exactly at 150 of the 155 uncapped
stores.

| | Successful pages |
|---|---|
| Before (150 lookups at most per store) | 12,843 |
| One request per size, with no cap | 30,572 |
| **After: bulk `type=variation&parent=…`, about 100 sizes a request** | **≈ 669** |

The 48 capped stores go from 7,284 pages, with 1,628 products unpriced, to about 386, with every size priced.

### What still stops a fetch early, and how it is recorded

The run's deadline (`ingest.run(deadline_s=…)`, the connector's 225-minute window) now also stops a catalog being
read: no request is sent after it (`http.DeadlineReached`). The products read whole are stored as an ok fetch with
`fetches.partial = true`, so dbt keeps the unread products listed (`stg_runs.is_current`) and never reads them as
delisted. A partial fetch is not "done" for the week: the resume slot reads the catalog again, and dbt prefers a
complete batch of the week. Nothing read is an error, never an empty catalog.

## 2026-11-02: #99's fix round

**Readable size names (WooCommerce).** The Store API lists a variation's attributes as `{name, value}`, and for a
taxonomy attribute the value is the term's *slug* (`12-oz`, `0-5-lb`); the readable name is in the parent's own
`attributes[].terms` in the same response (ProductSchema.php `get_variations` and `get_attributes`). The crawl now
stores the term's name, matched by attribute name, then by slug, and keeps the raw value when no term matches (no
hyphen rule: `0-5-lb` would read as 5 lb). **The owner waived CLAUDE.md steps 2–7 for this change.** Counted on the
read-only local lake copy (2026-11-02, latest ok WooCommerce fetch per roaster; no request sent, nothing written):
**1,391 products at 77 roasters** (8,867 variants, of 10,691 products) have a slug-shaped variant title, so their
`title` changes; of them, **918 products** (7,836 variants, 49 roasters; `build/research/woocommerce-review-2026-11-02.md`)
gain `grams`, which `dim_coffee` needs for the per-100 g price. The title count is an estimate: a custom attribute
whose value only looks like a slug keeps it. Who judged: Claude, from the code and these counts; no product was
labeled.

**Each size's own stock.** A size priced from the bulk `type=variation` response (or its own lookup) now takes that
variation's `is_in_stock`, not the parent's (true when any size is). The lake keeps only the old, parent-wide value,
so how many sizes change can't be counted offline; the WooCommerce review's live sample (hoodrivercoffeeroasters.com,
100 sizes) had 0 that differ.

## 2026-11-04: each price's currency; USD and US roasters only in the public tables

**What changed.** Ingestion stores each variant's currency as the store states it (`snapshots.variants.currency`,
raw contract 1.6.0): WooCommerce `prices.currency_code`; Shopify the `cart_currency` cookie its `/products.json`
response sets (or the one its request carried); Squarespace `priceMoney.currency`; Square Online the store page's
`storeInfo.currency`; generic schema.org `priceCurrency`. The price stays as stated. In dbt, `stg_snapshots` keeps
a variant's price as `price_usd` only when it is in USD (`is_usd`): the store states USD, or states no currency and
the roaster is in the US (`stg_registry.country`). `public_roaster` keeps US roasters only, and every public table
follows it.

**Old rows.** Every lake row before raw 1.6.0 has no currency. For a US roaster such a row reads as USD, so its
prices, per-100 g prices, market stats and price events are exactly as before. For a roaster the registry places
outside the US, it reads as not USD, and the roaster isn't public anyway. A US roaster whose store starts stating a
non-USD currency loses those prices from the public tables at that run, and the switch is not a price event (both
sides of a `price` event must be USD).

**Count, on the read-only lake copy** (`dq-week/lake`, release 2026-11-02, build `20261103T203218-956abd`), with
DuckDB over the released public tables; nothing was written to any lake. The change to public output comes entirely
from the 5 corrected roasters that are public today (the other 2 have no catalog): **34 products** leave
`public_coffee` (aromacoffee.co.za 5, blacksheepcoffee.co.uk 4, lacabra.dk 17, macacotostadores.com 5,
thecoffeeroaster.sg 3), with their 96 rows of `public_price_event` (none of them a `price` event) and 5 rows of
`public_roaster`; 13 origin x process rows of `public_market_weekly` change (Ethiopia unknown p90 7.34 -> 7.31,
Uganda washed p90 5.60 -> 5.29, India washed median 5.73 -> 6.03, ...). No US roaster's price changes: every one of
their rows has no currency yet. Once raw 1.6.0 is crawled, a US-registered store that states another currency
(a Canadian Shopify store's CAD, say) drops its prices from the public tables; how many is unknown until that run.

**Hand-check.** Each of the 34 is a product of a store hand-checked as outside the US, whose published "USD" is its
own currency: 21 are listed in the PR body (store, price as published, the currency the store states); 13 are in
another field's sealed test set (`eval/origin`, `eval/match`) and are counted only. Who judged: Claude, from the
stores' own answers (one request each, 2026-11-04) and these counts; no product was labeled.

**Process.** 34 is under step 9's 50, but the running total for these fields was already over 50 before this
change, and the USD rule is a new rule for what is published (deterministic on what the store states, not a
judgement). Steps 2-7 were not run: the owner waived them for #107 on 2026-11-04. #107's fix round (price events
across a run in another currency, the WooCommerce fallback to the listing's currency, the lake's schema file) changes
no product's output on the current lake, where no row states a currency yet.

## 2026-11-20: the null WooCommerce size prices, traced and measured at the first uncapped crawl

**The report.** martinhenrycoffee.com has 375 null prices of 650. The unit is a size (a variant), not a product:
375 of its 650 sizes, in 56 of its 133 products, in each of its three stored weeks (10-28, 11-02, 11-09).

**The cause, from the stored snapshots.** Not a price shape the parser misreads. Until #99 the adapter asked for at
most 150 variation prices per store (`MAX_VARIATION_LOOKUPS`) and stored every later size of a multi-price product
with a null price; a lookup that failed was stored as null too. #99 removed both on 2026-11-02, after that day's
crawl; the 11-09 crawl still ran the capped code (martinhenrycoffee.com: 152 pages = 2 listing pages + 150 lookups),
and 11-16 was the first without it.

| Run | Stores | Sizes | Null sizes | Past the 150-lookup cap | A failed lookup stored as null |
|---|---|---|---|---|---|
| 2026-10-26 | 227 | 50,011 | 19,351 | 19,336 | 15 |
| 2026-10-28 | 223 | 46,941 | 18,724 | 18,711 | 13 |
| 2026-10-30 | 92 | 20,798 | 7,202 | 7,193 | 9 |
| 2026-11-02 | 198 | 43,787 | 17,728 | 17,684 | 44 |
| 2026-11-09 | 197 | 44,595 | 18,166 | 18,139 | 27 |
| **2026-11-16** | 218 | 50,428 | **0** | 0 | 0 |

- A size is "past the cap" when more than 150 sizes that needed their own lookup come before it in its fetch, in
  the stored order (the listing's). In every capped run no size past the 150th has a price, and at most 150 per
  store do; on 11-16, 19,502 sizes past the 150th are priced.
- No product without sizes (a simple product) has a null price in any run. No other shape leaves a null: a price
  range, minor units with their exponent and an empty price are pinned in `tests/test_adapters.py`
  (`test_woocommerce_price_shapes` and the two tests after it), with the cap and the failed lookup in
  `tests/test_crawl_caps.py` and `tests/test_crawl_fix_round.py`.
- **The stored rows don't contain the missing prices.** The lake keeps the parsed product, not the Store API's
  response, and those variations were never asked for. No derivation over the stored weeks can fill them, and the
  stored files stay as they are.

**Before and after, per store.** Before: each store's last snapshot before 11-16. After: its 11-16 snapshot.
59 stores had 19,794 null sizes in 1,918 products.

| What 11-16 says about a size that was null | Sizes | Products | Stores |
|---|---|---|---|
| Priced | 18,615 | 1,708 | 55 |
| No longer listed (its product, or the size) | 69 | 9 | 6 |
| Still null | 0 | 0 | 0 |
| The store wasn't read on 11-16 | 1,110 | 203 | 4 |

The four stores not read again, whose latest stored snapshot (11-09) still has the nulls:

| Store | Null sizes of sizes | Products with one | Why it wasn't read on 11-16 |
|---|---|---|---|
| martinhenrycoffee.com | 375 of 650 | 56 of 133 | `Blocked`: its robots.txt disallows the Store API. #182 reads such a store from its sitemap and pages, with each size's price from the page; nothing on `/wp-json/` is asked for. |
| meanbeanroasters.com | 471 of 842 | 119 of 387 | Not in the run's manifest (`dim_roaster.status` is `unreachable`). |
| cartelroasting.co | 132 of 349 | 14 of 61 | robots.txt answered 429 twice; the listing answered 400 once. |
| cartelcoffeelab.com | 132 of 349 | 14 of 61 | An alias of cartelroasting.co, no longer crawled. |

**The measurement.** `woo_null_prices.py` over a read-only local copy of the lake (downloaded 2026-11-20: runs
10-26 to 11-16, release build `20261117T061926-d7559f`). It prints totals only. No store was fetched and nothing was
written to the lake.

    uv run python eval/crawl/woo_null_prices.py <lake copy> [--per-roaster <a csv outside git>]

- **Census.** The tables above count every stored size, so they have no sampling error.
- **The two reads agree.** At the 59 stores, 15,038 sizes have a price both before (one lookup each) and on 11-16
  (the bulk query): 15,027 are the same price; 11 differ, at 3 stores. 188 of the sizes null before had a price in
  an earlier stored week: all 188 match their 11-16 price.
- **Sample, read by hand.** Sizes null before, keyed `sha256("coffeedata-woo-prices-2026-11-20:" + roaster_id + "/"
  + product_id + "/" + variant_id)`, the 2 lowest per store: 118 sizes at 59 stores. There is no train, validation
  or test split: nothing was tuned, and the sample is read once. Each was read against its stored rows of both runs:
  **110 of 118 have a price on 11-16** (Wilson 95%: 87.2% to 96.5%; the census says 94.0%, 18,615 of 19,794), and the
  other 8 are the four unread stores' 2 each. Of the 110, 108 have a positive price that fits their product's other
  sizes on 11-16 (a larger bag costs more); 2 (one product, a subscription) are a stated $0.00.
- **Who judged:** Claude, from the stored rows. Not labeled twice, and no person checked. Whether a price equals
  what the store shows today wasn't checked: that needs a read of the store.

**Step 9.** This change is tests and this measurement: it changes no product's output, so the running total stays
where it was. The 1,708 products priced on 11-16 are #99's change, already in the first row (an estimate of 1,628
then, for 48 stores on 11-02).

**Seen on the way, not changed here:**
- The served release (11-16) has 11,806 WooCommerce `price` events with no previous price, at 49 stores: a size
  going from an unread price to its first read one (`fct_price_event`; its unit test `price_event_types` expects
  that event). #87 is the same event in the other direction. Earlier runs have 3 to 46.
- 1,324 WooCommerce sizes at 79 stores have a stated price of 0 on 11-16 (661 on 11-09, when the cap hid others).
  The marts leave them out (`price_usd > 0`). Whether the store means free, or prices them some other way, isn't
  known from the lake.

## 2026-11-23: stores that cap `per_page`

**The case.** One WooCommerce store answered exactly 10 products (the Store API's default page) to `per_page=100`,
while its X-WP-Total said 44. `_pages` stopped at that short first page, so 34 products were never refreshed and the
fetch was stored as partial every week. This is inferred from the stored counts; no request was sent to the store.

**The change.** When the query's first page is shorter than asked and every count header the store sends
(X-WP-Total, X-WP-TotalPages; at least one) says there is more, its length is taken as the store's page size and the
rest is read at it, up to the store's own count plus one page. A short, empty or repeated page ends the query, and the
X-WP-Total check still marks a count that differs as partial. A page that times out at the store's size is asked for
again at a smaller size that divides what was read; if every size times out, the pages read are stored as partial
("timed out at page N"), never an error with nothing stored. Products are kept once each, by id. Per-host pacing,
robots.txt and the deadline apply to every page; there is no page cap (removed 2026-11-02), as before.

**Step 9.** Only a store whose first page is short while its own count headers say there is more can change.
- One that sends X-WP-Total is an X-WP-Total partial today. Of the 11-16 WooCommerce partials, one fits (34
  products); the other two read a full first page or have a count below what was read. 11-23's third WooCommerce
  partial isn't identified yet.
- One that sends X-WP-TotalPages but no X-WP-Total was never partial, so its change isn't in the 34. How many there
  are isn't known: this change read no lake data. The Store API sends both headers, so it should be rare; the
  post-deploy crawl's fetch rows would show any store whose product count rises.
- A store that honours `per_page` costs no extra request when either header agrees with its first page.

34 is under 50 on its own; the running total above was already over 50, so whether steps 2–7 apply is the owner's
call, as for the volunteercoffee.com fetch fix (a fetch fix, not a rule). The confirmation is the owner's one-store
Test crawl from the admin after deploy.
