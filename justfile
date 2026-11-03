# bd-track task runner. `just --list` shows the recipes.

# List recipes.
default:
    @just --list

# Verify runtime dependencies (bd only; timew is no longer required).
check-deps:
    @command -v bd >/dev/null 2>&1 || { echo "bd (beads) not found on PATH: https://github.com/steveyegge/beads" >&2; exit 1; }
    @echo "bd: $(bd --version | head -1)"
    @command -v pipx >/dev/null 2>&1 || { echo "pipx not found on PATH" >&2; exit 1; }
    @echo "pipx: $(pipx --version)"

# Install bd-track from this checkout into a pipx venv (a snapshot).
install: check-deps
    -pipx uninstall bd-track
    pipx install .

# Install editable: code changes in this checkout take effect immediately.
install-dev: check-deps
    -pipx uninstall bd-track
    pipx install --editable .

# Run the test suite (extra args pass through to pytest).
test *args:
    uv run pytest -q {{args}}

# Lint and check formatting (what pre-commit and CI enforce).
lint:
    uv run ruff check src tests
    uv run ruff format --check src tests

# Apply ruff formatting and safe lint fixes.
fmt:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# Remove the pipx install.
uninstall:
    pipx uninstall bd-track
