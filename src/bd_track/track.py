"""Command implementations — start/stop/switch/status/active/report/resolve.

Re-wired off Timewarrior onto the append-only JSONL event log (bd-timew-9hn).
There is no ambient "active interval" anymore: every command re-derives its
view by reading + folding the per-session logs (``aggregate``). This is what
makes concurrent sessions safe — a session can only ever close an interval ULID
it can see, and ``start``/``stop`` touch only the caller's own session.

Single-active-per-session: ``start`` first stops the calling session's own open
interval(s), preserving the timew muscle-memory ("starting a new thing ends the
old one") without ever reaching across sessions — the property timew lacked.

``pause --all`` / ``resume --all`` are the deliberate exception: idle and sleep
hooks run outside any session, so they stop and reopen other sessions'
intervals, writing each event into that interval's own session log.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from bd_track.aggregate import (
    POLICIES,
    Interval,
    load_intervals,
    open_intervals,
    paused_intervals,
    report,
)
from bd_track.billing import get_issue, load_sidecar, resolve_tuple
from bd_track.events import (
    _UNSET,
    cancel_interval,
    correct_interval,
    log_dir,
    resolve_provenance,
    start_interval,
    stop_interval,
)
from bd_track.session import resolve_session_id
from bd_track.util import find_beads_dir, record_activity, root_log

# Billing-tuple fields surfaced in start/status/active output. The *shape* is
# sidecar-defined (bd-timew-qny); these are the BOCO-NetSuite defaults the
# display layer knows how to label. Stored flat as "client:foo" tags.
_TUPLE_KEYS = ("client", "case", "svc")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tags(tuple_: dict) -> list[str]:
    """Flat billing tags from a resolved tuple (``key:value`` + billable flag)."""
    tags = [f"{key}:{val}" for key in _TUPLE_KEYS if (val := tuple_.get(key))]
    if not tuple_.get("svc") or tuple_.get("svc") == "none":
        tags.append("billable:false")
    return tags


def _tuple_from_tags(tags: list[str]) -> dict[str, str | None]:
    """Reconstruct the display tuple from flat ``key:value`` tags."""
    out: dict[str, str | None] = {k: None for k in _TUPLE_KEYS}
    for tag in tags:
        for key in _TUPLE_KEYS:
            if tag.startswith(f"{key}:"):
                out[key] = tag[len(f"{key}:") :]
    return out


def _format_duration(delta: dt.timedelta) -> str:
    total = int(delta.total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _elapsed(start_iso: str) -> str:
    start = dt.datetime.fromisoformat(start_iso)
    now = dt.datetime.now().astimezone() if start.tzinfo else dt.datetime.now()
    return _format_duration(now - start)


def _project_root(project_dir: Path | None = None) -> Path:
    """Project root (parent of the active ``.beads/`` dir)."""
    return find_beads_dir(project_dir).parent


def _project_roots(project_dir: Path | None, global_scope: bool) -> list[Path]:
    """Resolve the set of project roots implied by scope flags."""
    if global_scope:
        from bd_track.util import all_project_dirs

        return all_project_dirs()
    return [_project_root(project_dir)]


def _session_open(
    session_id: str,
    project_root: Path,
    *,
    bead: str | None = None,
) -> list[Interval]:
    """Open intervals belonging to ``session_id`` (optionally for one bead)."""
    opens = [
        iv
        for iv in open_intervals(load_intervals(log_dir(project_root)))
        if iv.session == session_id
    ]
    if bead:
        opens = [iv for iv in opens if iv.bead == bead]
    return opens


def _session_paused(
    session_id: str,
    project_root: Path,
    *,
    bead: str | None = None,
) -> list[Interval]:
    """This session's interval awaiting ``resume`` (at most one; optionally for one bead)."""
    return [
        iv
        for iv in paused_intervals(load_intervals(log_dir(project_root)))
        if iv.session == session_id and (bead is None or iv.bead == bead)
    ]


