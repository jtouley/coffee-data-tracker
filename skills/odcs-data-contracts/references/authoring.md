<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Writing an ODCS v3.2.0 contract

The normative text is the ODCS docs, one page per section:
https://github.com/bitol-io/open-data-contract-standard/tree/main/docs (inside an ODCS checkout, `docs/`).
Worked examples for every feature are in `docs/examples/`. This page is the short path through them; open
the doc page when you need a field not covered here.

Contents: [Skeleton](#skeleton) · [Schema](#schema) · [Type mapping](#type-mapping) ·
[Quality](#quality) · [Relationships](#relationships) · [SLA](#sla) · [Servers](#servers) ·
[Team, support, roles](#team-support-roles) · [Custom properties](#custom-properties) ·
[Upgrading older contracts](#upgrading-older-contracts)

## Skeleton

Required at the top level: `apiVersion`, `kind`, `id`, `version`, `status`. Everything else is optional.

```yaml
apiVersion: v3.2.0
kind: DataContract
id: orders-contract            # stable; a UUID or a readable slug
name: Orders
version: 1.0.0                 # the contract's own semver: bump major on breaking changes
status: draft                  # proposed | draft | active | deprecated | retired
domain: sales
description:
  purpose: What the data is for.
  usage: How consumers should use it.
  limitations: What it must not be used for, known gaps.
schema:
  - name: orders
    physicalName: orders
    physicalType: table
    description: One row per order.
    dataGranularityDescription: One row per order_id.
    properties:
      - name: order_id
        logicalType: string
        physicalType: varchar(36)
        description: Order identifier.
        primaryKey: true
        primaryKeyPosition: 1
        required: true
        unique: true
```

Name the file `<something>.odcs.yaml` so editors pick up the schema. Quote YAML strings that look like
other types (`'08:00'`, `'2024-01-01'`, `'yes'`), since ODCS expects strings there.

## Schema

`schema` is a list of **objects** (tables, views, topics, files, API payloads); each has `properties`
(columns, fields), which can nest (`logicalType: object` with `properties`, or `array` with `items`).

Per object: `name` (required), `physicalName`, `physicalType` (`table`, `view`, `topic`, `file`...),
`description`, `dataGranularityDescription`, `quality`, `relationships`, `context`, `tags`.

Per property, the fields worth filling when the source says so:

| Field | From the source when... |
| --- | --- |
| `required: true` | `NOT NULL`, or a not_null test |
| `unique: true` | a unique constraint or test |
| `primaryKey` + `primaryKeyPosition` (1-based) | a primary key; composite keys number each column |
| `partitioned` + `partitionKeyPosition` | partition columns |
| `enum: [{value: a}, {value: b}]` | a CHECK/IN list, accepted_values test, or enum type; entries unique, at least one |
| `classification` | the user names a sensitivity level (`public`, `restricted`, `confidential`...) |
| `criticalDataElement: true` | the user says it's critical |
| `examples` | sample data, a few values |
| `logicalTypeOptions` | length, pattern, min/max, format the source states (see below) |
| `transformSourceObjects`, `transformLogic` | lineage the user gave you |
| `semanticType: measure` / `dimension` | semantic-layer use; a measure's aggregation goes in `transformLogic` |
| `deprecated: true` | a column kept for compatibility; say the replacement in `description` |
| `synonyms: [{synonym: ...}]` | alternative business names, for catalogs and natural-language tools |

`logicalTypeOptions` by type: string `minLength` `maxLength` `pattern` `format`; integer/number `minimum`
`maximum` `exclusiveMinimum` `exclusiveMaximum` `multipleOf` `format`; date/timestamp/time `format` (Java
DateTimeFormatter, e.g. `yyyy-MM-dd`) `minimum` `maximum`, timestamp/time `timezone` `defaultTimezone`;
array `minItems` `maxItems` `uniqueItems`; object `required` `minProperties` `maxProperties`; vector
`dimensions` (required) `elementType` `distanceMetric` `normalized` `embeddingModel` `embeddingModelVersion`.

Maps and vectors (v3.2.0):

```yaml
      - name: attributes
        logicalType: map
        physicalType: MAP<STRING, STRING>
        map:                     # required with logicalType: map; both key and value required
          key: {logicalType: string}
          value: {logicalType: string}
      - name: embedding
        logicalType: vector
        physicalType: ARRAY<FLOAT>
        logicalTypeOptions:
          dimensions: 1536       # required
          distanceMetric: cosine
```

## Type mapping

Keep the source type verbatim in `physicalType`; choose `logicalType` by meaning:

| Source types | logicalType |
| --- | --- |
| char, varchar, text, string, uuid, enum types | `string` |
| smallint, int, integer, bigint, tinyint | `integer` |
| decimal, numeric, float, double, real, money | `number` |
| boolean, bool, bit(1) | `boolean` |
| date | `date` |
| timestamp, datetime, timestamptz, timestamp_ntz/ltz/tz | `timestamp` (`logicalTypeOptions.timezone: true` when it carries one) |
| time | `time` |
| struct, record, json/jsonb/variant with known fields | `object` with `properties` |
| array, list, repeated | `array` with `items` |
| map<k,v>, dictionary | `map` with a `map` block |
| fixed-length float array used for embeddings | `vector` with `dimensions` |
| binary, bytea, blob | `string` with `logicalTypeOptions.format: binary` (there is no binary logicalType) |
| json/variant with unknown shape | `object` (no properties) and say so in `description` |

## Quality

Rules go in a `quality` list on the object (table-level) or property (column-level). Four types:

```yaml
    quality:
      - type: text                     # a human-readable expectation, not executable yet
        description: Totals match the finance ledger at month end.
      - metric: rowCount               # library (type: library may be omitted when metric is set)
        mustBeGreaterThan: 0
      - metric: duplicateValues        # table-level compound uniqueness
        mustBe: 0
        arguments:
          properties: [tenant_id, order_id]
      - type: sql
        query: SELECT COUNT(*) FROM {object} WHERE amount < 0
        mustBe: 0
      - type: custom                   # a check that lives in another tool
        engine: dbt
        implementation: tests/orders_amount_positive.sql
```

Column-level library metrics: `nullValues`, `missingValues` (`arguments.missingValues: [null, '', 'N/A']`),
`invalidValues` (`arguments.validValues: [...]` or `arguments.pattern: '...'`), `duplicateValues`.
Table-level: `rowCount`, `duplicateValues` with `arguments.properties`.

Operators take numbers: `mustBe`, `mustNotBe`, `mustBeGreaterThan`, `mustBeGreaterOrEqualTo`,
`mustBeLessThan`, `mustBeLessOrEqualTo`, and two-element arrays `mustBeBetween: [min, max]`,
`mustNotBeBetween`. Optional on any rule: `id`, `name`, `description`, `dimension` (`accuracy`,
`completeness`, `conformity`, `consistency`, `coverage`, `timeliness`, `uniqueness`), `severity`,
`businessImpact`, `unit` (`rows`, `percent`), `scheduler` + `schedule`.

In SQL, `{object}` and `{property}` stand for the current table and column (per the data-quality page).
Write the query in the dialect of the contract's server.

Whether one rule may carry several operators at once (e.g. a lower and an upper bound) changed in the
v3.2.0 schema during development; `mustBeBetween` is the portable way to express a range.

## Relationships

Foreign keys. On a property, give only `to` (the property itself is the `from`); at the object level give
both `from` and `to`. Both are strings for one column, or equal-length arrays for composite keys.

```yaml
schema:
  - name: orders
    relationships:                   # object level: from and to
      - from: [orders.tenant_id, orders.customer_id]
        to: [customers.tenant_id, customers.id]
    properties:
      - name: product_id
        logicalType: string
        relationships:               # property level: to only
          - to: products.id
```

`table.column` shorthand works for relationships only; elsewhere references use ids:
`schema/<object id>/properties/<property id>`.

## SLA

```yaml
slaProperties:
  - property: latency      # availability, throughput, errorRate, generalAvailability, endOfSupport,
    value: 4               # endOfLife, retention, frequency, latency, timeToDetect, timeToNotify, timeToRepair
    unit: d
    element: orders.updated_at
  - property: frequency
    value: 1
    unit: d
```

`property` and `value` are required per entry. Only include SLAs the producer actually commits to.

## Servers

Each server needs `server` (an identifier) and `type`, plus the type's own required fields (e.g.
`postgresql` needs `host`, `port`, `database`, `schema`; `snowflake` needs `account`, `database`,
`schema`; `s3` needs `location`; `iceberg` needs `catalog` and `catalogUrl`; `bigquery` needs `project` and `dataset`). Check the type's section in
`docs/infrastructure-servers.md`, and the validator will name any missing field. Use variables for
anything environment-specific:

```yaml
servers:
  - server: prod
    type: postgresql
    environment: prod
    host: ${DB_HOST}
    port: 5432
    database: ${DB_NAME:-orders}
    schema: public
```

Variables are allowed in string fields. `port` accepts an integer or a string, so `port: ${DB_PORT}` is
fine; integer-only fields such as `primaryKeyPosition` or `partitionKeyPosition` can't hold `${...}`.

## Team, support, roles

```yaml
team:
  name: Orders team
  members:
    - username: jdoe
      role: owner
support:
  - channel: '#orders-data'
    tool: slack
    url: https://example.slack.com/archives/C0000000
roles:
  - role: orders_reader
    access: read
    firstLevelApprovers: Orders team
```

`team` as a bare list is the deprecated pre-v3.1 form. Only fill these from what the user tells you.

## Custom properties

Anything the standard doesn't cover (thresholds, policies, tool settings) goes in `customProperties`:

```yaml
customProperties:
  - property: refreshPolicy
    value: Full reload nightly.
    vendor: acme            # optional (v3.2.0): lowercase id of the tool or vendor it belongs to
```

`customProperties` is allowed at the top level and on most elements (objects, properties, quality rules,
SLA entries, servers).

## Upgrading older contracts

- **v3.0/v3.1 → v3.2.0:** set `apiVersion: v3.2.0`; nothing breaks. Then consider `enum` instead of an
  `invalidValues` list, `team: {members: [...]}` instead of a list, and dropping `dataProduct` (link from
  the ODPS data product instead). Validate.
- **v2.x → v3:** a breaking redesign. Rebuild section by section against the docs rather than renaming keys
  blindly, and validate as you go. The main renames:
  - `uuid` → `id`; `dataset` → `schema`; `dataset[].table` → `schema[].name`; `dataGranularity` →
    `dataGranularityDescription`; `columns` → `properties`; `columns[].column` → `properties[].name`.
  - `isPrimaryKey` → `primaryKey`; `isUnique` → `unique`; `isNullable: false` → `required: true` (the sense
    flips); `partitionStatus` → `partitioned`; `clusterStatus`/`clusterKeyPosition` have no v3 field (keep
    them as `customProperties` if they matter); `sampleValues` → `examples`;
    `criticalDataElementStatus` → `criticalDataElement`; `encryptedColumnName` → `encryptedName`;
    `transformSourceTables` → `transformSourceObjects`.
  - `stakeholders` → `team`; `slaDefaultColumn` → `element` on each SLA entry; top-level connection fields
    (`server`, `database`, `driver`, `username`, `password`...) → a `servers` entry, with secrets as
    `${VARS}`.
  - Quality: v2 rules (`code`, `toolName`, `toolRuleName`, `templateName`) become `type: custom` with
    `engine` and `implementation`, or a library `metric` with an operator when the intent is a standard
    metric.
