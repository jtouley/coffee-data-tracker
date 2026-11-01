<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Agent skills for ODCS

Two [Agent Skills](https://agentskills.io): folders of plain Markdown instructions, plus a script, that
AI coding and data agents can load when a task calls for them. They follow the open Agent Skills format,
so any agent that supports it can use them; nothing in them is tied to one vendor.

> Drafted in [coffee-data-tracker](https://github.com/jtouley/coffee-data-tracker), which publishes an ODCS
> v3.2.0 contract, as a proposal for [bitol-io/open-data-contract-standard](https://github.com/bitol-io/open-data-contract-standard).
> Paths such as `docs/` and `src/script/negative-tests/` refer to that repository.

| Skill | What it does |
| --- | --- |
| [`odcs-data-contracts`](odcs-data-contracts/SKILL.md) | Writes ODCS contracts from DDL, dbt, schemas or descriptions; validates them and explains failures; fixes and reviews them; upgrades older versions. |
| [`odcs-context`](odcs-context/SKILL.md) | Builds the v3.2.0 `context` block, synonyms and measures by asking the data's owners, then writes and validates it. |

The ODCS documentation in [`docs/`](../docs/README.md) is normative. Where a skill and the docs disagree,
the docs win; please open an issue.

## Use them

Copy or link a skill's folder into the skills directory your agent reads (see its documentation), keeping
the folder name. Install both side by side: `odcs-context` uses the validator from `odcs-data-contracts`.

The validator also runs on its own:

```bash
pip install pyyaml "jsonschema>=4.18"
python skills/odcs-data-contracts/scripts/validate_contract.py my-contract.odcs.yaml
```

It validates against the schema matching the contract's `apiVersion` (a local copy if it finds one, else a
download), names the real cause of each error instead of draft 2019-09's "unevaluated properties"
messages, and warns about deprecated fields. For example, on ODCS's own negative test
`stable-id-denied-characters.odcs.yaml`, a plain draft 2019-09 validator's best match is

```text
$.schema[0]: Unevaluated properties are not allowed ('id', 'name', 'physicalType' were unexpected)
```

which names three keys that are fine. The validator reports the cause:

```text
ERROR   $.schema[0].id: 'fdir.dataset' does not match '^[^\\s.#/\\\\@!%&^]+$'
```

## Example: building `context` by interview

`odcs-context` doesn't write AI guidance from the schema alone. It reads the contract, then asks the data's
owner what the contract can't say, in a few short rounds, each question with an answer proposed from the
contract. This is a real run on coffee-data-tracker's
[public catalog contract](https://github.com/jtouley/coffee-data-tracker/blob/main/contracts/public-catalog.odcs.yaml):
four tables of weekly facts about US specialty coffee roasters' catalogs.

The agent first listed what the contract already settles, so it wouldn't ask: what one row is in each table,
that `catalog_status` in (`available`, `preorder`) means buyable, the join keys, USD prices, weekly runs at
most 8 days old, the facts-only rule, and that attributes come from rules or an LLM. Then five rounds:

| Round | Asked (proposed answer in brackets) | Owner chose |
| --- | --- | --- |
| 1. Audience | Who reads the context? Which questions do people ask? Which wrong assumption most needs heading off? | Public data users; finding coffees and roaster lookups; "attributes are exact" and "it's live" |
| 2. Traps | Which rows count when finding coffees? [`catalog_status IN ('available','preorder') AND is_wholesale = false`] Which roasters are current? [`status = 'active'`] How to handle missing attributes? Which price column? [`price_per_100g` to compare] | As proposed; active only; null means unknown, link `source_url`, match flavours on `flavor_categories`; give both prices, labelled |
| 3. Prohibitions | Which rules bind every table? How to treat opted-out roasters? Is there a license to cite? | Not live, facts only, no quality verdicts, no buying on the user's behalf; never mention opted-out roasters; the repo's MIT License |
| 4. Questions | Which canonical questions? Any known-good answers? | Six questions; questions only, no answers until verified |
| 5. Vocabulary | Which synonyms? | Coffee, roaster and price words |

The agent then showed the draft in plain language, marking each statement "from the contract" or "you
said", and the owner approved it. An excerpt of what it wrote:

```yaml
context:
  instructions: >-
    Weekly facts about the public catalogs of US specialty coffee roasters in this project's registry, not every
    US roaster. Every row is as of a weekly run (release_run_date) at most max_run_age_days (8) days old; it is
    never live. ... Origin, process, varietals, roast level and tasting notes come from rules or an LLM reading
    the roaster's own text (attributes_source), so they can be wrong or missing; a null means not stated, never
    "none". When recommending a coffee, give its source_url so the user can check the roaster's own page.
  constraints:
  - id: not-live
    constraint: Do not present any row as current stock or price; say which release_run_date it is as of.
  - id: no-quality-verdicts
    constraint: Do not rank or judge roasters or coffees as best or worst; the data holds catalog facts, not
      quality.
  - id: no-opted-out-roasters
    constraint: Never mention roasters with status opted_out, or their coffees, or count them, even in questions
      about the registry.
schema:
- name: public_coffee
  context:
    instructions: >-
      ... For coffees someone can buy now, filter catalog_status IN ('available', 'preorder') AND
      is_wholesale = false ... Match broad flavours (e.g. fruity) on flavor_categories; use tasting_notes only
      for the roaster's exact words.
    verifiedStatements:
    - question: Which washed Ethiopian coffees can I buy right now?
    - question: Show me fruity light roasts under $20 a bag.
  synonyms:
  - synonym: beans
```

The questions have no `answer`: agents are told to repeat a verified answer word for word, so the skill
records one only when the owner gives or confirms it. Everything else in the contract was left as it was;
two suggestions outside `context` (citing the license in `authoritativeDefinitions`, and a clearer
`roaster_id` description) went to the owner instead of being edited in.

## Keep them current

```bash
python skills/odcs-data-contracts/scripts/selftest.py .
```

This runs the validator over `docs/examples/` (all must pass) and `src/script/negative-tests/` (all must
fail, each with an explained error). Add it to CI next to `validate-examples.sh` so a change to the
standard that the skills don't cover shows up as a failure. When you add a field or a negative test, check
whether `odcs-data-contracts/references/` needs a line too.
