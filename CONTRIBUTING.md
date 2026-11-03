# Contributing

This is a personal project with one owner. Issues are welcome; pull requests are accepted once the change is
agreed in an issue.

- **Bugs, questions and ideas:** open an issue on the
  [issue tracker](https://github.com/jtouley/coffee-data-tracker/issues), using a template (below).
- **Vulnerabilities:** don't describe them in an issue. Follow [SECURITY.md](SECURITY.md).
- **Pull requests:** open an issue first to agree the change. Then fork the repository, branch from `main`, open the pull request against
  `main` and link the issue. All checks must pass.

## Issues

Use the forms in `.github/ISSUE_TEMPLATE/` where one fits:

- **Bug:** what happened, what you expected and how to reproduce it.
- **Enhancement:** every product enhancement states its **target user**, the **problem**, a **measurable outcome**
  (a metric and a target) and **evidence of demand** (links, requests or data). A proposed approach is optional.
  An enhancement without a user, an outcome and evidence isn't prioritized until it has them.
- **Log a shot** and **Add a roaster** are the owner's own forms; the `issue-forms` workflow processes them
  automatically. Use the Bug or Enhancement form, or a blank issue, for anything else.

Blank issues stay enabled for questions and for the "Security contact request" in [SECURITY.md](SECURITY.md).

### Labels

The owner labels issues and pull requests at triage. An item can carry several.

| Label | Means |
|---|---|
| `customer-request` | Asked for by a user or customer. The issue links the request. |
| `data-quality` | Correctness of the published data: classification, prices, coverage, dbt tests. |
| `reliability` | Pipeline, crawler and app uptime: alerts, retries, recovery, CI health. |
| `security` | Hardening, secrets and access. Vulnerabilities themselves are reported privately. |
| `growth` | More coverage, users or reach: new regions, listings, integrations. |
| `good first issue` | Small and well scoped, with no cloud or lake access needed. A good place to start. |
| `bug` | Something is wrong. |
| `enhancement` | A new feature or a change to an existing one. |
| `documentation` | Docs only. |
| `dependencies`, `github_actions`, `python:uv` | Dependency updates from Dependabot. |
| `help wanted` | The owner would welcome a pull request. |
| `question`, `duplicate`, `invalid`, `wontfix` | Triage outcomes. |
| `shot`, `roaster` | Reserved for the owner's issue forms, which the `issue-forms` workflow acts on. Don't use them elsewhere. |

## Setting up

```bash
make install   # uv sync
make hooks     # once: install the pre-commit hooks
make test      # make lint, then pytest
make fmt       # ruff fix and format over src, app and tests
```

`make lint` runs the pre-commit hooks in `.pre-commit-config.yaml`. The README's
[Development](README.md#development) section covers what they check, the browser test and what CI runs.

## Requirements for a change

- `make test` passes.
- Code follows the ruff lint and format settings and the mypy settings in `pyproject.toml`.
- New features and bug fixes come with tests in `tests/`, or for dbt, a unit test in `dbt/models/unit_tests.yml`
  (model logic) or a data test in `dbt/tests/` (data rules). A bug fix includes a test that reproduces the bug.
- Changes to heuristics or the classifier (classifying products, extracting attributes, matching bags to coffees,
  choosing a Claude model or prompt) follow the evaluation rules in [CLAUDE.md](CLAUDE.md), which scale the
  effort to the size of the change (step 9 covers small changes).
  The accuracy floors in `tests/test_classifier_accuracy.py` and `tests/test_wholesale_accuracy.py` are only ever
  raised.
- Don't commit data downloaded from the lake. Test fixtures and eval sets are the exception, in `tests/fixtures/`
  and `eval/`.

## License

The code is under the [MIT License](LICENSE). By contributing, you agree that your contribution is licensed
under the same license. The public dataset the project publishes is under CDLA-Permissive-2.0
([`licenses/CDLA-Permissive-2.0.txt`](licenses/CDLA-Permissive-2.0.txt)).