def _caller_session(root: Path, session_id: str | None) -> str | None:
    """The caller's session id in ``root``, or None when it cannot be resolved."""
    try:
        return resolve_session_id(root, explicit=session_id)
    except SystemExit:
        return None


def _select(
    ivs: list[Interval], *, beads: list[str], all_sessions: bool, sid: str | None
) -> list[Interval]:
    """Filter pause/resume candidates by session scope and bead ids."""
    if not all_sessions:
        ivs = [iv for iv in ivs if sid is not None and iv.session == sid]
    if beads:
        ivs = [iv for iv in ivs if iv.bead in beads]
    return ivs


# ---------------------------------------------------------------------------
# start / stop / switch
# ---------------------------------------------------------------------------


def cmd_start(
    issue_id: str,
    *,
    dry_run: bool = False,
    session_id: str | None = None,
    project_dir: Path | None = None,
) -> None:
    """Resolve the billing tuple, claim the bead, append a ``start`` event.

    ``bd`` runs in the project root, so with ``project_dir`` the claim lands in the same
    project as the interval whatever the cwd.
    """
    beads_dir = find_beads_dir(project_dir)
    project_root = beads_dir.parent
    sidecar = load_sidecar(beads_dir)
    issue = get_issue(issue_id, cwd=project_root)
    labels: list[str] = issue.get("labels") or []
    tuple_ = resolve_tuple(labels, sidecar)

    root_log.info("Issue:  %s  %s", issue.get("id", "?"), issue.get("title", ""))
    root_log.info("Labels: %s", ", ".join(labels) or "(none)")
    for key in _TUPLE_KEYS:
        root_log.info("%-7s %s", key.capitalize() + ":", tuple_[key] or "(none)")

    if dry_run:
        return

    sid = resolve_session_id(project_root, explicit=session_id)

    # Single-active-per-session: end this session's own open interval(s) first.
    for iv in _session_open(sid, project_root):
        stop_interval(iv.interval, session_id=sid, project_dir=project_root)
        root_log.info("Stopped prior interval %s (%s)", iv.bead or "?", _elapsed(iv.start))

    tags = _build_tags(tuple_)
    provenance = resolve_provenance()
    interval = start_interval(
        issue_id,
        tags,
        session_id=sid,
        project_dir=project_root,
        **provenance,
    )
    root_log.info("Started interval %s  [session %s]", interval[:8], sid)

    if issue.get("status") != "in_progress":
        from bd_track.util import run

        run(["bd", "update", issue_id, "--claim"], check=False, cwd=project_root)

    record_activity(str(project_root.resolve()))


def cmd_stop(
    issue_id: str | None = None,
    *,
    clean: bool = True,
    session_id: str | None = None,
    at: str | None = None,
    project_dir: Path | None = None,
) -> None:
    """Append a ``stop`` event for this session's open interval(s).

    With ``issue_id`` given, stops only the interval(s) tagged with that bead.
    Unlike the old timew backend, a no-argument stop can *never* reach another
    session's interval — it only closes ULIDs in the caller's own session log.

    ``at`` overrides the stop timestamp (natural language or ISO-8601 string).
    Must be in the past and after the interval's own start time.

    When ``clean`` is True (default), sweeps closed beads from queue scopes
    afterward (``--no-clean`` to skip). Deferred beads stay queued.
    """
    project_root = _project_root(project_dir)
    sid = resolve_session_id(project_root, explicit=session_id)
    record_activity(str(project_root.resolve()))

    at_dt: dt.datetime | None = None
    if at is not None:
        from bd_track.util import parse_datetime

        try:
            at_dt = parse_datetime(at)
        except ValueError as exc:
            root_log.error("--at: %s", exc)
            sys.exit(1)
        if at_dt > dt.datetime.now().astimezone():
            root_log.error("--at timestamp is in the future: %s", at_dt.isoformat())
            sys.exit(1)

    opens = _session_open(sid, project_root, bead=issue_id)
    if not opens:
        paused = _session_paused(sid, project_root, bead=issue_id)
        for iv in paused:
            correct_interval(iv.interval, session_id=sid, paused=False, project_dir=project_root)
            root_log.info("Cleared pause on %s  %s", iv.bead or "(no bead)", iv.interval[:8])
        if not paused:
            scope = f" for {issue_id}" if issue_id else ""
            root_log.info("no active interval%s in this session (%s)", scope, sid)
    for iv in opens:
        if at_dt is not None and iv.start is not None:
            start_dt = dt.datetime.fromisoformat(iv.start)
            if at_dt < start_dt:
                root_log.error(
                    "--at %s is before interval start %s",
                    at_dt.isoformat(),
                    iv.start,
                )
                sys.exit(1)
        ts = at_dt.isoformat(timespec="seconds") if at_dt is not None else None
        stop_interval(iv.interval, session_id=sid, project_dir=project_root, ts=ts)
        if at_dt is not None and iv.start is not None:
            elapsed = _format_duration(at_dt - dt.datetime.fromisoformat(iv.start))
        else:
            elapsed = _elapsed(iv.start)
        root_log.info("Stopped %s  %s  (%s)", iv.bead or "(no bead)", iv.interval[:8], elapsed)

    if clean:
        from bd_track.queue import STOP_SWEEP_STATUSES, cmd_clean

        try:
            cmd_clean(quiet=True, statuses=STOP_SWEEP_STATUSES, project_dir=project_root)
        except SystemExit:
            pass


