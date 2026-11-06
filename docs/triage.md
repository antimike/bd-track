# Triage: the beads layer for a ticket inbox

A ticket inbox is a directory of requests left for a project by other projects, other agents or
by hand. `bd-track triage` (epic `bd-timew-4is`) is the beads layer over it: it lists a project's
open tickets and, for each one handled, creates a bead or records why not, then moves the ticket
to `done/`. Deciding *whether* to accept a ticket is left to a person or an agent (in MH's setup,
the `/triage` skill in a Claude session); bd-track supplies the mechanics.

## Inbox layout

bd-track reads this layout and parses it leniently. Every frontmatter key is optional, and a
free-form file or a one-line paragraph is a valid ticket.

```
<inbox>/<project>/
  triage/*.md        # one ticket per file
  triage/inbox.md    # quick notes, one ticket per paragraph
  done/              # handled tickets, with resolution keys added
```

The recommended ticket format (frontmatter keys, resolution keys) belongs to the tool that files
tickets, not to bd-track. In MH's setup that is the standalone `ticket` command; bd-track links
to its spec instead of restating it.

### Where the inbox is

`--inbox DIR` names it directly. Otherwise it is `<root>/<project>`, where `<root>` is
`$BD_TRACK_INBOX_ROOT`, else `$XDG_STATE_HOME/triage` (`~/.local/state/triage` when `XDG_STATE_HOME` is unset or relative). `<project>` is
the [project registry](registry-schema.md#resolving-a-project) name of `--project` or of the
current git repository (its `path`, else its `origin` matched against `remote`), so every clone
and worktree of a project shares one inbox. An unregistered directory, or an unreadable registry
(reported as a warning), falls back to the directory's basename.

## Working the inbox interactively

```
bd-track triage [--dry-run] [--inbox DIR] [--project NAME-OR-DIR]
```

With no action, `triage` shows each open ticket in turn and asks what to do:
**[a]ccept** (prompts for the bead's title, type and priority, then `resolve --bead`),
**[r]eject** (a reason), **[d]uplicate** (what it duplicates), **[s]kip**, **as[k]** (a question;
the ticket stays open, see below) or **[q]uit**. The loop is built only on `list`, `resolve` and
`ask`, and re-reads the inbox after every action. `--dry-run` asks the same questions and prints
each action instead of writing. It needs a terminal; scripts and agents use the commands below.

The shared options (`--inbox`, `--project`, `--dry-run`) go before or after the action.
`--project` takes a registry name or a directory.

## Asking about a ticket

```
bd-track triage ask <ticket> <question> [--inbox DIR] [--project NAME-OR-DIR] [--dry-run]
```

Appends a `## Triage YYYY-MM-DD` section with the question (what is unclear and who must answer)
and leaves the ticket open in `triage/`. A quick note has no room for a section, so it is first
promoted to its own `triage/<date>-<slug>.md` (its stamp's keys become frontmatter, with
`status: open`) and its paragraph leaves `inbox.md`.

## Listing tickets

```
bd-track triage list [--json] [--inbox DIR] [--project NAME-OR-DIR]
```

Lists the open tickets: files in `triage/` in name order, then `inbox.md` paragraphs. A file whose
`status` is anything but `open` is skipped. The table shows id, date, sender, type/urgency and
title, with `-` where a ticket does not say.

`--json` is the contract for scripts and agents:

```json
{
  "inbox": "/abs/path/to/inbox",
  "tickets": [
    {
      "id": "2026-10-29-some-ticket",   // or inbox-<hash> for a quick note
      "path": "/abs/path/to/inbox/triage/2026-10-29-some-ticket.md",
      "source": "2026-10-29-some-ticket.md",  // inbox.md for a note
      "kind": "file",                   // or "note"
      "title": "...",
      "from": "...", "date": "YYYY-MM-DD",    // null when absent
      "type": "feature", "urgency": "high",   // null unless a recognised value
      "status": "open",
      "meta": {},                       // every frontmatter or stamp key, as strings
      "body": "..."                     // text after the frontmatter or stamp
    }
  ]
}
```

## Resolving a ticket

```
bd-track triage resolve <ticket> (--bead | --reject REASON | --duplicate ID | --done OUTCOME)
                        [--title T] [--description D] [--type T] [--priority N]
                        [--inbox DIR] [--project NAME-OR-DIR] [--dry-run]
```

`<ticket>` is a file name (with or without `.md`) or a quick note's `inbox-<hash>` id.

| Option | `status` | `outcome` |
|---|---|---|
| `--bead` | `accepted` | `bead <id>` |
| `--reject REASON` | `rejected` | the reason |
| `--duplicate ID` | `duplicate` | `duplicate of <ID>` |
| `--done OUTCOME` | `done` | the outcome (e.g. a doc path) |

`resolved` is set to today. The keys are written into the ticket's frontmatter, replacing any
existing line for the same key and leaving the rest as written; a free-form ticket gets a new
block. The ticket then moves to `done/` under the same name. A quick note becomes its own
`done/<date>-<slug>.md`, keeping its stamp's `from`, `date`, `type` and `urgency`, and its
paragraph is removed from `inbox.md` (deleted once empty). bd-track never overwrites a file in
`done/`.

`--bead` runs `bd create` in the project with label `triage`, before anything moves; if it fails
the ticket stays open. Defaults, each overridable:

- **title**: the ticket's title; **description**: the ticket's body. A line
  `ticket: done/<file> (from <sender>, <date>)` is always appended.
- **type**: `bug`, `feature` and `task` map to themselves; `idea`, `question` and anything else
  become `task`.
- **priority**: urgency `high`/`normal`/`low` become 1/2/3; no urgency is 2.

An agent that rewrites the ticket into the project's bead style (as the `/triage` skill does)
passes `--title` and `--description`.

## The filing tool stays outside bd-track

Filing a ticket (the `ticket` command, or `echo >> inbox.md`) is not part of bd-track, and the
ticket format spec lives with the filing tool. bd-track implements only the beads layer.

Why:

- **The inbox must work without beads.** Projects without a `.beads/` database, and people
  without `bd` or Dolt, still file and read tickets. bd-track requires `bd`.
- **bd-track's scope is already wide**: time tracking, project setup, queues, maintenance.
- **Release coupling.** A change to filing or to the format should not need a bd-track release.

Folding filing in had real advantages, which this decision gives up: bd-track already keeps the
project list, handles beads, and has the Python, test and release setup. If the shared project
registry later moves into its own package ([A shared package](registry-schema.md#a-shared-package-deferred)),
`ticket` may ship as that package's CLI; it still would not be part of bd-track.

Because bd-track parses leniently, it does not have to follow every change to the recommended
format, and tickets written by hand or by other tools still work.

## Decision log

| Date | Decision |
|---|---|
| 2026-10-29 | The filing tool (`ticket`) stays standalone, outside bd-track, and owns the ticket format spec. bd-track implements only the beads layer and links to the spec (`bd-timew-4is.1`; MH, from the 2026-10-29 analysis in the triage ticket). |
| 2026-10-29 | bd-track parses tickets leniently: all frontmatter keys optional; free-form files and `inbox.md` paragraphs are valid tickets. |
| 2026-10-29 | The core is non-interactive primitives (`triage list --json`, `triage resolve`), with an interactive mode layered on top. |
| 2026-10-29 | A handled `inbox.md` paragraph is removed from `inbox.md` and written to `done/` as its own file with resolution keys. |
| 2026-10-30 | Resolution statuses are `accepted`, `rejected`, `duplicate` and `done`, the same set the `/triage` skill writes (`bd-timew-4is.3`). |
| 2026-10-30 | The default inbox root is `$BD_TRACK_INBOX_ROOT`, else `$XDG_STATE_HOME/triage`; `--inbox` overrides it per call (`bd-timew-4is.3`). |
| 2026-10-30 | `ask` is its own command (not a `resolve` option), since the ticket stays open; asking about a quick note promotes it to a ticket file (`bd-timew-4is.5`). |
| 2026-10-30 | The inbox is named by the project's registry name, falling back to the directory basename (`bd-timew-4is.7`). |
