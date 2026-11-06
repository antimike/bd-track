"""Tests for the re-wired command layer (track.py, bd-timew-9hn).

Exercises the JSONL backend behavior change: start/stop/switch/status/active/
report no longer touch Timewarrior. The key invariants are (a) single-active
*per session* and (b) a session can never close another session's interval —
the concurrency bug (bd-timew-nfr) that motivated the rewrite.

Session ids are pinned via the explicit ``session_id=`` arg, which short-circuits
``resolve_session_id`` so no pointer/env state is involved.
"""

from __future__ import annotations

import pytest

from bd_track import events, registry, track
from bd_track.aggregate import load_intervals
from bd_track.events import log_dir


@pytest.fixture
def proj(monkeypatch, tmp_path):
    """A fake local beads dir; patch find_beads_dir everywhere it's imported."""
    bdir = tmp_path / "proj" / ".beads"
    bdir.mkdir(parents=True)
    monkeypatch.setattr(track, "find_beads_dir", lambda project_dir=None: bdir)
    monkeypatch.setattr(events, "find_beads_dir", lambda project_dir=None: bdir)
    # Billing + side effects: keep the command pure for the test.
    monkeypatch.setattr(track, "load_sidecar", lambda beads_dir: {"default": {}, "patterns": []})
    monkeypatch.setattr(
        track, "resolve_tuple", lambda labels, sidecar: {"client": "X", "case": "Y", "svc": "Z"}
    )
    monkeypatch.setattr(track, "record_activity", lambda *a, **k: None)
    # Never read the developer's real host config (long-interval warning threshold).
    monkeypatch.setattr(registry, "load_host_config", lambda: {"long_interval_hours": 8})
    return bdir


def _issue(monkeypatch, *, status="open", title="t"):
    monkeypatch.setattr(
        track,
        "get_issue",
        lambda iid, **k: {"id": iid, "title": title, "labels": [], "status": status},
    )


def _claims(monkeypatch):
    """Capture util.run calls (the bd --claim path is a late `from util import run`)."""
    calls = []
    monkeypatch.setattr("bd_track.util.run", lambda cmd, **k: calls.append(cmd))
    return calls


def _intervals(bdir):
    return load_intervals(log_dir(bdir.parent))


def _open(bdir, session=None):
    ivs = [iv for iv in _intervals(bdir) if iv.status == "open"]
    return [iv for iv in ivs if session is None or iv.session == session]


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_appends_start_event_with_tags_and_claims(proj, monkeypatch):
    _issue(monkeypatch, status="open")
    calls = _claims(monkeypatch)
    track.cmd_start("bead-1", session_id="s1")

    opens = _open(proj, "s1")
    assert len(opens) == 1
    iv = opens[0]
    assert iv.bead == "bead-1"
    assert iv.tags == ["client:X", "case:Y", "svc:Z"]
    # open bead is claimed
    assert ["bd", "update", "bead-1", "--claim"] in calls


def test_start_skips_claim_when_already_in_progress(proj, monkeypatch):
    _issue(monkeypatch, status="in_progress")
    calls = _claims(monkeypatch)
    track.cmd_start("bead-1", session_id="s1")
    assert not any("--claim" in c for c in calls)


