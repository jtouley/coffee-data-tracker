# Shopify refusers: who failed on which IP, ranked by catalog size (2026-10-31)

**Sources (read only).** GitHub runner: the lake's `fetches` for run 2026-10-30 (and 2026-10-28 for context), from
a read-only copy of `hf://datasets/jtouley/coffee-data` (identical to /tmp/lakecopy/fetches). Live test: this
container's IP, 2026-10-31 01:44-02:04 UTC, `live/live_responses.jsonl` and `live/live_summaries.jsonl` (phases A, B
and the retry phase C; see live.md). Importance: `coffees_listed` in `dim_roaster` built from `main` on that lake copy
as of 2026-10-30 (retail coffees in the roaster's latest successful catalog, archived and wholesale left out), with
the product count of its latest successful fetch and its location count as proxies. Script:
/tmp/carry/scripts/refusers.py (scratch, not in git).

## Who failed where

- **GitHub runner, 2026-10-30** (a partial run: 1,000 of ~2,545 expected roasters tried before the 240-min timeout):
  57 roasters failed; 120 got at least one 429 and 116 of those still succeeded. Of the 57, 10 have a catalog with
  coffees (169 coffees in all): Big Guns (43), Backstage (23), City League (18), Colorado River (15), Cherry (14),
  Good News (14), Hailey (13), Dessert Oasis (12), Groundwire (9), Cascadia (8). Only 5 of the 57 are Shopify
  (Big Guns, Cherry, Dessert Oasis, Groundwire, Cascadia); 39 are the generic sitemap adapter (403s, robots
  unavailable), with no coffees listed.
- **GitHub runner, 2026-10-28** (mass Shopify 429s): 934 Shopify roasters failed, including 10 of the 12 that failed in
  the live test.
- **Live test, this IP** (200 Shopify hosts sampled by salted hash): 12 failed in the end, 10 of them by 429 on every
  `/products.json`: Atlas Coffee Club, Equator, Counter Culture, Purity, Bongo Java, Temple, Zoka, Sightglass, La
  Vela, Offset (297 coffees together). Kaldi (more than 20 pages) and Java Masters (404) failed for other reasons. 3
  got 429s and recovered on a retry: Coffee King, Saint Frank, Cat & Cloud.
- **Overlap: none.** None of the 57 runner failures was in the live sample, and none of the 12 live failures failed on
  the 10-30 runner: 8 weren't tried that day (the run timed out), 2 succeeded after a 429 (Atlas, Equator) and 2 had
  no 429 at all (Counter Culture, Bongo Java). Each IP sees a different set of stores refuse.

## Top 30 by coffees in the latest good catalog

The candidates are the 72 roasters that failed on the 10-30 runner or got a 429 or failed in the live test. Only 23
of them list any coffee, so rows 24-30 are roasters with no coffee catalog, ranked by products.

| # | roaster | adapter | coffees listed (latest good) | in stock | products (last ok fetch) | locations | last ok run | runner 10-30 | runner 10-28 | live 10-31 (429s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Coffee King Roasting and Supply Co. (`coffeekinginc.com`) | shopify | 74 | 74 | 478 | 1 | 2026-10-30 | ok | FAILED | recovered (429) (5) |
| 2 | Big Guns Coffee Farm And Roastery (`biggunscoffee.com`) | shopify | 43 | 43 | 97 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 3 | Atlas Coffee Club (`atlascoffeeclub.com`) | shopify | 41 | 30 | 261 | 1 | 2026-10-30 | ok after 429 (1) | ok | failed (429) (4) |
| 4 | Equator Coffees (`equatorcoffees.com`) | shopify | 40 | 36 | 135 | 1 | 2026-10-30 | ok after 429 (1) | FAILED | failed (429) (4) |
| 5 | Counter Culture Coffee (`counterculturecoffee.com`) | shopify | 38 | 23 | 179 | 2 | 2026-10-30 | ok | FAILED | failed (429) (4) |
| 6 | Purity Coffee (`puritycoffee.com`) | shopify | 36 | 34 | 103 | 1 | 2026-10-26 | not tried | FAILED | failed (429) (4) |
| 7 | Bongo Java (`bongojava.com`) | shopify | 35 | 33 | 101 | 1 | 2026-10-30 | ok | FAILED | failed (429) (10) |
| 8 | Temple Coffee (`templecoffee.com`) | shopify | 31 | 31 | 135 | 1 | 2026-10-27 | not tried | FAILED | failed (429) (4) |
| 9 | Zoka Coffee (`zokacoffee.com`) | shopify | 28 | 28 | 67 | 4 | 2026-10-26 | not tried | FAILED | failed (429) (7) |
| 10 | Backstage Coffee Roasters (`backstageroasters.com`) | woocommerce | 23 | 22 | 73 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 11 | Sightglass (`sightglasscoffee.com`) | shopify | 22 | 21 | 54 | 1 | 2026-10-26 | not tried | FAILED | failed (429) (9) |
| 12 | City League Coffee Roasters (`cityleaguecoffee.com`) | woocommerce | 18 | 18 | 30 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 13 | La Vela Coffee Roasters And Catering (`lavelacoffee.com`) | shopify | 16 | 14 | 30 | 1 | 2026-10-26 | not tried | FAILED | failed (429) (4) |
| 14 | Colorado River Coffee Roasters (`crcoffeeroasters.com`) | woocommerce | 15 | 15 | 18 | 1 | 2026-10-28 | FAILED | ok | not in sample |
| 15 | Cherry Coffee Roasters - New Orleans (`cherrycoffeeroasters.com`) | shopify | 14 | 14 | 48 | 3 | 2026-10-26 | FAILED (3) | FAILED | not in sample |
| 16 | Good News Roasters Coffee (`goodnewsroasters.com`) | woocommerce | 14 | 14 | 15 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 17 | Saint Frank (`saintfrankcoffee.com`) | shopify | 13 | 12 | 41 | 1 | 2026-10-26 | not tried | FAILED | recovered (429) (6) |
| 18 | Cat & Cloud (`catandcloud.com`) | shopify | 13 | 13 | 23 | 1 | 2026-10-30 | ok | FAILED | recovered (429) (2) |
| 19 | Hailey Coffee Roaster (`haileycoffeeco.com`) | woocommerce | 13 | 13 | 15 | 1 | 2026-10-28 | FAILED | ok | not in sample |
| 20 | Dessert Oasis Coffee Roasters (`dessertoasiscoffee.com`) | shopify | 12 | 12 | 45 | 2 | 2026-10-26 | FAILED (3) | FAILED | not in sample |
| 21 | Offset Coffee Roasters (`offsetcoffee.com`) | shopify | 10 | 10 | 31 | 2 | 2026-10-28 | not tried | ok | failed (429) (4) |
| 22 | Groundwire Coffee Roasters (`groundwire.coffee`) | shopify | 9 | 9 | 12 | 1 | 2026-10-26 | FAILED (3) | FAILED | not in sample |
| 23 | Cascadia Coffee Roasters (`cascadiaroasters.com`) | shopify | 8 | 8 | 18 | 1 | 2026-10-26 | FAILED (3) | FAILED | not in sample |
| 24 | Kaldi Gourmet Coffee Roasters (`kaldi.com`) | shopify | 0 | 0 | 5000 | 1 | 2026-10-26 | not tried | FAILED | failed (FetchError) |
| 25 | Blue Mountain Coffee Roasters (`bluemtncoffee.com`) | generic | 0 | 0 | 0 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 26 | Alpine Coffee Roasters (`alpinecoffeeroasters.com`) | generic | 0 | 0 | 0 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 27 | Columbia River Coffee Roaster (`columbiarivercoffeeroaster.com`) | generic | 0 | 0 | 0 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 28 | Aspire Coffee Roasters (`aspirecoffeeroasters.com`) | generic | 0 | 0 | 0 | 2 | 2026-10-26 | FAILED | FAILED | not in sample |
| 29 | Green Star Coffee Roastery (`greenstarcoffee.com`) | generic | 0 | 0 | 0 | 1 | 2026-10-26 | FAILED | FAILED | not in sample |
| 30 | Gourmet Coffee Roasters (`javamasters.com`) | shopify | 0 | 0 | 0 | 1 | 2026-10-26 | not tried | FAILED | failed (FetchError) |

"recovered (429)": refused, then answered on a retry in the same test. "ok after 429 (n)": n 429s on the runner, then
a success. "not tried": the 10-30 run ended before reaching the roaster.

**What it means for carry-forward.** Every one of these has a successful catalog from 2026-10-26 or later, so with
`carry_forward_weeks` = 4 all would stay listed, flagged `carried_forward`, through about four weeks of refusals.
On the 10-30 build, Purity, Temple, Zoka and Sightglass are listed from 10-26/27 catalogs (carried); Counter Culture,
Equator, Bongo Java and Coffee King were fetched on 10-30 (not carried).
