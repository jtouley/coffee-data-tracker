# Contributing

This is a personal project with one owner. Issues are welcome; pull requests are accepted once an issue has agreed
the change.

- **Bugs, questions and ideas:** open an issue on the
  [issue tracker](https://github.com/jtouley/coffee-data-tracker/issues).
- **Vulnerabilities:** don't open an issue. Follow [SECURITY.md](SECURITY.md).
- **Pull requests:** open an issue first and agree the change there. Then branch from `main`, open the pull request
  against `main` and link the issue. CI (`.github/workflows/ci.yml`) must pass.

## Setting up

```bash
make install   # uv sync
make hooks     # once: install the pre-commit hooks
make test      # make lint, then pytest
make fmt       # ruff fix and format over src, app and tests
```

`make lint` runs the pre-commit hooks on every file: whitespace, YAML and TOML, large-file and private-key checks,
`uv.lock` in step with `pyproject.toml`, ruff, ruff format, and mypy over `src` and `app`. The README's
[Development](README.md#development) section covers the browser test and what CI runs.

## Requirements for a change

- `make test` passes.
- Code follows the ruff lint and format settings and the mypy settings in `pyproject.toml`.
- New features and bug fixes come with tests in `tests/`, or in `dbt/tests/` for dbt data rules. A bug fix
  includes a test that reproduces the bug.
- Changes to heuristics or the classifier (classifying products, extracting attributes, matching bags to coffees,
  choosing a Claude model or prompt) follow the evaluation rules in [CLAUDE.md](CLAUDE.md). They're measured on
  held-out data, and the accuracy floors in `tests/test_classifier_accuracy.py` and
  `tests/test_wholesale_accuracy.py` are only ever raised.
- Data stays in the lake, never in git.

## License

The project is under the [MIT License](LICENSE). By contributing, you agree that your contribution is licensed
under the same license.
