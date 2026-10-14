#!/usr/bin/env sh
# bd-track installer — fetches system deps and installs the tool via pipx.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/AudensAurantius/bd-track/main/install.sh | sh
#
# Or for a specific version:
#   curl -fsSL https://raw.githubusercontent.com/AudensAurantius/bd-track/main/install.sh | sh -s -- v0.5.0
#
# Environment:
#   BD_TRACK_REF    Git ref to install (default: main)
#   BD_TRACK_REPO   Repo URL (default: https://github.com/AudensAurantius/bd-track.git)

set -eu

REF="${1:-${BD_TRACK_REF:-main}}"
REPO="${BD_TRACK_REPO:-https://github.com/AudensAurantius/bd-track.git}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log()  { printf '\033[1;34m[bd-track install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bd-track install]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[bd-track install]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Detect package manager
# ---------------------------------------------------------------------------
detect_pkg_mgr() {
    if command -v brew >/dev/null 2>&1; then
        echo brew
    elif command -v apt-get >/dev/null 2>&1; then
        echo apt
    elif command -v dnf >/dev/null 2>&1; then
        echo dnf
    elif command -v pacman >/dev/null 2>&1; then
        echo pacman
    else
        echo unknown
    fi
}

PKG_MGR=$(detect_pkg_mgr)
log "Detected package manager: $PKG_MGR"

# ---------------------------------------------------------------------------
# Install a system package via the detected manager
# ---------------------------------------------------------------------------
install_pkg() {
    pkg="$1"
    case "$PKG_MGR" in
        brew)   brew install "$pkg" ;;
        apt)    sudo apt-get install -y "$pkg" ;;
        dnf)    sudo dnf install -y "$pkg" ;;
        pacman) sudo pacman -S --noconfirm "$pkg" ;;
        *)      fail "No supported package manager. Install '$pkg' manually and re-run." ;;
    esac
}

# Timewarrior is no longer a dependency — bd-track uses an append-only JSONL
# event log, not the timew backend. (Removed in the 0.5.0 rewrite.)

# ---------------------------------------------------------------------------
# Ensure pipx is installed
# ---------------------------------------------------------------------------
if command -v pipx >/dev/null 2>&1; then
    log "pipx already installed"
else
    log "Installing pipx..."
    case "$PKG_MGR" in
        brew)   install_pkg pipx ;;
        apt)    install_pkg pipx ;;
        dnf)    install_pkg pipx ;;
        pacman) install_pkg python-pipx ;;
        *)      fail "Cannot auto-install pipx. See https://pipx.pypa.io/stable/installation/" ;;
    esac
    pipx ensurepath
fi

# ---------------------------------------------------------------------------
# Check for bd (beads) — required at runtime, not auto-installed
# ---------------------------------------------------------------------------
if command -v bd >/dev/null 2>&1; then
    log "bd (beads) already installed: $(bd --version 2>/dev/null | head -1)"
else
    warn "bd (beads) not found on PATH."
    warn "Install a release from https://github.com/steveyegge/beads/releases."
    warn "Continuing — bd-track will be installed but won't function until bd is on PATH."
fi

# ---------------------------------------------------------------------------
# Install bd-track via pipx
# ---------------------------------------------------------------------------
log "Installing bd-track from $REPO@$REF..."
pipx install --force "git+$REPO@$REF"

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
if command -v bd-track >/dev/null 2>&1; then
    log "Installed: $(bd-track --help 2>&1 | head -1 || echo 'bd-track')"
    log "Run 'bd-track config init' inside a project with .beads/ to scaffold a sidecar."
else
    warn "bd-track installed but not on PATH. You may need to restart your shell or run 'pipx ensurepath'."
fi
