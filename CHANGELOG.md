# Changelog

All notable changes to bd-track (formerly `bd-timew`) are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **One project flag** (`bd-timew-aji`): `--project <name-or-path>` works on every
  command that acts on one project, `start`, `stop`, `switch`, `status` and `resolve`
  included (they used to ignore it and read the cwd), before or after the subcommand.
  A registry name resolves through the registry; an existing directory is used as given,
  registered or not. `start` and `stop` run `bd` in that project, so the claim and the
  post-stop queue sweep land where the interval does; `queue clean`, `prune` and
  `--titles` look beads up there too. `--project` given to `init-project`, `servers`,
  `server-stop` or `idle-stop`, or together with `--global`, is now an error.

- **`init-project` no longer writes host settings** (`bd-timew-ekm`): it used to rewrite
  `idle_stop_hours` (from a prompt that defaulted to `4`, not the file's value) and
  `check_interval` in `~/.config/bd-track/config.yaml` on every run, drifting a file a config
  manager may own. It now reads both for the systemd units it installs;
  `--check-interval` / `--idle-stop-hours` apply to those units only, and the idle-hours
  prompt is gone.

### Added

- **`bd-track config set <key> <value>`** (`bd-timew-ekm`): writes one host setting into
  `config.yaml` `global:`. The value is parsed as YAML, comments are kept, and a value that
  fails validation is rolled back. Refused while host settings still come from a legacy
  `repos.yaml`.

### Deprecated

- **`--project-dir`**: use `--project`. It still works for one release, with a warning.

## [0.8.1] - 2026-11-04

Pause and resume tracking intervals, for idle and sleep hooks.

### Added

- **`pause` / `resume`** (`bd-timew-11e`): `pause` stops open intervals with `paused: true`
  in the stop event; `resume` reopens each with a new interval on the same session, bead,
  tags and provenance (`resumed_from` on the start event). Default scope is this session;
  `--all` covers every session and `--global` every registered project, `<issue-id>`
  arguments narrow it, `--dry-run` writes nothing. Both exit 0 with a note when nothing
  matches, so idle and sleep hooks can call them blindly. A pause is cleared by a later
  `start` in that session or by `stop` (a `paused: false` correction).
- **`active --json`** lists open and paused intervals (state, project, session, bead, title,
  interval, since, tags); the table view also shows paused intervals, and `status` shows
  this session's.

## [0.8.0] - 2026-11-03

Off-site backup for beads data: `cleanup` can export issues, memories and session logs to a
state repo and push the Dolt database to a private git remote. Keys and semantics:
docs/registry-schema.md ("State export", "Dolt push").

### Breaking

- **The host config is validated when loaded.** In `~/.config/bd-track/config.yaml`,
  `global.host_name` must be one git ref component, `global.default_url_prefix` must end in
  `:` or `/`, `global.dolt_push` / `global.state_export` must be `always`, `never`, `true` or
  `false`, and `global.dolt_push_timeout` must be a positive number. A host config that
  fails makes `init-project` exit with an error before changing anything, makes registry
  commands that scan for clones exit with an error, and makes `cleanup` skip its export and
  push phases (compact and gc still run). In the registry, a `beads.dolt_push` or
  `beads.dolt_remote_private` that is not a boolean (YAML 1.2 reads `no` / `off` as
  strings) skips that project's push with an error.
- **`queue push` / `unshift` reject ids that are not bead-shaped** (see Changed).

### Added

- **State export in `cleanup`** (`bd-timew-ayn`): after gc, `bd export` writes the project's
  issues and `bd remember` memories into a state repo (`global.state_repo`,
  `global.state_layout`), replaced atomically, so `bd import` of the one file restores both.
  `global.state_export` (`always` / `never` / `true` / `false`) with a per-project
  `beads.state_repo` (`true` / `false` or a path) turns it on. bd-track runs no git in the
  state repo; failures are logged and never stop cleanup.
- **Session event logs in the state export** (`bd-timew-q9o.1`): `cleanup`'s state export
  copies each project's session logs (billing data, gitignored and outside the Dolt
  database) into `sessions/` beside the issues file, new or grown files only.
