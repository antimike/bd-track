"""Tests for queue.py — scoped queue operations."""

from __future__ import annotations

import json
import os
import subprocess
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
    data = {"default": ["A", "B"], "tooling": ["C"]}
    save_all_queues(beads_dir, data)
    assert load_all_queues(beads_dir) == data


def test_save_empty_deletes_file(beads_dir):
    save_all_queues(beads_dir, {"default": ["A"]})
    save_all_queues(beads_dir, {"default": []})
    assert not (beads_dir / "queue.yaml").exists()


def test_save_drops_empty_scopes(beads_dir):
    save_all_queues(beads_dir, {"default": ["A"], "empty": []})
    loaded = load_all_queues(beads_dir)
    assert loaded == {"default": ["A"]}


# ---------------------------------------------------------------------------
# cmd_queue actions
# ---------------------------------------------------------------------------

def test_push_appends_and_dedups(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B"], project_dir=beads_dir.parent)
    cmd_queue("push", ids=["B", "C"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["A", "B", "C"]}


def test_unshift_prepends_and_moves_existing(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B", "C"], project_dir=beads_dir.parent)
    cmd_queue("unshift", ids=["B"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["B", "A", "C"]}


def test_pop_removes_and_prints_head(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B"], project_dir=beads_dir.parent)
    capsys.readouterr()  # discard push log
    cmd_queue("pop", project_dir=beads_dir.parent)
    captured = capsys.readouterr()
    assert captured.out.strip() == "A"
    assert load_all_queues(beads_dir) == {"default": ["B"]}


def test_peek_does_not_remove(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B"], project_dir=beads_dir.parent)
    capsys.readouterr()
    cmd_queue("peek", project_dir=beads_dir.parent)
    captured = capsys.readouterr()
    assert captured.out.strip() == "A"
    assert load_all_queues(beads_dir) == {"default": ["A", "B"]}


def test_remove_deletes_specific_ids(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B", "C"], project_dir=beads_dir.parent)
    cmd_queue("remove", ids=["B"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["A", "C"]}


def test_clear_scope_removes_only_named(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A"], scope_arg="default", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["X"], scope_arg="tooling", project_dir=beads_dir.parent)
    cmd_queue("clear", scope_arg="tooling", project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["A"]}


def test_clear_scope_all_empties_everything(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A"], scope_arg="default", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["X"], scope_arg="tooling", project_dir=beads_dir.parent)
    cmd_queue("clear", scope_arg="all", project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {}


def test_pop_empty_queue_is_noop(beads_dir, capsys, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("pop", project_dir=beads_dir.parent)
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout


def test_push_separates_scopes(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A"], scope_arg="default", project_dir=beads_dir.parent)
    cmd_queue("push", ids=["B"], scope_arg="tooling", project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"default": ["A"], "tooling": ["B"]}


def test_env_var_sets_scope(beads_dir, monkeypatch):
    monkeypatch.setenv("BD_TRACK_SCOPE", "tooling")
    cmd_queue("push", ids=["A"], project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"tooling": ["A"]}


# ---------------------------------------------------------------------------
# clean — mechanical sweep of stale entries
# ---------------------------------------------------------------------------

def _stub_get_issue(statuses: dict[str, str], dependencies: dict[str, list] | None = None,
                    labels: dict[str, list[str]] | None = None):
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
    cmd_queue("push", ids=["A", "B", "C"], scope_arg="pipeline",
              project_dir=beads_dir.parent)

    statuses = {"A": "open", "B": "closed", "C": "deferred"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    removed = cmd_clean(project_dir=beads_dir.parent)
    assert removed == 2
    assert load_all_queues(beads_dir) == {"pipeline": ["A"]}


def test_clean_sweeps_all_scopes_by_default(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B"], scope_arg="pipeline",
              project_dir=beads_dir.parent)
    cmd_queue("push", ids=["X", "Y"], scope_arg="tooling",
              project_dir=beads_dir.parent)

    statuses = {"A": "open", "B": "closed", "X": "deferred", "Y": "in_progress"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    cmd_clean(project_dir=beads_dir.parent)
    assert load_all_queues(beads_dir) == {"pipeline": ["A"], "tooling": ["Y"]}


def test_clean_keeps_unknown_beads_for_prune(beads_dir, monkeypatch):
    """Beads that fail bd lookup are kept by `clean`; `prune` surfaces them."""
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "GHOST"], scope_arg="pipeline",
              project_dir=beads_dir.parent)

    statuses = {"A": "open"}  # GHOST not in dict — get_issue raises SystemExit
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    removed = cmd_clean(project_dir=beads_dir.parent)
    assert removed == 0
    assert load_all_queues(beads_dir) == {"pipeline": ["A", "GHOST"]}


def test_clean_scope_filter_limits_sweep(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B"], scope_arg="pipeline",
              project_dir=beads_dir.parent)
    cmd_queue("push", ids=["X", "Y"], scope_arg="tooling",
              project_dir=beads_dir.parent)

    statuses = {"A": "open", "B": "closed", "X": "closed", "Y": "open"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    cmd_clean(scope_arg="pipeline", project_dir=beads_dir.parent)
    # Only pipeline scope was swept; tooling untouched.
    assert load_all_queues(beads_dir) == {"pipeline": ["A"], "tooling": ["X", "Y"]}


# ---------------------------------------------------------------------------
# generate — populate from `bd list` filters
# ---------------------------------------------------------------------------

def _stub_run_bd_list(issues: list[dict]):
    """Return a fake `bd_track.queue.run` that pretends `bd list --json` returned `issues`."""
    def _impl(cmd, *, check=True, capture=False, cwd=None):
        # Only intercept `bd list --json`; other run() calls would surprise us.
        assert cmd[:3] == ["bd", "list", "--json"], f"unexpected run() call: {cmd}"
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout=json.dumps(issues), stderr="",
        )
    return _impl


def test_generate_populates_empty_queue(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    issues = [{"id": "PROJ-a"}, {"id": "PROJ-b"}]
    monkeypatch.setattr("bd_track.queue.run", _stub_run_bd_list(issues))

    cmd_generate(scope_arg="pipeline", project_dir=beads_dir.parent,
                 labels_all=["area:pipeline"], yes=True)
    assert load_all_queues(beads_dir) == {"pipeline": ["PROJ-a", "PROJ-b"]}


def test_generate_append_dedupes(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["PROJ-a"], scope_arg="pipeline",
              project_dir=beads_dir.parent)
    issues = [{"id": "PROJ-a"}, {"id": "PROJ-c"}]
    monkeypatch.setattr("bd_track.queue.run", _stub_run_bd_list(issues))

    cmd_generate(scope_arg="pipeline", project_dir=beads_dir.parent, append=True)
    assert load_all_queues(beads_dir) == {"pipeline": ["PROJ-a", "PROJ-c"]}


def test_generate_replace_requires_yes_when_non_interactive(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["PROJ-old"], scope_arg="pipeline",
              project_dir=beads_dir.parent)
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
    cmd_queue("push", ids=["PROJ-a"], scope_arg="pipeline",
              project_dir=beads_dir.parent)
    monkeypatch.setattr("bd_track.queue.run", _stub_run_bd_list([]))

    cmd_generate(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    assert load_all_queues(beads_dir) == {"pipeline": ["PROJ-a"]}


# ---------------------------------------------------------------------------
# prune — analytical: identify stale, mismatched, out-of-order entries
# ---------------------------------------------------------------------------

def test_prune_yes_removes_stale(beads_dir, monkeypatch):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B", "C"], scope_arg="pipeline",
              project_dir=beads_dir.parent)

    statuses = {"A": "open", "B": "closed", "C": "deferred"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    assert load_all_queues(beads_dir) == {"pipeline": ["A"]}


def test_prune_non_interactive_without_yes_is_report_only(beads_dir, monkeypatch, capsys):
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B"], scope_arg="pipeline",
              project_dir=beads_dir.parent)

    statuses = {"A": "open", "B": "closed"}
    monkeypatch.setattr("bd_track.queue.get_issue", _stub_get_issue(statuses))
    monkeypatch.setattr("bd_track.queue.is_interactive", lambda: False)

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=False)
    # Queue unchanged; the proposal was logged but not applied.
    assert load_all_queues(beads_dir) == {"pipeline": ["A", "B"]}


def test_prune_detects_scope_mismatch(beads_dir, monkeypatch, capsys):
    """A scope:local bead in 'pipeline' should be flagged for the tooling queue."""
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A"], scope_arg="pipeline",
              project_dir=beads_dir.parent)

    statuses = {"A": "open"}
    labels = {"A": ["scope:local", "area:tooling"]}
    monkeypatch.setattr(
        "bd_track.queue.get_issue",
        _stub_get_issue(statuses, labels=labels),
    )

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    out = capsys.readouterr().out
    assert "[move]" in out and "A" in out and "scope:local" in out
    # Move recommendations are surfaced but not auto-applied.
    assert load_all_queues(beads_dir) == {"pipeline": ["A"]}


def test_prune_detects_dependency_ordering(beads_dir, monkeypatch, capsys):
    """If bead X depends on Y and Y comes later in the queue, flag it."""
    monkeypatch.delenv("BD_TRACK_SCOPE", raising=False)
    cmd_queue("push", ids=["A", "B"], scope_arg="pipeline",
              project_dir=beads_dir.parent)

    statuses = {"A": "open", "B": "open"}
    dependencies = {
        "A": [{"id": "B", "dependency_type": "blocks", "status": "open"}],
    }
    monkeypatch.setattr(
        "bd_track.queue.get_issue",
        _stub_get_issue(statuses, dependencies=dependencies),
    )

    cmd_prune(scope_arg="pipeline", project_dir=beads_dir.parent, yes=True)
    out = capsys.readouterr().out
    assert "[reorder]" in out
    # Reorder recommendations are not auto-applied.
    assert load_all_queues(beads_dir) == {"pipeline": ["A", "B"]}
