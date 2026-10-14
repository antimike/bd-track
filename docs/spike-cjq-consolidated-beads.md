# Spike: Feasibility of consolidating all projects' beads into a shared store

- **Bead:** `bd-timew-cjq` (P1 spike, gates the consolidation epic)
- **Date:** 2026-06-14 → 2026-06-16
- **bd version under test:** 1.0.3 (`1b2dd2cb`)
- **Status:** Complete — gating question answered **YES**; recommendation **reframed** by findings.

---

## TL;DR / Verdict

1. **The gating question is answered YES.** bd has a first-class, cwd-resolved, `BEADS_DIR`-free pointer mechanism: **`.beads/redirect`**. The feared LOE-balloon ("fighting the tool's per-project architecture") **does not materialize** — consolidation is supported natively along the whole stack (shared server + redirect + hydration/routing).

2. **Privacy and consolidation are separable goals**, and the privacy goal has a *much cheaper* native solution than the 5–8-day migration epic the bead scoped:
   - **`.beads/redirect`** moves a project's entire store to a central private location, leaving only a **gitignored pointer file** in the project repo (zero issue data, zero client tags). **Validated empirically.**
   - Or, even simpler for projects that keep their own store: **gitignore `.beads/issues.jsonl`**. **Validated empirically.**

3. **Correction from empirical testing:** my pre-test recommendation floated `backup.git-repo` as "the cleaner native alternative" to redirect/git-crypt. **Testing disproved that.** `backup.git-repo` does **not** redirect the primary plaintext `issues.jsonl` out of the repo; it writes an opaque Dolt `.darc` archive to a side repo (and failed to auto-commit in the test). It is a finicky *secondary* backup, not a privacy redirect. Use `.beads/redirect` or a plain gitignore instead.

4. **Drop the "collapse everything into `beads_global`" idea (the earlier "Model B").** Per the changelog, `beads_global` (v0.59.0) is a *project-agnostic* peer store with a sentinel UUID — it is **not** an inbox issues flow through, and there is **no native command to move/refile issues between databases** in either direction. Collapsing all projects into it runs against the tool's design and has no clean split-back-out path.

---

## The three storage models (as found on this machine)

| Model | Server | Databases | cwd-resolved, no `BEADS_DIR`? | Privacy |
|---|---|---|---|---|
| **A. Shared-server** (`bd-track` today) | 1 server @ `~/.beads/shared-server` | N (one per project) | **Yes**, via `.beads/` pointer | Partial* |
| **B. `--global` / `beads_global`** | 1 server | 1 (`beads_global`) | No — needs `--global` flag; mis-aligned with intent | Full but wrong tool |
| **C. Per-project** (the other 7 repos) | N servers, N ports | N | Yes | None (status quo) |

\* Under Model A the Dolt *data* leaves the repo, but the git-tracked `.beads/issues.jsonl` export remains in-repo — that is the actual leak vector. Closing it needs `.beads/redirect` or a gitignore (see Empirical Validation).

### Inventory of registered repos (`~/.config/bd-track/repos.yaml`)

Only `bd-track` uses the shared `~/.beads` server (`dolt.shared-server: true`, port 3308, DBs `bd_timew` + `beads_global`). The other seven each run their **own** Dolt server on their own port with a distinct `dolt_user`:

| Repo | DB name | Port | issues.jsonl git-tracked? |
|---|---|---|---|
| `bd-track` (this) | `bd_timew` | 3308 (shared) | yes |
| `client-project` | `ClientProject` | 44835 | no |
| `claude-config` | `ClaudeConfig` | 33905 | yes |
| `beads-test` | `Test` | (not running) | no |
| `chezmoi` | `Chezmoi` | 40163 | yes |
| `wsl-toast` | `WSLToast` | (not running) | yes |
| `confluence-connector` | `Confluence` | 40073 | yes |
| `transition-2026` | `Transition2026` | 46079 | yes |
| `/tmp/tmp.efduAFLT81` | — | — | **no `.beads/` — stale registry entry** |

Project IDs are all distinct; DB names are all distinct (no DB-name collision). Issue-prefix collision audit still owed for the full epic, but prefixes look distinct (`bd-timew-`, etc.).

---

## The native mechanisms (what each is *for*)

