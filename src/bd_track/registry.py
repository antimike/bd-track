"""Project registry: load ``projects.yaml`` (or legacy ``repos.yaml``) and write it back.

The schema, location order and ownership rules are specified in docs/registry-schema.md; this
module implements them. Summary:

- **Location**: ``--registry`` → ``$BD_TRACK_REGISTRY`` → ``~/.config/projects.yaml`` if it
  exists → legacy ``~/.config/bd-track/repos.yaml``.
- **Schema**: ``projects: {<name>: {remote, path, beads: {prefix, ...}}}``. A legacy
  ``repos:`` list (keyed by absolute path) is read as projects named by path basename.
- **Remotes** (``bd-timew-q9o.6``): ``remote`` is any URL git accepts, or the ``owner/repo``
  shorthand. Projects match clones by the URL's path (host ignored, so ssh host aliases match);
  a shorthand's clone URL is the host's ``default_url_prefix`` + ``owner/repo``.
- **Bridge** (until ``bd-timew-d0t.4`` migrates ``repos.yaml``): when the registry is the new
  file and ``repos.yaml`` also exists, each legacy entry fills in the path and bd-track settings
  of the project whose remote matches its clone's ``origin``, or is added under its basename.
- **Finding clones** (``bd-timew-d0t.3``): a project with a ``remote`` but no ``path`` gets its
  path from a cache of earlier finds (each checked against the clone's ``origin``), else from a
  scan of the host's ``scan_roots`` for a clone whose ``origin`` matches. A remote with two or
  more clones stays unresolved (pin one with ``path:``). With no roots configured, no scan runs.
- **Host settings** (``global:``) come from ``~/.config/bd-track/config.yaml``, else the legacy
  ``repos.yaml`` ``global:``.
- **Switches** (``dolt_push``, ``state_export``): the host value ``always``/``never`` forces,
  ``true``/``false`` is a default that the project's ``beads.<key>`` overrides.
- **Writes** round-trip with ruamel.yaml (comments, order and unknown keys survive), touch only
  bd-track-owned keys, and replace the file atomically. The legacy file is never written here.

YAML is read as 1.2, so ``yes``/``no``/``on``/``off`` are strings, not booleans.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from bd_track.util import _CACHE_DIR, _CONFIG_DIR, REPOS_CONFIG, root_log

DEFAULT_REGISTRY = Path.home() / ".config" / "projects.yaml"
HOST_CONFIG = _CONFIG_DIR / "config.yaml"
PATH_CACHE = _CACHE_DIR / "registry-paths.json"

# A full scan runs at most this often while projects stay unresolved; a cache miss for a project
# the user names (``--project``) forces one.
SCAN_TTL = dt.timedelta(hours=24)
SCAN_MAX_DEPTH = 5
SCAN_SKIP_DIRS = {"node_modules", ".venv", "venv", ".cache", ".tox", "__pycache__"}

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

# beads.* keys bd-track owns; ``prefix`` is shared (see the ownership table in the schema doc).
BD_TRACK_BEADS_KEYS = (
    "commit_cadence", "compact_days", "statuses", "dolt_push", "dolt_remote_url",
    "dolt_remote_private", "state_repo", "server",
)
SHARED_KEYS = ("remote", "path")

# The owner/repo shorthand: exactly two components, no scheme, no ':'.
_SHORTHAND_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# scp form [user@]host:path; a ':' after the first '/' is part of a path instead.
_SCP_RE = re.compile(r"^(?:[^@/:\s]+@)?[^/:\s]+:(?!//)(?P<path>.*)$")


class RegistryError(Exception):
    """A registry or host config file that cannot be used as written."""


@dataclass
class Project:
    """One registry entry. ``path`` is absolute when known (from the file or the bridge)."""

    name: str
    remote: str | None = None  # match key (``normalize_remote``)
    path: Path | None = None
    beads: dict = field(default_factory=dict)
    legacy: bool = False  # came from (or was completed by) repos.yaml
    remote_spec: str | None = None  # ``remote`` as written in the registry

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

    def for_directory(self, path: Path) -> Project | None:
        """The project a clone at ``path`` belongs to (schema doc, "Resolving a project").

        A project whose ``path`` is this directory wins. Otherwise the clone's ``origin`` is
        matched against ``remote``, but only for projects without a ``path``: a path-pinned
        project names only its own clone.
        """
        pinned = self.by_path(path)
        if pinned is not None:
            return pinned
        remote = origin_remote(path)
        if remote is None:
            return None
        return next(
            (p for p in self.projects.values() if p.remote == remote and p.path is None),
            None,
        )

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


def is_shorthand(remote: str) -> bool:
    """Whether ``remote`` is the ``owner/repo`` shorthand rather than a URL or path."""
    return bool(_SHORTHAND_RE.fullmatch(remote.strip()))


def normalize_remote(remote: str) -> str | None:
    """The match key of a remote: its path, lowercased, without ``.git`` or edge slashes.

    The host is dropped, so ``git@github.com:o/r.git``, an ssh alias ``git@me.github.com:o/r``,
    ``https://github.com/o/r`` and the shorthand ``o/r`` all give ``o/r``, while a nested path
    (``group/sub/repo``) keeps every component. ``None`` for anything with fewer than two path
    components or with whitespace.
    """
    text = remote.strip().removeprefix("git+")
    if not text or any(c.isspace() for c in text):
        return None
    if _SCHEME_RE.match(text):
        path = text.split("://", 1)[1].partition("/")[2]
    elif (match := _SCP_RE.match(text)) and not text.startswith(("/", "~", ".")):
        path = match["path"]
    else:
        path = text
    path = path.strip("/").removesuffix(".git").removeprefix("~/").strip("/")
    parts = [part for part in path.split("/") if part not in ("", ".")]  # Dolt: host/./o/r
    if len(parts) < 2:
        return None
    return "/".join(parts).lower()


def clone_url(project: Project, host: dict) -> str | None:
    """The URL to clone ``project`` from: its ``remote`` when that is a URL or path, or
    ``global.default_url_prefix`` + the ``owner/repo`` shorthand; ``None`` when neither."""
    spec = project.remote_spec
    if not spec:
        return None
    if is_shorthand(spec):
        prefix = host.get("default_url_prefix")
        return f"{prefix}{spec}" if prefix else None
    return spec


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
        spec = str(entry["remote"]).strip() if entry.get("remote") else None
        remote = normalize_remote(spec) if spec else None
        if spec and remote is None:
            root_log.warning(
                "%s: project %r: remote %r is not a git URL or owner/repo; ignored",
                source, name, spec,
            )
            spec = None
        projects[name] = Project(
            name=name,
            remote=remote,
            path=_expand(entry["path"]) if entry.get("path") else None,
            beads=dict(beads) if isinstance(beads, dict) else {},
            remote_spec=spec,
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


def load_registry(explicit: Path | None = None, *, scan: bool | None = None) -> Registry:
    """Load the registry (see the module docstring); an absent file is an empty registry.

    ``scan`` controls finding clones of path-less projects: ``None`` scans when the last scan is
    older than ``SCAN_TTL``, ``True`` always scans, ``False`` uses the cache only.
    """
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
    resolve_clones(registry, scan=scan)
    return registry


# ---------------------------------------------------------------------------
# Finding clones of path-less projects
# ---------------------------------------------------------------------------


def scan_roots() -> list[Path]:
    """``$BD_TRACK_SCAN_ROOTS`` (``:``-separated), else host ``global.scan_roots``, else none."""
    env = os.environ.get("BD_TRACK_SCAN_ROOTS")
    if env is not None:
        raw: list = [part for part in env.split(os.pathsep) if part.strip()]
    else:
        raw = load_host_config().get("scan_roots") or []
        if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
            raise RegistryError(f"{HOST_CONFIG}: global.scan_roots must be a list of paths")
    return [Path(r).expanduser() for r in raw]


def find_clones(roots: list[Path], max_depth: int = SCAN_MAX_DEPTH) -> dict[str, list[Path]]:
    """Map normalized ``origin`` remote to the clones under ``roots`` that have it.

    A clone is a directory holding a ``.git`` directory; worktrees and submodules (whose ``.git``
    is a file) are not counted, so a project's worktrees do not make its remote ambiguous.
    """
    clones: dict[str, set[Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        base = len(root.resolve().parts)
        for current, dirs, _files in os.walk(root.resolve()):
            here = Path(current)
            if (here / ".git").is_dir():
                remote = origin_remote(here)
                if remote:
                    clones.setdefault(remote, set()).add(here)
            if len(here.parts) - base >= max_depth:
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if d != ".git" and d not in SCAN_SKIP_DIRS]
    return {remote: sorted(paths) for remote, paths in clones.items()}


def _load_cache() -> dict:
    try:
        data = json.loads(PATH_CACHE.read_text())
    except (OSError, ValueError):
        return {"paths": {}}
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        return {"paths": {}}
    return data


def _save_cache(cache: dict) -> None:
    try:
        _atomic_write(PATH_CACHE, json.dumps(cache, indent=2, sort_keys=True) + "\n")
    except OSError as err:
        root_log.debug("could not write %s: %s", PATH_CACHE, err)


def _scan_due(cache: dict) -> bool:
    stamp = cache.get("scanned_at")
    try:
        last = dt.datetime.fromisoformat(stamp) if stamp else None
    except (TypeError, ValueError):
        last = None
    return last is None or dt.datetime.now(dt.timezone.utc) - last >= SCAN_TTL


def resolve_clones(registry: Registry, *, scan: bool | None = None) -> None:
    """Fill in ``path`` for projects that have a ``remote`` but no ``path`` (module docstring)."""
    wanted = {p.remote: p for p in registry.projects.values() if p.remote and p.path is None}
    if not wanted:
        return
    taken = {p.path for p in registry.projects.values() if p.path is not None}
    cache = _load_cache()
    paths: dict[str, str] = cache["paths"]
    changed = False

    for remote, project in wanted.items():
        cached = paths.get(remote)
        if cached is None:
            continue
        clone = Path(cached)
        if clone not in taken and origin_remote(clone) == remote:
            project.path = clone
        else:
            root_log.debug("dropping stale cached path %s for %s", cached, remote)
            del paths[remote]
            changed = True

    unresolved = [r for r, p in wanted.items() if p.path is None]
    if unresolved and (scan or (scan is None and _scan_due(cache))):
        roots = scan_roots()
        if roots:
            found = find_clones(roots)
            for remote in unresolved:
                clones = [c for c in found.get(remote, []) if c not in taken]
                if len(clones) == 1:
                    wanted[remote].path = clones[0]
                    paths[remote] = str(clones[0])
                elif clones:
                    root_log.warning(
                        "project %s: %d clones of %s (%s); pin one with 'path:' in the registry",
                        wanted[remote].name,
                        len(clones),
                        remote,
                        ", ".join(str(c) for c in clones),
                    )
            cache["scanned_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            changed = True
    if changed:
        _save_cache(cache)


DEFAULT_STATE_LAYOUT = "{project}/issues.jsonl"
DEFAULT_PUSH_TIMEOUT = 120
SWITCH_KEYS = ("dolt_push", "state_export")


def load_host_config() -> dict:
    """Host settings: ``config.yaml`` ``global:``, else legacy ``repos.yaml`` ``global:``.

    Missing keys get defaults. The switches (``dolt_push``, ``state_export``) must be
    ``always``, ``never``, ``true`` or ``false``; ``state_layout`` must name ``{project}`` and
    stay inside the state repo.
    """
    source = HOST_CONFIG if HOST_CONFIG.exists() else REPOS_CONFIG
    data = _read(source) if source.exists() else {}
    host = dict(data.get("global") or {})
    host.setdefault("check_interval", "daily")
    host.setdefault("idle_stop_hours", 4)
    host.setdefault("dolt_push", False)
    host.setdefault("dolt_remote_url", None)
    host.setdefault("dolt_push_timeout", DEFAULT_PUSH_TIMEOUT)
    host.setdefault("state_export", False)
    host.setdefault("state_repo", None)
    host.setdefault("state_layout", DEFAULT_STATE_LAYOUT)
    host.setdefault("host_name", default_host_name())
    host.setdefault("default_url_prefix", None)
    defaults = dict(host.get("defaults") or {})
    defaults.setdefault("statuses", [])
    defaults.setdefault("hooks", [])
    host["defaults"] = defaults
    for key in SWITCH_KEYS:
        value = host[key]
        if not (isinstance(value, bool) or value in ("always", "never")):
            raise RegistryError(
                f"{source}: global.{key} must be always, never, true or false (got {value!r})"
            )
    timeout = host["dolt_push_timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise RegistryError(
            f"{source}: global.dolt_push_timeout must be a positive number of seconds "
            f"(got {timeout!r})"
        )
    _check_layout(host["state_layout"], source)
    if not is_ref_component(host["host_name"]):
        raise RegistryError(
            f"{source}: global.host_name must be usable as one git ref component (letters, "
            f"digits, '-', '_', '.'; no '/', no leading '.') (got {host['host_name']!r})"
        )
    prefix = host["default_url_prefix"]
    if prefix is not None and not (
        isinstance(prefix, str) and prefix.strip() and prefix.endswith((":", "/"))
    ):
        raise RegistryError(
            f"{source}: global.default_url_prefix must be a URL prefix ending in ':' or '/', "
            f"e.g. 'git@github.com:' (got {prefix!r})"
        )
    return host


def default_host_name() -> str:
    """The short hostname, lowercased: ``global.host_name`` when unset."""
    return socket.gethostname().split(".")[0].lower()


_REF_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\/]|\.\.|@\{")


def is_ref_component(name: object) -> bool:
    """Whether ``name`` is valid as one ``/``-free component of a git ref name
    (``git check-ref-format`` rules)."""
    return (
        isinstance(name, str) and bool(name) and name != "@"
        and not name.startswith(".") and not name.endswith((".", ".lock"))
        and not _REF_BAD.search(name)
    )


def _check_layout(layout: object, source: Path) -> None:
    """A layout is a relative path template with exactly the field ``{project}``."""
    problem = None
    if not isinstance(layout, str):
        problem = "must be a string"
    else:
        try:
            rel = Path(layout.format(project="x"))
        except (KeyError, IndexError, ValueError):
            problem = "may use only the field {project}"
        else:
            if "{project}" not in layout:
                problem = "must contain {project}"
            elif rel.is_absolute() or ".." in rel.parts:
                problem = "must be a relative path inside the state repo"
    if problem:
        raise RegistryError(f"{source}: global.state_layout {problem} (got {layout!r})")


def switch_enabled(host_value: bool | str, project_value: object) -> bool:
    """Resolve a four-valued host switch against a project's override (schema doc, dolt_push).

    ``always``/``never`` force the result; ``true``/``false`` is the default, which a project
    value overrides. Any non-boolean project value other than ``None`` (e.g. a path) counts as on.
    """
    if host_value in ("always", "never"):
        return host_value == "always"
    if project_value is None:
        return bool(host_value)
    if isinstance(project_value, bool):
        return project_value
    return True


def dolt_push_enabled(project: Project, host: dict) -> bool:
    """Whether cleanup pushes ``project``'s database (schema doc, ``dolt_push``).

    ``beads.dolt_push`` must be a boolean when set: YAML 1.2 reads ``no`` or ``off`` as
    strings, which must not count as on. ``beads.dolt_remote_private`` likewise. Raises
    ``RegistryError`` otherwise.
    """
    for key in ("dolt_push", "dolt_remote_private"):
        value = project.beads.get(key)
        if value is not None and not isinstance(value, bool):
            raise RegistryError(
                f"project {project.name}: beads.{key} must be true or false (got {value!r})"
            )
    return switch_enabled(host["dolt_push"], project.beads.get("dolt_push"))


def state_export_target(project: Project, host: dict) -> tuple[Path, Path] | None:
    """``(state repo, export file)`` for ``project``'s ``cleanup`` export, or ``None`` if off.

    ``beads.state_repo`` is ``true``/``false`` or a path (on, and that repo instead of the
    host's ``state_repo``). Raises ``RegistryError`` when export is on but no repo is known, or
    the project value has the wrong type.
    """
    value = project.beads.get("state_repo")
    if value is not None and not isinstance(value, (bool, str)):
        raise RegistryError(
            f"project {project.name}: beads.state_repo must be true, false or a path "
            f"(got {value!r})"
        )
    if not switch_enabled(host["state_export"], value):
        return None
    repo = value if isinstance(value, str) else host.get("state_repo")
    if not repo:
        raise RegistryError(
            f"project {project.name}: state export is on but no state repo is configured "
            "(set global.state_repo, or a path in beads.state_repo)"
        )
    root = _expand(repo)
    return root, root / host["state_layout"].format(project=project.name)


def state_sessions_dir(project: Project, host: dict, export_file: Path) -> Path:
    """Where the export puts ``project``'s session event logs: ``sessions/`` beside the issues
    export, or ``sessions/<project>/`` there when the layout's directory is shared by all
    projects (``{project}`` only in the file name), so no two projects share one directory.
    """
    sessions = export_file.parent / "sessions"
    if "{project}" in str(Path(host["state_layout"]).parent):
        return sessions
    return sessions / project.name


def load_registry_or_exit(explicit: Path | None = None, *, scan: bool | None = None) -> Registry:
    """``load_registry`` for CLI callers: a broken file is a clean ``bd-track:`` exit."""
    try:
        return load_registry(explicit, scan=scan)
    except RegistryError as err:
        sys.exit(f"bd-track: {err}")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def set_project(name: str, values: dict, explicit: Path | None = None) -> Path:
    """Write ``values`` for project ``name`` into the registry file; return the file.

    Only bd-track-owned keys are replaced; a ``None`` value removes one. Shared identity keys
    (``remote``, ``path``, ``beads.prefix``) are written only when absent, so a new project can
    be registered but another tool's values are never changed. Everything else in the file is
    preserved.
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
            if key not in beads_values:
                continue
            if beads_values[key] is None:
                beads.pop(key, None)
            else:
                beads[key] = beads_values[key]

    out = StringIO()
    yaml.dump(data, out)
    _atomic_write(target, out.getvalue())
    return target


HOST_KEYS = (
    "check_interval", "idle_stop_hours", "dolt_push", "defaults", "scan_roots",
    "dolt_remote_url", "dolt_push_timeout", "state_export", "state_repo", "state_layout",
    "host_name", "default_url_prefix",
)


def set_host(values: dict) -> Path:
    """Write ``values`` into ``config.yaml`` ``global:`` (created if missing), round-trip.

    Only the given keys change; other keys and comments are kept.
    """
    unknown = set(values) - set(HOST_KEYS)
    if unknown:
        raise RegistryError(f"unknown host setting(s): {', '.join(sorted(unknown))}")
    yaml = _yaml("rt")
    data = None
    if HOST_CONFIG.exists():
        try:
            data = yaml.load(HOST_CONFIG.read_text())
        except YAMLError as err:
            raise RegistryError(f"{HOST_CONFIG}: not valid YAML ({err})") from err
        if data is not None and not isinstance(data, dict):
            raise RegistryError(f"{HOST_CONFIG}: expected a mapping at the top level")
    if data is None:
        data = yaml.load("global: {}\n")
    if data.get("global") is None:
        data["global"] = {}
    for key, value in values.items():
        data["global"][key] = value
    out = StringIO()
    yaml.dump(data, out)
    _atomic_write(HOST_CONFIG, out.getvalue())
    return HOST_CONFIG


def seed_path_cache(paths: dict[str, Path]) -> None:
    """Record known clones (normalized remote → path) in the path cache, keeping the rest."""
    cache = _load_cache()
    for remote, path in paths.items():
        cache["paths"][remote] = str(path)
    _save_cache(cache)


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
