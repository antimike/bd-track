# Design (draft): off-site beads backup from bd-track cleanup

Status: **draft, 2026-10-27; reconciled with chezmoi D71 on 2026-11-01** (bead `bd-timew-ayn.1`).
Written in a chezmoi session (MH with Claude) while setting up beads for a new project; finished
in bd-track sessions. Implementation is tracked by epic `bd-timew-ayn`. Everything marked
*provisional* or *open* needs MH's decision before it is built on.

Two copies leave the machine, both written by `bd-track cleanup`:

1. **A JSONL export** of each project into a private **state repo** (readable, diffable; committed
   and pushed by a separate tool, never by bd-track).
2. **Optionally, a Dolt push** of each project's database to a private git repo under its own ref
   (full fidelity; behind a per-host switch, with a hard timeout and a timing log). *Provisional*,
   see [Dolt push](#dolt-push-provisional).

MH's own setup is the motivating example throughout (chezmoi `docs/decisions.md` D71, draft:
one private repo for all private project data, `~/.local/share/project-data`). bd-track itself
assumes no particular dotfiles manager or repo layout.

## Problem

1. **Bead state lives on one disk.** In embedded mode a project's issues exist only in its
   `.beads/` database. MH's convention (global `~/.claude/CLAUDE.md`, "Network ops in beads
   workflows") is that the JSONL export, pushed with plain git, is the dependable off-machine
   copy, because `bd dolt push` stalled on network trouble and must not block a session end.
2. **The JSONL cannot always go in the project's own repo.** For a public project it would
   publish working notes (machine details, decision numbers, personal context). bd-track itself
   already keeps `.beads/issues.jsonl` out of git for that reason.
3. **bd's default Dolt remote can be public.** Lesson from the 2026-10-29 re-home of bd-track's
   own beads: `bd init` made the code repo's `origin` a Dolt remote, and bd-track's `origin` is a
   public GitHub repo that carried a stale beads database under `refs/dolt/data`. Any push step
   must never target a public repo or a project's code origin.
4. **Evidence that this bites today (zbook, 2026-10-27):**
   - bd-track, `chezmoi-boco` and `claude-config` carry `dolt.shared-server: true` from the WSL
     work laptop. On zbook the shared server (`~/.beads/shared-server/`) has never held data;
     `bd dolt show` reports "Server not reachable". Their only local copies are untracked
     `issues.jsonl` files. (bd-track was since re-homed to embedded mode, 2026-10-29.)
   - No bd-track cleanup timer exists on zbook.
   - The only scheduled backup (restic) covers `~/.claude/projects`, not `~/.local/src` or
     `~/Source`.
   - New project `amontillado` (public) starts with beads in embedded mode, `.beads/` gitignored:
     until this design exists its beads are not backed up.

## JSONL export to a state repo

- **A private state repo** holds one export per registered project. The layout inside it is
  configurable; MH's D71 layout is `projects/<name>/beads/issues.jsonl` in the project-data repo
  (and possibly memories, see the open questions).
- **bd-track writes the exports.** It already has the project registry (`~/.config/projects.yaml`,
  [registry-schema.md](../registry-schema.md); legacy `repos.yaml` read-only) and a daily pass
  over every project (`bd-track cleanup`: `bd dolt commit`, `bd compact`, `bd gc`). One more
  step in that pass: `bd export` into the state repo's working tree, skipped for projects without
  a state repo. **No git operations in bd-track.**
- **Another tool commits and pushes the state repo.** In MH's setup that is chezmoi's
  `sync-repos` timer, as it already does for `claude-state`: in-script gitleaks scan (refuses
  without gitleaks), `pull --rebase`, retries, failure notifications (chezmoi D36).
- **Scanning:** gitleaks yes (credentials do not belong on any remote, private or not); a
  client-term grep of the kind used for public repos **no**: the state repo may hold client data
  by design, and that grep would block every commit.
- **Restore:** `bd import` of a project's export, or `bd init --from-jsonl`.

### Proposed bd-track changes (to be refined)

