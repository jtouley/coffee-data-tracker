# Labeling definitions for the data-quality rules

You are labeling items from registry.coffee, a public catalog of the coffees that US specialty roasters sell on
their web stores. The catalog is crawled each week from each store's platform (Shopify, WooCommerce, Squarespace,
Square Online, or a generic HTML reader). Each item below shows the evidence for one case. Decide, from that
evidence and what you know about coffee retail, whether the data is wrong, using only the labels of the section for
this rule. You see no rule's verdict, and you must not guess one: judge the data.

Use `unsure` when the evidence can't settle it. Don't guess: an `unsure` is better than a confident wrong label.

## dupes

An item is a **pair of roaster ids** (each id is a store's web host) whose currently listed coffees overlap. You see
each store's name, host, city and state, platform, how many coffees it lists, the titles both list (with the price
and bag sizes each store gives for them), and some titles only one lists.

- `duplicate`: the two ids are **one roaster's catalog counted twice**. Either they are the same business under two
  hosts (an alias or old domain, a rebrand such as a renamed company, a `*.myshopify.com` host and the custom
  domain, a café site and the roastery site of the same company), or one store's listing is the other's catalog
  fed through as-is by the same company. Strong signs: the same titles with the same prices and bag sizes, names or
  hosts that are variants of each other, the same town.
- `distinct`: two separate businesses whose lists overlap for another reason: generic titles ("Colombia",
  "Decaf", "House Blend", "Espresso"), a café or grocer that resells some of another roaster's coffees at its own
  prices, two roasters buying the same importer's lots, a sister brand with a line-up of its own.
- `unsure`: you can't tell from the evidence, e.g. identical titles and prices but nothing in the names, hosts or
  towns links the two.

## price

An item is **one listed coffee** and its computed `price_per_100g` (USD per 100 g: the cheapest available bag
between 150 g and 2,500 g, price divided by grams, times 100). You see the store's host, name and state, the title,
the origin and process when known, and each bag size (variant) with its price, grams and availability, as the
crawler read them. Prices are read as US dollars.

For reference, most US specialty retail coffee sells for about $3 to $10 per 100 g. Rare lots (Gesha, competition
or auction lots, Kona, Jamaica Blue Mountain, St Helena, some anaerobic or co-fermented micro-lots) can reach $15 to
$60 or more. Grocery and bulk coffee can be under $2.

- `wrong`: `price_per_100g` is **not the real US-dollar price per 100 g of this coffee as sold**. For example:
  - the store prices in another currency (a non-US store: `.co.za`, `.dk`, `.sg`, `.ca`, `.au`, `.uk` and similar,
    or prices that only make sense as rand, krone, pesos...);
  - a price or weight misread: cents as dollars, a "2 lb" or "5 lb" bag read as a few hundred grams, a case or box
    of several bags priced as one bag's weight, a placeholder price ($0.01, $1, $999);
  - the item isn't a bag of coffee: a gift box or bundle with mugs or gear, a class, a subscription whose price
    covers several shipments, a sampler of tiny bags priced as one large bag, an equipment item.
- `right`: the value is plausible as what this coffee really costs per 100 g, even when it is very expensive (a
  real Gesha) or very cheap (a 5 lb bulk bag). Being unusual is not being wrong.
- `unsure`: the evidence can't settle it (e.g. an expensive single lot with no sign of what it is).

## adapter

An item is **one platform (adapter) in one weekly run**, measured over a fixed subset of that platform's stores (a
random third or so of all stores; the subset is the same in every run). You see how many of the subset's stores the
run was expected to fetch, how many it tried, how many fetches succeeded and how many brought back products; the
errors and HTTP statuses it got; how many requests were rate limited; and, for the stores that were fetched in both
this run and the previous run, how many products and how many priced products they returned in each.

- `collapse`: in this run, **this platform's crawl failed to bring back a material part of its stores' catalogs**,
  beyond its normal week-to-week failures, for a reason on the crawling side (blocks or challenges, rate limits,
  timeouts, a request budget, a parser or adapter fault). Material: about 10% or more of its stores that have a
  catalog, or about 10% or more of their products, or of their prices.
- `normal`: within ordinary variation for the platform, including a run that simply fetched **fewer stores** (an
  incomplete or partial run) where the stores it did fetch came back as usual, and stores that changed their own
  catalogs.
- `unsure`: too few stores to judge, or the evidence conflicts.

## recon

An item is **one disagreement between two tables** of the same release (for example, a price event whose coffee
isn't in the coffee table, or a total in one table that doesn't match the rows of another). You see both sides and
what the build knows about the rows involved (for example, whether the product is still classified as coffee).

- `defect`: a consumer who joins or sums these tables as documented gets a wrong answer, and nothing in the
  evidence explains the difference as intended.
- `by_design`: the difference follows from a stated definition (for example, a product the classifier no longer
  counts as coffee leaves the coffee table, so its old events have no coffee row; or one table counts wholesale or
  archived coffees and the other doesn't), and the evidence shows that definition applies to these rows.
- `unsure`: the evidence can't settle it.
