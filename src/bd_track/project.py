"""Project lifecycle: init-project, cleanup, run-service.

Configures Beads projects with recommended settings, registers them for
automated cleanup via systemd timers, and provisions optional Dolt
server-mode wiring (.envrc + pass credential).

`init-project` bootstraps `bd init` when `.beads/` is missing, writes the
empirically-verified hang preventer (`dolt.auto-push: false`) directly to
`.beads/config.yaml` before any `bd config set` call, then applies the
remaining settings via the bd CLI. The auto-push hang is a known beads/Dolt
remote pitfall.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from bd_track.templates import render
from bd_track.util import (
    CLEANUP_STATE,
    REPOS_CONFIG,
    SYSTEMD_CLEANUP_NAME,
    SYSTEMD_IDLE_STOP_NAME,
    cleanup_log,
    confirm,
    find_beads_dir,
    prompt,
    root_log,
    run,
)

_ENVRC_MARKER = "# bd-track: beads dolt server"

_INTERVAL_ALIASES: dict[str, str] = {
    "daily": "1d",
    "weekly": "7d",
    "hourly": "1h",
}


def _bootstrap_bd_init(
    project_root: Path,
    *,
    server_mode: bool | None,
    sandbox: bool,
    prefix: str | None,
    agents_profile: str,
) -> None:
    """Run `bd init` in `project_root` when `.beads/` is missing (PROJ-xji).

    Forwards the recommended flags from the empirically-verified hang-safe
    init flow: ``--actor bd-track --dolt-auto-commit batch [--server]
    [--sandbox] [--prefix ...] [--agents-profile full]``.

    ``--non-interactive`` is intentionally NOT forwarded — bd 1.0.x has been
    observed to hang under that flag. If the caller is non-interactive, they
    must supply enough flags that bd init has no prompts to ask. If bd init
    fails, this function exits the process; do not try to recover.
    """
    cmd = ["bd", "--actor", "bd-track", "--dolt-auto-commit", "batch", "init"]
    if server_mode:
        cmd.append("--server")
    if sandbox:
        cmd.append("--sandbox")
    if prefix:
        cmd.extend(["--prefix", prefix])
    if agents_profile:
        cmd.extend(["--agents-profile", agents_profile])

    root_log.info("Bootstrapping Beads: %s (cwd=%s)", " ".join(cmd), project_root)
    result = run(cmd, cwd=project_root, check=False)
    if result.returncode != 0:
        sys.exit(
            f"bd-track init-project: `bd init` failed (exit {result.returncode}); "
            "fix the failure and re-run, or pass --no-bootstrap to skip this step."
        )


def _ensure_auto_push_disabled(beads_dir: Path) -> None:
    """Append `dolt.auto-push: false` to `.beads/config.yaml` if not already disabled.

    Done BEFORE any `bd config set` invocation to prevent the auto-push hang
    that fires on every bd write when `sync.remote` is set. See the
    `pitfalls-beads-dolt-remote.md` "Three push mechanisms" entry for the
    empirical verification.

    Honors both flat (``dolt.auto-push: false``) and nested
    (``dolt:`` / ``  auto-push: false``) forms — bd's runtime accepts either.
    Writes nested form when starting clean, flat-key form when appending to
    an existing config (avoids brittle YAML structural editing).
    """
    config_yaml = beads_dir / "config.yaml"
    content = config_yaml.read_text() if config_yaml.exists() else ""

    # Already disabled in either form? (Crude but the strings are
    # unambiguous in this context — no other config key overlaps.)
    if "auto-push: false" in content or "auto-push: \"false\"" in content:
        return

    if content.strip() == "":
        new = "dolt:\n  auto-push: false\n"
    else:
        # Append flat-key form. bd's runtime resolves both; using the flat
        # form here avoids parsing existing YAML structure and lets bd's
        # `config show` precedence rules sort it out.
        sep = "" if content.endswith("\n") else "\n"
        new = content + f"{sep}dolt.auto-push: false\n"

    config_yaml.write_text(new)
    root_log.info("Wrote dolt.auto-push: false to %s (prevents init-project hang)", config_yaml)


# ---------------------------------------------------------------------------
# Repos config helpers
# ---------------------------------------------------------------------------

def load_repos_config() -> dict:
    if not REPOS_CONFIG.exists():
        return {"global": {"defaults": {"statuses": [], "hooks": []}}, "repos": []}
    with REPOS_CONFIG.open() as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("global", {})
    data["global"].setdefault("defaults", {})
    data["global"]["defaults"].setdefault("statuses", [])
    data["global"]["defaults"].setdefault("hooks", [])
    data.setdefault("repos", [])
    return data


def save_repos_config(data: dict) -> None:
    REPOS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with REPOS_CONFIG.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


def _get_repo_entry(config: dict, project_path: str) -> dict | None:
    for repo in config["repos"]:
        if repo.get("path") == project_path:
            return repo
    return None


def _load_cleanup_state() -> dict:
    if not CLEANUP_STATE.exists():
        return {}
    with CLEANUP_STATE.open() as f:
        return json.load(f)


def _save_cleanup_state(state: dict) -> None:
    CLEANUP_STATE.parent.mkdir(parents=True, exist_ok=True)
    with CLEANUP_STATE.open("w") as f:
        json.dump(state, f, indent=2, default=str)


def parse_cadence(cadence_str: str) -> dt.timedelta:
    """Parse a cadence like '1d', '12h', '30m' into a timedelta."""
    m = re.fullmatch(r"(\d+)([dhm])", cadence_str.strip())
    if not m:
        sys.exit(f"bd-track: invalid cadence '{cadence_str}' — use e.g. 1d, 7d, 12h")
    value, unit = int(m.group(1)), m.group(2)
    return {"d": dt.timedelta(days=value), "h": dt.timedelta(hours=value),
            "m": dt.timedelta(minutes=value)}[unit]


# ---------------------------------------------------------------------------
# Server-mode helpers
# ---------------------------------------------------------------------------

def _pass_entry_exists(pass_path: str) -> bool:
    return run(["pass", "show", pass_path], check=False, capture=True).returncode == 0


def _provision_pass_entry(pass_path: str, *, yes: bool = False) -> None:
    if _pass_entry_exists(pass_path):
        root_log.info("pass entry already exists at %s", pass_path)
        return

    root_log.info("No pass entry found at %s", pass_path)
    if confirm("Generate a random password with `pass generate`?", default=True, yes=yes):
        run(["pass", "generate", "--no-symbols", pass_path, "32"])
        root_log.info("Generated password stored at %s", pass_path)
    else:
        password = getpass.getpass(f"Enter password for {pass_path}: ")
        subprocess.run(
            ["pass", "insert", "--multiline", "--force", pass_path],
            input=password + "\n",
            text=True,
            check=True,
        )
        root_log.info("Stored password at %s", pass_path)


def _update_envrc(project_path: Path, pass_path: str) -> None:
    envrc = project_path / ".envrc"
    stanza = render("envrc/dolt-server.envrc.tmpl", PASS_PATH=pass_path)

    if envrc.exists():
        content = envrc.read_text()
        if _ENVRC_MARKER in content:
            root_log.info(".envrc already contains Dolt server stanza (not duplicated)")
            return
        envrc.write_text(content.rstrip("\n") + "\n\n" + stanza)
    else:
        envrc.write_text(stanza)

    root_log.info("Updated %s", envrc)
    root_log.info("Run `direnv allow %s` to activate the new stanza.", project_path)


def _provision_dolt_user(
    project_root: Path, dolt_user: str, pass_path: str,
) -> None:
    """Create or update the Dolt MySQL user with the password from `pass`.

    `bd dolt set user` only writes client-side metadata.json; the Dolt
    server's mysql.user table is independent and must be updated separately.
    Without this step, bd commands fail with `Access denied` on first
    connection after a fresh server-mode init.

    Idempotent: uses CREATE USER IF NOT EXISTS + ALTER USER + GRANT for the
    'localhost', '127.0.0.1', and '%' host variants.
    """
    if not shutil.which("mysql"):
        root_log.warning(
            "mysql client not found on PATH; skipping Dolt user provisioning. "
            "Create the user manually: CREATE USER '%s'@'localhost' IDENTIFIED BY ...",
            dolt_user,
        )
        return

    pw_result = run(["pass", "show", pass_path], check=False, capture=True)
    if pw_result.returncode != 0:
        root_log.warning(
            "pass show %s failed; skipping Dolt user provisioning.", pass_path,
        )
        return
    password = pw_result.stdout.split("\n", 1)[0]
    if not password:
        root_log.warning(
            "pass entry %s has empty first line; skipping Dolt user provisioning.",
            pass_path,
        )
        return

    show = run(["bd", "dolt", "show"], check=False, capture=True, cwd=project_root)
    host = "127.0.0.1"
    port = "3306"
    for line in show.stdout.splitlines():
        line = line.strip()
        if line.startswith("Host:"):
            host = line.split(":", 1)[1].strip()
        elif line.startswith("Port:"):
            port = line.split(":", 1)[1].strip()

    # SQL string-literal escape: double any single quote.
    pw_esc = password.replace("'", "''")

    stmts = []
    for h in ("localhost", "127.0.0.1", "%"):
        stmts.append(f"CREATE USER IF NOT EXISTS '{dolt_user}'@'{h}' IDENTIFIED BY '{pw_esc}';")
        stmts.append(f"ALTER USER '{dolt_user}'@'{h}' IDENTIFIED BY '{pw_esc}';")
        stmts.append(f"GRANT ALL PRIVILEGES ON *.* TO '{dolt_user}'@'{h}' WITH GRANT OPTION;")
    stmts.append("FLUSH PRIVILEGES;")
    sql = "\n".join(stmts) + "\n"

    proc = subprocess.run(
        ["mysql", "-h", host, "-P", port, "-u", "root", "--protocol=TCP"],
        input=sql, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        root_log.warning(
            "Dolt user provisioning failed (mysql exit %d): %s",
            proc.returncode, proc.stderr.strip(),
        )
        return
    root_log.info("Dolt user %s provisioned on %s:%s (localhost, 127.0.0.1, %%)",
                  dolt_user, host, port)


# ---------------------------------------------------------------------------
# init-project
# ---------------------------------------------------------------------------

def cmd_init_project(
    path: Path | None,
    days: int | None,
    commit_cadence: str | None,
    statuses_arg: str | None,
    hooks_arg: bool | None,
    no_git_ops: bool,
    install_systemd: bool,
    check_interval: str,
    idle_stop_hours: int | None,
    server_mode: bool | None,
    dolt_user: str | None,
    pass_path: str | None,
    yes: bool,
    bootstrap: bool = True,
    sandbox: bool = True,
    prefix: str | None = None,
    agents_profile: str = "full",
) -> None:
    # Bootstrap path (PROJ-xji): if .beads/ doesn't exist, run `bd init`
    # ourselves. Forwards --server (when known), --sandbox (default true,
    # safest setting from empirical testing), --prefix, and the chosen
    # agents profile. Skip with --no-bootstrap to preserve the legacy
    # "register-only" behavior for pre-existing .beads/ directories.
    candidate_root = (path or Path.cwd()).resolve()
    if bootstrap and not (candidate_root / ".beads").is_dir():
        _bootstrap_bd_init(
            candidate_root,
            server_mode=server_mode,
            sandbox=sandbox,
            prefix=prefix,
            agents_profile=agents_profile,
        )

    beads_dir = find_beads_dir(path)
    project_root = beads_dir.parent.resolve()
    project_path = str(project_root)

    # Pre-empt the auto-push hang (see pitfalls-beads-dolt-remote.md): write
    # `dolt.auto-push: false` directly to .beads/config.yaml BEFORE any
    # `bd config set` invocation. If we let `bd config set` run first, that
    # very write triggers the auto-push and hangs.
    _ensure_auto_push_disabled(beads_dir)

    sidecar = beads_dir / "bd-track.yaml"
    if not sidecar.exists():
        sidecar.write_text(render("sidecar/bd-track.yaml.tmpl"))
        root_log.info("Created %s — edit it to map bead labels to billing tuple values.", sidecar)
    else:
        root_log.debug("Sidecar already exists at %s, skipping scaffold.", sidecar)

    config = load_repos_config()
    existing_entry = _get_repo_entry(config, project_path)
    if existing_entry is not None:
        root_log.info("Project already registered: %s", project_path)
        if not confirm(
            "Re-apply all init-project settings to this project?",
            default=True, yes=yes,
        ):
            root_log.info("Aborted.")
            return
    else:
        root_log.info("Initialising Beads project at %s", project_path)

    global_defaults = config["global"]["defaults"]

    effective_days = days
    if effective_days is None:
        effective_days = int(prompt("Compact history window (days)", "7", yes=yes))

    effective_cadence = commit_cadence
    if effective_cadence is None:
        effective_cadence = prompt("Commit cadence (e.g. 1d, 12h)", "1d", yes=yes)

    default_statuses_str = ",".join(global_defaults["statuses"]) or ""
    if statuses_arg is not None:
        raw_statuses = statuses_arg
    else:
        raw_statuses = prompt(
            "Custom statuses to enable (comma-separated, blank for none)",
            default_statuses_str, yes=yes,
        )
    effective_statuses = [s.strip() for s in raw_statuses.split(",") if s.strip()]

    if hooks_arg is not None:
        effective_hooks: bool = hooks_arg
    else:
        effective_hooks = confirm("Run  bd hooks install  to install git hook shims?",
                                  default=True, yes=yes)

    dolt_available = bool(shutil.which("dolt"))
    if server_mode is not None:
        effective_server: bool = server_mode
    elif not dolt_available:
        effective_server = False
        root_log.info("dolt binary not found — skipping server mode wiring")
    else:
        effective_server = confirm(
            "Configure Dolt server mode (.envrc + pass credential)?",
            default=True, yes=yes,
        )

    if idle_stop_hours is not None:
        effective_idle_hours: int = idle_stop_hours
    else:
        effective_idle_hours = int(
            prompt("Idle server stop threshold (hours, 0 to disable)", "4", yes=yes)
        )

    # `dolt.auto-push: false` is set via direct file write earlier (see
    # _ensure_auto_push_disabled) to break the chicken-and-egg hang; do not
    # duplicate it here. Setting `no-push: true` would gate `bd dolt push`
    # (the explicit subcommand), which we don't need to suppress.
    settings: list[tuple[str, str]] = [
        ("dolt.auto-commit", "batch"),
    ]
    if no_git_ops:
        settings.append(("no-git-ops", "true"))
    if effective_statuses:
        settings.append(("status.custom", ",".join(effective_statuses)))

    for key, value in settings:
        result = run(
            ["bd", "config", "set", key, value],
            check=False, capture=True, cwd=project_root,
        )
        if result.returncode != 0:
            root_log.warning("config set %s=%s failed: %s", key, value, result.stderr.strip())
        else:
            root_log.info("config set %s=%s", key, value)

    if effective_hooks:
        result = run(["bd", "hooks", "install"], check=False, capture=True, cwd=project_root)
        if result.returncode != 0:
            root_log.warning("bd hooks install failed: %s", result.stderr.strip())
        else:
            root_log.info("bd hooks install: ok")

    effective_pass_path: str | None = pass_path
    if effective_server:
        if not dolt_available:
            root_log.warning(
                "--server requested but dolt binary not found; skipping server wiring"
            )
            effective_server = False
        else:
            effective_user = dolt_user or project_root.name.lower()
            result = run(
                ["bd", "dolt", "set", "user", effective_user],
                check=False, capture=True, cwd=project_root,
            )
            if result.returncode != 0:
                root_log.warning(
                    "bd dolt set user %s failed: %s",
                    effective_user, result.stderr.strip(),
                )
            else:
                root_log.info("Dolt user set to %s", effective_user)

            if not shutil.which("pass"):
                root_log.warning(
                    "pass not found on PATH; skipping .envrc password wiring. "
                    "Add BEADS_DOLT_PASSWORD to .envrc manually."
                )
            else:
                if effective_pass_path is None:
                    default_suggestion = f"beads/{project_root.name.lower()}"
                    effective_pass_path = prompt(
                        "pass store path for Dolt password",
                        default_suggestion, yes=yes,
                    )
                _provision_pass_entry(effective_pass_path, yes=yes)
                _update_envrc(project_root, effective_pass_path)
                _provision_dolt_user(project_root, effective_user, effective_pass_path)

    repo_entry: dict = existing_entry if existing_entry is not None else {}
    repo_entry["path"] = project_path
    repo_entry["commit_cadence"] = effective_cadence
    repo_entry["compact_days"] = effective_days
    repo_entry["statuses"] = effective_statuses
    if effective_server and effective_pass_path:
        repo_entry["server"] = {
            "enabled": True,
            "pass_path": effective_pass_path,
            "dolt_user": dolt_user or project_root.name.lower(),
        }
    elif not effective_server:
        repo_entry.pop("server", None)

    if existing_entry is None:
        config["repos"].append(repo_entry)
    config["global"]["idle_stop_hours"] = effective_idle_hours
    save_repos_config(config)
    root_log.info(
        "%s in %s",
        "Updated" if existing_entry else "Registered",
        REPOS_CONFIG,
    )

    global_interval = config["global"].get("check_interval", check_interval)
    try:
        interval_td = parse_cadence(_INTERVAL_ALIASES.get(global_interval, global_interval))
        repo_cadence_td = parse_cadence(effective_cadence)
        if interval_td > repo_cadence_td:
            root_log.warning(
                "check_interval '%s' is coarser than commit_cadence '%s' — "
                "commits for this project may be delayed",
                global_interval, effective_cadence,
            )
    except SystemExit:
        pass

    if not install_systemd:
        return

    _install_systemd_units(check_interval, effective_idle_hours, config)


def _install_systemd_units(
    check_interval: str, idle_stop_hours: int, config: dict,
) -> None:
    """Install the cleanup + idle-stop systemd user units (idempotent)."""
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    bd_track_path = shutil.which("bd-track") or sys.argv[0]

    def _write_unit(path: Path, content: str, label: str) -> None:
        if not path.exists():
            path.write_text(content)
            root_log.info("Wrote %s", path)
        else:
            root_log.info("%s already exists, skipping: %s", label, path)

    cleanup_service = systemd_dir / f"{SYSTEMD_CLEANUP_NAME}.service"
    cleanup_timer = systemd_dir / f"{SYSTEMD_CLEANUP_NAME}.timer"
    _write_unit(
        cleanup_service,
        render("systemd/bd-track-cleanup.service.tmpl", BD_TRACK_PATH=bd_track_path),
        "Cleanup service",
    )
    if not cleanup_timer.exists():
        config["global"]["check_interval"] = check_interval
        save_repos_config(config)
        cleanup_timer.write_text(render(
            "systemd/bd-track-cleanup.timer.tmpl",
            CHECK_INTERVAL=check_interval,
            SERVICE_NAME=SYSTEMD_CLEANUP_NAME,
        ))
        root_log.info("Wrote %s", cleanup_timer)
    else:
        root_log.info("Cleanup timer already exists, skipping: %s", cleanup_timer)

    if idle_stop_hours > 0:
        idle_service = systemd_dir / f"{SYSTEMD_IDLE_STOP_NAME}.service"
        idle_timer = systemd_dir / f"{SYSTEMD_IDLE_STOP_NAME}.timer"
        _write_unit(
            idle_service,
            render(
                "systemd/bd-track-idle-stop.service.tmpl",
                BD_TRACK_PATH=bd_track_path,
                IDLE_STOP_HOURS=str(idle_stop_hours),
            ),
            "Idle-stop service",
        )
        _write_unit(
            idle_timer,
            render("systemd/bd-track-idle-stop.timer.tmpl", SERVICE_NAME=SYSTEMD_IDLE_STOP_NAME),
            "Idle-stop timer",
        )

    result = run(["systemctl", "--user", "daemon-reload"], check=False, capture=True)
    if result.returncode != 0:
        root_log.error("daemon-reload failed: %s", result.stderr.strip())
        return

    units_to_enable = [f"{SYSTEMD_CLEANUP_NAME}.timer"]
    if idle_stop_hours > 0:
        units_to_enable.append(f"{SYSTEMD_IDLE_STOP_NAME}.timer")

    result = run(
        ["systemctl", "--user", "enable", "--now", *units_to_enable],
        check=False, capture=True,
    )
    if result.returncode != 0:
        root_log.error("enable --now failed: %s", result.stderr.strip())
    else:
        for u in units_to_enable:
            root_log.info("Timer enabled and started: %s", u)


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

def cmd_cleanup(
    days: int,
    *,
    commit: bool = False,
    project_dir: Path | None = None,
) -> None:
    """Run Beads/Dolt maintenance for the active project (or --project-dir)."""
    beads_dir = find_beads_dir(project_dir)
    project_root = beads_dir.parent.resolve()
    log = logging.LoggerAdapter(
        cleanup_log,
        extra={"project": str(project_root)},
    )
    log.info("Starting cleanup (days=%d, commit=%s)", days, commit)

    if commit:
        log.info("Phase 1/3: dolt commit")
        result = run(
            ["bd", "dolt", "commit"], check=False, capture=True, cwd=project_root
        )
        if result.returncode != 0:
            log.warning("dolt commit: %s", result.stderr.strip() or result.stdout.strip())
        else:
            log.info("dolt commit: %s", result.stdout.strip())
    else:
        log.info("Phase 1/3: dolt commit (skipped — pass --commit to include)")

    log.info("Phase 2/3: compact --days %d", days)
    result = run(
        ["bd", "compact", f"--days={days}", "--force"],
        check=False, capture=True, cwd=project_root,
    )
    if result.returncode != 0:
        log.error("compact failed: %s", result.stderr.strip())
    else:
        log.info("compact: %s", result.stdout.strip())

    log.info("Phase 3/3: gc")
    result = run(
        ["bd", "gc", "--skip-decay", "--force"],
        check=False, capture=True, cwd=project_root,
    )
    if result.returncode != 0:
        log.error("gc failed: %s", result.stderr.strip())
    else:
        log.info("gc: %s", result.stdout.strip())

    log.info("Cleanup complete")


# ---------------------------------------------------------------------------
# run-service (systemd cleanup entrypoint)
# ---------------------------------------------------------------------------

def cmd_run_service() -> None:
    """Iterate registered repos and run cleanup when due per repo cadence."""
    config = load_repos_config()
    state = _load_cleanup_state()
    now = dt.datetime.now(dt.timezone.utc)

    for repo in config.get("repos", []):
        path = repo.get("path")
        if not path:
            cleanup_log.warning("repos.yaml entry missing 'path', skipping")
            continue

        cadence_str = repo.get("commit_cadence", "1d")
        cadence = parse_cadence(cadence_str)
        last_run_str = state.get(path, {}).get("last_run")
        last_run = (
            dt.datetime.fromisoformat(last_run_str).replace(tzinfo=dt.timezone.utc)
            if last_run_str
            else dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        )

        if now - last_run < cadence:
            cleanup_log.info(
                "%s: next cleanup in %s, skipping",
                path, cadence - (now - last_run),
            )
            continue

        cleanup_log.info("%s: due for cleanup (cadence=%s)", path, cadence_str)
        try:
            cmd_cleanup(
                days=repo.get("compact_days", 7),
                commit=True,
                project_dir=Path(path),
            )
            state.setdefault(path, {})["last_run"] = now.isoformat()
            _save_cleanup_state(state)
        except Exception:
            cleanup_log.exception("cleanup failed for %s", path)


# ---------------------------------------------------------------------------
# config init (per-project sidecar scaffold)
# ---------------------------------------------------------------------------

def cmd_config_init(project_dir: Path | None = None) -> None:
    """Scaffold a per-project .beads/bd-track.yaml from the packaged template.

    Refuses to overwrite an existing sidecar.
    """
    beads_dir = find_beads_dir(project_dir)
    sidecar = beads_dir / "bd-track.yaml"
    if sidecar.exists():
        sys.exit(f"bd-track config init: refusing to overwrite existing {sidecar}")
    sidecar.write_text(render("sidecar/bd-track.yaml.tmpl"))
    root_log.info("Created %s", sidecar)
    root_log.info("Edit it to map your bead labels to billing tuple values.")
