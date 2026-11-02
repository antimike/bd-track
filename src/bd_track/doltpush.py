"""cleanup's Dolt push step: push a project's database to a private git repo under its own ref.

Design: docs/dev/design-beads-state-repo.md, "Dolt push"; keys: docs/registry-schema.md.

- **Where**: ``beads.dolt_remote_url``, else ``global.dolt_remote_url``. bd-track provisions a
  Dolt remote named ``bd-track-backup`` in the project's embedded database with
  ``dolt remote add --ref refs/dolt/<prefix>``, so many projects share one repo.
- **Guard**: never push to one of the code repo's own remotes. A github.com URL (or an ssh
  alias host ending in ``.github.com``) must not be a public repo (unauthenticated API: 200 is
  public, 404 is private or missing; anything else skips this run). A local path or ``file://``
  URL is allowed. Any other host needs ``beads.dolt_remote_private: true``.
- **Push**: ``bd dolt push --remote bd-track-backup --no-adopt`` (never let bd adopt the git
  origin), in its own process group, killed after ``global.dolt_push_timeout`` seconds.
- Every failure is logged and returns; nothing here raises into cleanup. The next run retries.

WSL2 network stacks have stalled Dolt pushes; such hosts should set ``global.dolt_push: never``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bd_track.registry import Project, normalize_remote, switch_enabled

REMOTE_NAME = "bd-track-backup"
GITHUB_API = "https://api.github.com/repos/{slug}"
API_TIMEOUT = 10


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


def _host(url: str) -> str | None:
    """The host of an ssh/https/scp-style URL, lowercased."""
    url = url.removeprefix("git+")
    if "://" in url:
        rest = url.split("://", 1)[1]
    elif ":" in url:  # scp form: [user@]host:path
        rest = url.split(":", 1)[0]
    else:
        return None
    host = rest.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0]
    return host.lower() or None


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
    host = _host(url)
    slug = normalize_remote(url.removeprefix("git+"))
    # ``<alias>.github.com`` covers ssh config aliases such as ``antimike.github.com``.
    if host and (host == "github.com" or host.endswith(".github.com")) and slug:
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
# Provisioning
# ---------------------------------------------------------------------------


def dolt_dir(project_root: Path) -> Path:
    """The embedded Dolt database directory; ``PushSkipped`` for other modes."""
    found = [p.parent for p in (project_root / ".beads" / "embeddeddolt").glob("*/.dolt")]
    if len(found) != 1:
        raise PushSkipped(
            "no single embedded Dolt database under .beads/embeddeddolt/ "
            "(server mode is not supported by the push step yet)"
        )
    return found[0]


def _remotes(db: Path) -> dict[str, tuple[str, str | None]]:
    """``dolt remote -v``: name -> (url, git ref or None)."""
    result = subprocess.run(
        ["dolt", "remote", "-v"], cwd=db, check=False, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise PushSkipped(f"dolt remote -v failed: {result.stderr.strip()}")
    remotes = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        ref = None
        if len(parts) == 3:
            try:
                ref = json.loads(parts[2]).get("git_ref")
            except (json.JSONDecodeError, AttributeError):
                ref = None
        remotes[parts[0]] = (parts[1], ref)
    return remotes


def ensure_remote(db: Path, url: str, ref: str, log) -> None:
    """Add (or re-point) the ``bd-track-backup`` remote; bd-track owns that name."""
    current = _remotes(db).get(REMOTE_NAME)
    if current is not None:
        if same_repo(current[0], url) and current[1] == ref:
            return
        log.info("push: re-pointing remote %s (was %s %s)", REMOTE_NAME, *current)
        subprocess.run(["dolt", "remote", "remove", REMOTE_NAME], cwd=db, check=False,
                       capture_output=True)
    result = subprocess.run(
        ["dolt", "remote", "add", "--ref", ref, REMOTE_NAME, url],
        cwd=db, check=False, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise PushSkipped(f"dolt remote add failed: {result.stderr.strip()}")
    log.info("push: added remote %s -> %s (%s)", REMOTE_NAME, url, ref)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


PUSH_ARGV = ("bd", "dolt", "push", "--remote", REMOTE_NAME, "--no-adopt")


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


def push_project(project: Project, project_root: Path, host: dict, log) -> PushResult | None:
    """The whole push step for one project. Returns the push result, or None if not pushed."""
    phase = "Phase 5/5: dolt push"
    if not switch_enabled(host["dolt_push"], project.beads.get("dolt_push")):
        log.info("%s (off for this project)", phase)
        return None
    url = project.beads.get("dolt_remote_url") or host.get("dolt_remote_url")
    if not url:
        log.error("%s (skipped — push is on but no dolt_remote_url is configured)", phase)
        return None
    prefix = project.beads.get("prefix") or project.name
    ref = f"refs/dolt/{prefix}"
    log.info("%s to %s (%s)", phase, url, ref)
    try:
        check_remote(url, project, project_root)
        ensure_remote(dolt_dir(project_root), url, ref, log)
    except PushSkipped as err:
        log.error("push skipped: %s", err)
        return None
    result = run_push(project_root, host["dolt_push_timeout"])
    if result.ok:
        log.info("push: ok in %.1fs (%s)", result.duration, result.detail)
    else:
        log.error("push failed in %.1fs: %s (retried next run)", result.duration, result.detail)
    return result
