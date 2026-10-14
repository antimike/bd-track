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
    pipx install --force .

# Install editable: code changes in this checkout take effect immediately.
install-dev: check-deps
    pipx install --force --editable .

# Remove the pipx install.
uninstall:
    pipx uninstall bd-track
