"""Tests for the aggregator + reports (aggregate.py, bd-timew-zzl).

Covers folding (pairing, correction per-field-latest-wins, cancel, open
detection), the union-vs-sum policy math (machine / wallclock / billing-swarm),
output grouping, open-interval exclusion+surfacing, and malformed-line skipping.
"""

from __future__ import annotations

import datetime as dt
import json

from bd_track import aggregate
from bd_track.aggregate import POLICIES


def T(hour: int) -> str:
    return f"2026-06-02T{hour:02d}:00:00"


def start(eid, iid, ts, *, session="s1", bead="b", tags=None, group_id=None, actor=None, role=None):
    return {
        "v": 1,
        "eid": eid,
        "event": "start",
        "interval": iid,
        "session_id": session,
        "ts": ts,
        "bead": bead,
        "tags": tags or [],
        "group_id": group_id,
        "actor": actor,
        "role": role,
    }


def stop(eid, iid, ts, session="s1"):
    return {"v": 1, "eid": eid, "event": "stop", "interval": iid, "session_id": session, "ts": ts}


def cancel(eid, iid, ts, session="s1"):
    return {"v": 1, "eid": eid, "event": "cancel", "interval": iid, "session_id": session, "ts": ts}


def correction(eid, iid, ts, session="s1", **fields):
    d = {
        "v": 1,
        "eid": eid,
        "event": "correction",
        "interval": iid,
        "session_id": session,
        "ts": ts,
    }
    d.update(fields)
    return d


def hours(n: float) -> dt.timedelta:
    return dt.timedelta(hours=n)


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------


def test_pairs_start_and_stop():
    ivs = aggregate.fold_intervals([start("1", "A", T(9)), stop("2", "A", T(10))])
    assert ivs["A"].status == "closed"
    assert ivs["A"].duration == hours(1)


def test_cancel_marks_cancelled():
    ivs = aggregate.fold_intervals([start("1", "A", T(9)), cancel("2", "A", T(10))])
    assert ivs["A"].status == "cancelled"


def test_open_when_no_stop():
    ivs = aggregate.fold_intervals([start("1", "A", T(9))])
    assert ivs["A"].status == "open"
    assert aggregate.open_intervals(ivs.values()) == [ivs["A"]]


def test_correction_per_field_latest_wins():
    evs = [
        start("1", "A", T(9), actor="claude", tags=["case:OLD"]),
        correction("3", "A", T(9), actor="bot"),  # higher eid
        correction("2", "A", T(9), actor="human"),  # lower eid, applied earlier
        correction("4", "A", T(9), tags=["case:NEW"]),
    ]
    iv = aggregate.fold_intervals(evs)["A"]
    assert iv.actor == "bot"  # eid 3 beats eid 2
    assert iv.tags == ["case:NEW"]  # latest tags correction


def test_correction_can_set_stop_and_close():
    ivs = aggregate.fold_intervals(
        [
            start("1", "A", T(9)),
            correction("2", "A", T(9), stop=T(11)),
        ]
    )
    assert ivs["A"].status == "closed"
    assert ivs["A"].duration == hours(2)


def test_correction_can_clear_group_id_to_null():
    ivs = aggregate.fold_intervals(
        [
            start("1", "A", T(9), group_id="g1"),
            stop("2", "A", T(10)),
            correction("3", "A", T(9), group_id=None),
        ]
    )
    assert ivs["A"].group_id is None


# ---------------------------------------------------------------------------
# Union math + policies
# ---------------------------------------------------------------------------


def test_union_duration_overlap_disjoint_adjacent():
    assert aggregate._union_duration([(T(9), T(11)), (T(10), T(12))]) == hours(3)
    assert aggregate._union_duration([(T(9), T(10)), (T(11), T(12))]) == hours(2)
    assert aggregate._union_duration([(T(9), T(10)), (T(10), T(11))]) == hours(2)


def _two_overlapping_sessions():
    # s1 [9,11] and s2 [10,12] — overlap 10-11.
    return aggregate.fold_intervals(
        [
            start("1", "A", T(9), session="s1", actor="claude", group_id="g1"),
            stop("2", "A", T(11), session="s1"),
            start("3", "B", T(10), session="s2", actor="claude", group_id="g1"),
            stop("4", "B", T(12), session="s2"),
        ]
    ).values()


def test_machine_sums_across_sessions():
    # every axis partitions → two separate partitions → 2h + 2h.
    assert aggregate.total_duration(_two_overlapping_sessions(), POLICIES["machine"]) == hours(4)


def test_wallclock_unions_everything():
    # collapse all → union [9,12] = 3h.
    assert aggregate.total_duration(_two_overlapping_sessions(), POLICIES["wallclock"]) == hours(3)


def test_billing_swarm_collapses_group_sessions():
    # group_id+session collapse, actor partition: one (claude) partition, union → 3h.
    assert aggregate.total_duration(_two_overlapping_sessions(), POLICIES["billing"]) == hours(3)


