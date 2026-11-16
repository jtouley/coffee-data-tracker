# Why Germack Coffee (germackcoffee.com, Detroit) is missing from the catalog

Investigated 2026-11-16 against `origin/main` at `36d5e3d` and read-only copies of the lake
(`hf://datasets/jtouley/coffee-data`, as published after the 2026-11-09 run). No roaster site was fetched, no
discover, probe or crawl job was run, and nothing was written to the lake.

## Where it drops out

**Stage 1–2: the registry. It was never in the roaster list, so every later stage never saw it.**
The catalog's roaster list today is `config/roasters.csv` alone. The lake's `registry/` table, where discovery
(Overture, OSM) and the add-roaster issue form write, is still empty. Germack has no row in either one. Its
Overture listing evidently didn't pass the discovery filter when the seed was built, and the monthly discover job,
which also asks OSM, has never run in production.

| Stage | Result |
|---|---|
| 1. Seed `config/roasters.csv` | **Absent.** No row matches `germack` in any casing, with or without www. |
| 2. Lake registry (Overture/OSM) | **Absent: the table is empty.** No discover job has run yet. |
| 3. Probe / platform | Never probed: the lake `probes/` table is empty, and the seed has no row. |
| 4. Crawl fetches | Never fetched: 0 of 13,495 `fetches` rows and 0 of 8,288 `run_manifests` rows. |
| 5. Classification / dbt | No products: 0 rows in `dim_roaster`, `dim_coffee`, `public_roaster` or `public_coffee`. |
| 6. Site | Not excluded by the site: it has nothing to build from. |

## Evidence

**Seed.** `git grep -niE 'germack|germac|germak' origin/main` exits 1 with no output: no match in
`config/roasters.csv` or anywhere else in the repo. The `ccr-53fdc95c-2qdy7y` branch gives the same result. The
seed has 3,203 roasters, 89 of them in MI. These are its Detroit-city rows:

```
anthologycoffee.com,Anthology Coffee,Detroit,MI,curated,1,,true,,false,anthologycoffee.com,woocommerce,2026-10-26,US,
coffeeha.us,Coffeehaus,Detroit,MI,overture,1,,true,false,false,coffeeha.us,shopify?,2026-10-26,US,
dessertoasiscoffee.com,Dessert Oasis Coffee Roasters,Detroit,MI,overture,2,,false,true,false,docr.coffee,shopify,2026-10-26,US,
fausthausrc.com,Faust Haus Roasting Co.,Detroit,MI,overture,1,,false,true,false,www.fausthausroasting.com,wix?,2026-10-26,US,
greatlakescoffee.com,The Great Lakes Coffee Roasting Company,Detroit,MI,overture,5,,false,true,false,greatlakescoffee.com,shopify,2026-10-26,US,
mgmgranddetroit.mgmresorts.com,The Roasted Bean,Detroit,MI,overture,1,,false,true,false,www.mgmgranddetroit.mgmresorts.com,,2026-10-26,US,
roastingplant.com,Roasting Plant Coffee,Detroit,MI,overture,7,,false,true,false,roastingplant.com,shopify,2026-10-26,US,
```

**Lake registry and probes.** The dataset's file list holds one file each under `registry/` and `probes/`:
`registry/source=schema/schema.parquet` and `probes/run_date=0000-00-00/batch=schema.parquet`. Both have 0 rows.
`job_manifests` holds only these jobs: `crawl` (47 rows), `merge` (6) and `enrich` (4). There is no `discover` and
no `probe` row.

**Why discovery never ran.** The discover job and its schedule were added in #132 (`5fcf838`, 2026-11-07):
`src/terraform/ingestion.tf`, `"discover" = { job = "discover", cron = "17 3 1 * *" } # the 1st of each month`.
The first scheduled run after that commit is **2026-12-01**. So the only discovery behind today's list is the
one-off Overture scan that built the seed (probed 2026-10-26). OSM has never been asked: the seed's `sources` are
`overture` 2,993, `curated` 114 and `curated+overture` 96, with no `osm`.

**Fetches, release and marts.** Searching every column of every row, cast to text, for `germack` finds 0 rows in
`fetches/` (all six run dates, 2026-10-26 to 2026-11-09), `run_manifests/`, `run_overrides/`,
`marts/dim_roaster`, `marts/dim_coffee`, and every table of release
`releases/run_date=2026-11-09/build=20261110T061836-971bfe/`. That release's `dim_roaster` has 3,205 rows,
against the seed's 3,203 roasters, since the lake registry has none to add. Its `public_roaster` has 1,852.

## Root cause

The Overture scan that built the seed keeps a US place only if Overture tagged it a roastery, or if "roast" is in
its name and it is some kind of coffee place (`src/coffeedata/discover.py`, `overture()`):

