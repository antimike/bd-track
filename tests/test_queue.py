"""Tests for queue.py — scoped queue operations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bd_track.queue import (
    cmd_clean,
    cmd_generate,
    cmd_prune,
    cmd_queue,
    load_all_queues,
    resolve_scope,
    save_all_queues,
)


@pytest.fixture
def beads_dir(tmp_path: Path) -> Path:
    """A temporary .beads/-like directory for queue file ops."""
    d = tmp_path / ".beads"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# resolve_scope
# ---------------------------------------------------------------------------


def test_resolve_scope_explicit_wins():
    with patch.dict(os.environ, {"BD_TRACK_SCOPE": "from_env"}, clear=False):
        assert resolve_scope("explicit") == "explicit"


def test_resolve_scope_env_var_used_when_no_flag():
    with patch.dict(os.environ, {"BD_TRACK_SCOPE": "from_env"}, clear=False):
        assert resolve_scope(None) == "from_env"


def test_resolve_scope_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    assert resolve_scope(None) == "default"


def test_resolve_scope_empty_env_var_treated_as_unset(monkeypatch):
    monkeypatch.setenv("BD_TRACK_SCOPE", "   ")
    assert resolve_scope(None) == "default"


def test_resolve_scope_passes_all_through():
    """clear --scope all uses 'all' as a special value; resolve_scope must not normalize it."""
    assert resolve_scope("all") == "all"


# ---------------------------------------------------------------------------
# load_all_queues / save_all_queues round-trip
# ---------------------------------------------------------------------------


def test_load_all_queues_missing_file_returns_empty(beads_dir):
    assert load_all_queues(beads_dir) == {}


def test_save_then_load_round_trip(beads_dir):
    data = {"default": ["t-a", "t-b"], "tooling": ["t-c"]}
    save_all_queues(beads_dir, data)
    assert load_all_queues(beads_dir) == data


def test_save_empty_deletes_file(beads_dir):
    save_all_queues(beads_dir, {"default": ["t-a"]})
    save_all_queues(beads_dir, {"default": []})
    assert not (beads_dir / "queue.yaml").exists()


def test_save_drops_empty_scopes(beads_dir):
    save_all_queues(beads_dir, {"default": ["t-a"], "empty": []})
    loaded = load_all_queues(beads_dir)
    assert loaded == {"default": ["t-a"]}


# ---------------------------------------------------------------------------
# cmd_queue actions
# ---------------------------------------------------------------------------


def test_push_appends_and_dedups(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b"], project_dir=beads_dir.parent)
    cmd_queue("push", ids=["t-b", "t-c"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["t-a", "t-b", "t-c"]}


def test_unshift_prepends_and_moves_existing(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b", "t-c"], project_dir=beads_dir.parent)
    cmd_queue("unshift", ids=["t-b"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["t-b", "t-a", "t-c"]}


@pytest.mark.parametrize("bead_id", ["bd-timew-as5", "amo-aqp.23", "PROJ-18077", "chz-a1.2.3"])
def test_push_accepts_bead_shaped_ids(beads_dir, bead_id):
    cmd_queue("push", ids=[bead_id], scope_arg="s", project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"s": [bead_id]}


@pytest.mark.parametrize("bad", ["a b", "amo-hq0 amo-jak", "A", "-x", "amo-aqp.", "amo-", " amo-x"])
def test_push_rejects_non_id_and_leaves_queue_unchanged(beads_dir, bad):
    """A quote-joined id list (zsh, no word splitting) must not become one entry."""
    save_all_queues(beads_dir, {"s": ["t-a"]})
    with pytest.raises(SystemExit) as exc:
        cmd_queue("push", ids=["t-b", bad], scope_arg="s", project_dir=beads_dir.parent)
    assert exc.value.code != 0
    assert repr(bad) in str(exc.value.code)
    assert load_all_queues(beads_dir) == {"s": ["t-a"]}


def test_unshift_rejects_non_id_and_leaves_queue_unchanged(beads_dir):
    save_all_queues(beads_dir, {"s": ["t-a"]})
    with pytest.raises(SystemExit) as exc:
        cmd_queue("unshift", ids=["a b"], scope_arg="s", project_dir=beads_dir.parent)
    assert exc.value.code != 0
    assert load_all_queues(beads_dir) == {"s": ["t-a"]}


def test_cli_push_with_space_exits_nonzero(beads_dir):
    """The bead's done-when: `bd-track queue push --scope s "a b"` fails, queue unchanged."""
    argv = ["queue", "push", "--scope", "s", "a b", "--project-dir", str(beads_dir.parent)]
    proc = subprocess.run(
        [sys.executable, "-m", "bd_track", *argv],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "'a b'" in proc.stderr
    assert load_all_queues(beads_dir) == {}


def test_pop_removes_and_prints_head(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b"], project_dir=beads_dir.parent)
    capsys.readouterr()  # discard push log
    cmd_queue("pop", project_dir=beads_dir.parent)
    captured = capsys.readouterr()
    assert captured.out.strip() == "t-a"
    assert load_all_queues(beads_dir) == {"default": ["t-b"]}


def test_peek_does_not_remove(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b"], project_dir=beads_dir.parent)
    capsys.readouterr()
    cmd_queue("peek", project_dir=beads_dir.parent)
    captured = capsys.readouterr()
    assert captured.out.strip() == "t-a"
    assert load_all_queues(beads_dir) == {"default": ["t-a", "t-b"]}


def test_remove_deletes_specific_ids(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b", "t-c"], project_dir=beads_dir.parent)
    cmd_queue("remove", ids=["t-b"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["t-a", "t-c"]}


def test_clear_scope_removes_only_named(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a"], scope_arg="default", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["t-x"], scope_arg="tooling", project_dir=beads_dir.parent)
    cmd_queue("clear", scope_arg="tooling", project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["t-a"]}


def test_clear_scope_all_empties_everything(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a"], scope_arg="default", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["t-x"], scope_arg="tooling", project_dir=beads_dir.parent)
    cmd_queue("clear", scope_arg="all", project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {}


def test_pop_empty_queue_is_noop(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("pop", project_dir=beads_dir.parent)
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout


def test_push_separates_scopes(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a"], scope_arg="default", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["t-b"], scope_arg="tooling", project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["t-a"], "tooling": ["t-b"]}


def test_env_var_sets_scope(beads_dir, monkeypatch):
    monkeypatch.setenv("BD_TRACK_SCOPE", "tooling")
    cmd_queue("push", ids=["t-a"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"tooling": ["t-a"]}


# ---------------------------------------------------------------------------
# clean — mechanical sweep of stale entries
# ---------------------------------------------------------------------------


def _stub_get_issue(
    statuses: dict[str, str],
    dependencies: dict[str, list] | None = None,
    labels: dict[str, list[str]] | None = None,
):
    """Build a fake bd_track.queue.get_issue impl backed by the given dicts."""
    deps = dependencies or {}
    lbls = labels or {}

    def _impl(bead_id: str) -> dict:
        if bead_id not in statuses:
            # Simulate the SystemExit that get_issue raises on bd failure.
            raise SystemExit(f"missing bead: {bead_id}")
        return {
            "id": bead_id,
            "title": f"Title for {bead_id}",
            "status": statuses[bead_id],
            "labels": lbls.get(bead_id, []),
            "dependencies": deps.get(bead_id, []),
        }

    return _impl


def test_clean_removes_closed_and_deferred(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b", "t-c"], scope_arg="pipeline", project_dir=beads_dir.parent)

    statuses = {"t-a": "open", "t-b": "closed", "t-c": "deferred"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    removed = cmd_clean(project_dir=beads_dir.parent)
    assert removed == 2
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a"]}


def test_clean_sweeps_all_scopes_by_default(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b"], scope_arg="pipeline", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["t-x", "t-y"], scope_arg="tooling", project_dir=beads_dir.parent)

    statuses = {"t-a": "open", "t-b": "closed", "t-x": "deferred", "t-y": "in_progress"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    cmd_clean(project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a"], "tooling": ["t-y"]}


def test_clean_keeps_unknown_beads_for_prune(beads_dir, monkeypatch):
    """Beads that fail bd lookup are kept by `clean`; `prune` surfaces them."""
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-ghost"], scope_arg="pipeline", project_dir=beads_dir.parent)

    statuses = {"t-a": "open"}  # GHOST not in dict — get_issue raises SystemExit
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    removed = cmd_clean(project_dir=beads_dir.parent)
    assert removed == 0
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a", "t-ghost"]}


def test_clean_scope_filter_limits_sweep(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b"], scope_arg="pipeline", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["t-x", "t-y"], scope_arg="tooling", project_dir=beads_dir.parent)

    statuses = {"t-a": "open", "t-b": "closed", "t-x": "closed", "t-y": "open"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    cmd_clean(scope_arg="pipeline", project_dir=beads_dir.parent)
    # Only pipeline scope was swept; tooling untouched.
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a"], "tooling": ["t-x", "t-y"]}


# ---------------------------------------------------------------------------
# generate — populate from `bd list` filters
# ---------------------------------------------------------------------------


def _stub_run_bd_list(issues: list[dict]):
    """Return a fake `bd_track.queue.run` that pretends `bd list --json` returned `issues`."""

    def _impl(cmd, *, check=True, capture=False, cwd=None):
        # Only intercept `bd list --json`; other run() calls would surprise us.
        assert cmd[:3] == ["bd", "list", "--json"], f"unexpected run() call: {cmd}"
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(issues),
            stderr="",
        )

    return _impl


def test_generate_populates_empty_queue(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    issues = [{"id": "PROJ-a"}, {"id": "PROJ-b"}]
    monkeypatch.setattr("bd_track.queue.run", _stub_run_bd_list(issues))

    cmd_generate(
        scope_arg="pipeline", project_dir=beads_dir.parent, labels_all=["area:pipeline"], yes=True
    )
    assert load_all_queues(beads_dir) == {"pipeline": ["PROJ-a", "PROJ-b"]}


def test_generate_append_dedupes(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["PROJ-a"], scope_arg="pipeline", project_dir=beads_dir.parent)
    issues = [{"id": "PROJ-a"}, {"id": "PROJ-c"}]
    monkeypatch.setattr("bd_track.queue.run", _stub_run_bd_list(issues))

    cmd_generate(scope_arg="pipeline", project_dir=beads_dir.parent, append=True)
    assert load_all_queues(beads_dir) == {"pipeline": ["PROJ-a", "PROJ-c"]}


def test_generate_replace_requires_yes_when_non_interactive(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["PROJ-old"], scope_arg="pipeline", project_dir=beads_dir.parent)
    issues = [{"id": "PROJ-new"}]
    monkeypatch.setattr("bd_track.queue.run", _stub_run_bd_list(issues))
    # Force non-interactive
    monkeypatch.setattr("bd_track.queue.is_interactive", lambda: False)

    with pytest.raises(SystemExit):
        cmd_generate(scope_arg="pipeline", project_dir=beads_dir.parent)
    # Queue unchanged on abort.
    assert load_all_queues(beads_dir) == {"pipeline": ["PROJ-old"]}


def test_generate_no_matches_leaves_queue_unchanged(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["PROJ-a"], scope_arg="pipeline", project_dir=beads_dir.parent)
    monkeypatch.setattr("bd_track.queue.run", _stub_run_bd_list([]))

    cmd_generate(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    assert load_all_queues(beads_dir) == {"pipeline": ["PROJ-a"]}


# ---------------------------------------------------------------------------
# prune — analytical: identify stale, mismatched, out-of-order entries
# ---------------------------------------------------------------------------


def test_prune_yes_removes_stale(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b", "t-c"], scope_arg="pipeline", project_dir=beads_dir.parent)

    statuses = {"t-a": "open", "t-b": "closed", "t-c": "deferred"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a"]}


def test_prune_non_interactive_without_yes_is_report_only(beads_dir, monkeypatch, capsys):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b"], scope_arg="pipeline", project_dir=beads_dir.parent)

    statuses = {"t-a": "open", "t-b": "closed"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))
    monkeypatch.setattr("bd_track.queue.is_interactive", lambda: False)

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=False)
    # Queue unchanged; the proposal was logged but not applied.
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a", "t-b"]}


def test_prune_detects_scope_mismatch(beads_dir, monkeypatch, capsys):
    """A scope:local bead in 'pipeline' should be flagged for the tooling queue."""
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a"], scope_arg="pipeline", project_dir=beads_dir.parent)

    statuses = {"t-a": "open"}
    labels = {"t-a": ["scope:local", "area:tooling"]}
    monkeypatch.setattr(
        "bd_track.queue.get_issue",
        _stub_get_issue(statuses, labels=labels),
    )

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    out = capsys.readouterr().out
    assert "[move]" in out and "t-a" in out and "scope:local" in out
    # Move recommendations are surfaced but not auto-applied.
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a"]}


def test_prune_detects_dependency_ordering(beads_dir, monkeypatch, capsys):
    """If bead X depends on Y and Y comes later in the queue, flag it."""
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-a", "t-b"], scope_arg="pipeline", project_dir=beads_dir.parent)

    statuses = {"t-a": "open", "t-b": "open"}
    dependencies = {
        "t-a": [{"id": "t-b", "dependency_type": "blocks", "status": "open"}],
    }
    monkeypatch.setattr(
        "bd_track.queue.get_issue",
        _stub_get_issue(statuses, dependencies=dependencies),
    )

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    out = capsys.readouterr().out
    assert "[reorder]" in out
    # Reorder recommendations are not auto-applied.
    assert load_all_queues(beads_dir) == {"pipeline": ["t-a", "t-b"]}


def test_prune_ignores_non_blocking_edges(beads_dir, monkeypatch, capsys):
    """A queued child's parent epic is not a prerequisite (bd-timew-ctp).

    An open epic stays open until its children close, so proposing to queue
    it first is unworkable. Only blocking edges imply ordering.
    """
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["t-e.1", "t-e.2"], scope_arg="pipeline", project_dir=beads_dir.parent)

    statuses = {"t-e": "open", "t-e.1": "open", "t-e.2": "open", "t-r": "open"}
    dependencies = {
        "t-e.1": [{"id": "t-e", "dependency_type": "parent-child", "status": "open"}],
        "t-e.2": [
            {"id": "t-e", "dependency_type": "parent-child", "status": "open"},
            {"id": "t-e.1", "dependency_type": "blocks", "status": "open"},
            {"id": "t-r", "dependency_type": "related", "status": "open"},
        ],
    }
    monkeypatch.setattr(
        "bd_track.queue.get_issue",
        _stub_get_issue(statuses, dependencies=dependencies),
    )

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    out = capsys.readouterr().out
    assert "[add-before]" not in out
    assert "[reorder]" not in out
