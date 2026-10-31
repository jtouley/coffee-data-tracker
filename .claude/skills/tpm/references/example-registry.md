registry.coffee · reference

# Outside skills we've used

The Claude skills from other people's repositories that shaped the app, the public site and the README. Each was read before use, as third-party code. Skills that needed API keys, network installs or scripts were skipped.

## Where they were used

| Work | Skills |
| --- | --- |
| App design direction (`docs/app/DESIGN.md`, “Bench Scale”) | Six designers, one per repo, each following that repo's skills; the result became Bench Scale |
| README rewrite (#14) | readme-roast, good-readme (two rewrites merged by a judge); reviewed with readme-roast, good-readme, better-writing and no-ai-design-slop |
| Public site and app review (#35) | landing-page-design, build-awwwards-quality-sites, audit-ai-design-slop, interface-review, navigation-patterns, critique-brand-consistency, heuristic-evaluation |
| Reading animation sign-off (#38/#44) | Six independent reviews, one per repo, all passed |

## Design skills

**Emil Kowalski**[github.com/emilkowalski/skills](https://github.com/emilkowalski/skills)

- apple-designMotion and interaction: respond on press, interruptible springs, system type. DESIGN.md#35#38
- emil-design-eng · mobile-nativeRead alongside apple-design for the app direction. DESIGN.md
- animate-expoSkipped: needs npm/npx installs.

**ConardLi · garden-skills**[github.com/ConardLi/garden-skills](https://github.com/ConardLi/garden-skills)

- web-design-engineer“Group Head” direction: a Braun-style control panel, hairlines, one brown signal. Its Dieter Rams recipe fed Bench Scale. DESIGN.md#35#38
- gpt-image-2Skipped: needs an image API key.

**Elaya Design**[github.com/elayadesign/ai-design-skills](https://github.com/elayadesign/ai-design-skills)

- landing-page-designConversion and information-architecture rules for the beta call to action. Its absolutes (Geist only, gradient hero text, word-reveal) weren't applied; they conflict with Bench Scale and the CSP. DESIGN.md#35#38

**Meng To**[github.com/MengTo/Skills](https://github.com/MengTo/Skills)

- build-awwwards-quality-sitesCraft bar for the site. Its GSAP/Three.js stack wasn't used. DESIGN.md#35#38
- no-ai-design-slop · audit-ai-design-slopChecks for generic, AI-looking design; used on the README and the reading animation. #14#35#38
- elevenlabs-tts · codex-gpt-image-2-5-flare · write-like-meng-on-x · game and illustration skillsSkipped: API keys, a tweet corpus or Node scripts.

**Jakub Krehel**[github.com/jakubkrehel/skills](https://github.com/jakubkrehel/skills)

- interface-reviewUI critique of the site and app. #35#38
- better-interface · better-ui · better-colors · better-typographyThe app's visual details, e.g. chart text moved from 3.5:1 to 6.5:1 contrast. DESIGN.md
- better-writingConcise, specific labels in the app; one of the README reviewers. DESIGN.md#14

**Owl-Listener · designer-skills**[github.com/Owl-Listener/designer-skills](https://github.com/Owl-Listener/designer-skills)

- navigation-patterns · critique-brand-consistency · heuristic-evaluationThe site/app navigation and one-brand review. #35#38
- color-system · dark-mode-design · typography-scale · fitts-law · form-design · gesture-patterns · feedback-patterns · interfaces-that-feelUI and interaction principles behind the app's direction. DESIGN.md
- scripts/\*.py · build-gemini.shSkipped: scripts.

## README skills

**Ade Oshineye · good-readme**[github.com/adewale/good-readme](https://github.com/adewale/good-readme)

- good-readmeREADME structure and accuracy; one of the two rewrites and a reviewer in #14. #14

**readme-roast**[github.com/hidai25/readme-roast](https://github.com/hidai25/readme-roast)

- readme-roastA critical README audit, with readme-audit, hero, install, structure and star-killers sub-skills. The other rewrite and a reviewer in #14. #14

The README review also used better-writing (Jakub Krehel) and no-ai-design-slop (Meng To), listed above.

## Data skills

Cloned read-only. Their SKILL.md files were used as review criteria for the dbt hardening (#49) and the earlier dbt and Great Expectations review behind the quality gate.

**dbt Labs · dbt-agent-skills**[github.com/dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills)

- using-dbt-for-analytics-engineeringModeling, materialization and testing practice, with its writing-data-tests reference. #36#49
- adding-dbt-unit-testBehind the unit tests for the suspect-run and price-event macros. #49
- maintaining-dbt-documentation · running-dbt-commands · using-dbt-stateDocs, command and state practice in the review brief. #49

**wshobson · agents**[github.com/wshobson/agents](https://github.com/wshobson/agents)

- dbt-transformation-patterns · data-quality-frameworksFrom the data-engineering plugin; transformation layering and data-quality checks. #36#49

**DuckDB · duckdb-skills**[github.com/duckdb/duckdb-skills](https://github.com/duckdb/duckdb-skills)

- duckdb-skillsOfficial, but mostly about using the CLI, with little best-practice content. #49

**MotherDuck · agent-skills**[github.com/motherduckdb/agent-skills](https://github.com/motherduckdb/agent-skills)

- build-data-pipeline · model-data · load-dataDuckDB pipeline, modeling and loading practice. #49

The review also used two DuckDB documentation pages, which aren't skills: [file formats](https://duckdb.org/docs/stable/guides/performance/file_formats) and [Parquet tips](https://duckdb.org/docs/stable/data/parquet/tips).

## How well they worked

Each skill is rated 1–5 from what happened to its work in the PRs and session records. Output is the quality of what it produced, including real bugs found. Acceptance is how much was kept rather than rejected or reverted. Ideation is whether it changed decisions, not just checked them.

| Skill | Output | Acceptance | Ideation | Overall | Verdict |
| --- | --- | --- | --- | --- | --- |
| good-readme | 5 | 4 | 4 | **4.5** | The most useful. Its README audit found three real code bugs (grind direction, `make init --public`, the facts-only guardrail), all fixed. |
| Owl-Listener designer-skills | 5 | 4 | 5 | **4.5** | The best design reviewer. It caught a stale prefilled shot time that fed the advice, and its ratio bar, taste mark and 5 s undo shipped. |
| better-writing (Krehel) | 4 | 5 | 3 | **4** | A dependable copy editor: 11 of 12 README findings applied. Copy only. |
| Krehel UI set + interface-review | 4 | 4 | 4 | **4** | Found the unannounced notice and contrast fixes. The Bench Scale direction came partly from it. |
| no-ai-design-slop / build-awwwards (Meng To) | 4 | 3 | 4 | **4** | Strong for design (one authored moment, no tracked caps), middling on README prose. |
| apple-design (Kowalski) | 3 | 4 | 4 | **3.5** | Set the app's motion and press feel. One designer run didn't finish. |
| readme-roast | 3 | 4 | 3 | **3.5** | Good on structure and install steps. It pushed growth-hacking (star asks, invented timings) that was rejected. |
| web-design-engineer (ConardLi) | 4 | 3 | 3 | **3.5** | A sharp systems auditor (22 font sizes, off-grid spacing). Its own look was mostly rejected. |
| dbt Labs · adding-dbt-unit-test | 4 | 5 | 4 | **4.5** | The best data skill. It kept unit tests out of the weekly build, where they would have failed every release, and led to the testable macros. Merged. |
| MotherDuck agent-skills | 4 | 4 | 4 | **4** | Staging tables and NOT NULL constraints both merged, with identical output on the real lake. About 4% slower on the real build. |
| wshobson · dbt-transformation-patterns | 4 | 4 | 3 | **3.5** | One rule for a passing build, read through staging. Merged. |
| dbt Labs · using-dbt-for-analytics-engineering | 4 | 2 | 3 | **3** | It found real drift in the wholesale filter (fixed in #37, still open). Its sources finding wasn't built. |
| wshobson · data-quality-frameworks | 2 | 1 | 3 | **2** | Its median baseline clashed with approving real changes. Blocked in review and reverted. |
| dbt Labs · maintaining-dbt-documentation | – | 1 | 2 | **1.5** | Its one finding (synced column docs, provenance fields) wasn't built. |
| DuckDB · duckdb-skills | 1 | – | 1 | **1** | Nothing traceable to it; mostly about the command line. The ordered export came from DuckDB's docs instead. |
| landing-page-design (Elaya) | 3 | 2 | 3 | **3** | Useful on flows (the invite landing, the product name). Its visual absolutes clashed with the brief and were overruled most often. |

Evidence is thinner for the app-milestone design reviews (#15–#21), rated from PR text and DESIGN.md rather than per-reviewer reports. Even after four skill reviews, the code review of #14 found eight places where the README and code disagreed; the skills had caught two of them. The data skills are rated from #49, where each finding quoted its source. The earlier review behind #36's quality checks used the dbt Labs and wshobson repos but wasn't traced finding by finding, and dbt Labs' running-dbt-commands and using-dbt-state had no traceable effect.

## Install status

Nine of the vetted skills were installed into the design session's `~/.claude/skills` with your go-ahead: apple-design, web-design-engineer, landing-page-design, build-awwwards-quality-sites, audit-ai-design-slop, interface-review, navigation-patterns, critique-brand-consistency and heuristic-evaluation. They live in that session's container, not in the repo, so a new session has to install them again.