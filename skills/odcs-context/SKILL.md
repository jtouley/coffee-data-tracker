---
name: odcs-context
description: >-
  Build the AI and semantic layer of an ODCS data contract (v3.2.0 `context` block with
  instructions, constraints and verifiedStatements, plus `synonyms` and `semanticType`
  measures/dimensions) by interviewing the people who know the data, then writing it into the
  contract. Use it whenever someone wants a data contract to steer a text-to-SQL agent, an LLM, a
  chatbot, a BI or semantic-layer tool; asks how agents should query a dataset or what they must not
  do with it; wants verified questions and answers, business synonyms, or measures and dimensions in
  a contract; or says their AI tool keeps querying a table wrong. Use it even if they don't say
  "context block".
license: Apache-2.0
metadata:
  version: "0.1.0"
  odcs-version: "v3.2.0"
---

<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# ODCS context, built by interview

An agent reading a contract already has the schema. What it lacks is what lives in people's heads: which
rows count as "current", which join fans out, what a column must never be summed over, which questions come
up every week and what the right answer is. You can't infer that from the YAML, and a guess written as
`context` is worse than nothing, because agents are told to trust it (a verified answer is meant to be
returned verbatim). So this skill is a short interview: read what the contract already says, ask only what
it doesn't, confirm a draft with the user, then write and validate.

The field reference (levels, cascading rules, examples) is in
[references/context-block.md](references/context-block.md); the question bank is in
[references/questions.md](references/questions.md).

## Workflow

### 1. Read before asking

Read the whole contract (and anything the user points at: dbt docs, a semantic model, sample queries,
a README). Write yourself a short list of what is already known per schema object: grain
(`dataGranularityDescription`, keys), status/enum values, time columns, join keys (`relationships`, or
`*_id` columns described as references), limitations, SLAs. Every question you skip because the contract
answers it is time saved for the user; confirm those facts in the draft instead of asking.

Also find out the target: which tool or agent will read this (text-to-SQL, a chat assistant, a BI semantic
layer), because that decides how much to say about SQL versus business meaning.

### 2. Interview in rounds

Ask in small rounds (two to four questions at a time), each with a proposed answer drawn from the
contract so the user can confirm or correct rather than write from scratch. If your environment offers a
structured way to ask the user questions, use it; otherwise ask in plain text. Go in this order, because each round shapes the next:

1. **Purpose and audience:** who asks questions of this data, through what tool, and what they mostly want.
2. **Per table, the traps:** what "current" or "active" means, the time column to filter on, the joins and
   any that duplicate rows, units and currency, how to read nulls.
3. **Prohibitions:** what an agent must not do: personal data, minimum aggregation, columns not to sum or
   average, uses the producer doesn't support, policies to link (they become `authoritativeDefinitions`).
4. **Real questions:** the five to ten questions people actually ask. For each, ask whether there is a
   known-good answer or query.
5. **Vocabulary (optional):** other names people use for tables and columns (`synonyms`), and which columns
   are measures or dimensions (`semanticType`), with each measure's aggregation.

Pick questions from [references/questions.md](references/questions.md) to fit the data; skip any the
contract already answers or that don't apply. Stop a round early if the user says enough, and don't run
more than about five rounds: a smaller context the user stands behind beats a long one they skimmed.

### 3. Verified answers: only what's verified

`verifiedStatements[].answer` is the one field agents are told to repeat. Add an answer only when:

- the user gave it or confirmed it, or
- you ran the query against the data (the user gave access) and the user agreed with the result.

Otherwise record the question alone (it still primes text-to-SQL) and list it in your report as "needs an
answer". If the user wants guidance on how to answer without a verified result, write it as a query recipe
in `instructions` for that object, not as an `answer`.

### 4. Draft, confirm, write

Show the user the draft context in plain language (per level: instructions, constraints, questions,
synonyms) and say where each statement came from: "contract" or "you said". Apply their corrections, then
write it into the contract:

- `context` goes at the contract level and on schema objects only, never on properties (not in v3.2.0).
  Contract level is for what holds everywhere; object level adds what is specific to that table. Don't
  repeat the contract level in each object; constraints at the top bind every object anyway.
- `synonyms` go on objects and properties; `semanticType` on properties.
- Change nothing else in the contract. If the interview surfaces a problem elsewhere (a wrong description,
  a missing relationship, an SLA), list it as a suggestion instead of editing it, and bump the contract
  `version` only if the user asks.

Then validate. If the `odcs-data-contracts` skill is installed alongside this one, use its validator
(`scripts/validate_contract.py` in that skill's folder):

```bash
python ../odcs-data-contracts/scripts/validate_contract.py path/to/contract.odcs.yaml
```

Otherwise validate against the ODCS JSON schema for the contract's `apiVersion` with any draft 2019-09
validator.

### 5. Report

Summarise what was added per level, the questions still without answers, the suggestions outside
`context`, and the validator's result line.

## When nobody can answer

If you're running without a user to ask (a batch job, an automated agent, CI), don't invent the interview. Write
only what the contract itself supports (grain, the meaning of enum values it documents, its stated
limitations as constraints, questions without answers), and put the questions you would have asked in your
report so a person can finish the job.
