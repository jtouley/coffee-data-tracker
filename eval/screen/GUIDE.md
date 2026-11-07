# Labeling guide: is this an actual roaster that sells coffee it roasts, online?

Each item is one business website that Overture Maps tags as a coffee shop or café (`overture_tag`). The catalog lists
US roasters' coffee by the bag, so the question is whether this website belongs in the roaster registry.

Label each item from the business's own website (start at `website`; the shop, about and product pages), as it is
on the day you label. A web search may help find the shop or confirm who roasts, but the website's own words count
most. Don't use the screen's results; you won't be shown them.

## label

**in_scope**: an actual roaster that sells coffee it roasts, online. All three must hold:
- **It roasts coffee itself.** The business (or the company that owns it, under the same brand) roasts the beans:
  "we roast", "roasted in house", "our roastery", a named roastery location, roasting equipment shown as theirs, or
  an about page that tells its roasting story. A café that is part of a roaster's own brand (the roaster's flagship
  café) counts.
- **It sells that coffee by the bag.** Whole bean or ground, by weight, under its own name: at least one coffee a
  person could buy (as `src/coffeedata/coffee_definition.md` defines a coffee; samplers, pods and drinks don't count).
- **Online.** A person can put a bag in a cart on this website (or on a store this website links as its own shop,
  under the same brand). Buying only in person, by phone, or through a third-party marketplace (Amazon, a
  delivery app) is not online here.

**out_of_scope**: anything else, including:
- A café, coffee shop, bakery or restaurant that serves or sells another roaster's coffee, even with a working
  online shop of those bags.
- A shop or market that sells several roasters' coffee (a multi-roaster retailer), or bags with its own label that
  another company roasts for it, when the site says so (private label).
- A roaster with no online sale of bags (bags in the shop only, or wholesale only).
- A site that doesn't load, is closed, parked or for sale, or isn't a coffee business at all.

**unsure**: you looked and can't tell, most often whether a store's own-label coffee is roasted by the business or
for it. Use it honestly: a confident wrong label is worse than unsure. Unsure items are left out of the scores.

## kind

Give one, whatever the label, so a wrong admission can be told from a near miss:
- `roaster`: roasts its own coffee (in scope if it also sells bags online).
- `cafe`: a café, coffee shop, bakery or restaurant that doesn't roast.
- `reseller`: sells other roasters' coffee, or private-label coffee roasted for it, by the bag (online or not).
- `other`: anything else (closed, unreachable, not a coffee business, a chain's corporate site).

Admitting a `cafe` or a `reseller` is a confidently wrong admission in the scores.

## Format

One JSON file per labeler and set, `labels_<set>_<A|B>.json`: a list of `{"id": ..., "label": "in_scope" |
"out_of_scope" | "unsure", "kind": "roaster" | "cafe" | "reseller" | "other", "note": "optional, short"}`, one entry
per item in `items_<set>.json`. The third labeler settles `to_settle_<set>.json` into `settled_<set>.json`, the same
format. Say who labeled (a person, or Claude and which model) in README.md.
