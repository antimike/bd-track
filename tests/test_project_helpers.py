"""Tests for pure helpers in project.py.

Covers cadence parsing and init-project's registry writes — the pieces with
real logic. Does NOT test cmd_init_project (heavy subprocess mocking;
deferred to a follow-up bead).
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest

from bd_track.project import (
    _INTERVAL_ALIASES,
    _ensure_auto_push_disabled,
    _registry_target,
    parse_cadence,
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
# registry target + registration (init-project's writes, bd-timew-d0t.4)
# ---------------------------------------------------------------------------


@pytest.fixture
def reg_home(tmp_path: Path, monkeypatch) -> Path:
    from bd_track import registry

    monkeypatch.delenv("BD_TRACK_REGISTRY", raising=False)
    monkeypatch.delenv("BD_TRACK_SCAN_ROOTS", raising=False)
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY", tmp_path / "projects.yaml")
    monkeypatch.setattr(registry, "REPOS_CONFIG", tmp_path / "repos.yaml")
    monkeypatch.setattr(registry, "HOST_CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(registry, "PATH_CACHE", tmp_path / "paths.json")
    return tmp_path


def _git(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def test_registry_target_new_existing_and_clashes(reg_home: Path):
    from bd_track import registry

    clone = _git(reg_home / "src" / "MyTool", "git@github.com:me/mytool.git")
    assert _registry_target(clone, None) == ("mytool", None)
    assert _registry_target(clone, "tool")[0] == "tool"
    registry.DEFAULT_REGISTRY.write_text(
        "projects:\n  tool:\n    remote: me/mytool\n  mytool:\n    remote: me/other\n"
    )
    name, existing = _registry_target(clone, None)
    assert name == "tool" and existing is not None  # found by remote
    with pytest.raises(SystemExit, match="already registered as 'tool'"):
        _registry_target(clone, "renamed")
    other = _git(reg_home / "src" / "mytool2", "git@github.com:me/x.git")
    with pytest.raises(SystemExit, match="taken by another project"):
        _registry_target(other, "mytool")
    with pytest.raises(SystemExit, match="not a valid project name"):
        _registry_target(other, "Bad Name")


def test_register_writes_identity_and_beads_keys(reg_home: Path, monkeypatch):
    from bd_track import project, registry

    monkeypatch.setattr(project, "_issue_prefix", lambda root: "mt")
    clone = _git(reg_home / "src" / "mytool", "git@github.com:me/mytool.git")
    project._register(clone, "mytool", {"commit_cadence": "1d", "server": None}, prefix=None)
    entry = registry.load_registry(scan=False).get("mytool")
    assert entry.remote == "me/mytool" and entry.path is None
    assert entry.beads == {"prefix": "mt", "commit_cadence": "1d"}
    project._register(
        clone, "mytool", {"commit_cadence": "12h", "server": {"enabled": True}}, prefix="zz"
    )
    project._register(clone, "mytool", {"server": None}, prefix=None)
    entry = registry.load_registry(scan=False).get("mytool")
    assert entry.beads == {"prefix": "mt", "commit_cadence": "12h"}  # prefix kept, server removed


def test_register_without_remote_writes_home_relative_path(reg_home: Path, monkeypatch):
    from bd_track import project, registry

    monkeypatch.setattr(project, "_issue_prefix", lambda root: None)
    monkeypatch.setattr(Path, "home", lambda: reg_home)
    clone = _git(reg_home / "src" / "local")
    project._register(clone, "local", {"compact_days": 3}, prefix=None)
    assert "path: ~/src/local" in registry.DEFAULT_REGISTRY.read_text()


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
