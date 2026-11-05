"""Tests for registry.py — project registry location, schemas, bridge, host config, writes."""

from __future__ import annotations

import json
import os
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
    monkeypatch.setattr(registry, "PATH_CACHE", tmp_path / "cache" / "registry-paths.json")
    monkeypatch.delenv("BD_TRACK_SCAN_ROOTS", raising=False)
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


@pytest.mark.parametrize("remote", ["not a remote", "", "justaname", "git@host:repo.git", "a b/c"])
def test_normalize_remote_rejects_junk(remote: str):
    assert normalize_remote(remote) is None


@pytest.mark.parametrize(
    "remote, key",
    [
        ("git@gitlab.com:group/sub/repo.git", "group/sub/repo"),  # nested path kept
        ("https://gitlab.example.org/group/sub/repo", "group/sub/repo"),
        ("ssh://git@host:2222/o/r.git", "o/r"),
        ("git+ssh://git@github.com/./o/r.git", "o/r"),  # Dolt's scp rewrite
        ("/srv/git/tools/x.git", "srv/git/tools/x"),
        ("file:///srv/git/x.git", "srv/git/x"),
        ("~/repos/x.git", "repos/x"),
    ],
)
def test_normalize_remote_keeps_full_path(remote: str, key: str):
    assert normalize_remote(remote) == key


@pytest.mark.parametrize(
    "remote, shorthand",
    [
        ("antimike/bd-track", True),
        ("a.b/c_d", True),
        ("git@github.com:o/r", False),
        ("https://github.com/o/r", False),
        ("/srv/o", False),
        ("./o/r", False),
        ("o/r/s", False),
        (".o/r", False),
    ],
)
def test_is_shorthand(remote: str, shorthand: bool):
    assert registry.is_shorthand(remote) is shorthand


def test_clone_url():
    from bd_track.registry import Project, clone_url

    host = {"default_url_prefix": "git@me.github.com:"}
    short = Project("a", remote="o/a", remote_spec="o/a")
    full = Project("b", remote="o/b", remote_spec="https://git.example.org/o/b.git")
    assert clone_url(short, host) == "git@me.github.com:o/a"
    assert clone_url(short, {"default_url_prefix": None}) is None
    assert clone_url(full, {"default_url_prefix": None}) == "https://git.example.org/o/b.git"
    assert clone_url(Project("c"), host) is None


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


def test_full_url_remotes_match_by_path(home: Path, tmp_path: Path, caplog):
    nested = _clone(tmp_path / "src" / "repo", "git@gl-alias:group/sub/repo.git")  # ssh alias
    other = _clone(tmp_path / "src" / "other", "git@gitlab.com:sub/repo.git")
    registry.DEFAULT_REGISTRY.write_text(
        "projects:\n"
        "  nested:\n    remote: https://gitlab.com/group/sub/repo.git\n"
        "  short:\n    remote: sub/repo\n"
        "  junk:\n    remote: not a url\n"
    )
    reg = load_registry()
    assert reg.get("nested").remote_spec == "https://gitlab.com/group/sub/repo.git"
    assert reg.for_directory(nested) is reg.get("nested")  # full path, host ignored
    assert reg.for_directory(other) is reg.get("short")  # not the nested project
    junk = reg.get("junk")
    assert (junk.remote, junk.remote_spec) == (None, None)
    assert "not a git URL or owner/repo" in caplog.text


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
        "dolt_remote_url": None,
        "dolt_push_timeout": 120,
        "state_export": False,
        "state_repo": None,
        "state_layout": "{project}/issues.jsonl",
        "host_name": registry.default_host_name(),
        "default_url_prefix": None,
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


@pytest.mark.parametrize("value", ["yes", "off", "sometimes"])
def test_host_state_export_rejects_non_switch_values(home: Path, value: str):
    registry.HOST_CONFIG.write_text(f"global: {{state_export: {value}}}\n")
    with pytest.raises(RegistryError, match="state_export must be"):
        load_host_config()


@pytest.mark.parametrize("value", ["0", "-5", "soon", "true"])
def test_host_dolt_push_timeout_validated(home: Path, value: str):
    registry.HOST_CONFIG.write_text(f"global: {{dolt_push_timeout: {value}}}\n")
    with pytest.raises(RegistryError, match="dolt_push_timeout must be"):
        load_host_config()


