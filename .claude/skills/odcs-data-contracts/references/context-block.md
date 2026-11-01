<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# The `context` block (ODCS v3.2.0)

`context` carries guidance for AI agents, LLMs, BI tools and semantic layers: how to use the data, which
questions it answers, and what not to do with it. Normative sources: `docs/context.md` and
[RFC-0038](https://github.com/bitol-io/tsc/blob/main/rfcs/approved/odcs-v3.2.0/0038-context.md).

It's allowed in two places only: the **contract** (top level) and each **schema object**. There is no
property-level `context` (the TSC deferred it); guidance about one column goes in the object's `context`
or in the property's `description`. A plain string is allowed and means `instructions` only.

```yaml
context:                                   # contract level: domain, purpose, limits for the whole dataset
  instructions: >
    Weekly snapshots of US specialty coffee roasters' public catalogs. Use for market and assortment
    analysis. Prices are list prices in USD as shown on the roaster's store.
  constraints:
    - id: no-scraping-advice
      constraint: Do not present a row as a roaster's current stock; it is as of the last weekly run.
  verifiedStatements:
    - question: How many roasters are covered?
schema:
  - name: public_coffee
    context:                               # object level: joins, grain, filters
      instructions: >
        One row per coffee ever seen. Filter catalog_status = 'available' for what is on sale now.
        Join to public_roaster on roaster_id.
      constraints:
        - constraint: Do not count delisted or archived coffees as current offerings.
      verifiedStatements:
        - question: How many coffees are on sale now?
          answer: SELECT COUNT(*) FROM public_coffee WHERE catalog_status IN ('available', 'preorder')
```

## The three fields

| Field | What it is | Write it as |
| --- | --- | --- |
| `instructions` | Positive guidance, read like a system prompt scoped to this level | A few plain sentences: what the data is, its grain, how to filter and join it, units and time zones |
| `constraints[]` | Negative guidance; agents must not do these | One prohibition per entry in `constraint`; optional `id`, `tags`, `authoritativeDefinitions` (link the policy) |
| `verifiedStatements[]` | Canonical business questions | `question` (required). Add `answer` only when it's verified: agents SHOULD return it verbatim when a query is close |

## How levels combine (normative, RFC-0038)

- `instructions` add up from general to specific (product → contract → object). Don't repeat the contract's
  text in each object; say what is specific to that object.
- `verifiedStatements` merge by `question`; the more specific level wins for the same question.
- `constraints` merge across all levels and are never overridden: the most restrictive set applies. So a
  constraint at the contract level binds every object.

## Writing good context

Context is read by a model that has the schema too, so spend words on what the schema can't say:

- **Grain and time:** what one row is; which timestamp to filter by; whether data is a snapshot, an event
  log or slowly-changing; the refresh lag.
- **Joins:** the keys, and joins that look plausible but are wrong (fan-out, many-to-many).
- **Semantics the names hide:** sign conventions, units and currency, which status values mean "current",
  how nulls should be read.
- **Prohibitions** in `constraints`, each concrete and checkable: PII exposure, minimum aggregation
  levels, columns not to sum, uses the producer doesn't support. Mirror `description.limitations`.
- **Questions** people actually ask. Without an answer they prime text-to-SQL; with an answer they pin a
  known-good result, so only add answers you've checked (or that the user supplied), and keep them in
  sync when the schema changes. A stale verified answer is worse than none.

Don't put secrets, credentials or row-level data in `context`, and don't restate column descriptions.

## Related v3.2.0 fields

`context` is one of three semantic additions; use them together:

- `synonyms` on objects and properties: alternative names (`revenue`, `sales`, `TO`) so natural-language
  tools map words to the right column. Each entry needs `synonym`.
- `semanticType: measure | dimension` on properties, with a measure's aggregation in `transformLogic`.
- `authoritativeDefinitions` with `type: glossary | ontology | taxonomy` to link business vocabularies.

A note on `${...}` inside answers: RFC-0038's own example writes `${total_turnover_euros}` in an answer,
and RFC-0050 uses the same syntax for variables that tooling resolves at runtime. Until that's clarified
upstream, refer to columns by plain name (or by a fully qualified id) in answers to avoid a tool
substituting them.
