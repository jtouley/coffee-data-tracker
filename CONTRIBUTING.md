# Contributing

This is a personal project with one owner. Issues are welcome; pull requests are accepted once the change is
agreed in an issue.

- **Bugs, questions and ideas:** open an issue on the
  [issue tracker](https://github.com/jtouley/coffee-data-tracker/issues).
- **Vulnerabilities:** don't describe them in an issue. Follow [SECURITY.md](SECURITY.md).
- **Pull requests:** open an issue first to agree the change. Then branch from `main`, open the pull request against
  `main` and link the issue. All checks must pass.

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

The project is under the [MIT License](LICENSE). By contributing, you agree that your contribution is licensed
under the same license.