### `.beads/redirect` — the cwd-resolved pointer (the headline)
A single filesystem path (relative or absolute) to another `.beads` directory; documented in `ADVANCED.md` → "Database Redirects". Created **manually** (no command): `echo "/path/to/central/.beads" > .beads/redirect`. bd follows it for all operations; `bd where` reports `(via redirect from …)`. It is **gitignored by design** (`bd doctor --fix` untracks it). bd's hooks, sync, worktrees, and the MCP plugin all honor it.

### Shared-server mode (`dolt.shared-server: true`)
One Dolt server at `~/.beads/shared-server/dolt` hosting N databases (one per project). Resource consolidation; each project keeps a distinct DB. `bd-track` already runs this way.

### `--global` / `beads_global`
Introduced v0.59.0: *"Dedicated shared-server database for cross-project state."* Project-agnostic, fixed sentinel UUID (all-zeros) — which is why `bd --global status` from inside a project trips `PROJECT IDENTITY MISMATCH` (the guard compares the repo's `metadata.json` project_id to the DB's). Intended as a **peer store for project-less state**, *not* a consolidation target and *not* an inbox.

### `repo` (multi-repo hydration, bd-307)
Federation **of views**: a primary DB hydrates read-copies of `repos.additional` for unified querying; writes route back to the owning DB. Source of truth stays per-project. Does **not** centralize storage → does **not** solve privacy.

### Role-based auto-routing
bd's designed answer to "don't let planning pollute a pushed repo." `routing.mode: auto` (off by default in 1.0.3 per #1177; opt in via config or `bd init --contributor`). Role detected via `git config beads.role` → remote URL (SSH ⇒ maintainer, HTTPS ⇒ contributor) → fallback contributor. On `bd create`: maintainer → current repo; contributor → `~/.beads-planning` (auto-added to `repos.additional`). **Note:** this addresses *contributor planning leaking upstream* — it does **not** help our driver, where *maintainer-owned project issues* carry sensitive client tags.

### Cross-project work = reference, not relocation
`bd dep add <issue> external:<rig>:<id>`, `bd show rig:id`, `bd activity --town`, `bd ship <capability>`. There is **no** native move/transfer/reassign between DBs; relocation = export→import (the `beads-migration` skill).

---

## Empirical validation (scratch instances, never `~/.beads`)

Both tests ran fully isolated under `/tmp` in **embedded-Dolt** mode (no `--server`, so the real shared server was never contacted), with `BEADS_DIR`/`BEADS_DOLT_PASSWORD` unset. Post-test confirmation: the shared server still showed exactly its 5 databases — **zero pollution** — and no lingering scratch Dolt processes. Scratch dirs removed afterward. Scripts reproduced in the Appendix.

### Test A — `.beads/redirect` cwd resolution → **PASS (strong)**

Setup: a `central/` store (prefix `central`, one issue tagged `client:ACME-SENSITIVE`) and a `projectX/` git repo whose `.beads/` contained **only** a `redirect` file pointing at `central/.beads`, with `.beads/redirect` gitignored.

Observed, from `projectX` cwd with `BEADS_DIR` unset:

```
projectX 'bd where':
/tmp/bd-spike/central/.beads
  (via redirect from /tmp/bd-spike/projectX/.beads)
  database: /tmp/bd-spike/central/.beads/embeddeddolt
```

- `bd create` from `projectX` produced **`central-8qw`** — the `central-` prefix proves the write bound to central's DB, not a projectX DB.
- Central afterward held **both** issues (`central-g9v` with the sensitive label + `central-8qw`).
- `projectX/.beads` contained **only** `redirect`; the sole git-tracked file was `.gitignore`; `grep -r ACME-SENSITIVE projectX/` found **nothing**.

**Conclusion:** redirect resolves cwd→central with no `BEADS_DIR`, binds reads and writes to central, and leaves the project repo carrying a gitignored pointer and **zero issue data**. This single mechanism satisfies **both** consolidation and privacy.

### Test B — `backup.git-repo` (JSONL→separate private repo) → **PASS w/ caveats; corrects the recommendation**

Setup: a standalone `proj/` store with its own DB, `backup.enabled true`, `backup.git-repo` → a separate `private-backup/` git repo, `backup.git-push false`, and `.beads/issues.jsonl` gitignored in `proj`. Created an issue tagged `client:ACME-SENSITIVE`.

