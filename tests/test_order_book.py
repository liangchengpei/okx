import asyncio
from decimal import Decimal as D
import json

import pytest

from okx_gui.order_book import OrderBook, BookSnapshot, BookSyncError, aggregate_depth, OrderBookWorker

INST = 'BTC-USDT-SWAP'


def message(action='snapshot', seq=10, prev=-1, bids=None, asks=None):
    return {'arg': {'channel': 'books', 'instId': INST}, 'action': action,
            'data': [{'seqId': seq, 'prevSeqId': prev, 'ts': '1800000000000', 'checksum': 0,
                      'bids': bids if bids is not None else [['100', '2', '0', '3'], ['99', '1', '0', '1']],
                      'asks': asks if asks is not None else [['101', '3', '0', '2']]}]}


def test_snapshot_and_increments_replace_quantities_delete_and_sort():
    book = OrderBook()
    assert book.apply(message(), INST).bids[0] == (D(100), D(2))
    result = book.apply(message('update', 11, 10, bids=[['100', '5'], ['99', '0'], ['98', '4']], asks=[]), INST)
    assert result.bids == ((D(100), D(5)), (D(98), D(4)))
    assert result.asks == ((D(101), D(3)),)
    assert book.apply(message('update', 11, 11, bids=[], asks=[]), INST) == result
    # OKX maintenance can reset sequence numbers while preserving continuity.
    book.apply(message('update', 2, 11, bids=[], asks=[]), INST)
    assert book.sequence == 2
    book.apply(message(seq=20, bids=[['90', '1']], asks=[]), INST)
    assert book.bids == {D(90): D(1)}


def test_wrong_instrument_and_sequence_gaps_never_apply():
    book = OrderBook()
    assert book.apply(message(), 'OTHER') is None
    with pytest.raises(BookSyncError):
        book.apply(message('update', 11, 10), INST)
    book.apply(message(), INST)
    before = book.bids.copy()
    with pytest.raises(BookSyncError):
        book.apply(message('update', 12, 9, bids=[['100', '99']]), INST)
    assert book.bids == before and book.sequence == 10


@pytest.mark.parametrize('level', [['100', '-1'], ['NaN', '2'], ['100', 'Infinity'], ['0', '2'], ['bad']])
def test_invalid_level_rejected_atomically(level):
    book = OrderBook()
    book.apply(message(), INST)
    with pytest.raises(BookSyncError):
        book.apply(message('update', 11, 10, bids=[level]), INST)
    assert book.sequence == 10 and book.bids[D(100)] == 2


@pytest.mark.parametrize('step', ['0.1', '1', '10', '50'])
def test_price_aggregation_preserves_quantity_and_outward_rounding(step):
    levels = ((D('100.01'), D('1.2')), (D('100.09'), D('2.3')), (D('150'), D('0.5')))
    for side in ('buy', 'sell'):
        result = aggregate_depth(levels, step, side)
        assert sum(q for _, q in result) == D(4)
        assert all(price % D(step) == 0 for price, _ in result)
    assert aggregate_depth(levels[:2], '0.1', 'buy') == ((D('100.0'), D('3.5')),)
    assert aggregate_depth(levels[:2], '0.1', 'sell') == ((D('100.1'), D('3.5')),)


def test_depth_is_bounded_to_400_levels():
    book = OrderBook()
    snapshot = book.apply(message(bids=[[str(i), '1'] for i in range(1, 502)], asks=[]), INST)
    assert len(snapshot.bids) == 400 and snapshot.bids[0][0] == 501


