"""Billing tuple resolution.

Reads the per-project sidecar (``.beads/bd-track.yaml``), fetches the active
issue from ``bd``, and resolves a ``(client, case, svc)`` tuple via:

  1. Per-issue ``case:<string>`` label (escape hatch)
  2. First matching ``patterns:`` rule (regex on space-joined label list)
  3. ``default:`` block in the sidecar
  4. ``None`` for any unset field — the caller decides how to display
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

from bd_track.util import path_compat, root_log, run


def load_sidecar(beads_dir: Path) -> dict:
    """Load ``.beads/bd-track.yaml`` (legacy ``bd-timew.yaml`` fallback); warn if absent."""
    path = path_compat(beads_dir / "bd-track.yaml", beads_dir / "bd-timew.yaml")
    if not path.exists():
        root_log.warning(
            "no sidecar at %s; using minimal defaults. Run `bd-track config init` to scaffold one.",
            path,
        )
        return {"default": {}, "patterns": []}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("default", {})
    data.setdefault("patterns", [])
    return data


def get_issue(issue_id: str) -> dict:
    """Return the issue dict from ``bd show <id> --json``; exits on failure."""
    result = run(["bd", "show", issue_id, "--json"], check=False, capture=True)
    if result.returncode != 0:
        sys.exit(f"bd-track: `bd show {issue_id}` failed:\n{result.stderr}")
    data = json.loads(result.stdout)
    if isinstance(data, list):
        if not data:
            sys.exit(f"bd-track: issue {issue_id} not found")
        return data[0]
    return data


def resolve_tuple(labels: list[str], sidecar: dict) -> dict:
    """Resolve labels to ``{client, case, svc}`` via per-issue/pattern/default rules."""
    default = dict(sidecar.get("default") or {})
    tuple_: dict[str, str | None] = {
        "client": default.get("client") or None,
        "case": default.get("case") or None,
        "svc": default.get("svc") or None,
    }
    label_str = " ".join(labels)
    for rule in sidecar.get("patterns") or []:
        pattern = rule.get("match")
        if not pattern:
            continue
        m = re.search(pattern, label_str)
        if m:
            groups = m.groupdict()
            for key in ("client", "case", "svc"):
                if key in rule:
                    val = rule[key]
                    if groups and isinstance(val, str):
                        val = val.format(**groups)
                    tuple_[key] = val
            break
    for label in labels:
        if label.startswith("case:"):
            tuple_["case"] = label[len("case:") :]
            break
    return tuple_
