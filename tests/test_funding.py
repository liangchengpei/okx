import asyncio
from decimal import Decimal
import time

import pytest

from okx_gui.funding import FundingPoint, FundingStore, PricePoint, PriceStore, RETENTION_MS, fetch_current_funding


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


def test_price_chart_ignores_funding_history(qtbot):
    from okx_gui.funding_chart import FundingChart
    from PySide6.QtWidgets import QLabel
    now = int(time.time() * 1000)
    FundingStore().save('BTC-USDT-SWAP', [FundingPoint('okx', now, Decimal('0.001'))], now)
    points = [PricePoint(now - 1000, Decimal('82000'))]
    PriceStore().save('BTC-USDT-SWAP', points, now)
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    assert chart.plot.price_points == points
    assert all('费率' not in label.text() for label in chart.findChildren(QLabel))
    assert set(chart.plot.y_scales) == {'price'}
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


def test_chart_keeps_raw_samples_and_offers_price_aggregation(qtbot):
    from okx_gui.funding_chart import FundingChart
    from PySide6.QtWidgets import QComboBox
    now = int(time.time() * 1000)
    points = [PricePoint(now - offset, Decimal(rate))
              for offset, rate in [(45000, '82000'), (30000, '82100'), (15000, '81900')]]
    PriceStore().save('BTC-USDT-SWAP', points, now)
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    assert chart.plot.price_points == points
    assert chart.interval.count() == 2
    assert [chart.interval.itemData(i) for i in range(2)] == [1, 15]
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
    monkeypatch.setattr(chart.price_store, 'load', unexpected_read)
    now = int(time.time() * 1000)
    point = PricePoint(now, Decimal('82000'))
    chart.append_price(point)
    chart.advance_time()
    chart.append_price(point)
    assert chart.plot.price_points == [point]
    assert chart.shutdown()