@pytest.mark.parametrize(
    "layout, problem",
    [
        ("issues.jsonl", "must contain"),
        ("{name}/issues.jsonl", "only the field"),
        ("/abs/{project}.jsonl", "relative path"),
        ("../{project}.jsonl", "relative path"),
    ],
)
def test_host_state_layout_validated(home: Path, layout: str, problem: str):
    registry.HOST_CONFIG.write_text(f"global: {{state_layout: '{layout}'}}\n")
    with pytest.raises(RegistryError, match=problem):
        load_host_config()


@pytest.mark.parametrize(
    "host, project, expected",
    [
        ("never", True, False),
        ("always", False, True),
        (False, None, False),
        (True, None, True),
        (False, True, True),
        (True, False, False),
        (False, "~/state", True),
    ],
)
def test_switch_enabled(host, project, expected):
    assert registry.switch_enabled(host, project) is expected


@pytest.mark.parametrize(
    "host, project, expected",
    [
        ("never", True, False),
        ("always", False, True),
        (False, None, False),
        (True, None, True),
        (False, True, True),
        (True, False, False),
    ],
)
def test_dolt_push_enabled(host, project, expected):
    from bd_track.registry import Project, dolt_push_enabled

    beads = {"prefix": "p"} if project is None else {"prefix": "p", "dolt_push": project}
    assert dolt_push_enabled(Project("p", beads=beads), {"dolt_push": host}) is expected


@pytest.mark.parametrize("key", ["dolt_push", "dolt_remote_private"])
@pytest.mark.parametrize("value", ["no", "off", "always", 1])
def test_dolt_push_project_values_must_be_bool(key: str, value):
    from bd_track.registry import Project, dolt_push_enabled

    with pytest.raises(RegistryError, match=f"beads.{key} must be true or false"):
        dolt_push_enabled(Project("p", beads={key: value}), {"dolt_push": False})


def test_dolt_push_yaml12_no_is_rejected_end_to_end(home: Path):
    registry.DEFAULT_REGISTRY.write_text(
        "projects:\n  p:\n    path: /tmp/p\n    beads: { prefix: p, dolt_push: no }\n"
    )
    project = registry.load_registry().get("p")
    assert project.beads["dolt_push"] == "no"  # YAML 1.2: a string, not false
    with pytest.raises(RegistryError, match="must be true or false"):
        registry.dolt_push_enabled(project, {"dolt_push": True})


def _host(**over) -> dict:
    return {
        "state_export": False,
        "state_repo": None,
        "state_layout": "{project}/issues.jsonl",
        **over,
    }


def test_state_export_target(tmp_path: Path):
    from bd_track.registry import Project, state_export_target

    work = tmp_path / "work-state"
    plain = Project("amo", beads={"prefix": "amo"})
    on = Project("amo", beads={"prefix": "amo", "state_repo": True})
    elsewhere = Project("client", beads={"prefix": "cl", "state_repo": str(work)})
    host = _host(state_repo=str(tmp_path / "state"))

    assert state_export_target(plain, host) is None  # default false, no override
    assert state_export_target(on, host) == (
        tmp_path / "state",
        tmp_path / "state" / "amo" / "issues.jsonl",
    )
    assert state_export_target(elsewhere, host) == (work, work / "client" / "issues.jsonl")
    assert state_export_target(on, {**host, "state_export": "never"}) is None
    d71 = {
        **host,
        "state_export": "always",
        "state_layout": "projects/{project}/beads/issues.jsonl",
    }
    assert state_export_target(plain, d71)[1] == (
        tmp_path / "state" / "projects" / "amo" / "beads" / "issues.jsonl"
    )


def test_state_export_target_errors():
    from bd_track.registry import Project, state_export_target

    with pytest.raises(RegistryError, match="no state repo is configured"):
        state_export_target(Project("amo", beads={"state_repo": True}), _host())
    with pytest.raises(RegistryError, match="must be true, false or a path"):
        state_export_target(Project("amo", beads={"state_repo": 3}), _host())


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


