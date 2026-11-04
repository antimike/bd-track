"""Aggregator + reports over the JSONL event log (bd-timew-zzl).

Full-walk reader: walks per-session JSONL files, pairs events by interval ULID,
folds corrections (per-field latest-wins by ``eid``), drops cancelled intervals,
and surfaces open/stale ones. Applies a flat aggregation policy
(partition/collapse per axis → union within a partition, sum across partitions)
to produce report rows grouped by an output dimension.

Policy axes are ``group_id``, ``actor``, ``role``, **and ``session``**. (The
``blh`` design listed the first three; ``session`` is included here because
machine-hours mode must *sum* across concurrent sessions — collapsing it would
under-count them. Billing/wall-clock modes collapse it instead.)

Deferred to follow-up beads, never silently: hierarchical nested-subtotal
reporting (bd-timew-an7) and an incremental summary cache (bd-timew-wav). This
module always performs a correct full walk and is the correctness oracle for
that future cache.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from bd_track.events import log_dir
from bd_track.util import root_log

# Policy axes available for partition/collapse. Output-grouping dimensions
# (bead, session, or a tag key like "client") are a separate concern (see report).
POLICY_AXES = ("group_id", "actor", "role", "session")

_MUTABLE = ("start", "stop", "tags", "group_id", "actor", "role")


@dataclass
class Interval:
    """An interval's effective state after folding its events."""

    interval: str
    bead: str | None = None
    session: str | None = None
    start: str | None = None  # ISO-8601
    stop: str | None = None  # ISO-8601; None => open
    tags: list[str] = field(default_factory=list)
    group_id: str | None = None
    actor: str | None = None
    role: str | None = None
    status: str = "open"  # open | closed | cancelled
    paused: bool = False  # stopped by `pause` (and not cleared); see paused_intervals
    resumed_from: str | None = None

    @property
    def duration(self) -> dt.timedelta:
        if self.start is None or self.stop is None:
            return dt.timedelta(0)
        return _parse(self.stop) - _parse(self.start)


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# Read + fold
# ---------------------------------------------------------------------------


def read_events(directory: Path | None = None) -> Iterator[dict]:
    """Yield events from every per-session JSONL file, skipping malformed lines.

    A rare concurrent-append interleave can corrupt a single line; we log and
    skip it rather than aborting the whole aggregation (defense in depth — the
    per-session-file design makes such interleaves unlikely to begin with).
    """
    directory = directory or log_dir()
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.jsonl")):
        with path.open() as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    root_log.warning("skipping malformed JSONL at %s:%d", path.name, lineno)
                    continue
                if isinstance(ev, dict) and ev.get("interval") and ev.get("event"):
                    yield ev
                else:
                    root_log.warning(
                        "skipping event missing interval/event at %s:%d", path.name, lineno
                    )


def fold_intervals(events: Iterable[dict]) -> dict[str, Interval]:
    """Fold events into effective intervals, keyed by interval ULID.

    Events are applied in ``eid`` order so corrections resolve per-field
    latest-wins and start precedes stop. ``cancel`` marks the interval dropped;
    a present ``stop`` (from a stop event or a correction) marks it closed.
    """
    ordered = sorted(events, key=lambda e: e.get("eid", ""))
    intervals: dict[str, Interval] = {}
    cancelled: set[str] = set()
    for ev in ordered:
        iid = ev["interval"]
        iv = intervals.setdefault(iid, Interval(interval=iid))
        etype = ev["event"]
        if etype == "start":
            iv.session = ev.get("session_id")
            iv.bead = ev.get("bead")
            iv.start = ev.get("ts")
            iv.tags = list(ev.get("tags") or [])
            iv.group_id = ev.get("group_id")
            iv.actor = ev.get("actor")
            iv.role = ev.get("role")
            iv.resumed_from = ev.get("resumed_from")
        elif etype == "stop":
            iv.stop = ev.get("ts")
            iv.paused = bool(ev.get("paused"))
        elif etype == "cancel":
            cancelled.add(iid)
        elif etype == "correction":
            if "start" in ev:
                iv.start = ev["start"]
            if "stop" in ev:
                iv.stop = ev["stop"]
            if "tags" in ev:
                iv.tags = list(ev["tags"] or [])
            if "group_id" in ev:
                iv.group_id = ev["group_id"]
            if "actor" in ev:
                iv.actor = ev["actor"]
            if "role" in ev:
                iv.role = ev["role"]
            if "paused" in ev:
                iv.paused = bool(ev["paused"])
    for iid, iv in intervals.items():
        iv.status = "cancelled" if iid in cancelled else ("closed" if iv.stop else "open")
    return intervals


