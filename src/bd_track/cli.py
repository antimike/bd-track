"""Argparse setup and dispatch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bd_track.util import HelpFormatter, setup_logger

# Mirrors aggregate.POLICIES keys; duplicated here so argparse choices don't
# force an eager import of the aggregator at CLI-startup time.
_REPORT_POLICIES = ("billing", "machine", "wallclock")

__doc_summary__ = """\
bd-track: Beads time-tracking with project lifecycle management.

Resolves a Beads issue's labels to an external billing tuple
(client, case, svc) via a per-project sidecar (.beads/bd-track.yaml),
then appends start/stop events to a per-session, append-only JSONL log,
tagged accordingly. Concurrent sessions are safe — there is no single
global active interval to clobber (the timew backend's failure mode).

Also provides project maintenance commands (cleanup, init-project,
config init), Dolt server management, and named bead-execution queues.

Resolution order for billing tuple (first match wins):
  1. Per-issue  case:<string>  label (escape hatch)
  2. First matching rule in  patterns:  (regex on space-joined label list)
  3.  default:  block in the sidecar
  4. None for any unset field — display layer renders as "(none)"
"""


def _add_queue_parsers(sub: argparse._SubParsersAction) -> None:
    """Attach the unified ``queue <action>`` parser tree.

    All queue operations are subcommands of ``queue`` (push, unshift, pop,
    peek, list, remove, clear, clean, generate, prune). The flat top-level
    forms (``bd-track push``, etc.) were removed in favor of this shape so
    the analytical commands (``clean``, ``generate``, ``prune``) live next to
    the mechanical ones rather than scattered at the top level.
    """
    p_queue = sub.add_parser(
        "queue",
        help="Bead-execution queue operations (push, pop, list, clean, generate, prune, ...).",
        description=(
            "Maintain ordered, scoped queues of beads to work through. Default "
            "scope resolves from --scope flag → $BD_TRACK_SCOPE → 'default'. "
            "The queue file lives at .beads/queue.yaml."
        ),
        epilog=(
            "All <action> subcommands accept these common options:\n"
            "  --scope <name>      Queue scope (env: BD_TRACK_SCOPE; default: 'default')\n"
            "  --project-dir PATH  Project root containing .beads/ (default: active workspace)\n"
            "  --titles, -t        Show bead titles alongside IDs (slower; one bd call per bead)\n"
            "\n"
            "Use `bd-track queue <action> --help` for action-specific options."
        ),
        formatter_class=HelpFormatter,
    )
    qsub = p_queue.add_subparsers(dest="queue_action", required=True,
                                  title="queue actions", metavar="<action>")

    # Common args reused by every action.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--scope", metavar="<scope>", default=None,
        help="Queue scope name. Resolved: --scope flag → $BD_TRACK_SCOPE → 'default'.",
    )
    common.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project root containing .beads/. Defaults to active workspace.",
    )
    common.add_argument(
        "--titles", "-t", action="store_true", default=False,
        help="Show bead titles alongside IDs (fetches each bead from bd; slower).",
    )

    # -- push / unshift / pop / peek / list / remove / clear -----------------
    p_push = qsub.add_parser("push", parents=[common],
                             help="Append one or more beads to the tail of a queue scope.",
                             formatter_class=HelpFormatter)
    p_push.add_argument("ids", nargs="+", metavar="<id>")

    p_unshift = qsub.add_parser("unshift", parents=[common],
                                help="Prepend a bead to the head of a queue scope.",
                                formatter_class=HelpFormatter)
    p_unshift.add_argument("bead_id", metavar="<id>")

    qsub.add_parser("pop", parents=[common],
                    help="Remove and print the head bead of a queue scope.",
                    formatter_class=HelpFormatter)
    qsub.add_parser("peek", parents=[common],
                    help="Print the head bead of a queue scope without removing.",
                    formatter_class=HelpFormatter)
    qsub.add_parser("list", parents=[common],
                    help="List queue contents (all scopes, or one with --scope).",
                    formatter_class=HelpFormatter)

    p_remove = qsub.add_parser("remove", parents=[common],
                               help="Remove one or more beads from a queue scope.",
                               formatter_class=HelpFormatter)
    p_remove.add_argument("ids", nargs="+", metavar="<id>")

    qsub.add_parser("clear", parents=[common],
                    help="Empty a queue scope (--scope all clears every scope).",
                    formatter_class=HelpFormatter)

    # -- clean -- mechanical sweep of stale entries (closed / deferred) ------
    qsub.add_parser(
        "clean", parents=[common],
        help="Remove closed/deferred beads from queue scopes (no confirmation).",
        description=(
            "Mechanical sweep: queries bd for the status of every queue entry "
            "and drops anything closed or deferred. With no --scope, sweeps all "
            "scopes. Also invoked automatically after `bd-track stop`."
        ),
        formatter_class=HelpFormatter,
    )

    # -- generate -- build a queue from filters ------------------------------
    p_gen = qsub.add_parser(
        "generate", parents=[common],
        help="Generate a queue from `bd list` search/filter criteria.",
        description=(
            "Populate a queue scope from beads matching the given filters. "
            "Without --append, replaces the existing queue (with confirmation "
            "if non-empty)."
        ),
        formatter_class=HelpFormatter,
    )
    p_gen.add_argument(
        "--status", default="open,in_progress",
        help="Comma-separated bd statuses to include (default: open,in_progress).",
    )
    p_gen.add_argument(
        "--label", action="append", default=[], metavar="<label>",
        help="Require this label (repeatable; AND).",
    )
    p_gen.add_argument(
        "--label-any", action="append", default=[], metavar="<label>",
        help="Require at least one of these labels (repeatable; OR).",
    )
    p_gen.add_argument(
        "--label-pattern", default=None, metavar="<glob>",
        help="Filter by label glob pattern (e.g. 'area:*').",
    )
    p_gen.add_argument(
        "--keyword", "-k", default=None, metavar="<text>",
        help="Title substring match (case-insensitive).",
    )
    p_gen.add_argument(
        "--append", action="store_true", default=False,
        help="Append matches to existing queue instead of replacing.",
    )
    p_gen.add_argument(
        "--yes", "-y", action="store_true", default=False,
        help="Skip confirmation when replacing a non-empty queue.",
    )

    # -- prune -- analytical: identify issues, propose changes ---------------
    p_prune = qsub.add_parser(
        "prune", parents=[common],
        help="Identify stale/scope-mismatched/out-of-order entries; propose changes.",
        description=(
            "Analyse one or all queue scopes and surface: stale entries, scope "
            "mismatches (heuristic), dependency-ordering problems, and missing "
            "blockers. Prints proposals; prompts for confirmation before applying "
            "destructive changes (removal of stale entries). Move/reorder/add-"
            "before recommendations are reported but not auto-applied — they "
            "require manual judgment."
        ),
        formatter_class=HelpFormatter,
    )
    p_prune.add_argument(
        "--yes", "-y", action="store_true", default=False,
        help="Apply destructive changes (stale removal) without confirmation.",
    )


def get_cli_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bd-track",
        description=__doc_summary__,
        formatter_class=HelpFormatter,
    )
    parser.add_argument(
        "--loglevel", "-l",
        default="INFO",
        type=lambda v: v.strip().upper(),
        help="Logging verbosity (DEBUG, INFO, WARNING, ERROR).",
    )
    parser.add_argument(
        "--session-id", metavar="<id>", default=None,
        help="Explicit session id (overrides $BD_TRACK_SESSION_ID, "
             "$CLAUDE_CODE_SESSION_ID, and the current-session pointer).",
    )

    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--project", metavar="<slug-or-path>", dest="project_scope", default=None,
        help="Override project scope: resolve <slug-or-path> via repos.yaml name or path.",
    )
    scope_group.add_argument(
        "--global", action="store_true", dest="global_scope", default=False,
        help="Operate across all repos registered in repos.yaml.",
    )

    sub = parser.add_subparsers(
        dest="cmd", required=True,
        title="subcommands", metavar="<command>",
    )

    # -- start ---------------------------------------------------------------
    p_start = sub.add_parser(
        "start",
        help="Claim a Beads issue and start a tracking interval.",
        description=(
            "Resolves the billing tuple for <issue-id>, claims it in Beads "
            "(if not already in_progress), and appends a tagged start event. "
            "Ends this session's own open interval first (single-active-per-"
            "session) but never touches another session's interval."
        ),
        formatter_class=HelpFormatter,
    )
    p_start.add_argument("issue_id", metavar="<issue-id>")

    # -- stop ----------------------------------------------------------------
    p_stop = sub.add_parser(
        "stop",
        help="Stop this session's open interval(s).",
        description=(
            "Appends a stop event for this session's open interval(s); with "
            "<issue-id>, only the interval tagged with that bead. Unlike the old "
            "timew backend, a no-argument stop can never reach another session's "
            "interval — it only closes ULIDs in the caller's own session log. "
            "After stopping, runs `queue clean` automatically to drop any newly-"
            "closed beads from queue scopes; pass --no-clean to skip."
        ),
        formatter_class=HelpFormatter,
    )
    p_stop.add_argument(
        "issue_id", metavar="<issue-id>", nargs="?", default=None,
        help="Stop only the interval tagged with this bead ID (recommended).",
    )
    p_stop.add_argument(
        "--at", metavar="<datetime>", default=None,
        help="Override stop timestamp (natural language or ISO-8601; "
             "must be in the past and after the interval's start time).",
    )
    p_stop.add_argument(
        "--no-clean", action="store_false", dest="clean", default=True,
        help="Skip the post-stop queue sweep for closed/deferred beads.",
    )

    # -- show ----------------------------------------------------------------
    p_show = sub.add_parser(
        "show",
        help="Show the effective state of a single interval after folding all its events.",
        description=(
            "Locate an interval by ULID (or unique 8-character prefix), fold its "
            "start/stop/correction/cancel events, and display the result. Useful "
            "for verifying amend corrections or inspecting a specific interval's "
            "history."
        ),
        formatter_class=HelpFormatter,
    )
    p_show.add_argument("interval_id", metavar="<interval-id>",
                        help="Full interval ULID or unique 8-character prefix.")
    p_show.add_argument(
        "--format", dest="fmt", default="table",
        choices=("table", "json", "yaml", "csv", "tsv"),
        help="Output format (default: table).",
    )
    p_show.add_argument(
        "--pretty", action="store_true", default=False,
        help="2-space indent for JSON output; no-op for other formats.",
    )

    # -- cancel --------------------------------------------------------------
    p_cancel = sub.add_parser(
        "cancel",
        help="Drop an interval from aggregation by appending a cancel event.",
        description=(
            "Appends a cancel event for the given interval ULID (or unique "
            "8-character prefix). Cancelled intervals are excluded from all "
            "reports and totals; the original events are never deleted. Prompts "
            "for confirmation unless --yes is given."
        ),
        formatter_class=HelpFormatter,
    )
    p_cancel.add_argument("interval_id", metavar="<interval-id>",
                          help="Full interval ULID or unique 8-character prefix.")
    p_cancel.add_argument(
        "--yes", "-y", action="store_true", default=False,
        help="Skip confirmation prompt.",
    )

    # -- amend ---------------------------------------------------------------
    p_amend = sub.add_parser(
        "amend",
        help="Correct an existing interval's timestamps, tags, or provenance.",
        description=(
            "Appends a correction event for the given interval ULID (or unique "
            "8-character prefix), overriding any subset of start/stop timestamps, "
            "tags, and provenance fields. The aggregator applies corrections per-"
            "field with latest-eid-wins semantics — the original event is never "
            "modified. Corrections land in the current session's log regardless of "
            "which session originated the interval.\n\n"
            "Tag manipulation: three mutually-exclusive surfaces.\n"
            "  --add-tag/--remove-tag  explicit incremental edits (repeatable)\n"
            "  --tag +foo / --tag ~foo  shorthand; ~ avoids argparse - collision\n"
            "  --tags foo,bar,...       wholesale replace (comma-delimited)\n"
            "--tags is mutually exclusive with --add-tag/--remove-tag/--tag."
        ),
        formatter_class=HelpFormatter,
    )
    p_amend.add_argument("interval_id", metavar="<interval-id>",
                         help="Full interval ULID or unique 8-character prefix.")
    p_amend.add_argument(
        "--start", metavar="<datetime>", default=None,
        help="Override start time (natural language or ISO-8601).",
    )
    p_amend.add_argument(
        "--stop", metavar="<datetime>", default=None,
        help="Override stop time (natural language or ISO-8601).",
    )
    p_amend.add_argument(
        "--add-tag", dest="add_tags", action="append", default=[], metavar="<tag>",
        help="Add a tag (repeatable; mutually exclusive with --tags).",
    )
    p_amend.add_argument(
        "--remove-tag", dest="remove_tags", action="append", default=[], metavar="<tag>",
        help="Remove a tag (repeatable; mutually exclusive with --tags).",
    )
    p_amend.add_argument(
        "--tag", dest="tag_ops", action="append", default=[], metavar="+tag|~tag",
        help="Add (+tag) or remove (~tag) a tag shorthand (repeatable; "
             "mutually exclusive with --tags).",
    )
    p_amend.add_argument(
        "--tags", dest="tags_csv", default=None, metavar="<tag,tag,...>",
        help="Wholesale-replace the tag list (comma-delimited; "
             "mutually exclusive with --add-tag/--remove-tag/--tag).",
    )
    p_amend.add_argument("--actor", default=None, metavar="<str>",
                         help="Override actor field.")
    p_amend.add_argument("--role", default=None, metavar="<str>",
                         help="Override role field.")
    p_amend.add_argument("--group-id", dest="group_id", default=None, metavar="<str>",
                         help="Override group_id field.")

    # -- switch --------------------------------------------------------------
    p_switch = sub.add_parser(
        "switch",
        help="Stop current interval and start a new one on <issue-id>.",
        formatter_class=HelpFormatter,
    )
    p_switch.add_argument("issue_id", metavar="<issue-id>")
    p_switch.add_argument(
        "--from", dest="from_issue_id", metavar="<issue-id>", default=None,
        help="Stop only the interval tagged with this bead ID (recommended).",
    )

    # -- list ----------------------------------------------------------------
    p_list = sub.add_parser(
        "list",
        help="List intervals with filtering and sorting (discovery before show/amend/cancel).",
        description=(
            "Walk the JSONL log and emit an itemized list of intervals with their "
            "ULIDs prominently shown. Filters narrow the result; --from/--to accept "
            "natural language or ISO-8601. Default (no range flags): intervals whose "
            "start falls on today."
        ),
        formatter_class=HelpFormatter,
    )
    p_list.add_argument(
        "--from", dest="from_dt", metavar="<datetime>", default=None,
        help="Include intervals starting at or after this time (natural language or ISO-8601).",
    )
    p_list.add_argument(
        "--to", dest="to_dt", metavar="<datetime>", default=None,
        help="Include intervals starting at or before this time (natural language or ISO-8601).",
    )
    p_list.add_argument(
        "--bead", metavar="<id>", default=None,
        help="Filter to intervals tagged with this bead ID.",
    )
    p_list.add_argument(
        "--actor", metavar="<str>", default=None,
        help="Filter by actor field.",
    )
    p_list.add_argument(
        "--status", dest="status_filter", metavar="open|closed|cancelled",
        action="append", default=None,
        choices=("open", "closed", "cancelled"),
        help="Include only intervals with this status (repeatable; default: all).",
    )
    p_list.add_argument(
        "--session", dest="session_filter", metavar="<uuid-prefix>", default=None,
        help="Filter to intervals from this session (full UUID or unique prefix).",
    )
    p_list.add_argument(
        "--format", dest="fmt", default="table",
        choices=("table", "json", "yaml", "csv", "tsv"),
        help="Output format (default: table).",
    )
    p_list.add_argument(
        "--pretty", action="store_true", default=False,
        help="2-space indent for JSON output; no-op for other formats.",
    )
    p_list.add_argument(
        "--sort", dest="sort_by", default="start",
        choices=("start", "stop", "duration"),
        help="Sort field (default: start).",
    )
    p_list.add_argument(
        "--desc", action="store_true", default=False,
        help="Reverse sort order.",
    )

    # -- status --------------------------------------------------------------
    sub.add_parser(
        "status",
        help="Show the active interval's bead, billing tuple, and elapsed time.",
        formatter_class=HelpFormatter,
    )

    # -- active --------------------------------------------------------------
    sub.add_parser(
        "active",
        help="Show ALL open intervals across ALL sessions (the multi-active view).",
        description=(
            "List every open (started, not yet stopped) interval across every "
            "session in the log — the concurrent-tracking view the timew backend "
            "structurally could not provide. The caller's own session is marked '*'."
        ),
        formatter_class=HelpFormatter,
    )

    # -- report --------------------------------------------------------------
    p_report = sub.add_parser(
        "report",
        help="Aggregate closed intervals by a dimension under a named policy.",
        description=(
            "Walk the JSONL log, pair + correct intervals, and total closed time "
            "grouped by an output dimension. Open/stale intervals are excluded "
            "from totals (surfaced via `active`)."
        ),
        formatter_class=HelpFormatter,
    )
    p_report.add_argument(
        "--by", dest="group_by", default="bead", metavar="<dim>",
        help="Output grouping: bead|session|actor|role|group_id or a tag key "
             "(client/case/svc/...). Default: bead.",
    )
    p_report.add_argument(
        "--policy", dest="policy_name", default="billing",
        choices=sorted(_REPORT_POLICIES), metavar="<policy>",
        help="Aggregation policy: billing|machine|wallclock. Default: billing.",
    )
    p_report.add_argument("--since", default=None, metavar="<YYYY-MM-DD>",
                          help="Only intervals starting on/after this date.")
    p_report.add_argument("--until", default=None, metavar="<YYYY-MM-DD>",
                          help="Only intervals starting on/before this date.")

    # -- resolve -------------------------------------------------------------
    p_resolve = sub.add_parser(
        "resolve",
        help="Print the resolved billing tuple without starting tracking.",
        formatter_class=HelpFormatter,
    )
    p_resolve.add_argument("issue_id", metavar="<issue-id>")

    # -- session -------------------------------------------------------------
    p_session = sub.add_parser(
        "session",
        help="Inspect session identity (currently: 'session current' only).",
        description=(
            "Session identity for the JSONL timetracking backend. Resolution "
            "precedence: --session-id → $BD_TRACK_SESSION_ID → "
            "$CLAUDE_CODE_SESSION_ID → current-session pointer → generated id."
        ),
        formatter_class=HelpFormatter,
    )
    p_session_sub = p_session.add_subparsers(dest="session_action", required=True)
    p_session_current = p_session_sub.add_parser(
        "current",
        help="Resolve and print the session id for this invocation context.",
        formatter_class=HelpFormatter,
    )
    p_session_current.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project root containing .beads/. Defaults to active workspace.",
    )

    # -- cleanup -------------------------------------------------------------
    p_cleanup = sub.add_parser(
        "cleanup",
        help="Compact Dolt history and run GC; optionally commit first.",
        formatter_class=HelpFormatter,
    )
    p_cleanup.add_argument("--days", type=int, default=7,
                          help="Retain this many days of Dolt commit history.")
    p_cleanup.add_argument("--commit", action="store_true",
                          help="Run  bd dolt commit  before compacting.")
    p_cleanup.add_argument("--project-dir", type=Path, default=None,
                          help="Project root containing .beads/. Defaults to active workspace.")

    # -- init-project --------------------------------------------------------
    p_init = sub.add_parser(
        "init-project",
        help="Configure a Beads project and register it for auto-cleanup.",
        formatter_class=HelpFormatter,
    )
    p_init.add_argument("--path", type=Path, default=None)
    p_init.add_argument("--days", type=int, default=None)
    p_init.add_argument("--commit-cadence", type=str, default=None)
    p_init.add_argument("--statuses", type=str, default=None)
    p_init.add_argument("--hooks", action=argparse.BooleanOptionalAction, default=None)
    p_init.add_argument("--no-git-ops", action="store_true")
    p_init.add_argument("--install-systemd", action="store_true")
    p_init.add_argument("--check-interval", type=str, default="daily")
    p_init.add_argument("--idle-stop-hours", type=int, default=None)
    p_init.add_argument("--server", action=argparse.BooleanOptionalAction, default=None)
    p_init.add_argument("--dolt-user", type=str, default=None)
    p_init.add_argument("--pass-path", type=str, default=None)
    p_init.add_argument("--yes", "-y", action="store_true")
    p_init.add_argument(
        "--bootstrap", action=argparse.BooleanOptionalAction, default=True,
        help="Run `bd init` first if .beads/ is missing (default: enabled).",
    )
    p_init.add_argument(
        "--sandbox", action=argparse.BooleanOptionalAction, default=True,
        help="Forward --sandbox to bd init (default: enabled — disables auto-sync "
             "during init, the empirically-safe setting).",
    )
    p_init.add_argument(
        "--prefix", type=str, default=None,
        help="Issue prefix for bd init (e.g. 'PROJ'). Forwarded only when bootstrapping.",
    )
    p_init.add_argument(
        "--agents-profile", type=str, default="full",
        choices=["minimal", "full"],
        help="bd init --agents-profile value (default: full — full bd command reference "
             "in the generated AGENTS.md).",
    )

    # -- config init ---------------------------------------------------------
    p_config = sub.add_parser(
        "config",
        help="Manage bd-track configuration (currently: 'config init' only).",
        formatter_class=HelpFormatter,
    )
    p_config_sub = p_config.add_subparsers(dest="config_action", required=True)
    p_config_init = p_config_sub.add_parser(
        "init",
        help="Scaffold a per-project .beads/bd-track.yaml from the packaged template.",
        formatter_class=HelpFormatter,
    )
    p_config_init.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project root containing .beads/. Defaults to active workspace.",
    )

    # -- idle-stop -----------------------------------------------------------
    p_idle = sub.add_parser(
        "idle-stop",
        help="Stop Dolt servers idle longer than a threshold.",
        formatter_class=HelpFormatter,
    )
    p_idle.add_argument("--hours", type=float, default=4.0)

    # -- run-service / servers / server-stop ---------------------------------
    sub.add_parser("run-service", help="[internal] systemd cleanup service entrypoint",
                   formatter_class=HelpFormatter)
    sub.add_parser("servers", help="List registered repos and Dolt server status.",
                   formatter_class=HelpFormatter)
    p_server_stop = sub.add_parser(
        "server-stop",
        help="Gracefully stop the Dolt server for one or all registered repos.",
        formatter_class=HelpFormatter,
    )
    p_server_stop.add_argument("--path", type=Path, default=None)

    # -- migrate -------------------------------------------------------------
    p_migrate = sub.add_parser(
        "migrate",
        help=(
            "One-way migrations: 'migrate rename' and 'migrate import' for "
            "the bd-timew → bd-track cutover."
        ),
        description=(
            "Migration subcommands. 'rename' completes the bd-timew → bd-track "
            "cutover by renaming on-disk artifacts (config/cache/state dirs, the "
            ".beads sidecar + session logs, and BD_TIMEW_* env vars) so the "
            "read-fallback shims stop firing. 'import' replays a Timewarrior "
            "export into the JSONL event log."
        ),
        formatter_class=HelpFormatter,
    )
    p_migrate_sub = p_migrate.add_subparsers(dest="migrate_action", required=True,
                                             title="migrate actions", metavar="<action>")
    p_migrate_rename = p_migrate_sub.add_parser(
        "rename",
        help="Rename bd-timew on-disk artifacts to bd-track (dry-run by default).",
        description=(
            "Rename the global ~/.config|cache|state|local-share/bd-timew dirs, "
            "this project's .beads/bd-timew.yaml sidecar + <beads>/bd-timew session "
            "logs, and rewrite BD_TIMEW_* → BD_TRACK_* in .envrc/.env/.envrc.local/"
            "mise.toml. Dry-run by default; chezmoi-managed home/dotfile targets are "
            "skipped with a warning (use `chezmoi apply` for those)."
        ),
        formatter_class=HelpFormatter,
    )
    p_migrate_rename.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project root containing .beads/. Defaults to active workspace.",
    )
    p_migrate_rename.add_argument(
        "--all-repos", action="store_true", default=False,
        help="Sweep every repo registered in repos.yaml, not just the current one.",
    )
    p_migrate_rename.add_argument(
        "--apply", action="store_true", default=False,
        help="Perform the migration (default is a dry-run preview).",
    )
    p_migrate_rename.add_argument(
        "--no-backup", action="store_false", dest="backup", default=True,
        help="Skip the per-file .bak backup before rewriting env files.",
    )

    p_migrate_import = p_migrate_sub.add_parser(
        "import",
        help="Import existing Timewarrior intervals into the JSONL log (dry-run by default).",
        description=(
            "One-shot: read a Timewarrior export and replay each closed, bead-tagged "
            "interval as a start+stop event pair, preserving the historical "
            "timestamps. Open and bead-less intervals are skipped. Idempotent — a "
            "re-run skips already-imported intervals. Dry-run by default. Imported "
            "events land in a dedicated 'imported-timew' session log."
        ),
        formatter_class=HelpFormatter,
    )
    p_migrate_import.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project root containing .beads/. Defaults to active workspace.",
    )
    p_migrate_import.add_argument(
        "--from-file", type=Path, default=None,
        help="Read a saved `timew export` JSON file instead of invoking timew.",
    )
    p_migrate_import.add_argument(
        "--apply", action="store_true", default=False,
        help="Perform the import (default is a dry-run preview).",
    )

    # -- queue (parent) + push/unshift/pop/peek/list/remove/clear/clean/generate/prune
    _add_queue_parsers(sub)

    return parser.parse_args()


def _dispatch_queue(args: argparse.Namespace) -> None:
    """Route ``bd-track queue <action>`` to the right queue.py function."""
    action = args.queue_action

    if action in ("push", "unshift", "pop", "peek", "list", "remove", "clear"):
        from bd_track.queue import cmd_queue
        if action == "push":
            ids = args.ids
        elif action == "unshift":
            ids = [args.bead_id]
        elif action == "remove":
            ids = args.ids
        else:
            ids = None
        cmd_queue(
            action, ids=ids, scope_arg=args.scope,
            project_dir=args.project_dir, titles=args.titles,
        )
        return

    if action == "clean":
        from bd_track.queue import cmd_clean
        cmd_clean(scope_arg=args.scope, project_dir=args.project_dir)
        return

    if action == "generate":
        from bd_track.queue import cmd_generate
        statuses = [s.strip() for s in args.status.split(",") if s.strip()]
        cmd_generate(
            scope_arg=args.scope,
            project_dir=args.project_dir,
            statuses=statuses,
            labels_all=args.label,
            labels_any=args.label_any,
            label_pattern=args.label_pattern,
            keyword=args.keyword,
            append=args.append,
            yes=args.yes,
        )
        return

    if action == "prune":
        from bd_track.queue import cmd_prune
        cmd_prune(
            scope_arg=args.scope,
            project_dir=args.project_dir,
            yes=args.yes,
        )
        return


def _resolve_project_scope(args: argparse.Namespace) -> "Path | None":
    """Return the project_dir implied by ``--project``, or ``None``."""
    if args.project_scope:
        from bd_track.util import resolve_project_dir
        return resolve_project_dir(args.project_scope)
    return None


def main_deprecated() -> None:
    """Entrypoint for the deprecated ``bd-timew`` alias: warn, then dispatch.

    The package + executable were renamed to ``bd-track`` (the timew backend is
    gone). This alias keeps existing callers working through the transition.
    """
    print(
        "warning: `bd-timew` is deprecated and will be removed in a future "
        "release; use `bd-track` instead. Run `bd-track migrate rename` to "
        "migrate config/env naming.",
        file=sys.stderr,
    )
    main()


def main() -> None:
    """Parse args and dispatch to the appropriate cmd_* function."""
    args = get_cli_arguments()
    setup_logger(
        args.loglevel,
        enable_file_handler=args.cmd in ("cleanup", "run-service", "idle-stop"),
    )

    # Late imports keep CLI startup snappy and avoid loading heavyweight modules
    # for unrelated subcommands.
    if args.cmd == "start":
        from bd_track.track import cmd_start
        cmd_start(args.issue_id, session_id=args.session_id)
    elif args.cmd == "stop":
        from bd_track.track import cmd_stop
        cmd_stop(args.issue_id, clean=args.clean, session_id=args.session_id, at=args.at)
    elif args.cmd == "show":
        from bd_track.track import cmd_show
        cmd_show(args.interval_id, fmt=args.fmt, pretty=args.pretty,
                 session_id=args.session_id,
                 project_dir=_resolve_project_scope(args),
                 global_scope=args.global_scope)
    elif args.cmd == "cancel":
        from bd_track.track import cmd_cancel
        cmd_cancel(args.interval_id, yes=args.yes, session_id=args.session_id,
                   project_dir=_resolve_project_scope(args))
    elif args.cmd == "amend":
        from bd_track.track import cmd_amend
        cmd_amend(
            args.interval_id,
            start=args.start,
            stop=args.stop,
            add_tags=args.add_tags,
            remove_tags=args.remove_tags,
            tag_ops=args.tag_ops,
            tags_csv=args.tags_csv,
            actor=args.actor,
            role=args.role,
            group_id=args.group_id,
            session_id=args.session_id,
            project_dir=_resolve_project_scope(args),
        )
    elif args.cmd == "switch":
        from bd_track.track import cmd_switch
        cmd_switch(args.issue_id, from_issue_id=args.from_issue_id,
                   session_id=args.session_id)
    elif args.cmd == "list":
        from bd_track.track import cmd_list
        cmd_list(
            from_dt=args.from_dt,
            to_dt=args.to_dt,
            bead=args.bead,
            actor=args.actor,
            statuses=args.status_filter,
            session_filter=args.session_filter,
            fmt=args.fmt,
            pretty=args.pretty,
            sort_by=args.sort_by,
            desc=args.desc,
            project_dir=_resolve_project_scope(args),
            global_scope=args.global_scope,
            session_id=args.session_id,
        )
    elif args.cmd == "status":
        from bd_track.track import cmd_status
        cmd_status(session_id=args.session_id)
    elif args.cmd == "active":
        from bd_track.track import cmd_active
        cmd_active(session_id=args.session_id,
                   project_dir=_resolve_project_scope(args),
                   global_scope=args.global_scope)
    elif args.cmd == "report":
        from bd_track.track import cmd_report
        cmd_report(group_by=args.group_by, policy_name=args.policy_name,
                   since=args.since, until=args.until, session_id=args.session_id,
                   project_dir=_resolve_project_scope(args),
                   global_scope=args.global_scope)
    elif args.cmd == "resolve":
        from bd_track.track import cmd_start
        cmd_start(args.issue_id, dry_run=True, session_id=args.session_id)
    elif args.cmd == "session":
        if args.session_action == "current":
            from bd_track.session import cmd_session_current
            cmd_session_current(project_dir=args.project_dir, explicit=args.session_id)
    elif args.cmd == "cleanup":
        from bd_track.project import cmd_cleanup
        cmd_cleanup(args.days, commit=args.commit, project_dir=args.project_dir)
    elif args.cmd == "init-project":
        from bd_track.project import cmd_init_project
        cmd_init_project(
            path=args.path, days=args.days, commit_cadence=args.commit_cadence,
            statuses_arg=args.statuses, hooks_arg=args.hooks,
            no_git_ops=args.no_git_ops, install_systemd=args.install_systemd,
            check_interval=args.check_interval, idle_stop_hours=args.idle_stop_hours,
            server_mode=args.server, dolt_user=args.dolt_user,
            pass_path=args.pass_path, yes=args.yes,
            bootstrap=args.bootstrap, sandbox=args.sandbox,
            prefix=args.prefix, agents_profile=args.agents_profile,
        )
    elif args.cmd == "config":
        if args.config_action == "init":
            from bd_track.project import cmd_config_init
            cmd_config_init(project_dir=args.project_dir)
    elif args.cmd == "idle-stop":
        from bd_track.server import cmd_idle_stop
        cmd_idle_stop(args.hours)
    elif args.cmd == "run-service":
        from bd_track.project import cmd_run_service
        cmd_run_service()
    elif args.cmd == "servers":
        from bd_track.server import cmd_servers
        cmd_servers()
    elif args.cmd == "server-stop":
        from bd_track.server import cmd_server_stop
        cmd_server_stop(args.path)
    elif args.cmd == "migrate":
        if args.migrate_action == "rename":
            from bd_track.migrate import cmd_migrate_rename
            cmd_migrate_rename(
                project_dir=args.project_dir, all_repos=args.all_repos,
                apply=args.apply, backup=args.backup,
            )
        elif args.migrate_action == "import":
            from bd_track.migrate import cmd_migrate_import
            cmd_migrate_import(
                project_dir=args.project_dir, from_file=args.from_file,
                apply=args.apply,
            )
    elif args.cmd == "queue":
        _dispatch_queue(args)