def test_resolve_project_dir(two_projects, tmp_path: Path, monkeypatch):
    from bd_track.util import resolve_project_dir

    chz, amo = two_projects
    (tmp_path / "docs").mkdir()
    monkeypatch.chdir(tmp_path)
    assert resolve_project_dir("dotfiles") == chz
    assert resolve_project_dir("chezmoi") == chz  # legacy basename still matches
    assert resolve_project_dir("amo") == amo
    assert resolve_project_dir(str(amo)) == amo
    with pytest.raises(SystemExit, match=r"'docs' is in the project registry.*clone was not found"):
        resolve_project_dir("docs")  # no known path; ./docs must not stand in for it
    with pytest.raises(SystemExit, match=r"neither a project in the registry.*dotfiles, amo"):
        resolve_project_dir("nowhere")


def test_resolve_project_dir_accepts_unregistered_directory(two_projects, tmp_path: Path):
    from bd_track.util import resolve_project_dir

    loose = tmp_path / "loose"
    loose.mkdir()
    assert resolve_project_dir(str(loose)) == loose.resolve()


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
        project,
        "cmd_cleanup",
        lambda days, commit, project_dir, project, pushed: calls.append((project_dir, days)),
    )
    project.cmd_run_service()
    chz, amo = two_projects
    assert calls == [(chz, 3), (amo, 7)]


def test_run_service_logs_broken_registry(home: Path, monkeypatch):
    from bd_track import project

    registry.DEFAULT_REGISTRY.write_text("projects: [\n")
    monkeypatch.setattr(project, "cmd_cleanup", lambda **kw: pytest.fail("should not run"))
    project.cmd_run_service()  # logs, does not raise


# ---------------------------------------------------------------------------
# for_directory
# ---------------------------------------------------------------------------


def test_for_directory(home: Path, tmp_path: Path):
    pinned = _clone(tmp_path / "docs" / "resume", "git@github.com:me/resume.git")
    other = _clone(tmp_path / "old" / "resume", "git@github.com:me/resume.git")
    worktree = _clone(tmp_path / "wt" / "feature", "git@github.com:me/dotfiles.git")
    bare = _clone(tmp_path / "bare", None)
    registry.DEFAULT_REGISTRY.write_text(
        "projects:\n"
        f"  resume:\n    remote: me/resume\n    path: {pinned}\n"
        "  dots:\n    remote: me/dotfiles\n"
    )
    reg = load_registry()
    assert reg.for_directory(pinned).name == "resume"
    assert reg.for_directory(other) is None  # the remote is pinned to another clone
    assert reg.for_directory(worktree).name == "dots"
    assert reg.for_directory(bare) is None


# ---------------------------------------------------------------------------
# finding clones of path-less projects (d0t.3)
# ---------------------------------------------------------------------------

REMOTE_ONLY = "projects:\n  tool:\n    remote: me/tool\n    beads: { prefix: tl }\n"


def _cache(home: Path) -> dict:
    return json.loads(registry.PATH_CACHE.read_text())


def test_no_roots_no_scan(home: Path, tmp_path: Path):
    _clone(tmp_path / "src" / "tool", "git@github.com:me/tool.git")
    registry.DEFAULT_REGISTRY.write_text(REMOTE_ONLY)
    assert load_registry().get("tool").path is None
    assert not registry.PATH_CACHE.exists()


def test_scan_finds_and_caches(home: Path, tmp_path: Path, monkeypatch):
    clone = _clone(tmp_path / "src" / "deep" / "tool", "git@alias.github.com:me/tool.git")
    _clone(tmp_path / "src" / "other", "git@github.com:me/other.git")
    monkeypatch.setenv("BD_TRACK_SCAN_ROOTS", f"{tmp_path / 'src'}{os.pathsep}{tmp_path / 'gone'}")
    registry.DEFAULT_REGISTRY.write_text(REMOTE_ONLY)
    reg = load_registry()
    assert reg.get("tool").path == clone.resolve()
    assert reg.beads_projects() == [reg.get("tool")]  # now a cleanup target
    assert _cache(home)["paths"] == {"me/tool": str(clone.resolve())}
    assert "scanned_at" in _cache(home)


