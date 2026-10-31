"""Tests for registry.py — project registry location, schemas, bridge, host config, writes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bd_track import registry
from bd_track.registry import (
    RegistryError,
    load_host_config,
    load_registry,
    normalize_remote,
    registry_path,
    set_project,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Point every registry location at a temp dir; none of the files exist yet."""
    monkeypatch.delenv("BD_TRACK_REGISTRY", raising=False)
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY", tmp_path / "projects.yaml")
    monkeypatch.setattr(registry, "REPOS_CONFIG", tmp_path / "bd-track" / "repos.yaml")
    monkeypatch.setattr(registry, "HOST_CONFIG", tmp_path / "bd-track" / "config.yaml")
    (tmp_path / "bd-track").mkdir()
    return tmp_path


def _clone(path: Path, origin: str | None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


# ---------------------------------------------------------------------------
# normalize_remote
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "remote",
    [
        "antimike/amontillado",
        "git@github.com:antimike/amontillado.git",
        "git@antimike.github.com:antimike/amontillado.git",  # ssh host alias
        "https://github.com/antimike/amontillado",
        "https://github.com/Antimike/Amontillado.git/",
        "ssh://git@github.com/antimike/amontillado.git",
    ],
)
def test_normalize_remote_forms(remote: str):
    assert normalize_remote(remote) == "antimike/amontillado"


def test_normalize_remote_rejects_junk():
    assert normalize_remote("not a remote") is None


# ---------------------------------------------------------------------------
# location
# ---------------------------------------------------------------------------


def test_location_order(home: Path, monkeypatch):
    assert registry_path() is None
    registry.REPOS_CONFIG.write_text("repos: []\n")
    assert registry_path() == registry.REPOS_CONFIG
    registry.DEFAULT_REGISTRY.write_text("projects: {}\n")
    assert registry_path() == registry.DEFAULT_REGISTRY
    monkeypatch.setenv("BD_TRACK_REGISTRY", str(home / "env.yaml"))
    assert registry_path() == home / "env.yaml"
    assert registry_path(home / "flag.yaml") == home / "flag.yaml"


def test_explicit_missing_file_is_an_error(home: Path):
    with pytest.raises(RegistryError, match="no such registry file"):
        load_registry(home / "nope.yaml")


def test_no_files_is_empty(home: Path):
    reg = load_registry()
    assert reg.projects == {} and reg.source is None


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def test_new_schema(home: Path):
    registry.DEFAULT_REGISTRY.write_text(
        "# header comment\n"
        "projects:\n"
        "  amontillado:\n"
        "    remote: git@github.com:antimike/amontillado.git\n"
        "    path: ~/src/amo\n"
        "    beads: { prefix: amo, commit_cadence: 12h }\n"
        "  notes:\n"
        "    remote: antimike/notes\n"
        "  Bad_Name:\n"
        "    remote: x/y\n"
    )
    reg = load_registry()
    assert list(reg.projects) == ["amontillado", "notes"]
    amo = reg.get("amontillado")
    assert amo.remote == "antimike/amontillado"
    assert amo.path == (Path.home() / "src" / "amo").resolve()
    assert amo.beads == {"prefix": "amo", "commit_cadence": "12h"}
    assert amo.uses_beads and not reg.get("notes").uses_beads
    assert reg.beads_projects() == [amo]
    assert reg.by_path(Path("~/src/amo")) is amo


def test_legacy_schema(home: Path, tmp_path: Path):
    clone = _clone(tmp_path / "src" / "amontillado", "git@github.com:antimike/amontillado.git")
    bare = _clone(tmp_path / "src" / "scratch", None)
    registry.REPOS_CONFIG.write_text(
        f"global: {{check_interval: daily}}\n"
        f"repos:\n"
        f"- path: {clone}\n  commit_cadence: 1d\n  compact_days: 7\n  statuses: [review]\n"
        f"- path: {bare}\n"
        f"- commit_cadence: 1d\n"  # no path: skipped
    )
    reg = load_registry()
    assert reg.legacy
    assert list(reg.projects) == ["amontillado", "scratch"]
    amo = reg.get("amontillado")
    assert (amo.remote, amo.path) == ("antimike/amontillado", clone.resolve())
    assert amo.beads == {"commit_cadence": "1d", "compact_days": 7, "statuses": ["review"]}
    assert reg.get("scratch").remote is None
    assert [p.name for p in reg.beads_projects()] == ["amontillado", "scratch"]


def test_invalid_yaml_and_shape(home: Path):
    registry.DEFAULT_REGISTRY.write_text("projects: [\n")
    with pytest.raises(RegistryError, match="not valid YAML"):
        load_registry()
    registry.DEFAULT_REGISTRY.write_text("projects: [a, b]\n")
    with pytest.raises(RegistryError, match="must be a mapping"):
        load_registry()


