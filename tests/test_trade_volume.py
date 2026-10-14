import asyncio
from decimal import Decimal
import json
import time

import pytest

from okx_gui.trade_volume_feed import parse_trades, TradeVolumeWorker
from okx_gui.trade_volume_store import Trade, TradeVolumeStore, HISTORY_SECONDS
from okx_gui.trade_volume_chart import TradeVolumeChart, BUY_COLOR, SELL_COLOR

INST = 'BTC-USDT-SWAP'


def payload(**changes):
    row = dict(instId=INST, tradeId='42', side='buy', sz='0.12', px='82000', ts='1800000000000', count='50')
    row.update(changes)
    return dict(arg=dict(channel='trades', instId=INST), data=[row])


def test_parser_uses_taker_side_and_aggregated_size_without_count_multiplier():
    trades = parse_trades(payload(), INST)
    assert trades == [Trade(1800000000, '42', 'buy', Decimal('0.12'), Decimal('82000'))]
    assert parse_trades(payload(side='sell'), INST)[0].side == 'sell'
    assert not parse_trades(payload(), 'SPCX-USDT-SWAP')


@pytest.mark.parametrize('changes', [
    {'sz': 'NaN'}, {'sz': 'Infinity'}, {'sz': '-1'}, {'sz': '0'}, {'px': 'bad'},
    {'side': 'long'}, {'instId': 'ETH-USDT-SWAP'}, {'ts': 'nan'}, {'tradeId': ''},
])
def test_parser_rejects_invalid_rows(changes):
    assert parse_trades(payload(**changes), INST) == []


@pytest.mark.parametrize('minutes', [1, 5, 15])
def test_exact_volume_dedup_restart_and_bucket_alignment(tmp_path, minutes):
    store = TradeVolumeStore(tmp_path / 'volume.db')
    now = 1800000900
    trades = [Trade(now - 1, '1', 'buy', Decimal('0.1'), Decimal(82000)),
              Trade(now, '2', 'buy', Decimal('0.2'), Decimal(82000)),
              Trade(now, '3', 'sell', Decimal('5.5'), Decimal(82000))]
    store.save(INST, trades + trades, now)
    store = TradeVolumeStore(store.path)
    store.save(INST, trades, now)
    buckets = store.buckets(INST, minutes, now)
    assert all(b.start % (minutes * 60) == 0 for b in buckets)
    assert buckets[-2].buy == Decimal('0.1')
    assert buckets[-1].buy == Decimal('0.2')
    assert buckets[-1].sell == Decimal('5.5')
    assert sum(b.buy for b in buckets) == Decimal('0.3')
    assert sum(b.sell for b in store.buckets('OTHER', minutes, now)) == 0


def test_reaggregation_and_retention(tmp_path):
    store = TradeVolumeStore(tmp_path / 'volume.db')
    now = 1800000899
    trades = [Trade(now - i * 60, str(i), 'buy', Decimal('0.1'), Decimal(1)) for i in range(15)]
    store.save(INST, trades, now)
    assert sum(b.buy for b in store.buckets(INST, 1, now)) == Decimal('1.5')
    assert sum(b.buy for b in store.buckets(INST, 5, now)[-3:]) == Decimal('1.5')
    assert store.buckets(INST, 15, now)[-1].buy == Decimal('1.5')
    store.save(INST, [], now + HISTORY_SECONDS + 120)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM minutes').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM seen').fetchone()[0] == 0


def test_chart_period_zoom_and_decimal_bars(qtbot, tmp_path):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    store = TradeVolumeStore(tmp_path / 'volume.db')
    now = time.time()
    store.save(INST, [Trade(now, 'a', 'buy', Decimal('0.125'), Decimal(82000)),
                     Trade(now, 'b', 'sell', Decimal('0.05'), Decimal(82000))], now)
    chart = TradeVolumeChart(INST, store=store, start_worker=False)
    qtbot.addWidget(chart)
    chart.resize(700, 320)
    chart.show()
    assert chart.timer.interval() == 1000
    chart.timer.stop()
    assert BUY_COLOR != SELL_COLOR
    for index, minutes in enumerate((1, 5, 15)):
        chart.interval.setCurrentIndex(index)
        assert chart.plot.minutes == minutes
        assert chart.plot.buckets[-1].buy == Decimal('0.125')
        assert '0.125 张' in chart.details.text()
    assert chart.plot.grab().width() > 0
    position = QPointF(chart.plot.rect().center())
    event = QWheelEvent(position, position, QPoint(), QPoint(0, -120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(chart.plot, event)
    assert event.isAccepted() and len(chart.plot.buckets) > 44
    assert chart.plot.buckets[-1].sell == Decimal('0.05')
    assert chart.shutdown()


def test_worker_subscribes_public_trades_and_flushes_on_stop(monkeypatch, tmp_path):
    from okx_gui import trade_volume_feed as feed
    path = tmp_path / 'volume.db'
    worker = TradeVolumeWorker(INST, path)
    sent = []
    stopped = [False]
    monkeypatch.setattr(worker, 'isInterruptionRequested', lambda: stopped[0])
    class Socket:
        async def send(self, message):
            sent.append(message)
        async def recv(self):
            stopped[0] = True
            return json.dumps(payload(ts=str(int(time.time() * 1000))))
    class Connection:
        async def __aenter__(self): return Socket()
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(feed, 'connect', lambda *args, **kwargs: Connection())
    asyncio.run(worker._run())
    assert json.loads(sent[0]) == {'op': 'subscribe', 'args': [{'channel': 'trades', 'instId': INST}]}
    assert TradeVolumeStore(path).buckets(INST, 1, time.time())[-1].buy == Decimal('0.12')