def cmd_switch(
    issue_id: str,
    *,
    from_issue_id: str | None = None,
    session_id: str | None = None,
    project_dir: Path | None = None,
) -> None:
    """Stop the current interval and start one on ``issue_id`` (composition)."""
    cmd_stop(from_issue_id, clean=False, session_id=session_id, project_dir=project_dir)
    cmd_start(issue_id, session_id=session_id, project_dir=project_dir)


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


def cmd_pause(
    beads: list[str] | None = None,
    *,
    all_sessions: bool = False,
    dry_run: bool = False,
    session_id: str | None = None,
    project_dir: Path | None = None,
    global_scope: bool = False,
) -> None:
    """Stop open intervals with ``paused: true`` so ``resume`` can reopen them.

    Default scope is this session; ``all_sessions`` widens it to every session
    (the idle/sleep-hook form), ``beads`` narrows it. Nothing to pause is not an
    error: hooks call this blindly.
    """
    count = 0
    for root in _project_roots(project_dir, global_scope):
        sid = _caller_session(root, session_id)
        opens = open_intervals(load_intervals(log_dir(root)))
        for iv in _select(opens, beads=beads or [], all_sessions=all_sessions, sid=sid):
            count += 1
            if not dry_run:
                stop_interval(iv.interval, session_id=iv.session, paused=True, project_dir=root)
            root_log.info(
                "%s %s  %s  (%s)  [session %s]",
                "Would pause" if dry_run else "Paused",
                iv.bead or "(no bead)",
                iv.interval[:8],
                _elapsed(iv.start),
                iv.session,
            )
    if not count:
        root_log.info("nothing to pause: no matching open interval")


def cmd_resume(
    beads: list[str] | None = None,
    *,
    all_sessions: bool = False,
    dry_run: bool = False,
    session_id: str | None = None,
    project_dir: Path | None = None,
    global_scope: bool = False,
) -> None:
    """Open a new interval for each paused one, same session, bead, tags and provenance.

    Scope as for ``cmd_pause``. The bead is not re-claimed and the tuple is not
    re-resolved: the resumed interval continues the paused one.
    """
    count = 0
    for root in _project_roots(project_dir, global_scope):
        sid = _caller_session(root, session_id)
        paused = paused_intervals(load_intervals(log_dir(root)))
        for iv in _select(paused, beads=beads or [], all_sessions=all_sessions, sid=sid):
            count += 1
            new = "-"
            if not dry_run:
                new = start_interval(
                    iv.bead,
                    iv.tags,
                    session_id=iv.session,
                    group_id=iv.group_id,
                    actor=iv.actor,
                    role=iv.role,
                    resumed_from=iv.interval,
                    project_dir=root,
                )[:8]
            root_log.info(
                "%s %s  %s -> %s  [session %s]",
                "Would resume" if dry_run else "Resumed",
                iv.bead or "(no bead)",
                iv.interval[:8],
                new,
                iv.session,
            )
    if not count:
        root_log.info("nothing to resume: no matching paused interval")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _find_interval(interval_id: str, roots: list[Path]) -> tuple[str, Interval]:
    """Locate an interval by full ULID or unique prefix across project roots."""
    prefix = interval_id.upper()
    matches: dict[str, Interval] = {}
    for root in roots:
        for iv in load_intervals(log_dir(root)):
            if iv.interval.startswith(prefix):
                matches[iv.interval] = iv
    if not matches:
        root_log.error("interval %s not found", interval_id)
        sys.exit(1)
    if len(matches) > 1:
        root_log.error("interval prefix %s is ambiguous (%d matches)", interval_id, len(matches))
        sys.exit(1)
    return next(iter(matches.items()))


