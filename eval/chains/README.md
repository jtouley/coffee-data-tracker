# Chains: which roasters are left out of the public data

The owner's decision, verbatim: "Chains should still be scraped, but filtered from the public result. Let's call a
chain anything with over 20 stores."

The field is `is_chain` (`dbt/models/intermediate/int_roaster_state.sql`): more than `chain_store_threshold` (20)
stores, where the store count is `stg_registry.locations`, unless `dbt/seeds/chain_overrides.csv` says otherwise,
either way, with a reason. Once the admin's chain flags are published (Sources; the snapshot's `chains`, passed
to dbt as var `chain_flags`), they replace the seed, whose four rows are their initial values. `public_roaster`
leaves a chain out, so its coffees, price events and market counts leave
every public table, the site, the public dataset and the shares. A chain is still crawled and stays in the lake and
the internal marts (`dim_roaster`, `dim_coffee`, `fct_price_event`; `mart_market` already left chains out).

There is no labeled evaluation of this field. This README keeps the running total CLAUDE.md step 9 asks for.

## Running total (CLAUDE.md step 9)

**Why steps 2–7 don't apply here.** Step 9 asks for the full process when a change alters how a rule decides, and
this one does: `is_chain` moves from a hand-set flag to "more than 20 stores, unless overridden". It is measured here
by hand instead, for three reasons. The rule is the owner's own definition, verbatim, not a heuristic tuned to data;
the owner also decided that the store count stays as discovery records it, so there is no alternative count to
compare; and there is no independent labeled set of store counts to score it against. What can be checked was: on all
3,203 seed roasters the rule plus its four overrides gives exactly the nine chains `main` flagged by hand (0
changed), each hand-checked below. A labeled evaluation would need real store counts per roaster, which no source in
the lake has.

| Change | Roasters whose `is_chain` changed | public_roaster | public_coffee | public_price_event |
|---|---|---|---|---|
| Store-count rule with overrides; chains leave the public tables (2026-11-11) | **0** | -2 | -149 | -438 |

`is_chain` changed for no roaster against `main`: the rule and its four overrides give the same nine chains that
`config/roasters.csv` flagged by hand. The public tables lose the two of those nine that published coffee (Peet's and
The Coffee Bean & Tea Leaf), as the owner's decision asks; that is the decision, not a change in who is a chain.

## The store count

`locations` is the number of US Overture places whose website is the roaster's and that are roasteries: tagged
`coffee_roastery`, or a coffee place with "roast" in its name (`discover.overture`). The seed recorded it when it was
built from an Overture release. Discovery writes its own count to the lake's `registry` table on each run, but
`stg_registry` takes the seed's value for a seeded roaster (`coalesce(s.locations, a.locations, 1)`): **a seeded
roaster's count is frozen at seeding**, and only a roaster the lake alone lists follows discovery. So no seeded roaster
crosses the threshold by itself (Coffee Bean's override guards against a change that can't come through this path
today); the owner decided the count stays as discovery records it, and this README says so rather than changing it. Nothing new is crawled to decide `is_chain`, and no
backfill is needed: every roaster has a count (1 when unknown, and for OpenStreetMap-only roasters).

It is a count of roastery places, not of stores. A café that serves the roaster's coffee but isn't tagged as a
roastery isn't counted, so **a chain of cafés reads low**. In the 2026-11-09 release, 486 of 3,205 roasters have
more than one place and 6 have more than 20.

## Measurement (2026-11-11)

**How.** Read-only copies of release `20261110T061836-971bfe` (run 2026-11-09): `dim_roaster`, `dim_coffee`,
`public_coffee`, `public_roaster`, `public_market_weekly`, and `public_price_event` counted by roaster. Nothing was
written to the lake and nothing was crawled. `public_market_weekly` was recomputed from `public_coffee` (it matches
the release: 196 cells, 19,964 coffees) and again without the chains. Who checked: Claude, against the roasters'
public store counts as generally known; nobody labeled, and nobody counted stores against a source.

**Every roaster the rule or an override calls a chain.**

| roaster_id | Name in the registry | Store count | Coffees (public) | Status | Source | Chain by | Hand check |
|---|---|---|---|---|---|---|---|
| dutchbros.com | Dutch Bros Coffee | 1,114 | 0 | no_catalog | overture | count | Dutch Bros, about a thousand drive-throughs: yes. The seed had one place's name ("Hebron"); renamed to the brand. |
| farmerbros.com | Farmer Brothers | 62 | 0 | unreachable | overture | count | A foodservice roaster and distributor: its places are likely branches and customers' sites, not stores. **Noisy**; no public effect while it publishes nothing. |
| pjscoffee.com | PJ's Coffee of New Orleans | 41 | 0 | no_catalog | overture | count | A franchise of well over 20 cafés: yes. |
| blankstreet.com | Blank Street Coffee | 31 | 0 | unreachable | overture | count | Dozens of cafés: yes. |
| caffenero.com | Caffè Nero | 25 | 0 | no_catalog | overture | count | Its US cafés number in the dozens: yes. |
| coffeebean.com | The Coffee Bean & Tea Leaf | 21 | 44 | active | overture | override (and count) | Hundreds of stores: yes. **One place above the threshold**, so an override pins it. |
| cariboucoffee.com | Caribou Coffee | 14 | 0 | unreachable | overture | override | Hundreds of coffeehouses: yes. The count reads low. |
| cleaneatz.com | Clean Eatz | 13 | 0 | no_catalog | overture | override | A restaurant franchise with well over 20 locations: yes. The count reads low. |
| peets.com | Peet's Coffee | 1 | 105 | active | curated | override | Hundreds of coffeehouses: yes. The count reads 1. |

**Public impact.** public_roaster 1,852 → 1,850; public_coffee 45,857 → 45,708 (-149: Peet's 105, The Coffee Bean
44; 148 of them listed); public_price_event 309,316 → 308,878 (-438); public_market_weekly keeps all 196 cells, 35 of
them with lower counts (19,964 → 19,889 coffees). The site's catalog already left these 149 coffees out; its headline
coffee and roaster totals now do too.

## Known limits

- **One status per roaster_id, not per store.** `is_chain` is decided per id, and `public_roaster` publishes each
  store's canonical id. A chain domain that is an alias of a non-chain canonical id would leave that store's coffees
  public. None exists today: no seed alias touches a chain, and release 971bfe has no alias rows. If one appears,
  decide it per store (`bool_or(is_chain) over (partition by canonical_roaster_id)`).
- **The seeded count is frozen** (above).

## Likely chains the count misses (the owner's decision)

The count can't see chains of cafés. These roasters have 20 or fewer roastery places but, by their public store
counts, likely have more than 20 stores. None is overridden here; each is the owner's call.

| roaster_id | Store count | Public coffees | Status |
|---|---|---|---|
| lacolombe.com | 13 | 25 | active |
| bluebottlecoffee.com | 14 | 0 | unreachable |
| philzcoffee.com | 11 | 0 | unreachable |
| blackriflecoffee.com | 1 | 0 | unreachable |

Close to the line, worth a look: stumptowncoffee.com (15 places, 35 coffees), hammercoffee.com (14, 24),
vervecoffee.com (10, 118), joecoffeecompany.com (1, 19), kaldiscoffee.com (1, 22), intelligentsia.com (1, 184).
A truer count needs a discovery change: count every US Overture place whose website is the roaster's, whatever its
category, in the same scan, and store it next to `locations`.
