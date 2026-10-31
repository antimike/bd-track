# bd-track

> Formerly **`bd-timew`**. The package, module, and command renamed to `bd-track`
> in 0.5.0 when the Timewarrior backend was replaced with an append-only JSONL
> event log. A deprecated `bd-timew` command alias still works (it prints a
> warning, then dispatches). The Beads **issue prefix** `bd-timew-` is unchanged —
> only the tool renamed.

A bridge between [Beads](https://github.com/steveyegge/beads) (issue tracker) and
an append-only **JSONL time-tracking event log**, with project lifecycle
management and scoped execution queues.

`bd-track` resolves a Beads issue's labels to a billing tuple `(client, case, svc)`
via a per-project sidecar (`.beads/bd-track.yaml`), then appends a tagged tracking
event to a **per-session** event log. Because each session writes its own log and
state is re-derived by folding those logs, there is no shared "active interval" —
concurrent sessions can track simultaneously without clobbering each other. It also
provides project maintenance commands (`init-project`, `cleanup`), Dolt server
lifecycle commands, named bead-execution queues, and one-way migrations off the old
naming and backend.

## Status

Alpha. The tool was extracted from a personal time-tracking workflow and may have
rough edges for general use. The `(client, case, svc)` tuple maps to whatever
vocabulary your billing system uses; configure it per-project in
`.beads/bd-track.yaml`.

## Why a JSONL event log?

The original backend drove a single global [Timewarrior](https://timewarrior.net/)
interval. Under parallel agent sessions that one shared interval was a race: one
session's `start` auto-ended another's, and a later `stop` could close the wrong
session's interval. The 0.5.0 rewrite removes the shared state entirely:

- **Append-only event log.** Every action is an immutable event
  (`start` / `stop` / `cancel` / `correction`) written with `O_APPEND`. Records are
  small (~400 bytes), well under the POSIX `PIPE_BUF` atomicity floor, so concurrent
  appends never interleave — no locks required.
- **Per-session logs.** Each session writes its own JSONL file, so concurrent
  writers touch different files. State is re-derived by folding the logs at read
  time (`events.py` pairs `start`/`stop` by interval ULID, applies `correction`s
  per-field latest-wins, drops `cancel`led intervals).
- **Single-active-per-session, not single-active-global.** A `start` ends only the
  caller's own open interval; a no-argument `stop` can only close ULIDs in the
  caller's own log. The cross-session view is opt-in via `bd-track active`.
- **Policy-based aggregation.** `bd-track report` walks the log and totals closed
  intervals under a chosen policy (`billing` / `machine` / `wallclock`) grouped by a
  dimension (`aggregate.py`).

### Storage location

Per-session logs live under the Beads directory at
`<beads_dir>/bd-track/sessions/<session-id>.jsonl` (server-mode fallback
`~/.local/share/bd-track/<project>/`). Config, cache, and state live under
`~/.config|cache|state/bd-track`. When a `bd-track` path is absent, the legacy
`bd-timew` location is read as a fallback, so existing data keeps resolving until
you run `bd-track migrate rename`.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/antimike/bd-track/main/install.sh | sh
```

Timewarrior is **no longer a dependency** (removed in 0.5.0). The installer pulls
`pipx` via your platform's package manager, then installs from a pinned tag.

Manual install (if you already have `bd` and `pipx`):

```bash
pipx install git+https://github.com/antimike/bd-track.git
```

> The GitHub repository is still named `bd-timew`; only the installed package,
> module, and command are `bd-track`.

## Quick start

```bash
# Scaffold a per-project sidecar
cd ~/Source/your-project
bd-track config init

# Start tracking a bead
bd-track start <bead-id>

# Check what THIS session is tracking
bd-track status

# Check every open interval across ALL sessions
bd-track active

# Stop (also sweeps closed beads from queues; deferred ones stay — pass --no-clean to skip)
bd-track stop <bead-id>

# Report closed time, grouped and totaled under a policy
bd-track report --by bead --policy billing --since 2026-06-01

# Queue beads for later execution
bd-track queue push <bead-id> [<bead-id>...]
bd-track queue list --titles
bd-track start "$(bd-track queue pop)"

# Build a queue from a search
bd-track queue generate --scope pipeline --label area:pipeline --status open,in_progress

# Audit a queue: surface stale, mismatched, and out-of-order entries
bd-track queue prune --scope pipeline
```

## Subcommand reference

| Command | Purpose |
|---|---|
| `start <id>` | Claim a bead and append a tagged `start` event (ends this session's own open interval first) |
| `stop [<id>] [--no-clean]` | Append a `stop` event for this session's open interval(s); sweeps closed beads from queues unless `--no-clean` |
| `switch <id> [--from <id>]` | Stop this session's current interval and start a new one |
| `status` | Show this session's active interval, bead, billing tuple, elapsed time, and a count of other active sessions |
| `active` | List **all** open intervals across **all** sessions (the caller's own marked `*`) |
| `report [--by <dim>] [--policy <p>] [--since/--until]` | Aggregate closed intervals; `--by bead\|session\|actor\|role\|group_id\|<tag-key>`, `--policy billing\|machine\|wallclock` |
| `resolve <id>` | Print resolved billing tuple without starting |
| `session current` | Resolve and print the session id for this invocation |
| `queue push <id...>` | Append beads to a queue scope |
| `queue unshift <id>` | Prepend a bead to a queue scope |
| `queue pop` | Remove and print head of a queue scope |
| `queue peek` | Print head of a queue scope without removing |
| `queue list` | List queue contents (all scopes, or one with `--scope`) |
| `queue remove <id...>` | Remove beads from a queue scope |
| `queue clear` | Empty a queue scope (`--scope all` to clear every scope) |
| `queue clean` | Mechanical sweep: drop closed/deferred beads from queues |
| `queue generate [filters]` | Build a queue from `bd list` search/filter criteria |
| `queue prune [--yes]` | Analytical audit: identify stale, mismatched, out-of-order entries |
| `migrate rename [--apply] [--all-repos]` | Rename `bd-timew` on-disk artifacts to `bd-track` (dry-run by default) |
| `migrate import [--apply] [--from-file]` | Import existing Timewarrior intervals into the JSONL log (dry-run by default) |
| `cleanup` | Run Beads/Dolt maintenance (commit, compact, GC) |
| `init-project` | Configure a Beads project for billing tuple resolution and registered cleanup |
| `config init` | Scaffold a per-project sidecar with annotated defaults |
| `servers` | List registered repos and their Dolt server status |
| `server-stop [--path]` | Stop Dolt servers for one or all registered repos |
| `idle-stop --hours N` | Stop Dolt servers idle longer than threshold |

A global `--session-id <id>` flag (and `$BD_TRACK_SESSION_ID` /
`$CLAUDE_CODE_SESSION_ID`) overrides session resolution for any command. All
`queue` subcommands accept `--scope <name>` (or `$BD_TRACK_SCOPE`) and
`--titles`/`-t` to fetch and display bead titles inline.

### `queue clean` vs `queue prune`

- **`clean`** is mechanical: it queries `bd` for the status of each entry and drops
  anything `closed` or `deferred`. No confirmation, no judgment calls. Also runs
  automatically after `bd-track stop`.
- **`prune`** is analytical: it surfaces a set of *proposals* — stale entries,
  scope-mismatched beads (heuristic on `scope:local` for now), dependency-ordering
  issues, and missing blockers — and asks for confirmation. Only the destructive
  subset (stale removal) is applied; move/reorder/add-before recommendations are
  surfaced for manual action. `--yes` skips confirmation for fully unattended runs.

## Migrating off `bd-timew`

Two one-way migrations ease the transition (both dry-run by default; pass `--apply`
to write):

- **`bd-track migrate rename`** renames on-disk artifacts so the legacy
  read-fallback shims stop firing: the config/cache/state dirs, the `.beads`
  sidecar, the `<beads>/bd-timew` session logs, and `BD_TIMEW_*` → `BD_TRACK_*` env
  rewrites. `--all-repos` sweeps every repo registered in `repos.yaml`. It is
  idempotent and guards home/dotfile targets managed by chezmoi.
- **`bd-track migrate import`** replays `timew export` into the JSONL log,
  preserving the historical timestamps, skipping open and bead-less intervals, and
  deduplicating by a content-hash `import_key` (so re-runs are safe). `--from-file`
  imports from a saved export instead of invoking `timew`.
- **`bd-track migrate registry`** moves `~/.config/bd-track/repos.yaml` into the
  shared project registry `~/.config/projects.yaml` (and its `global:` block into
  `~/.config/bd-track/config.yaml` if that file is missing), then renames it to
  `repos.yaml.migrated`. See [docs/registry-schema.md](docs/registry-schema.md).

## Per-project sidecar (`.beads/bd-track.yaml`)

Maps Beads issue labels to a billing tuple. Run `bd-track config init` to scaffold
an annotated template (the legacy `.beads/bd-timew.yaml` is still read as a
fallback):

```yaml
default:
  client: ""        # default client when no pattern matches
  case: ""          # default case
  svc: ""           # default service category

patterns:
  - match: "area:billable"
    client: "AcmeCo"
    case: "Sprint Q2"
    svc: "Engineering"
  - match: "area:internal"
    client: "internal"
    svc: "(none)"

# Per-issue override: a bead with `case:special-case` label uses
# that value for `case` regardless of pattern matches.
```

## Platform support

- **Linux**: full support (systemd timers for cleanup and idle-stop).
- **WSL2**: full support (treated as Linux).
- **macOS**: not yet — see issue tracker.
- **Windows native**: not yet — see issue tracker.

## License

MIT
