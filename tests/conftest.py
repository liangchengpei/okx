"""Use headless Qt by default; allow an explicit platform override."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


import pytest


@pytest.fixture(autouse=True)
def isolated_application_data(monkeypatch, tmp_path):
    """GUI tests must never read or overwrite the user's persistent history."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture(autouse=True)
def offline_liquidation_workers(monkeypatch):
    from okx_gui.liquidation_feed import LiquidationWorker
    from okx_gui.funding import FundingWorker
    from okx_gui.account_ratio import RatioWorker
    monkeypatch.setattr(RatioWorker, "start", lambda self: None)
    from okx_gui.order_book import OrderBookWorker
    monkeypatch.setattr(OrderBookWorker, "start", lambda self: None)
    from okx_gui.trade_volume_feed import TradeVolumeWorker
    monkeypatch.setattr(TradeVolumeWorker, "start", lambda self: None)
    monkeypatch.setattr(FundingWorker, "start", lambda self: None)
    monkeypatch.setattr(LiquidationWorker, "start", lambda self: None)


@pytest.fixture(autouse=True)
def offline_leverage_rules(monkeypatch):
    from decimal import Decimal
    from okx_gui.trading import LeverageTask
    def start(task):
        task.completed.emit((Decimal('100'), Decimal('10')))
        task.finished.emit()
    monkeypatch.setattr(LeverageTask, 'start', start)


@pytest.fixture(autouse=True)
def isolate_order_audit(monkeypatch):
    monkeypatch.setattr('okx_gui.order_audit.audit', lambda *args, **kwargs: True)
