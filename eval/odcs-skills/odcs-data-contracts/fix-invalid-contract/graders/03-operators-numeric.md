---
type: llm
focus: trace
weight: 1
---

The settled_at SQL rule compares to the number 0 rather than the string 'zero', and the rowCount rule is valid while still requiring at least 1000 rows (for example mustBeGreaterOrEqualTo: 1000), not an invented upper bound presented as fact.
