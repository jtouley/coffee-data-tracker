# dbt convention: `dbt/` and `dbt_importers/`

One set of rules for both dbt projects: the public one (`dbt/`, project `coffeedata`) and the private importers
one (`dbt_importers/`). New and touched code follows it. Existing code that doesn't is listed in
[section 12](#12-where-the-projects-stand); it is brought into line one reviewed change at a time, never in a
wholesale rewrite.

## 1. The owner's standing rules

These five are not up for discussion in a review. The rest of this file is how the projects keep them.

1. **ELT.** Ingest, enrichment and classify jobs only load. Every derived value (is it coffee, attributes, grams,
   status, prices per 100 g, flags) is computed in dbt, after raw. A value a crawler once recorded
   (`fetches.coffees`, `snapshots.is_coffee`, `variants.grams`) is an input, never an output. The site, the MCP
   server and the app read published models and re-derive nothing.
2. **Builds are idempotent and ingestion is immutable.** The same raw builds the same output, every time, and
   stored raw is never rewritten or deleted. [Section 2](#2-idempotent-builds-immutable-ingestion) says what that
   forbids.
3. **Every entity lives in a dimension, with flags.** Every entity seen in raw has a row in its `dim_` for good.
   An exclusion is a flag on that row (`is_coffee`, `is_alias`, `is_chain`, `opted_out`, `is_listed`,
   `is_publishable`), and facts carry the dimension's key and the flags they need. Rows are removed only in
   `mart_` and `public_` models, only by a `where` on named flags.
4. **Published outputs have contracts.** See [section 6](#6-contracts).
5. **Heuristics are measured, not argued.** A change to a rule whose quality is "how often is it right" follows
   the evaluation steps in [CLAUDE.md](../../CLAUDE.md); accuracy floors are only ever raised.

## 2. Idempotent builds, immutable ingestion

A dbt build is a function of the raw it reads, the repository at one commit and the build's vars. Run it twice on
the same three and every model has the same rows. A release can then be rebuilt and checked by anyone, a re-run of
a week changes nothing, and a difference between two builds always has a cause in raw, code or vars.

**Raw is append-only.** dbt never writes raw. A correction, an override or a deletion is a new record, and staging
picks the latest per key (`stg_bags`, `stg_shots`, `stg_run_overrides`). Nothing rewrites or deletes a stored raw
file: not a job, not a migration, not a fix for bad data. Bad data is superseded by a later record or held out by
a flag (`stg_run_quality.counted`).

**What a model must not do:**

| Forbidden | Why | Instead |
|---|---|---|
| The wall clock: `now()`, `current_timestamp`, `current_date`, `today()`, dbt's `run_started_at` | Two builds of one lake differ by when they ran | A build var set once by `coffeedata build` (`as_of_run`, `today`), or a timestamp from raw |
| Randomness: `random()`, `uuid()`, `using sample` | Never the same twice | A hash of the row's key (`md5(...)`) |
| A pick with no total order: `row_number()`, `arg_max()`, `arg_min()`, `first()`, `distinct on`, `limit` whose `order by` can tie | DuckDB returns tied rows in whatever order it read them | End the `order by` with columns that can't tie (the key, or the value itself), and say so in a comment, as `stg_run_overrides` and `stg_verdicts` do |
| `any_value()` over values that can differ within the group | The same | `max()`, or `arg_max()` with a total order. `any_value()` is fine only where the group key decides the value (`body_text` by `body_hash`) |
| `list()` or `string_agg()` without `order by` | The list's order is the read order | `list(x order by x)` |
| The session's time zone: a timestamp cast to a date or to text without a zone | A rebuild on a laptop differs from CI's | State the zone (`at time zone 'UTC'`); `profiles.yml` also pins the session to UTC |
| Reading an earlier build's tables: the database file, `marts/`, a release, the lake's `builds` records | The output then depends on which builds ran before | Derive from raw. Two exceptions, below |
| Incremental models, snapshots | Their output depends on the state the last run left | Rebuild everything from raw on every build ([section 9](#9-materializations)) |
| A Python model that reads a file, the network or the clock, or iterates a `set` or an unsorted `dict` into its output | The same | A pure `row()` over a SQL `*_inputs` model ([section 8](#8-python-models)) |

**The two exceptions**, both outside the release tables. A gate test may compare this build with earlier ones (the
volume check and the anomaly bands read the lake's `builds` records through `stg_builds`), because that is what the
gate is for. And `release_changes`, a report-only model run after the gate, compares this build's public tables
with the served release. No model that feeds a release table reads either.

**The per-build columns.** `release_build_id` names the build a row came from, so it differs between two builds by
design (`quality.PER_BUILD_COLUMNS`). It comes from a var, not from the clock, and nothing else may differ.

**The test.** `tests/test_reproducible.py` builds one synthetic lake twice through `coffeedata build`. The second
build reads a copy with every file's rows, and every folder's files, in reverse order, in another time zone. Every
model, tables and views, must have the same rows in both, per-build columns aside (`stg_builds`, which is about
the builds themselves, is the one model left out). The lake holds a tie for every "latest" pick on purpose. A new
pick gets a tie in that fixture.

## 3. Layers, folders and prefixes

| Layer (folder) | Prefix | Holds | Reads | Materialization |
|---|---|---|---|---|
| raw (the lake, outside dbt) | none | Append-only files from jobs | n/a | n/a |
| staging (`staging/`) | `stg_<table>` | One model per raw table or config file: select, rename, cast, decode, null-key handling, dedupe to the table's natural grain (the latest record per id) | The only layer that reads raw | View; a table when two or more models read it or the read is remote, with the reason as a comment in `dbt_project.yml` |
| intermediate (`intermediate/`) | `int_<entity>_<what>` | Business rules: the classifier, extraction, the run walk, suspect runs, state, joins across entities | Staging, intermediate, seeds | Table |
| marts (`marts/`) | `dim_<entity>`, `fct_<event>`, `mart_<use>` | Dimensions (one row per entity, ever), facts (one row per event), aggregates for one consumer | Intermediate, staging, other marts | Table |
| ops (`marts/ops/`) | `ops_<what>` | Pipeline and gate tables: run completeness, the build profile, release changes, parity checks | Anything, published models included (to reconcile) | Table |
| published (`public/`) | `public_<entity>` | Projections of dimensions and facts filtered by flags; no new logic | Marts only | Table |

- Each project has one source system, so staging has no `stg_<source>__` double prefix.
- **The published layer** is any model read outside dbt: the open release (`public_*`), the marts served with a
  release (`quality.RELEASE_TABLES`) or exported for the app (`cli.MARTS`), and `importer_offers`. A served mart
  keeps its `dim_`, `fct_` or `mart_` prefix, because a rename breaks its consumers; what marks it as published is
  `access: public` and an enforced contract. A new open table takes `public_`, the prefix the ODCS contract and
  every consumer already use.
- **No upward refs.** Intermediate never reads a mart, and a mart never reads `public_`. Only `ops_` may, to
  reconcile.
- A vocabulary generated by code (`stg_importer_vocab`) reads no raw, so it is intermediate, not staging.

## 4. Reading raw

Raw is read through the `lake()` and `importer_raw()` macros, and only in staging. They union the local and the
Hub roots and skip a remote folder that doesn't exist yet, which a plain dbt source can't do.

Every raw table is also declared in a `sources:` block (description, columns from `lake.SCHEMAS` or the raw ODCS
contract, `loaded_at_field` for freshness), so lineage and docs reach raw. Whether dbt-duckdb's
`external_location` can express the multi-root read, and so replace the macros with `source()`, hasn't been
tested. A config file read with `read_csv` (`config/roasters.csv`) is declared the same way, or becomes a seed.
A file a build is handed from outside (`previous_release`) is read in `ops_` only.

## 5. Keys, grain and tests

- **A description starts with the grain**: "One row per ...". Keys are named `<entity>_id`.
- **Every model has a grain test** on the key its description claims.
  - A single-column key takes `unique` and `not_null` on the column.
  - A compound key takes the project's generic test `grain` (`dbt/tests/generic/grain.sql`) on the model:

    ```yaml
    data_tests:
      - grain:
          arguments: {columns: [roaster_id, run_date]}
    ```

    It fails on a key that more than one row has and on a null in any key column. A column that may
    legitimately be null is named in `nullable` (`mart_dial_in`'s `grinder_id`); two rows that share a null are
    still a duplicate.
  - Never `unique` over `a || '|' || b`. A null part makes the whole expression null, and `unique` skips nulls, so
    the rows most likely to be wrong pass. It also reads `('a|b', 'c')` and `('a', 'b|c')` as one key.
- **A grain that fails is reported, not weakened.** If a model has duplicates on the key it claims, the claim or
  the model is wrong: fix one, or record the failing test and its reason in the pull request. Don't add a
  `where`, a `distinct` or a wider key to make the test pass.
- **`severity: warn` needs a written plan** for when it becomes an error, beside the test (as `band_k` has in
  `dbt_project.yml`).
- Tests use `data_tests:` with `arguments:`. A rule across tables is a singular test in `tests/`.
- **A `ref()` inside a var check is declared outside it.** A test that does its work only when a build var is set
  (`{% if var('as_of_run') %} ... ref('run_completeness') ... {% endif %}`) starts with
  `-- depends_on: {{ ref('run_completeness') }}`. Without that line dbt sees the dependency only when the var is
  set, so lineage, `--select model+` and the build order would depend on runtime vars. The same goes for a ref
  inside a macro the test calls.
- **No silent drops.** A row-removing `where` in staging, intermediate, a dimension or a fact names a flag that
  stays visible downstream, or is a null-key filter with a warn test that counts what it removes.

## 6. Contracts

A published model has `contract: {enforced: true}`, `access: public`, a `data_type` and a description on every
column, and a `not_null` constraint on every required column.

For the open release the ODCS file (`contracts/public-catalog.odcs.yaml`) is the definition and dbt enforces it:
a change starts in the contract. `tests/test_contracts.py` fails when dbt's columns, types, required and key
columns, allowed values or descriptions differ from it. The descriptions in `dbt/models/schema.yml` are the
contract's, word for word.

A breaking change (a column removed, renamed or retyped) bumps the contract's major version and uses a dbt model
version for the migration window.

## 7. Documentation

- Every model has a description in YAML, grain first. The SQL header comment keeps the "why"; dbt docs and
  catalogs don't read it, so the description has to stand alone.
- Columns: all of them on dimensions, facts and published models; keys and derived columns elsewhere. A column a
  model passes through is described in the upstream model's words.
- Seeds and sources are documented.
- **Where the YAML lives.** dbt takes exactly one YAML entry per model, so a model is described in one file.
  `models/schema.yml` and `models/unit_tests.yml` hold what they hold today, in layer order. A model with no entry
  yet gets one in `_<layer>__models.yml` beside its SQL (`models/marts/_marts__models.yml`). The public tables'
  entries stay in `models/schema.yml`: `tests/test_contracts.py`, `tests/test_raw_contracts.py` and
  `contract_docs.py` read that file by name.

## 8. Python models

A Python model is allowed only to run a rule that lives in `src/coffeedata` and that another component also runs
(ingest, enrichment, the registry, an eval), so the rule has one implementation. Its shape:

- a SQL `*_inputs` model that dedupes by an input hash, so the rule runs once per distinct input;
- a pinned `pyarrow` `SCHEMA`;
- a pure `row()`: no I/O, clock or network, and no order taken from a `set`;
- `materialized="table"` and a grain test;
- its logic tested by pytest on `row()`, since a dbt unit test can't mock a Python model.

It lives in intermediate.

## 9. Materializations

As in the table in [section 3](#3-layers-folders-and-prefixes). No incremental models and no snapshots: every build
re-derives everything from raw ([section 2](#2-idempotent-builds-immutable-ingestion)). Revisit one model only
when its build time is measured as a problem; `docs/ingestion/DESIGN.md` follow-up 20 is the first candidate.

## 10. Unit tests

Every SQL model with a rule (a `case`, a window, a non-key join or a filter) has at least one dbt unit test in
`unit_tests.yml`. A bug fix adds the case that reproduces the bug ([CONTRIBUTING.md](../../CONTRIBUTING.md)).

## 11. SQL style

For new and touched code: an explicit join type (`inner join`, `left join`), `as` for every alias, one CTE per
step. This is the importers project's style, the newer and more readable of the two; the public project is not
reformatted wholesale.

## 12. Where the projects stand

Measured from `dbt parse` manifests in November 2026. Met by both projects: `stg_`/`int_`/`dim_`/`fct_` prefixes
and layer folders, `data_tests:` with `arguments:`, the Python model shape, full rebuilds from raw, documented and
tested seeds, raw never written by dbt.

Met by the public project since the change that added this file: a description on every model, a grain test on
every model, no `||` keys, var-gated refs declared, the public tables' column descriptions.

Not yet met, each for a change of its own:

| Rule | What is open |
|---|---|
| 1.2 Idempotent | `run_completeness` falls back to `now()` for a run with no fetch; `stg_shots`, `stg_bags` and `stg_equipment` pick the latest record by `logged_at` alone, so two records of one id in the same second tie |
| 1.3 Dimension and flags | `dim_coffee` and `fct_price_event` filter out products that aren't coffee and alias roasters instead of flagging them; no dimension holds every product. `public_roaster` decides eligibility itself, and `public_coffee` drops rows on a null `url` |
| 1.1 ELT | `dim_roaster.status` still reads the crawl's recorded coffee count where there is one (`docs/ingestion/DESIGN.md` follow-up 13) |
| 3 Layers | The run walk, the suspect-run rule, the classifier and the bag-weight rules sit in staging; `int_current_catalog` reads `dim_coffee`; `release_changes`, `run_completeness` and `mart_build_profile` are ops tables under mart names |
| 4 Reading raw | Neither project declares `sources:`. `run_completeness`, `mart_build_profile`, `int_fetch_product_inputs` and `int_escaped_tag_inputs` read raw outside staging; `stg_registry` reads `config/roasters.csv` undeclared |
| 5 Grain | `dbt_importers` still builds compound keys with `\|\|`, and has no `grain` test yet. `stg_probes` drops `batch_id`, the only column that tells two probes of one roaster in the same second apart, so its grain test on (`roaster_id`, `probed_at`) only warns |
| 5 No silent drops | Staging's `where <key> is not null` filters have no test that counts what they remove |
| 6 Contracts | The served marts (`dim_coffee`, `dim_roaster`, `fct_price_event`, `mart_market`, `mart_new_arrivals`, `fct_shot`, `mart_bag`) have no enforced contract; no model sets `access: public` |
| 7 Documentation | Column descriptions are incomplete outside the public tables, in both projects |
| 10 Unit tests | None on `release_changes`, `mart_build_profile`, `stg_registry`, `stg_fetches`, `dim_roaster`, `mart_new_arrivals`, `stg_variant_grams_inputs`, `int_importer_inputs` |
