"""Tests for pure helpers in project.py.

Covers cadence parsing and init-project's registry writes — the pieces with
real logic — plus cmd_init_project's host-settings contract (subprocesses stubbed)
and `config set`.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest

from bd_track.project import (
    _INTERVAL_ALIASES,
    _bd_config_settings,
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


def test_init_project_bad_host_config_exits_before_side_effects(reg_home: Path, monkeypatch):
    """A host config failing validation is a clean exit, not a traceback after writes."""
    from bd_track import project, registry

    registry.HOST_CONFIG.write_text('global:\n  host_name: "a/b"\n')
    proj = reg_home / "proj"
    (proj / ".beads").mkdir(parents=True)
    monkeypatch.setattr(project, "run", lambda *a, **k: pytest.fail("ran a command"))
    with pytest.raises(SystemExit, match=r"^bd-track: .*global\.host_name"):
        project.cmd_init_project(
            path=proj,
            days=7,
            commit_cadence="1d",
            statuses_arg="",
            hooks_arg=False,
            no_git_ops=False,
            install_systemd=False,
            check_interval="daily",
            idle_stop_hours=0,
            server_mode=False,
            dolt_user=None,
            pass_path=None,
            yes=True,
        )
    assert list((proj / ".beads").iterdir()) == []  # no config.yaml, no sidecar


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


# ---------------------------------------------------------------------------
# _bd_config_settings (init-project's bd config set pairs, bd-timew-4fv)
# ---------------------------------------------------------------------------


def test_bd_config_settings_defaults():
    assert _bd_config_settings(no_git_ops=False, statuses=[]) == [
        ("dolt.auto-commit", "batch"),
        ("backup.git-push", "false"),
    ]


def test_bd_config_settings_optional_keys():
    settings = dict(_bd_config_settings(no_git_ops=True, statuses=["review", "operator"]))
    assert settings["backup.git-push"] == "false"
    assert settings["no-git-ops"] == "true"
    assert settings["status.custom"] == "review,operator"
    assert "no-push" not in settings  # would gate the explicit `bd dolt push`


# ---------------------------------------------------------------------------
# cleanup: state export step (bd stubbed)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_bd(monkeypatch):
    """Replace project.run: record argv; ``bd export -o <file>`` writes a line to <file>."""
    from bd_track import project

    calls: list[list[str]] = []
    outcome = {"export_rc": 0}

    def fake_run(argv, check=False, capture=False, cwd=None):
        calls.append(argv)
        rc = 0
        if argv[:2] == ["bd", "export"]:
            rc = outcome["export_rc"]
            Path(argv[argv.index("-o") + 1]).write_text('{"id": "amo-1"}\n')
        return subprocess.CompletedProcess(argv, rc, stdout="ok", stderr="boom" if rc else "")

    monkeypatch.setattr(project, "run", fake_run)
    return calls, outcome


def _cleanup(tmp_path: Path, beads: dict, host: dict, monkeypatch):
    from bd_track import events, project, registry
    from bd_track.registry import Project

    root = tmp_path / "amo"
    (root / ".beads").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(events, "LOG_FALLBACK_DIR", tmp_path / "share" / "bd-track")
    monkeypatch.setattr(events, "_LEGACY_FALLBACK_DIR", tmp_path / "share" / "bd-timew")
    monkeypatch.setattr(
        registry,
        "load_host_config",
        lambda: {
            "dolt_push": False,
            "dolt_remote_url": None,
            "dolt_push_timeout": 120,
            "state_export": False,
            "state_repo": None,
            "state_layout": "{project}/issues.jsonl",
            **host,
        },
    )
    project.cmd_cleanup(7, project_dir=root, project=Project("amo", path=root, beads=beads))


def test_cleanup_exports_after_gc(tmp_path: Path, stub_bd, monkeypatch):
    calls, _ = stub_bd
    state = tmp_path / "state"
    state.mkdir()
    _cleanup(
        tmp_path, {"prefix": "amo", "state_repo": True}, {"state_repo": str(state)}, monkeypatch
    )
    assert [c[:2] for c in calls] == [["bd", "compact"], ["bd", "gc"], ["bd", "export"]]
    assert "--include-memories" in calls[-1]
    assert (state / "amo" / "issues.jsonl").read_text() == '{"id": "amo-1"}\n'
    assert not list((state / "amo").glob(".*.tmp"))


def test_cleanup_export_off_by_default(tmp_path: Path, stub_bd, monkeypatch):
    calls, _ = stub_bd
    _cleanup(tmp_path, {"prefix": "amo"}, {"state_repo": str(tmp_path)}, monkeypatch)
    assert ["bd", "export"] not in [c[:2] for c in calls]


def test_cleanup_export_missing_repo_is_skipped(tmp_path: Path, stub_bd, monkeypatch):
    calls, _ = stub_bd
    _cleanup(
        tmp_path,
        {"prefix": "amo"},
        {"state_export": "always", "state_repo": str(tmp_path / "nope")},
        monkeypatch,
    )
    assert ["bd", "export"] not in [c[:2] for c in calls]
    assert not (tmp_path / "nope").exists()


def test_cleanup_export_failure_keeps_previous_file(tmp_path: Path, stub_bd, monkeypatch):
    _, outcome = stub_bd
    outcome["export_rc"] = 1
    state = tmp_path / "state"
    (state / "amo").mkdir(parents=True)
    (state / "amo" / "issues.jsonl").write_text("previous\n")
    _cleanup(
        tmp_path,
        {"prefix": "amo"},
        {"state_export": "always", "state_repo": str(state)},
        monkeypatch,
    )
    assert (state / "amo" / "issues.jsonl").read_text() == "previous\n"
    assert not list((state / "amo").glob(".*.tmp"))


def test_cleanup_export_config_error_does_not_raise(tmp_path: Path, stub_bd, monkeypatch):
    calls, _ = stub_bd
    _cleanup(tmp_path, {"prefix": "amo", "state_repo": True}, {}, monkeypatch)  # no repo
    assert ["bd", "gc"] in [c[:2] for c in calls]


def test_cleanup_pushes_after_export(tmp_path: Path, stub_bd, monkeypatch):
    from bd_track import doltpush

    calls, _ = stub_bd
    pushed = []
    monkeypatch.setattr(
        doltpush,
        "push_project",
        lambda project, root, host, log, done: pushed.append((project.name, len(calls))),
    )
    state = tmp_path / "state"
    state.mkdir()
    _cleanup(
        tmp_path, {"prefix": "amo", "state_repo": True}, {"state_repo": str(state)}, monkeypatch
    )
    assert pushed == [("amo", 3)]  # after compact, gc and export


# ---------------------------------------------------------------------------
# cleanup: session event logs in the state export (bd-timew-q9o.1)
# ---------------------------------------------------------------------------


def _sessions(tmp_path: Path) -> Path:
    d = tmp_path / "amo" / ".beads" / "bd-track" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _export_on(tmp_path: Path, stub_bd, monkeypatch, layout="{project}/issues.jsonl") -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    _cleanup(
        tmp_path,
        {"prefix": "amo"},
        {"state_export": "always", "state_repo": str(state), "state_layout": layout},
        monkeypatch,
    )
    return state


def test_cleanup_exports_new_session_logs(tmp_path: Path, stub_bd, monkeypatch):
    logs = _sessions(tmp_path)
    (logs / "s1.jsonl").write_text('{"event":"start"}\n')
    (logs / "s2.jsonl").write_text('{"event":"stop"}\n')
    (logs / "notes.txt").write_text("not a log\n")
    state = _export_on(tmp_path, stub_bd, monkeypatch)
    out = state / "amo" / "sessions"
    assert sorted(p.name for p in out.iterdir()) == ["s1.jsonl", "s2.jsonl"]
    assert (out / "s1.jsonl").read_text() == '{"event":"start"}\n'


def test_cleanup_exports_grown_session_log_and_skips_unchanged(tmp_path, stub_bd, monkeypatch):
    logs = _sessions(tmp_path)
    (logs / "s1.jsonl").write_text("a\n")
    (logs / "s2.jsonl").write_text("b\n")
    state = _export_on(tmp_path, stub_bd, monkeypatch)
    out = state / "amo" / "sessions"
    (logs / "s1.jsonl").write_text("a\nmore\n")  # grown
    (out / "s2.jsonl").write_text("B\n")  # same size: treated as unchanged, not recopied
    _export_on(tmp_path, stub_bd, monkeypatch)
    assert (out / "s1.jsonl").read_text() == "a\nmore\n"
    assert (out / "s2.jsonl").read_text() == "B\n"
    assert not list(out.glob(".*.tmp"))


def test_cleanup_never_shrinks_a_backed_up_session_log(tmp_path, stub_bd, monkeypatch):
    logs = _sessions(tmp_path)
    (logs / "s1.jsonl").write_text("a\n")
    out = tmp_path / "state" / "amo" / "sessions"
    out.mkdir(parents=True)
    (out / "s1.jsonl").write_text("a\nlonger backup\n")
    _export_on(tmp_path, stub_bd, monkeypatch)
    assert (out / "s1.jsonl").read_text() == "a\nlonger backup\n"


def test_cleanup_exports_fallback_dir_session_logs(tmp_path: Path, stub_bd, monkeypatch):
    from bd_track.session import project_id

    logs = _sessions(tmp_path)
    (logs / "s1.jsonl").write_text("beads\n")
    (logs / "dup.jsonl").write_text("beads copy\n")
    fallback = tmp_path / "share" / "bd-track" / project_id(tmp_path / "amo") / "sessions"
    fallback.mkdir(parents=True)
    (fallback / "s9.jsonl").write_text("fallback\n")
    (fallback / "dup.jsonl").write_text("fallback copy, longer\n")
    state = _export_on(tmp_path, stub_bd, monkeypatch)
    out = state / "amo" / "sessions"
    assert sorted(p.name for p in out.iterdir()) == ["dup.jsonl", "s1.jsonl", "s9.jsonl"]
    assert (out / "s9.jsonl").read_text() == "fallback\n"
    assert (out / "dup.jsonl").read_text() == "beads copy\n"  # first dir wins


def test_cleanup_session_logs_flat_layout_get_a_project_dir(tmp_path, stub_bd, monkeypatch):
    (_sessions(tmp_path) / "s1.jsonl").write_text("a\n")
    state = _export_on(tmp_path, stub_bd, monkeypatch, layout="{project}.jsonl")
    assert (state / "sessions" / "amo" / "s1.jsonl").read_text() == "a\n"


def test_cleanup_session_logs_off_with_export(tmp_path: Path, stub_bd, monkeypatch):
    (_sessions(tmp_path) / "s1.jsonl").write_text("a\n")
    _cleanup(tmp_path, {"prefix": "amo"}, {"state_repo": str(tmp_path / "state")}, monkeypatch)
    assert not (tmp_path / "state").exists()


def test_cleanup_session_logs_copied_even_if_bd_export_fails(tmp_path, stub_bd, monkeypatch):
    _, outcome = stub_bd
    outcome["export_rc"] = 1
    (_sessions(tmp_path) / "s1.jsonl").write_text("a\n")
    state = _export_on(tmp_path, stub_bd, monkeypatch)
    assert (state / "amo" / "sessions" / "s1.jsonl").exists()


# ---------------------------------------------------------------------------
# init-project leaves host settings alone; config set writes them (bd-timew-ekm)
# ---------------------------------------------------------------------------

_HOST_YAML = (
    "# managed elsewhere\nglobal:\n  check_interval: weekly  # keep\n  idle_stop_hours: 6\n"
)


def _init_defaults(proj: Path, **overrides) -> None:
    from bd_track import project

    kwargs = dict(
        path=proj,
        days=None,
        commit_cadence=None,
        statuses_arg=None,
        hooks_arg=None,
        no_git_ops=False,
        install_systemd=True,
        check_interval=None,
        idle_stop_hours=None,
        server_mode=None,
        dolt_user=None,
        pass_path=None,
        yes=True,
    )
    kwargs.update(overrides)
    project.cmd_init_project(**kwargs)


@pytest.fixture
def init_env(reg_home: Path, monkeypatch) -> Path:
    """A new project, a hand-written config.yaml, and no real subprocesses or systemd."""
    from bd_track import project

    registry_host = reg_home / "config.yaml"
    registry_host.write_text(_HOST_YAML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(reg_home / "xdg"))
    monkeypatch.setattr(
        project,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(project.shutil, "which", lambda name: None)
    proj = _git(reg_home / "src" / "newproj")
    (proj / ".beads").mkdir()
    return proj


def test_init_project_defaults_leave_host_config_byte_identical(init_env: Path, reg_home: Path):
    before = (reg_home / "config.yaml").read_bytes()
    _init_defaults(init_env)
    assert (reg_home / "config.yaml").read_bytes() == before
    units = reg_home / "xdg" / "systemd" / "user"
    # the installed units take the host values, not hard-coded defaults
    assert "weekly" in (units / "bd-track-cleanup.timer").read_text()
    assert "--hours 6" in (units / "bd-track-idle-stop.service").read_text()


def test_init_project_flags_apply_to_units_only(init_env: Path, reg_home: Path, caplog):
    before = (reg_home / "config.yaml").read_bytes()
    with caplog.at_level("INFO", logger="bd-track"):
        _init_defaults(init_env, check_interval="daily", idle_stop_hours=2)
    assert (reg_home / "config.yaml").read_bytes() == before
    units = reg_home / "xdg" / "systemd" / "user"
    assert "--hours 2" in (units / "bd-track-idle-stop.service").read_text()
    assert "bd-track config set idle_stop_hours 2" in caplog.text


def test_config_set_writes_parsed_value_and_keeps_comments(reg_home: Path):
    from bd_track import project
    from bd_track.registry import load_host_config

    (reg_home / "config.yaml").write_text(_HOST_YAML)
    project.cmd_config_set("idle_stop_hours", "3")
    project.cmd_config_set("scan_roots", "[~/src, ~/work]")
    host = load_host_config()
    assert host["idle_stop_hours"] == 3
    assert host["scan_roots"] == ["~/src", "~/work"]
    text = (reg_home / "config.yaml").read_text()
    assert "# managed elsewhere" in text and "# keep" in text


def test_config_set_creates_missing_file(reg_home: Path):
    from bd_track import project
    from bd_track.registry import load_host_config

    project.cmd_config_set("dolt_push", "never")
    assert load_host_config()["dolt_push"] == "never"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("idle_stop_hours", "-1", "whole number"),
        ("idle_stop_hours", "true", "whole number"),
        ("check_interval", "[daily]", "must be a string"),
        ("dolt_push", "maybe", "dolt_push"),  # rejected by load_host_config, then rolled back
    ],
)
def test_config_set_rejects_invalid_values_without_writing(reg_home: Path, key, value, message):
    from bd_track import project

    (reg_home / "config.yaml").write_text(_HOST_YAML)
    with pytest.raises(SystemExit, match=message):
        project.cmd_config_set(key, value)
    assert (reg_home / "config.yaml").read_text() == _HOST_YAML


def test_config_set_refuses_while_repos_yaml_holds_host_settings(reg_home: Path):
    from bd_track import project

    (reg_home / "repos.yaml").write_text("global: {idle_stop_hours: 2}\nrepos: []\n")
    with pytest.raises(SystemExit, match="migrate registry"):
        project.cmd_config_set("idle_stop_hours", "3")
    assert not (reg_home / "config.yaml").exists()


def test_cli_host_keys_match_registry():
    from bd_track import cli, registry

    assert cli._HOST_KEYS == registry.HOST_KEYS
