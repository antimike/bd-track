"""Tests for the unified ``--project <name-or-path>`` flag (bd-timew-aji).

Two layers: the command layer (start/stop/status act on ``project_dir`` and run ``bd`` there,
whatever the cwd) and the CLI layer (``--project`` before or after the subcommand, the
deprecated ``--project-dir`` alias, and the conflicts that exit instead of being ignored).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bd_track import cli, queue, registry, track
from bd_track.aggregate import load_intervals, open_intervals
from bd_track.events import log_dir


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch) -> tuple[Path, list]:
    """A project with .beads/ and a cwd outside it; records the cwd of every bd call."""
    root = tmp_path / "proj"
    (root / ".beads").mkdir(parents=True)
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)

    calls: list = []

    def fake_issue(iid: str, *, cwd: Path | None = None) -> dict:
        calls.append(("show", iid, cwd))
        return {"id": iid, "title": "t", "labels": [], "status": "open"}

    monkeypatch.setattr(track, "get_issue", fake_issue)
    monkeypatch.setattr(queue, "get_issue", fake_issue)
    monkeypatch.setattr(
        "bd_track.util.run", lambda cmd, **k: calls.append((cmd[1], cmd[2], k.get("cwd")))
    )
    monkeypatch.setattr(track, "record_activity", lambda *a, **k: None)
    return root, calls


def _open(root: Path) -> list:
    return open_intervals(load_intervals(log_dir(root)))


def test_start_stop_act_on_project_dir_and_run_bd_there(elsewhere):
    root, calls = elsewhere
    queue.save_all_queues(root / ".beads", {"work": ["bead-1"]})

    track.cmd_start("bead-1", session_id="s1", project_dir=root)
    assert [iv.bead for iv in _open(root)] == ["bead-1"]
    assert ("update", "bead-1", root) in calls  # the claim lands in the same project
    assert all(c[2] == root for c in calls)

    calls.clear()
    track.cmd_stop(session_id="s1", project_dir=root)
    assert _open(root) == []
    # the post-stop queue sweep looked the queued bead up in the project, not the cwd
    assert ("show", "bead-1", root) in calls


def test_status_reads_project_dir(elsewhere, caplog):
    root, calls = elsewhere
    track.cmd_start("bead-1", session_id="s1", project_dir=root)
    calls.clear()
    with caplog.at_level("INFO", logger="bd-track"):
        track.cmd_status(session_id="s1", project_dir=root)
    assert "Tracking: bead-1" in caplog.text
    assert calls == [("show", "bead-1", root)]


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------


@pytest.fixture
def captured(tmp_path: Path, monkeypatch) -> dict:
    """Stub the commands the CLI tests dispatch to; record the project_dir each received.

    The registry points at an empty temp dir, so only directories resolve.
    """
    reg = tmp_path / "registry"
    monkeypatch.delenv("BD_TRACK_REGISTRY", raising=False)
    monkeypatch.delenv("BD_TRACK_SCAN_ROOTS", raising=False)
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY", reg / "projects.yaml")
    monkeypatch.setattr(registry, "REPOS_CONFIG", reg / "bd-track" / "repos.yaml")
    monkeypatch.setattr(registry, "HOST_CONFIG", reg / "bd-track" / "config.yaml")
    monkeypatch.setattr(registry, "PATH_CACHE", reg / "cache" / "registry-paths.json")
    seen: dict = {}

    def recorder(name: str):
        def _cmd(*args, project_dir=None, **kwargs):
            seen[name] = project_dir

        return _cmd

    monkeypatch.setattr(track, "cmd_status", recorder("status"))
    monkeypatch.setattr(track, "cmd_start", recorder("start"))
    monkeypatch.setattr(queue, "cmd_queue", recorder("queue"))
    return seen


def _main(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["bd-track", *argv])
    cli.main()


@pytest.mark.parametrize(
    "argv",
    [
        ("--project", "{root}", "status"),
        ("status", "--project", "{root}"),
    ],
)
def test_project_before_or_after_subcommand(monkeypatch, tmp_path, captured, argv):
    root = tmp_path / "proj"
    root.mkdir()
    _main(monkeypatch, *(a.format(root=root) for a in argv))
    assert captured["status"] == root.resolve()


def test_unregistered_path_is_used_as_given(monkeypatch, tmp_path, captured):
    root = tmp_path / "loose"
    root.mkdir()
    _main(monkeypatch, "start", "bead-1", "--project", str(root))
    assert captured["start"] == root.resolve()


def test_project_dir_alias_warns_and_still_works(monkeypatch, tmp_path, captured, caplog):
    root = tmp_path / "proj"
    with caplog.at_level("WARNING", logger="bd-track"):
        _main(monkeypatch, "queue", "list", "--project-dir", str(root))
    assert captured["queue"] == root
    assert "--project-dir is deprecated" in caplog.text


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("queue", "list", "--project", "{root}", "--project-dir", "{root}"), "not both"),
        (("--global", "status", "--project", "{root}"), "mutually exclusive"),
        (("--project", "{root}", "servers"), "does not apply to `servers`"),
        (("--project", "{root}/missing", "status"), "nor an existing directory"),
    ],
)
def test_project_conflicts_exit(monkeypatch, tmp_path, captured, argv, message):
    root = tmp_path / "proj"
    root.mkdir()
    with pytest.raises(SystemExit, match=message):
        _main(monkeypatch, *(a.format(root=root) for a in argv))
    assert captured == {}
