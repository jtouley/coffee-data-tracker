<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Question bank

Pick, don't recite. Each question names the field its answer feeds. Before asking, fill in a proposed answer
from the contract ("The contract says one row per order; right?") so the user only corrects.

## Round 1: purpose and audience → contract `context.instructions`

- Who asks questions of this data, and through what (a text-to-SQL agent, a chat assistant, a BI tool, a
  notebook)?
- What do they mostly want from it: trends, lookups, counts, monitoring?
- Is there anything people commonly assume about this data that isn't true? (Coverage, freshness, a
  population it doesn't include.)
- Which SQL dialect or engine will run the queries? (Only if the contract has no `servers` entry.)

## Round 2: per table → object `context.instructions`

- What is one row? (Confirm `dataGranularityDescription` or the keys.)
- What does "current" / "active" / "live" mean here: a status value, a flag, the latest snapshot date?
- Which time column should a question like "last month" filter on, and in which time zone?
- How should it be joined, and is there a join that looks right but duplicates rows?
- Units, currency, sign conventions, percentages stored as 0–1 or 0–100?
- What does a null mean in the columns people use most?
- Is this a snapshot, an event log, or slowly changing? Do old rows ever change?

## Round 3: prohibitions → `context.constraints`

- Is there personal or sensitive data an agent must never return, even if asked?
- Is there a minimum aggregation level (e.g. no individual orders, at least country level)?
- Columns that must not be summed or averaged (pre-aggregated values, percentiles, ratios, snapshot counts)?
- Uses the producer doesn't support (forecasting, financial reporting, ranking individuals)?
- A policy, regulation or glossary entry that backs a rule? (Becomes the constraint's
  `authoritativeDefinitions`, with `url` and `type`, e.g. `policy`, `glossary`.)

## Round 4: real questions → `context.verifiedStatements`

- What are the five to ten questions people ask most?
- For each: is there a known-good answer or a reviewed query? Can you share it, or may I run it?
- Which of these should apply to the whole contract, and which to one table?

## Round 5: vocabulary (optional) → `synonyms`, `semanticType`, `authoritativeDefinitions`

- What other names do people use for this table or its main columns (acronyms, old names, other
  languages)?
- Which columns are measures (aggregated, e.g. revenue) and which are dimensions (grouping, e.g. region)?
  For each measure, what is the aggregation (`SUM(amount)`)? (It goes in `transformLogic`.)
- Is there a business glossary, ontology or taxonomy to link (`authoritativeDefinitions`,
  `type: glossary | ontology | taxonomy`)?

## Turning answers into fields

| Answer looks like | Write it as |
| --- | --- |
| "Always / to do X, use Y" | an `instructions` sentence at the narrowest level it applies to |
| "Never / don't / must not" | one `constraints` entry each, concrete enough to check |
| "People ask Q" | `verifiedStatements: [{question: Q}]` |
| "...and the answer is A" (given or confirmed) | add `answer: A` |
| "We also call it N" | `synonyms: [{synonym: N}]` on that object or property |
| "It's a total / an average" | `semanticType: measure` with the aggregation in `transformLogic` |
| "Per the policy at URL" | `authoritativeDefinitions: [{url: URL, type: policy}]` on the constraint |