- Configuration (**built**, `bd-timew-ayn.2`): `global.state_export` (four-valued switch),
  `global.state_repo` and `global.state_layout` in `~/.config/bd-track/config.yaml`, and
  per-project `beads.state_repo` (`true`/`false` or a path, which covers option (b) below).
  Spec: [registry-schema.md, State export](../registry-schema.md#state-export).
- `cleanup`: the export step, phase 4 after commit/compact/gc (**built**, `bd-timew-ayn.2`).
- `bd-track state restore <project>`: import a project's export into a fresh `.beads/`.
- `bd-track state status`: which projects export where, and when each was last exported.

## Dolt push (provisional)

**MH decision 2026-10-29:** D71 provisionally overrides this doc's earlier rejection of
"`bd dolt push` on a timer", pending network benchmarks on zbook. Reasoning (chezmoi
`docs/plans/project-data.md`, "Dolt-native options"): the global rule exists to stop a push from
*blocking a session end*; a push from bd-track's daily timer with a hard timeout cannot block
anything (a stall fails, is logged and is retried the next run). MH suspects the stalls that led
to the rule came from WSL2's network stack, not from Dolt.

Shape:

- **One private git repo, distinct refs.** Dolt can use a git repository as a remote, storing
  its data under a git ref (`dolt remote add --ref <ref>`, default `refs/dolt/data`). Each
  database pushes under its own ref, `refs/dolt/<database>`, so all databases share one repo.
  Per database rather than per project, because a shared server's global database can serve
  several projects; for embedded and one-database-per-project setups the two coincide.
  Ordinary clones do not fetch `refs/dolt/*`. **Verified 2026-11-02** (`bd-timew-ayn.3`, Dolt
  2.4.0): two databases pushed to `refs/dolt/one` and `refs/dolt/two` of one local bare repo
  without interfering; Dolt also creates a shared `refs/heads/__dolt_remote_info__` branch, and
  the repo needs one branch before the first push. Compaction and restore: `bd-timew-ayn.6`.
- **Hard timeout, failure isolation** (**built**, `bd-timew-ayn.3`): after the export, push with
  a timeout (default 120 s, `global.dolt_push_timeout`). A failure or timeout is logged and never
  blocks other projects or other cleanup steps.
- **Public-remote guard** (**built**, `bd-timew-ayn.3`): refuse the code repo's own remotes,
  public GitHub repos (unauthenticated API check, after resolving the real host with
  `git ls-remote --get-url` and `ssh -G`) and unverifiable hosts unless vouched for
  (problem 3).
- **Server mode** (**built**, `bd-timew-ayn.3`): remotes are read and added through SQL
  (`dolt sql` for embedded, `bd sql` for servers). A timeout also kills the push inside the
  server; that path and server credentials (`SSH_AUTH_SOCK` under systemd) still need a test
  against a live shared server (follow-up bead). The push always passes `--no-adopt`, since `bd dolt push` would otherwise offer
  to adopt the git origin as its remote. Spec:
  [registry-schema.md, Dolt push](../registry-schema.md#dolt-push).
- **Per-host switch** (`bd-timew-ayn.4`): `global.dolt_push` in `~/.config/bd-track/config.yaml`
  takes `always` / `never` (forced) or `true` / `false` (default, overridden per project by
  `beads.dolt_push` in the registry). Default `false`. Semantics:
  [registry-schema.md, `dolt_push`](../registry-schema.md#dolt_push).
- **Timing log** (**built**, `bd-timew-ayn.5`): one JSON line per attempted push in
  `~/.local/state/bd-track/dolt-push.jsonl` (timestamp, host, project, database, mode, remote,
  database size as a proxy for bytes, duration, result, output tail), so about two weeks of zbook
  data can decide whether the global rule relaxes for non-WSL hosts. Until then the session rule
  stands. Columns and how to read it:
  [registry-schema.md, Push timing log](../registry-schema.md#push-timing-log).
- **Push after compaction** (`bd-timew-ayn.6`): `bd compact` rewrites history, so the next push
  must replace the remote ref; check whether `bd dolt push` needs force (only when the remote is
  ours). History growth is minor: compaction keeps databases at 0.4–1.5 MB.
- **bd's own `no-push` stays on** in each project: only the cleanup timer pushes.

> **WSL2 warning.** Dolt pushes have stalled on WSL2 network stacks (MTU/PMTU black holes, TCP
> metrics cache, offload bugs). On WSL2 hosts set `global.dolt_push: never`. Help text and user
> docs for the push step must say so.

## Client data (open; see D71)

Some projects' beads hold client or employer data. Client and employer agreements often forbid
storing it outside their systems, including in a private personal GitHub repo.

- **(a) Personal projects only.** Work projects' exports stay local and are backed up by restic
  to the encrypted external SSD. Simple; work projects get no off-site copy.
- **(b) Several state repos.** bd-track maps each project to a state repo, e.g. a personal one
  on GitHub and a work one on a remote the client allows (or none).
- **(c) Encryption.** Exports are encrypted before they are committed (age, or git-crypt), so a
  remote never sees plaintext. Cost: diffs and history are unreadable on the remote; key
  management and restore depend on the key.

**MH's provisional answer:** (b) + (c). D71 refines it for a single project-data repo: (c) as
**git-crypt by path** (`projects/<work-project>/**` encrypted in place, which bd-track does not
need to know about), with (b) kept for a client whose rules forbid GitHub altogether. Still open:
D71 open question 2. The same question applies to Dolt pushes: a work project's database should
not be pushed to a repo its client forbids, which the per-project `beads.dolt_push: false`
covers.

## Merge with `claude-state` (largely answered by D71)

`claude-state` is MH's private repo whose working tree is `~/.claude/projects` (Claude Code
auto-memory and session files), committed and pushed by `sync-repos`.

D71 (draft) proposes merging: one private repo (`project-data`) replaces `claude-state` and holds
Claude data, ticket inboxes, bead exports and bd-track config, with transcripts and Dolt
databases going to restic instead. How `~/.claude/projects` joins it (symlink, bind mount, or
keep `claude-state` separate for now) is D71 open question 1, a chezmoi concern. For bd-track the
answer changes nothing beyond the configured export path, which is why the layout is
configurable.

**`bd memories` export** (D71 open question 5): **answered for bd-track, MH 2026-11-02**:
memories are backed up. The export runs `bd export --include-memories`, so memory records
(`"_type":"memory"`) sit in the same file and `bd import` restores issues and memories together
(verified with a scratch restore: 57 issues, 9 memories). bd leaves memories out by default as
"sensitive agent context", which suits exports that get shared, not an opt-in private backup.
Whether memories overlap with Claude auto-memory is a separate question about where knowledge
lives, not about what is backed up.

## Independent decision: the shared Dolt server

`bd init --shared-server` runs one `dolt sql-server` for all projects (one database per project,
under `~/.beads/shared-server/`) instead of an embedded engine per project. That is a runtime
choice (a background process; concurrent access from several agents), **orthogonal** to the
state repo: `bd export` works in both modes, and the state repo stores exports either way. The
only coupling is that the export and push steps must reach each project's database (start the
server, or skip and report when it is down). D71 open question 4 (shared server vs shared
database) is decided separately and does not change this design.

## Rejected

- **JSONL in each project's own repo:** public for public projects (problem 2).
- **A git hook as the trigger:** beads change without git commits.
- **`mani` as the runner:** it lists every clone, not beads projects, and has no schedule.
- ~~**`bd dolt push` to Dolt remotes on a timer**~~: moved to [provisional](#dolt-push-provisional)
  on 2026-10-29. The earlier objection "one remote per database, and no single repo" is answered
  by distinct refs in one repo; the stall objection by the timeout, switch and benchmark.

## Pointers

- Epic `bd-timew-ayn` and children `.2`–`.6` (implementation); this doc is `.1`.
- [registry-schema.md](../registry-schema.md): registry location, `beads.*` keys, `dolt_push`.
- MH's global `~/.claude/CLAUDE.md`: "Persistent State" and "Network ops in beads workflows";
  `~/.claude/references/beads-performance.md` (batch mode, `no-push`, cleanup timer).
- chezmoi repo (`~/.local/share/chezmoi`): `docs/decisions.md` D71 (project-data repo, draft),
  D72 (ticket inboxes, registry), D35 (repo discovery, hooks), D36 (`sync-repos` list and
  unattended scans), D70 (amontillado); `docs/plans/project-data.md` (D71 detail, Dolt-native
  options, open questions); `docs/runbooks/memory-and-backups.md`.
- Originating ticket: `~/.local/state/triage/bd-track/done/2026-10-29-cleanup-dolt-push.md`.
- amontillado (`~/.local/src/amontillado`): `CLAUDE.md` (beads setup and the interim
  end-of-session export), the first project waiting on this design.
