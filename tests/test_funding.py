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


def test_live_updates_and_timer_do_not_reread_database(qtbot, monkeypatch):
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    def unexpected_read(*args):
        pytest.fail('Live refresh must not reread the entire database')
    monkeypatch.setattr(chart.store, 'load', unexpected_read)
    now = int(time.time() * 1000)
    point = FundingPoint('okx', now, Decimal('0.0001'))
    chart.append_sample(point)
    chart.advance_time()
    chart.append_sample(point)
    assert chart.plot.points == [point]
    assert chart.shutdown()


def test_paint_computes_time_span_once(qtbot, monkeypatch):
    from okx_gui.funding_chart import FundingPlot
    plot = FundingPlot()
    qtbot.addWidget(plot)
    plot.resize(600, 230)
    plot.points = [FundingPoint('okx', plot.now - i * 1000, Decimal('0.0001'))
                   for i in reversed(range(1000))]
    original = FundingPlot.span.fget
    accesses = []
    def span(self):
        accesses.append(1)
        return original(self)
    monkeypatch.setattr(FundingPlot, 'span', property(span))
    plot.grab()
    assert len(accesses) == 1


def test_display_reduction_preserves_spikes_and_original_samples():
    from okx_gui.funding_chart import render_points
    points = [FundingPoint('okx', i * 1000, Decimal('0')) for i in range(1000)]
    points[321] = FundingPoint('okx', 321000, Decimal('0.5'))
    points[322] = FundingPoint('okx', 322000, Decimal('-0.5'))
    reduced = render_points(points, 10)
    assert len(reduced) <= 40
    assert points[321] in reduced and points[322] in reduced
    assert reduced[0] == points[0] and reduced[-1] == points[-1]
    assert len(points) == 1000


def test_price_cache_is_independent_and_retains_seven_days(tmp_path):
    from okx_gui.funding import PricePoint, PriceStore
    now = 10 * RETENTION_MS
    path = tmp_path / 'funding.sqlite3'
    store = PriceStore(path)
    points = [PricePoint(now - RETENTION_MS - 1, Decimal('80000')),
              PricePoint(now - 1000, Decimal('82601.50001'))]
    store.save('BTC', points, now)
    assert PriceStore(path).load('BTC', now) == points[1:]
    assert store.load('ETH', now) == []
    assert FundingStore(path).load('BTC', now) == []
    store.save('BTC', [], now + RETENTION_MS)
    assert store.load('BTC', now + RETENTION_MS) == []


@pytest.mark.parametrize('stale', [False, True])
def test_price_sampling_uses_quote_feed_and_skips_stale_quotes(tmp_path, monkeypatch, stale):
    from okx_gui.funding import FundingWorker, PriceStore
    worker = FundingWorker('BTC-USDT-SWAP', tmp_path / 'samples.sqlite3')
    clock = [10.0]
    monkeypatch.setattr('okx_gui.funding.time.monotonic', lambda: clock[0])
    worker.set_price('82601.5')
    worker.set_price('invalid')
    if stale:
        clock[0] += 6
    stopped = [False]
    monkeypatch.setattr(worker, 'isInterruptionRequested', lambda: stopped[0])
    async def finish(_):
        stopped[0] = True
    monkeypatch.setattr('okx_gui.funding.asyncio.sleep', finish)
    asyncio.run(worker.collect_prices())
    points = PriceStore(worker.database_path).load('BTC-USDT-SWAP', int(time.time() * 1000))
    assert len(points) == (0 if stale else 1)
    if points:
        assert points[0].price == Decimal('82601.5')


def test_dual_axis_price_series_survives_refresh_and_zoom(qtbot):
    from okx_gui.funding import PricePoint, PriceStore
    from okx_gui.funding_chart import FundingChart
    now = int(time.time() * 1000)
    points = [PricePoint(now - 2000, Decimal('82601.5')),
              PricePoint(now - 1000, Decimal('82620'))]
    PriceStore().save('BTC-USDT-SWAP', points, now)
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    assert chart.plot.price_points == points
    chart.plot.manual_span = 60000
    chart.refresh()
    assert chart.plot.span == 60000
    assert chart.plot.price_points == points
    chart.resize(640, 280)
    assert not chart.plot.grab().isNull()
    assert chart.shutdown()


def test_ctrl_wheel_zooms_each_y_axis_independently(qtbot):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    plot = chart.plot
    plot.resize(640, 230)
    plot.grab()
    original_span = plot.span
    def wheel(x, delta):
        event = QWheelEvent(QPointF(x, 100), QPointF(x, 100), QPoint(),
                            QPoint(0, delta), Qt.NoButton, Qt.ControlModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(plot, event)
        assert event.isAccepted()
    wheel(10, 120)
    assert plot.y_scales == {'funding': 0.8, 'price': 1.0}
    assert plot.scaled_y_range('funding', -1, 1) == (-0.8, 0.8)
    wheel(630, -120)
    assert plot.y_scales == {'funding': 0.8, 'price': 1.25}
    assert plot.span == original_span
    chart.refresh()
    assert plot.y_scales == {'funding': 0.8, 'price': 1.25}
    wheel(10, -120)
    wheel(630, 120)
    assert plot.y_scales == {'funding': 1.0, 'price': 1.0}
    for _ in range(5):
        wheel(10, 2400)
        wheel(630, -2400)
    assert plot.y_scales == {'funding': 0.001, 'price': 1000.0}
    assert chart.shutdown()


def test_left_drag_pans_time_and_both_y_axes_and_double_click_restores(qtbot):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.resize(800, 300)
    chart.show()
    plot = chart.plot
    plot.grab()
    def mouse(kind, position, button, buttons):
        event = QMouseEvent(kind, QPointF(*position), QPointF(*position), button, buttons, Qt.NoModifier)
        QApplication.sendEvent(plot, event)
    start = (250, 90)
    mouse(QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    original_end = plot.end_time
    mouse(QEvent.MouseMove, (300, 110), Qt.NoButton, Qt.LeftButton)
    assert plot.end_time < original_end
    assert plot.y_offsets['funding'] > 0 and plot.y_offsets['price'] > 0
    frozen_end = plot.end_time
    chart.advance_time()
    assert plot.end_time == frozen_end
    mouse(QEvent.MouseButtonRelease, (300, 110), Qt.LeftButton, Qt.NoButton)
    assert plot._drag is None
    mouse(QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    mouse(QEvent.MouseMove, (200, 70), Qt.NoButton, Qt.LeftButton)
    assert plot.end_time > frozen_end
    mouse(QEvent.MouseButtonRelease, (200, 70), Qt.LeftButton, Qt.NoButton)
    mouse(QEvent.MouseButtonDblClick, start, Qt.LeftButton, Qt.LeftButton)
    assert plot.view_end is None
    assert plot.y_offsets == {'funding': 0.0, 'price': 0.0}
    assert chart.shutdown()
