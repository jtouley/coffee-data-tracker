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