def test_cache_used_without_scan_and_dropped_when_stale(home: Path, tmp_path: Path, monkeypatch):
    clone = _clone(tmp_path / "src" / "tool", "git@github.com:me/tool.git")
    monkeypatch.setenv("BD_TRACK_SCAN_ROOTS", str(tmp_path / "src"))
    registry.DEFAULT_REGISTRY.write_text(REMOTE_ONLY)
    load_registry()
    monkeypatch.setattr(registry, "find_clones", lambda roots: pytest.fail("scanned"))
    assert load_registry().get("tool").path == clone.resolve()  # cache, no scan
    subprocess.run(
        ["git", "-C", str(clone), "remote", "set-url", "origin", "x:me/else.git"], check=True
    )
    assert load_registry(scan=False).get("tool").path is None  # origin changed: dropped
    assert _cache(home)["paths"] == {}


def test_scan_respects_ttl_and_force(home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BD_TRACK_SCAN_ROOTS", str(tmp_path / "src"))
    (tmp_path / "src").mkdir()
    registry.DEFAULT_REGISTRY.write_text(REMOTE_ONLY)
    load_registry()  # scans (nothing found), stamps scanned_at
    calls = []
    real = registry.find_clones
    monkeypatch.setattr(registry, "find_clones", lambda roots: calls.append(1) or real(roots))
    load_registry()
    assert calls == []  # within SCAN_TTL
    clone = _clone(tmp_path / "src" / "tool", "git@github.com:me/tool.git")
    assert load_registry(scan=True).get("tool").path == clone.resolve()
    assert calls == [1]


def test_ambiguous_clones_resolve_none(home: Path, tmp_path: Path, monkeypatch, caplog):
    _clone(tmp_path / "src" / "tool", "git@github.com:me/tool.git")
    _clone(tmp_path / "src" / "old" / "tool", "https://github.com/me/tool")
    monkeypatch.setenv("BD_TRACK_SCAN_ROOTS", str(tmp_path / "src"))
    registry.DEFAULT_REGISTRY.write_text(REMOTE_ONLY)
    assert load_registry().get("tool").path is None
    assert "2 clones of me/tool" in caplog.text and "pin one with 'path:'" in caplog.text
    assert _cache(home)["paths"] == {}


def test_scan_skips_worktrees_pinned_clones_and_depth(home: Path, tmp_path: Path, monkeypatch):
    main = _clone(tmp_path / "src" / "tool", "git@github.com:me/tool.git")
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(tmp_path / "src" / "tool-wt")],
        check=True,
    )
    pinned = _clone(tmp_path / "src" / "res", "git@github.com:me/res.git")
    _clone(
        tmp_path / "src" / "a" / "b" / "c" / "d" / "e" / "f" / "far", "git@github.com:me/far.git"
    )
    monkeypatch.setenv("BD_TRACK_SCAN_ROOTS", str(tmp_path / "src"))
    registry.DEFAULT_REGISTRY.write_text(
        REMOTE_ONLY
        + f"  res:\n    remote: me/res\n    path: {pinned}\n"
        + "  res2:\n    remote: me/res\n"
        + "  far:\n    remote: me/far\n"
    )
    reg = load_registry()
    assert reg.get("tool").path == main.resolve()  # the worktree did not make it ambiguous
    assert reg.get("res2").path is None  # the only clone is pinned to res
    assert reg.get("far").path is None  # deeper than SCAN_MAX_DEPTH


def test_scan_roots_from_host_config(home: Path, tmp_path: Path):
    clone = _clone(tmp_path / "code" / "tool", "git@github.com:me/tool.git")
    registry.HOST_CONFIG.write_text(f"global:\n  scan_roots: [{tmp_path / 'code'}]\n")
    registry.DEFAULT_REGISTRY.write_text(REMOTE_ONLY)
    assert load_registry().get("tool").path == clone.resolve()
    registry.HOST_CONFIG.write_text("global:\n  scan_roots: ~/code\n")
    with pytest.raises(RegistryError, match="scan_roots must be a list"):
        registry.scan_roots()


def test_resolve_project_dir_forces_scan_on_miss(home: Path, tmp_path: Path, monkeypatch):
    from bd_track.util import resolve_project_dir

    monkeypatch.setenv("BD_TRACK_SCAN_ROOTS", str(tmp_path / "src"))
    (tmp_path / "src").mkdir()
    registry.DEFAULT_REGISTRY.write_text(REMOTE_ONLY)
    load_registry()  # stamps scanned_at: the TTL alone would skip the next scan
    clone = _clone(tmp_path / "src" / "tool", "git@github.com:me/tool.git")
    assert resolve_project_dir("tool") == clone.resolve()