def test_chart_aggregation_pan_zoom_and_live_refresh(qtbot):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication
    from okx_gui.order_book_chart import OrderBookChart
    chart = OrderBookChart(INST)
    qtbot.addWidget(chart)
    chart.resize(800, 350)
    chart.show()
    snapshot = OrderBook().apply(message(), INST)
    chart.worker._snapshot = snapshot
    chart.refresh()
    plot = chart.plot
    plot.grab()
    assert chart.aggregation.count() == 4 and plot.snapshot == snapshot
    assert '买一 100' in chart.details.text()
    for i, step in enumerate(('0.1', '1', '10', '50')):
        chart.aggregation.setCurrentIndex(i)
        assert plot.step == D(step)
        assert sum(q for _, q in plot.bids) == 3
    chart.aggregation.setCurrentIndex(0)
    origin = plot.plot_rect.center()
    target = origin + QPointF(40, 20)
    def mouse(kind, position, button, buttons):
        QApplication.sendEvent(plot, QMouseEvent(kind, position, position, button, buttons, Qt.NoModifier))
    original_center, original_span = plot.view()
    mouse(QEvent.MouseButtonPress, origin, Qt.LeftButton, Qt.LeftButton)
    mouse(QEvent.MouseMove, target, Qt.NoButton, Qt.LeftButton)
    mouse(QEvent.MouseButtonRelease, target, Qt.LeftButton, Qt.NoButton)
    assert plot.view()[0] < original_center and plot.pan_y == 20
    event = QWheelEvent(origin, origin, QPoint(), QPoint(0, 120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(plot, event)
    assert event.isAccepted() and plot.view()[1] < original_span
    fixed = plot.view()
    plot.set_snapshot(BookSnapshot(snapshot.timestamp + 100, ((D(105), D(9)),), ((D(106), D(5)),)))
    assert plot.view() == fixed
    assert not plot.grab().isNull()
    mouse(QEvent.MouseButtonDblClick, origin, Qt.LeftButton, Qt.LeftButton)
    assert plot.manual_center is None and plot.manual_span is None and plot.pan_y == 0
    assert chart.shutdown()



def full_payload(bids=None, asks=None, timestamp=1800000000000):
    return {'code': '0', 'data': [{'ts': str(timestamp),
        'bids': bids if bids is not None else [[str(10000-i), '1', '1'] for i in range(800)],
        'asks': asks if asks is not None else [[str(10001+i), '2', '1'] for i in range(800)]}]}


def test_full_snapshot_returns_800_levels_and_keeps_far_depth():
    from okx_gui.order_book import parse_full_book
    snapshot = parse_full_book(full_payload())
    assert len(snapshot.bids) == len(snapshot.asks) == 800
    assert snapshot.bids[-1][0] == 9201
    assert snapshot.asks[-1][0] == 10800
    assert sum(q for _, q in aggregate_depth(snapshot.bids, '50', 'buy')) == 800


def test_short_full_snapshot_does_not_pad_or_keep_deleted_levels():
    from okx_gui.order_book import parse_full_book
    snapshot = parse_full_book(full_payload(bids=[['100', '3', '1']], asks=[]))
    assert snapshot.bids == ((D(100), D(3)),) and snapshot.asks == ()


@pytest.mark.parametrize('payload', [
    {'code': '50011', 'msg': 'Rate limit reached'},
    {'code': '0', 'data': []},
    full_payload(bids=[['NaN', '1']]),
    full_payload(asks=[['101', '-2']]),
])
def test_bad_full_snapshots_are_rejected(payload):
    from okx_gui.order_book import parse_full_book
    with pytest.raises(BookSyncError):
        parse_full_book(payload)


def test_book_worker_requests_800_and_replaces_each_snapshot(monkeypatch):
    from okx_gui import order_book as module
    worker = OrderBookWorker(INST)
    calls, snapshots, delays = [], [], []
    responses = [full_payload(), full_payload(bids=[['100', '7']], asks=[], timestamp=1800000001000)]
    stopped = [False]
    monkeypatch.setattr(worker, 'isInterruptionRequested', lambda: stopped[0])
    class Response:
        def __init__(self, rules=False): self.rules = rules
        def raise_for_status(self): pass
        def json(self):
            if self.rules:
                return {'code': '0', 'data': [{'instId': INST, 'ctType': 'linear', 'ctValCcy': 'BTC', 'ctVal': '0.01', 'ctMult': '1'}]}
            return responses.pop(0)
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params):
            calls.append((url, params))
            return Response(url == module.INSTRUMENTS_URL)
    async def wait(): pass
    async def sleep(delay):
        snapshots.append(worker.take_snapshot())
        delays.append(delay)
        stopped[0] = not responses
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: Client())
    monkeypatch.setattr(module.DEPTH_LIMITER, 'wait', wait)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    asyncio.run(worker._run())
    assert calls == [(module.INSTRUMENTS_URL, {'instType': 'SWAP', 'instId': INST})] + [(module.FULL_BOOK_URL, {'instId': INST, 'sz': '800'})] * 2
    assert len(snapshots[0].bids) == 800
    assert snapshots[0].unit == "BTC"
    assert snapshots[1].bids == ((D(100), D('0.07')),) and snapshots[1].asks == ()
    assert all(0 <= delay <= 1 for delay in delays)
    assert worker.take_snapshot() is None


def test_full_snapshot_limiter_spaces_requests(monkeypatch):
    from okx_gui import order_book as module
    limiter = module.DepthRequestLimiter()
    clock, times = [0.0], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    async def sleep(delay): clock[0] += delay
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    async def run():
        for _ in range(12):
            await limiter.wait()
            times.append(clock[0])
    asyncio.run(run())
    assert all(b-a >= 0.25 for a, b in zip(times, times[1:]))


@pytest.mark.parametrize('face,multiplier,expected', [('0.01', '1', '0.02'), ('0.1', '10', '2')])
def test_coin_depth_uses_live_face_value_and_multiplier(face, multiplier, expected):
    from okx_gui.order_book import contract_coin_factor, coin_snapshot
    rule = {'instId': INST, 'ctType': 'linear', 'ctValCcy': 'BTC', 'ctVal': face, 'ctMult': multiplier}
    factor, unit = contract_coin_factor({'code': '0', 'data': [rule]}, INST)
    raw = BookSnapshot(1000, ((D(100), D(2)),), ((D(101), D(3)),))
    converted = coin_snapshot(raw, factor, unit)
    assert converted.bids[0][1] == D(expected)
    assert converted.asks[0][1] == D(expected) * D('1.5')
    assert raw.unit == '张' and converted.unit == 'BTC'


@pytest.mark.parametrize('changes', [{'ctVal': '0'}, {'ctVal': 'NaN'}, {'ctValCcy': 'USD'}, {'instId': 'OTHER'}])
def test_bad_contract_metadata_cannot_be_labeled_as_coins(changes):
    from okx_gui.order_book import contract_coin_factor
    rule = {'instId': INST, 'ctType': 'linear', 'ctValCcy': 'BTC', 'ctVal': '0.01', 'ctMult': '1', **changes}
    with pytest.raises(BookSyncError):
        contract_coin_factor({'code': '0', 'data': [rule]}, INST)