def test_start_auto_stops_own_prior_interval(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-1", session_id="s1")
    track.cmd_start("bead-2", session_id="s1")
    opens = _open(proj, "s1")
    assert len(opens) == 1 and opens[0].bead == "bead-2"


def test_start_does_not_touch_other_session(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_start("bead-B", session_id="s2")
    # both sessions still have their own open interval
    assert len(_open(proj, "s1")) == 1
    assert len(_open(proj, "s2")) == 1


def test_start_records_billable_false_when_no_svc(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    monkeypatch.setattr(
        track, "resolve_tuple", lambda labels, sidecar: {"client": "X", "case": "Y", "svc": None}
    )
    track.cmd_start("bead-1", session_id="s1")
    assert "billable:false" in _open(proj, "s1")[0].tags


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_closes_own_open_interval(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-1", session_id="s1")
    track.cmd_stop(clean=False, session_id="s1")
    assert _open(proj, "s1") == []


def test_stop_sweep_keeps_deferred_drops_closed(proj, monkeypatch):
    """Parked (deferred) beads survive the post-stop sweep; closed ones do not (bd-timew-eue)."""
    from bd_track import queue

    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    monkeypatch.setattr(queue, "find_beads_dir", lambda project_dir=None: proj)
    statuses = {"PARKED": "deferred", "DONE": "closed", "NEXT": "open"}
    monkeypatch.setattr(queue, "get_issue", lambda iid, **k: {"id": iid, "status": statuses[iid]})
    queue.save_all_queues(proj, {"work": ["DONE", "NEXT"], "work.operator": ["PARKED"]})
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-1", session_id="s1")
    track.cmd_stop(session_id="s1")
    assert queue.load_all_queues(proj) == {"work": ["NEXT"], "work.operator": ["PARKED"]}


def test_stop_never_closes_other_session(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_start("bead-B", session_id="s2")
    # s1 stops with no arg — must NOT close s2's interval
    track.cmd_stop(clean=False, session_id="s1")
    assert _open(proj, "s1") == []
    assert len(_open(proj, "s2")) == 1


def test_stop_with_bead_filters(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    # one open interval; stopping a different bead id is a no-op
    track.cmd_start("bead-1", session_id="s1")
    track.cmd_stop("bead-OTHER", clean=False, session_id="s1")
    assert len(_open(proj, "s1")) == 1
    track.cmd_stop("bead-1", clean=False, session_id="s1")
    assert _open(proj, "s1") == []


def test_stop_no_active_is_quiet_noop(proj, monkeypatch):
    _issue(monkeypatch)
    track.cmd_stop(clean=False, session_id="s1")  # nothing open
    assert _intervals(proj) == []


# ---------------------------------------------------------------------------
# switch
# ---------------------------------------------------------------------------


def test_switch_stops_then_starts(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-1", session_id="s1")
    track.cmd_switch("bead-2", session_id="s1")
    opens = _open(proj, "s1")
    assert len(opens) == 1 and opens[0].bead == "bead-2"
    # the first interval is closed, not lingering
    closed = [iv for iv in _intervals(proj) if iv.status == "closed"]
    assert len(closed) == 1 and closed[0].bead == "bead-1"


# ---------------------------------------------------------------------------
# active / report (smoke — output via logging, assert no crash + state)
# ---------------------------------------------------------------------------


def test_active_lists_all_sessions(proj, monkeypatch, caplog):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_start("bead-B", session_id="s2")
    import logging

    with caplog.at_level(logging.INFO, logger="bd-track"):
        track.cmd_active(session_id="s1")
    text = caplog.text
    assert "bead-A" in text and "bead-B" in text and "s1" in text and "s2" in text


def test_report_totals_closed_intervals(proj, monkeypatch, caplog):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-1", session_id="s1")
    track.cmd_stop(clean=False, session_id="s1")
    import logging

    with caplog.at_level(logging.INFO, logger="bd-track"):
        track.cmd_report(group_by="bead", policy_name="billing", session_id="s1")
    assert "bead-1" in caplog.text and "TOTAL" in caplog.text


# ---------------------------------------------------------------------------
# pause / resume (bd-timew-11e)
# ---------------------------------------------------------------------------


def _paused(bdir, session=None):
    from bd_track.aggregate import paused_intervals

    return [
        iv for iv in paused_intervals(_intervals(bdir)) if session is None or iv.session == session
    ]


def test_pause_defaults_to_own_session(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_start("bead-B", session_id="s2")
    track.cmd_pause(session_id="s1")
    assert _open(proj, "s1") == [] and len(_open(proj, "s2")) == 1
    assert [iv.bead for iv in _paused(proj)] == ["bead-A"]


def test_pause_all_then_resume_all_restores_same_set(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_start("bead-B", session_id="s2")
    before = {iv.session: iv for iv in _open(proj)}
    track.cmd_pause(all_sessions=True)
    assert _open(proj) == [] and len(_paused(proj)) == 2

    track.cmd_resume(all_sessions=True)
    after = {iv.session: iv for iv in _open(proj)}
    assert _paused(proj) == []
    for sess in ("s1", "s2"):
        old, new = before[sess], after[sess]
        assert new.interval != old.interval
        assert (new.bead, new.tags, new.actor) == (old.bead, old.tags, old.actor)
        assert new.resumed_from == old.interval


def test_pause_and_resume_are_idempotent(proj, monkeypatch, caplog):
    import logging

    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    with caplog.at_level(logging.INFO, logger="bd-track"):
        track.cmd_pause(all_sessions=True)
        track.cmd_pause(all_sessions=True)
        track.cmd_resume(all_sessions=True)
        track.cmd_resume(all_sessions=True)
    assert "nothing to pause" in caplog.text and "nothing to resume" in caplog.text
    assert len(_open(proj, "s1")) == 1
    # one stop (paused) + one resumed start; the repeats appended nothing
    assert len(_intervals(proj)) == 2


def test_pause_resume_bead_filter(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_start("bead-B", session_id="s2")
    track.cmd_pause(["bead-B"], all_sessions=True)
    assert [iv.bead for iv in _open(proj)] == ["bead-A"]
    track.cmd_pause(all_sessions=True)
    track.cmd_resume(["bead-A"], all_sessions=True)
    assert [iv.bead for iv in _open(proj)] == ["bead-A"]
    assert [iv.bead for iv in _paused(proj)] == ["bead-B"]


def test_pause_dry_run_writes_nothing(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_pause(all_sessions=True, dry_run=True)
    assert len(_open(proj, "s1")) == 1


def test_stop_clears_pause(proj, monkeypatch, caplog):
    import logging

    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_pause(session_id="s1")
    with caplog.at_level(logging.INFO, logger="bd-track"):
        track.cmd_stop("bead-A", clean=False, session_id="s1")
    assert "Cleared pause" in caplog.text
    assert _paused(proj) == []
    track.cmd_resume(all_sessions=True)
    assert _open(proj) == []


def test_start_after_pause_clears_it(proj, monkeypatch):
    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_pause(session_id="s1")
    track.cmd_start("bead-B", session_id="s1")
    track.cmd_resume(all_sessions=True)
    assert [iv.bead for iv in _open(proj, "s1")] == ["bead-B"]


def test_status_shows_paused(proj, monkeypatch, caplog):
    import logging

    _issue(monkeypatch)
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_pause(session_id="s1")
    with caplog.at_level(logging.INFO, logger="bd-track"):
        track.cmd_status(session_id="s1")
    assert "Paused:   bead-A" in caplog.text


def test_active_json_lists_open_and_paused(proj, monkeypatch, capsys):
    import json

    _issue(monkeypatch, title="Title A")
    _claims(monkeypatch)
    track.cmd_start("bead-A", session_id="s1")
    track.cmd_start("bead-B", session_id="s2")
    track.cmd_pause(["bead-A"], all_sessions=True)
    capsys.readouterr()
    track.cmd_active(fmt="json", session_id="s1")
    rows = json.loads(capsys.readouterr().out)
    assert [(r["state"], r["bead"]) for r in rows] == [("open", "bead-B"), ("paused", "bead-A")]
    assert rows[1]["title"] == "Title A" and rows[1]["session"] == "s1"


# ---------------------------------------------------------------------------
# add / amend --split-at (bd-timew-1fj)
# ---------------------------------------------------------------------------


def _dt(ts):
    import datetime as dt

    return dt.datetime.fromisoformat(ts)


def _past(bdir, *, bead="bead-1", session="s1", start, stop=None, paused=False):
    """An interval with explicit past times, written straight to the log."""
    iid = events.start_interval(bead, ["svc:Z"], session_id=session, ts=start, actor="human")
    if stop:
        events.stop_interval(iid, session_id=session, ts=stop, paused=paused)
    return iid


def _by_id(bdir):
    return {iv.interval: iv for iv in _intervals(bdir)}


def test_add_records_closed_interval_without_claim_or_touching_open(proj, monkeypatch):
    _issue(monkeypatch, status="closed")
    calls = _claims(monkeypatch)
    track.cmd_start("other", session_id="s1")
    track.cmd_add(
        "bead-1",
        start="2026-11-04T17:08:00+00:00",
        stop="2026-11-04T17:20:00+00:00",
        session_id="s1",
    )

    added = [iv for iv in _intervals(proj) if iv.bead == "bead-1"]
    assert len(added) == 1
    assert added[0].status == "closed"
    assert added[0].session == "s1"
    assert added[0].tags == ["client:X", "case:Y", "svc:Z"]
    assert added[0].duration.total_seconds() == 12 * 60
    # The open interval on "other" is untouched and bead-1 was never claimed.
    assert [iv.bead for iv in _open(proj, "s1")] == ["other"]
    assert ["bd", "update", "bead-1", "--claim"] not in calls


@pytest.mark.parametrize(
    "start,stop",
    [
        ("2026-11-04T17:20:00+00:00", "2026-11-04T17:08:00+00:00"),  # stop before start
        ("2026-11-04T17:08:00+00:00", "2099-01-01T00:00:00+00:00"),  # stop in the future
    ],
)
def test_add_rejects_bad_span(proj, monkeypatch, start, stop):
    _issue(monkeypatch)
    with pytest.raises(SystemExit):
        track.cmd_add("bead-1", start=start, stop=stop, session_id="s1")
    assert _intervals(proj) == []


def test_add_dry_run_writes_nothing(proj, monkeypatch):
    _issue(monkeypatch)
    track.cmd_add(
        "bead-1",
        start="2026-11-04T17:08:00+00:00",
        stop="2026-11-04T17:20:00+00:00",
        dry_run=True,
        session_id="s1",
    )
    assert _intervals(proj) == []


def test_add_warns_on_overlap_with_same_bead(proj, monkeypatch, caplog):
    _issue(monkeypatch)
    _past(proj, start="2026-11-04T17:00:00+00:00", stop="2026-11-04T18:00:00+00:00")
    track.cmd_add(
        "bead-1",
        start="2026-11-04T17:30:00+00:00",
        stop="2026-11-04T18:30:00+00:00",
        session_id="s1",
    )
    assert "overlaps interval" in caplog.text


def test_split_closed_interval_removes_gap(proj):
    iid = _past(proj, start="2026-11-04T17:08:00+00:00", stop="2026-11-05T16:41:00+00:00")
    track.cmd_amend(
        iid[:8],
        split_at="2026-11-04T17:20:00+00:00",
        resume_at="2026-11-05T15:15:00+00:00",
        session_id="s1",
    )

    ivs = _by_id(proj)
    assert len(ivs) == 2
    first = ivs.pop(iid)
    (second,) = ivs.values()
    assert _dt(first.stop) == _dt("2026-11-04T17:20:00+00:00")
    assert _dt(second.start) == _dt("2026-11-05T15:15:00+00:00")
    assert _dt(second.stop) == _dt("2026-11-05T16:41:00+00:00")
    assert (second.bead, second.session, second.tags, second.actor) == (
        "bead-1",
        "s1",
        ["svc:Z"],
        "human",
    )
    total = first.duration + second.duration
    assert total.total_seconds() == (12 + 86) * 60


def test_split_open_interval_leaves_second_part_open(proj, monkeypatch):
    iid = _past(proj, session="s2", start="2026-11-04T17:08:00+00:00")
    track.cmd_amend(
        iid,
        split_at="2026-11-04T17:20:00+00:00",
        resume_at="2026-11-05T15:15:00+00:00",
        session_id="s1",
    )

    ivs = _by_id(proj)
    assert ivs[iid].status == "closed"
    (second,) = [iv for iid2, iv in ivs.items() if iid2 != iid]
    assert second.status == "open"
    assert second.session == "s2"  # the owning session, not the caller's
    # The owning session's stop closes the second part.
    track.cmd_stop(session_id="s2")
    assert _open(proj) == []


def test_split_paused_interval_moves_pause_to_second_part(proj):
    iid = _past(
        proj,
        start="2026-11-04T17:08:00+00:00",
        stop="2026-11-05T16:41:00+00:00",
        paused=True,
    )
    track.cmd_amend(
        iid,
        split_at="2026-11-04T17:20:00+00:00",
        resume_at="2026-11-05T15:15:00+00:00",
        session_id="s1",
    )
    ivs = _by_id(proj)
    assert not ivs.pop(iid).paused
    (second,) = ivs.values()
    assert second.paused


@pytest.mark.parametrize(
    "kwargs",
    [
        # resume before split
        {"split_at": "2026-11-05T15:15:00+00:00", "resume_at": "2026-11-04T17:20:00+00:00"},
        # split before the interval's start
        {"split_at": "2026-11-04T17:00:00+00:00", "resume_at": "2026-11-05T15:15:00+00:00"},
        # resume after the interval's stop
        {"split_at": "2026-11-04T17:20:00+00:00", "resume_at": "2026-11-05T17:00:00+00:00"},
        # only one of the pair
        {"split_at": "2026-11-04T17:20:00+00:00"},
        # combined with another correction
        {
            "split_at": "2026-11-04T17:20:00+00:00",
            "resume_at": "2026-11-05T15:15:00+00:00",
            "role": "x",
        },
    ],
)
def test_split_rejects_invalid(proj, kwargs):
    iid = _past(proj, start="2026-11-04T17:08:00+00:00", stop="2026-11-05T16:41:00+00:00")
    with pytest.raises(SystemExit):
        track.cmd_amend(iid, session_id="s1", **kwargs)
    assert len(_intervals(proj)) == 1


# ---------------------------------------------------------------------------
# long-interval warning (bd-timew-pnj)
# ---------------------------------------------------------------------------


def _hours_ago(hours):
    import datetime as dt

    return (dt.datetime.now().astimezone() - dt.timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


def test_stop_warns_on_interval_over_threshold(proj, caplog):
    iid = _past(proj, start=_hours_ago(23))
    track.cmd_stop(clean=False, session_id="s1")
    assert "longer than long_interval_hours (8h)" in caplog.text
    assert f"amend {iid[:8]} --split-at" in caplog.text


def test_stop_quiet_under_threshold(proj, caplog):
    _past(proj, start=_hours_ago(2))
    track.cmd_stop(clean=False, session_id="s1")
    assert "long_interval_hours" not in caplog.text


def test_stop_at_measures_to_the_given_time(proj, caplog):
    _past(proj, start=_hours_ago(23))
    track.cmd_stop(clean=False, session_id="s1", at=_hours_ago(22))
    assert "long_interval_hours" not in caplog.text


def test_long_interval_warning_disabled_by_zero(proj, monkeypatch, caplog):
    monkeypatch.setattr(registry, "load_host_config", lambda: {"long_interval_hours": 0})
    _past(proj, start=_hours_ago(23))
    track.cmd_stop(clean=False, session_id="s1")
    assert "long_interval_hours" not in caplog.text


def test_long_interval_warning_survives_broken_host_config(proj, monkeypatch):
    def broken():
        raise registry.RegistryError("bad config")

    monkeypatch.setattr(registry, "load_host_config", broken)
    _past(proj, start=_hours_ago(23))
    track.cmd_stop(clean=False, session_id="s1")
    assert _open(proj) == []


def test_pause_and_start_warn_on_long_interval(proj, monkeypatch, caplog):
    _issue(monkeypatch, status="in_progress")
    _past(proj, start=_hours_ago(23))
    track.cmd_pause(session_id="s1")
    assert caplog.text.count("long_interval_hours") == 1

    caplog.clear()
    _past(proj, bead="bead-2", start=_hours_ago(23))
    track.cmd_start("bead-3", session_id="s1")
    assert caplog.text.count("long_interval_hours") == 1
