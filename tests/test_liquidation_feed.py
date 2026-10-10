import asyncio
import json
import time

import pytest

from okx_gui.liquidation_feed import LiquidationWorker, binance_symbol, parse_liquidations
from okx_gui.liquidation_store import LiquidationRecord, LiquidationStore


def okx_message(timestamp):
    return {'arg': {'channel': 'liquidation-orders'}, 'data': [{'instId': 'BTC-USDT-SWAP', 'details': [
        {'ts': str(timestamp), 'posSide': 'long', 'side': 'sell', 'sz': '3'},
        {'ts': str(timestamp), 'posSide': 'short', 'side': 'buy', 'sz': '2'}]}]}


def test_okx_parses_each_detail_and_ignores_other_contracts_and_invalid_records():
    message = okx_message(1800000000000)
    message['data'].append({'instId': 'ETH-USDT-SWAP', 'details': message['data'][0]['details']})
    message['data'][0]['details'].append({'ts': 'bad', 'posSide': 'long'})
    records = parse_liquidations(message, 'okx', 'BTC-USDT-SWAP')
    assert [(r.exchange, r.side, r.timestamp) for r in records] == [
        ('okx', 'long', 1800000000), ('okx', 'short', 1800000000)]
    assert records == parse_liquidations(message, 'okx', 'BTC-USDT-SWAP')
    assert parse_liquidations({'event': 'subscribe'}, 'okx', 'BTC-USDT-SWAP') == []


@pytest.mark.parametrize('direction,expected', [('SELL', 'long'), ('BUY', 'short')])
def test_binance_order_side_is_reversed_to_liquidated_position(direction, expected):
    message = {'e': 'forceOrder', 'st': 1, 'o': {'s': 'BTCUSDT', 'S': direction, 'T': 1800000000000}}
    record = parse_liquidations({'data': message}, 'binance', 'BTC-USDT-SWAP')[0]
    assert (record.side, record.exchange) == (expected, 'binance')
    assert parse_liquidations(message, 'binance', 'ETH-USDT-SWAP') == []
    message['st'] = 2
    assert parse_liquidations(message, 'binance', 'BTC-USDT-SWAP') == []


def test_contract_mapping_does_not_guess_different_assets():
    assert binance_symbol('BTC-USDT-SWAP') == 'BTCUSDT'
    assert binance_symbol('BTC-USD-SWAP') is None
    assert binance_symbol('BTC-USDT-261225') is None


def test_worker_subscribes_and_persists_repeated_snapshots_only_once(qtbot, tmp_path, monkeypatch):
    from okx_gui import liquidation_feed as feed
    timestamp = int(time.time() * 1000)
    messages = iter([json.dumps({'event': 'subscribe'}), json.dumps(okx_message(timestamp)),
                     json.dumps(okx_message(timestamp))])
    sent = []
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
    monkeypatch.setattr(feed, 'connect', lambda *args, **kwargs: Socket())
    path = tmp_path / 'live.sqlite3'
    worker = LiquidationWorker('BTC-USDT-SWAP', path)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._platform('okx'))
    assert sent == [{'op': 'subscribe', 'args': [{'channel': 'liquidation-orders', 'instType': 'SWAP'}]}]
    assert len(LiquidationStore(path).load('BTC-USDT-SWAP', 'live', time.time())) == 2
    assert LiquidationStore(path).load('BTC-USDT-SWAP', 'simulation', time.time()) is None


def test_live_chart_only_displays_live_history(qtbot):
    from okx_gui.liquidation_chart import LiquidationChart
    now = time.time()
    store = LiquidationStore()
    store.save('BTC-USDT-SWAP', 'simulation', [LiquidationRecord(now, 'long')], now)
    chart = LiquidationChart('BTC-USDT-SWAP')
    qtbot.addWidget(chart)
    chart.timer.stop()
    assert sum(b.long_count for b in chart.plot.buckets) == 0
    store.save('BTC-USDT-SWAP', 'live', [LiquidationRecord(now, 'short', exchange='binance')], now)
    chart.refresh()
    assert sum(b.binance_short for b in chart.plot.buckets) == 1
    chart.interval.setCurrentIndex(2)
    assert sum(b.binance_short for b in chart.plot.buckets) == 1
    assert chart.shutdown()


def test_binance_rest_restriction_does_not_block_websocket_records(qtbot, tmp_path, monkeypatch):
    import httpx
    from okx_gui import liquidation_feed as feed
    timestamp = int(time.time() * 1000)
    message = {'e': 'forceOrder', 'o': {'s': 'BTCUSDT', 'S': 'SELL', 'T': timestamp}}
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, *args):
            raise httpx.ConnectError('REST unavailable')
    class Socket:
        received = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def recv(self):
            if self.received:
                raise asyncio.CancelledError
            self.received = True
            return json.dumps(message)
    monkeypatch.setattr(feed.httpx, 'AsyncClient', lambda **kwargs: Client())
    monkeypatch.setattr(feed, 'connect', lambda *args, **kwargs: Socket())
    path = tmp_path / 'live.sqlite3'
    worker = LiquidationWorker('BTC-USDT-SWAP', path)
    statuses = []
    worker.status.connect(lambda exchange, status: statuses.append(status))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._platform('binance'))
    assert any('合约未核实' in status for status in statuses)
    records = LiquidationStore(path).load('BTC-USDT-SWAP', 'live', time.time())
    assert len(records) == 1
    assert (records[0].exchange, records[0].side) == ('binance', 'long')


def test_store_visible_window_does_not_load_older_history(tmp_path):
    now = time.time()
    store = LiquidationStore(tmp_path / 'range.sqlite3')
    recent = LiquidationRecord(now - 10, 'long')
    store.save('BTC', 'live', [recent, LiquidationRecord(now - 3600, 'short')], now)
    assert store.load('BTC', 'live', now, since=now - 60) == [recent]
