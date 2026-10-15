"""Tests for `bd-track migrate` — rename (bd-timew-jy9) + import (bd-timew-73v).

rename: one-way bd-timew → bd-track on-disk migration (global dirs, the
per-project .beads sidecar + session logs, BD_TIMEW_* env-var rewrites).
import: replay a Timewarrior export into the JSONL log, preserving historical
timestamps, skipping open + bead-less intervals, idempotent by content hash.
Both are dry-run by default.
"""

from __future__ import annotations

import json
from pathlib import Path

from bd_track import migrate
from bd_track.migrate import (
    _import_key,
    _is_managed,
    _plan_env,
    _plan_move,
    _split_bead_tags,
    _timew_ts_to_iso,
    cmd_migrate_import,
    cmd_migrate_rename,
)

# ---------------------------------------------------------------------------
# chezmoi guard
# ---------------------------------------------------------------------------

def test_is_managed_none_or_empty():
    assert _is_managed(Path("/x"), None) is False
    assert _is_managed(Path("/x"), set()) is False


def test_is_managed_exact_file(tmp_path):
    f = tmp_path / ".envrc"
    f.touch()
    assert _is_managed(f, {f.resolve()}) is True


def test_is_managed_dir_with_managed_descendant(tmp_path):
    d = tmp_path / "cfg"
    (d / "sub").mkdir(parents=True)
    inner = d / "sub" / "file"
    inner.touch()
    assert _is_managed(d, {inner.resolve()}) is True
    assert _is_managed(tmp_path / "other", {inner.resolve()}) is False


# ---------------------------------------------------------------------------
# _plan_move
# ---------------------------------------------------------------------------

def test_plan_move_source_absent(tmp_path):
    a = _plan_move(tmp_path / "old", tmp_path / "new", "dir",
                   managed=None, check_chezmoi=True)
    assert a.do is False and "absent" in a.detail