# ---------------------------------------------------------------------------
# set_host, set_project deletes, migrate registry (d0t.4)
# ---------------------------------------------------------------------------


def test_set_host_round_trips(home: Path):
    registry.HOST_CONFIG.write_text("# mine\nglobal:\n  dolt_push: never  # WSL\n")
    registry.set_host({"idle_stop_hours": 2})
    text = registry.HOST_CONFIG.read_text()
    assert text.startswith("# mine\n") and "dolt_push: never  # WSL" in text
    assert load_host_config()["idle_stop_hours"] == 2
    with pytest.raises(RegistryError, match="unknown host setting"):
        registry.set_host({"colour": "red"})


def test_set_host_creates_file(home: Path):
    registry.set_host({"check_interval": "weekly"})
    assert load_host_config()["check_interval"] == "weekly"


def test_set_project_none_removes_owned_key(home: Path):
    registry.DEFAULT_REGISTRY.write_text(REGISTRY_TEXT)
    set_project("amontillado", {"beads": {"server": {"enabled": True}, "compact_days": 3}})
    set_project("amontillado", {"beads": {"server": None}})
    assert load_registry().get("amontillado").beads == {"prefix": "amo", "compact_days": 3}


@pytest.fixture
def legacy_setup(home: Path, tmp_path: Path, monkeypatch) -> dict[str, Path]:
    from bd_track import project

    prefixes = {"newtool": "nt", "scratch": "sc", "docs": "dc"}
    monkeypatch.setattr(project, "_issue_prefix", lambda root: prefixes.get(root.name))
    chz = _clone(
        tmp_path / "share" / "chezmoi", "git@alias.github.com:antimike/chezmoi-dotfiles.git"
    )
    new = _clone(tmp_path / "src" / "newtool", "git@github.com:me/newtool.git")
    bare = _clone(tmp_path / "src" / "scratch", None)
    taken = _clone(tmp_path / "src" / "docs", "git@github.com:me/docs-fork.git")
    registry.DEFAULT_REGISTRY.write_text(
        "# seeded\n"
        "projects:\n"
        "  chezmoi:\n    remote: antimike/chezmoi-dotfiles\n"
        "    beads: { prefix: chz, compact_days: 3 }\n"
        "  docs:\n    remote: me/docs\n"
    )
    registry.REPOS_CONFIG.write_text(
        "global: {check_interval: daily, idle_stop_hours: 4}\n"
        "repos:\n"
        f"- path: {chz}\n  commit_cadence: 1d\n  compact_days: 7\n"
        f"- path: {new}\n  commit_cadence: 2d\n"
        f"- path: {bare}\n  compact_days: 5\n"
        f"- path: {taken}\n"
    )
    return {"chz": chz, "new": new, "bare": bare, "taken": taken}


def test_migrate_dry_run_writes_nothing(legacy_setup, home: Path, capsys):
    from bd_track.migrate import cmd_migrate_registry

    before = registry.DEFAULT_REGISTRY.read_text()
    cmd_migrate_registry()
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "chezmoi: merge into existing" in out
    assert "SKIP" in out and "'docs' is taken" in out
    assert registry.DEFAULT_REGISTRY.read_text() == before
    assert registry.REPOS_CONFIG.exists() and not registry.HOST_CONFIG.exists()


def test_migrate_apply_with_skip_keeps_repos_yaml(legacy_setup, home: Path):
    from bd_track.migrate import cmd_migrate_registry

    cmd_migrate_registry(apply=True)
    reg = load_registry(scan=False)
    assert reg.get("chezmoi").beads == {"prefix": "chz", "compact_days": 3, "commit_cadence": "1d"}
    assert reg.get("newtool").remote == "me/newtool"
    assert reg.get("scratch").beads == {"prefix": "sc", "compact_days": 5}
    assert "# seeded" in registry.DEFAULT_REGISTRY.read_text()
    assert registry.REPOS_CONFIG.exists()  # 'docs' entry could not move
    assert load_host_config()["idle_stop_hours"] == 4


