# Routing work to skills

Read this when a workstream would benefit from skills: the ones installed in the session, or outside skill
repositories the user points you at. `example-registry.md` is a real record from a past project, kept as the
model for how a catalog of outside skills should look; copy its shape, not its contents.

## Patterns

**One agent per skill set.** Give each agent one repository or coherent set of skills and name the skills it
should follow. An agent juggling five unrelated repos blends them into something generic; one per set keeps each
point of view distinct and makes it traceable which skill produced which finding.

**Makers plus a judge, for creative work** (a design direction, a README rewrite, naming). Run N makers
independently, each from a different skill set, none seeing the others. Then hand all N artifacts to a separate
judge agent that picks or merges, and says what it took from each and why. Independence is the point: makers who
see each other converge.

**N reviewers, all must pass, for sign-off.** One reviewer per skill set, each given the artifact (the diff, the
page, the file) and its skills, not your summary of the work. The change ships only when every reviewer passes;
a fail goes back to a maker with the finding quoted. Even then, skill reviews miss things (in the example a later
code review found eight README/code mismatches that four skill reviews had caught two of), so keep a correctness
review such as `code-review` in the loop for anything that touches code.

**Outside skills are third-party code.** Before any use, an agent reads the skill's files and reports what they
do. Skip skills that need API keys, network installs (npm/npx/pip), or that ship scripts to run; use only the
SKILL.md text as guidance or review criteria. A skill's absolutes yield to the user's brief and the repo's rules.
Installing a skill into the session needs the user's go-ahead.

**Record what was used and skipped.** Keep a catalog (where the project wants one) so the next orchestration
doesn't re-vet from scratch, and so the skills that actually helped are visible.

## Catalog shape

```markdown
# Outside skills we've used

## Where they were used
| Work | Skills |
| --- | --- |
| README rewrite (#14) | skill-a, skill-b (two rewrites merged by a judge); reviewed with skill-a, skill-c |

## <Domain> skills
**<Author · repo>** [github.com/<owner>/<repo>](https://github.com/<owner>/<repo>)
- skill-a: what it was used for, what was deliberately not applied. <links to PRs/docs>
- skill-x: Skipped: needs an API key.

## How well they worked
| Skill | Output | Acceptance | Ideation | Overall | Verdict |
| --- | --- | --- | --- | --- | --- |
| skill-a | 5 | 4 | 4 | 4.5 | One line on what its work led to, with evidence. |

## Install status
Which skills were installed where, with whose approval, and whether a new session has to reinstall them.
```

Rate from what happened to the work (kept, rejected, reverted, bugs found), not from how the output read, and
say where the evidence is thin.