# ---------------------------------------------------------------------------
# bridge from repos.yaml
# ---------------------------------------------------------------------------


def test_bridge_fills_path_and_settings_by_remote(home: Path, tmp_path: Path):
    clone = _clone(
        tmp_path / "share" / "chezmoi", "git@alias.github.com:antimike/chezmoi-dotfiles.git"
    )
    orphan = _clone(tmp_path / "src" / "orphan", "git@github.com:antimike/orphan.git")
    registry.DEFAULT_REGISTRY.write_text(
        "projects:\n"
        "  chezmoi:\n"
        "    remote: antimike/chezmoi-dotfiles\n"
        "    beads: { prefix: chz, compact_days: 3 }\n"
        "  bd-track:\n"
        "    remote: antimike/bd-track\n"
        "    beads: { prefix: bd-timew }\n"
    )
    registry.REPOS_CONFIG.write_text(
        f"repos:\n"
        f"- path: {clone}\n  commit_cadence: 1d\n  compact_days: 7\n"
        f"- path: {orphan}\n  commit_cadence: 2d\n"
    )
    reg = load_registry()
    chz = reg.get("chezmoi")
    assert chz.path == clone.resolve()
    assert chz.beads == {
        "prefix": "chz",
        "compact_days": 3,
        "commit_cadence": "1d",
    }  # registry wins
    assert reg.get("bd-track").path is None  # no legacy clone: not resolvable yet (d0t.3)
    assert reg.get("orphan").beads == {"commit_cadence": "2d"}  # unmatched entry kept
    assert [p.name for p in reg.beads_projects()] == ["chezmoi", "orphan"]


def test_bridge_name_clash_is_skipped(home: Path, tmp_path: Path):
    clone = _clone(tmp_path / "src" / "tools", "git@github.com:someone/tools.git")
    registry.DEFAULT_REGISTRY.write_text("projects:\n  tools:\n    remote: other/tools\n")
    registry.REPOS_CONFIG.write_text(f"repos:\n- path: {clone}\n")
    reg = load_registry()
    assert reg.get("tools").remote == "other/tools"
    assert reg.get("tools").path is None


def test_bridge_skipped_when_registry_is_repos_yaml(home: Path, tmp_path: Path, monkeypatch):
    clone = _clone(tmp_path / "src" / "a", None)
    registry.REPOS_CONFIG.write_text(f"repos:\n- path: {clone}\n")
    monkeypatch.setenv("BD_TRACK_REGISTRY", str(registry.REPOS_CONFIG))
    assert list(load_registry().projects) == ["a"]


# ---------------------------------------------------------------------------
# host config
# ---------------------------------------------------------------------------


def test_host_config_defaults_and_sources(home: Path):
    assert load_host_config() == {
        "check_interval": "daily",
        "idle_stop_hours": 4,
        "dolt_push": False,
        "defaults": {"statuses": [], "hooks": []},
    }
    registry.REPOS_CONFIG.write_text("global: {idle_stop_hours: 2}\nrepos: []\n")
    assert load_host_config()["idle_stop_hours"] == 2
    registry.HOST_CONFIG.write_text("global: {idle_stop_hours: 0, dolt_push: never}\n")
    host = load_host_config()
    assert (host["idle_stop_hours"], host["dolt_push"]) == (0, "never")


@pytest.mark.parametrize("value", ["yes", "on", "off", "1", "sometimes"])
def test_host_dolt_push_rejects_yaml11_booleans(home: Path, value: str):
    registry.HOST_CONFIG.write_text(f"global: {{dolt_push: {value}}}\n")
    with pytest.raises(RegistryError, match="dolt_push must be"):
        load_host_config()


@pytest.mark.parametrize("value, parsed", [("true", True), ("false", False), ("always", "always")])
def test_host_dolt_push_accepts(home: Path, value: str, parsed):
    registry.HOST_CONFIG.write_text(f"global: {{dolt_push: {value}}}\n")
    assert load_host_config()["dolt_push"] == parsed


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

REGISTRY_TEXT = """\
# Project registry: managed by hand.
projects:
  amontillado:
    remote: antimike/amontillado   # preferred over path
    beads: { prefix: amo }
    triage: true                   # another tool's key
  notes:
    remote: antimike/notes
"""


def test_set_project_round_trips_and_owns_keys(home: Path):
    registry.DEFAULT_REGISTRY.write_text(REGISTRY_TEXT)
    registry.DEFAULT_REGISTRY.chmod(0o640)
    set_project(
        "amontillado",
        {"remote": "someone/else", "beads": {"prefix": "xxx", "commit_cadence": "12h"}},
    )
    text = registry.DEFAULT_REGISTRY.read_text()
    assert text.startswith("# Project registry: managed by hand.\n")
    assert "remote: antimike/amontillado   # preferred over path" in text  # shared key unchanged
    assert "triage: true                   # another tool's key" in text
    reg = load_registry()
    assert reg.get("amontillado").beads == {"prefix": "amo", "commit_cadence": "12h"}
    assert list(reg.projects) == ["amontillado", "notes"]
    assert registry.DEFAULT_REGISTRY.stat().st_mode & 0o777 == 0o640


