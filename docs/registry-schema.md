# Project registry schema

The project registry names each project, matches it to its clone, and holds per-project settings
for the tools that read it. Default location: `$XDG_CONFIG_HOME/projects.yaml` (`~/.config/projects.yaml` when unset). This document is the
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
3. `$XDG_CONFIG_HOME/projects.yaml`, if it exists
4. `$XDG_CONFIG_HOME/bd-track/repos.yaml` (legacy schema, read-only support; see [Legacy](#legacy-reposyaml))

`$XDG_CONFIG_HOME` is used when set to an absolute path, else `~/.config` (the XDG rule; a
relative or empty value is ignored). Tools sharing the registry should resolve it the same way.
Elsewhere in this document, `~/.config`, `~/.cache`, `~/.local/state` and `~/.local/share` stand
for `$XDG_CONFIG_HOME`, `$XDG_CACHE_HOME`, `$XDG_STATE_HOME` and `$XDG_DATA_HOME` under that rule.

## Schema

```yaml
projects:
  amontillado:                        # project name: --project <name>, and other tools' key
    remote: antimike/amontillado      # owner/repo shorthand, or any git URL
    path: ~/.local/src/amontillado    # optional
    beads:
      prefix: amo
      commit_cadence: 1d
      compact_days: 7
      statuses: [review]
      dolt_push: false
      dolt_remote_url: git@github.com:me/project-data.git   # optional; else global
      state_repo: true
      server: { enabled: true, pass_path: beads/amontillado, dolt_user: amontillado }
```

| Key | Owner | Meaning |
|---|---|---|
| `projects.<name>` | shared | Project name: lowercase, `[a-z0-9][a-z0-9_-]*`. Other tools may key on it too (for example a ticket inbox directory). |
| `remote` | shared | The clone's `origin`: any URL git accepts (`git@host:o/r.git`, `https://host/o/r.git`, `ssh://…`, a local path), or the `owner/repo` shorthand. Matched by the URL's path, not its host ([Remotes](#remotes)). Preferred over `path`. |
| `path` | shared | `~`-relative path. Needed for a repo without a remote, or to pin one of several clones of a remote (only that clone gets the name). |
| `beads.prefix` | shared | The project's bd issue prefix. Its presence means the project uses beads. |
| `beads.commit_cadence` | bd-track | Minimum interval between `cleanup` passes (commit, compact, gc) for this project (`1d`, `12h`). Default `1d`. |
| `beads.compact_days` | bd-track | `bd compact --days` value for `cleanup`. Default `7`. |
| `beads.statuses` | bd-track | Custom bd statuses `init-project` enables (names only, no `:category`). |
| `beads.dolt_push` | bd-track | `true` / `false`: per-project Dolt push choice, subject to the host setting ([dolt_push](#dolt_push)). Absent: the host default applies. Any other value (including `no`/`off`, which YAML 1.2 reads as strings) is an error, and the project is not pushed. |
| `beads.dolt_remote_url` | bd-track | Git repo this project's Dolt database is pushed to, instead of `global.dolt_remote_url` ([Dolt push](#dolt-push)). |
| `beads.dolt_remote_private` | bd-track | `true` vouches that `dolt_remote_url` is private when bd-track cannot check it (a host other than GitHub). |
| `beads.state_repo` | bd-track | `true` / `false`, or a path: per-project state export choice, subject to the host setting ([state export](#state-export)). A path turns export on and names the state repo for this project instead of `global.state_repo`. Absent: the host default applies. |
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
  long_interval_hours: 8      # warn when stop/pause closes a longer interval; 0 disables
  dolt_push: false            # always | never | true | false
  dolt_remote_url: ~          # git repo for Dolt pushes (none: no default)
  dolt_push_timeout: 120      # seconds before a push is killed
  host_name: laptop           # this host in Dolt push refs (default: short hostname, lowercased)
  state_export: false         # always | never | true | false
  state_repo: ~/state         # path of the state repo's working tree (none: no default repo)
  state_layout: "{project}/issues.jsonl"   # export file inside the state repo
  default_url_prefix: ~       # clone URL prefix for owner/repo remotes, e.g. 'git@github.com:'
  scan_roots: []              # where to look for clones of remote-only projects (none: no scan)
  defaults:
    statuses: []
    hooks: []
```

bd-track writes this file only when asked: `bd-track config set <key> <value>` (one key,
value parsed as YAML, comments kept, validated and rolled back on failure) and
`migrate registry` (only when the file does not exist yet). `init-project` reads
`check_interval` and `idle_stop_hours` for the systemd units it installs and never writes
them; its `--check-interval` / `--idle-stop-hours` flags apply to those units only
(`bd-timew-ekm`). So a config manager can own the file without bd-track drifting it.

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
booleans; so is any non-boolean `beads.dolt_push`. Hosts with unreliable network stacks (WSL2)
should use `never`. A dotfiles manager can render the value per host (for example `never` on
WSL hosts).

### Dolt push

When `dolt_push` resolves to on, `cleanup` pushes the project's Dolt database after the export.
Design: [dev/design-beads-state-repo.md](dev/design-beads-state-repo.md#dolt-push-provisional).

- **Where:** `beads.dolt_remote_url`, else `global.dolt_remote_url`, a git repo that may hold
  many databases. bd-track adds a Dolt remote named `bd-track-backup` to the project's database
  (`CALL dolt_remote('add', '--ref', 'refs/dolt/<host>/<database>', …)`), so each database
  has its own ref per host, and re-points it when the URL or ref changes. `<host>` is
  `global.host_name`, default the short hostname, lowercased; it must be one valid git ref
  component (no `/`, no leading `.`, no spaces or `~^:?*[\`, not ending in `.` or `.lock`). A
  project registered on two hosts keeps both hosts' copies. Use a URL Dolt reads as git
  (`git@host:owner/repo.git`, an `https://…/repo.git`, or a local path). The repo needs at least
  one branch before the first push.
- **Modes:** embedded databases are reached with `dolt sql` in their directory, server-mode
  databases (a shared server, or the global database) with `bd sql`. The ref is named after the
  host and the **database**, not the project: projects sharing a database push it once per cleanup run (the
  first such project with push on decides the URL). A server that is down skips the push.
- **Guard:** bd-track refuses a URL that names one of the code repo's own git remotes. It then
  resolves the URL's real host: git's `url.<base>.insteadOf` rewrites (`git ls-remote
  --get-url`), then, for ssh, the host ssh really connects to (`ssh -G`, so aliases, `Include`
  and `Match` count). On `github.com` the repo is checked through the unauthenticated GitHub
  API: a public repo is refused, a private or missing one (404) is allowed, and any other answer
  skips the push for this run. Local paths and `file://` are allowed. Any other host (including
  GitHub Enterprise) is refused unless the project sets `beads.dolt_remote_private: true`.
- **Push:** `bd dolt push --remote bd-track-backup --no-adopt` (bd never adopts the git origin
  as a remote), killed with its child processes after `global.dolt_push_timeout` seconds.
  Failures and timeouts are logged and retried on the next cleanup; they never stop cleanup or
  other projects. Each project's bd `no-push` setting stays on: only cleanup pushes.
- **After compaction:** `bd compact` rewrites history, so the next plain push is rejected as
  non-fast-forward. bd-track then pushes again with `--force` (the ref is its own and the remote
  passed the guard), within what is left of the timeout. Any other failure is not forced.
- **Restore:** pick the host's copy (`git ls-remote <url> 'refs/dolt/*'` lists them), then
  `dolt clone --ref refs/dolt/<host>/<database> <url> <dir>`; for an embedded project,
  `bd init --prefix <prefix>` in a fresh clone, then replace `.beads/embeddeddolt/<database>`
  with the cloned directory.
- **Server mode:** the push runs inside the Dolt server (`CALL DOLT_PUSH('bd-track-backup',
  'main')`). Verified against a scratch `dolt sql-server` (`bd-timew-ayn.7`):
  - When bd-track kills a timed-out `bd dolt push`, the server cancels the query on disconnect
    and leaves no git or ssh process behind. If the query is still listed, bd-track kills it
    (`information_schema.processlist`, `KILL QUERY`), which also works.
  - Git credentials come from the **server's** environment, not the caller's. For ssh, either
    the server process has `SSH_AUTH_SOCK`, or the ssh config names the agent itself
    (`IdentityAgent`, e.g. a gpg-agent socket) or a key file. A server started by a systemd
    unit without either cannot authenticate; the push fails and the error is logged. A hardware
    key that is not plugged in fails the same way.
  - Projects sharing a database (`bd init --server --database <db>`) push it once per run,
    under `refs/dolt/<host>/<db>`.

- **Migrating from `refs/dolt/<database>`** (bd-track before `bd-timew-q9o.2`): the next
  cleanup re-points the `bd-track-backup` remote to the host ref and pushes there. The old
  `refs/dolt/<database>` ref stays in the backup repo, frozen at its last push; it still
  restores. Once each host has pushed its own ref, delete it to avoid confusion: `git push <url>
  --delete refs/dolt/<database>` (or `git update-ref -d` in a local bare repo). Delete it
  *before* the first push if a host name equals a database name: git cannot hold both
  `refs/dolt/<name>` and `refs/dolt/<name>/<database>`.

### Push timing log

Every attempted push (not one skipped by the switch, the guard or a configuration error) appends
one JSON object per line to `~/.local/state/bd-track/dolt-push.jsonl`. It exists to judge whether
automated pushes are reliable on a host (for example, two weeks of data before deciding whether
pushes are safe outside a session). bd-track never trims it; delete it to start over.

| Field | Meaning |
|---|---|
| `v` | Record version, `1`. Bumped only when a field changes meaning; new fields may appear without it, so readers should select fields by name |
| `timestamp` | UTC, ISO 8601, when the push finished |
| `host` | Machine hostname as the OS reports it, not `global.host_name` (useful if logs from several hosts are compared) |
| `project` | Registry name of the project whose cleanup ran the push |
| `database` | Dolt database pushed (the ref is `refs/dolt/<host_name>/<database>`) |
| `mode` | `"embedded"` or `"server"` |
| `remote` | The configured `dolt_remote_url` |
| `db_bytes` | On-disk size of the embedded database in bytes (a proxy: the push sends only new chunks); `null` in server mode |
| `duration_s` | Wall-clock seconds (number), including a timed-out push's full wait |
| `result` | `"ok"`, `"failed"`, or `"timeout"` (killed after `dolt_push_timeout`) |
| `forced` | `true` when a non-fast-forward rejection (after a compaction) was retried with `--force`; `duration_s` covers both attempts |
| `output` | Last 5 lines of `bd dolt push` output, verbatim, at most 1000 characters |

Each line is written with one append-mode `write()` and stays far below 4096 bytes, so
concurrent cleanups never interleave records. Reading it:

```sh
LOG=~/.local/state/bd-track/dolt-push.jsonl
# as a table
jq -r '[.timestamp, .project, .duration_s, .result] | @tsv' "$LOG" | column -t
# count of each result
jq -r .result "$LOG" | sort | uniq -c
# the five slowest pushes, and every timeout with its output
jq -s 'sort_by(-.duration_s) | .[:5][] | {timestamp, project, duration_s, result}' "$LOG"
jq 'select(.result == "timeout")' "$LOG"
```

Any `timeout` record is a stall that the timeout contained; repeated ones point at the network
path (on WSL2, typically MTU or offload problems). Errors writing the log are reported in
`~/.cache/bd-track/cleanup.log` and never stop cleanup.

### State export

`cleanup` can `bd export` each project's issues into the working tree of a **state repo**, a
private git repo that another tool commits and pushes; bd-track never runs git in it. Design:
[dev/design-beads-state-repo.md](dev/design-beads-state-repo.md).

- `global.state_export` is a switch with the same four values and semantics as
  [`dolt_push`](#dolt_push), overridden per project by `beads.state_repo`. A path in
  `beads.state_repo` counts as `true`.
- The export goes to `<repo>/<state_layout>`, where `<repo>` is the project's
  `beads.state_repo` path, else `global.state_repo`, and `state_layout` is a relative path with
  the field `{project}` (the registry name). Default `{project}/issues.jsonl`. For example, a
  repo laid out by project with a `beads/` directory each uses
  `projects/{project}/beads/issues.jsonl`.
- The export holds the project's issues and its `bd remember` memories (`bd export
  --include-memories`); `bd import` of the file restores both.
- The export also copies the project's session event logs (`*.jsonl`, the billing data) into
  `sessions/` beside the issues file: `<beads>/bd-track/sessions/` and the
  `~/.local/share/bd-track/<project-id>/sessions/` fallback dir. They are gitignored and outside
  the Dolt database, so neither `bd export` nor the Dolt push carries them. When the layout's
  directory is shared by all projects (`{project}` only in the file name, e.g.
  `beads/{project}.jsonl`), they go to `sessions/<project>/` there instead. The logs are
  append-only, so only new or grown files are copied (each atomically); a backup copy larger
  than its source is kept, with a warning.
- The repo directory must exist (bd-track does not create repos); the project's subdirectory
  is created. The file is replaced atomically, and a failed export keeps the previous file.
- Export on with no repo known, a missing repo, or a failed export is logged; it never stops the
  rest of `cleanup` or other projects.
- The default (`false`) exports nothing: with client or employer data in some projects, a
  project should reach a personal repo only by choice.

## Remotes

`remote` is a string in one of two forms (`bd-timew-q9o.6`):

- **A git URL or path**, in any form git accepts. bd-track clones from it as written (`bd-track
  restore`), so it also picks the protocol: write `https://…` for https, `git@…` for ssh. To
  rewrite URLs for every project on a host, use git's `url.<base>.insteadOf`.
- **The `owner/repo` shorthand** (exactly two components, no scheme, no `:`). Its clone URL is
  the host's `global.default_url_prefix` followed by `owner/repo`; for example, with the prefix
  `git@me.github.com:` (an ssh host alias), `antimike/notes` clones from
  `git@me.github.com:antimike/notes`. With no prefix set the shorthand still matches clones, but
  bd-track has no URL to clone it from. The prefix must end in `:` or `/`.

**Matching.** A project matches a clone when their paths agree: the URL's path, lowercased,
without `.git` and edge slashes. The host is ignored, so ssh host aliases match the canonical
host (`git@me.github.com:o/r` and `https://github.com/o/r` are both `o/r`). Nested paths keep
every component: `group/sub/repo` does not match a shorthand `sub/repo`. Two hosts serving the
same path match the same project; pin one clone with `path:` if that happens. A `remote` that is
neither form is ignored with a warning.

## Resolving a project

- **By name** (`--project <name>`): the key under `projects:`. A registered name never falls
  back to a same-named directory: with no known clone it is an error.
- **By path** (`--project <dir>`): a registered `path`, else any existing directory, registered
  or not (`bd-timew-aji`).
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
readable until `bd-track migrate registry --apply` moves it (`bd-timew-d0t.4`; a dry run by
default):

| `repos.yaml` | Registry |
|---|---|
| `repos[].path` | project name = path basename (or the existing project with that path, or the path-less one whose `remote` matches the clone's `origin`); `remote` from the clone's `origin`; `path` kept only when there is no remote. The clone's path goes into the path cache, so it resolves without a scan. |
| (registration) | `beads.prefix` read from the clone (`bd config get issue_prefix`); an entry whose prefix cannot be read is skipped |
| `repos[].commit_cadence`, `compact_days`, `statuses`, `server` | `beads.` same key |
| `global:` | `~/.config/bd-track/config.yaml` `global:`, only when that file does not exist yet |

Registry values win: a project that already sets a key keeps it. When every entry has moved,
`repos.yaml` is renamed to `repos.yaml.migrated` (rename it back to undo); if any entry was
skipped (name taken, no prefix) it stays, and the bridge below keeps covering it. Since
`bd-timew-d0t.4`, `init-project` writes the registry, never `repos.yaml` (nor, since
`bd-timew-ekm`, `config.yaml`); `--name` names a new project when its
basename is taken.

`--project <slug>` keeps matching a legacy path basename.

**Bridge while both files exist.** Until the migration, a registry may list a project by
`remote` only while `repos.yaml` still knows its clone. So when the registry in use is not `repos.yaml` itself and `repos.yaml` also
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
| 2026-10-30 | `migrate registry --apply` renames `repos.yaml` to `repos.yaml.migrated` once every entry has moved; it copies `global:` to `config.yaml` only when that file is missing; `init-project` records its host keys in `config.yaml` (round-trip). Migrated projects get `beads.prefix` from the clone, and their paths are seeded into the path cache (`bd-timew-d0t.4`, MH). |
| 2026-10-30 | Scan roots come from `global.scan_roots` or `$BD_TRACK_SCAN_ROOTS`, with no default (no scan unless configured); two or more clones of a remote resolve to none, with a warning; every beads project with a known clone is a cleanup target (`bd-timew-d0t.3`, MH). |
| 2026-10-30 | `global.dolt_push` takes `always`/`never` (force) or `true`/`false` (default, overridden per project). Supersedes `bd-timew-ayn.4`'s "per-project override" wording. |
| 2026-11-01 | State export: `global.state_export` is a four-valued switch like `dolt_push`; `beads.state_repo` is `true`/`false` or a path (on, and that repo); `global.state_repo` is the default repo and `global.state_layout` a `{project}` template, default `{project}/issues.jsonl` (`bd-timew-ayn.2`, MH). |
| 2026-11-02 | Dolt push: bd-track provisions a `bd-track-backup` remote through SQL from `dolt_remote_url` (project, else host) under `refs/dolt/<database>`, in embedded and server modes, one push per database per run; the guard resolves the real host (`git ls-remote --get-url`, `ssh -G`), refuses code remotes and public GitHub repos (API check), and other hosts unless `beads.dolt_remote_private: true`; `global.dolt_push_timeout` (default 120 s) kills a stalled push, server-side too (`bd-timew-ayn.3`, MH). |
| 2026-11-02 | The state export includes `bd remember` memories (`--include-memories`), with no setting: a backup that drops them loses data that exists only in `.beads/` (`bd-timew-ayn.2`, MH). |
| 2026-11-02 | Dolt push refs are `refs/dolt/<host>/<database>`, host = `global.host_name` (default the short hostname): a project on two hosts pushed both to one ref, and the forced retry let the last host replace the other's copy. Ref per host, not branch per host: a Dolt remote keeps all branches in one store under one git ref, and `reset-data` rebuilds it from local HEAD, wiping other hosts' branches (`bd-timew-q9o.2`, MH). |
| 2026-11-02 | The state export copies session event logs to `sessions/` beside the issues file (`sessions/<project>/` for a flat layout), new or grown files only, with no setting: they are billing data outside every other backup (`bd-timew-q9o.1`). |
| 2026-11-03 | `remote` accepts any git URL or the `owner/repo` shorthand, still a string (no `remote.github`/`remote.ssh` subdict: the URL carries the protocol, `insteadOf` covers host-wide rewrites); `global.default_url_prefix` (per host, no default) makes a shorthand cloneable; matching uses the URL path without the host, so ssh aliases match and nested paths stay distinct (`bd-timew-q9o.6`, MH). |
| 2026-11-03 | `init-project` never writes host settings: it reads `check_interval` and `idle_stop_hours` from `config.yaml` for the units it installs (flags override for those units only), and `bd-track config set <key> <value>` writes host settings explicitly. Supersedes the 2026-10-30 "`init-project` records its host keys in `config.yaml`" (`bd-timew-ekm`, MH). |
| 2026-11-03 | One project flag: `--project <name-or-path>` on every single-project command (`start`/`stop`/`switch`/`status`/`resolve` included), accepted before or after the subcommand; a registry name resolves through the registry, an existing directory is used as given. `--project-dir` stays one release as a hidden alias with a deprecation warning; `--global` is unchanged. `bd` runs in the resolved root so a claim lands in the same project as its interval (`bd-timew-aji`, MH). |
| 2026-11-05 | `--project-dir` removed in 0.10.0 as the 0.9.0 deprecation promised; `--project` is the only project flag (`bd-timew-56q`, MH). |
