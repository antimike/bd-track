"""XDG base-directory resolution (bd-timew-h31).

Every bd-track path under the user's home resolves through ``util.xdg_home``. The
module-level path constants are computed at import, so the end-to-end checks run
a fresh interpreter with a controlled environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bd_track import migrate, project, triage
from bd_track.util import xdg_home

XDG_VARS = {
    "config": "XDG_CONFIG_HOME",
    "cache": "XDG_CACHE_HOME",
    "state": "XDG_STATE_HOME",
    "data": "XDG_DATA_HOME",
}
DEFAULTS = {
    "config": (".config",),
    "cache": (".cache",),
    "state": (".local", "state"),
    "data": (".local", "share"),
}

# Prints every import-time path constant, keyed by the XDG base it should sit under.
_PROBE = """
import json
from bd_track import events, registry, session, doltpush, util
print(json.dumps({
    "config": [str(util._CONFIG_DIR), str(util.REPOS_CONFIG), str(registry.HOST_CONFIG),
               str(registry.DEFAULT_REGISTRY)],
    "cache": [str(util._CACHE_DIR), str(util.CLEANUP_STATE), str(util.CLEANUP_LOG),
              str(util.ACTIVITY_STATE), str(registry.PATH_CACHE)],
    "state": [str(session.SESSION_STATE_DIR), str(doltpush.PUSH_LOG)],
    "data": [str(events.LOG_FALLBACK_DIR), str(events._LEGACY_FALLBACK_DIR)],
}))
"""


def _probe(env: dict[str, str]) -> dict[str, list[str]]:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _base_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in XDG_VARS.values()}
    env["HOME"] = str(home)
    return env


# ---------------------------------------------------------------------------
# xdg_home
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(XDG_VARS))
def test_xdg_home_uses_absolute_env(monkeypatch, tmp_path, kind):
    monkeypatch.setenv(XDG_VARS[kind], str(tmp_path / kind))
    assert xdg_home(kind) == tmp_path / kind


@pytest.mark.parametrize("kind", sorted(XDG_VARS))
@pytest.mark.parametrize("value", [None, "", "relative/dir"])
def test_xdg_home_falls_back_to_default(monkeypatch, tmp_path, kind, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    if value is None:
        monkeypatch.delenv(XDG_VARS[kind], raising=False)
    else:
        monkeypatch.setenv(XDG_VARS[kind], value)
    assert xdg_home(kind) == tmp_path.joinpath(*DEFAULTS[kind])


# ---------------------------------------------------------------------------
# Import-time constants (fresh interpreter)
# ---------------------------------------------------------------------------


def test_constants_follow_xdg_vars(tmp_path):
    env = _base_env(tmp_path / "home")
    for kind, var in XDG_VARS.items():
        env[var] = str(tmp_path / f"xdg-{kind}")
    for kind, paths in _probe(env).items():
        base = tmp_path / f"xdg-{kind}"
        for p in paths:
            assert Path(p).is_relative_to(base), (kind, p)


def test_constants_default_under_home_when_unset(tmp_path):
    home = tmp_path / "home"
    for kind, paths in _probe(_base_env(home)).items():
        base = home.joinpath(*DEFAULTS[kind])
        for p in paths:
            assert Path(p).is_relative_to(base), (kind, p)


# ---------------------------------------------------------------------------
# Call-time paths
# ---------------------------------------------------------------------------


def test_inbox_root_follows_xdg_state(monkeypatch, tmp_path):
    monkeypatch.delenv("BD_TRACK_INBOX_ROOT", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert triage.inbox_root() == tmp_path / "state" / "triage"


def test_migrate_global_dirs_follow_xdg(monkeypatch, tmp_path):
    for kind, var in XDG_VARS.items():
        monkeypatch.setenv(var, str(tmp_path / kind))
    actions = migrate._plan_global(managed=set())
    assert {a.src.parent for a in actions} == {tmp_path / k for k in XDG_VARS}


def test_systemd_units_follow_xdg_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(project, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    project._install_systemd_units("1h", 4)
    units = list((tmp_path / "config" / "systemd" / "user").iterdir())
    assert units
    assert not (tmp_path / "home").exists()
