---
tags: [odcs]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

we're starting to publish data contracts for the sales tables so the analytics team knows what they can rely on. here's the DDL for orders and customers from our postgres warehouse (orders.sql). can you turn it into an ODCS data contract, saved as sales.odcs.yaml? we haven't agreed SLAs or owners yet.

Input files: orders.sql
