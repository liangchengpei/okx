"""Public OKX full-depth snapshots and depth aggregation."""
import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import threading
import time

from PySide6.QtCore import QThread, Signal
import httpx

FULL_BOOK_URL = "https://www.okx.com/api/v5/market/books-full"
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
DEPTH = 800


@dataclass(frozen=True)
class BookSnapshot:
    timestamp: int
    bids: tuple
    asks: tuple
    unit: str = "张"


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


def parse_full_book(payload):
    if not isinstance(payload, dict) or payload.get('code') != '0':
        code = payload.get('code', '?') if isinstance(payload, dict) else '?'
        message = payload.get('msg', '无效响应') if isinstance(payload, dict) else '无效响应'
        raise BookSyncError(f'完整订单本失败（{code}）：{message}')
    try:
        rows = payload['data']
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('无效快照')
        row = rows[0]
        timestamp = int(row['ts'])
        if timestamp <= 0:
            raise ValueError('无效时间')
        sides = []
        for name in ('bids', 'asks'):
            if not isinstance(row[name], list):
                raise ValueError('无效档位')
            levels = {}
            for level in row[name]:
                price, quantity = Decimal(level[0]), Decimal(level[1])
                if not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity < 0:
                    raise ValueError('无效价格或数量')
                if price in levels:
                    raise ValueError('重复档位')
                if quantity:
                    levels[price] = quantity
            sides.append(tuple(sorted(levels.items(), reverse=name == 'bids')[:DEPTH]))
        return BookSnapshot(timestamp, *sides)
    except (KeyError, TypeError, ValueError, InvalidOperation, IndexError) as exc:
        raise BookSyncError(f'完整订单本数据不完整：{exc}') from exc


def contract_coin_factor(payload, instrument):
    try:
        if payload.get('code') != '0':
            raise ValueError(payload.get('msg', '读取合约信息失败'))
        rule = next(row for row in payload['data'] if row.get('instId') == instrument)
        base = instrument.split('-')[0]
        if rule.get('ctType') != 'linear' or rule.get('ctValCcy') != base:
            raise ValueError('该合约面值币种不支持直接换算，不能显示为币数')
        value, multiplier = Decimal(rule['ctVal']), Decimal(rule.get('ctMult') or '1')
        if not value.is_finite() or value <= 0 or not multiplier.is_finite() or multiplier <= 0:
            raise ValueError('合约面值或乘数无效')
        return value * multiplier, base
    except (KeyError, TypeError, ValueError, InvalidOperation, StopIteration, AttributeError) as exc:
        raise BookSyncError(f'币数换算失败：{exc}') from exc


def coin_snapshot(snapshot, factor, unit):
    return BookSnapshot(snapshot.timestamp,
                        tuple((price, size * factor) for price, size in snapshot.bids),
                        tuple((price, size * factor) for price, size in snapshot.asks), unit)


class DepthRequestLimiter:
    """Shared across worker threads; leave headroom below the IP quota."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last_request = float('-inf')

    async def wait(self):
        while True:
            with self.lock:
                delay = self.last_request + 0.25 - time.monotonic()
                if delay <= 0:
                    self.last_request = time.monotonic()
                    return
            await asyncio.sleep(delay)


DEPTH_LIMITER = DepthRequestLimiter()


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
        self.status.emit('OKX：读取800档完整订单本')
        last_timestamp = 0
        conversion = None
        rules_loaded = 0
        async with httpx.AsyncClient(timeout=8) as client:
            while not self.isInterruptionRequested():
                started = time.monotonic()
                delay = 1
                try:
                    if conversion is None or time.monotonic() - rules_loaded >= 3600:
                        rules = await client.get(INSTRUMENTS_URL, params={'instType': 'SWAP', 'instId': self.instrument})
                        rules.raise_for_status()
                        conversion = contract_coin_factor(rules.json(), self.instrument)
                        rules_loaded = time.monotonic()
                    await DEPTH_LIMITER.wait()
                    if self.isInterruptionRequested():
                        break
                    response = await client.get(FULL_BOOK_URL, params={'instId': self.instrument, 'sz': str(DEPTH)})
                    response.raise_for_status()
                    snapshot = coin_snapshot(parse_full_book(response.json()), *conversion)
                    if snapshot.timestamp < last_timestamp:
                        raise BookSyncError('收到过期快照，保留较新订单本')
                    last_timestamp = snapshot.timestamp
                    with self._lock:
                        self._snapshot = snapshot
                    self.status.emit(f'OKX：完整快照 · 买盘{len(snapshot.bids)}档 / 卖盘{len(snapshot.asks)}档 · 每秒刷新')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.status.emit(f'OKX：更新失败（保留旧数据）· {str(exc)[:100]}')
                    delay = 3
                await asyncio.sleep(max(0, delay - (time.monotonic() - started)))
