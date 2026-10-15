# Changelog

All notable changes to bd-track (formerly `bd-timew`) are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/AudensAurantius/bd-timew/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/AudensAurantius/bd-timew/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/AudensAurantius/bd-timew/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/AudensAurantius/bd-timew/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/AudensAurantius/bd-timew/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AudensAurantius/bd-timew/releases/tag/v0.1.0
