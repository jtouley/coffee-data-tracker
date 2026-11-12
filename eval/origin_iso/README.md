# Origin spelling to ISO 3166-1: a census

This measures the mapping from each origin spelling the lake has captured to ISO 3166-1
(`dbt/seeds/origin_country_iso.csv`, resolved by `dbt/macros/origin_iso.sql` through `int_origin_country`), which
dim_coffee and the public tables publish as `origin_country` (the ISO English short name) and `origin_country_code`
(alpha-2). The owner's decision: "Clean up the spelling, standardize in our model to the ISO names, that's what
should be public."

**Running total (CLAUDE.md step 9):** 0 products whose ISO `origin_country` output changed since the last full
evaluation, which is this one. Add each later change's count here.

## Why a census, not a split

The unit that can be wrong is a distinct spelling → country mapping, not a product: every product with the same
captured spelling gets the same answer. There are only 149 spellings, so every one is labeled and there is nothing to
sample or hold out; CLAUDE.md steps 2-5 (split, train, validation, sealed test) don't apply. The score is reported over
spellings and weighted by the products that carry each spelling (the full population, the owner's rule: at least 95%
weighted by products). Wrong mappings, a country published that the label says is another country or no single
country, must be 0.

The mapping was frozen before any label existed: `origin_country_iso.csv` sha256
`f39259734a10839748a44dd83b447ea3fda70dae9812f6d010f5c6267eea1840`, `origin_iso.sql` sha256
`7ce95f188a45ce73bd7bbc2b989531143a130cd7383c226dfd24a4767a102598` (commit of the first draft of the change).

## Data

- Source: a read-only copy of `hf://datasets/jtouley/coffee-data` (downloaded 2026-11-11; runs to 2026-11-09, the
  2026-11-09 release 20261110T061836-971bfe), built twice with `coffeedata build` and the stored verdicts
  (`COFFEEDATA_VERDICT_MODEL=claude-haiku-5-5`): main at 0879254 and the change. Nothing was written to the remote.
- The census: every distinct non-null `origin_country` in `stg_products` (the rules) and in Claude's rows of
  `stg_enrichments`, in either build: 148 in main, 149 in the union (the change's rules also produce "Puerto Rico";
  "Guinea" is no longer produced by the rules but stays in the census and the seed).
- Weights: the coffees in the change's dim_coffee carrying each spelling (21,923 coffees have one at the final
  commit; a spelling only an older product version carries weighs 0). Main's dim_coffee has 21,927.
- The 2026-11-09 release had 52 values in public_market_weekly for about 46 countries; the change's build has 45 (one
  per country), and 45 distinct countries over all of public_coffee, down from 120 values.

## Labels

- **Labelers:** Claude subagents, all on Claude Haiku (`claude-haiku-5-5`, chosen and approved by the owner), each
  blind to the mapping and to the others: two independent labelers (A, B) over the full packet and a third (C) over
  the spellings they disagree on. The settled
  label is the one at least two of the three give; a three-way split is reported as unresolved. No person has
  labeled; a spot-check by one is still to come.
- **Definition:** `GUIDE.md`. Each item is one spelling with up to five example listings (title, roaster, recorded
  region), shuffled by a salted hash (`coffeedata-origin-iso-2026-11-11`), without the mapping, its kind or any
  weight. Labels: an ISO 3166-1 alpha-2 code, `MULTIPLE`, `NOT_A_COUNTRY` or `AMBIGUOUS`.
- `labels.json` (in git) keeps the settled label and product count per spelling. The packet and the labelers' files
  quote product titles, so they stay out of git, in a scratch folder.

## Scoring

- Agreement: the build's answer equals the label. `MULTIPLE`, `NOT_A_COUNTRY` and `AMBIGUOUS` publish the same
  thing (no country), so they agree with one another.
- Wrong: the build publishes a country and the label is a different country or no single country.
- A spelling the build leaves without a country where the label names one is a miss, not wrong (abstaining over
  guessing, CLAUDE.md step 7).
- Wilson 95% intervals over spellings; the product-weighted share is a full-population figure.

## Results (2026-11-11)

**Agreement between labelers:** A and B agree on 148 of 149 spellings, 99.3% (Wilson 95% 96.3-99.9%). The one
disagreement was bare "Guinea" (A: GN, B: AMBIGUOUS); C, blind to both, answered AMBIGUOUS, so it settled as
AMBIGUOUS. No spelling was unresolved. Settled: 51 countries, 90 MULTIPLE, 6 NOT_A_COUNTRY, 2 AMBIGUOUS (Guinea, New
Guinea).

