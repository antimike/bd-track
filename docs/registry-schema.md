# Project registry schema

The project registry names each project, matches it to its clone, and holds per-project settings
for the tools that read it. Default location: `~/.config/projects.yaml`. This document is the
schema's single source of truth (bead `bd-timew-d0t.1`); a header comment in the file should link
here rather than restate it.

bd-track reads it once `bd-timew-d0t.2` lands, replacing `~/.config/bd-track/repos.yaml`. Other
tools may read the same file (in MH's setup, the `ticket` command and `mani-discover`).

## Ownership

- **bd-track owns this schema document**: it is the published tool that reads it. A schema change
  is a bd-track change, recorded in the decision log below.
- **Each key has one owning tool** (table below). A tool reads any key but writes only its own,
  and preserves every key it does not own, including keys it does not recognise.
- **How the file is created and shared is up to the user**: kept by hand, generated or seeded by a
  dotfiles manager, or synced in a repo, in whole or in part. Some projects may be registered on
  every machine and others on one host only. bd-track assumes none of this; see
  [Writing](#writing) for its one requirement.

## Location

bd-track resolves the registry file in this order:

1. `--registry <path>` (commands that read the registry)
2. `$BD_TRACK_REGISTRY`
3. `~/.config/projects.yaml`, if it exists
4. `~/.config/bd-track/repos.yaml` (legacy schema, read-only support; see [Legacy](#legacy-reposyaml))

## Schema

```yaml
projects:
  amontillado:                        # project name: --project <name>, and other tools' key
    remote: antimike/amontillado      # owner/repo of the clone's origin
    path: ~/.local/src/amontillado    # optional
    beads:
      prefix: amo
      commit_cadence: 1d
      compact_days: 7
      statuses: [review]
      dolt_push: false
      server: { enabled: true, pass_path: beads/amontillado, dolt_user: amontillado }
```

| Key | Owner | Meaning |
|---|---|---|
| `projects.<name>` | shared | Project name: lowercase, `[a-z0-9][a-z0-9_-]*`. Other tools may key on it too (for example a ticket inbox directory). |
| `remote` | shared | `owner/repo` of the clone's `origin`. Matched against any host or URL form (`git@github.com:o/r.git`, `https://…/o/r`). Preferred over `path`. |
| `path` | shared | `~`-relative path. Needed for a repo without a remote, or to pin one of several clones of a remote (only that clone gets the name). |
| `beads.prefix` | shared | The project's bd issue prefix. Its presence means the project uses beads. |
| `beads.commit_cadence` | bd-track | Minimum interval between `cleanup` passes (commit, compact, gc) for this project (`1d`, `12h`). Default `1d`. |
| `beads.compact_days` | bd-track | `bd compact --days` value for `cleanup`. Default `7`. |
| `beads.statuses` | bd-track | Custom bd statuses `init-project` enables (names only, no `:category`). |
| `beads.dolt_push` | bd-track | `true` / `false`: per-project Dolt push choice, subject to the host setting ([dolt_push](#dolt_push)). Absent: the host default applies. |
| `beads.server` | bd-track | Shared Dolt server wiring written by `init-project --server`: `enabled`, `pass_path`, `dolt_user`. |

Other top-level project keys (for example a future `triage:` flag) are added by the tool that owns
them, with a row in this table.

## Host settings stay out of the registry

The registry describes projects, and an entry may be shared by several machines. Settings that
vary per host live in bd-track's own config, `~/.config/bd-track/config.yaml`, which can differ
on each host:

```yaml
global:
  check_interval: daily       # systemd timer cadence for bd-track cleanup
  idle_stop_hours: 4          # 0 disables the idle-stop timer
  dolt_push: false            # always | never | true | false
  scan_roots: []              # where to look for clones of remote-only projects (none: no scan)
  defaults:
    statuses: []
    hooks: []
```

Resolved clone paths (remote → path, `bd-timew-d0t.3`) are cached in
`~/.cache/bd-track/registry-paths.json`. The cache is never authoritative: a cached path whose
`origin` no longer matches is dropped and re-resolved.

### `scan_roots`

Directories searched (up to 5 levels deep) for a clone of a project that has a `remote` but no
`path`. `$BD_TRACK_SCAN_ROOTS` (`:`-separated) overrides it. There is no default: with neither
set, no scan runs and such a project resolves only through the cache. A clone is a directory with
a `.git` directory, so worktrees and submodules are not counted. A full scan runs at most once a
day while projects stay unresolved; `--project <name>` for a project with no known clone forces
one.

### `dolt_push`

The global value either sets a default or forces a value; YAML types keep the two apart.

| `global.dolt_push` | Effect |
|---|---|
| `never` | No project pushes on this host; `beads.dolt_push` is ignored. |
| `always` | Every beads project pushes; `beads.dolt_push` is ignored. |
| `false` (default) | Default off; a project with `beads.dolt_push: true` pushes. |
| `true` | Default on; a project with `beads.dolt_push: false` does not push. |

Any other value is an error. `on`, `off`, `yes` and `no` are rejected rather than read as YAML 1.1
booleans. Hosts with unreliable network stacks (WSL2) should use `never`.

## Resolving a project

- **By name** (`--project <name>`): the key under `projects:`.
- **By directory** (the current repo's project): `path` if set and equal, else the clone's `origin`
  matched against `remote`. A path-pinned project wins over a remote match for the same clone.
- **Clone of a name**: `path` if set, else the cache, else a scan of the
  [scan roots](#scan_roots) for a clone whose `origin` matches (`bd-timew-d0t.3`). A clone already
  pinned by another project's `path` is not a candidate. Two or more matching clones leave the
  path unknown, with a warning naming them: pin one with `path:`.

## Writing

bd-track writes the registry in place (`init-project`, `migrate`):

- Round-trip with `ruamel.yaml`, so comments, key order and unknown keys survive. PyYAML would
  strip the header comments. (ruamel does not keep the inner spaces of flow maps:
  `{ prefix: amo }` is written back as `{prefix: amo}`.)
- Touch only bd-track-owned keys. A new project gets `remote` (from `origin`) or `path`, and
  `beads.prefix`.
- Write atomically: a temp file in the same directory, then `rename()`.
- bd-track does not detect or call whatever manages the file. **Requirement:** a manager that
  generates or seeds the file must merge into it rather than overwrite it, or bd-track's writes
  are lost on its next run. For example, a chezmoi `modify_` script that deep-merges a seed of
  identity keys (`remote`, `path`, `beads.prefix`) and leaves other keys alone.

## Legacy `repos.yaml`

`~/.config/bd-track/repos.yaml` (a `repos:` list keyed by absolute `path`, plus `global:`) stays
readable until `bd-timew-d0t.4` migrates it:

| `repos.yaml` | Registry |
|---|---|
| `repos[].path` | project name = path basename; `remote` from the clone's `origin`; `path` kept only when there is no remote |
| `repos[].commit_cadence`, `compact_days`, `statuses`, `server` | `beads.` same key |
| `global:` | `~/.config/bd-track/config.yaml` `global:` |

`--project <slug>` keeps matching a legacy path basename.

**Bridge while both files exist.** Until the migration, `init-project` still writes `repos.yaml`,
and a registry may list a project by `remote` only, with no way yet to find its clone
(`bd-timew-d0t.3`). So when the registry in use is not `repos.yaml` itself and `repos.yaml` also
exists, bd-track reads both. Each `repos.yaml` entry completes the registry project with the same
`path`, else the path-less project whose `remote` matches the clone's `origin`: it supplies the
`path` and any `beads.` key the registry does not set (the registry wins). An entry that matches
no project is added under its basename, unless that name is taken (then it is skipped with a
warning). Host settings come from `config.yaml` when it exists, else `repos.yaml` `global:`.

## A shared package (deferred)

`ticket` and bd-track will each parse this file. When the drift costs more than a release would,
the schema, a loader and project resolution move into one small package, and `ticket` ships as its
CLI (ticket addendum, MH agreed 2026-10-29). This document is the specification that package would
implement.

## Decision log

| Date | Decision |
|---|---|
| 2026-10-30 | bd-track owns this schema document. Keys have one owning tool; unknown keys are preserved. |
| 2026-10-30 | Host-specific settings (`global:`, path cache) stay in bd-track's own config, not the registry. |
| 2026-10-30 | bd-track writes the registry in place with `ruamel.yaml` (new dependency), atomically. |
| 2026-10-30 | bd-track is agnostic about how the file is managed or shared (no dotfiles-manager detection or hints); managers must merge, not overwrite. Projects may be registered on all hosts or only some. |
| 2026-10-30 | While `repos.yaml` still exists, bd-track reads it alongside the registry and lets it fill in paths and bd-track settings ([bridge](#legacy-reposyaml)); `bd-timew-d0t.4`'s migration retires it. Chosen over switching outright (cleanup would lose path-less projects) or deferring the switch (`bd-timew-d0t.2`, MH). |
| 2026-10-30 | Scan roots come from `global.scan_roots` or `$BD_TRACK_SCAN_ROOTS`, with no default (no scan unless configured); two or more clones of a remote resolve to none, with a warning; every beads project with a known clone is a cleanup target (`bd-timew-d0t.3`, MH). |
| 2026-10-30 | `global.dolt_push` takes `always`/`never` (force) or `true`/`false` (default, overridden per project). Supersedes `bd-timew-ayn.4`'s "per-project override" wording. |
