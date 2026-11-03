# Development

How to set up a checkout, run the checks, and what enforces them.

## Setup

```bash
uv sync                      # venv with the dev group (pytest, ruff, pre-commit)
uv run pre-commit install    # once per clone: ruff check + format on every commit
git config blame.ignoreRevsFile .git-blame-ignore-revs   # once per clone
```

`just install-dev` puts an editable `bd-track` on `PATH` via pipx.

## Checks

| Command | What it runs |
|---|---|
| `just test [args]` | `uv run pytest -q [args]` |
| `just lint` | `ruff check` and `ruff format --check` over `src` and `tests` |
| `just fmt` | `ruff format`, then `ruff check --fix` |

Everything calls ruff through `uv run`, so the version is the one pinned in `uv.lock`. A
system-wide `ruff` can be a different version and format differently; don't use it on this
repo.

## Enforcement

- **pre-commit** (`.pre-commit-config.yaml`): `ruff check` and `ruff format` on staged Python
  files. Local hooks running `uv run ruff`, not the `ruff-pre-commit` repo, so the hook
  version follows `uv.lock` with no second pin to bump.
- **CI** (`.github/workflows/ci.yml`): on pushes to `main` and on pull requests, a lint job
  (`ruff check`, `ruff format --check`) and pytest on Python 3.10, 3.11 and 3.12. The real
  Dolt push tests in `tests/test_doltpush.py` need `bd` and `dolt`, and skip on the runner.

## Decision log

| Date | Decision |
|---|---|
| 2026-11-03 | Adopt `ruff format` as the project style. Before this, only `ruff check` ran and the code was hand-styled. One formatting-only commit, listed in `.git-blame-ignore-revs` (`bd-timew-as5`, MH). |
| 2026-11-03 | Enforce lint and format with a pre-commit hook and GitHub Actions CI, both running the ruff pinned in `uv.lock` (MH). |
