"""Session identity resolution for the JSONL timetracking backend (bd-timew-sfk).

Resolves a session id for a ``bd-track`` invocation, agent-agnostically, in
this precedence order:

  1. an explicit ``--session-id``
  2. ``$BD_TRACK_SESSION_ID``      — coordinator / future claude-config plumbing
  3. ``$CLAUDE_CODE_SESSION_ID``   — auto-present in any Claude Code subprocess,
                                     so a live Claude session resolves correctly
                                     with no cooperation from the session itself
  4. the current-session pointer   — cross-invocation continuity for non-Claude
                                     terminal callers (humans, scripts)
  5. a freshly generated, human-friendly id (xkcdpass), cached in the pointer

Session ids need only be unique among *concurrently active* sessions within a
log, not globally unique: start/stop pairing keys on the interval ULID (see
bd-timew-ahp), and historical session instances stay separable by event
timestamps. Generation therefore uses a low-entropy, human-legible
``word.word.word`` phrase guarded by an active-collision check.

The pointer is operational state only — machine-local, never synced, and safe
to lose: its loss merely starts a fresh session on the next call. It maps a
caller key (controlling tty, else PPID) to the session id to reuse, so a
human's ``start`` … ``stop`` in one terminal share an id without anyone having
to remember or retype it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from bd_track.util import env_compat, find_beads_dir, path_compat, xdg_home

# Operational state — XDG_STATE_HOME, not ~/.cache: the pointer is regenerable
# but not throwaway mid-session. Never synced (see the operational/authoritative
# split in docs/dev/timetracking-architecture.06022026.md).
SESSION_STATE_DIR = path_compat(xdg_home("state") / "bd-track", xdg_home("state") / "bd-timew")
POINTER_NAME = "current-session.json"

# A pointer entry unused for longer than this is treated as a finished session;
# the next call on that key mints a fresh id rather than conflating two work
# sessions that happened to share a terminal.
POINTER_STALE_HOURS = 4
_GEN_MAX_ATTEMPTS = 8

ENV_SESSION = "BD_TRACK_SESSION_ID"
ENV_CLAUDE_SESSION = "CLAUDE_CODE_SESSION_ID"


def project_id(project_root: Path) -> str:
    """Stable per-project key: ``<basename>-<8-char path hash>``.

    The hash disambiguates same-named directories at different paths so their
    pointers don't collide under a shared state root.
    """
    resolved = project_root.resolve()
    digest = hashlib.sha1(str(resolved).encode()).hexdigest()[:8]
    return f"{resolved.name}-{digest}"


def _pointer_path(project_root: Path) -> Path:
    return SESSION_STATE_DIR / project_id(project_root) / POINTER_NAME


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _session_key() -> str:
    """Identify the calling context for pointer continuity.

    Prefers the controlling tty (stable across a human's terminal session);
    falls back to the parent PID when there is no tty (non-interactive callers).
    """
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                return f"tty:{os.ttyname(fd)}"
        except OSError:
            continue
    return f"ppid:{os.getppid()}"


def _load_pointer(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # The pointer is disposable operational state; a corrupt or unreadable
        # file just resets continuity rather than being an error.
        return {}
    return data if isinstance(data, dict) else {}


def _save_pointer(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _is_fresh(entry: object, now: dt.datetime) -> bool:
    if not isinstance(entry, dict):
        return False
    ts = entry.get("last_seen")
    if not ts:
        return False
    try:
        last = dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    return (now - last) < dt.timedelta(hours=POINTER_STALE_HOURS)


def _active_ids(pointer: dict, now: dt.datetime) -> set[str]:
    """Session ids of currently-fresh pointer entries (the active-collision set)."""
    return {
        entry["session_id"]
        for entry in pointer.values()
        if _is_fresh(entry, now) and entry.get("session_id")
    }


def _generate_session_id(taken: set[str]) -> str:
    """Generate a human-friendly ``word.word.word`` id, avoiding active collisions."""
    from xkcdpass import xkcd_password as xp

    wordfile = xp.locate_wordfile()
    words = xp.generate_wordlist(wordfile=wordfile, min_length=3, max_length=7)
    candidate = ""
    for _ in range(_GEN_MAX_ATTEMPTS):
        candidate = xp.generate_xkcdpassword(words, numwords=3, delimiter=".")
        if candidate not in taken:
            return candidate
    # Astronomically unlikely with a 3-word phrase; disambiguate deterministically.
    return f"{candidate}.{os.getpid()}"


def resolve_session_id(
    project_root: Path | None = None,
    *,
    explicit: str | None = None,
) -> str:
    """Resolve the session id for this invocation (see module docstring for precedence)."""
    if explicit:
        return explicit

    injected = env_compat(ENV_SESSION) or os.environ.get(ENV_CLAUDE_SESSION)
    if injected:
        return injected

    # Pointer path: non-Claude terminal continuity. Only here do we need the
    # project root (so an env/flag-injected caller never pays for `bd where`).
    if project_root is None:
        project_root = find_beads_dir().parent
    path = _pointer_path(project_root)
    pointer = _load_pointer(path)
    now = _now()
    key = _session_key()

    entry = pointer.get(key)
    if _is_fresh(entry, now) and isinstance(entry, dict) and entry.get("session_id"):
        session_id = entry["session_id"]
    else:
        session_id = _generate_session_id(_active_ids(pointer, now))

    pointer[key] = {"session_id": session_id, "last_seen": now.isoformat()}
    _save_pointer(path, pointer)
    return session_id


def cmd_session_current(
    project_dir: Path | None = None,
    *,
    explicit: str | None = None,
) -> None:
    """Print the resolved session id for this invocation context (machine-readable)."""
    project_root = find_beads_dir(project_dir).parent if project_dir else None
    print(resolve_session_id(project_root, explicit=explicit))