| | spellings agree | weighted by coffees | wrong mappings (spellings / coffees) |
|---|---:|---:|---:|
| main 0879254 (publishes the captured spelling) | 39 of 149, 26.2% (19.8-33.8%) | 97.1% of 21,927 (96.9-97.3%) | 110 spellings not ISO's; 98 of them (108 coffees) a list, region or shared place shown as one origin |
| the frozen seed (version .1, hashes above) | 148 of 149, 99.3% (96.3-99.9%) | 100% of 21,923 | 1 / 0 |
| **the change (seed version .2)** | **149 of 149, 100% (97.5-100%)** | **100% of 21,923** | **0 / 0** |

- Main's row reads its published value as a name: it is right only where the captured spelling already is the label's
  ISO short name. Its weighted score is high because most coffees carry a plain country name that is already ISO's.
- The frozen seed's one wrong mapping was "Guinea" → GN, which no coffee in the change's build carries any more (the
  same change stops the rules reading "New Guinea" as Guinea; 17 coffees in main). Version .2 makes bare "Guinea"
  ambiguous, following the settled label. That change was made after the labels were read, and the census has no
  held-out part, so the .2 row is fitted to these labels; the frozen row is the unfitted score.
- **The owner's rule (at least 95% weighted by coffees, full population): met, 100%.**
- No spelling is unmapped, so `origin_countries_are_mapped` passes on this data.
- Floor: `tests/test_origin_iso_accuracy.py`, 149 of 149 spellings, at least 0.999 weighted, 0 wrong. It runs in the
  ordinary test job (labels and seed are both in git).

## Round 2 (review of #144): pending labels

- **Seed rows without a census label.** 17 spellings: the ISO and common forms of every seeded country that weren't
  already spellings ("Tanzania, United Republic of", "Viet Nam", "DR Congo", "Côte d'Ivoire", …), and 9 seed rows the
  rules can produce or that only appear as parts of lists (Central African Republic, Gabon, Hawaii, Sierra Leone,
  Sri Lanka, Sumatra, Trinidad and Tobago, Usa, Central American). Same guide, same blind protocol (two labelers on
  Claude Haiku, a tiebreak), with no example listings. The 8 new forms join the seed only once labelled;
  `test_every_seed_row_has_a_label` is a strict xfail until then.
- The six spellings the review found missing in a seed restricted to the first batch (North America, Perú, Latin
  America, Paraguay, Việt Nam, South America) are already in the census and the seed, labelled in round 1.
- `SHA256SUMS` holds the round-1 packet, census and guide hashes (written before any label) and round 2's packets and
  prompts (prefix round2/). The packets quote product titles and descriptions, so they stay out of git; the hashes
  let them be checked.

## Commands

```
python origin_iso_eval.py census <main.duckdb> <branch.duckdb> <out-dir>   # census.json, packet.json
python origin_iso_eval.py agree  <out-dir>                                   # A vs B; packet_C.json
python origin_iso_eval.py settle <out-dir>                                   # A, B, C -> labels.json
python origin_iso_eval.py score  <out-dir>                                   # the seed and macro against labels.json
```

`score` renders `dbt/macros/origin_iso.sql` with Jinja over the seed CSV, the same SQL the build runs.

## Decisions for the owner

- **Display names.** ISO's short names include formal forms the site now shows: "Bolivia, Plurinational State of",
  "Tanzania, United Republic of", "Congo, The Democratic Republic of the", "Venezuela, Bolivarian Republic of",
  "Lao People's Democratic Republic", "Viet Nam", "Côte d'Ivoire". dbt gives each origin a display name,
  `dim_coffee.origin_country_display` (also in `mart_new_arrivals`), which is what the site shows: with the dbt var
  `origin_names: common` (the default, recommended) a common name from `dbt/seeds/origin_display_name.csv` (Bolivia,
  Tanzania, Laos, Vietnam, Venezuela, DR Congo, Côte d'Ivoire), with `iso` the ISO short name. One switch in
  `dbt/dbt_project.yml`; the weekly build and the site, which reads the served release, follow it.
  `origin_country` and `origin_country_code` stay ISO's in every table.
- **Chart labels.** The site's bar charts leave a fixed label column; an ISO short name as long as "Tanzania, United
  Republic of" would spill past it. With common names no label is that long, and no long ISO name is in the top 8 or
  12 today; left as is (review of #144).
- **New Guinea** (13 coffees, mostly the rules' "New Guinea Highlands" style titles) has no country: the island is
  Papua New Guinea and Indonesia's Papua. Most such coffee is Papua New Guinea's, but that is a guess the mapping
  doesn't make.
- **Hawaii and Puerto Rico from the rules.** The rules fold Hawaii and Kona into "United States" before anything is
  stored, so their region isn't kept for the rules' coffees (Claude's rows keep theirs in `origin_region`). Puerto
  Rico now stays "Puerto Rico" (PR); 12 of Claude's stored extractions from one Puerto Rican roaster were folded into
  "United States" when they were captured, and stay US until those products are enriched again (the 12 listings that mention Puerto Rico; one of them is a choice of three countries).
