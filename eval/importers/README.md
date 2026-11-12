# Importer rules: evaluation status and running totals

The rules that derive the green importers' values (the private dbt project `dbt_importers/`: `int_importer_rules`,
`int_importer_name_rules`, `int_royal_rules`, over the vocabulary in `src/coffeedata/importer_rules.py`) decide "how
often is it right" questions. Under the root `CLAUDE.md` they are judged on held-out data, like a model.

## Status

**Not evaluated yet. Nothing is labeled.** No split, labels, floors or sealed test set exist for these rules.

#148 (importer derivation moved into dbt; InterAmerican's product names give origin, region, process, grade and
screen) changes the output for far more than 50 products, so `CLAUDE.md` step 9's small-change path doesn't apply.
The full process (steps 2–7) is owed. Whether a private view that is never published needs that evaluation before it
merges is the **owner's call, still pending**. Until the owner decides, #148 waits.

## Running totals since the last full evaluation (step 9)

There has been no full evaluation, so these count from `main` before #148. A product here is one lot of one stored
week, compared field by field with `main`'s read-time Python (`importers.offers()` at 060cd16) on the only stored
week (2026-11-09). Each total counts the lots whose value for that field changed.

| Importer | Field | Lots changed | Change |
|---|---|---|---|
| InterAmerican | origin | 173 | 0 → filled from the product name |
| InterAmerican | grade | 118 | new field, from the product name |
| InterAmerican | process (as stated) | 60 | 0 → filled from the product name |
| InterAmerican | region | 59 | 0 → filled from the product name |
| InterAmerican | screen | 24 | new field, from the product name |
| InterAmerican | certifications | 2 | `[organic, fair_trade]` → unknown (SMBC, Bird Friendly, isn't modeled) |
| Cafe Imports | every field | 0 | identical on all 379 lots (only `rules_version`, 2 → 3) |
| Royal Coffee | every field | 0 | no stored week yet. On the 6 synthetic products and 50 rule cases, identical (only `rules_version`, royal-2 → royal-3) |

**Royal's derivation from its real title shapes (royal-3 → royal-4).** Compared field by field with `main`'s build
(the private dbt project at `main`) on the week of 2026-11-09: 14,282 Royal, 379 Cafe Imports and 177 InterAmerican
lots. Royal's labelled-text path (`int_royal_labels`) matched 0 rows of the real listing and is gone.

| Importer | Field | Lots changed | Change |
|---|---|---|---|
| Royal Coffee | lot_id | 14,261 | 152 → 14,261 filled: Royal's five-digit lot ID part; the 152 were producers' "Lot N" |
| Royal Coffee | warehouse | 14,261 | 0 → 14,261: the last title part's warehouse code, without SPOT |
| Royal Coffee | status | 13,195 | 0 → 13,195 `spot` (the title says SPOT); other lots abstain. SPOT is the stated location, not availability: 12,776 of them are sold out, so read status with is_available or currently_offered |
| Royal Coffee | est_arrival | 330 | 0 → 330: "ETA: <date>" and "<Month YYYY> Delivery"; a shipment month gives none |
| Royal Coffee | bag_size, bag_kg | 8,035; 14,260 | 0 → filled: the title's packaging part; its stated weight, else the variant's grams |
| Royal Coffee | is_available, currently_offered | 14,282 | new: from Shopify's variants.available (1,310 offered) |
| Royal Coffee | origin | 906 | 13,302 → 14,204: Papua New Guinea 373, Indonesia 336 (Bali, Java, Flores, Komodo, Sumatra), Timor-Leste 189, 6 others whose title names a second place; 2 non-coffee products (no lot ID, now is_lot false) lose the origin their name mentioned |
| Royal Coffee | process (as stated) | 14,021 | 0 → 14,021: the process tag, as written |
| Royal Coffee | process_type | 126 | null → filled: decaf no longer read from the body's prose |
| Royal Coffee | certifications | 2,755 | fair trade added: Royal's FT, FT-FLO, FT-USA, FT-FLO/USA |
| Royal Coffee | region, varieties, altitude, harvest | 0 | null before and after: left for Claude |
| Cafe Imports | origin | 42 | as written → the catalog's country (Sumatra, Java, Flores, Bali → Indonesia; DR Congo; Hawaii → United States); as written kept in origin_as_written |
| InterAmerican | every field | 0 | identical (new key and flag columns only) |

Checks on that week (training data: it was read while writing the rules, so none of this is a held-out score):
- **Agreement with Royal's process tag.** Of the 12,552 lots with exactly one kind of process tag, process_type agrees
  on 12,486, is anaerobic (a fermentation the tags don't name) on 53, abstains on 13, and disagrees on 0 (`main`:
  12,368 agree, 131 abstain, 0 disagree). The rule reads the tags, so agreement is partly by construction; the test
  `royal_process_type_agrees_with_its_tag` keeps it.
- **Hand check.** 40 changed Royal lots drawn at random (ordered by a salted hash of the row), each changed value read
  against its title, tags and variants by Claude (one labeler, not the double labeling `CLAUDE.md` asks for): 38
  correct, 0 wrong, 2 abstained (forward lots whose title gives only a shipment month: status and arrival left null).
- **Fill-rate floors** (`dbt_importers/tests/importer_fill_floors.sql`, which fails the build, and
  `importer_fill_floors_by_mix.sql`, which only warns, for Royal's status, est_arrival and certifications, whose fill
  follows the week's mix of spot and forward lots) are set a little below these fills on each importer's latest
  week, and only rise:

| Importer | Field | Fill on 2026-11-09 | Floor |
|---|---|---|---|
| Royal Coffee | lot_id, warehouse | 0.999 | 0.97 |
| Royal Coffee | origin | 0.994 | 0.97 |
| Royal Coffee | bag_kg, is_available | 0.998, 1.0 | 0.97 |
| Royal Coffee | process | 0.982 | 0.95 |
| Royal Coffee | status (warns) | 0.924 | 0.88 |
| Royal Coffee | process_type | 0.888 | 0.85 |
| Royal Coffee | bag_size | 0.563 | 0.50 |
| Royal Coffee | certifications (warns) | 0.404 | 0.35 |
| Royal Coffee | est_arrival (warns) | 0.023 | 0.01 |
| Cafe Imports | lot_id, origin, process, status, is_available, bags, bag_kg | 1.0 | 0.95 |
| Cafe Imports | process_type, region | 0.926, 0.736 | 0.85, 0.60 |
| InterAmerican | status, warehouse, is_available, bags, bag_kg | 1.0 | 0.95 |
| InterAmerican | origin | 0.977 | 0.90 |
| InterAmerican | process, region, process_type | 0.339, 0.333, 0.209 | 0.25, 0.25, 0.15 |

**The held-out evaluation of these Royal rules runs on the first scheduled week stored after this merges**
(2026-11-16 or later): the week of 2026-11-09 was read by the reviewer and while writing these rules, so it is
training data. On that week: a salted hash split grouped by lot (Royal's Shopify id) or producer, so one lot's
products never sit on both sides; train 60%, validation 20%, test 20%, with the rule and salt recorded here; each
sampled lot labeled twice, independently, against a written definition, with a third labeler for disagreements; the
current code against `main` on every set, with Wilson 95% intervals; the test set scored once. Until then there is no
held-out score for these rules.

Add each later change's changed-lot counts to these rows until a full evaluation resets them.

## Held-out data

**The week of 2026-11-09 is used up as held-out data for these rules.** While #148 was developed:
- every derived InterAmerican value of that week was read by hand (all 69 distinct product names, 177 lots);
- all 556 Cafe Imports and InterAmerican lots of that week were compared with `main`.

A validation or test set for these rules must come from **later weeks only** (2026-11-16 onward). Draw it by the
salted, importer-grouped hash split that `CLAUDE.md` step 2 describes, and record the rule and the salt here when it
is drawn. Royal's synthetic products are invented test fixtures, not data. They never count as a sample.

## When the evaluation is done
Follow `CLAUDE.md` steps 2–7:
- labels made twice, independently, against a written definition, with a third labeler for disagreements;
- `main` scored against the change on every set, with Wilson 95% intervals;
- the sealed test set scored once.

Floors then go into the tests, and these totals reset to zero.
