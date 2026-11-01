---
name: odcs-data-contracts
description: Write, convert, validate, fix and review data contracts in the Open Data Contract Standard (ODCS, Bitol, `*.odcs.yaml`, `kind: DataContract`), including the v3.2.0 AI `context` block. Use it whenever someone wants a data contract for a table, view, topic, API or dataset; turns SQL DDL, a dbt schema.yml, a JSON/Avro schema, a sample file or a plain description into ODCS; asks why a contract fails schema validation; wants quality rules, SLAs, relationships, servers or semantic/AI context added to a contract; upgrades an older ODCS (v2.x, v3.0/v3.1) contract; or wants a contract kept in sync with dbt models. Use it even when they just say "data contract" or "schema contract" without naming ODCS.
---

<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# ODCS data contracts

A data contract is an agreement between the people who produce a dataset and the people who use it. ODCS is
the open YAML standard for writing one down (https://github.com/bitol-io/open-data-contract-standard). The
JSON schema checks the shape; the agreement itself is only as good as the facts in it. So this skill has two
jobs: produce YAML that validates, and keep every claim in it traceable to something the user said or showed.

## Workflow

1. **Find the version.** New contracts use `apiVersion: v3.2.0` unless the project already pins another
   (look for existing `*.odcs.yaml` files or a vendored `odcs-json-schema-*.json`). When editing a
   contract, keep its version unless the user asks to upgrade.
2. **Gather the source.** DDL, dbt models, sample rows, an existing contract, or the user's description.
   Read it fully before writing; columns, keys, nullability and allowed values come from here.
3. **Write or edit the contract.** Follow [references/authoring.md](references/authoring.md): the skeleton,
   type mapping, and the shapes for quality rules, relationships, SLAs and servers. For the `context` block
   (guidance for AI agents and BI tools) read [references/context-block.md](references/context-block.md).
4. **Validate, then fix until valid.**
   ```bash
   python <skill-dir>/scripts/validate_contract.py path/to/contract.odcs.yaml
   ```
   It picks the schema matching `apiVersion` (a local copy if one is in the project, else a download, cached),
   reports the root cause of each error rather than jsonschema's often misleading top-level message, and
   warns about deprecated fields. Look up unfamiliar errors in [references/errors.md](references/errors.md).
   Needs `pyyaml` and `jsonschema`; if they can't be installed, say the contract is unvalidated.
5. **Report.** Say what the contract covers, what you inferred and from what, and what is left as a
   `TODO` for the user to decide. Paste the validator's final line so the user sees which schema it passed.

If the project keeps contracts next to dbt models, also read [references/dbt.md](references/dbt.md): the
contract and the models should say the same thing, and a test can hold them to it.

## Facts, not filler

The schema accepts almost any value for an owner, an SLA or a threshold, so a validator can't catch an
invented one, and a consumer who reads `latency: 1 d` will plan around it. Therefore:

- Take names, types, keys, nullability and allowed values from the source. If the source doesn't say a
  column is required or unique, don't mark it so.
- Leave out what nobody stated (team, SLAs, support channels, pricing, servers). If the section matters to
  the user's purpose, add it with a clear `TODO` in `description` or a YAML comment, and list it in your
  report. Don't guess a hostname, an owner or an SLA value.
- When you infer something reasonable (e.g. `logicalType: integer` from `BIGINT`, `required: true` from
  `NOT NULL`, a primary key from a `PRIMARY KEY` constraint), that's fine; it's derived, not invented.
- Secrets and environment-specific values (hosts, buckets, credentials) go in as `${VAR_NAME}` variables
  (v3.2.0), never as literals.

## Choices that trip people up

- `name` is the business-facing name; `physicalName` is the actual table/column name when it differs.
  `physicalType` is the platform type verbatim (`VARCHAR(18)`, `timestamp with time zone`).
- Every schema object and property needs `name`. `logicalType` is one of `string`, `date`, `timestamp`,
  `time`, `number`, `integer`, `object`, `array`, `boolean`, `map`, `vector`; anything else (e.g. `binary`,
  `decimal`, `json`) is invalid. Put the precise type in `physicalType`.
- Add `id` to elements that something references (relationships, quality rules others point to). Ids can't
  contain whitespace or `. # / \ @ ! % & ^`.
- Use `enum` (v3.2.0) for a closed set of values; it's checked by the schema and tools read it.
- Quality rules sit under the object or property they check: no `element` key. Library metrics
  (`nullValues`, `missingValues`, `invalidValues`, `duplicateValues`, `rowCount`) take numeric operators
  (`mustBe`, `mustBeLessThan`, `mustBeBetween: [a, b]` ...). Rules that live in another tool (dbt, Soda,
  Great Expectations) are `type: custom` with `engine` and `implementation`.
- Deprecated but still valid: top-level `dataProduct` (the ODPS data product references the contract
  instead), `slaDefaultElement`, `quality[].rule`, and `team` as a list. Don't introduce them in new
  contracts; when you meet them, mention it and offer to migrate rather than silently rewriting.

## Reviewing someone's contract

Validate first, then read it as a consumer would: can I tell what one row is (`dataGranularityDescription`,
keys), what each column means, how fresh it is, who to ask, and what it must not be used for
(`description.limitations`, `context.constraints`)? Report schema errors separately from judgment calls,
and point to the line or path for each.

## When the standard and the JSON schema disagree

The standard (the `docs/` pages) takes precedence over the JSON schema, which is a companion and may lag.
The rolling `odcs-json-schema-vX.Y.Z.json` also gets silent fixes, so a contract can pass against one copy
and fail against a newer one; that's why the validator prints the schema's SHA-256. If a contract follows
the docs but fails the schema, say so plainly rather than bending the contract, and suggest the user raise
it with the ODCS project.