def test_migrate_apply_renames_and_keeps_resolution(legacy_setup, home: Path):
    from bd_track.migrate import cmd_migrate_registry

    registry.REPOS_CONFIG.write_text(
        registry.REPOS_CONFIG.read_text().replace(f"- path: {legacy_setup['taken']}\n", "")
    )

    def view() -> dict:
        return {
            p.name: (p.path, {k: v for k, v in p.beads.items() if k != "prefix"})
            for p in load_registry(scan=False).beads_projects()
        }

    before = view()
    cmd_migrate_registry(apply=True)
    assert not registry.REPOS_CONFIG.exists()
    assert registry.REPOS_CONFIG.with_name("repos.yaml.migrated").exists()
    assert view() == before  # no scan roots: the seeded cache keeps every clone resolving
    cmd_migrate_registry(apply=True)  # second run: nothing to migrate


def test_migrate_never_overwrites_host_config(legacy_setup, home: Path):
    from bd_track.migrate import cmd_migrate_registry

    registry.HOST_CONFIG.write_text("global: {idle_stop_hours: 9}\n")
    cmd_migrate_registry(apply=True)
    assert load_host_config()["idle_stop_hours"] == 9


def test_migrate_skips_entry_without_prefix(legacy_setup, home: Path, monkeypatch, capsys):
    from bd_track import project
    from bd_track.migrate import cmd_migrate_registry

    registry.REPOS_CONFIG.write_text(
        registry.REPOS_CONFIG.read_text().replace(f"- path: {legacy_setup['taken']}\n", "")
    )
    monkeypatch.setattr(project, "_issue_prefix", lambda root: None)
    cmd_migrate_registry(apply=True)
    assert "could not read its beads prefix" in capsys.readouterr().out
    assert registry.REPOS_CONFIG.exists()
    assert load_registry(scan=False).get("chezmoi").beads["commit_cadence"] == "1d"  # has prefix


def test_state_sessions_dir(tmp_path: Path):
    from bd_track.registry import Project, state_sessions_dir

    amo = Project("amo", beads={})
    per_project = _host(state_layout="projects/{project}/beads/issues.jsonl")
    f = tmp_path / "projects" / "amo" / "beads" / "issues.jsonl"
    assert state_sessions_dir(amo, per_project, f) == f.parent / "sessions"
    flat = _host(state_layout="beads/{project}.jsonl")
    g = tmp_path / "beads" / "amo.jsonl"
    assert state_sessions_dir(amo, flat, g) == tmp_path / "beads" / "sessions" / "amo"


@pytest.mark.parametrize(
    "name, ok",
    [
        ("laptop", True),
        ("wsl-box_2", True),
        ("a.b", True),
        ("", False),
        ("a/b", False),
        (".hidden", False),
        ("x.lock", False),
        ("end.", False),
        ("a..b", False),
        ("sp ace", False),
        ("a:b", False),
        ("@", False),
        ("a@{b", False),
        ("a~1", False),
        ("a^", False),
        ("a*", False),
        ("a[b", False),
        ("a\\b", False),
        (3, False),
    ],
)
def test_is_ref_component(name, ok):
    assert registry.is_ref_component(name) is ok


def test_host_name_default_and_validation(home: Path, monkeypatch):
    monkeypatch.setattr(registry.socket, "gethostname", lambda: "Box.example.org")
    assert load_host_config()["host_name"] == "box"
    registry.HOST_CONFIG.write_text("global: {host_name: work-laptop}\n")
    assert load_host_config()["host_name"] == "work-laptop"
    registry.HOST_CONFIG.write_text("global: {host_name: 'a/b'}\n")
    with pytest.raises(RegistryError, match="host_name must be"):
        load_host_config()


@pytest.mark.parametrize("value", ["''", "git@github.com", "3", "true"])
def test_default_url_prefix_validated(home: Path, value: str):
    registry.HOST_CONFIG.write_text(f"global: {{default_url_prefix: {value}}}\n")
    with pytest.raises(RegistryError, match="default_url_prefix must be"):
        load_host_config()


def test_default_url_prefix_accepted(home: Path):
    registry.HOST_CONFIG.write_text("global: {default_url_prefix: 'git@me.github.com:'}\n")
    assert load_host_config()["default_url_prefix"] == "git@me.github.com:"
