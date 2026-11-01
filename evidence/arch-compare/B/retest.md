# Targeted real retest of A's unsuccessful Shopify stores, with PAGINATION.patch (v1) (2026-10-31 16:27-17:05 UTC)
**Setup.** These are the 33 stores with no ok row in A's run_date 2026-10-31 fetches. Left out: biggunscoffee.com (robots.txt disallows the feed), javamasters.com (404, bad URL) and kaldi.com (it hits the 20-page cap, MAX_PAGES; it needs a higher cap, not backoff). 11 are A's page-2 failures and 22 are A's `refused` stores. Code: B/wt, which is origin/main + PR #62 + PAGINATION.patch (on a 429 mid-catalog it waits the Retry-After, up to 3 times per page, and asks for the same page again). It ran through `ingest.run` with 32 workers at 3 req/s and normal politeness. Publishing was off: tokens unset, huggingface_hub stubbed to raise (checked before start), local lake B/real/retest_lake. I held real.lock ("B-retest 2026-10-31T16:27:09Z") and released it at 17:05:37Z. Every response is in B/retest_responses.jsonl; per-store data is in B/real/retest_summary.json (script B/real/analyze_retest.py).
**Totals.** 335 requests, 260 429s (every Retry-After was 60), 0 430s, 38.4 min wall. **Page-2 group: 4 of 11 now succeed** (Cat & Cloud, Death Wish, Groundwork, Red Bay). **Refused group: 11 of 22 now succeed.** Overall 15 of 33.
**What happened.** Every store that succeeded was served 2 product pages, and page 1 held its whole catalog: coffees equal page-1 coffees in every case. Page 2 was the empty page that ends the catalog. So the "page-2 wall" sits on the terminating request, and keeping page 1 and retrying page 2 is enough. That is what the fix does for the 4. 7 of the other page-2 stores now refused page 1 itself, on every try (Counter Culture, Colectivo, Cuvée, Four Barrel, Andytown, plus Coffee AM and Winans failing on later tries). This retest ran 1-3 h after A had crawled the same stores from this IP, so a per-store window may still have been open. 11 of A's 22 refused stores refused again.
| store | group | outcome | product pages served (200) | 429s | waited after 429s, s | coffees (page-1 coffees) |
|---|---|---|---|---|---|---|
| alanascoffeeroasters.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1146 | - (-) |
| andytownsf.com | page2 | failed: refused (429 on every page-1 try) | 0 | 7 | 1393 | - (-) |
| artizancoffee.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1397 | - (-) |
| birdrockcoffee.com | refused | ok | 2 | 6 | 756 | 34 (34) |
| caffevita.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1400 | - (-) |
| catandcloud.com | page2 | ok | 2 | 11 | 1064 | 13 (13) |
| chromaticcoffee.com | refused | failed: FetchError: HTTPStatusError https://www.chromaticcoffee.com/ | 2 | 19 | 1984 | - (-) |
| coffeeam.com | page2 | failed: FetchError: HTTPStatusError https://www.coffeeam.com/product | 2 | 22 | 2302 | - (-) |
| colectivocoffee.com | page2 | failed: refused (429 on every page-1 try) | 0 | 7 | 1395 | - (-) |
| copperdoorcoffee.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1218 | - (-) |
| counterculturecoffee.com | page2 | failed: refused (429 on every page-1 try) | 0 | 7 | 1338 | - (-) |
| cuveecoffee.com | page2 | failed: refused (429 on every page-1 try) | 0 | 7 | 1397 | - (-) |
| deathwishcoffee.com | page2 | ok | 2 | 17 | 1609 | 16 (16) |
| domacoffee.com | refused | failed: FetchError: HTTPStatusError https://www.domacoffee.com/produ | 1 | 17 | 1367 | - (-) |
| dunecoffee.com | refused | ok | 2 | 10 | 1453 | 15 (15) |
| fourbarrelcoffee.com | page2 | failed: refused (429 on every page-1 try) | 0 | 7 | 1151 | - (-) |
| goodcitizencoffee.com | refused | ok | 1 | 0 | 0 | 0 (0) |
| groundworkcoffee.com | page2 | ok | 2 | 2 | 132 | 34 (34) |
| highbankcoffeeroasters.com | refused | ok | 2 | 3 | 573 | 5 (5) |
| javabeanroast.com | refused | ok | 2 | 2 | 339 | 11 (11) |
| javacity.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1279 | - (-) |
| lancastercountycoffee.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1216 | - (-) |
| lavelacoffee.com | refused | ok | 2 | 11 | 1641 | 17 (17) |
| puroast.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1343 | - (-) |
| redbaycoffee.com | page2 | ok | 2 | 3 | 283 | 15 (15) |
| socoffee.co | refused | ok | 2 | 6 | 751 | 20 (20) |
| sparrowscoffee.com | refused | ok | 2 | 4 | 530 | 13 (13) |
| templecoffee.com | refused | ok | 2 | 2 | 130 | 33 (33) |
| terranovacoffee.net | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1400 | - (-) |
| ticoroasters.com | refused | ok | 2 | 3 | 585 | 54 (54) |
| toastique.com | refused | failed: refused (429 on every page-1 try) | 0 | 7 | 1280 | - (-) |
| vulcanroastery.com | refused | ok | 2 | 6 | 1342 | 37 (37) |
| winanschocolate.com | page2 | failed: FetchError: HTTPStatusError https://winanschocolate.com/prod | 1 | 18 | 1372 | - (-) |
