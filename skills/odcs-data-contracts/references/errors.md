<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Validation errors and their fixes

Each row is a failure the ODCS project keeps a negative test for (`src/script/negative-tests/` in an ODCS
checkout), with the message `scripts/validate_contract.py` prints for it.

If you run a plain JSON-schema validator (ajv, `check-jsonschema`, an IDE) instead, many of these show up as
`Unevaluated properties are not allowed ('name', 'deprecated' were unexpected)` on the parent object. That
message names keys that are *fine*; it's a side effect of ODCS's draft 2019-09 schema. Look for a nested
error, or run the script, before renaming anything.

## Top level

| Message | Cause | Fix |
| --- | --- | --- |
| `'id' is a required property` (or `version`, `status`, `kind`, `apiVersion`) | a required top-level field is missing | add it; `id` is any stable string, `version` the contract's semver |
| `$.apiVersion: 'v4.0.0' is not one of [...]` | unknown standard version | use a released one, normally `v3.2.0` |
| `$.kind: ... is not one of ['DataContract']` | wrong kind (`DataModel`, `DataProduct`...) | `kind: DataContract`; data products are ODPS, a separate standard |
| `` `foo` is not an ODCS field here `` | a key the standard doesn't define | move it to `customProperties` (`property`/`value`) |
| `$.customProperties[0].vendor: ... not of type 'string'` | `vendor` must be a string | quote it; lowercase id like `acme` |

## Schema objects and properties

| Message | Cause | Fix |
| --- | --- | --- |
| `'name' is a required property` | element without `name` | every object and property needs `name` |
| `logicalType: 'binary' is not one of [...]` | not a logical type | pick from `string date timestamp time number integer object array boolean map vector`; precise type goes in `physicalType` |
| `deprecated: 'maybe' is not of type 'boolean'` | `deprecated` is true/false | `deprecated: true`, explain in `description` |
| `primaryKeyPosition: '${KEY_POS}' is not of type 'integer'` | variables only work in string fields | write the number |
| `$.schema[0].id: '...' does not match '^[^\s.#/\\@!%&^]+$'` | id contains `.` `#` `/` `\` `@` `!` `%` `&` `^` or whitespace | use `_` or `-`, e.g. `sales_orders` |
| `'map' is a required property` | `logicalType: map` without its `map` block | add `map: {key: {...}, value: {...}}` |
| `map: 'key' is a required property` / `'value'` | map block incomplete | both `key` and `value` are required, each a property shape |
| `logicalTypeOptions: 'dimensions' is a required property` | `vector` needs a size | `logicalTypeOptions: {dimensions: 1536}` |
| `enum: [] should be non-empty` | empty enum | list at least one `{value: ...}` or drop `enum` |
| `enum: [...] has non-unique elements` | duplicate enum entries | each `value` once |
| `enum[0]: 'value' is a required property` | enum entries are objects | `- value: pending` (optionally `label`, `description`) not `- pending` |
| `synonyms[0]: 'synonym' is a required property` | wrong key in a synonym entry | `- synonym: revenue` |

## Quality rules

| Message | Cause | Fix |
| --- | --- | --- |
| `type: 'unknown' is not one of ['text', 'library', 'sql', 'custom']` | unknown rule type | use one of the four |
| `'metric' is a required property` | `type: library` without a metric | add `metric:` (or change the type) |
| `metric: '...' is not one of [...]` | not a library metric | `nullValues missingValues invalidValues duplicateValues rowCount`; anything else is `sql` or `custom` |
| `'query' is a required property` | `type: sql` without `query` | add the SQL; `{object}` and `{property}` stand for the table and column |
| `'implementation' is a required property` | `type: custom` without it | add `engine` and `implementation` (a path, code or a mapping) |
| `mustBeBetween: [100] is too short` | range operators take exactly two numbers | `mustBeBetween: [100, 120]` |
| `` `mustBeLessThan` has an invalid value: 'five' is not of type 'number' `` | operators take numbers | write the number |
| `dimension: 'validity' is not one of [...]` | not an ODCS dimension | `accuracy completeness conformity consistency coverage timeliness uniqueness`; validity is usually `conformity` |
| `` `element` is not allowed here `` | v2-style targeting | delete it: a rule applies to the object/property it is nested under |
| `` `rule` is not allowed here `` | deprecated v2 key | use `metric` with an operator, or `sql`/`custom` |

## Relationships

| Message | Cause | Fix |
| --- | --- | --- |
| `must not set 'from' here` | `from` on a property-level relationship | delete `from`: the property itself is the source |
| `missing required: 'from', 'to'` | object-level relationship incomplete | both are required at object level; strings, or equal-length arrays for composite keys |

## Servers

| Message | Cause | Fix |
| --- | --- | --- |
| `missing required: 'server', 'type'` | every server needs both | `server` is your identifier, `type` the technology |
| `type: 'mongodb' is not one of [...]` | unsupported type | the closest listed type, or `custom` with details in `customProperties` |
| `'host' is a required property` (or `database`, `schema`, `catalogUrl`, `location`...) | the type's own required fields | see the type in `docs/infrastructure-servers.md`; use `${VARS}` for values you don't want in git |
| `encoding: 65001 is not of type 'string'` | encoding is a name | `encoding: UTF-8` |

## SLA

| Message | Cause | Fix |
| --- | --- | --- |
| `slaProperties[0]: 'value' is a required property` | entry without a value | each entry needs `property` and `value` |
| `slaProperties[0].authoritativeDefinitions[0]: 'type' is a required property` | link without a type | every authoritativeDefinitions entry needs `url` and `type` |

## Valid against one copy, invalid against another

The rolling `odcs-json-schema-vX.Y.Z.json` gets silent fixes. If the user's CI or IDE disagrees with the
script, compare the SHA-256s the script prints and pin a dated snapshot
(`odcs-json-schema-vX.Y.Z-YYYYMMDD.json`) where reproducibility matters.