def test_paint_computes_time_span_once(qtbot, monkeypatch):
    from okx_gui.funding_chart import FundingPlot
    plot = FundingPlot()
    qtbot.addWidget(plot)
    plot.resize(600, 230)
    plot.price_points = [PricePoint(plot.now - i * 1000, Decimal('82000'))
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


def test_price_series_survives_refresh_and_zoom(qtbot):
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


def test_ctrl_wheel_zooms_price_from_either_side(qtbot):
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
    assert plot.y_scales == {'price': 0.8}
    assert plot.scaled_y_range('price', -1, 1) == (-0.8, 0.8)
    wheel(630, -120)
    assert plot.y_scales == {'price': 1.0}
    assert plot.span == original_span
    chart.refresh()
    assert plot.y_scales == {'price': 1.0}
    wheel(10, -120)
    wheel(630, 120)
    assert plot.y_scales == {'price': 1.0}
    for _ in range(5):
        wheel(10, 2400)
    assert plot.y_scales == {'price': 0.001}
    for _ in range(5):
        wheel(630, -2400)
    assert plot.y_scales == {'price': 1000.0}
    assert chart.shutdown()


def test_left_drag_pans_time_and_price_axis_and_double_click_restores(qtbot):
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
    assert plot.y_offsets['price'] > 0
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
    assert plot.y_offsets == {'price': 0.0}
    assert chart.shutdown()


def test_left_drag_allows_future_space_and_refresh_preserves_view(qtbot):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.resize(800, 300)
    chart.show()
    chart.plot.grab()
    plot = chart.plot
    original_now = plot.now
    original_prices = list(plot.price_points)
    origin = plot.plot_rect.center()
    target = origin - QPointF(plot.plot_rect.width() / 4, 0)

    def mouse(kind, position, button, buttons):
        QApplication.sendEvent(plot, QMouseEvent(kind, position, position, button, buttons, Qt.NoModifier))

    mouse(QEvent.MouseButtonPress, origin, Qt.LeftButton, Qt.LeftButton)
    mouse(QEvent.MouseMove, target, Qt.NoButton, Qt.LeftButton)
    mouse(QEvent.MouseButtonRelease, target, Qt.LeftButton, Qt.NoButton)
    assert plot.end_time == pytest.approx(original_now + plot.span / 4)
    assert plot.y_offsets == {'price': 0.0}
    frozen = plot.end_time
    chart.advance_time()
    assert plot.end_time == frozen
    assert plot.price_points == original_prices
    assert plot.grab().width() > 0
    mouse(QEvent.MouseButtonDblClick, origin, Qt.LeftButton, Qt.LeftButton)
    assert plot.view_end is None and plot.end_time == plot.now
    assert chart.shutdown()


def test_worker_no_longer_polls_funding(tmp_path, monkeypatch):
    from okx_gui.funding import FundingWorker
    worker = FundingWorker('BTC-USDT-SWAP', tmp_path / 'prices.sqlite3')
    calls = []
    async def prices():
        calls.append('prices')
    async def forbidden(*args):
        pytest.fail('Price chart must not request funding rates')
    monkeypatch.setattr(worker, 'collect_prices', prices)
    monkeypatch.setattr('okx_gui.funding.fetch_current_funding', forbidden)
    asyncio.run(worker._run())
    assert calls == ['prices']


@pytest.mark.parametrize('minutes', [1, 15])
def test_price_candles_preserve_ohlc_gaps_and_precision(minutes):
    from okx_gui.funding_chart import aggregate_prices, PriceCandle
    period = minutes * 60000
    points = [PricePoint(period * 100, Decimal('82000')),
              PricePoint(period * 101 - 1, Decimal('82001.123456')),
              PricePoint(period * 101, Decimal('81900')),
              PricePoint(period * 103 + 5, Decimal('82100'))]
    result = aggregate_prices(points, minutes)
    assert result == [PriceCandle(period * 100, Decimal('82000'), Decimal('82001.123456'), Decimal('82000'), Decimal('82001.123456')),
                      PriceCandle(period * 101, *[Decimal('81900')]*4),
                      PriceCandle(period * 103, *[Decimal('82100')]*4)]
    assert len(points) == 4


def test_price_segments_use_green_up_red_down_gray_flat():
    from okx_gui.funding_chart import price_color, UP_COLOR, DOWN_COLOR, PRICE_COLOR
    assert price_color(Decimal('1'), Decimal('2')) == UP_COLOR == '#15966b'
    assert price_color(Decimal('2'), Decimal('1')) == DOWN_COLOR == '#d94b55'
    assert price_color(Decimal('1'), Decimal('1')) == PRICE_COLOR


def test_switching_price_period_reaggregates_and_keeps_view(qtbot):
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    now = 1800000899000  # End of a 15-minute interval.
    chart.plot.now = now
    chart.plot.price_points = [PricePoint(now - i * 60000, Decimal(82000 + i)) for i in reversed(range(15))]
    chart.plot.manual_span = 3600000
    chart.plot.view_end = now + 60000
    before = list(chart.plot.price_points)
    assert len(chart.plot.visible_prices(now, chart.plot.span)) == 15
    chart.interval.setCurrentIndex(1)
    points = chart.plot.visible_prices(now, chart.plot.span)
    assert len(points) == 1 and points[0].price == Decimal(82000)
    assert chart.plot.view_end == now + 60000 and chart.plot.span == 3600000
    assert chart.plot.price_points == before
    chart.plot.price_points.append(PricePoint(now + 100, Decimal('82099.5')))
    chart.plot.now = now + 100
    assert chart.plot.visible_prices(now + 100, chart.plot.span)[-1].price == Decimal('82099.5')
    chart.plot.grab()
    assert chart.shutdown()


def test_candle_wicks_include_intraperiod_extremes_and_current_close():
    from okx_gui.funding_chart import aggregate_prices
    points = [PricePoint(60000 + i * 1000, Decimal(value))
              for i, value in enumerate(('100', '120', '80', '110'))]
    candle = aggregate_prices(points, 1)[0]
    assert (candle.open, candle.high, candle.low, candle.close) == (100, 120, 80, 110)
    points.append(PricePoint(65000, Decimal('90')))
    updated = aggregate_prices(points, 1)[0]
    assert (updated.open, updated.high, updated.low, updated.close) == (100, 120, 80, 90)


def test_visible_extremes_exclude_offscreen_candles_and_use_wicks():
    from okx_gui.funding_chart import PriceCandle, visible_extremes, format_price
    candles = [PriceCandle(0, 1, 999, 0, 1),
               PriceCandle(60000, 100, 120, 80, 110),
               PriceCandle(120000, 110, 115, 70, 100)]
    high, low = visible_extremes(candles, 60000, 120000)
    assert high.timestamp == 60000 and high.high == 120
    assert low.timestamp == 120000 and low.low == 70
    assert visible_extremes(candles, 180000, 240000) is None
    assert format_price(Decimal('83079.900')) == '83,079.9'
    assert format_price(Decimal('0.000000123')) == '0.000000123'


def test_live_price_annotation_updates_before_sampling_and_stays_live_when_panned(qtbot):
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.resize(800, 330)
    chart.plot.price_points = [PricePoint(chart.plot.now - 60000, Decimal('82000'))]
    assert chart.plot.latest_price == Decimal('82000')
    chart.set_price('82112.0123')
    assert chart.plot.latest_price == Decimal('82112.0123')
    assert len(chart.plot.price_points) == 1
    chart.plot.view_end = chart.plot.now - 3600000
    assert chart.plot.latest_price == Decimal('82112.0123')
    chart.set_price('NaN')
    assert chart.plot.latest_price == Decimal('82112.0123')
    assert not chart.plot.grab().isNull()
    assert chart.shutdown()


@pytest.mark.parametrize('value,step,expected', [
    (82137.923456, 43.211, '82,137.9'),
    (163.472222, 0.8, '163.5'),
    (0.001234567, 0.0002, '0.0012'),
    (82600.1234, 0.01, '82,600.12'),
])
def test_price_axis_labels_use_compact_scale_aware_precision(value, step, expected):
    from okx_gui.funding_chart import axis_price_label
    assert axis_price_label(value, step) == expected


def test_default_price_view_is_last_hour_even_with_seven_days_of_cache(qtbot):
    from okx_gui.funding_chart import FundingChart
    now = int(time.time() * 1000)
    PriceStore().save('BTC-USDT-SWAP', [
        PricePoint(now - RETENTION_MS + 60000, Decimal('80000')),
        PricePoint(now - 1000, Decimal('82000')),
    ], now)
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    assert len(chart.plot.price_points) == 2
    assert chart.plot.span == 3600000
    assert chart.plot.end_time == chart.plot.now
    chart.advance_time()
    assert chart.plot.span == 3600000
    chart.plot.manual_span = 7200000
    chart.refresh()
    assert chart.plot.span == 7200000
    assert chart.shutdown()


def test_future_pan_resumes_follow_when_new_prices_reach_right_edge(qtbot, monkeypatch):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from okx_gui.funding_chart import FundingChart
    chart = FundingChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.resize(800, 330)
    chart.show()
    plot = chart.plot
    plot.grab()
    origin = plot.plot_rect.center()
    target = origin - QPointF(plot.plot_rect.width() / 4, 0)
    def mouse(kind, position, button, buttons):
        QApplication.sendEvent(plot, QMouseEvent(kind, position, position, button, buttons, Qt.NoModifier))
    mouse(QEvent.MouseButtonPress, origin, Qt.LeftButton, Qt.LeftButton)
    mouse(QEvent.MouseMove, target, Qt.NoButton, Qt.LeftButton)
    mouse(QEvent.MouseButtonRelease, target, Qt.LeftButton, Qt.NoButton)
    edge = plot.view_end
    assert edge > plot.now and plot.resume_follow_at == edge
    span = plot.span
    offsets = dict(plot.y_offsets)
    monkeypatch.setattr('okx_gui.funding_chart.time.time', lambda: (edge + 1000) / 1000)
    chart.append_price(PricePoint(int(edge - 1), Decimal('82000')))
    assert plot.view_end == edge  # Wall clock alone must not consume empty space.
    chart.append_price(PricePoint(int(edge + 1), Decimal('82001')))
    assert plot.view_end is None and plot.resume_follow_at is None
    assert plot.end_time == plot.now and plot.span == span and plot.y_offsets == offsets
    monkeypatch.setattr('okx_gui.funding_chart.time.time', lambda: (edge + 2000) / 1000)
    chart.advance_time()
    assert plot.end_time == int(edge + 2000)
    assert chart.shutdown()


def test_auto_follow_does_not_interrupt_drag_or_history_view(qtbot):
    from okx_gui.funding_chart import FundingPlot
    plot = FundingPlot()
    qtbot.addWidget(plot)
    plot.view_end = plot.now - 60000
    plot.price_points = [PricePoint(plot.now, Decimal('82000'))]
    plot.resume_follow_if_ready()
    assert plot.view_end == plot.now - 60000
    plot.resume_follow_at = plot.now
    plot._drag = object()
    plot.resume_follow_if_ready()
    assert plot.view_end is not None
    plot._drag = None
    plot.resume_follow_if_ready()
    assert plot.view_end is None
