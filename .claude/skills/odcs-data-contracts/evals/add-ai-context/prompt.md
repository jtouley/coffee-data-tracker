---
tags: [odcs]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

We're pointing a text-to-SQL agent at the public coffee tables. Add whatever ODCS 3.2.0 lets us put in public-catalog.odcs.yaml to steer an AI agent: how to query these tables correctly and what it must not do. Only use what the contract already tells you; don't make up numbers. Save it in place and keep it valid.

Input files: public-catalog.odcs.yaml
