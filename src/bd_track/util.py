"""Foundation helpers used across modules.

Includes the subprocess wrapper, beads workspace discovery, the Rich-styled
argparse formatter, logging setup, interactive prompt helpers, activity
tracking for idle-stop, and shared path constants.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.prompt import Confirm, Prompt
from rich_argparse import RawDescriptionRichHelpFormatter

# ---------------------------------------------------------------------------
# bd-timew → bd-track back-compat shims
#
# The rename keeps old-name on-disk artifacts and env vars readable so an
# upgraded bd-track keeps working on a project still laid out for bd-timew
# (e.g. a live sidecar / .envrc). `bd-track migrate rename` performs the
# one-way cleanup; these shims cover the interim. Prefer the new name; fall
# back to the legacy one only when the new one is absent.
# ---------------------------------------------------------------------------

def env_compat(name: str, default: str | None = None) -> str | None:
    """Read ``BD_TRACK_*`` env var, falling back to the legacy ``BD_TIMEW_*``."""
    val = os.environ.get(name)
    if val is not None:
        return val
    if name.startswith("BD_TRACK_"):
        val = os.environ.get("BD_TIMEW_" + name[len("BD_TRACK_"):])
        if val is not None:
            return val
    return default


def path_compat(new: Path, old: Path) -> Path:
    """Prefer ``new``; fall back to ``old`` only if it exists and ``new`` does not."""
    return old if (old.exists() and not new.exists()) else new


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_DIR = path_compat(Path.home() / ".config" / "bd-track",
                          Path.home() / ".config" / "bd-timew")
_CACHE_DIR = path_compat(Path.home() / ".cache" / "bd-track",
                         Path.home() / ".cache" / "bd-timew")

REPOS_CONFIG = _CONFIG_DIR / "repos.yaml"
CLEANUP_STATE = _CACHE_DIR / "cleanup-state.json"
CLEANUP_LOG = _CACHE_DIR / "cleanup.log"
ACTIVITY_STATE = _CACHE_DIR / "activity-state.json"
QUEUE_FILE = "queue.yaml"

SYSTEMD_CLEANUP_NAME = "bd-track-cleanup"
SYSTEMD_IDLE_STOP_NAME = "bd-track-idle-stop"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

root_log = logging.getLogger("bd-track")
cleanup_log = logging.getLogger("bd-track.cleanup")


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    RawDescriptionRichHelpFormatter,
):
    """Preserve description/epilog whitespace, show defaults, and style with Rich."""


def setup_logger(loglevel: str, *, enable_file_handler: bool = False) -> None:
    """Configure root and cleanup loggers; enable rotating file handler if requested."""
    level = getattr(logging, loglevel.upper(), logging.INFO)
    root_log.setLevel(level)
    root_log.addHandler(
        RichHandler(
            level=level,
            console=Console(stderr=True),
            show_path=False,
            rich_tracebacks=True,
        )
    )
    if enable_file_handler:
        CLEANUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            CLEANUP_LOG, maxBytes=1_000_000, backupCount=3
        )
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        cleanup_log.addHandler(fh)


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------

def run(
    cmd: list[str], *, check: bool = True, capture: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """Thin wrapper over ``subprocess.run`` with text mode and consistent kwargs."""
    return subprocess.run(
        cmd, check=check, text=True, capture_output=capture, cwd=cwd,
    )


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------

def find_beads_dir(project_dir: Path | None = None) -> Path:
    """Return the active workspace's .beads/ directory.

    If `project_dir` is given, validates `.beads/` exists under it.
    Otherwise calls `bd where` to discover the active workspace.
    Exits with a message if no workspace is found.
    """
    if project_dir is not None:
        candidate = project_dir / ".beads"
        if not candidate.is_dir():
            sys.exit(f"bd-track: no .beads/ directory found under {project_dir}")
        return candidate
    result = run(["bd", "where"], check=False, capture=True)
    if result.returncode != 0:
        sys.exit(
            "bd-track: no active Beads workspace. cd into a project with "
            ".beads/, or pass --project-dir."
        )
    return Path(result.stdout.strip().split("\n", 1)[0].strip())


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def prompt(message: str, default: str, *, yes: bool) -> str:
    if yes or not is_interactive():
        return default
    return Prompt.ask(message, default=default)


def confirm(message: str, default: bool, *, yes: bool) -> bool:
    if yes or not is_interactive():
        return default
    return Confirm.ask(message, default=default)


# ---------------------------------------------------------------------------
# Activity tracking (for idle-stop)
# ---------------------------------------------------------------------------

def load_activity_state() -> dict:
    if not ACTIVITY_STATE.exists():
        return {}
    with ACTIVITY_STATE.open() as f:
        return json.load(f)


def save_activity_state(state: dict) -> None:
    ACTIVITY_STATE.parent.mkdir(parents=True, exist_ok=True)
    with ACTIVITY_STATE.open("w") as f:
        json.dump(state, f, indent=2, default=str)


def record_activity(project_path: str) -> None:
    """Record the current time as the last-activity timestamp for a project."""
    state = load_activity_state()
    state[project_path] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_activity_state(state)


# ---------------------------------------------------------------------------
# Project registry lookups (--project / --global)
# ---------------------------------------------------------------------------

def resolve_project_dir(slug_or_path: str) -> Path:
    """Resolve ``--project <name-or-path>`` to a project root via the registry.

    Matches a registry name, a path basename (legacy repos.yaml names), or the path itself.
    A registered name whose clone is not known yet forces a fresh scan of the scan roots.
    """
    from bd_track.registry import load_registry_or_exit

    candidate = Path(slug_or_path).expanduser()
    registry = load_registry_or_exit()
    for attempt in (1, 2):
        known = [p for p in registry.projects.values() if p.path is not None]
        for project in known:
            if slug_or_path in (project.name, project.path.name) or project.path == candidate:
                return project.path
        pending = registry.get(slug_or_path)
        if attempt == 2 or pending is None or pending.remote is None:
            break
        registry = load_registry_or_exit(scan=True)
    names = ", ".join(p.name for p in known)
    sys.exit(
        f"bd-track: --project {slug_or_path!r} not found in the project registry"
        + (f" ({registry.source})" if registry.source else "")
        + (f"; known: {names}" if names else "")
    )


def all_project_dirs() -> list[Path]:
    """Return the roots of every registered beads project with a known path (``--global``)."""
    from bd_track.registry import load_registry_or_exit

    projects = load_registry_or_exit().beads_projects()
    if not projects:
        sys.exit("bd-track: --global requires at least one beads project with a known path "
                 "in the project registry")
    return [p.path for p in projects]


# ---------------------------------------------------------------------------
# Datetime parsing
# ---------------------------------------------------------------------------

def parse_datetime(value: str) -> dt.datetime:
    """Parse a human-readable or ISO-8601 datetime string.

    Tries dateparser first (natural language, relative expressions), then falls
    back to ``datetime.fromisoformat()`` for strict ISO-8601. Always returns a
    timezone-aware datetime. Raises ``ValueError`` if neither succeeds.
    """
    try:
        import dateparser as _dp
        result = _dp.parse(
            value,
            settings={"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": True},
        )
        if result is not None:
            return result
    except ImportError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    except ValueError:
        pass
    raise ValueError(f"cannot parse datetime: {value!r}")