```sql
AND (taxonomy.primary = 'coffee_roastery'
     OR list_contains(taxonomy.alternates, 'coffee_roastery')
     OR (names.primary ILIKE '%roast%'
         AND (taxonomy.primary ILIKE '%coffee%' OR basic_category ILIKE '%coffee%'
              OR names.primary ILIKE '%coffee%'))))
```

"Germack Coffee" has no "roast" in its name. It could only have entered by its Overture category being
`coffee_roastery`, and it did not enter. (Its actual Overture record was not looked up; see below.) OSM
(`craft=coffee_roaster` / `roastery=yes`) is the second net, but it has never been asked. The add-roaster issue form
has never been used for it.

**This is a general gap, not a Germack-specific fault.** Roasters whose name lacks "roast" and that Overture doesn't
tag `coffee_roastery` are invisible to discovery:

- In the seed's Overture rows, 2,600 of 3,089 entered with "roast" in the name. Only 732 (23.7%, Wilson 95% CI
  22.2–25.2%) carry the `coffee_roastery` tag. Overture tags few roasteries as such, so the name test does most of
  the work.
- **Recall, measured on the curated list.** The 210 curated roasters (`curated` + `curated+overture`, all US) are an
  independent, hand-picked sample of real roasters that sell online. The Overture filter found 96 of them:
  **recall 45.7%, Wilson 95% CI 39.1–52.5%.** Among the 114 it missed are Anthology Coffee (Detroit), Madcap,
  Sparrows, Populace (MI), Intelligentsia, Metric, Big Shoulders (IL) and Crimson Cup (OH). Only 3 of the 114
  missed names contain "roast", which is the same pattern as Germack.
- **Estimate of roasters missing the same way.** If that recall holds for all US roasters, the 3,089 the filter
  found are about 46% of roughly 6,800. That leaves **about 3,700 roasters undiscovered (range about 2,800–4,800,
  from the recall CI).** This is rough. The curated list leans toward well-known specialty roasters, and it is not a
  random sample. Some of the 3,089 Overture rows are cafes or non-roasters, which would lower the count. Roasters
  with no Overture listing or no website on it would raise it. Read it as "thousands, about half", not as a precise
  figure.

## Smallest fix (not implemented)

1. **For Germack now:** the owner files the repo's **Add a roaster** issue form. `issue-forms.yml` acts only on
   the repo owner's issues. The form is (`.github/ISSUE_TEMPLATE/add-roaster.yml`) with
   Name `Germack Coffee`, Website `https://germackcoffee.com`, State `MI`. The `issue-forms` workflow runs
   `coffeedata add-roaster`. That writes a `registry/source=manual/germackcoffee.com-….parquet` row
   (`tagged_roastery: true`, an open source, so `stg_registry` makes it publishable), probes the site once and
   publishes. The next weekly run fetches its catalog. `registry.normalize_domain` and `is_storefront_host` accept
   it: `https://www.GermackCoffee.com/` becomes `germackcoffee.com`, `storefront: True`, checked offline. File it
   after today's crawl finishes, since the form probes the site at once. An equivalent alternative is a hand-checked
   seed row in `config/roasters.csv` with `sources=curated`, which is how Anthology Coffee got in.
2. **For the general gap**, which is already planned:
   - the monthly discover job's first run, 2026-12-01, adds OSM;
   - draft PR #131 ("Admin: Sources") records the full Overture coffee-place list (`coffee_shop`/`cafe` included,
     ~47k places) as candidates the owner flags by hand, per the 2026-11-07 owner decision in `notes/NEXT-STEPS.md`;
   - that is the route for mis-tagged roasters like Germack and Rival Bros.

   No new work is needed beyond landing #131 and checking that the 12-01 discover run writes rows.

## What wasn't verified

- **Germack's Overture and OSM records.** I did not query Overture's places release or Overpass for Germack, so its
  Overture category, name, website field, and whether it is listed at all are unconfirmed. The cause could also be a
  missing or different website in Overture (for example a parent-company domain), not only its category.
- **That germackcoffee.com is a live store that sells roasted coffee online, and which platform it runs.** The site
  was not fetched, by rule. The add-roaster probe will answer this. If it's an unsupported platform, it will be
  registered but fetch nothing, and that becomes the next stage to check.
- **Whether the 2026-11-02 weekly run tried discovery in-process.** `cmd_weekly` discovers in the first week of the
  month. That run predates #132 and left no discover rows or registry files, so I treat discovery as never run. I
  did not read Cloud Run logs (no cloud CLI, by rule).
- **The size of the gap** rests on the curated list being representative. See the caveats above.
- **No existing issue or PR names Germack** (GitHub search for `germack`: 0 issues, 0 PRs). Draft PR #131 covers
  the general gap.
