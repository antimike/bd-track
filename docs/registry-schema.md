# Project registry schema

The project registry names each of MH's projects, matches it to its clone, and holds per-project
settings for the tools that read it. Default location: `~/.config/projects.yaml`. This document is
the schema's single source of truth (bead `bd-timew-d0t.1`); the file's header comment should
link here rather than restate it.

Readers today: `ticket` (valid project names, the default project), `mani-discover` (names clones
in `~/mani.yaml`) and, once `bd-timew-d0t.2` lands, bd-track (replacing
`~/.config/bd-track/repos.yaml`).

## Ownership

- **bd-track owns this schema document**: it is the published tool, and the other readers are
  scripts in MH's dotfiles. A schema change is a bd-track change, recorded in the decision log
  below.
- **Each key has one owning tool** (table below). A tool reads any key but writes only its own,
  and preserves every key it does not own, including keys it does not recognise.
- **Who manages the file** (chezmoi, or the `project-data` repo of chezmoi D71) is not part of
  this schema; see [Open](#open).

## Location

bd-track resolves the registry file in this order:

1. `--registry <path>` (commands that read the registry)
2. `$BD_TRACK_REGISTRY`
3. `~/.config/projects.yaml`, if it exists
4. `~/.config/bd-track/repos.yaml` (legacy schema, read-only support; see [Legacy](#legacy-reposyaml))

## Schema

```yaml
projects:
  amontillado:                        # project name: the key everywhere (ticket, mani, --project)
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
| `projects.<name>` | shared | Project name: lowercase, `[a-z0-9][a-z0-9_-]*`. Also the ticket inbox name, `~/.local/state/triage/<name>/`. |
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

The registry is identical on every machine (chezmoi D35). Settings that vary per host live in
bd-track's own config, `~/.config/bd-track/config.yaml`, which chezmoi may render per host:

```yaml
global:
  check_interval: daily       # systemd timer cadence for bd-track cleanup
  idle_stop_hours: 4          # 0 disables the idle-stop timer
  dolt_push: false            # always | never | true | false
  defaults:
    statuses: []
    hooks: []
```

Resolved clone paths (remote → path, `bd-timew-d0t.3`) are cached in
`~/.cache/bd-track/registry-paths.json`. The cache is never authoritative: a cached path whose
`origin` no longer matches is dropped and re-resolved.

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
- **Clone of a name**: `path` if set, else the cache, else a scan of known roots for a clone whose
  `origin` matches (`bd-timew-d0t.3`).

## Writing

bd-track writes the registry in place (`init-project`, `migrate`):

- Round-trip with `ruamel.yaml`, so comments, key order and unknown keys survive. PyYAML would
  strip the header comments.
- Touch only bd-track-owned keys. A new project gets `remote` (from `origin`) or `path`, and
  `beads.prefix`.
- Write atomically: a temp file in the same directory, then `rename()`.
- If chezmoi manages the file (`chezmoi source-path <file>` succeeds), print the follow-up
  `chezmoi re-add <file>`. bd-track does not run chezmoi itself.

## Legacy `repos.yaml`

`~/.config/bd-track/repos.yaml` (a `repos:` list keyed by absolute `path`, plus `global:`) stays
readable until `bd-timew-d0t.4` migrates it:

| `repos.yaml` | Registry |
|---|---|
| `repos[].path` | project name = path basename; `remote` from the clone's `origin`; `path` kept only when there is no remote |
| `repos[].commit_cadence`, `compact_days`, `statuses`, `server` | `beads.` same key |
| `global:` | `~/.config/bd-track/config.yaml` `global:` |

`--project <slug>` keeps matching a legacy path basename.

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
| 2026-10-30 | `global.dolt_push` takes `always`/`never` (force) or `true`/`false` (default, overridden per project). Supersedes `bd-timew-ayn.4`'s "per-project override" wording. |

## Open

- **How the file is managed** (a chezmoi question, not a schema question). Options: plain
  chezmoi-managed (bd-track writes it, then `chezmoi re-add`; today's state); a chezmoi `modify_`
  script seeding required projects (entries added on one host do not reach the others); or moving
  it into the D71 `project-data` repo, synced by `sync-repos` and writable by tools. bd-track
  works with any of these because the location is configurable.
