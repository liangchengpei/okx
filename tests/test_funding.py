import asyncio
from decimal import Decimal
import time

import pytest

from okx_gui.funding import FundingPoint, FundingStore, RETENTION_MS, fetch_current_funding


def test_seven_day_cache_precision_retention_dedup_and_contract_isolation(tmp_path):
    now = 10 * RETENTION_MS
    path = tmp_path / 'funding.sqlite3'
    store = FundingStore(path)
    points = [FundingPoint('okx', now - RETENTION_MS, Decimal('0.000123456789')),
              FundingPoint('bybit', now - 10, Decimal('-0.0001')),
              FundingPoint('binance', now - RETENTION_MS - 1, Decimal('0.0002'))]
    store.save('BTC', points, now)
    store.save('BTC', points, now)
    assert FundingStore(path).load('BTC', now) == points[:2]
    assert store.load('ETH', now) == []
    assert store.load('BTC', now + 1) == points[1:2]
    corrected = FundingPoint('bybit', now - 10, Decimal('0.0003'))
    store.save('BTC', [corrected], now)
    assert store.load('BTC', now) == [corrected]


def test_current_rate_uses_local_sampling_time_and_not_settlement_rate(monkeypatch):
    monkeypatch.setattr('okx_gui.funding.time.time', lambda: 200.001)
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'code': '0', 'data': [{'instId': 'BTC-USDT-SWAP',
                'ts': '123456', 'fundingTime': '999999',
                'fundingRate': '-0.000123456789', 'settFundingRate': '0.0009'}]}
    class Client:
        async def get(self, url, params):
            assert url.endswith('/public/funding-rate')
            assert params == {'instId': 'BTC-USDT-SWAP'}
            return Response()
    assert asyncio.run(fetch_current_funding(Client(), 'BTC-USDT-SWAP')) == FundingPoint(
        'okx', 200001, Decimal('-0.000123456789'))


def test_api_error_does_not_create_fake_points():
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'code': '51000', 'msg': 'unknown instrument'}
    class Client:
        async def get(self, *args, **kwargs):
            return Response()
    with pytest.raises(ValueError, match='unknown instrument'):
        asyncio.run(fetch_current_funding(Client(), 'UNKNOWN-USDT-SWAP'))


def test_live_cache_does_not_read_old_settlement_table(tmp_path):
    import sqlite3
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE funding_rates (instrument, exchange, timestamp, rate)')
        db.execute("INSERT INTO funding_rates VALUES ('BTC','okx',100,'0.1')")
    assert FundingStore(path).load('BTC', 200) == []


def test_chart_restores_only_okx_cache_and_keeps_curve_on_failure(qtbot):
    from okx_gui.funding_chart import FundingChart, COLORS
    now = int(time.time() * 1000)
    points = [FundingPoint(exchange, now - 60000, Decimal(value)) for exchange,value in
              [('okx','0.0001'),('binance','-0.0001'),('bybit','0.0002')]]
    FundingStore().save('BTC-USDT-SWAP', points, now)
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.resize(640,280)
    chart.show()
    assert chart.plot.points == [points[0]]
    chart.update_status('okx', '网络连接失败（保留缓存）')
    assert chart.plot.points == [points[0]]
    assert set(COLORS) == {'okx'}
    from okx_gui.funding import EXCHANGES
    assert EXCHANGES == ('okx',)
    assert '币安' not in chart.status_label.text()
    assert 'Bybit' not in chart.status_label.text()
    assert chart.plot.grab().width() > 0
    assert chart.shutdown()


@pytest.mark.parametrize('status,body,expected', [
    (451, 'restricted location', '网络出口地区受限'),
    (403, 'block access from your country', '网络出口地区受限'),
    (403, 'Forbidden', '接口返回 HTTP 403'),
    (500, 'server error', '接口返回 HTTP 500'),
])
def test_http_failures_show_actionable_reason(status, body, expected):
    import httpx
    from okx_gui.funding import funding_error_message
    response = httpx.Response(status, text=body, request=httpx.Request('GET', 'https://example.com'))
    with pytest.raises(httpx.HTTPStatusError) as error:
        response.raise_for_status()
    assert expected in funding_error_message(error.value)


def test_funding_proxy_is_optional_and_does_not_override_environment_defaults(monkeypatch):
    from okx_gui.funding import funding_client_options
    monkeypatch.delenv('OKX_FUNDING_PROXY', raising=False)
    assert funding_client_options() == {'timeout': 12}
    monkeypatch.setenv('OKX_FUNDING_PROXY', ' http://127.0.0.1:7890 ')
    assert funding_client_options()['proxy'] == 'http://127.0.0.1:7890'


def test_chart_keeps_each_live_sample_without_time_buckets(qtbot):
    from okx_gui.funding_chart import FundingChart
    from PySide6.QtWidgets import QComboBox
    now = int(time.time() * 1000)
    points = [FundingPoint('okx', now - offset, Decimal(rate))
              for offset, rate in [(45000, '0.0001'), (30000, '0.0002'), (15000, '-0.0001')]]
    FundingStore().save('BTC-USDT-SWAP', points, now)
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    assert chart.plot.points == points
    assert not chart.findChildren(QComboBox)
    assert chart.plot.span == 3600000
    assert chart.shutdown()


def test_same_exchange_timestamp_keeps_separate_successful_samples(monkeypatch, tmp_path):
    import httpx
    ticks = iter([200.0, 201.0])
    monkeypatch.setattr('okx_gui.funding.time.time', lambda: next(ticks))
    class Client:
        async def get(self, *args, **kwargs):
            return httpx.Response(200, request=httpx.Request('GET', 'https://example.com'),
                json={'code': '0', 'data': [{'instId': 'BTC-USDT-SWAP',
                    'ts': '100000', 'fundingRate': '0.0001'}]})
    async def collect():
        return [await fetch_current_funding(Client(), 'BTC-USDT-SWAP') for _ in range(2)]
    points = asyncio.run(collect())
    store = FundingStore(tmp_path / 'samples.sqlite3')
    store.save('BTC', points, 201000)
    assert [p.timestamp for p in store.load('BTC', 201000)] == [200000, 201000]


def test_wheel_zoom_keeps_latest_time_and_survives_refresh(qtbot):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    plot = chart.plot
    def wheel(delta):
        event = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(),
                            QPoint(0, delta), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(plot, event)
        assert event.isAccepted()
    original = plot.span
    wheel(120)
    assert plot.span < original
    zoomed = plot.span
    chart.refresh()
    assert plot.span == zoomed
    wheel(-120)
    assert abs(plot.span - original) <= 1
    for _ in range(4):
        wheel(2400)
    assert plot.span == 60000
    for _ in range(4):
        wheel(-2400)
    assert plot.span == RETENTION_MS
    assert chart.shutdown()
