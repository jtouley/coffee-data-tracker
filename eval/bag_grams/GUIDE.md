# Labeling guide: how much coffee does this option's price buy?

Each item is one listing on a roaster's store and one of its options (the one named in `label_this_option`). You
see the listing's title, URL handle, product type, tags, description, and every option with its title, price and
availability. The answer feeds the catalog's price per 100 g, so the question is: **for that option's price, how
many grams of coffee does the buyer get?**

Write the answer as a whole number of grams, or `unknown`, or `unclear`.

## Grams

- Use the weight the listing states for that option: in the option's own title first, then the listing's title,
  then the description.
- Convert with 1 oz = 28.3495 g, 1 lb = 453.592 g, 1 kg = 1000 g, and round to the gram: 12 oz is 340, 16 oz and
  1 lb are 454, 2 lb is 907, 5 lb is 2268, 8 oz and 1/2 lb are 227. A listing that gives both ("12 oz (340 g)",
  "1 kg (2.2 lb)") means one weight: write the metric one if it gives one, else convert.
- When the price buys several bags, write the total: "2 x 12 oz" is 680, "12 oz bags x 3" is 1020, "case of 6
  10 oz bags" is 1701, "5x 1lb bags" is 2268. Only when the listing says how many and how big.
- An option that varies only by grind, roast level or decaf, with no weight of its own, has the weight the listing
  title or description gives for the coffee, if it gives one. If the options also differ in size (different
  prices, or titles like "Small" / "Large") and the listing doesn't say which size this option is, it's `unknown`.
- Samples count: a "4 oz sample" option is 113.
- A subscription option ("every 2 weeks") is the weight of one delivery.
- Use the listing only. Don't use the URL's handle when it disagrees with the title (stores copy old pages), don't
  open the URL, and don't guess from the price or from the roaster's other listings.

## unknown

The listing doesn't say how much coffee this option buys, or says it only ambiguously:
- no weight anywhere ("Whole Bean", "Medium Roast", "Default Title" and nothing in the title or description);
- a weight that may not be this option's: "available in 12 oz and 5 lb bags" with no way to tell which option is
  which; "orders over 2 lb ship free"; a lot size ("only 20 lb this year"); a brewing amount ("20 g per cup");
- a pack or sampler that doesn't say its total or its bag count and size;
- pods, capsules, K-Cups, drip or steep bags, portion packets, cans and bottles, unless the listing states the total
  coffee weight;
- a drink sold by the cup ("12 oz Latte", "Espresso Double Shot") or anything that isn't coffee by weight.

`unknown` is a correct answer, not a failure: the rules are meant to leave these blank.

## unclear

You can't decide between a number and `unknown`, or between two numbers, after reading everything given. Use it
sparingly: a confident wrong number is worse than `unclear`. Unclear items are left out of scoring and counted.

## Format

One JSON object per labeler: `{"<coffee_id>": 340, "<coffee_id>": "unknown", ...}`, a key for every item in the
packet. Label each item on its own: don't look at another labeler's file, at any prediction, or at the scoring
script's output.
