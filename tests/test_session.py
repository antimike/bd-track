"""Tests for session-id resolution (session.py, bd-timew-sfk).

Covers the resolution precedence (flag → env → pointer → generate), the
pointer's continuity + staleness behaviour, the active-collision guard, and
project-id derivation. Pointer tests are hermetic: SESSION_STATE_DIR is
redirected to a tmp dir, the caller key is pinned, env vars are cleared, and
generation is stubbed (a separate guarded test exercises real xkcdpass).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from bd_track import session


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Hermetic session env: tmp state dir, pinned caller key, no env injection."""
    monkeypatch.setattr(session, "SESSION_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(session, "_session_key", lambda: "tty:/dev/pts/test")
    monkeypatch.delenv(session.ENV_SESSION, raising=False)
    monkeypatch.delenv(session.ENV_CLAUDE_SESSION, raising=False)
    return tmp_path


PROJECT = Path("/home/u/proj")


# ---------------------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------------------


def test_explicit_flag_wins(isolated, monkeypatch):
    monkeypatch.setenv(session.ENV_SESSION, "from-env")
    monkeypatch.setenv(session.ENV_CLAUDE_SESSION, "from-claude")
    assert session.resolve_session_id(PROJECT, explicit="explicit.id") == "explicit.id"


def test_bd_track_env_used(isolated, monkeypatch):
    monkeypatch.setenv(session.ENV_SESSION, "from-env")
    assert session.resolve_session_id(PROJECT) == "from-env"


def test_claude_env_used(isolated, monkeypatch):
    monkeypatch.setenv(session.ENV_CLAUDE_SESSION, "claude-uuid")
    assert session.resolve_session_id(PROJECT) == "claude-uuid"


def test_bd_track_env_beats_claude_env(isolated, monkeypatch):
    monkeypatch.setenv(session.ENV_SESSION, "from-env")
    monkeypatch.setenv(session.ENV_CLAUDE_SESSION, "claude-uuid")
    assert session.resolve_session_id(PROJECT) == "from-env"


def test_injected_id_does_not_write_pointer(isolated, monkeypatch):
    monkeypatch.setenv(session.ENV_CLAUDE_SESSION, "claude-uuid")
    session.resolve_session_id(PROJECT)
    assert not session._pointer_path(PROJECT).exists()


# ---------------------------------------------------------------------------
# Pointer continuity + generation
# ---------------------------------------------------------------------------


def test_generates_and_persists_pointer(isolated, monkeypatch):
    monkeypatch.setattr(session, "_generate_session_id", lambda taken: "blue.cat.tree")
    got = session.resolve_session_id(PROJECT)
    assert got == "blue.cat.tree"
    data = json.loads(session._pointer_path(PROJECT).read_text())
    assert data["tty:/dev/pts/test"]["session_id"] == "blue.cat.tree"


def test_reuses_pointer_without_regenerating(isolated, monkeypatch):
    calls = {"n": 0}

    def gen(taken):
        calls["n"] += 1
        return "blue.cat.tree"

    monkeypatch.setattr(session, "_generate_session_id", gen)
    first = session.resolve_session_id(PROJECT)
    second = session.resolve_session_id(PROJECT)
    assert first == second == "blue.cat.tree"
    assert calls["n"] == 1  # second call reused the pointer, no regeneration


def test_stale_entry_mints_new_id(isolated, monkeypatch):
    path = session._pointer_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_h = session.POINTER_STALE_HOURS + 1
    old = (session._now() - dt.timedelta(hours=stale_h)).isoformat()
    path.write_text(
        json.dumps({"tty:/dev/pts/test": {"session_id": "old.dead.id", "last_seen": old}})
    )

    monkeypatch.setattr(session, "_generate_session_id", lambda taken: "fresh.new.id")
    assert session.resolve_session_id(PROJECT) == "fresh.new.id"


def test_generate_receives_active_ids_as_taken(isolated, monkeypatch):
    path = session._pointer_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = session._now().isoformat()
    path.write_text(
        json.dumps({"tty:/dev/pts/other": {"session_id": "live.other.id", "last_seen": fresh}})
    )

    captured = {}

    def gen(taken):
        captured["taken"] = taken
        return "x.y.z"

    monkeypatch.setattr(session, "_generate_session_id", gen)
    session.resolve_session_id(PROJECT)
    assert "live.other.id" in captured["taken"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_fresh_boundaries():
    now = session._now()
    assert session._is_fresh({"last_seen": now.isoformat()}, now)
    stale = (now - dt.timedelta(hours=session.POINTER_STALE_HOURS + 1)).isoformat()
    assert not session._is_fresh({"last_seen": stale}, now)
    assert not session._is_fresh({}, now)
    assert not session._is_fresh("not-a-dict", now)
    assert not session._is_fresh({"last_seen": "garbage"}, now)


def test_project_id_stable_and_path_unique():
    a = session.project_id(Path("/home/u/proj"))
    assert a == session.project_id(Path("/home/u/proj"))  # stable
    assert a.startswith("proj-")
    # Same basename, different path → different id.
    assert session.project_id(Path("/other/proj")) != a


def test_corrupt_pointer_resets_cleanly(isolated, monkeypatch):
    path = session._pointer_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")
    monkeypatch.setattr(session, "_generate_session_id", lambda taken: "recovered.id.here")
    assert session.resolve_session_id(PROJECT) == "recovered.id.here"


# ---------------------------------------------------------------------------
# Generation guard
# ---------------------------------------------------------------------------


def test_generate_avoids_active_collision(monkeypatch):
    xp = pytest.importorskip("xkcdpass.xkcd_password")
    seq = iter(["dup.dup.dup", "dup.dup.dup", "fresh.win.word"])
    monkeypatch.setattr(xp, "locate_wordfile", lambda: "wordfile")
    monkeypatch.setattr(xp, "generate_wordlist", lambda **k: ["a", "b", "c"])
    monkeypatch.setattr(xp, "generate_xkcdpassword", lambda words, **k: next(seq))
    assert session._generate_session_id({"dup.dup.dup"}) == "fresh.win.word"


def test_generate_real_format_is_three_dotted_words():
    pytest.importorskip("xkcdpass")
    got = session._generate_session_id(set())
    assert len(got.split(".")) == 3
    assert all(part.isalpha() for part in got.split("."))
