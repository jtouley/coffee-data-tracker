# Labeling definition: do these two roaster ids serve the same store?

You are labeling items from registry.coffee, a public catalog of the coffees that US specialty roasters sell on
their web stores. Every roaster in the catalog has an id, the web domain it was found under (from Overture Maps,
OpenStreetMap or a hand-made list). The catalog is read each week from the site each id leads to. When two ids lead
to one store, that store's coffees are counted twice.

Each item is a **pair of roaster ids**, A and B. For each you see:

- each id's name, town and state, store platform, the host its site resolves to after redirects, and how many
  coffees it lists now;
- how many coffee titles both list now, and up to 12 of them, with the lowest price and the bag sizes (grams) each
  store gives;
- up to 5 titles only one of them lists.

Decide from that evidence alone. You see no rule's verdict and must not guess one. Don't visit the sites.

## Labels

- `same_store`: A and B are **one business selling one catalog through one store**. A shopper reaching either
  domain ends up buying from the same shop. Typical cases:
  - an old or alternate domain that redirects to the store (a former name, a spelling or a `.co`/`.com` twin, a
    café's domain that sends visitors to the roastery's shop, a `*.myshopify.com` host beside the custom domain);
  - a rebrand: the company renamed itself and both names lead to the new shop, even when the old domain no longer
    redirects but its listing is the same catalog at the same prices from the same town;
  - a resolved host that differs only in case or a leading `www.`, `shop.` or `store.` label
    (`www.Example.com` is `example.com`).
- `different_store`: two separate businesses, or no store at all on one side:
  - **white label**: separate businesses on their own domains selling one supplier's or roaster's catalog,
    usually at their own prices, in their own towns. Same supplier, different business: `different_store`,
    however many titles they share;
  - two roasters whose lists overlap only in generic titles ("Colombia", "Decaf", "House Blend", "Espresso") or
    in the same importer's lots;
  - a café or grocer reselling some of another roaster's coffees beside its own;
  - a sister brand with its own line-up, prices or shop;
  - either domain resolves to a page that is not a store: a parked or for-sale domain (`hugedomains.com`,
    `expireddomains.com`), a link shortener (`bit.ly`, `bitly.com`), a directory or news site. Two such domains
    landing on the same parked page are **not** one store.
- `unsure`: the evidence can't settle it, e.g. the same host but no catalog on either side and nothing else that
  links the two, or identical titles and prices with nothing in the names, hosts or towns that links them.

An `unsure` is better than a confident wrong label. The same resolved host is strong evidence but not proof (see
the parked-domain case); identical catalogs are strong evidence but not proof (see white label).

## Output

One JSON file per labeler and set, `{"meta": {...}, "labels": [{"id": "<a>|<b>", "label": "...", "reason": "..."}]}`,
with `meta` naming the labeler (A, B or C), who or what labelled (a person, or Claude and which model), the set,
and the date. `reason` is one short sentence. Labeler C (the tiebreak) labels only the items where A and B
disagree, without seeing their labels.