def test_set_project_adds_new_project(home: Path):
    registry.DEFAULT_REGISTRY.write_text(REGISTRY_TEXT)
    set_project("bd-track", {"remote": "antimike/bd-track", "beads": {"prefix": "bd-timew"}})
    reg = load_registry()
    assert reg.get("bd-track").remote == "antimike/bd-track"
    assert reg.get("bd-track").beads == {"prefix": "bd-timew"}
    assert "triage: true" in registry.DEFAULT_REGISTRY.read_text()


def test_set_project_creates_file_and_never_writes_legacy(home: Path):
    registry.REPOS_CONFIG.write_text("repos: []\n")
    target = set_project("fresh", {"path": "~/src/fresh", "beads": {"prefix": "fr"}})
    assert target == registry.DEFAULT_REGISTRY
    assert registry.REPOS_CONFIG.read_text() == "repos: []\n"
    assert load_registry().get("fresh").beads == {"prefix": "fr"}


def test_set_project_rejects_foreign_keys_and_bad_names(home: Path):
    with pytest.raises(RegistryError, match="does not own"):
        set_project("a", {"triage": True})
    with pytest.raises(RegistryError, match="does not own"):
        set_project("a", {"beads": {"color": "red"}})
    with pytest.raises(RegistryError, match="invalid project name"):
        set_project("Bad Name", {"beads": {"prefix": "x"}})
    assert not registry.DEFAULT_REGISTRY.exists()


def test_set_project_refuses_legacy_schema_file(home: Path, monkeypatch):
    path = home / "old.yaml"
    path.write_text("repos: []\n")
    with pytest.raises(RegistryError, match=r"legacy repos\.yaml schema"):
        set_project("a", {"beads": {"prefix": "x"}}, explicit=path)


# ---------------------------------------------------------------------------
# callers: --project, --global, the cleanup service
# ---------------------------------------------------------------------------


@pytest.fixture
def two_projects(home: Path, tmp_path: Path) -> tuple[Path, Path]:
    chz = _clone(tmp_path / "share" / "chezmoi", "git@github.com:antimike/chezmoi-dotfiles.git")
    amo = _clone(tmp_path / "src" / "amontillado", None)
    registry.DEFAULT_REGISTRY.write_text(
        "projects:\n"
        "  dotfiles:\n"
        "    remote: antimike/chezmoi-dotfiles\n"
        "    beads: { prefix: chz }\n"
        f"  amo:\n    path: {amo}\n    beads: {{ prefix: amo, commit_cadence: 12h }}\n"
        "  docs:\n    remote: antimike/docs\n"
    )
    registry.REPOS_CONFIG.write_text(f"repos:\n- path: {chz}\n  compact_days: 3\n")
    return chz.resolve(), amo.resolve()


def test_resolve_project_dir(two_projects):
    from bd_track.util import resolve_project_dir

    chz, amo = two_projects
    assert resolve_project_dir("dotfiles") == chz
    assert resolve_project_dir("chezmoi") == chz  # legacy basename still matches
    assert resolve_project_dir("amo") == amo
    assert resolve_project_dir(str(amo)) == amo
    with pytest.raises(SystemExit, match=r"not found in the project registry.*dotfiles, amo"):
        resolve_project_dir("docs")  # no known path


def test_all_project_dirs(two_projects, home: Path):
    from bd_track.util import all_project_dirs

    assert all_project_dirs() == list(two_projects)
    registry.DEFAULT_REGISTRY.write_text("projects: {}\n")
    registry.REPOS_CONFIG.unlink()
    with pytest.raises(SystemExit, match="--global requires"):
        all_project_dirs()


def test_run_service_uses_registry_settings(two_projects, tmp_path: Path, monkeypatch):
    from bd_track import project

    calls = []
    monkeypatch.setattr(project, "_load_cleanup_state", dict)
    monkeypatch.setattr(project, "_save_cleanup_state", lambda state: None)
    monkeypatch.setattr(
        project, "cmd_cleanup", lambda days, commit, project_dir: calls.append((project_dir, days))
    )
    project.cmd_run_service()
    chz, amo = two_projects
    assert calls == [(chz, 3), (amo, 7)]


def test_run_service_logs_broken_registry(home: Path, monkeypatch):
    from bd_track import project

    registry.DEFAULT_REGISTRY.write_text("projects: [\n")
    monkeypatch.setattr(project, "cmd_cleanup", lambda **kw: pytest.fail("should not run"))
    project.cmd_run_service()  # logs, does not raise