- **Dolt push in `cleanup`** (`bd-timew-ayn`): pushes the project's Dolt database to a
  private git repo (`global.dolt_remote_url` or `beads.dolt_remote_url`) under
  `refs/dolt/<host>/<database>`, when `global.dolt_push` / `beads.dolt_push` turn it on.
  - Refuses the project's own code remotes, public GitHub repos and hosts it cannot check
    (unless `beads.dolt_remote_private: true`), resolving the real host through git
    `insteadOf` and `ssh -G`.
  - Runs `bd dolt push --no-adopt` in its own process group, killed after
    `global.dolt_push_timeout` seconds (default 120); a failed push is retried next run.
  - Retries a non-fast-forward rejection (after `bd compact` rewrites history) with
    `--force` within the remaining timeout.
  - Works in embedded and server mode; a database shared by several projects is pushed
    once per run, and a remote whose URL moved to another host is re-pointed.
  - Logs each attempt as one JSON record in `~/.local/state/bd-track/dolt-push.jsonl`
    (duration, size, result, the first real error line).
- **`global.host_name`** (`bd-timew-q9o.2`): names this host in Dolt push refs (default: the
  short hostname, lowercased), so a project registered on two hosts keeps one backup per
  host.
- **Full git URLs in the registry's `remote:`** (`bd-timew-q9o.6`): any URL git accepts, or
  the `owner/repo` shorthand; `global.default_url_prefix` (per host) gives the shorthand a
  clone URL. Matching uses the URL path without the host.

### Changed

- **Dolt push refs are per host** (`bd-timew-q9o.2`): checkouts that ran the pre-release
  push under `refs/dolt/<database>` have their remotes re-pointed to
  `refs/dolt/<host>/<database>` on the next cleanup; the old refs can be deleted afterwards
  (docs/registry-schema.md, "Dolt push").
- **`queue push` / `unshift` validate bead IDs** (`bd-timew-uph`): each id must look like a
  bead id (`prefix-hash`, optional `.N` children, no whitespace), or the command exits
  non-zero naming the bad argument and leaves the queue unchanged. Several ids passed as one
  quoted argument (zsh does not word-split `"$T"`) used to be stored as a single entry.
- **`init-project` sets `backup.git-push: false`** (`bd-timew-4fv`): bd's auto-backup
  stays local instead of pushing to the project's git remote. bd 1.3 already defaults it
  off; the explicit key covers older bd and shows the choice in `.beads/config.yaml`.

### Fixed

- **`init-project` with a bad host config** (`bd-timew-hpw`): exits with `bd-track: <error>`
  before `bd init` or any file write, instead of a traceback after them.

## [0.7.0] - 2026-10-30

### Added

- **`bd-track migrate registry`** (`bd-timew-d0t.4`): moves `repos.yaml`'s projects
  into `~/.config/projects.yaml` (registry values win; `beads.prefix` read from each
  clone; clone paths seeded into the path cache), copies its `global:` block into
  `~/.config/bd-track/config.yaml` when that file is missing, and renames it to
  `repos.yaml.migrated` once every entry has moved. Dry-run by default.
- Registry projects with a `remote` but no `path` are found on disk by scanning the
  host's `global.scan_roots` (or `$BD_TRACK_SCAN_ROOTS`) for a clone whose `origin`
  matches; results are cached in `~/.cache/bd-track/registry-paths.json`. No roots, no
  scan. Two or more clones of a remote are reported and left unresolved. Such projects
  become cleanup, `--project` and `--global` targets (`bd-timew-d0t.3`).

### Changed

- `init-project` registers the project in the registry (`remote` from `origin`, else
  `path`; `beads.prefix` and its settings) instead of `repos.yaml`, and records
  `idle_stop_hours`/`check_interval` in `~/.config/bd-track/config.yaml`. New `--name`
  for a project whose basename is taken (`bd-timew-d0t.4`).
- **Upgrading:** run `bd-track migrate registry` (dry run), then `--apply`, once per
  host that still has `~/.config/bd-track/repos.yaml`; until then it keeps being read
  alongside the registry.

## [0.6.0] - 2026-10-30

### Added

