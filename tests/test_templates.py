"""Tests for template rendering — packaged systemd/envrc/sidecar templates."""

from __future__ import annotations

import pytest

from bd_track.templates import render


def test_render_systemd_cleanup_service():
    out = render("systemd/bd-track-cleanup.service.tmpl", BD_TRACK_PATH="/usr/local/bin/bd-track")
    assert "ExecStart=/usr/local/bin/bd-track run-service" in out
    assert "${BD_TRACK_PATH}" not in out


def test_render_systemd_cleanup_timer():
    out = render(
        "systemd/bd-track-cleanup.timer.tmpl",
        CHECK_INTERVAL="daily",
        SERVICE_NAME="bd-track-cleanup",
    )
    assert "OnCalendar=daily" in out
    assert "Unit=bd-track-cleanup.service" in out


def test_render_systemd_idle_stop_service():
    out = render(
        "systemd/bd-track-idle-stop.service.tmpl",
        BD_TRACK_PATH="/usr/local/bin/bd-track",
        IDLE_STOP_HOURS="4",
    )
    assert "/usr/local/bin/bd-track idle-stop --hours 4" in out


def test_render_envrc_dolt_server():
    out = render("envrc/dolt-server.envrc.tmpl", PASS_PATH="beads/myproj")
    assert "pass show beads/myproj" in out
    assert "${PASS_PATH}" not in out


def test_render_sidecar_template_has_no_unbound_vars():
    """The sidecar scaffold has no ${VAR} references — should render with no kwargs."""
    out = render("sidecar/bd-track.yaml.tmpl")
    assert "default:" in out
    assert "patterns:" in out


def test_render_unbound_variable_raises():
    with pytest.raises(ValueError, match="references unset variable"):
        render("systemd/bd-track-cleanup.service.tmpl")  # missing BD_TRACK_PATH


def test_render_does_not_expand_bare_dollar_var():
    """expandvars is configured surrounded_vars_only=True; bare $VAR must not expand.

    The .envrc template legitimately contains $(pass show ...) and "$(...|head -1)".
    Those must survive rendering unchanged.
    """
    out = render("envrc/dolt-server.envrc.tmpl", PASS_PATH="beads/myproj")
    assert "$(pass show" in out
    assert "$(pass show beads/myproj | head -1)" in out
