---
tags: [odcs]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Our CI started rejecting payments.odcs.yaml after we bumped to ODCS 3.2.0 and the ajv output is a wall of 'unevaluated properties' noise. Can you fix the contract so it validates, without changing what it means, and tell me what was wrong? Save the fixed file over payments.odcs.yaml.

Input files: payments.odcs.yaml
