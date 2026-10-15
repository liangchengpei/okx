"""Public OKX books snapshots and sequenced incremental depth."""
import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
import threading
import time

from PySide6.QtCore import QThread, Signal
from websockets import connect

from okx_gui.market import PUBLIC_URL


@dataclass(frozen=True)
class BookSnapshot:
    timestamp: int
    bids: tuple
    asks: tuple


class BookSyncError(ValueError):
    pass


class OrderBook:
    def __init__(self):
        self.bids, self.asks = {}, {}
        self.sequence = None

    def apply(self, payload, instrument):
        if not isinstance(payload, dict):
            return None
        arg = payload.get('arg')
        if not isinstance(arg, dict) or arg.get('channel') != 'books' or arg.get('instId') != instrument:
            return None
        action = payload.get('action')
        if action not in ('snapshot', 'update'):
            return None
        rows = payload.get('data')
        if not isinstance(rows, list) or not rows:
            raise BookSyncError('订单本数据不完整')
        result = None
        for row in rows:
            try:
                seq, previous, timestamp = int(row['seqId']), int(row['prevSeqId']), int(row['ts'])
                if timestamp <= 0:
                    raise ValueError('无效时间')
                if action == 'update' and (self.sequence is None or previous != self.sequence):
                    raise BookSyncError('订单本序列不连续，重新获取快照')
                bids = {} if action == 'snapshot' else self.bids.copy()
                asks = {} if action == 'snapshot' else self.asks.copy()
                for field, book in (('bids', bids), ('asks', asks)):
                    if not isinstance(row[field], list):
                        raise ValueError('无效档位')
                    for level in row[field]:
                        price, quantity = Decimal(level[0]), Decimal(level[1])
                        if not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity < 0:
                            raise ValueError('无效价格或数量')
                        if quantity == 0:
                            book.pop(price, None)
                        else:
                            book[price] = quantity  # Absolute quantity, never an increment.
                bids = dict(sorted(bids.items(), reverse=True)[:400])
                asks = dict(sorted(asks.items())[:400])
                # checksum is deprecated by OKX; seqId/prevSeqId establish continuity.
                self.bids, self.asks, self.sequence = bids, asks, seq
                result = BookSnapshot(timestamp, tuple(bids.items()), tuple(asks.items()))
            except (KeyError, TypeError, ValueError, InvalidOperation, IndexError) as exc:
                raise BookSyncError(str(exc)) from exc
        return result


def aggregate_depth(levels, step, side):
    step = Decimal(str(step))
    if step not in (Decimal('0.1'), Decimal(1), Decimal(10), Decimal(50)):
        raise ValueError('订单本聚合只支持0.1、1、10、50')
    if side not in ('buy', 'sell'):
        raise ValueError('无效方向')
    rounding = ROUND_FLOOR if side == 'buy' else ROUND_CEILING
    totals = {}
    for price, quantity in levels:
        bucket = (price / step).to_integral_value(rounding=rounding) * step
        totals[bucket] = totals.get(bucket, Decimal(0)) + quantity
    return tuple(sorted(totals.items()))


class OrderBookWorker(QThread):
    status = Signal(str)

    def __init__(self, instrument, parent=None):
        super().__init__(parent)
        self.instrument = instrument
        self._lock = threading.Lock()
        self._loop = self._task = None
        self._snapshot = None

    def take_snapshot(self):
        with self._lock:
            snapshot, self._snapshot = self._snapshot, None
        return snapshot

    def stop(self):
        self.requestInterruption()
        with self._lock:
            if self._loop and self._task:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except RuntimeError:
                    pass

    def run(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        finally:
            with self._lock:
                self._loop = self._task = None

    async def _run(self):
        with self._lock:
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        while not self.isInterruptionRequested():
            book = OrderBook()
            try:
                self.status.emit('OKX：连接中')
                async with connect(PUBLIC_URL, open_timeout=10, close_timeout=1, ping_interval=None) as socket:
                    await socket.send(json.dumps({'op': 'subscribe', 'args': [
                        {'channel': 'books', 'instId': self.instrument}]}))
                    last_message = time.monotonic()
                    snapshot_deadline = last_message + 10
                    ping_sent = False
                    while not self.isInterruptionRequested():
                        now = time.monotonic()
                        if book.sequence is None and now > snapshot_deadline:
                            raise TimeoutError('等待订单本快照超时')
                        if now - last_message > 30:
                            raise TimeoutError('订单本心跳超时')
                        if now - last_message > 20 and not ping_sent:
                            await socket.send('ping')
                            ping_sent = True
                        try:
                            message = await asyncio.wait_for(socket.recv(), 0.5)
                        except asyncio.TimeoutError:
                            continue
                        last_message = time.monotonic()
                        ping_sent = False
                        if message == 'pong':
                            continue
                        payload = json.loads(message)
                        if isinstance(payload, dict) and payload.get('event') == 'error':
                            self.status.emit('OKX：订阅失败 · ' + str(payload.get('msg', '未知错误'))[:100])
                            return
                        first = book.sequence is None
                        snapshot = book.apply(payload, self.instrument)
                        if snapshot is not None:
                            with self._lock:
                                self._snapshot = snapshot  # Coalesce; never flood the GUI event queue.
                            if first:
                                self.status.emit('OKX：实时订单本 · 每侧最多400档')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.emit(f'OKX：重连中（旧数据暂停更新）· {str(exc)[:80]}')
                await asyncio.sleep(3)
