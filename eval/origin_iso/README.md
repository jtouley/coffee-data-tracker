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
- Weights: the coffees in the change's dim_coffee carrying each spelling (21,928 coffees have one; a spelling only an
  older product version carries weighs 0).
- The 2026-11-09 release had 52 values in public_market_weekly for about 46 countries; the change's build has 45 (one
  per country), and 45 distinct countries over all of public_coffee, down from 120 values.

## Labels

- **Labelers:** Claude subagents on Claude Haiku (model chosen and approved by the owner), two independent labelers
  (A, B) over the full packet and a third (C), blind to A and B, over the spellings they disagree on. The settled
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
  "Lao People's Democratic Republic", "Viet Nam", "Côte d'Ivoire". The `iso_3166_1` seed carries iso-codes' common
  names (Bolivia, Tanzania, Venezuela, Laos, Vietnam) if the site should show those while the data keeps ISO's.
- **New Guinea** (13 coffees, mostly the rules' "New Guinea Highlands" style titles) has no country: the island is
  Papua New Guinea and Indonesia's Papua. Most such coffee is Papua New Guinea's, but that is a guess the mapping
  doesn't make.
- **Hawaii and Puerto Rico from the rules.** The rules fold Hawaii and Kona into "United States" before anything is
  stored, so their region isn't kept for the rules' coffees (Claude's rows keep theirs in `origin_region`). Puerto
  Rico now stays "Puerto Rico" (PR); 12 of Claude's stored extractions from one Puerto Rican roaster were folded into
  "United States" when they were captured, and stay US until those products are enriched again (the 12 listings that mention Puerto Rico; one of them is a choice of three countries).