def test_billing_sums_distinct_actors():
    ivs = aggregate.fold_intervals(
        [
            start("1", "A", T(9), session="s1", actor="human", group_id="g1"),
            stop("2", "A", T(11), session="s1"),
            start("3", "B", T(10), session="s2", actor="claude", group_id="g1"),
            stop("4", "B", T(12), session="s2"),
        ]
    ).values()
    # distinct actors partition → 2h + 2h even though they overlap.
    assert aggregate.total_duration(ivs, POLICIES["billing"]) == hours(4)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_report_group_by_bead_sorted_desc():
    ivs = aggregate.fold_intervals(
        [
            start("1", "A", T(9), bead="beadA"),
            stop("2", "A", T(11)),  # 2h
            start("3", "B", T(9), bead="beadB"),
            stop("4", "B", T(10)),  # 1h
        ]
    ).values()
    rows = aggregate.report(ivs, group_by="bead", policy=POLICIES["machine"])
    assert [(r.group, r.duration) for r in rows] == [("beadA", hours(2)), ("beadB", hours(1))]


def test_report_group_by_tag_key():
    ivs = aggregate.fold_intervals(
        [
            start("1", "A", T(9), tags=["client:X"]),
            stop("2", "A", T(11)),
            start("3", "B", T(9), tags=["client:Y"]),
            stop("4", "B", T(10)),
        ]
    ).values()
    rows = aggregate.report(ivs, group_by="client", policy=POLICIES["machine"])
    assert {r.group for r in rows} == {"X", "Y"}


def test_report_excludes_open_intervals():
    ivs = aggregate.fold_intervals(
        [
            start("1", "A", T(9), bead="beadA"),
            stop("2", "A", T(10)),
            start("3", "B", T(9), bead="beadB"),  # open
        ]
    ).values()
    rows = aggregate.report(ivs, group_by="bead", policy=POLICIES["machine"])
    assert [r.group for r in rows] == ["beadA"]
    assert len(aggregate.open_intervals(ivs)) == 1


# ---------------------------------------------------------------------------
# Reader resilience
# ---------------------------------------------------------------------------


def test_read_events_skips_malformed(tmp_path, caplog):
    log = tmp_path / "s1.jsonl"
    good = json.dumps(
        {"v": 1, "eid": "1", "event": "start", "interval": "A", "session_id": "s1", "ts": T(9)}
    )
    log.write_text(good + "\n" + "{ not json\n" + '{"v":1,"event":"start"}\n')  # 1 good, 2 bad
    events = list(aggregate.read_events(tmp_path))
    assert len(events) == 1 and events[0]["interval"] == "A"
    assert sum("skipping" in r.message for r in caplog.records) == 2


def test_read_events_empty_when_no_dir(tmp_path):
    assert list(aggregate.read_events(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# pause / resume (bd-timew-11e)
# ---------------------------------------------------------------------------


def paused_stop(eid, iid, ts, session="s1"):
    return {**stop(eid, iid, ts, session), "paused": True}


def _paused(events):
    return [
        iv.interval for iv in aggregate.paused_intervals(aggregate.fold_intervals(events).values())
    ]


def test_paused_stop_is_listed_and_still_billed():
    evs = [start("e1", "I1", T(9)), paused_stop("e2", "I1", T(10))]
    ivs = aggregate.fold_intervals(evs)
    assert ivs["I1"].status == "closed" and ivs["I1"].paused
    assert _paused(evs) == ["I1"]
    assert aggregate.total_duration(ivs.values(), POLICIES["billing"]) == hours(1)


def test_plain_stop_is_not_paused():
    assert _paused([start("e1", "I1", T(9)), stop("e2", "I1", T(10))]) == []


def test_later_start_in_same_session_clears_pause():
    evs = [
        start("e1", "I1", T(9)),
        paused_stop("e2", "I1", T(10)),
        {**start("e3", "I2", T(11)), "resumed_from": "I1"},
    ]
    assert _paused(evs) == []
    assert aggregate.fold_intervals(evs)["I2"].resumed_from == "I1"


def test_start_in_other_session_keeps_pause():
    evs = [
        start("e1", "I1", T(9)),
        paused_stop("e2", "I1", T(10)),
        start("e3", "I2", T(11), session="s2"),
    ]
    assert _paused(evs) == ["I1"]


def test_correction_clears_pause():
    evs = [
        start("e1", "I1", T(9)),
        paused_stop("e2", "I1", T(10)),
        correction("e3", "I1", T(11), paused=False),
    ]
    assert _paused(evs) == []


def test_cancelled_later_interval_does_not_hide_pause():
    evs = [
        start("e1", "I1", T(9)),
        paused_stop("e2", "I1", T(10)),
        start("e3", "I2", T(11)),
        cancel("e4", "I2", T(11)),
    ]
    assert _paused(evs) == ["I1"]
