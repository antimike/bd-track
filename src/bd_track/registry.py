"""Project registry: load ``projects.yaml`` (or legacy ``repos.yaml``) and write it back.

The schema, location order and ownership rules are specified in docs/registry-schema.md; this
module implements them. Summary:

- **Location**: ``--registry`` → ``$BD_TRACK_REGISTRY`` → ``~/.config/projects.yaml`` if it
  exists → legacy ``~/.config/bd-track/repos.yaml``.
- **Schema**: ``projects: {<name>: {remote, path, beads: {prefix, ...}}}``. A legacy
  ``repos:`` list (keyed by absolute path) is read as projects named by path basename.
- **Bridge** (until ``bd-timew-d0t.4`` migrates ``repos.yaml``): when the registry is the new
  file and ``repos.yaml`` also exists, each legacy entry fills in the path and bd-track settings
  of the project whose remote matches its clone's ``origin``, or is added under its basename.
- **Host settings** (``global:``) come from ``~/.config/bd-track/config.yaml``, else the legacy
  ``repos.yaml`` ``global:``.
- **Writes** round-trip with ruamel.yaml (comments, order and unknown keys survive), touch only
  bd-track-owned keys, and replace the file atomically. The legacy file is never written here.

YAML is read as 1.2, so ``yes``/``no``/``on``/``off`` are strings, not booleans.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from bd_track.util import _CONFIG_DIR, REPOS_CONFIG, root_log

DEFAULT_REGISTRY = Path.home() / ".config" / "projects.yaml"
HOST_CONFIG = _CONFIG_DIR / "config.yaml"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

# beads.* keys bd-track owns; ``prefix`` is shared (see the ownership table in the schema doc).
BD_TRACK_BEADS_KEYS = ("commit_cadence", "compact_days", "statuses", "dolt_push", "server")
SHARED_KEYS = ("remote", "path")

_REMOTE_RE = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<repo>[^/:]+?)(?:\.git)?/?$")


class RegistryError(Exception):
    """A registry or host config file that cannot be used as written."""


@dataclass
class Project:
    """One registry entry. ``path`` is absolute when known (from the file or the bridge)."""

    name: str
    remote: str | None = None
    path: Path | None = None
    beads: dict = field(default_factory=dict)
    legacy: bool = False  # came from (or was completed by) repos.yaml

    @property
    def uses_beads(self) -> bool:
        return "prefix" in self.beads or self.legacy


@dataclass
class Registry:
    source: Path | None
    legacy: bool
    projects: dict[str, Project] = field(default_factory=dict)

    def get(self, name: str) -> Project | None:
        return self.projects.get(name)

    def by_path(self, path: Path) -> Project | None:
        """The project whose known path is ``path``."""
        target = path.expanduser().resolve()
        return next((p for p in self.projects.values() if p.path == target), None)

    def beads_projects(self) -> list[Project]:
        """Projects that use beads and have a known path, in registry order."""
        return [p for p in self.projects.values() if p.uses_beads and p.path is not None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yaml(typ: str = "safe") -> YAML:
    yaml = YAML(typ=typ)
    if typ == "rt":
        yaml.preserve_quotes = True
        yaml.width = 4096
    return yaml


def normalize_remote(remote: str) -> str | None:
    """``owner/repo`` (lowercase) from any URL or ``owner/repo`` form, else ``None``."""
    text = remote.strip()
    if "/" in text and ":" not in text and text.count("/") == 1:
        text = "/" + text
    match = _REMOTE_RE.search(text)
    if not match:
        return None
    return f"{match['owner']}/{match['repo']}".lower()


def origin_remote(path: Path) -> str | None:
    """Normalized ``origin`` of the clone at ``path``, or ``None``."""
    if not path.is_dir():
        return None
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        check=False,
        text=True,
        capture_output=True,
    )
    return normalize_remote(result.stdout) if result.returncode == 0 else None


def _expand(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _read(path: Path) -> dict:
    try:
        data = _yaml().load(path.read_text())
    except YAMLError as err:
        raise RegistryError(f"{path}: not valid YAML ({err})") from err
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: expected a mapping at the top level")
    return data


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


def registry_path(explicit: Path | None = None) -> Path | None:
    """The registry file to use, or ``None`` when there is none anywhere."""
    if explicit is not None:
        return explicit.expanduser()
    env = os.environ.get("BD_TRACK_REGISTRY")
    if env:
        return Path(env).expanduser()
    if DEFAULT_REGISTRY.exists():
        return DEFAULT_REGISTRY
    if REPOS_CONFIG.exists():
        return REPOS_CONFIG
    return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_projects(data: dict, source: Path) -> dict[str, Project]:
    projects: dict[str, Project] = {}
    raw = data.get("projects") or {}
    if not isinstance(raw, dict):
        raise RegistryError(f"{source}: 'projects' must be a mapping of name to entry")
    for name, entry in raw.items():
        name = str(name)
        if not NAME_RE.fullmatch(name):
            root_log.warning(
                "%s: skipping project %r (names are [a-z0-9][a-z0-9_-]*)", source, name
            )
            continue
        entry = entry or {}
        if not isinstance(entry, dict):
            root_log.warning("%s: skipping project %r (entry is not a mapping)", source, name)
            continue
        beads = entry.get("beads") or {}
        projects[name] = Project(
            name=name,
            remote=normalize_remote(str(entry["remote"])) if entry.get("remote") else None,
            path=_expand(entry["path"]) if entry.get("path") else None,
            beads=dict(beads) if isinstance(beads, dict) else {},
        )
    return projects


def _legacy_projects(data: dict, source: Path) -> list[Project]:
    projects = []
    for entry in data.get("repos") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            root_log.warning("%s: skipping a repos entry without 'path'", source)
            continue
        path = _expand(entry["path"])
        projects.append(
            Project(
                name=path.name,
                remote=origin_remote(path),
                path=path,
                beads={k: entry[k] for k in BD_TRACK_BEADS_KEYS if k in entry},
                legacy=True,
            )
        )
    return projects


def _bridge(projects: dict[str, Project], legacy: list[Project]) -> None:
    """Complete registry projects from repos.yaml entries (see the module docstring)."""
    for old in legacy:
        match = next(
            (p for p in projects.values() if p.path == old.path),
            None,
        ) or next(
            (
                p
                for p in projects.values()
                if old.remote and p.remote == old.remote and p.path is None
            ),
            None,
        )
        if match is None:
            if old.name in projects:
                root_log.warning(
                    "repos.yaml entry %s: name %r is taken in the registry by another "
                    "project; skipped",
                    old.path,
                    old.name,
                )
                continue
            projects[old.name] = old
            continue
        match.path = match.path or old.path
        match.legacy = True
        for key, value in old.beads.items():
            match.beads.setdefault(key, value)


def load_registry(explicit: Path | None = None) -> Registry:
    """Load the registry (see the module docstring); an absent file is an empty registry."""
    source = registry_path(explicit)
    if source is None:
        return Registry(source=None, legacy=False)
    if not source.exists():
        if explicit is not None or os.environ.get("BD_TRACK_REGISTRY"):
            raise RegistryError(f"{source}: no such registry file")
        return Registry(source=source, legacy=False)
    data = _read(source)
    if "projects" not in data and "repos" in data:
        projects = {p.name: p for p in _legacy_projects(data, source)}
        return Registry(source=source, legacy=True, projects=projects)
    registry = Registry(source=source, legacy=False, projects=_parse_projects(data, source))
    if REPOS_CONFIG.exists() and REPOS_CONFIG.resolve() != source.resolve():
        _bridge(registry.projects, _legacy_projects(_read(REPOS_CONFIG), REPOS_CONFIG))
    return registry


def load_host_config() -> dict:
    """Host settings: ``config.yaml`` ``global:``, else legacy ``repos.yaml`` ``global:``.

    Missing keys get defaults. ``dolt_push`` must be ``always``, ``never``, ``true`` or ``false``.
    """
    source = HOST_CONFIG if HOST_CONFIG.exists() else REPOS_CONFIG
    data = _read(source) if source.exists() else {}
    host = dict(data.get("global") or {})
    host.setdefault("check_interval", "daily")
    host.setdefault("idle_stop_hours", 4)
    host.setdefault("dolt_push", False)
    defaults = dict(host.get("defaults") or {})
    defaults.setdefault("statuses", [])
    defaults.setdefault("hooks", [])
    host["defaults"] = defaults
    value = host["dolt_push"]
    if not (isinstance(value, bool) or value in ("always", "never")):
        raise RegistryError(
            f"{source}: global.dolt_push must be always, never, true or false "
            f"(got {host['dolt_push']!r})"
        )
    return host


def load_registry_or_exit(explicit: Path | None = None) -> Registry:
    """``load_registry`` for CLI callers: a broken file is a clean ``bd-track:`` exit."""
    try:
        return load_registry(explicit)
    except RegistryError as err:
        sys.exit(f"bd-track: {err}")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def set_project(name: str, values: dict, explicit: Path | None = None) -> Path:
    """Write ``values`` for project ``name`` into the registry file; return the file.

    Only bd-track-owned keys are replaced. Shared identity keys (``remote``, ``path``,
    ``beads.prefix``) are written only when absent, so a new project can be registered but
    another tool's values are never changed. Everything else in the file is preserved.
    """
    if not NAME_RE.fullmatch(name):
        raise RegistryError(f"invalid project name {name!r} (names are [a-z0-9][a-z0-9_-]*)")
    unknown = set(values) - {*SHARED_KEYS, "beads"}
    unknown |= set(values.get("beads") or {}) - {*BD_TRACK_BEADS_KEYS, "prefix"}
    if unknown:
        raise RegistryError(f"bd-track does not own registry key(s): {', '.join(sorted(unknown))}")

    target = registry_path(explicit) or DEFAULT_REGISTRY
    if target.exists() and target.resolve() == REPOS_CONFIG.resolve():
        target = DEFAULT_REGISTRY  # never write the legacy file; start the new one
    yaml = _yaml("rt")
    if target.exists():
        try:
            data = yaml.load(target.read_text())
        except YAMLError as err:
            raise RegistryError(f"{target}: not valid YAML ({err})") from err
        if data is not None and not isinstance(data, dict):
            raise RegistryError(f"{target}: expected a mapping at the top level")
        if data is not None and "projects" not in data and "repos" in data:
            raise RegistryError(f"{target}: legacy repos.yaml schema; migrate it first")
    else:
        data = None
    if data is None:
        data = yaml.load("projects: {}\n")
    if data.get("projects") is None:
        data["projects"] = {}
    projects = data["projects"]
    if projects.get(name) is None:
        projects[name] = {}
    entry = projects[name]

    for key in SHARED_KEYS:
        if key in values and key not in entry:
            entry[key] = values[key]
    beads_values = values.get("beads") or {}
    if beads_values:
        if entry.get("beads") is None:
            entry["beads"] = {}
        beads = entry["beads"]
        if "prefix" in beads_values and "prefix" not in beads:
            beads["prefix"] = beads_values["prefix"]
        for key in BD_TRACK_BEADS_KEYS:
            if key in beads_values:
                beads[key] = beads_values[key]

    out = StringIO()
    yaml.dump(data, out)
    _atomic_write(target, out.getvalue())
    return target


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
