"""Tests for the JSONL event schema + appender (events.py, bd-timew-ahp).

Covers the v1 event shapes, the two-ULID design, append round-trip, correction
field semantics (including correct-to-null via the sentinel), log-dir resolution
(beads-dir branch + server-mode fallback), provenance resolution, and the
PIPE_BUF size guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bd_track import events

SESSION = "blue.cat.tree"


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def beads(monkeypatch, tmp_path):
    """A fake local beads dir so log_dir() takes the <beads_dir>/bd-track/sessions branch."""
    bdir = tmp_path / "proj" / ".beads"
    bdir.mkdir(parents=True)
    monkeypatch.setattr(events, "find_beads_dir", lambda project_dir=None: bdir)
    return bdir


@pytest.fixture
def session_log(beads):
    return beads / "bd-track" / "sessions" / f"{SESSION}.jsonl"


def _is_ulid(s: str) -> bool:
    return isinstance(s, str) and len(s) == 26 and s.isalnum() and s.isupper()


# ---------------------------------------------------------------------------
# start / stop / cancel
# ---------------------------------------------------------------------------


def test_start_event_shape_and_returns_interval(beads, session_log):
    interval = events.start_interval(
        "bd-timew-ahp",
        ["client:X", "case:Y", "svc:Z"],
        session_id=SESSION,
        group_id=None,
        actor="claude",
        role=None,
    )
    assert _is_ulid(interval)
    [ev] = _read_lines(session_log)
    assert ev["v"] == 1
    assert ev["event"] == "start"
    assert ev["interval"] == interval
    assert _is_ulid(ev["eid"]) and ev["eid"] != interval
    assert ev["session_id"] == SESSION
    assert ev["bead"] == "bd-timew-ahp"
    assert ev["tags"] == ["client:X", "case:Y", "svc:Z"]
    assert ev["group_id"] is None and ev["actor"] == "claude" and ev["role"] is None
    assert "ts" in ev


def test_stop_and_cancel_reference_interval(beads, session_log):
    interval = events.start_interval("b", [], session_id=SESSION)
    events.stop_interval(interval, session_id=SESSION)
    events.cancel_interval(interval, session_id=SESSION)
    _start, stop, cancel = _read_lines(session_log)
    assert stop["event"] == "stop" and stop["interval"] == interval
    assert cancel["event"] == "cancel" and cancel["interval"] == interval
    # stop/cancel carry common fields only — no provenance.
    assert "tags" not in stop and "bead" not in stop


def test_eids_are_distinct_across_events(beads, session_log):
    interval = events.start_interval("b", [], session_id=SESSION)
    events.stop_interval(interval, session_id=SESSION)
    eids = [ev["eid"] for ev in _read_lines(session_log)]
    assert len(set(eids)) == len(eids)


def test_appends_accumulate(beads, session_log):
    for _ in range(3):
        events.start_interval("b", [], session_id=SESSION)
    assert len(_read_lines(session_log)) == 3


# ---------------------------------------------------------------------------
# correction
# ---------------------------------------------------------------------------


def test_correction_subset_of_fields(beads, session_log):
    interval = events.start_interval("b", [], session_id=SESSION)
    events.correct_interval(
        interval, session_id=SESSION, start="2026-06-02T09:00:00-05:00", tags=["case:NEW"]
    )
    corr = _read_lines(session_log)[-1]
    assert corr["event"] == "correction" and corr["interval"] == interval
    assert corr["start"] == "2026-06-02T09:00:00-05:00"
    assert corr["tags"] == ["case:NEW"]
    assert "stop" not in corr and "group_id" not in corr  # untouched fields omitted


def test_correction_can_clear_provenance_to_null(beads, session_log):
    interval = events.start_interval("b", [], session_id=SESSION, group_id="swarm-1")
    events.correct_interval(interval, session_id=SESSION, group_id=None)
    corr = _read_lines(session_log)[-1]
    # sentinel distinguishes correct-to-null from untouched:
    assert "group_id" in corr and corr["group_id"] is None


def test_correction_requires_a_change(beads):
    with pytest.raises(ValueError):
        events.correct_interval("01HZZZZZZZZZZZZZZZZZZZZZZZZ", session_id=SESSION)


# ---------------------------------------------------------------------------
# log_dir resolution
# ---------------------------------------------------------------------------


def test_log_dir_uses_beads_dir_when_present(beads):
    assert events.log_dir() == beads / "bd-track" / "sessions"


def test_log_dir_falls_back_when_no_beads_dir(monkeypatch, tmp_path):
    def no_workspace(project_dir=None):
        raise SystemExit("no beads workspace")

    monkeypatch.setattr(events, "find_beads_dir", no_workspace)
    monkeypatch.setattr(events, "LOG_FALLBACK_DIR", tmp_path / "share")
    got = events.log_dir(project_dir=Path("/home/u/serverproj"))
    assert got.parent.parent == tmp_path / "share"
    assert got.name == "sessions"
    assert got.parent.name.startswith("serverproj-")  # project_id slug


# ---------------------------------------------------------------------------
# provenance + guards
# ---------------------------------------------------------------------------


def test_resolve_provenance_infers_actor(monkeypatch):
    monkeypatch.delenv("BD_TRACK_GROUP_ID", raising=False)
    monkeypatch.delenv("BD_TRACK_ACTOR", raising=False)
    monkeypatch.delenv("BD_TRACK_ROLE", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "uuid")
    assert events.resolve_provenance()["actor"] == "claude"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert events.resolve_provenance()["actor"] == "human"


def test_resolve_provenance_precedence(monkeypatch):
    monkeypatch.setenv("BD_TRACK_ACTOR", "env-actor")
    monkeypatch.setenv("BD_TRACK_GROUP_ID", "env-group")
    assert events.resolve_provenance(actor="explicit")["actor"] == "explicit"  # arg beats env
    assert events.resolve_provenance()["group_id"] == "env-group"  # env beats default


def test_normal_event_well_under_pipe_buf(beads):
    sid = "3a0f7512-da05-42c3-a540-90390f5fbbd6"  # realistic Claude UUID session
    events.start_interval(
        "bd-timew-ahp",
        [
            "client:PRJ001125 BOCO : BOCO : BOCO : BOCO AI Innovation",
            "case:01_AI Innovation (Project Task)",
            "svc:Technology Services",
        ],
        session_id=sid,
        actor="claude",
    )
    line = events._session_log_path(sid).read_bytes()
    assert len(line) < events.PIPE_BUF
    assert len(line) < 600  # the long-BOCO-tags worst case is ~400 bytes


def test_oversize_line_warns(beads, session_log, caplog):
    huge = ["x" * 5000]
    events.start_interval("b", huge, session_id=SESSION)
    assert any("PIPE_BUF" in r.message for r in caplog.records)


def test_safe_filename_sanitises():
    assert events._safe_filename("a/b c:d") == "a_b_c_d"
    assert events._safe_filename("blue.cat.tree") == "blue.cat.tree"
    assert events._safe_filename("3a0f-7512") == "3a0f-7512"
