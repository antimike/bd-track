"""Tests for pure helpers in project.py.

Covers cadence parsing and repos config round-trip — the pieces with
real logic. Does NOT test cmd_init_project (heavy subprocess mocking;
deferred to a follow-up bead).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pytest

from bd_track.project import (
    _INTERVAL_ALIASES,
    _ensure_auto_push_disabled,
    _get_repo_entry,
    load_repos_config,
    parse_cadence,
    save_repos_config,
)

# ---------------------------------------------------------------------------
# parse_cadence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec, expected",
    [
        ("1d", dt.timedelta(days=1)),
        ("7d", dt.timedelta(days=7)),
        ("12h", dt.timedelta(hours=12)),
        ("30m", dt.timedelta(minutes=30)),
        ("  3d  ", dt.timedelta(days=3)),
    ],
)
def test_parse_cadence_valid(spec, expected):
    assert parse_cadence(spec) == expected


@pytest.mark.parametrize("spec", ["", "1", "x", "1y", "abc", "1.5d", "-1d"])
def test_parse_cadence_invalid_exits(spec):
    with pytest.raises(SystemExit):
        parse_cadence(spec)


def test_interval_aliases_resolvable():
    """Daily/weekly/hourly aliases should each parse via the alias map."""
    for alias, raw in _INTERVAL_ALIASES.items():
        assert parse_cadence(raw) > dt.timedelta(0), f"alias {alias} → {raw} did not parse"


# ---------------------------------------------------------------------------
# repos config round-trip
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_config_path(tmp_path: Path):
    """Redirect REPOS_CONFIG to a tmp file for the duration of the test."""
    config_file = tmp_path / "repos.yaml"
    with patch("bd_track.project.REPOS_CONFIG", config_file), \
         patch("bd_track.util.REPOS_CONFIG", config_file):
        yield config_file


def test_load_missing_config_returns_skeleton(patched_config_path):
    data = load_repos_config()
    assert data == {
        "global": {"defaults": {"statuses": [], "hooks": []}},
        "repos": [],
    }


def test_save_then_load_round_trip(patched_config_path):
    data = {
        "global": {"defaults": {"statuses": ["review"], "hooks": []}, "idle_stop_hours": 4},
        "repos": [
            {"path": "/home/x/proj1", "commit_cadence": "1d", "compact_days": 7,
             "statuses": [], "server": {"enabled": True, "pass_path": "beads/proj1",
                                        "dolt_user": "proj1"}},
        ],
    }
    save_repos_config(data)
    loaded = load_repos_config()
    assert loaded == data


def test_load_supplies_missing_global_defaults(patched_config_path):
    """A config file with only `repos:` should round-trip with defaults filled in."""
    patched_config_path.write_text("repos: []\n")
    data = load_repos_config()
    assert data["global"]["defaults"]["statuses"] == []
    assert data["global"]["defaults"]["hooks"] == []


# ---------------------------------------------------------------------------
# _get_repo_entry
# ---------------------------------------------------------------------------

def test_get_repo_entry_finds_by_path():
    config = {"repos": [{"path": "/a"}, {"path": "/b"}]}
    assert _get_repo_entry(config, "/b") == {"path": "/b"}


def test_get_repo_entry_returns_none_when_absent():
    config = {"repos": [{"path": "/a"}]}
    assert _get_repo_entry(config, "/missing") is None


def test_get_repo_entry_handles_empty_repos():
    assert _get_repo_entry({"repos": []}, "/anything") is None


# ---------------------------------------------------------------------------
# _ensure_auto_push_disabled (PROJ-i85 / pitfalls-beads-dolt-remote)
# ---------------------------------------------------------------------------

def test_ensure_auto_push_disabled_creates_nested_when_empty(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    _ensure_auto_push_disabled(beads)
    assert (beads / "config.yaml").read_text() == "dolt:\n  auto-push: false\n"


def test_ensure_auto_push_disabled_appends_flat_to_existing(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    cfg = beads / "config.yaml"
    cfg.write_text("sync:\n  remote: git+ssh://example/foo.git\n")
    _ensure_auto_push_disabled(beads)
    out = cfg.read_text()
    # Existing content preserved
    assert "sync:\n  remote: git+ssh://example/foo.git\n" in out
    # New flat-key form appended
    assert out.endswith("dolt.auto-push: false\n")


def test_ensure_auto_push_disabled_idempotent_nested(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    cfg = beads / "config.yaml"
    cfg.write_text("dolt:\n  auto-push: false\n")
    _ensure_auto_push_disabled(beads)
    # No change — already has the value
    assert cfg.read_text() == "dolt:\n  auto-push: false\n"


def test_ensure_auto_push_disabled_idempotent_flat(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    cfg = beads / "config.yaml"
    cfg.write_text("dolt.auto-push: false\n")
    _ensure_auto_push_disabled(beads)
    assert cfg.read_text() == "dolt.auto-push: false\n"


def test_ensure_auto_push_disabled_handles_missing_trailing_newline(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    cfg = beads / "config.yaml"
    cfg.write_text("sync:\n  remote: foo")  # no trailing newline
    _ensure_auto_push_disabled(beads)
    out = cfg.read_text()
    assert "sync:\n  remote: foo\ndolt.auto-push: false\n" == out
