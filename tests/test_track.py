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

from bd_track import events, track
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
