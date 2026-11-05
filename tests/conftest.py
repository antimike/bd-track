"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_xdg_homes(monkeypatch):
    """Clear the XDG base-dir vars so a test that redirects HOME redirects every base.

    Without this, a developer shell exporting ``XDG_CONFIG_HOME`` etc. would point
    call-time path resolution (``util.xdg_home``) at the real directories. Tests that
    exercise the vars set them explicitly.
    """
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
