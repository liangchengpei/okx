import asyncio
from decimal import Decimal
import time

import pytest

from okx_gui.account_ratio import RatioPoint, RatioStore, RatioWorker, parse_ratios, POLL_SECONDS
from okx_gui.funding import RETENTION_MS


def test_ratio_parser_preserves_source_timestamps_orders_and_deduplicates():
    points = parse_ratios({'code': '0', 'data': [['600000', '1.25'], ['300000', '0.9'], ['600000', '1.3']]})
    assert points == [RatioPoint(300000, Decimal('0.9')), RatioPoint(600000, Decimal('1.3'))]


@pytest.mark.parametrize('payload', [
    {'code': '51000', 'msg': 'invalid instrument'},
    {'code': '0', 'data': [['300000', 'NaN']]},
    {'code': '0', 'data': [['300000', '-1']]},
    {'code': '0', 'data': None},
])
def test_ratio_errors_never_create_fake_values(payload):
    with pytest.raises(ValueError):
        parse_ratios(payload)


def test_ratio_cache_isolated_and_retains_seven_days(tmp_path):
    now = RETENTION_MS * 10
    store = RatioStore(tmp_path / 'ratio.sqlite3')
    points = [RatioPoint(now - RETENTION_MS - 1, Decimal('2')), RatioPoint(now - 1000, Decimal('1.123456'))]
    store.save('BTC-USDT-SWAP', points, now)
    store.save('BTC-USDT-SWAP', points, now)
    assert store.load('BTC-USDT-SWAP', now) == points[1:]
    assert store.load('SPCX-USDT-SWAP', now) == []
    assert store.load('BTC-USDT-SWAP', now + RETENTION_MS) == []


def test_ratio_worker_requests_5m_on_start_then_waits_five_minutes(tmp_path, monkeypatch):
    from okx_gui import account_ratio as module
    now = int(time.time() * 1000)
    calls, delays, received = [], [], []
    worker = RatioWorker('BTC-USDT-SWAP', tmp_path / 'ratio.sqlite3')
    worker.sampled.connect(received.append)
    stopped = [False]
    monkeypatch.setattr(worker, 'isInterruptionRequested', lambda: stopped[0])
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'code': '0', 'data': [[str(now), '1.25']]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params):
            calls.append((url, params))
            return Response()
    async def sleep(delay):
        delays.append(delay)
        stopped[0] = True
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: Client())
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    asyncio.run(worker._run())
    assert calls == [(module.RATIO_URL, {'instId': 'BTC-USDT-SWAP', 'period': '5m', 'limit': '100'})]
    assert 295 < delays[0] <= POLL_SECONDS == 300
    assert received == [[RatioPoint(now, Decimal('1.25'))]]


def test_ratio_overlay_independent_of_candle_period_and_left_axis_zoom(qtbot):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.resize(800, 350)
    chart.show()
    now = int(time.time() * 1000)
    points = [RatioPoint(now - 300000, Decimal('1.1')), RatioPoint(now, Decimal('1.3'))]
    chart.append_ratios(points)
    chart.append_ratios(points)
    chart.interval.setCurrentIndex(1)
    assert chart.plot.ratio_points == points
    assert '1.3' in chart.ratio_status.text()
    chart.plot.grab()
    event = QWheelEvent(QPointF(10, 100), QPointF(10, 100), QPoint(), QPoint(0, 120),
                        Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(chart.plot, event)
    assert chart.plot.y_scales == {'price': 1.0, 'ratio': 0.8}
    chart.update_ratio_status('接口暂不可用')
    assert chart.plot.ratio_points == points
    assert chart.ratio_status.text() == '接口暂不可用'
    assert chart.shutdown()