def test_plan_move_target_exists(tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    a = _plan_move(tmp_path / "old", tmp_path / "new", "dir",
                   managed=None, check_chezmoi=True)
    assert a.do is False and "already exists" in a.detail


def test_plan_move_chezmoi_managed_skipped(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    a = _plan_move(old, tmp_path / "new", "dir",
                   managed={old.resolve()}, check_chezmoi=True)
    assert a.do is False and "chezmoi-managed" in a.detail


def test_plan_move_chezmoi_check_disabled(tmp_path):
    """Project-internal .beads artifacts skip the chezmoi check entirely."""
    old = tmp_path / "old"
    old.mkdir()
    a = _plan_move(old, tmp_path / "new", "dir",
                   managed={old.resolve()}, check_chezmoi=False)
    assert a.do is True


def test_plan_move_normal(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    a = _plan_move(old, tmp_path / "new", "dir",
                   managed=set(), check_chezmoi=True)
    assert a.do is True and a.dst == tmp_path / "new"


# ---------------------------------------------------------------------------
# _plan_env
# ---------------------------------------------------------------------------

def test_plan_env_no_file(tmp_path):
    assert _plan_env(tmp_path / ".envrc", managed=None) is None


def test_plan_env_no_tokens(tmp_path):
    f = tmp_path / ".envrc"
    f.write_text("export FOO=bar\n")
    assert _plan_env(f, managed=None) is None


def test_plan_env_counts_tokens(tmp_path):
    f = tmp_path / ".envrc"
    f.write_text("export BD_TIMEW_SCOPE=x\nexport BD_TIMEW_ACTOR=y\n")
    a = _plan_env(f, managed=set())
    assert a.do is True and a.count == 2


def test_plan_env_managed_skipped(tmp_path):
    f = tmp_path / ".envrc"
    f.write_text("export BD_TIMEW_SCOPE=x\n")
    a = _plan_env(f, managed={f.resolve()})
    assert a.do is False and "chezmoi-managed" in a.detail


# ---------------------------------------------------------------------------
# cmd_migrate_rename — end to end
# ---------------------------------------------------------------------------

def _make_project(root: Path, *, sidecar=True, sessions=True, envrc=True) -> None:
    beads = root / ".beads"
    beads.mkdir(parents=True)
    if sidecar:
        (beads / "bd-timew.yaml").write_text("billing: {}\n")
    if sessions:
        (beads / "bd-timew" / "sessions").mkdir(parents=True)
        (beads / "bd-timew" / "sessions" / "s.jsonl").write_text("{}\n")
    if envrc:
        (root / ".envrc").write_text("export BD_TIMEW_SCOPE=demo\n")


def _isolate(monkeypatch, tmp_path) -> Path:
    """Point HOME at a tmp dir and stub chezmoi to 'manages nothing'."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(migrate, "_chezmoi_managed_set", lambda: set())
    return home


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    _make_project(proj)

    cmd_migrate_rename(project_dir=proj, apply=False)

    # Nothing renamed.
    assert (proj / ".beads" / "bd-timew.yaml").exists()
    assert (proj / ".beads" / "bd-track.yaml").exists() is False
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_migrates_project(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    _make_project(proj)

    cmd_migrate_rename(project_dir=proj, apply=True)

    beads = proj / ".beads"
    assert (beads / "bd-track.yaml").exists()
    assert (beads / "bd-timew.yaml").exists() is False
    assert (beads / "bd-track" / "sessions" / "s.jsonl").exists()
    assert (beads / "bd-timew").exists() is False
    assert (proj / ".envrc").read_text() == "export BD_TRACK_SCOPE=demo\n"
    assert (proj / ".envrc.bak").read_text() == "export BD_TIMEW_SCOPE=demo\n"


def test_apply_migrates_global_dirs(tmp_path, monkeypatch):
    home = _isolate(monkeypatch, tmp_path)
    (home / ".config" / "bd-timew").mkdir(parents=True)
    (home / ".config" / "bd-timew" / "repos.yaml").write_text("repos: []\n")
    proj = tmp_path / "proj"
    _make_project(proj, sidecar=False, sessions=False, envrc=False)

    cmd_migrate_rename(project_dir=proj, apply=True)

    assert (home / ".config" / "bd-track" / "repos.yaml").exists()
    assert (home / ".config" / "bd-timew").exists() is False


def test_apply_idempotent(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    _make_project(proj)

    cmd_migrate_rename(project_dir=proj, apply=True)
    # Second run: everything already renamed → no-op, no exception.
    cmd_migrate_rename(project_dir=proj, apply=True)

    assert (proj / ".beads" / "bd-track.yaml").exists()
    assert (proj / ".envrc").read_text() == "export BD_TRACK_SCOPE=demo\n"


def test_no_backup_flag(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    _make_project(proj, sidecar=False, sessions=False)

    cmd_migrate_rename(project_dir=proj, apply=True, backup=False)

    assert (proj / ".envrc").read_text() == "export BD_TRACK_SCOPE=demo\n"
    assert (proj / ".envrc.bak").exists() is False


def test_chezmoi_managed_env_skipped(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    _make_project(proj, sidecar=False, sessions=False)
    # Mark the .envrc as chezmoi-managed.
    monkeypatch.setattr(migrate, "_chezmoi_managed_set",
                        lambda: {(proj / ".envrc").resolve()})

    cmd_migrate_rename(project_dir=proj, apply=True)

    # Untouched — still the old token.
    assert (proj / ".envrc").read_text() == "export BD_TIMEW_SCOPE=demo\n"
    assert (proj / ".envrc.bak").exists() is False


def test_all_repos_sweeps_registry(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _make_project(r1, sessions=False, envrc=False)
    _make_project(r2, sessions=False, envrc=False)
    monkeypatch.setattr(migrate, "_registered_repo_roots", lambda: [r1, r2])

    cmd_migrate_rename(all_repos=True, apply=True)

    assert (r1 / ".beads" / "bd-track.yaml").exists()
    assert (r2 / ".beads" / "bd-track.yaml").exists()


# ===========================================================================
# migrate import (bd-timew-73v)
# ===========================================================================

# Helpers ------------------------------------------------------------------

def test_timew_ts_to_iso():
    assert _timew_ts_to_iso("20260419T021551Z") == "2026-04-19T02:15:51+00:00"


def test_split_bead_tags_normal():
    bead, tags = _split_bead_tags(
        ["PROJ-dbo", "client:personal", "case:area:claude", "svc:none"])
    assert bead == "PROJ-dbo"
    assert tags == ["client:personal", "case:area:claude", "svc:none"]


def test_split_bead_tags_no_bead():
    bead, tags = _split_bead_tags(["billable:false", "client:personal"])
    assert bead is None
    assert tags == ["billable:false", "client:personal"]


def test_import_key_stable_and_order_independent():
    k1 = _import_key("S", "E", "bead-1", ["b:2", "a:1"])
    k2 = _import_key("S", "E", "bead-1", ["a:1", "b:2"])  # tag order swapped
    k3 = _import_key("S", "E", "bead-2", ["a:1", "b:2"])  # different bead
    assert k1 == k2
    assert k1 != k3


# Fixture export -----------------------------------------------------------

_EXPORT = [
    # closed, bead-tagged → imported
    {"id": 3, "start": "20260419T010000Z", "end": "20260419T020000Z",
     "tags": ["bead-aaa", "client:acme", "case:x"]},
    # closed, bead-tagged → imported (2h)
    {"id": 2, "start": "20260420T000000Z", "end": "20260420T020000Z",
     "tags": ["bead-bbb", "billable:false"]},
    # bead-less → skipped
    {"id": 1, "start": "20260421T000000Z", "end": "20260421T003000Z",
     "tags": ["billable:false"]},
    # open (no end) → skipped
    {"id": 0, "start": "20260422T000000Z", "tags": ["bead-ccc"]},
]


def _write_export(tmp_path) -> Path:
    f = tmp_path / "export.json"
    f.write_text(json.dumps(_EXPORT))
    return f


def _make_beads(tmp_path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".beads").mkdir(parents=True)
    return proj


def _read_import_log(proj) -> list[dict]:
    log = proj / ".beads" / "bd-track" / "sessions" / "imported-timew.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# Behaviour ----------------------------------------------------------------

def test_import_dry_run_writes_nothing(tmp_path, capsys):
    proj = _make_beads(tmp_path)
    cmd_migrate_import(project_dir=proj, from_file=_write_export(tmp_path), apply=False)

    assert _read_import_log(proj) == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "import: 2" in out and "skip(open): 1" in out and "skip(no-bead): 1" in out


def test_import_apply_writes_paired_events(tmp_path):
    proj = _make_beads(tmp_path)
    cmd_migrate_import(project_dir=proj, from_file=_write_export(tmp_path), apply=True)

    events = _read_import_log(proj)
    starts = [e for e in events if e["event"] == "start"]
    stops = [e for e in events if e["event"] == "stop"]
    assert len(starts) == 2 and len(stops) == 2
    # Historical ts preserved; bead/tags split correctly; provenance marked.
    s0 = next(e for e in starts if e["bead"] == "bead-aaa")
    assert s0["ts"] == "2026-04-19T01:00:00+00:00"
    assert s0["tags"] == ["client:acme", "case:x"]
    assert s0["source"] == "timew" and s0["import_key"]


def test_import_round_trips_through_aggregator(tmp_path):
    """Imported events fold to the correct historical durations."""
    from bd_track.aggregate import load_intervals
    from bd_track.events import log_dir

    proj = _make_beads(tmp_path)
    cmd_migrate_import(project_dir=proj, from_file=_write_export(tmp_path), apply=True)

    intervals = {iv.bead: iv for iv in load_intervals(log_dir(proj))}
    assert set(intervals) == {"bead-aaa", "bead-bbb"}
    assert intervals["bead-aaa"].duration.total_seconds() == 3600  # 1h
    assert intervals["bead-bbb"].duration.total_seconds() == 7200  # 2h
    assert all(iv.status == "closed" for iv in intervals.values())


def test_import_idempotent_rerun(tmp_path):
    proj = _make_beads(tmp_path)
    export = _write_export(tmp_path)

    cmd_migrate_import(project_dir=proj, from_file=export, apply=True)
    first = len(_read_import_log(proj))
    cmd_migrate_import(project_dir=proj, from_file=export, apply=True)
    second = len(_read_import_log(proj))

    assert first == second == 4  # 2 intervals x (start+stop); re-run added nothing