- **`bd-track triage`** (`bd-timew-4is`): the beads layer over a project's ticket
  inbox (`<inbox>/triage/*.md` files and `triage/inbox.md` quick notes; see
  `docs/triage.md`). Parsing is lenient: every frontmatter key is optional and
  free-form tickets are valid.
  - `triage list [--json]`: open tickets as a table, or JSON for scripts and agents.
  - `triage resolve <ticket> (--bead | --reject | --duplicate | --done)`: record the
    decision (`status`, `resolved`, `outcome`) and move the ticket to `done/`;
    `--bead` creates a bead (label `triage`, type and priority mapped from the
    ticket, overridable with `--title`/`--description`/`--type`/`--priority`).
  - `triage ask <ticket> <question>`: append a `## Triage <date>` section; the ticket
    stays open (a quick note is promoted to its own file first).
  - Bare `bd-track triage`: an interactive loop over the open tickets.
  - `--dry-run` on all of them; the inbox is `--inbox`, else
    `$BD_TRACK_INBOX_ROOT/<project>`, else `$XDG_STATE_HOME/triage/<project>`, where
    `<project>` is the directory's registry name.
- **Shared project registry** (`bd-timew-d0t.2`, `docs/registry-schema.md`): bd-track
  reads `~/.config/projects.yaml` (or `--registry`, `$BD_TRACK_REGISTRY`), falling back
  to the legacy `~/.config/bd-track/repos.yaml`. While `repos.yaml` still exists, its
  entries fill in paths and bd-track settings for registry projects with a matching
  remote. Host settings come from `~/.config/bd-track/config.yaml`, else `repos.yaml`
  `global:`. Cleanup, `servers`, `server-stop`, `idle-stop`, `migrate`, `--project` and
  `--global` read the registry; `init-project` still writes `repos.yaml` for now.
- New dependency: `ruamel.yaml` (round-trip registry writes). An existing editable
  install needs `just install-dev` to pick it up.

### Fixed

- The automatic queue sweep after `bd-track stop` removes only closed beads, so
  deferred beads (e.g. parked in a `<scope>.operator` scope) stay queued; `queue clean`
  and `queue prune` still treat deferred as stale (`bd-timew-eue`).
- `bd-track queue prune` no longer proposes `add-before` for a queued bead's
  parent epic; only blocking dependency edges imply ordering (`bd-timew-ctp`).
- Repository links in `pyproject.toml`, the README install commands and this
  changelog point at `antimike/bd-track` (they named an old account and the
  pre-rename `bd-timew` repo).

## [0.5.1] - 2026-10-14

### Fixed

- `just install`/`install-dev` now uninstall any existing pipx venv first;
  `pipx install --force` failed on re-run when pipx used uv to create venvs.
- `install.sh` no longer runs `cargo install beads` (wrong crate) and defaults
  to the `antimike/bd-track` repo.

### Added

- **`bd-track migrate rename`** (`bd-timew-jy9`) — one-way, in-place rename of
  legacy on-disk artifacts so the read-fallback shims stop firing: the
  config/cache/state dirs, the `.beads` sidecar, the `<beads>/bd-timew` session
  logs, and `BD_TIMEW_*` → `BD_TRACK_*` env rewrites. Dry-run by default
  (`--apply` to write); `--all-repos` sweeps `repos.yaml`; `--no-backup`;
  chezmoi-guarded on home/dotfile targets; idempotent.
- **`bd-track migrate import`** (`bd-timew-73v`) — replays `timew export` into the
  JSONL log, preserving historical timestamps, skipping open and bead-less
  intervals, idempotent by content-hash `import_key`. `--from-file` imports from a
  saved export instead of invoking `timew`. Dry-run by default (`--apply` to write).

### Docs

- README rewritten for the JSONL backend and `bd-track` rename; CHANGELOG and CLI
  help brought current with the shipped `migrate` subcommands (`bd-timew-7bh`).

## [0.5.0] - 2026-06-03

The cutover: the CLI is re-wired off Timewarrior onto the JSONL event log, and
the package is renamed **`bd-timew` → `bd-track`** (epic `bd-timew-nfr`,
bead `bd-timew-9hn`). This resolves the originating bug — concurrent sessions
clobbering each other's single global timew interval — structurally: there is
no shared active interval, and a session can only ever close an interval ULID
in its own per-session log.

### Changed

- **BREAKING — package + executable renamed to `bd-track`.** The Python module
  is `bd_track`; the command is `bd-track`. A deprecated **`bd-timew` alias**
  remains (prints a stderr warning, then dispatches) so existing wrappers and
  `.envrc` callers keep working through the transition. Env vars are now
  `BD_TRACK_*` (legacy `BD_TIMEW_*` still read); the sidecar is
  `.beads/bd-track.yaml` (legacy `.beads/bd-timew.yaml` still read); state
  lives under `~/.config|cache|state/bd-track` and `<beads>/bd-track/sessions/`
  (legacy locations still read when the new ones are absent). Run
  `bd-track migrate rename` to migrate naming in place (see below). The bd
  **issue prefix** `bd-timew-` is unchanged.
