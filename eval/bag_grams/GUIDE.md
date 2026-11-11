# Labeling guide: how much coffee does this option's price buy?

Each item is one listing on a roaster's store and one of its options (the one named in `label_this_option`). You
see the listing's title, URL handle, product type, tags, description, and every option with its title, price and
availability. The answer feeds the catalog's price per 100 g, so the question is: **for that option's price, how
many grams of coffee does the buyer get?**

Write the answer as a whole number of grams, or `unknown`, or `unclear`.

## Grams

- Use the weight the listing states for that option: in the option's own title first, then the listing's title,
  then its tags, then the description, then the URL handle.
- Convert with 1 oz = 28.3495 g, 1 lb = 453.592 g, 1 kg = 1000 g, and round to the gram: 12 oz is 340, 16 oz and
  1 lb are 454, 2 lb is 907, 5 lb is 2268, 8 oz and 1/2 lb are 227. A listing that gives both ("12 oz (340 g)",
  "1 kg (2.2 lb)") means one weight: write the metric one if it gives one, else convert.
- When the price buys several bags, write the total: "2 x 12 oz" is 680, "12 oz bags x 3" is 1020, "case of 6
  10 oz bags" is 1701, "5x 1lb bags" is 2268. Only when the listing says how many and how big. A count written as
  bags counts the same way: "Two 12 oz bags" and "2 Bags - 12 oz" are 680, a listing titled "... 12oz Whole Beans
  ... x 2 Bags" is 680, and an option "3 bags" on a listing whose title says each bag is 12 oz is 1020. An option
  "5 bags" whose bag size the listing doesn't give is `unknown`.
- An option that varies only by grind, roast level or decaf, with no weight of its own, has the weight the listing
  title or description gives for the coffee, if it gives one. If the options also differ in size (different
  prices, or titles like "Small" / "Large") and the listing doesn't say which size this option is, it's `unknown`.
- Portion packs follow the same rule as other packs: a count and a size give the total ("Portion Pack 48/2 oz" is
  2722, "42 / 2.5oz packets" is 2977, "PP 42ct" of 2.5 oz is 2977); a single packet that states its weight is that
  weight ("A 1.5 oz packet" is 43). Without a count or a size it's `unknown`.
- Samples count: a "4 oz sample" option is 113.
- A subscription option ("every 2 weeks") is the weight of one delivery.
- Tags are part of the listing. Use a size tag ("12oz", "12 oz.", "Size: 5 lb") only when the option and the
  listing's title give no weight, and only when the tags name exactly one size; tags naming two sizes ("12oz",
  "5lb") say nothing about this option.
- Use the URL handle only when the title gives no weight and the handle is this product's own (its words are the
  title's): stores copy pages, so "italian-roast-12oz" on "French Roast", or a handle starting "copy-of-", doesn't
  count.
- Use the listing only. Don't open the URL, and don't guess from the price or from the roaster's other listings.

## unknown

The listing doesn't say how much coffee this option buys, or says it only ambiguously:
- no weight anywhere ("Whole Bean", "Medium Roast", "Default Title" and nothing in the title or description);
- a weight that may not be this option's: "available in 12 oz and 5 lb bags" with no way to tell which option is
  which; "orders over 2 lb ship free"; a lot size ("only 20 lb this year"); a brewing amount ("20 g per cup");
- a pack or sampler that doesn't say its total or its bag count and size;
- pods, capsules, K-Cups, drip or steep bags, canned or bottled drinks and concentrates, unless the listing states
  the total coffee weight (a ground coffee sold in a can, "1 lb. Can", is a weight);
- a bundle with merchandise ("12oz + Mug");
- a drink sold by the cup ("12 oz Latte", "Espresso Double Shot") or anything that isn't coffee by weight.

`unknown` is a correct answer, not a failure: the rules are meant to leave these blank.

## unclear

You can't decide between a number and `unknown`, or between two numbers, after reading everything given. Use it
sparingly: a confident wrong number is worse than `unclear`. Unclear items are left out of scoring and counted.

## Format

One JSON object per labeler: `{"<coffee_id>": 340, "<coffee_id>": "unknown", ...}`, a key for every item in the
packet. Label each item on its own: don't look at another labeler's file, at any prediction, or at the scoring
script's output.

## Changes

- 2026-11-11, after the first labeling round, for the second test set: portion packs follow the packs rule (count
  × size), and tags and the URL handle are named as sources, settling the two questions the labelers raised. The
  first round's labels were made under the earlier text and are not changed.
- 2026-11-11, round 3 (bag counts): examples of counts written as bags, which the packs rule already covered. The
  earlier labels are not changed.
