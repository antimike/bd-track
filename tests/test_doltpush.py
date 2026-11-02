"""Tests for cleanup's Dolt push step (doltpush.py).

Network and bd are stubbed except in the end-to-end test, which pushes a scratch embedded bd
database to a local bare git repo (skipped without bd and dolt).
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import time
import urllib.error
from pathlib import Path

import pytest

from bd_track import doltpush
from bd_track.doltpush import PushResult, PushSkipped, check_remote, same_repo
from bd_track.registry import Project

log = logging.getLogger("test-doltpush")
HOST = {"dolt_push": True, "dolt_remote_url": None, "dolt_push_timeout": 120}
needs_dolt = pytest.mark.skipif(
    not (shutil.which("dolt") and shutil.which("bd")), reason="needs bd and dolt"
)


@pytest.fixture(autouse=True)
def push_log(tmp_path: Path, monkeypatch) -> Path:
    """Keep the timing log out of the real state directory."""
    path = tmp_path / "state" / "dolt-push.tsv"
    monkeypatch.setattr(doltpush, "PUSH_LOG", path)
    return path


def _git(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


# ---------------------------------------------------------------------------
# URLs and the guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("git@github.com:o/r.git", "git+ssh://git@github.com/./o/r.git", True),
        ("https://github.com/o/r.git", "git+https://github.com/o/r.git", True),
        ("git@github.com:o/r.git", "git@github.com:o/other.git", False),
        ("/srv/backup.git", "file:///srv/backup.git", True),
        ("/srv/backup.git", "git@github.com:o/r.git", False),
    ],
)
def test_same_repo(a: str, b: str, same: bool):
    assert same_repo(a, b) is same


@pytest.fixture
def ssh_aliases(monkeypatch):
    """``ssh -G`` stand-in: me.github.com and gh are aliases of github.com."""
    aliases = {"me.github.com": "github.com", "gh": "github.com"}
    monkeypatch.setattr(doltpush, "_ssh_hostname", lambda host: aliases.get(host, host))


@pytest.mark.parametrize(
    "url, host",
    [
        ("git@github.com:o/r.git", "github.com"),
        ("git+ssh://git@GitHub.com/./o/r.git", "github.com"),
        ("git@me.github.com:o/r.git", "github.com"),
        ("gh:o/r.git", "github.com"),
        ("https://git.example.org:8443/o/r.git", "git.example.org"),
        ("git@evil-github.com:o/r.git", "evil-github.com"),
        ("/srv/x.git", None),
        ("file:///srv/x.git", None),
    ],
)
def test_resolve_host(url: str, host: str | None, ssh_aliases):
    assert doltpush.resolve_host(url) == host


def test_resolve_host_applies_git_insteadof(tmp_path: Path, monkeypatch, ssh_aliases):
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text('[url "git@me.github.com:"]\n\tinsteadOf = mirror:\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    assert doltpush._git_rewrite("mirror:o/r.git") == "git@me.github.com:o/r.git"
    assert doltpush.resolve_host("mirror:o/r.git") == "github.com"


def test_ssh_hostname_reads_ssh_G(monkeypatch):
    out = "user me\nhostname github.com\nport 22\n"
    monkeypatch.setattr(
        doltpush.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=out, stderr=""),
    )
    assert doltpush._ssh_hostname("me.github.com") == "github.com"


@needs_dolt
def test_ssh_hostname_real_ssh_without_alias():
    assert doltpush._ssh_hostname("example.invalid") == "example.invalid"


def test_guard_refuses_code_remote(tmp_path: Path):
    root = _git(tmp_path / "proj", "git@github.com:antimike/proj.git")
    with pytest.raises(PushSkipped, match="remote of the code repo"):
        check_remote("https://github.com/antimike/proj.git", Project("p"), root)


def test_guard_github_public_and_private(tmp_path: Path, monkeypatch, ssh_aliases):
    root = _git(tmp_path / "proj")
    monkeypatch.setattr(doltpush, "github_is_public", lambda slug: slug == "o/public")
    with pytest.raises(PushSkipped, match="public GitHub repo"):
        check_remote("git@github.com:o/public.git", Project("p"), root)
    check_remote("git@github.com:o/private.git", Project("p"), root)  # no raise
    with pytest.raises(PushSkipped, match="public GitHub repo"):  # ssh alias host
        check_remote("git@me.github.com:o/public.git", Project("p"), root)


def test_guard_local_and_unknown_hosts(tmp_path: Path, monkeypatch, ssh_aliases):
    root = _git(tmp_path / "proj")
    monkeypatch.setattr(doltpush, "github_is_public", lambda slug: pytest.fail("no API call"))
    check_remote(str(tmp_path / "backup.git"), Project("p"), root)
    with pytest.raises(PushSkipped, match="cannot verify"):
        check_remote("git@git.example.org:o/r.git", Project("p"), root)
    with pytest.raises(PushSkipped, match="cannot verify"):  # not an alias of github.com
        check_remote("git@evil-github.com:o/r.git", Project("p"), root)
    vouched = Project("p", beads={"dolt_remote_private": True})
    check_remote("git@git.example.org:o/r.git", vouched, root)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO())


@pytest.mark.parametrize(
    "outcome, expected",
    [(None, True), (_http_error(404), False), (_http_error(403), PushSkipped),
     (urllib.error.URLError("down"), PushSkipped)],
)
def test_github_is_public(monkeypatch, outcome, expected):
    def fake_urlopen(request, timeout):
        if outcome is not None:
            raise outcome
        return io.BytesIO(b"{}")

    monkeypatch.setattr(doltpush.urllib.request, "urlopen", fake_urlopen)
    if expected is PushSkipped:
        with pytest.raises(PushSkipped, match="cannot tell"):
            doltpush.github_is_public("o/r")
    else:
        assert doltpush.github_is_public("o/r") is expected


# ---------------------------------------------------------------------------
# run_push: timeout kills the whole process group
# ---------------------------------------------------------------------------


def test_run_push_timeout_kills_group(tmp_path: Path):
    marker = tmp_path / "survived"
    argv = ("sh", "-c", f"(sleep 2; touch {marker}) & sleep 30")
    start = time.monotonic()
    result = doltpush.run_push(tmp_path, 0.5, argv=argv)
    assert not result.ok and "timed out" in result.detail
    assert time.monotonic() - start < 5
    time.sleep(2.5)
    assert not marker.exists()  # the background child died with the group


def test_run_push_reports_exit_status(tmp_path: Path):
    assert doltpush.run_push(tmp_path, 5, argv=("sh", "-c", "echo pushed")).ok
    failed = doltpush.run_push(tmp_path, 5, argv=("sh", "-c", "echo nope; exit 1"))
    assert (failed.ok, failed.detail) == (False, "nope")


# ---------------------------------------------------------------------------
# push_project flow (stubbed)
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr(doltpush, "check_remote", lambda url, p, root: calls.append("guard"))
    monkeypatch.setattr(
        doltpush, "open_database",
        lambda root: doltpush.Database("amo", f"embedded:{root}", root, root),
    )
    monkeypatch.setattr(
        doltpush, "ensure_remote", lambda db, url, ref, log: calls.append(("remote", url, ref))
    )
    monkeypatch.setattr(
        doltpush, "run_push", lambda root, timeout: calls.append(("push", timeout))
        or PushResult(True, 0.1, "ok"),
    )
    return calls


def test_push_project_off_or_unconfigured(tmp_path: Path, stubbed):
    off = Project("p", beads={"prefix": "p"})
    assert doltpush.push_project(off, tmp_path, {**HOST, "dolt_push": False}, log) is None
    assert doltpush.push_project(off, tmp_path, HOST, log) is None  # on, but no URL
    assert stubbed == []


def test_push_project_rejects_string_override(tmp_path: Path, stubbed):
    project = Project("p", beads={"prefix": "p", "dolt_push": "no"})
    assert doltpush.push_project(project, tmp_path, {**HOST, "dolt_remote_url": "/b"}, log) is None
    assert stubbed == []


def test_push_project_flow(tmp_path: Path, stubbed):
    project = Project("amo", beads={"prefix": "amo", "dolt_remote_url": "/srv/b.git"})
    host = {**HOST, "dolt_remote_url": "/srv/default.git", "dolt_push_timeout": 30}
    assert doltpush.push_project(project, tmp_path, host, log).ok
    assert stubbed == ["guard", ("remote", "/srv/b.git", "refs/dolt/amo"), ("push", 30)]


def test_push_project_guard_blocks_push(tmp_path: Path, stubbed, monkeypatch):
    def refuse(url, p, root):
        raise PushSkipped("public")

    monkeypatch.setattr(doltpush, "check_remote", refuse)
    project = Project("amo", beads={"prefix": "amo"})
    assert doltpush.push_project(project, tmp_path, {**HOST, "dolt_remote_url": "x"}, log) is None
    assert stubbed == []


def test_push_project_once_per_database(tmp_path: Path, stubbed):
    host = {**HOST, "dolt_remote_url": "/srv/b.git"}
    pushed: set[str] = set()
    first = Project("a", beads={"prefix": "a"})
    second = Project("b", beads={"prefix": "b"})  # same database (stubbed key)
    assert doltpush.push_project(first, tmp_path, host, log, pushed).ok
    assert doltpush.push_project(second, tmp_path, host, log, pushed) is None
    assert [c for c in stubbed if c[0] == "push"] == [("push", 120)]


def test_push_timeout_in_server_mode_kills_server_query(tmp_path: Path, stubbed, monkeypatch):
    queries = []

    class FakeServerDB(doltpush.Database):
        def sql(self, query):
            queries.append(query)
            return [{"id": "42"}] if "processlist" in query else []

    monkeypatch.setattr(
        doltpush, "open_database", lambda root: FakeServerDB("beads_global", "server:g", root),
    )
    monkeypatch.setattr(
        doltpush, "run_push", lambda root, timeout: PushResult(False, 120, "timed out after 120s"),
    )
    project = Project("amo", beads={"prefix": "amo"})
    doltpush.push_project(project, tmp_path, {**HOST, "dolt_remote_url": "/srv/b.git"}, log)
    assert "CONNECTION_ID()" in queries[0] and doltpush.REMOTE_NAME in queries[0]
    assert queries[1:] == ["KILL QUERY 42"]


# ---------------------------------------------------------------------------
# Database: SQL through dolt (embedded) or bd (server)
# ---------------------------------------------------------------------------


def test_sql_literal_escapes():
    assert doltpush.sql_literal("a'b\\c") == "'a''b\\\\c'"


def _fake_run(outputs: dict[str, str], seen: list):
    def fake(argv, cwd=None, **kw):
        seen.append((argv, cwd))
        out = next((v for k, v in outputs.items() if k in argv[-1]), "")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    return fake


def test_open_database_server_mode_uses_bd_sql(tmp_path: Path, monkeypatch):
    seen = []
    monkeypatch.setattr(doltpush.subprocess, "run", _fake_run({
        "database()": "db\nbeads_global\n",
        "dolt_remotes": 'name,url,params\nbd-track-backup,git+file:///b.git,'
                        '"{""git_ref"": ""refs/dolt/beads_global""}"\n',
    }, seen))
    db = doltpush.open_database(tmp_path)  # no embeddeddolt directory
    assert (db.name, db.key, db.server) == ("beads_global", "server:beads_global", True)
    assert doltpush.remotes(db) == {
        "bd-track-backup": ("git+file:///b.git", "refs/dolt/beads_global")
    }
    assert all(argv[:3] == ["bd", "sql", "--csv"] and cwd == tmp_path for argv, cwd in seen)


# ---------------------------------------------------------------------------
# end to end: scratch bd database -> local bare repo, distinct refs
# ---------------------------------------------------------------------------


def _bare_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    seed = path.parent / "seed"
    subprocess.run(["git", "clone", "-q", str(path), str(seed)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:main"], check=True,
                   capture_output=True)
    return path


@needs_dolt
def test_push_end_to_end(tmp_path: Path):
    remote = _bare_repo(tmp_path / "backup.git")
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["bd", "init", "--prefix", "e2e", "--quiet"], cwd=root, check=True,
                   capture_output=True)
    project = Project("proj", path=root, beads={"prefix": "e2e"})
    host = {**HOST, "dolt_remote_url": str(remote)}

    assert doltpush.push_project(project, root, host, log).ok
    assert doltpush.push_project(project, root, host, log).ok  # remote reused
    refs = subprocess.run(["git", "-C", str(remote), "for-each-ref", "--format=%(refname)"],
                          check=True, text=True, capture_output=True).stdout.split()

    db = doltpush.open_database(root)
    assert not db.server and f"refs/dolt/{db.name}" in refs
    doltpush.ensure_remote(db, str(remote), "refs/dolt/other", log)  # re-point
    assert doltpush.remotes(db)[doltpush.REMOTE_NAME][1] == "refs/dolt/other"


def test_push_never_adopts_git_origin():
    assert "--no-adopt" in doltpush.PUSH_ARGV
    assert doltpush.PUSH_ARGV[doltpush.PUSH_ARGV.index("--remote") + 1] == doltpush.REMOTE_NAME


# ---------------------------------------------------------------------------
# timing log
# ---------------------------------------------------------------------------


def _rows(path: Path) -> list[list[str]]:
    return [line.split("\t") for line in path.read_text().splitlines()]


def test_push_writes_timing_log(tmp_path: Path, stubbed, push_log: Path):
    project = Project("amo", beads={"prefix": "amo"})
    host = {**HOST, "dolt_remote_url": "/srv/b.git"}
    doltpush.push_project(project, tmp_path, host, log)
    doltpush.push_project(project, tmp_path, host, log)
    rows = _rows(push_log)
    assert rows[0] == list(doltpush.PUSH_LOG_COLUMNS)  # header once
    assert len(rows) == 3
    record = dict(zip(rows[0], rows[1], strict=True))
    assert (record["project"], record["database"], record["mode"]) == ("amo", "amo", "embedded")
    assert (record["remote"], record["duration_s"]) == ("/srv/b.git", "0.10")
    assert record["result"] == "ok"


def test_timing_log_outcomes_and_sanitizing(tmp_path: Path, push_log: Path):
    db = doltpush.Database("beads_global", "server:g", tmp_path)
    project = Project("p")
    doltpush.record_push(project, db, "u", PushResult(False, 120.0, "timed out after 120s"))
    doltpush.record_push(project, db, "u", PushResult(False, 1.5, "bad\tnews\nhere" + "x" * 300))
    timeout, failed = (
        dict(zip(doltpush.PUSH_LOG_COLUMNS, r, strict=True)) for r in _rows(push_log)[1:]
    )
    assert (timeout["result"], timeout["mode"], timeout["db_bytes"]) == ("timeout", "server", "")
    assert failed["result"] == "failed"
    assert failed["detail"].startswith("bad news here") and len(failed["detail"]) <= 200
    assert all(len(r) == len(doltpush.PUSH_LOG_COLUMNS) for r in _rows(push_log))


def test_timing_log_not_written_when_push_skipped(tmp_path: Path, stubbed, push_log: Path):
    off = Project("p", beads={"prefix": "p"})
    doltpush.push_project(off, tmp_path, {**HOST, "dolt_push": False}, log)
    assert not push_log.exists()


def test_timing_log_unwritable_is_ignored(tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    db = doltpush.Database("d", "k", tmp_path)
    doltpush.record_push(Project("p"), db, "u", PushResult(True, 1, "ok"),
                         log_path=blocker / "dolt-push.tsv")  # parent is a file: no raise
