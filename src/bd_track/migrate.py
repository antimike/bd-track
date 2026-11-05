"""``bd-track migrate rename`` — one-way bd-timew → bd-track on-disk migration.

Companion to the read-fallback shims in :mod:`bd_track.util` (``env_compat`` /
``path_compat``), which let an upgraded bd-track keep working on a project still
laid out for bd-timew. This command performs the *cleanup* those shims defer:
rename the on-disk artifacts to the new name so the fallbacks stop firing.

Scope (see ``bd-timew-jy9``):
  * Global dirs:  ~/.config|cache|state/bd-timew, ~/.local/share/bd-timew
  * Per project:  .beads/bd-timew.yaml sidecar, <beads>/bd-timew session logs,
                  and ``BD_TIMEW_*`` → ``BD_TRACK_*`` env-var rewrites in
                  .envrc / .env / .envrc.local / mise.toml / .mise.toml
  * ``--all-repos`` sweeps every beads project in the registry (projects.yaml), not just cwd.

Safety: dry-run by default (``--apply`` to write); per-file ``.bak`` backups;
chezmoi-managed home/dotfile targets are skipped with a warning (editing a
chezmoi target desyncs its source — use ``chezmoi apply`` for those). The
``migrate`` namespace is shared with ``bd-timew-73v`` (timew-data import).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

from bd_track.util import find_beads_dir, root_log, run, xdg_home

# Env files scanned for BD_TIMEW_* → BD_TRACK_* rewrites, relative to a repo root.
_ENV_FILES = (".envrc", ".env", ".envrc.local", "mise.toml", ".mise.toml")
_ENV_OLD = "BD_TIMEW_"
_ENV_NEW = "BD_TRACK_"

# Old/new component name under each home base dir and the beads dir.
_OLD = "bd-timew"
_NEW = "bd-track"


@dataclasses.dataclass
class Action:
    """One planned migration step. ``do`` is False for skip/no-op entries."""

    kind: str  # "dir" | "file" | "env"
    label: str  # human-readable source description
    do: bool  # True = will act under --apply; False = informational skip
    detail: str  # right-hand description ("→ <dst>", "skipped: ...", etc.)
    src: Path | None = None
    dst: Path | None = None
    count: int = 0  # env: number of BD_TIMEW_* occurrences rewritten


# ---------------------------------------------------------------------------
# chezmoi guard
# ---------------------------------------------------------------------------


def _chezmoi_managed_set() -> set[Path] | None:
    """Resolve absolute paths chezmoi manages, or None if chezmoi is unavailable.

    None (vs empty set) signals "could not consult chezmoi" so the caller can
    note it rather than silently treating everything as unmanaged.
    """
    if shutil.which("chezmoi") is None:
        return None
    res = run(["chezmoi", "managed", "--path-style", "absolute"], check=False, capture=True)
    if res.returncode != 0:
        return None
    return {Path(line.strip()) for line in res.stdout.splitlines() if line.strip()}


def _is_managed(path: Path, managed: set[Path] | None) -> bool:
    """True if ``path`` (a file) or any descendant (a dir) is chezmoi-managed."""
    if not managed:
        return False
    rp = path.resolve()
    if rp in managed:
        return True
    # Directory target: managed if chezmoi owns anything beneath it.
    return any(rp == m or rp in m.parents for m in managed)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _plan_move(
    src: Path, dst: Path, kind: str, *, managed: set[Path] | None, check_chezmoi: bool
) -> Action:
    """Plan a rename of ``src`` → ``dst`` with idempotency + chezmoi guards."""
    label = str(src)
    if not src.exists():
        return Action(kind, label, do=False, detail="skipped: source absent", src=src, dst=dst)
    if dst.exists():
        return Action(
            kind,
            label,
            do=False,
            detail=f"skipped: target {dst} already exists (manual merge)",
            src=src,
            dst=dst,
        )
    if check_chezmoi and _is_managed(src, managed):
        return Action(
            kind,
            label,
            do=False,
            detail="skipped: chezmoi-managed (run `chezmoi apply` instead)",
            src=src,
            dst=dst,
        )
    return Action(kind, label, do=True, detail=f"→ {dst}", src=src, dst=dst)


def _plan_env(path: Path, *, managed: set[Path] | None) -> Action | None:
    """Plan a BD_TIMEW_* → BD_TRACK_* rewrite, or None if nothing to do."""
    if not path.exists():
        return None
    text = path.read_text()
    count = text.count(_ENV_OLD)
    if count == 0:
        return None
    label = str(path)
    if _is_managed(path, managed):
        return Action(
            "env",
            label,
            do=False,
            detail="skipped: chezmoi-managed (run `chezmoi apply` instead)",
            src=path,
            count=count,
        )
    return Action(
        "env",
        label,
        do=True,
        detail=f"rewrite {count} {_ENV_OLD}* → {_ENV_NEW}*",
        src=path,
        count=count,
    )


def _plan_global(managed: set[Path] | None) -> list[Action]:
    bases = [xdg_home(kind) for kind in ("config", "cache", "state", "data")]
    return [
        _plan_move(base / _OLD, base / _NEW, "dir", managed=managed, check_chezmoi=True)
        for base in bases
    ]


def _plan_project(root: Path, managed: set[Path] | None) -> list[Action]:
    """Plan per-project steps for the repo rooted at ``root``."""
    actions: list[Action] = []
    beads = root / ".beads"

    # Sidecar + session-log dir live inside .beads/ — project-internal data,
    # never chezmoi-managed, so no chezmoi check (per design decision).
    actions.append(
        _plan_move(
            beads / f"{_OLD}.yaml",
            beads / f"{_NEW}.yaml",
            "file",
            managed=managed,
            check_chezmoi=False,
        )
    )
    actions.append(
        _plan_move(beads / _OLD, beads / _NEW, "dir", managed=managed, check_chezmoi=False)
    )

    # Env files are dotfiles a chezmoi setup may own — guarded.
    for name in _ENV_FILES:
        action = _plan_env(root / name, managed=managed)
        if action is not None:
            actions.append(action)
    return actions


def _registered_repo_roots() -> list[Path]:
    """Roots of the registered beads projects, sorted + deduped."""
    from bd_track.registry import load_registry_or_exit

    roots = [p.path for p in load_registry_or_exit().beads_projects()]
    return sorted(set(roots), key=str)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _execute(action: Action, *, backup: bool) -> None:
    if action.kind in ("dir", "file"):
        assert action.src is not None and action.dst is not None
        shutil.move(str(action.src), str(action.dst))
        root_log.info("moved %s → %s", action.src, action.dst)
        return
    if action.kind == "env":
        assert action.src is not None
        path = action.src
        if backup:
            bak = path.parent / (path.name + ".bak")
            shutil.copy2(path, bak)
            root_log.info("backed up %s → %s", path, bak)
        path.write_text(path.read_text().replace(_ENV_OLD, _ENV_NEW))
        root_log.info("rewrote %d %s* → %s* in %s", action.count, _ENV_OLD, _ENV_NEW, path)


def _render_group(title: str, actions: list[Action]) -> None:
    print(f"\n{title}")
    if not actions:
        print("  (nothing to migrate)")
        return
    for a in actions:
        mark = "move " if a.do and a.kind != "env" else "edit " if a.do else "skip "
        print(f"  {mark} {a.label}  {a.detail}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_migrate_rename(
    *,
    project_dir: Path | None = None,
    all_repos: bool = False,
    apply: bool = False,
    backup: bool = True,
) -> None:
    """Rename bd-timew on-disk artifacts to bd-track. Dry-run unless ``apply``."""
    managed = _chezmoi_managed_set()

    # Resolve target repo roots BEFORE any global rename moves repos.yaml.
    if all_repos:
        roots = _registered_repo_roots()
    else:
        try:
            beads = find_beads_dir(project_dir)
            roots = [beads.parent]
        except SystemExit:
            roots = []

    global_actions = _plan_global(managed)
    project_actions = {root: _plan_project(root, managed) for root in roots}

    mode = "APPLYING" if apply else "DRY RUN (pass --apply to migrate)"
    print(f"bd-track migrate rename — {mode}")
    if managed is None:
        print("  note: chezmoi unavailable; home/dotfile targets are NOT guarded")

    _render_group("Global directories:", global_actions)
    for root, actions in project_actions.items():
        _render_group(f"Project {root}:", actions)

    todo = [a for a in global_actions if a.do]
    todo += [a for actions in project_actions.values() for a in actions if a.do]

    if not apply:
        print(f"\n{len(todo)} change(s) planned. Re-run with --apply to perform them.")
        return

    if not todo:
        print("\nNothing to migrate.")
        return

    for action in todo:
        _execute(action, backup=backup)
    print(f"\nMigrated {len(todo)} item(s).")


# ===========================================================================
# migrate import — one-shot timew export → JSONL event log (bd-timew-73v)
# ===========================================================================
#
# Reads a Timewarrior interval export and replays each closed, bead-tagged
# interval as a start+stop event pair in the JSONL log, preserving the
# *historical* start/stop timestamps (written into each event's ``ts``, which
# the aggregator folds directly into interval timing). Open intervals and
# bead-less intervals are skipped (design decision). Idempotent: each interval
# carries a deterministic ``import_key`` so a re-run skips already-imported
# rows. Imported events land in a dedicated ``imported-timew`` session log so
# they are isolated and identifiable.

_IMPORT_SESSION = "imported-timew"


def _timew_ts_to_iso(stamp: str) -> str:
    """Convert a Timewarrior UTC stamp (``20260419T021551Z``) to ISO-8601."""
    parsed = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    return parsed.isoformat()


def _split_bead_tags(tags: list[str]) -> tuple[str | None, list[str]]:
    """Separate the bead (the lone colonless tag) from the key:value billing tags."""
    colonless = [t for t in tags if ":" not in t]
    if not colonless:
        return None, list(tags)
    bead = colonless[0]
    return bead, [t for t in tags if t != bead]


def _import_key(start: str, end: str, bead: str, tags: list[str]) -> str:
    """Deterministic per-interval key for idempotent re-runs (timew ids are unstable)."""
    payload = f"{start}|{end}|{bead}|{','.join(sorted(tags))}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _load_timew(from_file: Path | None) -> list[dict]:
    """Load the timew export from a file, or by shelling out to ``timew export``."""
    if from_file is not None:
        return json.loads(from_file.read_text())
    if shutil.which("timew") is None:
        raise SystemExit("bd-track: timew not found on PATH; pass --from-file <export.json>.")
    res = run(["timew", "export"], check=False, capture=True)
    if res.returncode != 0:
        raise SystemExit("bd-track: `timew export` failed; pass --from-file instead.")
    return json.loads(res.stdout)


def _existing_import_keys(log_path: Path) -> set[str]:
    """Collect import_key values already present in the import session log."""
    if not log_path.exists():
        return set()
    keys: set[str] = set()
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "start" and ev.get("import_key"):
            keys.add(ev["import_key"])
    return keys


def cmd_migrate_import(
    *,
    project_dir: Path | None = None,
    from_file: Path | None = None,
    apply: bool = False,
) -> None:
    """Import timew intervals into the JSONL log. Dry-run unless ``apply``."""
    from ulid import ULID

    from bd_track.events import SCHEMA_VERSION, _session_log_path

    intervals = _load_timew(from_file)
    log_path = _session_log_path(_IMPORT_SESSION, project_dir)
    already = _existing_import_keys(log_path)

    planned: list[dict] = []  # event dicts to append (start then stop, in order)
    n_import = n_open = n_nobead = n_dup = 0

    for iv in intervals:
        start_raw, end_raw = iv.get("start"), iv.get("end")
        if not end_raw:  # open interval — skip (design decision)
            n_open += 1
            continue
        bead, tags = _split_bead_tags(iv.get("tags", []))
        if bead is None:  # no bead tag — skip (design decision)
            n_nobead += 1
            continue
        key = _import_key(start_raw, end_raw, bead, tags)
        if key in already:
            n_dup += 1
            continue
        already.add(key)  # guard against intra-run duplicates too
        n_import += 1

        start_iso, stop_iso = _timew_ts_to_iso(start_raw), _timew_ts_to_iso(end_raw)
        interval_id = str(ULID())
        # eids minted in order so the start sorts before the stop on fold.
        planned.append(
            {
                "v": SCHEMA_VERSION,
                "eid": str(ULID()),
                "event": "start",
                "interval": interval_id,
                "session_id": _IMPORT_SESSION,
                "ts": start_iso,
                "bead": bead,
                "tags": tags,
                "group_id": None,
                "actor": None,
                "role": None,
                "source": "timew",
                "import_key": key,
            }
        )
        planned.append(
            {
                "v": SCHEMA_VERSION,
                "eid": str(ULID()),
                "event": "stop",
                "interval": interval_id,
                "session_id": _IMPORT_SESSION,
                "ts": stop_iso,
            }
        )

    mode = "APPLYING" if apply else "DRY RUN (pass --apply to import)"
    print(f"bd-track migrate import — {mode}")
    print(f"  source: {from_file if from_file else 'timew export'}")
    print(f"  log:    {log_path}")
    print(
        f"  import: {n_import}   skip(open): {n_open}   "
        f"skip(no-bead): {n_nobead}   skip(already-imported): {n_dup}"
    )

    if not apply:
        print(f"\n{n_import} interval(s) would be imported. Re-run with --apply.")
        return
    if not planned:
        print("\nNothing to import.")
        return

    from bd_track.events import append_event

    for event in planned:
        append_event(event, session_id=_IMPORT_SESSION, project_dir=project_dir)
    print(f"\nImported {n_import} interval(s) into {log_path}.")


# ---------------------------------------------------------------------------
# migrate registry — repos.yaml → projects.yaml (bd-timew-d0t.4)
# ---------------------------------------------------------------------------


def _plan_registry() -> tuple[list[tuple[str, dict, str]], list[str], dict, dict[str, Path]]:
    """Plan the move of repos.yaml into the registry.

    Returns ``(writes, skipped, host, cache)``: ``writes`` are ``(name, values, note)`` for
    ``set_project``; ``skipped`` are entries that cannot move (name taken); ``host`` is the
    legacy ``global:`` block to copy when ``config.yaml`` is missing (else ``{}``); ``cache``
    maps remote to clone path, seeded so remote-only projects keep resolving without a scan.
    Registry values win: only keys a project does not already set are written.
    """
    from bd_track import registry as reg
    from bd_track.project import _issue_prefix

    legacy = reg._legacy_projects(reg._read(reg.REPOS_CONFIG), reg.REPOS_CONFIG)
    target = reg.registry_path()
    if target is None or target.resolve() == reg.REPOS_CONFIG.resolve():
        target = reg.DEFAULT_REGISTRY
    current = reg._parse_projects(reg._read(target), target) if target.exists() else {}

    writes: list[tuple[str, dict, str]] = []
    skipped: list[str] = []
    cache: dict[str, Path] = {}
    for old in legacy:
        match = next((p for p in current.values() if p.path == old.path), None) or next(
            (
                p
                for p in current.values()
                if old.remote and p.remote == old.remote and p.path is None
            ),
            None,
        )
        if match is None and old.name in current:
            skipped.append(f"{old.path}: name {old.name!r} is taken by another project")
            continue
        # repos.yaml entries are beads projects by registration; in the registry that is
        # beads.prefix, so a project without one would drop out of cleanup after migrating.
        prefix = None
        if match is None or "prefix" not in match.beads:
            prefix = _issue_prefix(old.path)
            if prefix is None:
                skipped.append(
                    f"{old.path}: could not read its beads prefix (bd config get issue_prefix)"
                )
                continue
        if match is None:
            identity = {"remote": old.remote} if old.remote else {"path": _home_relative(old.path)}
            beads = {"prefix": prefix, **old.beads}
            writes.append((old.name, {**identity, "beads": beads}, "new project"))
        else:
            beads = {k: v for k, v in old.beads.items() if k not in match.beads}
            if prefix:
                beads["prefix"] = prefix
            writes.append(
                (
                    match.name,
                    {"beads": beads},
                    "merge into existing" + ("" if beads else " (nothing to add)"),
                )
            )
        if old.remote:
            cache[old.remote] = old.path

    host: dict = {}
    if not reg.HOST_CONFIG.exists():
        host = dict(reg._read(reg.REPOS_CONFIG).get("global") or {})
    return writes, skipped, host, cache


def _home_relative(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def cmd_migrate_registry(*, apply: bool = False) -> None:
    """Move repos.yaml's projects into the registry, then rename it to ``repos.yaml.migrated``.

    Dry run by default. repos.yaml is renamed only when every entry moved; otherwise it stays,
    and the read-time bridge keeps covering it.
    """
    from bd_track import registry as reg

    if not reg.REPOS_CONFIG.exists():
        print(f"Nothing to migrate: {reg.REPOS_CONFIG} does not exist.")
        return
    try:
        writes, skipped, host, cache = _plan_registry()
    except reg.RegistryError as err:
        raise SystemExit(f"bd-track: {err}") from err
    target = reg.registry_path()
    if target is None or target.resolve() == reg.REPOS_CONFIG.resolve():
        target = reg.DEFAULT_REGISTRY
    renamed = reg.REPOS_CONFIG.with_name(reg.REPOS_CONFIG.name + ".migrated")

    print("APPLYING" if apply else "DRY RUN (pass --apply to migrate)")
    print(f"\nProjects -> {target}")
    for name, values, note in writes:
        print(
            f"  {name}: {note}" + (f" {values}" if values.get("beads") or len(values) > 1 else "")
        )
    for line in skipped:
        print(f"  SKIP {line}")
    if host:
        print(f"\nHost settings -> {reg.HOST_CONFIG} (new file): {host}")
    if cache:
        print(f"\nClone paths -> {reg.PATH_CACHE}:")
        for remote, path in cache.items():
            print(f"  {remote}: {path}")
    if skipped:
        print(
            f"\n{reg.REPOS_CONFIG} stays: {len(skipped)} entry(ies) could not move "
            "(register them under another name with init-project --name, then re-run)."
        )
    elif renamed.exists():
        print(f"\n{reg.REPOS_CONFIG} stays: {renamed} already exists.")
    else:
        print(f"\n{reg.REPOS_CONFIG} -> {renamed}")
    if not apply:
        return

    try:
        for name, values, _note in writes:
            reg.set_project(name, values)
        if host:
            reg.set_host({k: v for k, v in host.items() if k in reg.HOST_KEYS})
    except reg.RegistryError as err:
        raise SystemExit(f"bd-track: {err}; {reg.REPOS_CONFIG} left in place") from err
    if cache:
        reg.seed_path_cache(cache)
    if not skipped and not renamed.exists():
        reg.REPOS_CONFIG.rename(renamed)
        root_log.info("Renamed %s to %s", reg.REPOS_CONFIG, renamed)