- **`start`/`stop`/`switch`/`status` re-wired onto the JSONL backend.** No
  Timewarrior dependency remains (removed from `install.sh`). `start` resolves
  the billing tuple, appends a `start` event (bead + flat tags + provenance),
  and claims the bead; it ends **this session's own** open interval first
  (single-active-per-session) but never another session's. `stop` closes this
  session's open interval(s); a no-argument `stop` can no longer halt other
  sessions. `status` shows this session's interval(s) and a count of other
  active sessions.

### Added

- **`bd-track active`** — lists all open intervals across **all** sessions (the
  concurrent-tracking view the timew backend structurally could not provide).
- **`bd-track report`** — aggregates closed intervals via `bd_track.aggregate`:
  `--by <bead|session|actor|role|group_id|tag-key>`, `--policy
  billing|machine|wallclock`, `--since/--until`. Open/stale intervals are
  excluded from totals and surfaced separately.
- **`--session-id`** now threads through every command, not just
  `session current`.

## [0.4.0] - 2026-06-02

Aggregator layer of the JSONL rewrite (epic `bd-timew-nfr`). Internal/library —
not yet wired into the CLI (the cutover is the next layer).

### Added

- **Aggregator + reports** (`bd_timew.aggregate`) — full-walk reader over the
  per-session JSONL log: pairs intervals by ULID, folds `correction`s
  (per-field latest-wins by `eid`), drops `cancel`led, surfaces open/stale
  intervals (excluded from totals). Flat aggregation policy
  (partition/collapse over `group_id`/`actor`/`role`/`session`) with built-in
  `machine` / `wallclock` / `billing` modes; `report(group_by=…)` groups closed
  intervals by bead / session / actor / role / group_id / billing-tag key.
  Malformed lines are skipped-and-reported. Hierarchical subtotals
  (`bd-timew-an7`) and an incremental summary cache (`bd-timew-wav`) are
  deferred follow-ups.

## [0.3.0] - 2026-06-02

First milestone of the timew → JSONL backend rewrite (epic `bd-timew-nfr`):
the event-log foundation and session identity. Internal/library layers — the
CLI still drives Timewarrior until the cutover (which also renames the package
to `bd-track`).

### Added

- **Session identity** (`bd_timew.session`, `bd-timew session current`,
  global `--session-id`) — resolves a session id via `--session-id` →
  `$BD_TIMEW_SESSION_ID` → `$CLAUDE_CODE_SESSION_ID` → a per-terminal
  current-session pointer → a generated human-friendly `word.word.word` id.
  Claude sessions resolve automatically with no cooperation needed.
- **JSONL event-log backend** (`bd_timew.events`) — append-only schema v1
  (`start`/`stop`/`cancel`/`correction`) with dual ULIDs (event + interval),
  a flat org-agnostic `tags` list, and `group_id`/`actor`/`role` provenance.
  Provenance is set on `start` and mutable only via `correction`
  (per-field latest-wins). Atomic `O_APPEND` appender; per-session logs under
  `<beads_dir>/bd-timew/sessions/` (server-mode fallback
  `~/.local/share/bd-timew/<project>/`). Not yet wired into the CLI.

## [0.2.0] - 2026-05-04

Bundled release: queue surface refactor + `init-project` bootstrapping +
auto-push hang preventer + server discovery improvements. Breaks the
top-level queue CLI; queue commands now live under a single `queue`
parent.

### Added

- **`queue` parent command** with subcommands `push`, `unshift`, `pop`,
  `peek`, `list`, `remove`, `clear`, `clean`, `generate`, `prune`. Replaces
  the prior flat top-level commands. Each subcommand inherits the common
  `--scope` / `--project-dir` / `--titles` flags; `bd-timew queue --help`
  surfaces them in the parent's epilog.
- **`queue clean`** — mechanical sweep that drops closed/deferred beads
  from queue scopes. Wired into `bd-timew stop` so newly-closed beads
  drop out of any queue automatically; pass `--no-clean` to skip.
- **`queue generate`** — populate a queue from `bd list` filters
  (`--label`, `--label-any`, `--label-pattern`, `--status`, `--keyword`).
  `--append` extends; default replaces (with confirmation if non-empty,
  `--yes` to skip).
