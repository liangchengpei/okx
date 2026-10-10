"""Use headless Qt by default; allow an explicit platform override."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


import pytest


@pytest.fixture(autouse=True)
def isolated_application_data(monkeypatch, tmp_path):
    """GUI tests must never read or overwrite the user's persistent history."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def offline_liquidation_workers(monkeypatch):
    from okx_gui.liquidation_feed import LiquidationWorker
    monkeypatch.setattr(LiquidationWorker, "start", lambda self: None)
