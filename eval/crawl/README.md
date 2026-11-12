# Crawl output: how much of each catalog is read

The crawl's output fields are each roaster's **listed products** (which products a fetch read) and their **variant
prices** (`snapshots.variants[].price_usd`). They have no labeled evaluation yet: whether a product is listed, or
what a store charges, is read from the store, not judged. This README keeps the running total CLAUDE.md step 9 asks
for, so the next change to these fields knows how far it is from the last full look.

## Running total (CLAUDE.md step 9)

Products whose crawl output changed since the last full evaluation of these fields (there has been none): **at
least 13,266**: 11,750 from the caps' removal, 37 from Lone Oak's registry move (#98), 1,445 from #99's fix round,
and 34 from the currency change (below). Add each later change's count here. This total is far over step 9's 50,
so the next change to these fields needs the full process, or the owner's decision to waive it.

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
