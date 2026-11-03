"""Tests for billing.py — sidecar load + tuple resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from bd_track.billing import load_sidecar, resolve_tuple


@pytest.fixture
def sidecar_file(tmp_path: Path) -> Path:
    """A .beads/ dir; the test writes bd-track.yaml into it."""
    d = tmp_path / ".beads"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# load_sidecar
# ---------------------------------------------------------------------------


def test_load_sidecar_missing_returns_minimal_defaults(sidecar_file):
    data = load_sidecar(sidecar_file)
    assert data == {"default": {}, "patterns": []}


def test_load_sidecar_supplies_missing_keys(sidecar_file):
    (sidecar_file / "bd-track.yaml").write_text("default: {client: foo}\n")
    data = load_sidecar(sidecar_file)
    assert data["default"] == {"client": "foo"}
    assert data["patterns"] == []


def test_load_sidecar_full_round_trip(sidecar_file):
    (sidecar_file / "bd-track.yaml").write_text(
        "default:\n  client: c\n  case: ca\n  svc: s\n"
        "patterns:\n  - match: 'foo'\n    client: alt\n"
    )
    data = load_sidecar(sidecar_file)
    assert data["default"]["client"] == "c"
    assert len(data["patterns"]) == 1
    assert data["patterns"][0]["match"] == "foo"


# ---------------------------------------------------------------------------
# resolve_tuple
# ---------------------------------------------------------------------------


def test_resolve_uses_default_block():
    sidecar = {"default": {"client": "DefaultCo", "svc": "Eng"}, "patterns": []}
    result = resolve_tuple([], sidecar)
    assert result == {"client": "DefaultCo", "case": None, "svc": "Eng"}


def test_resolve_first_matching_pattern_wins():
    sidecar = {
        "default": {"client": "DefaultCo"},
        "patterns": [
            {"match": "area:billable", "client": "AcmeCo", "case": "Q2"},
            {"match": "area:billable", "client": "ShouldNotMatch"},  # earlier wins
        ],
    }
    result = resolve_tuple(["area:billable", "type:feature"], sidecar)
    assert result["client"] == "AcmeCo"
    assert result["case"] == "Q2"


def test_resolve_pattern_can_reference_capture_groups():
    sidecar = {
        "default": {},
        "patterns": [
            {"match": r"client:(?P<name>\w+)", "client": "{name}"},
        ],
    }
    result = resolve_tuple(["client:foo", "area:other"], sidecar)
    assert result["client"] == "foo"


def test_per_issue_case_label_overrides_pattern():
    sidecar = {
        "default": {},
        "patterns": [{"match": "area:billable", "case": "PatternCase"}],
    }
    result = resolve_tuple(["area:billable", "case:PerIssueCase"], sidecar)
    assert result["case"] == "PerIssueCase"


def test_no_match_falls_through_to_default():
    sidecar = {
        "default": {"client": "Default", "svc": "Eng"},
        "patterns": [{"match": "area:billable", "client": "AcmeCo"}],
    }
    result = resolve_tuple(["area:internal"], sidecar)
    assert result == {"client": "Default", "case": None, "svc": "Eng"}


def test_empty_default_string_treated_as_none():
    sidecar = {"default": {"client": "", "svc": ""}, "patterns": []}
    result = resolve_tuple([], sidecar)
    assert result["client"] is None
    assert result["svc"] is None


def test_pattern_without_match_key_is_skipped():
    sidecar = {
        "default": {"client": "Default"},
        "patterns": [
            {"client": "NoMatchKey"},  # skipped
            {"match": "area:billable", "client": "Acme"},
        ],
    }
    result = resolve_tuple(["area:billable"], sidecar)
    assert result["client"] == "Acme"
