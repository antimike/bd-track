"""Scoped bead-execution queue subcommands.

Maintains an ordered list of bead IDs per scope in ``.beads/queue.yaml``.
Scope resolution: ``--scope`` flag → ``$BD_TRACK_SCOPE`` env var → ``'default'``.

Schema (current, list-of-strings) — backward compatible:

    pipeline:
      - PROJ-foo
      - PROJ-bar
    tooling:
      - PROJ-baz

Speculative schema additions (queue-level filter/notes/description, per-item
notes) are documented in DESIGN_NOTES.md but not yet implemented.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from bd_track.billing import get_issue
from bd_track.util import (
    QUEUE_FILE,
    confirm,
    env_compat,
    find_beads_dir,
    is_interactive,
    root_log,
    run,
)

# Statuses that indicate a queue entry is no longer actionable. Removed by
# `clean` (mechanical) and surfaced by `prune` (analytical).
STALE_STATUSES = {"closed", "deferred"}


def _fetch_title(bead_id: str) -> str:
    """Return the bead title, or '' on any error (non-fatal)."""
    try:
        return get_issue(bead_id).get("title", "")
    except SystemExit:
        return ""


def _fetch_issue_safe(bead_id: str) -> dict | None:
    """Return the bead dict, or ``None`` if bd lookup fails."""
    try:
        return get_issue(bead_id)
    except SystemExit:
        return None


def resolve_scope(scope_arg: str | None) -> str:
    """Resolve scope: ``--scope`` flag → ``$BD_TRACK_SCOPE`` → ``'default'``.

    Passes ``'all'`` through unchanged (only meaningful for ``clear``/``clean``).
    """
    if scope_arg is not None:
        return scope_arg
    env_scope = (env_compat("BD_TRACK_SCOPE") or "").strip()
    return env_scope if env_scope else "default"


def load_all_queues(beads_dir: Path) -> dict[str, list[str]]:
    """Read the queue file; return a dict of scope → list of bead IDs."""
    path = beads_dir / QUEUE_FILE
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return {k: list(v or []) for k, v in data.items() if isinstance(v, list)}


def save_all_queues(beads_dir: Path, data: dict[str, list[str]]) -> None:
    """Persist non-empty scopes to the queue file; delete the file if all empty."""
    path = beads_dir / QUEUE_FILE
    cleaned = {k: v for k, v in data.items() if v}
    if cleaned:
        with path.open("w") as f:
            yaml.dump(cleaned, f, default_flow_style=False, allow_unicode=True)
    elif path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Core queue actions (push/unshift/pop/peek/list/remove/clear)
# ---------------------------------------------------------------------------

def cmd_queue(
    action: str,
    *,
    ids: list[str] | None = None,
    scope_arg: str | None = None,
    project_dir: Path | None = None,
    titles: bool = False,
) -> None:
    """Dispatch one of: list, push, unshift, pop, peek, remove, clear.

    Kept as a single dispatcher for backward compatibility with existing tests
    and the CLI router. New analytical commands (clean/generate/prune) live in
    their own ``cmd_*`` functions because their parameter shapes diverge.
    """
    beads_dir = find_beads_dir(project_dir)
    explicit_scope = scope_arg is not None or bool((env_compat("BD_TRACK_SCOPE") or "").strip())
    scope = resolve_scope(scope_arg)
    all_queues = load_all_queues(beads_dir)

    def _fmt(bead_id: str) -> str:
        if not titles:
            return bead_id
        title = _fetch_title(bead_id)
        return f"{bead_id}  {title}" if title else bead_id

    if action == "list":
        if not explicit_scope:
            if not all_queues:
                root_log.info("All queues are empty")
                return
            for scope_name, queue in all_queues.items():
                if queue:
                    print(f"[{scope_name}]")
                    for i, bead_id in enumerate(queue, 1):
                        print(f"  {i:2d}. {_fmt(bead_id)}")
        else:
            queue = all_queues.get(scope, [])
            if not queue:
                root_log.info("Queue '%s' is empty", scope)
                return
            for i, bead_id in enumerate(queue, 1):
                print(f"{i:2d}. {_fmt(bead_id)}")

    elif action == "push":
        if not ids:
            sys.exit("bd-track queue push: requires at least one <id>")
        queue = all_queues.get(scope, [])
        added = [i for i in ids if i not in queue]
        already = [i for i in ids if i in queue]
        queue.extend(added)
        all_queues[scope] = queue
        save_all_queues(beads_dir, all_queues)
        if added:
            root_log.info("[%s] Pushed: %s", scope, ", ".join(added))
        if already:
            root_log.warning("[%s] Already in queue (skipped): %s", scope, ", ".join(already))

    elif action == "unshift":
        if not ids or len(ids) != 1:
            sys.exit("bd-track queue unshift: requires exactly one <id>")
        bead_id = ids[0]
        queue = all_queues.get(scope, [])
        if bead_id in queue:
            queue.remove(bead_id)
        queue.insert(0, bead_id)
        all_queues[scope] = queue
        save_all_queues(beads_dir, all_queues)
        root_log.info("[%s] Prepended: %s", scope, bead_id)

    elif action == "pop":
        queue = all_queues.get(scope, [])
        if not queue:
            root_log.info("Queue '%s' is empty", scope)
            return
        head = queue.pop(0)
        all_queues[scope] = queue
        save_all_queues(beads_dir, all_queues)
        print(head)
        if titles:
            title = _fetch_title(head)
            if title:
                root_log.info("%s  %s", head, title)

    elif action == "peek":
        queue = all_queues.get(scope, [])
        if not queue:
            root_log.info("Queue '%s' is empty", scope)
            return
        print(queue[0])
        if titles:
            title = _fetch_title(queue[0])
            if title:
                root_log.info("%s  %s", queue[0], title)

    elif action == "remove":
        if not ids:
            sys.exit("bd-track queue remove: requires at least one <id>")
        queue = all_queues.get(scope, [])
        removed = []
        for bead_id in ids:
            if bead_id in queue:
                queue.remove(bead_id)
                removed.append(bead_id)
            else:
                root_log.warning("[%s] %s not in queue", scope, bead_id)
        if removed:
            all_queues[scope] = queue
            save_all_queues(beads_dir, all_queues)
            root_log.info("[%s] Removed: %s", scope, ", ".join(removed))

    elif action == "clear":
        if scope == "all":
            save_all_queues(beads_dir, {})
            root_log.info("All queues cleared")
        else:
            all_queues.pop(scope, None)
            save_all_queues(beads_dir, all_queues)
            root_log.info("Queue '%s' cleared", scope)


# ---------------------------------------------------------------------------
# clean — mechanical sweep of stale entries (closed/deferred beads)
# ---------------------------------------------------------------------------

def cmd_clean(
    *,
    scope_arg: str | None = None,
    project_dir: Path | None = None,
    quiet: bool = False,
) -> int:
    """Remove closed/deferred beads from one or all queue scopes.

    Default behaviour (no ``--scope`` and no env var): sweep every scope. This
    is the form invoked by ``bd-track stop`` after a stop completes. Pass an
    explicit scope to limit the sweep.

    Returns the number of entries removed across all scopes.
    """
    beads_dir = find_beads_dir(project_dir)
    explicit_scope = scope_arg is not None or bool((env_compat("BD_TRACK_SCOPE") or "").strip())
    all_queues = load_all_queues(beads_dir)

    if not all_queues:
        if not quiet:
            root_log.info("All queues are empty")
        return 0

    target_scopes = (
        [resolve_scope(scope_arg)] if explicit_scope else list(all_queues.keys())
    )

    removed_total = 0
    for scope in target_scopes:
        queue = all_queues.get(scope, [])
        if not queue:
            continue
        keep, drop = [], []
        for bead_id in queue:
            issue = _fetch_issue_safe(bead_id)
            if issue is None:
                # Bead not found — leave it so the user notices via prune.
                keep.append(bead_id)
                continue
            if issue.get("status") in STALE_STATUSES:
                drop.append((bead_id, issue.get("status")))
            else:
                keep.append(bead_id)
        if drop:
            all_queues[scope] = keep
            removed_total += len(drop)
            for bead_id, status in drop:
                root_log.info("[%s] Removed stale bead: %s (status=%s)", scope, bead_id, status)

    if removed_total:
        save_all_queues(beads_dir, all_queues)
    elif not quiet:
        root_log.info("No stale entries found")

    return removed_total


# ---------------------------------------------------------------------------
# generate — build a queue from search/filter criteria
# ---------------------------------------------------------------------------

def _bd_list(
    *,
    statuses: list[str] | None = None,
    labels_all: list[str] | None = None,
    labels_any: list[str] | None = None,
    label_pattern: str | None = None,
    title_contains: str | None = None,
    project_dir: Path | None = None,
) -> list[dict]:
    """Run ``bd list --json`` with the given filters and return the parsed list."""
    cmd = ["bd", "list", "--json"]
    if statuses:
        cmd += ["--status", ",".join(statuses)]
    for lbl in labels_all or []:
        cmd += ["--label", lbl]
    for lbl in labels_any or []:
        cmd += ["--label-any", lbl]
    if label_pattern:
        cmd += ["--label-pattern", label_pattern]
    if title_contains:
        cmd += ["--title-contains", title_contains]

    cwd = project_dir.parent if project_dir is not None else None
    result = run(cmd, check=False, capture=True, cwd=cwd)
    if result.returncode != 0:
        sys.exit(f"bd-track queue generate: `bd list` failed:\n{result.stderr}")
    data = json.loads(result.stdout) if result.stdout.strip() else []
    return data if isinstance(data, list) else []


def cmd_generate(
    *,
    scope_arg: str | None = None,
    project_dir: Path | None = None,
    statuses: list[str] | None = None,
    labels_all: list[str] | None = None,
    labels_any: list[str] | None = None,
    label_pattern: str | None = None,
    title_contains: str | None = None,
    keyword: str | None = None,
    append: bool = False,
    yes: bool = False,
) -> None:
    """Generate a queue scope from ``bd list`` filters.

    If the scope already has entries and ``--append`` is not set, prompts for
    confirmation before replacing (or aborts if non-interactive without -y).

    ``keyword`` is a convenience alias for ``--title-contains`` (kept distinct
    for future expansion to description matching).
    """
    beads_dir = find_beads_dir(project_dir)
    scope = resolve_scope(scope_arg)
    all_queues = load_all_queues(beads_dir)

    if title_contains is None and keyword:
        title_contains = keyword

    issues = _bd_list(
        statuses=statuses or ["open", "in_progress"],
        labels_all=labels_all,
        labels_any=labels_any,
        label_pattern=label_pattern,
        title_contains=title_contains,
        project_dir=beads_dir,
    )
    matched = [i["id"] for i in issues if i.get("id")]

    if not matched:
        root_log.info("No beads matched the given filters; queue '%s' unchanged", scope)
        return

    existing = all_queues.get(scope, [])
    if append:
        added = [bid for bid in matched if bid not in existing]
        existing.extend(added)
        all_queues[scope] = existing
        save_all_queues(beads_dir, all_queues)
        root_log.info("[%s] Appended %d new bead(s); queue length is now %d",
                      scope, len(added), len(existing))
        return

    # Replace mode
    if existing and not yes:
        if not is_interactive():
            sys.exit(
                f"bd-track queue generate: scope '{scope}' has {len(existing)} entries; "
                "pass --yes to replace, or use --append."
            )
        if not confirm(
            f"Replace {len(existing)} existing entries in '{scope}' with {len(matched)} new?",
            default=False, yes=False,
        ):
            root_log.info("Aborted; queue '%s' unchanged", scope)
            return

    all_queues[scope] = matched
    save_all_queues(beads_dir, all_queues)
    root_log.info("[%s] Generated queue with %d bead(s)", scope, len(matched))


# ---------------------------------------------------------------------------
# prune — analytical: identify issues and propose changes for confirmation
# ---------------------------------------------------------------------------

# Heuristic: scope name → labels expected on its members. Used for scope-mismatch
# detection until queue-level filters land (Part C).
_SCOPE_HEURISTICS = {
    "tooling": {"requires_label": "scope:local"},
    "pipeline": {"forbids_label": "scope:local"},
    "portal":   {"forbids_label": "scope:local"},
    "snowflake": {"forbids_label": "scope:local"},
}


def _proposals_for_queue(
    scope: str,
    queue: list[str],
    other_queues: dict[str, list[str]],
) -> list[tuple[str, str, str]]:
    """Return a list of (action, bead_id, reason) tuples for one queue.

    Actions:
      - ``remove``     — bead is closed/deferred or not found in beads
      - ``move``       — bead label suggests a different scope
      - ``reorder``    — bead has a blocker that appears later in the queue
      - ``add-before`` — a blocker of an in-queue bead is itself missing/queued
    """
    proposals: list[tuple[str, str, str]] = []
    fetched: dict[str, dict | None] = {bid: _fetch_issue_safe(bid) for bid in queue}
    pos = {bid: i for i, bid in enumerate(queue)}

    heuristic = _SCOPE_HEURISTICS.get(scope, {})

    for bid in queue:
        data = fetched[bid]
        if data is None:
            proposals.append(("remove", bid, "not found in beads"))
            continue

        # 1. Stale
        status = data.get("status")
        if status in STALE_STATUSES:
            proposals.append(("remove", bid, f"status is {status}"))
            continue

        labels = data.get("labels") or []

        # 2. Scope mismatch
        req = heuristic.get("requires_label")
        forb = heuristic.get("forbids_label")
        if req and req not in labels:
            proposals.append(("move", bid, f"missing expected label '{req}' for scope '{scope}'"))
        if forb and forb in labels:
            target = "tooling" if forb == "scope:local" else "(other)"
            proposals.append(
                ("move", bid, f"has '{forb}'; suggests '{target}' queue rather than '{scope}'"),
            )

        # 3. Dependency ordering
        deps = data.get("dependencies") or []
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            dep_type = dep.get("dependency_type")
            if dep_type not in ("blocks", "blocked-by", "parent-child"):
                continue
            dep_id = dep.get("id")
            if not dep_id:
                continue
            dep_status = dep.get("status")
            if dep_status in STALE_STATUSES:
                continue
            if dep_id in pos and pos[dep_id] > pos[bid]:
                proposals.append(
                    ("reorder", bid, f"blocked by {dep_id} which appears later in queue"),
                )
            elif dep_id not in pos:
                # Skip if the dependency lives in another known queue.
                already_elsewhere = any(
                    dep_id in q for s, q in other_queues.items() if s != scope
                )
                if already_elsewhere:
                    continue
                proposals.append(
                    ("add-before", dep_id,
                     f"required by {bid} (status={dep_status}) but missing from any queue"),
                )

    # Deduplicate while preserving order.
    seen, deduped = set(), []
    for p in proposals:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _apply_proposals(
    beads_dir: Path,
    scope: str,
    proposals: list[tuple[str, str, str]],
) -> None:
    """Apply the agreed-on proposals to the queue file.

    Only ``remove`` is auto-applied. ``move``, ``reorder``, and ``add-before``
    are surfaced for the user to act on manually — auto-moving across scopes
    or reordering risks user surprise. We log each as a recommendation and let
    the user run the appropriate ``push``/``remove``/``unshift`` themselves.
    """
    all_queues = load_all_queues(beads_dir)
    queue = list(all_queues.get(scope, []))
    removed_ids = {bid for action, bid, _ in proposals if action == "remove"}
    if removed_ids:
        queue = [bid for bid in queue if bid not in removed_ids]
        all_queues[scope] = queue
        save_all_queues(beads_dir, all_queues)
        root_log.info("[%s] Auto-removed %d stale bead(s)", scope, len(removed_ids))

    deferred = [(a, b, r) for a, b, r in proposals if a != "remove"]
    if deferred:
        root_log.info(
            "[%s] %d non-destructive recommendation(s) need manual action:",
            scope, len(deferred),
        )
        for action, bid, reason in deferred:
            root_log.info("  • [%s] %s — %s", action, bid, reason)


def cmd_prune(
    *,
    scope_arg: str | None = None,
    project_dir: Path | None = None,
    yes: bool = False,
) -> None:
    """Analyse one or all queue scopes and propose changes for confirmation.

    With ``--yes``, applies the destructive subset (stale removals) automatically
    and surfaces the rest as recommendations. Without it, requires interactive
    confirmation before any removals.
    """
    beads_dir = find_beads_dir(project_dir)
    explicit_scope = scope_arg is not None or bool((env_compat("BD_TRACK_SCOPE") or "").strip())
    all_queues = load_all_queues(beads_dir)

    if not all_queues:
        root_log.info("All queues are empty")
        return

    target_scopes = (
        [resolve_scope(scope_arg)] if explicit_scope else list(all_queues.keys())
    )

    any_proposals = False
    for scope in target_scopes:
        queue = all_queues.get(scope, [])
        if not queue:
            continue
        proposals = _proposals_for_queue(
            scope, queue,
            other_queues={s: q for s, q in all_queues.items() if s != scope},
        )
        if not proposals:
            root_log.info("[%s] no issues found", scope)
            continue
        any_proposals = True
        print(f"[{scope}] {len(proposals)} proposal(s):")
        for action, bid, reason in proposals:
            print(f"  • [{action}] {bid}  — {reason}")

        # Confirm before applying.
        if yes:
            _apply_proposals(beads_dir, scope, proposals)
            continue
        if not is_interactive():
            root_log.warning(
                "[%s] non-interactive and --yes not set; reporting only", scope,
            )
            continue
        if confirm(f"Apply destructive changes for '{scope}'?", default=False, yes=False):
            _apply_proposals(beads_dir, scope, proposals)
        else:
            root_log.info("[%s] skipped", scope)

    if not any_proposals:
        root_log.info("No issues found across queue(s)")