def load_intervals(directory: Path | None = None) -> list[Interval]:
    """Convenience: read + fold the whole log into a list of intervals."""
    return list(fold_intervals(read_events(directory)).values())


# ---------------------------------------------------------------------------
# Aggregation policy
# ---------------------------------------------------------------------------


@dataclass
class Policy:
    """Per-axis partition|collapse classification.

    ``axes`` maps a subset of POLICY_AXES to "partition" or "collapse".
    ``default`` classifies any POLICY_AXES key not in ``axes`` (no silent
    default at the API level — callers/config must choose).
    """

    axes: dict[str, str]
    default: str = "partition"

    def classify(self, axis: str) -> str:
        return self.axes.get(axis, self.default)

    def partition_axes(self) -> list[str]:
        return [a for a in POLICY_AXES if self.classify(a) == "partition"]


# Built-in policies (the `blh` "billing" rule plus the two endpoints).
POLICIES = {
    # person-hours: a group's concurrent sessions collapse to wall-clock; actors sum.
    "billing": Policy(
        axes={
            "group_id": "collapse",
            "session": "collapse",
            "actor": "partition",
            "role": "partition",
        }
    ),
    # compute-hours: every axis partitions → sum across sessions/groups/actors.
    "machine": Policy(axes={a: "partition" for a in POLICY_AXES}),
    # attended wall-clock: collapse everything → one big union.
    "wallclock": Policy(axes={a: "collapse" for a in POLICY_AXES}),
}


def _union_duration(spans: Iterable[tuple[str, str]]) -> dt.timedelta:
    """Duration of the union of [start, stop] spans (overlaps counted once)."""
    parsed = sorted((_parse(s), _parse(e)) for s, e in spans if s and e)
    total = dt.timedelta(0)
    cur_start: dt.datetime | None = None
    cur_end: dt.datetime | None = None
    for s, e in parsed:
        if cur_end is None or s > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = s, e
        elif e > cur_end:
            cur_end = e
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def total_duration(intervals: Iterable[Interval], policy: Policy) -> dt.timedelta:
    """Total billable time under ``policy``: union within each partition, summed.

    Only ``closed`` intervals contribute; open/cancelled are excluded (open ones
    are surfaced separately via ``open_intervals``).
    """
    groups: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    paxes = policy.partition_axes()
    for iv in intervals:
        if iv.status != "closed":
            continue
        key = tuple(getattr(iv, a) for a in paxes)
        groups[key].append((iv.start, iv.stop))
    return sum((_union_duration(spans) for spans in groups.values()), dt.timedelta(0))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _tag_value(iv: Interval, key: str) -> str | None:
    """Value of a flat 'key:value' billing tag (e.g. 'client'), or None."""
    prefix = f"{key}:"
    for tag in iv.tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _dimension(iv: Interval, group_by: str):
    """Resolve an output-grouping dimension: a field name or a tag key."""
    if group_by in ("bead", "session", "actor", "role", "group_id"):
        return getattr(iv, group_by)
    return _tag_value(iv, group_by)  # billing tag key (client/case/svc/...)


@dataclass
class ReportRow:
    group: object
    duration: dt.timedelta
    intervals: int


def report(
    intervals: Iterable[Interval],
    *,
    group_by: str,
    policy: Policy,
) -> list[ReportRow]:
    """Group closed intervals by an output dimension; total each under ``policy``.

    Rows are sorted by descending duration. Open/cancelled intervals are
    excluded (see ``open_intervals`` to surface stale ones).
    """
    buckets: dict[object, list[Interval]] = defaultdict(list)
    for iv in intervals:
        if iv.status == "closed":
            buckets[_dimension(iv, group_by)].append(iv)
    rows = [
        ReportRow(group=g, duration=total_duration(ivs, policy), intervals=len(ivs))
        for g, ivs in buckets.items()
    ]
    rows.sort(key=lambda r: r.duration, reverse=True)
    return rows


def open_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Open/stale intervals (start, no stop, not cancelled) — surfaced, not billed."""
    return [iv for iv in intervals if iv.status == "open"]


def paused_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Intervals awaiting ``resume``: per session, the latest live interval, if paused.

    A pause is cleared implicitly by any later start in the same session (a
    resume, or the session moving on to other work — resuming then would break
    single-active-per-session), and explicitly by a ``paused: false`` correction.
    Interval ULIDs are minted at start, so they order a session's intervals.
    """
    latest: dict[str | None, Interval] = {}
    for iv in intervals:
        if iv.status == "cancelled":
            continue
        cur = latest.get(iv.session)
        if cur is None or iv.interval > cur.interval:
            latest[iv.session] = iv
    return [iv for iv in latest.values() if iv.status == "closed" and iv.paused]
