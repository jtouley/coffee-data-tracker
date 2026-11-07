# Labeling guide: is this product a coffee for the catalog?

Every product here was listed as coffee and then dropped by the classifier. You are not told why, or what anyone
predicted. Label each one against the catalog's definition, which is the classifier's (`eval/sealed/GUIDE.md`),
repeated here so this file stands alone.

The catalog lists the coffees US roasters sell: one row per coffee a person could buy by the bag.

**coffee**: roasted coffee, whole bean or ground, sold by weight as one coffee.
- Single origins, blends, espresso roasts, decaf, half-caf.
- Flavored coffees ("French Vanilla", "Sugar Cookie Decaf", "Highlander Grogg") count, even when the title names only the flavor.
- Any bag size, including 2 oz samples of one coffee and 5 lb bulk bags.
- Wholesale bags and cases of retail bags of one coffee ("Company Blend - Wholesale", "12oz Case (6)").
- A coffee that can also be bought on subscription ("Subscribe & save") is still a coffee.
- Pre-measured filter packs, frac packs and portion packs of ground coffee for a drip brewer, and cold brew filter packs of ground coffee.
- A coffee with options (grind, size) is still one coffee.

**not_coffee**: everything else, including:
- Equipment and parts: grinders, brewers, machines, filters, kettles, scales, cups, mugs, burrs, gaskets, cleaners.
- Merch, books, classes, gift cards, services (toll roasting, consulting), shipping, donations.
- Subscriptions and prepaid gift subscriptions ("3 Month Gift"), even of a named coffee.
- More than one coffee in one product: samplers, variety packs, flights, bundles, gift sets and boxes.
- Single-serve formats: capsules, pods, K-Cups, drip/steep bags, instant coffee, ready-to-drink, cans, liquid concentrate.
- Other food and drink: tea, chai, matcha, cocoa, syrups, sauces, chocolate-covered beans, candy, baked goods, honey.
- Test or placeholder listings ("[Test] ...", "TEST BATCH").
- Green (unroasted) coffee sold for home roasting.

**unsure**: you can't tell from what's given, or it's a real edge case (for example a title so vague the description doesn't settle it). Use it sparingly but honestly: a wrong confident label is worse than "unsure".

Judge each product only from its title, product type, tags, bag sizes in grams and description. Don't guess from the roaster's name alone.

**One note for this set.** Some stores paste the same text into many products: a sample pack's paragraph on each
coffee it holds, a shipping or roasting notice, a brand story. Label the product the title, sizes and the rest of
the description describe. Text pasted from another product is not evidence about this one either way.

## How to label

- Read `work/items_<set>.json` (its path is in the hand-off). Each item has an `id`, a `coffee_id`, the roaster's
  name, the title, product type, tags, bag sizes in grams (`null` where the store gave none) and the description.
- Write `labels_<you>.json` beside this file: one object, `{coffee_id: "coffee" | "not_coffee" | "unsure"}`, with
  every item of every set you were given.
- Label on your own: don't compare notes with the other labeler, and don't look up predictions, verdicts or the
  store's live page.
