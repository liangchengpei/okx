import asyncio
import json
import time

import pytest

from okx_gui.liquidation_feed import LiquidationWorker, bybit_symbol, parse_liquidations
from okx_gui.liquidation_store import LiquidationRecord, LiquidationStore


def bybit_message(timestamp):
    return {'topic': 'allLiquidation.BTCUSDT', 'type': 'snapshot', 'ts': timestamp, 'data': [
        {'T': timestamp, 's': 'BTCUSDT', 'S': 'Buy', 'v': '3', 'p': '80000'},
        {'T': timestamp, 's': 'BTCUSDT', 'S': 'Sell', 'v': '2', 'p': '90000'}]}


def test_bybit_parses_all_details_with_position_side_and_stable_ids():
    message = bybit_message(1800000000000)
    message['data'] += [{'T': 'bad', 's': 'BTCUSDT', 'S': 'Buy'},
                        {'T': 1800000000000, 's': 'ETHUSDT', 'S': 'Buy'}]
    records = parse_liquidations(message, 'bybit', 'BTC-USDT-SWAP')
    assert [(r.exchange, r.side, r.timestamp) for r in records] == [
        ('bybit', 'long', 1800000000), ('bybit', 'short', 1800000000)]
    assert records == parse_liquidations(message, 'bybit', 'BTC-USDT-SWAP')
    assert parse_liquidations(message, 'okx', 'BTC-USDT-SWAP') == []
    assert parse_liquidations(message, 'binance', 'BTC-USDT-SWAP') == []
    assert parse_liquidations(message, 'bybit', 'ETH-USDT-SWAP') == []


def test_identical_details_in_one_batch_are_separate_records():
    message = bybit_message(1800000000000)
    message['data'] = [message['data'][0]] * 2
    records = parse_liquidations(message, 'bybit', 'BTC-USDT-SWAP')
    assert len(records) == 2
    assert records[0].event_id != records[1].event_id
    assert records == parse_liquidations(message, 'bybit', 'BTC-USDT-SWAP')


def test_contract_mapping_does_not_guess_other_products():
    assert bybit_symbol('BTC-USDT-SWAP') == 'BTCUSDT'
    assert bybit_symbol('BTC-USD-SWAP') is None
    assert bybit_symbol('BTC-USDC-SWAP') is None
    assert bybit_symbol('BTC-USDT-261225') is None


def test_worker_subscribes_only_bybit_and_persists_duplicate_batch_once(qtbot, tmp_path, monkeypatch):
    from okx_gui import liquidation_feed as feed
    timestamp = int(time.time() * 1000)
    messages = iter([json.dumps({'op': 'subscribe', 'success': True}),
                     json.dumps(bybit_message(timestamp)), json.dumps(bybit_message(timestamp))])
    sent, urls = [], []
    class Socket:
        async def send(self, message):
            sent.append(json.loads(message))
        async def recv(self):
            try:
                return next(messages)
            except StopIteration:
                raise asyncio.CancelledError
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    def connect(url, **kwargs):
        urls.append(url)
        return Socket()
    monkeypatch.setattr(feed, 'connect', connect)
    path = tmp_path / 'live.sqlite3'
    worker = LiquidationWorker('BTC-USDT-SWAP', path)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._run())
    assert urls == ['wss://stream.bybit.com/v5/public/linear']
    assert sent == [{'op': 'subscribe', 'args': ['allLiquidation.BTCUSDT']}]
    records = LiquidationStore(path).load('BTC-USDT-SWAP', 'live', time.time())
    assert len(records) == 2
    assert {record.exchange for record in records} == {'bybit'}


def test_live_chart_excludes_okx_binance_and_simulation(qtbot):
    from okx_gui.liquidation_chart import LiquidationChart
    now = time.time()
    store = LiquidationStore()
    store.save('BTC-USDT-SWAP', 'simulation', [LiquidationRecord(now, 'long', exchange='bybit')], now)
    store.save('BTC-USDT-SWAP', 'live', [LiquidationRecord(now, 'long', exchange='okx'),
                                       LiquidationRecord(now, 'long', exchange='binance')], now)
    chart = LiquidationChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.timer.stop()
    assert sum(b.long_count for b in chart.plot.buckets) == 0
    store.save('BTC-USDT-SWAP', 'live', [LiquidationRecord(now, 'short', exchange='bybit')], now)
    chart.refresh()
    assert sum(b.bybit_short for b in chart.plot.buckets) == 1
    assert all(b.okx_long == b.okx_short == 0 for b in chart.plot.buckets)
    chart.interval.setCurrentIndex(2)
    assert sum(b.bybit_short for b in chart.plot.buckets) == 1
    assert 'OKX：暂停' in chart.feed_status.text()
    assert chart.shutdown()


