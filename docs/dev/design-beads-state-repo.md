# Design (draft): a bd-track-managed state repo for beads

Status: **draft, 2026-10-27.** Written in a chezmoi session (MH with Claude) while setting up
beads for a new project; the design is to be finished in a bd-track session. Everything marked
*provisional* or *open* needs that discussion before implementation.

## Problem

1. **Bead state lives on one disk.** In embedded mode a project's issues exist only in its
   `.beads/` database. MH's convention (global `~/.claude/CLAUDE.md`, "Network ops in beads
   workflows") is that the JSONL export, pushed with plain git, is the dependable off-machine
   copy, because `bd dolt push` stalls on network trouble and must not run in automation.
2. **The JSONL cannot always go in the project's own repo.** For a public project it would
   publish working notes (machine details, decision numbers, personal context). bd-track itself
   already keeps `.beads/issues.jsonl` out of git for that reason.
3. **Evidence that this bites today (zbook, 2026-10-27):**
   - bd-track, `chezmoi-boco` and `claude-config` carry `dolt.shared-server: true` from the WSL
     work laptop. On zbook the shared server (`~/.beads/shared-server/`) has never held data;
     `bd dolt show` reports "Server not reachable". Their only local copies are untracked
     `issues.jsonl` files.
   - No bd-track cleanup timer and no `~/.config/bd-track/repos.yaml` exist on zbook.
   - The only scheduled backup (restic) covers `~/.claude/projects`, not `~/.local/src` or
     `~/Source`.
   - New project `amontillado` (public) starts with beads in embedded mode, `.beads/` gitignored:
     until this design exists its beads are not backed up.

## Design

- **A private state repo** holding one export per registered project:
  `<project>/issues.jsonl` (and possibly memories, see the open questions).
- **bd-track writes the exports.** It already has the registry (`repos.yaml`) and a daily pass
  over every project (`bd-track cleanup`: `bd dolt commit`, `bd compact`, `bd gc`). One more
  step in that pass: `bd export` into the state repo's working tree. No git operations in
  bd-track.
- **chezmoi's `sync-repos` timer commits and pushes the state repo**, as it already does for
  `claude-state`: in-script gitleaks scan (refuses without gitleaks), `pull --rebase`, retries,
  failure notifications. The state repo is provisioned by chezmoi, which is what lets it into
  the `sync-repos` list (chezmoi D36).
- **Scanning:** gitleaks yes (credentials do not belong on any remote, private or not); the
  client-term grep used for public repos **no**: the state repo may hold client data by design,
  and that grep would block every commit.
- **Restore:** `bd import` of a project's export, or `bd init --from-jsonl`.

### Proposed bd-track changes (to be refined)

- `repos.yaml`: state repo configuration (path; see options below for one vs several repos and
  per-repo encryption) and a per-project choice of state repo, or none.
- `cleanup`: the export step, skipped for projects without a state repo.
- `bd-track state restore <project>`: import a project's export into a fresh `.beads/`.
- `bd-track state status`: which projects export where, and when each was last exported.

## Client data: options (open)

Some projects' beads hold client or employer data. Client and employer agreements often forbid
storing it outside their systems, including in a private personal GitHub repo.

- **(a) Personal projects only.** Work projects' exports stay local and are backed up by restic
  to the encrypted external SSD. Simple; work projects get no off-site copy.
- **(b) Several state repos.** bd-track maps each project to a state repo, e.g. a personal one
  on GitHub and a work one on a remote the client allows (or none).
- **(c) Encryption.** Exports are encrypted before they are committed (age, or git-crypt), so a
  remote never sees plaintext. Cost: diffs and history are unreadable on the remote; key
  management (the YubiKey is already the GPG/SSH key; age has a YubiKey plugin) and restore
  depend on the key.

**MH's response (provisional, pending a bd-track session):** a combination of (b) and (c):
bd-track supports several state repos, and encryption can be turned on or off per state repo.
To be settled with the full bd-track context.

## Open: merge with `claude-state`, or keep separate?

`claude-state` is MH's private repo whose working tree is `~/.claude/projects` (Claude Code
auto-memory and session files), committed and pushed by `sync-repos`. Runbook: chezmoi
`docs/runbooks/memory-and-backups.md`.

- **For merging:** one private repo, one sync path, one place to restore from on a new machine;
  `bd remember` memories and Claude auto-memory are both "what the agent knows about a project"
  and would sit side by side.
- **For keeping them separate:** different owners and write patterns (Claude Code writes
  `~/.claude/projects` continuously; bd-track exports daily); different sensitivity and
  retention (session transcripts are large and personal; bead exports are small); the
  client-data split above applies to beads but may not map onto Claude's per-directory layout;
  a merged repo couples two tools' layouts.

This also decides whether **`bd memories`** are exported to the state repo: they live in
`.beads/` (whether `bd export` includes them is to be checked), and they overlap in purpose
with Claude auto-memory. MH sees valid arguments on both sides; **to be discussed in a bd-track session**.

## Independent decision: the shared Dolt server

`bd init --shared-server` runs one `dolt sql-server` for all projects (one database per project,
under `~/.beads/shared-server/`) instead of an embedded engine per project. That is a runtime
choice (a background process; concurrent access from several agents), **orthogonal** to the
state repo: `bd export` works in both modes, and the state repo stores exports either way. The
only coupling is that the export step must reach each project's database (start the server, or
skip and report when it is down). Decide it separately; it does not change this design.

Related: re-homing the three WSL-era projects above (embedded with an import from their JSONL,
or a working shared server on zbook) is a chezmoi open item.

## Rejected

- **JSONL in each project's own repo:** public for public projects (problem 2).
- **`bd dolt push` to Dolt remotes on a timer:** stalls on network trouble; against MH's global
  rule. One remote per database, and no single repo.
- **A git hook as the trigger:** beads change without git commits.
- **`mani` as the runner:** it lists every clone, not beads projects, and has no schedule.

## Pointers

- MH's global `~/.claude/CLAUDE.md`: "Persistent State" and "Network ops in beads workflows";
  `~/.claude/references/beads-performance.md` (batch mode, `no-push`, cleanup timer).
- chezmoi repo (`~/.local/share/chezmoi`): `docs/decisions.md` D35 (repo discovery, hooks),
  D36 (`sync-repos` list and unattended scans), D70 (amontillado);
  `docs/runbooks/memory-and-backups.md`; `docs/open-items.md` rows "beads state repo" and
  "Re-home the WSL-era beads projects".
- amontillado (`~/.local/src/amontillado`): `CLAUDE.md` (beads setup and the interim
  end-of-session export), the first project waiting on this design.
