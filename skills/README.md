<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Agent skills for ODCS

Two [Agent Skills](https://agentskills.io): folders of plain Markdown instructions, plus a script, that
AI coding and data agents can load when a task calls for them. They follow the open Agent Skills format,
so any agent that supports it can use them; nothing in them is tied to one vendor.

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
messages, and warns about deprecated fields.

## Keep them current

```bash
python skills/odcs-data-contracts/scripts/selftest.py .
```

This runs the validator over `docs/examples/` (all must pass) and `src/script/negative-tests/` (all must
fail, each with an explained error). Add it to CI next to `validate-examples.sh` so a change to the
standard that the skills don't cover shows up as a failure. When you add a field or a negative test, check
whether `odcs-data-contracts/references/` needs a line too.