def test_subscription_rejection_is_visible_and_no_data_written(qtbot, tmp_path, monkeypatch):
    from okx_gui import liquidation_feed as feed
    class Socket:
        async def send(self, message):
            pass
        async def recv(self):
            return json.dumps({'op': 'subscribe', 'success': False, 'ret_msg': 'symbol not found'})
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(feed, 'connect', lambda *args, **kwargs: Socket())
    path = tmp_path / 'live.sqlite3'
    worker = LiquidationWorker('SPCX-USDT-SWAP', path)
    statuses = []
    worker.status.connect(lambda exchange, status: statuses.append(status))
    asyncio.run(worker._platform('bybit'))
    assert any('symbol not found' in status for status in statuses)
    assert LiquidationStore(path).load('SPCX-USDT-SWAP', 'live', time.time()) is None


def test_store_visible_window_does_not_load_older_history(tmp_path):
    now = time.time()
    store = LiquidationStore(tmp_path / 'range.sqlite3')
    recent = LiquidationRecord(now - 10, 'long', exchange='bybit')
    store.save('BTC', 'live', [recent, LiquidationRecord(now - 3600, 'short')], now)
    assert store.load('BTC', 'live', now, since=now - 60) == [recent]


def test_received_liquidation_preserves_quantity_and_bankruptcy_price(tmp_path):
    now = time.time()
    message = bybit_message(int(now * 1000))
    record = parse_liquidations(message, 'bybit', 'BTC-USDT-SWAP')[0]
    assert (record.quantity, record.price) == ('3', '80000')
    path = tmp_path / 'details.sqlite3'
    LiquidationStore(path).save('BTC-USDT-SWAP', 'live', [record], now)
    restored = LiquidationStore(path).latest('BTC-USDT-SWAP', 'live', 'bybit', now)
    assert restored == record


def test_top_left_details_show_latest_even_outside_visible_window_and_hover_counts(qtbot):
    from okx_gui.liquidation_chart import LiquidationChart
    now = time.time()
    record = LiquidationRecord(now - 2 * 86400, 'long', exchange='bybit', quantity='0.123', price='82600.05')
    LiquidationStore().save('BTC-USDT-SWAP', 'live', [record], now)
    chart = LiquidationChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.timer.stop()
    assert '多头强平' in chart.details.text()
    assert '0.123 BTC' in chart.details.text()
    assert '破产价：82600.05 USDT' in chart.details.text()
    chart.plot.bucket_hovered.emit(chart.plot.buckets[-1])
    assert '0.123 BTC' in chart.details.text()
    chart.plot.bucket_hovered.emit(None)
    assert '0.123 BTC' in chart.details.text()
    chart.shutdown()


def test_hover_never_hides_details_or_moves_plot(qtbot):
    from PySide6.QtCore import QPoint
    from okx_gui.liquidation_chart import LiquidationChart
    now = time.time()
    LiquidationStore().save('BTC-USDT-SWAP', 'live', [
        LiquidationRecord(now, 'long', exchange='bybit', quantity='0.123', price='82600.05')], now)
    chart = LiquidationChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.timer.stop()
    chart.resize(640, 360)
    chart.show()
    qtbot.wait(20)
    geometry = chart.plot.geometry()
    details = chart.details.text()
    for position in (QPoint(100, 60), QPoint(200, 100), QPoint(300, 80)):
        qtbot.mouseMove(chart.plot, position)
        chart.refresh()
        qtbot.wait(10)
        assert chart.details.text() == details
        assert chart.plot.geometry() == geometry
    qtbot.mouseMove(chart, QPoint(1, 1))
    assert chart.details.text() == details
    assert chart.plot.geometry() == geometry
    chart.shutdown()
