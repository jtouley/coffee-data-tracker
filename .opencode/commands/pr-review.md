---
description: Review a pull request's diff (/pr-review 25). Needs gh signed in.
agent: pr-reviewer
---
Review pull request #$1.

!`gh pr diff $1 | python3 tools/pr_review/pr_review.py prep`