def _interval_to_dict(iv: Interval) -> dict:
    return {
        "interval": iv.interval,
        "status": iv.status,
        "bead": iv.bead,
        "session": iv.session,
        "start": iv.start,
        "stop": iv.stop,
        "duration": str(iv.duration) if iv.stop else None,
        "tags": iv.tags,
        "actor": iv.actor,
        "role": iv.role,
        "group_id": iv.group_id,
    }


def cmd_show(
    interval_id: str,
    *,
    fmt: str = "table",
    pretty: bool = False,
    session_id: str | None = None,
    project_dir: Path | None = None,
    global_scope: bool = False,
) -> None:
    """Show effective interval state after folding all its events."""
    roots = _project_roots(project_dir, global_scope)
    _, iv = _find_interval(interval_id, roots)
    data = _interval_to_dict(iv)

    if fmt == "json":
        import json

        indent = 2 if pretty else None
        print(json.dumps(data, indent=indent, ensure_ascii=False))
        return

    if fmt == "yaml":
        import yaml

        print(yaml.dump(data, default_flow_style=False, allow_unicode=True), end="")
        return

    if fmt in ("csv", "tsv"):
        import csv
        import io

        sep = "\t" if fmt == "tsv" else ","
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=sep)
        w.writerow(data.keys())
        w.writerow(
            [
                "|".join(v) if isinstance(v, list) else ("" if v is None else str(v))
                for v in data.values()
            ]
        )
        print(buf.getvalue(), end="")
        return

    # table (default) — Rich
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for key, val in data.items():
        if isinstance(val, list):
            display = ", ".join(val) if val else "(none)"
        else:
            display = str(val) if val is not None else "(none)"
        table.add_row(key, display)
    console.print(table)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def cmd_cancel(
    interval_id: str,
    *,
    yes: bool = False,
    session_id: str | None = None,
    project_dir: Path | None = None,
) -> None:
    """Drop an interval from aggregation by appending a cancel event."""
    project_root = _project_root(project_dir)

    # Find interval by full ULID or unique prefix.
    all_intervals = {iv.interval: iv for iv in load_intervals(log_dir(project_root))}
    prefix = interval_id.upper()
    matches = {ulid: iv for ulid, iv in all_intervals.items() if ulid.startswith(prefix)}
    if not matches:
        root_log.error("interval %s not found", interval_id)
        sys.exit(1)
    if len(matches) > 1:
        root_log.error("interval prefix %s is ambiguous (%d matches)", interval_id, len(matches))
        sys.exit(1)
    resolved_id, iv = next(iter(matches.items()))

    if iv.status == "cancelled":
        root_log.info("interval %s is already cancelled", resolved_id[:8])
        return

    bead_label = f"  bead: {iv.bead}" if iv.bead else ""
    start_label = f"  start: {iv.start}" if iv.start else ""
    if not yes:
        print(f"Cancel interval {resolved_id[:8]}?{bead_label}{start_label}")
        try:
            answer = input("  Confirm [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if answer not in ("y", "yes"):
            root_log.info("cancelled — no action taken")
            return

    sid = resolve_session_id(project_root, explicit=session_id)
    cancel_interval(resolved_id, session_id=sid, project_dir=project_root)
    root_log.info("Cancelled interval %s%s", resolved_id[:8], f" ({iv.bead})" if iv.bead else "")


# ---------------------------------------------------------------------------
# amend
# ---------------------------------------------------------------------------


def cmd_amend(
    interval_id: str,
    *,
    start: str | None = None,
    stop: str | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    tag_ops: list[str] | None = None,
    tags_csv: str | None = None,
    actor: str | None = None,
    role: str | None = None,
    group_id: str | None = None,
    session_id: str | None = None,
    project_dir: Path | None = None,
) -> None:
    """Correct an existing interval's timestamps, tags, or provenance fields."""
    from bd_track.util import parse_datetime

    project_root = _project_root(project_dir)

    # Validate tag surface mutual exclusivity.
    incremental = bool(add_tags or remove_tags or tag_ops)
    if incremental and tags_csv is not None:
        root_log.error("--tags cannot be combined with --add-tag/--remove-tag/--tag")
        sys.exit(1)

    # Find interval by full ULID or unique prefix.
    all_intervals = {iv.interval: iv for iv in load_intervals(log_dir(project_root))}
    prefix = interval_id.upper()
    matches = {ulid: iv for ulid, iv in all_intervals.items() if ulid.startswith(prefix)}
    if not matches:
        root_log.error("interval %s not found", interval_id)
        sys.exit(1)
    if len(matches) > 1:
        root_log.error("interval prefix %s is ambiguous (%d matches)", interval_id, len(matches))
        sys.exit(1)
    resolved_id, iv = next(iter(matches.items()))

    if iv.status == "cancelled":
        root_log.error(
            "interval %s is cancelled; cannot amend a cancelled interval", resolved_id[:8]
        )
        sys.exit(1)

    # Parse timestamps.
    start_iso: str | None = None
    stop_iso: str | None = None
    if start is not None:
        try:
            start_iso = parse_datetime(start).isoformat(timespec="seconds")
        except ValueError as exc:
            root_log.error("--start: %s", exc)
            sys.exit(1)
    if stop is not None:
        try:
            stop_dt = parse_datetime(stop)
        except ValueError as exc:
            root_log.error("--stop: %s", exc)
            sys.exit(1)
        if stop_dt > dt.datetime.now().astimezone():
            root_log.error("--stop timestamp is in the future: %s", stop_dt.isoformat())
            sys.exit(1)
        stop_iso = stop_dt.isoformat(timespec="seconds")

    # Validate effective start < stop.
    eff_start = start_iso or iv.start
    eff_stop = stop_iso or iv.stop
    if eff_start and eff_stop:
        if dt.datetime.fromisoformat(eff_stop) <= dt.datetime.fromisoformat(eff_start):
            root_log.error("stop must be after start (%s >= %s)", eff_stop, eff_start)
            sys.exit(1)

    # Compute new tag list.
    new_tags: list[str] | None = None
    if tags_csv is not None:
        new_tags = [t.strip() for t in tags_csv.split(",") if t.strip()]
    elif incremental:
        add_from_ops: list[str] = []
        rem_from_ops: list[str] = []
        for op in tag_ops or []:
            if op.startswith("+"):
                add_from_ops.append(op[1:])
            elif op.startswith("~"):
                rem_from_ops.append(op[1:])
            else:
                root_log.error("--tag: expected +tag or ~tag, got: %s", op)
                sys.exit(1)
        all_adds = set(add_tags or []) | set(add_from_ops)
        all_removes = set(remove_tags or []) | set(rem_from_ops)
        for t in all_removes:
            if t not in iv.tags:
                root_log.warning(
                    "--remove-tag / ~prefix: tag %r not present on interval %s", t, resolved_id[:8]
                )
        current = (set(iv.tags) - all_removes) | all_adds
        new_tags = sorted(current)

    # Nothing to change?
    if (
        start_iso is None
        and stop_iso is None
        and new_tags is None
        and actor is None
        and role is None
        and group_id is None
    ):
        root_log.error(
            "amend: nothing to change — specify at least one of "
            "--start, --stop, --tags, --add-tag, --remove-tag, --tag, "
            "--actor, --role, --group-id",
        )
        sys.exit(1)

    sid = resolve_session_id(project_root, explicit=session_id)
    correct_interval(
        resolved_id,
        session_id=sid,
        start=start_iso,
        stop=stop_iso,
        tags=new_tags,
        actor=actor if actor is not None else _UNSET,
        role=role if role is not None else _UNSET,
        group_id=group_id if group_id is not None else _UNSET,
        project_dir=project_root,
    )

    if start_iso is not None:
        root_log.info("Amended start:    %s", start_iso)
    if stop_iso is not None:
        root_log.info("Amended stop:     %s", stop_iso)
    if new_tags is not None:
        root_log.info("Amended tags:     %s", ", ".join(new_tags) or "(none)")
    if actor is not None:
        root_log.info("Amended actor:    %s", actor)
    if role is not None:
        root_log.info("Amended role:     %s", role)
    if group_id is not None:
        root_log.info("Amended group_id: %s", group_id)
    root_log.info("Correction appended for interval %s", resolved_id[:8])


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _fmt_ts(ts: str | None) -> str:
    """Short timestamp for table display: drop sub-second and timezone noise."""
    if ts is None:
        return ""
    try:
        return dt.datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def cmd_list(
    *,
    from_dt: str | None = None,
    to_dt: str | None = None,
    bead: str | None = None,
    actor: str | None = None,
    statuses: list[str] | None = None,
    session_filter: str | None = None,
    fmt: str = "table",
    pretty: bool = False,
    sort_by: str = "start",
    desc: bool = False,
    project_dir: Path | None = None,
    global_scope: bool = False,
    session_id: str | None = None,
) -> None:
    """List intervals matching the given filters."""
    from bd_track.util import parse_datetime

    # Resolve date range. Default: today if neither bound specified.
    use_default_range = from_dt is None and to_dt is None
    from_parsed: dt.datetime | None = None
    to_parsed: dt.datetime | None = None

    if use_default_range:
        today = dt.date.today()
        from_parsed = dt.datetime.combine(today, dt.time.min).astimezone()
    else:
        if from_dt is not None:
            try:
                from_parsed = parse_datetime(from_dt)
            except ValueError as exc:
                root_log.error("--from: %s", exc)
                sys.exit(1)
        if to_dt is not None:
            try:
                to_parsed = parse_datetime(to_dt)
            except ValueError as exc:
                root_log.error("--to: %s", exc)
                sys.exit(1)

    roots = _project_roots(project_dir, global_scope)
    all_ivs: list[Interval] = []
    for root in roots:
        all_ivs.extend(load_intervals(log_dir(root)))

    # Apply filters.
    filtered: list[Interval] = []
    for iv in all_ivs:
        # Date range filter on start time.
        if iv.start is not None:
            start_ts = dt.datetime.fromisoformat(iv.start)
            if from_parsed is not None and start_ts < from_parsed:
                continue
            if to_parsed is not None and start_ts > to_parsed:
                continue
        elif from_parsed is not None:
            # No start recorded; exclude when a lower bound is set.
            continue

        if statuses is not None and iv.status not in statuses:
            continue
        if bead is not None and iv.bead != bead:
            continue
        if actor is not None and iv.actor != actor:
            continue
        if session_filter is not None:
            if iv.session is None or not iv.session.startswith(session_filter):
                continue
        filtered.append(iv)

    # Sort.
    def _sort_key(iv: Interval) -> object:
        if sort_by == "stop":
            return iv.stop or ""
        if sort_by == "duration":
            return iv.duration.total_seconds()
        return iv.start or ""

    filtered.sort(key=_sort_key, reverse=desc)

    if not filtered:
        root_log.info("no intervals matched")
        return

    # Serialize to list-of-dicts for non-table formats.
    rows = [_interval_to_dict(iv) for iv in filtered]

    if fmt == "json":
        import json

        indent = 2 if pretty else None
        print(json.dumps(rows, indent=indent, ensure_ascii=False))
        return

    if fmt == "yaml":
        import yaml

        print(yaml.dump(rows, default_flow_style=False, allow_unicode=True), end="")
        return

    if fmt in ("csv", "tsv"):
        import csv
        import io

        sep = "\t" if fmt == "tsv" else ","
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=sep)
        if rows:
            w.writerow(rows[0].keys())
        for row in rows:
            w.writerow(
                [
                    "|".join(v) if isinstance(v, list) else ("" if v is None else str(v))
                    for v in row.values()
                ]
            )
        print(buf.getvalue(), end="")
        return

    # table (default) — Rich
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(show_header=True, box=None, padding=(0, 1))
    table.add_column("interval", style="bold cyan", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("bead", no_wrap=True)
    table.add_column("start", no_wrap=True)
    table.add_column("stop", no_wrap=True)
    table.add_column("duration", no_wrap=True)
    table.add_column("session", no_wrap=True)
    table.add_column("tags")

    for iv in filtered:
        dur = _format_duration(iv.duration) if iv.stop else "(open)"
        stop_display = _fmt_ts(iv.stop) if iv.stop else "(open)"
        session_short = (iv.session or "")[:8] or "(none)"
        tags_display = ", ".join(iv.tags) if iv.tags else ""
        table.add_row(
            iv.interval[:8],
            iv.status,
            iv.bead or "",
            _fmt_ts(iv.start),
            stop_display,
            dur,
            session_short,
            tags_display,
        )

    root_log.info("%d interval(s) matched", len(filtered))
    console.print(table)


# ---------------------------------------------------------------------------
# status / active
# ---------------------------------------------------------------------------


def cmd_status(*, session_id: str | None = None, project_dir: Path | None = None) -> None:
    """Show this session's open interval(s): bead, tuple, elapsed."""
    project_root = _project_root(project_dir)
    sid = resolve_session_id(project_root, explicit=session_id)
    all_open = open_intervals(load_intervals(log_dir(project_root)))
    mine = [iv for iv in all_open if iv.session == sid]

    if not mine:
        root_log.info("no active interval in this session (%s)", sid)
        for iv in _session_paused(sid, project_root):
            root_log.info(
                "Paused:   %s  (since %s ago; `bd-track resume` to reopen)",
                iv.bead or "(no bead)",
                _elapsed(iv.stop),
            )
        others = len({iv.session for iv in all_open})
        if others:
            root_log.info(
                "(%d interval(s) active in other sessions — see `bd-track active`)", len(all_open)
            )
        return

    for iv in mine:
        title = ""
        status = ""
        if iv.bead:
            try:
                issue = get_issue(iv.bead, cwd=project_root)
                title = issue.get("title", "")
                status = issue.get("status", "")
            except SystemExit:
                pass
        root_log.info("Tracking: %s  %s", iv.bead or "(no bead)", title)
        if status:
            root_log.info("Status:   %s", status)
        root_log.info("Elapsed:  %s", _elapsed(iv.start))
        tuple_ = _tuple_from_tags(iv.tags)
        for key in _TUPLE_KEYS:
            root_log.info("%-9s %s", key.capitalize() + ":", tuple_[key] or "(none)")

    other = [iv for iv in all_open if iv.session != sid]
    if other:
        root_log.info(
            "(%d interval(s) active in other sessions — see `bd-track active`)", len(other)
        )


def _bead_title(bead: str | None, root: Path) -> str:
    """Bead title for display, or "" when the bead cannot be shown."""
    if not bead:
        return ""
    try:
        return get_issue(bead, cwd=root).get("title", "")
    except SystemExit:
        return ""


def cmd_active(
    *,
    fmt: str = "table",
    session_id: str | None = None,
    project_dir: Path | None = None,
    global_scope: bool = False,
) -> None:
    """Show ALL open and paused intervals across ALL sessions (the multi-active view).

    ``fmt="json"`` prints one object per interval to stdout, with ``state``
    (open|paused), project, bead and title: the input for a pause/resume picker.
    """
    roots = _project_roots(project_dir, global_scope)
    # Resolve the caller's session id from the first available root; fall back
    # to None (no '*' marker) if the root has no local beads workspace.
    sid = _caller_session(roots[0], session_id)

    rows: list[tuple[str, Path, Interval]] = []
    for root in roots:
        ivs = load_intervals(log_dir(root))
        rows.extend(("open", root, iv) for iv in open_intervals(ivs))
        rows.extend(("paused", root, iv) for iv in paused_intervals(ivs))
    rows.sort(key=lambda r: (r[0], r[2].start or ""))

    if fmt == "json":
        import json

        print(
            json.dumps(
                [
                    {
                        "state": state,
                        "project": str(root),
                        "session": iv.session,
                        "bead": iv.bead,
                        "title": _bead_title(iv.bead, root),
                        "interval": iv.interval,
                        "since": iv.start if state == "open" else iv.stop,
                        "tags": iv.tags,
                    }
                    for state, root, iv in rows
                ],
                ensure_ascii=False,
            )
        )
        return

    opens = [iv for state, _, iv in rows if state == "open"]
    paused = [iv for state, _, iv in rows if state == "paused"]
    if not opens:
        root_log.info("no active intervals in any session")
    else:
        root_log.info("%d active interval(s):", len(opens))
    for iv in opens:
        marker = "*" if (sid and iv.session == sid) else " "
        tuple_ = _tuple_from_tags(iv.tags)
        tup = " ".join(f"{k}:{v}" for k in _TUPLE_KEYS if (v := tuple_[k])) or "(no tuple)"
        root_log.info(
            "%s %-20s %-14s %-7s  %s",
            marker,
            iv.session or "?",
            iv.bead or "(no bead)",
            _elapsed(iv.start),
            tup,
        )
    if paused:
        root_log.info("%d paused interval(s) (`bd-track resume`):", len(paused))
    for iv in paused:
        marker = "*" if (sid and iv.session == sid) else " "
        root_log.info(
            "%s %-20s %-14s paused %s ago",
            marker,
            iv.session or "?",
            iv.bead or "(no bead)",
            _elapsed(iv.stop),
        )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _in_range(iv: Interval, since: dt.date | None, until: dt.date | None) -> bool:
    if iv.start is None:
        return False
    d = dt.datetime.fromisoformat(iv.start).date()
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


def cmd_report(
    *,
    group_by: str = "bead",
    policy_name: str = "billing",
    since: str | None = None,
    until: str | None = None,
    session_id: str | None = None,
    project_dir: Path | None = None,
    global_scope: bool = False,
) -> None:
    """Aggregate closed intervals; group by a dimension under a named policy."""
    roots = _project_roots(project_dir, global_scope)
    policy = POLICIES[policy_name]
    since_d = dt.date.fromisoformat(since) if since else None
    until_d = dt.date.fromisoformat(until) if until else None
    all_ivs: list[Interval] = []
    for root in roots:
        all_ivs.extend(load_intervals(log_dir(root)))
    intervals = [iv for iv in all_ivs if _in_range(iv, since_d, until_d)]

    rows = report(intervals, group_by=group_by, policy=policy)
    if not rows:
        root_log.info("no closed intervals in range")
    else:
        root_log.info("Report by %s  [policy: %s]", group_by, policy_name)
        total = dt.timedelta(0)
        for r in rows:
            root_log.info(
                "  %-24s %8s  (%d interval(s))",
                str(r.group) if r.group is not None else "(none)",
                _format_duration(r.duration),
                r.intervals,
            )
            total += r.duration
        root_log.info("  %-24s %8s", "TOTAL", _format_duration(total))

    stale = [iv for iv in intervals if iv.status == "open"]
    if stale:
        root_log.info(
            "(%d open/stale interval(s) excluded from totals — see `bd-track active`)", len(stale)
        )