- **`queue prune`** — analytical audit: surfaces stale entries, scope
  mismatches (heuristic on `scope:local`), dependency-ordering issues,
  and missing blockers. Auto-applies destructive removals on confirmation;
  move/reorder recommendations are reported but require manual action.
  `--yes` skips confirmation for unattended automation.
- **`init-project --bootstrap` (default)** — runs `bd init` automatically
  when `.beads/` is missing, forwarding `--server`, `--sandbox`,
  `--prefix`, and `--agents-profile` flags from bd-timew's CLI. Resolves
  PROJ-xji ("init-project: bootstrap bd init when .beads/ is missing").
- **`init-project --sandbox` (default)** — forwards `--sandbox` to
  `bd init`, the empirically-safe setting that disables auto-sync during
  init and avoids hangs against an active git remote.
- **`init-project --prefix <prefix>`** — forwards an issue prefix to
  `bd init` when bootstrapping.
- **`init-project --agents-profile {minimal,full}`** — defaults to `full`
  so the generated AGENTS.md carries the full bd command reference.
- **Auto-push hang preventer** in `init-project`: writes
  `dolt.auto-push: false` directly to `.beads/config.yaml` BEFORE any
  `bd config set` invocation, breaking the chicken-and-egg hang where
  the very write that would disable auto-push triggers an auto-push
  itself. Verified empirically (`~/Source/beads-test`, 2026-05-04).
- **`bd-timew servers` two-pass discovery** — in addition to listing
  registered repos from `~/.config/bd-timew/repos.yaml`, scans running
  `dolt sql-server` processes via `pgrep` + `/proc/<pid>/cwd` and
  reports any unregistered servers with a hint pointing at
  `bd-timew init-project --path <path>`. Resolves PROJ-a3v.

### Changed

- **CLI shape (BREAKING)**: removed flat top-level queue commands
  (`bd-timew push`, `bd-timew pop`, etc.). Use `bd-timew queue push`,
  `bd-timew queue pop`, etc. The old `bd-timew queue` (list contents)
  is now `bd-timew queue list`.
- `init-project` no longer sets `no-push: true` via `bd config set` — that
  key only gates the explicit `bd dolt push` subcommand and was unrelated
  to the auto-push hang. The actual disable lives in
  `_ensure_auto_push_disabled` writing `dolt.auto-push: false` directly.

### Fixed

- `bd-timew init-project` no longer hangs indefinitely on the first
  `bd config set` when the project shares its source-code git remote
  with bd's `sync.remote`. The new `_ensure_auto_push_disabled` path
  pre-empts the auto-push that previously fired during settings
  bootstrap.

### Notes

- `init-project --non-interactive` is intentionally *not* forwarded to
  `bd init` — the upstream flag has been observed to hang in bd 1.0.x.
  Callers running non-interactively must supply enough flags that
  `bd init` has no prompts to ask.
- `--global` flag and `beads_global` shared-server inbox: known to exist
  in upstream bd; not yet integrated into bd-timew commands. Tracked for
  v0.3.x.

## [0.1.0] - 2026-04-28

Initial pipx-installable release. Extracted from a personal
time-tracking workflow into a multi-module Python package.

### Added

- `start` / `stop` / `switch` / `status` / `resolve` — Beads + Timewarrior
  bridge: resolves a Beads issue's labels to a `(client, case, svc)`
  billing tuple via per-project sidecar (`.beads/bd-timew.yaml`), then
  starts/stops a tagged Timewarrior interval.
- `init-project` — registers a Beads project for bd-timew automation
  (sidecar scaffold, repos.yaml entry, optional Dolt server-mode wiring).
- `cleanup` — wraps `bd compact --days 7 && bd gc` for routine
  maintenance.
- `servers` / `server-stop` / `idle-stop` — Dolt SQL server lifecycle
  management for registered repos.
- Flat-top-level queue commands: `push`, `unshift`, `pop`, `peek`,
  `queue` (list), `remove`, `clear`. Replaced by the `queue` parent in
  v0.2.0.
- systemd user units for cleanup and idle-stop timers.

[Unreleased]: https://github.com/antimike/bd-track/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/antimike/bd-track/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/antimike/bd-track/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/antimike/bd-track/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/antimike/bd-track/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/antimike/bd-track/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/antimike/bd-track/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/antimike/bd-track/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/antimike/bd-track/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/antimike/bd-track/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/antimike/bd-track/releases/tag/v0.1.0