Observed:
- The primary plaintext export **still landed in `proj/.beads/issues.jsonl`** — `backup.git-repo` did **not** redirect it.
- The gitignore worked: `issues.jsonl` was **not** git-tracked in `proj` (so it won't be pushed with the project repo). ← this is the real privacy lever in the keep-your-own-store model.
- `backup.git-repo` wrote an **opaque Dolt archive** (`*.darc`, `manifest`, `backup_state.json`) into `private-backup/backup/` — **not** plaintext JSONL; `grep ACME-SENSITIVE private-backup/` found nothing in cleartext. It also **failed to auto-commit** (`Warning: auto-export: git add failed: exit status 1`; left as `?? backup/`).
- `bd backup sync` errored (`no backup destination configured`), confirming the `bd backup` **Dolt-remote** path is a *separate subsystem* from the `backup.*` config block.

**Conclusion / correction:** `backup.git-repo` is a finicky *secondary* Dolt-archive backup, **not** a clean redirect of the primary plaintext export. Do **not** rely on it for the privacy goal. Prefer `.beads/redirect` (best — also consolidates) or a plain `.beads/issues.jsonl` gitignore.

### git-crypt verdict (unchanged by testing, now lower-priority)
git-crypt/age on `.beads/issues.jsonl` would close the leak (bd writes plaintext to the working tree; the committed blob is encrypted). Caveats: external readers (bv viewers, GitHub render, fresh clones pre-key) see ciphertext; key management reintroduces the account-rotation concern. Given Test A, it is **redundant** — redirect removes the file from the repo entirely, so there is nothing to encrypt.

---

## Recommendation

1. **Adopt `.beads/redirect` as the consolidation + privacy mechanism.** Point each project's `.beads/` at a central private store (the shared `~/.beads` server, or a per-account private `.beads` location with its own private sync). The project repo then tracks only a gitignored pointer. This is low-risk, native, and reversible.

2. **Decouple the privacy fix from the full migration.** The privacy driver — client-sensitive tags in pushed `issues.jsonl` — can be closed *today*, per repo, by either redirecting the store or gitignoring `issues.jsonl`. This does **not** require the 5–8-day epic.

3. **Drop `beads_global` (Model B) as a target** and drop `backup.git-repo` as a privacy lever (both disproven/mis-aligned above).

4. **Sequence for the follow-on epic (if pursued):** (a) prefix-collision audit across the 8 repos; (b) stand up the central private store + its private sync target (the privacy invariant); (c) migrate per-project via redirect, additively (keep per-project stores through a trial), validating round-trip `bd list --json` set-equality and cwd-discovery with `BEADS_DIR` unset per repo; (d) decommission the per-project Dolt servers last. Risk concentrates in (b)/(d), not in discovery (redirect solves that).

5. **`bd-track` integration** (`repos.yaml` + `log_dir`) centralizes automatically once `beads-dir` is the shared store — but all projects' session logs then co-mingle and need prefix subdirs (already noted in the bead).

---

## Appendix A — Full pre-writeup synthesis (verbatim)

> I have everything now — including the finding that directly resolves the spike's gating question. Let me give you the full synthesis.
>
> ## The headline finding: `.beads/redirect` is the native pointer mechanism
>
> The spike's gating question was: *"does bd natively support a consolidated store that resolves from each repo's cwd without `BEADS_DIR`? If there is no native redirect/pointer mechanism, the LOE balloons."*
>
> **There is one.** bd has a first-class `.beads/redirect` file (documented in `ADVANCED.md` → "Database Redirects"; gitignored by design; bd doctor, sync, worktrees, and the MCP plugin all follow it). It's a per-repo pointer that says "my store lives elsewhere" — exactly the cwd-resolved redirect the bead worried might not exist. So **the LOE-balloon scenario does not materialize.** Combined with what you already run (shared-server mode), bd supports consolidation natively along the whole stack. I'll fold the redirect mechanics into the spike writeup.
>
> ## Your specific questions
>
> ### 1. Intended purpose of the global DB
>
> Per the changelog, `beads_global` arrived in **v0.59.0**: *"Dedicated shared-server database for cross-project state."* It's **project-agnostic** — it deliberately doesn't belong to any project, which is why it carries a fixed sentinel UUID (the all-zeros ID), and why `bd --global status` from inside this repo tripped `PROJECT IDENTITY MISMATCH`: the guard compares your repo's `metadata.json` project_id against the DB's, and global's sentinel never matches a project.
>
> So of your two hypotheses, it's closer to **"a personal repository of non-project-specific state"** than **"an inbox to be reassigned."** It's designed as a *peer* store that sits alongside project DBs for things that belong to no project — not a staging area issues flow *through* on their way to a project.
>
> ### 2. Is there a recommended refile path (global ↔ project)?
>
> **No native move/transfer command exists**, in either direction — I confirmed this across the FAQ, the multi-repo docs, and the binary's command set. bd's design answer to "work that spans projects" is **reference, not relocation**:
> - Cross-rig dependencies: `bd dep add <issue> external:<rig>:<id>`
> - Cross-rig viewing: `bd show rig:id`, `bd activity --town` (aggregated feed)
> - `bd ship <capability>` to satisfy cross-project deps by closed-issue label
>
> The only way to physically relocate an issue between DBs is **export → import** (your `beads-migration` skill). This is a significant design fact: **collapsing everything into `beads_global` (the "Model B" I floated earlier) runs against the tool's grain** — it's not the global DB's intended role, and there's no clean path to split projects back out later if you change your mind.
>
> ### 3. Role-based auto-routing (what it does, when to use it)
>
> This is bd's *designed* answer to "don't let my planning pollute a pushed repo" — and it's directly relevant to your privacy driver.
>
> **Mechanism:**
> - `routing.mode`: `auto` (detect role and route) vs `explicit` (single fixed destination). In your installed v1.0.3, **auto-routing is off by default** (changed in #1177 — the older docs that say "auto is default" predate this); you opt in with `bd config set routing.mode auto` or `bd init --contributor`.
> - **Role detection** (priority order): `git config beads.role` → remote URL inspection (**SSH ⇒ maintainer**, **HTTPS/no-push ⇒ contributor**) → fallback contributor. *(This repo resolves to `maintainer` — I checked.)*
> - **Functional effect on `bd create`:**
>   - **Maintainer** → issue stored in the current repo's DB (`source_repo = "."`)
>   - **Contributor** → issue routed to a *separate* planning DB (`routing.contributor`, default `~/.beads-planning`), and that repo is auto-added to `repos.additional` so it still shows up in your `bd list`/`bd ready` via hydration.
> - `bd init --contributor` wires all of that up in one shot.
>
> **Intended use-case:** the OSS-contributor workflow — you're hacking on someone else's repo over an HTTPS fork, and you want your planning beads visible to *you* but **never committed into a PR**. Your planning lives in `~/.beads-planning`; the upstream repo stays clean.
>
> **Important caveat for your situation:** role-routing solves *"my personal planning shouldn't leak into upstream."* It does **not** solve your actual driver, which is different: your *legitimate maintainer-owned project issues* carry sensitive **client tags**, in repos you push. Those issues are exactly the ones role-routing keeps *in* the repo (you're the maintainer). So routing is the wrong lever for your privacy problem — useful to know it exists, but it doesn't apply here.
>
> ### 4. Would git-crypt on `issues.jsonl` solve privacy under Model A?
>
> **Yes, it closes the hole — but it's probably not the best tool.** Under shared-server mode (Model A) the Dolt *data* already lives outside the repo (`~/.beads/shared-server/dolt/`); the **only** remaining leak vector is the git-tracked `.beads/issues.jsonl` export. So:
>
> - **git-crypt (or age via `.gitattributes`) works**: bd writes plaintext into the working tree (fine, it's your machine), git-crypt encrypts the blob on commit/push. The sensitive tags never land in the remote in plaintext. ✅
> - **Caveats:** (a) any external reader of the remote — e.g. `bv` web viewers, GitHub's rendered view, a fresh clone before keys are loaded — sees ciphertext; (b) key management reintroduces the account-rotation concern your CLAUDE.md already flags for off-machine sync; (c) it encrypts the *symptom* rather than removing the data from the repo.
>
> - **The cleaner native alternative:** bd has a `backup.git-repo` config key — point the JSONL export at a **separate private git repo**, and gitignore `issues.jsonl` in the project repo. No crypto, no key distribution, and it preserves your "JSONL pushed for cross-session visibility" workflow — just to a private destination instead of the project remote. This is bd's built-in escape hatch for precisely this problem, and I'd reach for it before git-crypt.
>
> *(↑ NB: empirical Test B later corrected this last bullet — `backup.git-repo` does not redirect the primary `issues.jsonl` and writes an opaque Dolt archive; prefer `.beads/redirect` or a gitignore. See the Empirical Validation section above.)*
>
> ## How this reframes the recommendation
>
> The research changes the picture from my earlier table:
>
> - **`beads_global` (Model B) is mis-aligned with the tool's intent** and has no extraction path — drop it as the consolidation target.
> - The tool's *designed* shape is: **per-project DBs as source of truth** + **one shared server** (Model A, resource consolidation) + **`.beads/redirect`** for cwd resolution + **hydration/`external:` references** for unified views.
> - Your **privacy driver is best served not by storage consolidation at all**, but by **redirecting the `issues.jsonl` export to a private location** (`backup.git-repo` + gitignore), optionally hardened with git-crypt. That's a much smaller, lower-risk change than the 5–8-day migration epic the bead scoped.
>
> That last point is the big one: the spike may conclude that **the privacy goal and the consolidation goal are separable**, and the privacy goal has a cheap native solution that doesn't require the migration at all.
>
> Sources: beads FAQ, MULTI_REPO_AGENTS.md, Multi-Repository Features (DeepWiki), beads CHANGELOG, Discussion #759.

---

## Appendix B — Test scripts

### Test A (`/tmp/bd-spike-testA.sh`)

```bash
#!/usr/bin/env bash
# Spike bd-timew-cjq — Test A: .beads/redirect cwd resolution (no BEADS_DIR)
set -u
unset BEADS_DIR BEADS_DOLT_PASSWORD
ROOT=/tmp/bd-spike; rm -rf "$ROOT"; mkdir -p "$ROOT"
# 1. central store
mkdir -p "$ROOT/central"; cd "$ROOT/central"; git init -q
bd init --non-interactive --prefix central >/dev/null 2>&1
bd create --title "Central-owned issue" --type task --labels "client:ACME-SENSITIVE" -q
bd where
# 2. project repo holding ONLY a redirect pointer
mkdir -p "$ROOT/projectX/.beads"; cd "$ROOT/projectX"; git init -q
printf '%s\n' "$ROOT/central/.beads" > .beads/redirect
printf '%s\n' ".beads/redirect" > .gitignore
# 3. resolve from projectX cwd, BEADS_DIR unset
bd where                       # -> (via redirect from .../projectX/.beads)
# 4. write must land in central
bd create --title "Filed from projectX cwd" --type task -q --force
( cd "$ROOT/central" && bd list )   # -> shows central-g9v AND central-8qw
# 5. privacy check
ls -A "$ROOT/projectX/.beads"       # -> only: redirect
grep -rIl "ACME-SENSITIVE" "$ROOT/projectX"   # -> nothing
```

### Test B (`/tmp/bd-spike-testB.sh`)

```bash
#!/usr/bin/env bash
# Spike bd-timew-cjq — Test B: backup.git-repo (JSONL snapshot -> separate repo)
set -u
unset BEADS_DIR BEADS_DOLT_PASSWORD
ROOT=/tmp/bd-spike-b; rm -rf "$ROOT"; mkdir -p "$ROOT"
mkdir -p "$ROOT/proj" "$ROOT/private-backup"
( cd "$ROOT/private-backup" && git init -q )
cd "$ROOT/proj"; git init -q
bd init --non-interactive --prefix proj >/dev/null 2>&1
bd config set backup.enabled true
bd config set backup.git-repo "$ROOT/private-backup"
bd config set backup.git-push false
printf '%s\n' ".beads/issues.jsonl" >> .gitignore
bd create --title "Proj issue with client tag" --type task --labels "client:ACME-SENSITIVE" -q
bd export; bd backup sync; bd backup status
# findings: issues.jsonl stays in proj/.beads (gitignored, untracked);
# private-backup/backup/ gets opaque *.darc Dolt archive (no cleartext tag),
# and auto-commit into it failed ("git add failed: exit status 1").
```
