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


def test_book_worker_subscribes_and_coalesces_latest_snapshot(monkeypatch):
    from okx_gui import order_book as module
    worker = OrderBookWorker(INST)
    sent, incoming = [], [message(), message('update', 11, 10, bids=[['100', '7']], asks=[])]
    stopped = [False]
    monkeypatch.setattr(worker, 'isInterruptionRequested', lambda: stopped[0])
    class Socket:
        async def send(self, value): sent.append(json.loads(value))
        async def recv(self):
            value = incoming.pop(0)
            if not incoming:
                stopped[0] = True
            return json.dumps(value)
    class Connection:
        async def __aenter__(self): return Socket()
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(module, 'connect', lambda *args, **kwargs: Connection())
    asyncio.run(worker._run())
    assert sent == [{'op': 'subscribe', 'args': [{'channel': 'books', 'instId': INST}]}]
    assert worker.take_snapshot().bids[0] == (D(100), D(7))
    assert worker.take_snapshot() is None
