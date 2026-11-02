"""cleanup's Dolt push step: push a project's database to a private git repo under its own ref.

Design: docs/dev/design-beads-state-repo.md, "Dolt push"; keys: docs/registry-schema.md.

- **Where**: ``beads.dolt_remote_url``, else ``global.dolt_remote_url``. bd-track provisions a
  Dolt remote named ``bd-track-backup`` with ``CALL dolt_remote('add', '--ref',
  'refs/dolt/<database>', ...)``, so many databases share one repo. One ref per *database*:
  projects that share a database (a shared server's global database) push it once per run.
- **Modes**: SQL goes through ``dolt sql`` in the database directory (embedded mode; ``bd sql``
  does not support it) or ``bd sql`` (server modes).
- **Guard**: never push to one of the code repo's own remotes. The URL's real host is resolved
  (git ``insteadOf`` rewrites via ``git ls-remote --get-url``, then ``ssh -G`` for ssh aliases);
  on ``github.com`` the repo must not be public (unauthenticated API: 200 is public, 404 is
  private or missing; anything else skips this run). A local path or ``file://`` URL is allowed.
  Any other host needs ``beads.dolt_remote_private: true``.
- **Push**: ``bd dolt push --remote bd-track-backup --no-adopt`` (never let bd adopt the git
  origin), in its own process group, killed after ``global.dolt_push_timeout`` seconds. In
  server mode the push runs inside the server, so a timeout also kills the server-side query.
- Every failure is logged and returns; nothing here raises into cleanup. The next run retries.

WSL2 network stacks have stalled Dolt pushes; such hosts should set ``global.dolt_push: never``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bd_track.registry import Project, RegistryError, dolt_push_enabled, normalize_remote

REMOTE_NAME = "bd-track-backup"
GITHUB_API = "https://api.github.com/repos/{slug}"
API_TIMEOUT = 10
SQL_TIMEOUT = 30
RESOLVE_TIMEOUT = 10

# [user@]host:path, but not scheme://
_SCP_RE = re.compile(r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^/:]+):(?!//)(?P<path>.*)$")


class PushSkipped(Exception):
    """This project is not pushed on this run; the message says why."""


@dataclass
class PushResult:
    ok: bool
    duration: float
    detail: str


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def _local_path(url: str) -> Path | None:
    """The filesystem path a local remote URL names (``/x``, ``~/x``, ``file://``), else None."""
    for prefix in ("git+file://", "file://"):
        if url.startswith(prefix):
            return Path(url[len(prefix):]).expanduser().resolve()
    if url.startswith(("/", "~")):
        return Path(url).expanduser().resolve()
    return None


def _as_url(url: str) -> str:
    """``url`` without Dolt's ``git+`` prefix, with scp form rewritten as ``ssh://``."""
    url = url.removeprefix("git+")
    match = _SCP_RE.match(url)
    if match and "://" not in url:
        user = f"{match['user']}@" if match["user"] else ""
        return f"ssh://{user}{match['host']}/{match['path'].lstrip('/')}"
    return url


def _git_rewrite(url: str) -> str:
    """Apply git's ``url.<base>.insteadOf`` rules, as git (and so Dolt) would."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--get-url", url],
            check=False, text=True, capture_output=True, timeout=RESOLVE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return url
    if result.returncode != 0:
        return url
    return result.stdout.strip() or url


def _ssh_hostname(host: str) -> str:
    """The host ssh really connects to for ``host`` (``ssh -G``: aliases, Include, Match)."""
    try:
        result = subprocess.run(
            ["ssh", "-G", host],
            check=False, text=True, capture_output=True, timeout=RESOLVE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return host
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "hostname" and value:
            return value.strip().lower()
    return host


def resolve_host(url: str) -> str | None:
    """The real host a remote URL connects to, or None for a local URL or an unparsable one."""
    url = _git_rewrite(url.removeprefix("git+"))
    if _local_path(url) is not None:
        return None
    parsed = urllib.parse.urlsplit(_as_url(url))
    host = (parsed.hostname or "").lower() or None
    if host and parsed.scheme in ("ssh", "git+ssh"):
        return _ssh_hostname(host)
    return host


def same_repo(a: str, b: str) -> bool:
    """Whether two remote URLs name the same repository (paths, or owner/repo)."""
    pa, pb = _local_path(a), _local_path(b)
    if pa is not None or pb is not None:
        return pa == pb
    na, nb = normalize_remote(a.removeprefix("git+")), normalize_remote(b.removeprefix("git+"))
    return na is not None and na == nb


def _code_remotes(project_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(project_root), "remote", "-v"],
        check=False, text=True, capture_output=True,
    )
    if result.returncode != 0:
        return []
    return sorted({line.split()[1] for line in result.stdout.splitlines() if line.split()})


def github_is_public(slug: str) -> bool:
    """Ask the GitHub API without credentials: 200 is public, 404 is private or missing.

    Raises ``PushSkipped`` when the answer is unknown (network trouble, rate limit).
    """
    request = urllib.request.Request(
        GITHUB_API.format(slug=slug), headers={"Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT):
            return True
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        raise PushSkipped(
            f"cannot tell whether {slug} is public (GitHub API: HTTP {err.code})"
        ) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise PushSkipped(
            f"cannot tell whether {slug} is public (GitHub API: {err})"
        ) from err


def check_remote(url: str, project: Project, project_root: Path) -> None:
    """The public-remote guard; raises ``PushSkipped`` when ``url`` must not be pushed to."""
    for code in _code_remotes(project_root):
        if same_repo(url, code):
            raise PushSkipped(f"{url} is a remote of the code repo ({code}); refusing")
    if _local_path(url) is not None:
        return
    host = resolve_host(url)
    slug = normalize_remote(url.removeprefix("git+"))
    if host == "github.com" and slug:
        if github_is_public(slug):
            raise PushSkipped(f"{url} is a public GitHub repo; refusing")
        return
    if project.beads.get("dolt_remote_private") is True:
        return
    raise PushSkipped(
        f"cannot verify that {url} is private (host {host or 'unknown'}); set "
        "beads.dolt_remote_private: true if it is"
    )


# ---------------------------------------------------------------------------
# The database: embedded (dolt sql in its directory) or server (bd sql)
# ---------------------------------------------------------------------------


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


@dataclass
class Database:
    """A project's Dolt database and how to run SQL in it."""

    name: str
    key: str  # identity across projects, for one push per database per run
    project_root: Path
    directory: Path | None = None  # embedded mode

    @property
    def server(self) -> bool:
        return self.directory is None

    def sql(self, query: str) -> list[dict[str, str]]:
        """Run ``query``; rows as dicts (CSV output of both tools). ``PushSkipped`` on error."""
        if self.directory is not None:
            argv, cwd = ["dolt", "sql", "-r", "csv", "-q", query], self.directory
        else:
            argv, cwd = ["bd", "sql", "--csv", query], self.project_root
        try:
            result = subprocess.run(
                argv, cwd=cwd, check=False, text=True, capture_output=True, timeout=SQL_TIMEOUT,
            )
        except subprocess.TimeoutExpired as err:
            raise PushSkipped(f"SQL timed out after {SQL_TIMEOUT}s: {query}") from err
        if result.returncode != 0:
            raise PushSkipped(f"SQL failed ({query}): {result.stderr.strip()}")
        text = result.stdout.strip()
        if not text or text == "OK":
            return []
        return list(csv.DictReader(io.StringIO(text)))


def open_database(project_root: Path) -> Database:
    """Find the project's database: one embedded store, else ask bd's server connection."""
    embedded = [p.parent for p in (project_root / ".beads" / "embeddeddolt").glob("*/.dolt")]
    if len(embedded) == 1:
        directory = embedded[0].resolve()
        return Database(directory.name, f"embedded:{directory}", project_root, directory)
    if len(embedded) > 1:
        raise PushSkipped("more than one embedded Dolt database under .beads/embeddeddolt/")
    probe = Database("", "", project_root)
    rows = probe.sql("SELECT database() AS db")
    name = rows[0].get("db", "") if rows else ""
    if not name:
        raise PushSkipped("cannot tell which server database the project uses")
    return Database(name, f"server:{name}", project_root)


def remotes(db: Database) -> dict[str, tuple[str, str | None]]:
    """name -> (url, git ref or None), from the ``dolt_remotes`` system table."""
    found = {}
    for row in db.sql("SELECT name, url, params FROM dolt_remotes"):
        try:
            ref = json.loads(row.get("params") or "{}").get("git_ref")
        except (json.JSONDecodeError, AttributeError):
            ref = None
        found[row["name"]] = (row.get("url", ""), ref)
    return found


def ensure_remote(db: Database, url: str, ref: str, log) -> None:
    """Add (or re-point) the ``bd-track-backup`` remote; bd-track owns that name."""
    current = remotes(db).get(REMOTE_NAME)
    if current is not None:
        if same_repo(current[0], url) and current[1] == ref:
            return
        log.info("push: re-pointing remote %s (was %s %s)", REMOTE_NAME, *current)
        db.sql(f"CALL dolt_remote('remove', {sql_literal(REMOTE_NAME)})")
    db.sql(
        f"CALL dolt_remote('add', '--ref', {sql_literal(ref)}, "
        f"{sql_literal(REMOTE_NAME)}, {sql_literal(url)})"
    )
    log.info("push: added remote %s -> %s (%s)", REMOTE_NAME, url, ref)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

PUSH_ARGV = ("bd", "dolt", "push", "--remote", REMOTE_NAME, "--no-adopt")

# The server-side push bd-track started: its CALL names our remote. Our own connection is
# excluded, since this query's text matches too.
_SERVER_PUSH_QUERY = (
    "SELECT id FROM information_schema.processlist "
    f"WHERE info LIKE '%dolt_push%{REMOTE_NAME}%' AND id <> CONNECTION_ID()"
)


def kill_server_push(db: Database, log) -> None:
    """After a client-side timeout in server mode, stop the push still running in the server.

    Not yet tested against a live shared server (follow-up bead); failures are logged only.
    """
    try:
        ids = [row["id"] for row in db.sql(_SERVER_PUSH_QUERY) if row.get("id", "").isdigit()]
        for query_id in ids:
            db.sql(f"KILL QUERY {query_id}")
            log.info("push: killed server-side push query %s", query_id)
        if not ids:
            log.info("push: no server-side push query left to kill")
    except PushSkipped as err:
        log.error("push: could not kill the server-side push: %s", err)


def run_push(project_root: Path, timeout: float, argv: tuple[str, ...] = PUSH_ARGV) -> PushResult:
    """``bd dolt push`` in its own process group; the whole group is killed on timeout."""
    start = time.monotonic()
    proc = subprocess.Popen(
        argv, cwd=project_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return PushResult(False, time.monotonic() - start, f"timed out after {timeout:g}s")
    duration = time.monotonic() - start
    detail = (out or "").strip().splitlines()[-1:] or [""]
    return PushResult(proc.returncode == 0, duration, detail[0])


def push_project(
    project: Project,
    project_root: Path,
    host: dict,
    log,
    pushed: set[str] | None = None,
) -> PushResult | None:
    """The whole push step for one project. Returns the push result, or None if not pushed.

    ``pushed`` holds the keys of databases already tried this run (the cleanup service passes
    one set for all projects), so a database shared by several projects is pushed once, and a
    stalled one is not retried by each of its projects.
    """
    phase = "Phase 5/5: dolt push"
    try:
        enabled = dolt_push_enabled(project, host)
    except RegistryError as err:
        log.error("%s (skipped — %s)", phase, err)
        return None
    if not enabled:
        log.info("%s (off for this project)", phase)
        return None
    url = project.beads.get("dolt_remote_url") or host.get("dolt_remote_url")
    if not url:
        log.error("%s (skipped — push is on but no dolt_remote_url is configured)", phase)
        return None
    try:
        db = open_database(project_root)
        if pushed is not None and db.key in pushed:
            log.info("%s (database %s already tried this run)", phase, db.name)
            return None
        ref = f"refs/dolt/{db.name}"
        log.info("%s of %s to %s (%s)", phase, db.name, url, ref)
        check_remote(url, project, project_root)
        ensure_remote(db, url, ref, log)
    except PushSkipped as err:
        log.error("push skipped: %s", err)
        return None
    if pushed is not None:
        pushed.add(db.key)
    result = run_push(project_root, host["dolt_push_timeout"])
    if result.ok:
        log.info("push: ok in %.1fs (%s)", result.duration, result.detail)
    else:
        log.error("push failed in %.1fs: %s (retried next run)", result.duration, result.detail)
        if db.server and result.detail.startswith("timed out"):
            kill_server_push(db, log)
    return result
