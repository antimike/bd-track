"""Tests for the bd-timew → bd-track back-compat shims (bd-timew-9hn).

The "Full rename + fallbacks" cutover keeps old-name env vars and on-disk
artifacts readable so an upgraded bd-track keeps working on a project still
laid out for bd-timew, until `bd-track migrate rename` cleans it up.
"""

from __future__ import annotations

from bd_track import billing
from bd_track.util import env_compat, path_compat

# ---------------------------------------------------------------------------
# env_compat
# ---------------------------------------------------------------------------

def test_env_compat_prefers_new_name(monkeypatch):
    monkeypatch.setenv("BD_TRACK_SCOPE", "new")
    monkeypatch.setenv("BD_TIMEW_SCOPE", "old")
    assert env_compat("BD_TRACK_SCOPE") == "new"


def test_env_compat_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    monkeypatch.setenv("BD_TIMEW_SCOPE", "old")
    assert env_compat("BD_TRACK_SCOPE") == "old"


def test_env_compat_default_when_neither(monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    monkeypatch.delenv("BD_TIMEW_SCOPE", raising=False)
    assert env_compat("BD_TRACK_SCOPE", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# path_compat
# ---------------------------------------------------------------------------

def test_path_compat_prefers_new(tmp_path):
    new = tmp_path / "bd-track"
    old = tmp_path / "bd-timew"
    new.mkdir()
    old.mkdir()
    assert path_compat(new, old) == new


def test_path_compat_uses_old_only_when_new_absent(tmp_path):
    new = tmp_path / "bd-track"
    old = tmp_path / "bd-timew"
    old.mkdir()  # only the legacy one exists
    assert path_compat(new, old) == old


def test_path_compat_defaults_to_new_when_neither(tmp_path):
    new = tmp_path / "bd-track"
    old = tmp_path / "bd-timew"
    assert path_compat(new, old) == new


# ---------------------------------------------------------------------------
# sidecar fallback (the live-project case)
# ---------------------------------------------------------------------------

def test_sidecar_reads_legacy_bd_timew_yaml(tmp_path):
    bdir = tmp_path / ".beads"
    bdir.mkdir()
    (bdir / "bd-timew.yaml").write_text("default:\n  client: LegacyCo\n")
    data = billing.load_sidecar(bdir)
    assert data["default"]["client"] == "LegacyCo"


def test_sidecar_prefers_new_bd_track_yaml(tmp_path):
    bdir = tmp_path / ".beads"
    bdir.mkdir()
    (bdir / "bd-timew.yaml").write_text("default:\n  client: LegacyCo\n")
    (bdir / "bd-track.yaml").write_text("default:\n  client: NewCo\n")
    data = billing.load_sidecar(bdir)
    assert data["default"]["client"] == "NewCo"
