"""Public OKX trades feed; sizes already include exchange-side aggregation."""
import asyncio
import json
import math
import threading
import time
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThread, Signal
from websockets import connect

from okx_gui.market import PUBLIC_URL
from okx_gui.trade_volume_store import Trade, TradeVolumeStore


def parse_trades(payload, instrument):
    if not isinstance(payload, dict):
        return []
    arg = payload.get('arg')
    if not isinstance(arg, dict) or arg.get('channel') != 'trades' or arg.get('instId') != instrument:
        return []
    rows = payload.get('data')
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        try:
            if row.get('instId') != instrument or row.get('side') not in ('buy', 'sell'):
                continue
            quantity, price = Decimal(row['sz']), Decimal(row['px'])
            timestamp = float(row['ts']) / 1000
            identifier = row['tradeId']
            if (not isinstance(identifier, str) or not identifier or not math.isfinite(timestamp) or timestamp <= 0
                    or not quantity.is_finite() or quantity <= 0 or not price.is_finite() or price <= 0):
                continue
            # sz is already aggregated; count is NOT a quantity multiplier.
            result.append(Trade(timestamp, identifier, row['side'], quantity, price))
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError, OverflowError):
            continue
    return result


class TradeVolumeWorker(QThread):
    status = Signal(str)
    def __init__(self, instrument, database_path, parent=None):
        super().__init__(parent)
        self.instrument = instrument
        self.database_path = database_path
        self._lock = threading.Lock()
        self._loop = self._task = None

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
        store = TradeVolumeStore(self.database_path)
        pending = []
        last_flush = time.monotonic()
        while not self.isInterruptionRequested():
            try:
                self.status.emit('OKX：连接中')
                async with connect(PUBLIC_URL, open_timeout=10, close_timeout=1,
                                   ping_interval=None) as socket:
                    await socket.send(json.dumps({'op': 'subscribe', 'args': [
                        {'channel': 'trades', 'instId': self.instrument}]}))
                    last_message = time.monotonic()
                    subscribed = False
                    receiving = False
                    ping_sent = False
                    while not self.isInterruptionRequested():
                        now = time.monotonic()
                        if now - last_flush >= 1:
                            if pending:
                                store.save(self.instrument, pending, time.time())
                                pending.clear()
                            last_flush = now
                        if now - last_message > (30 if subscribed else 10):
                            raise TimeoutError('成交订阅或心跳超时')
                        if now - last_message > 20 and not ping_sent:
                            await socket.send('ping')
                            ping_sent = True
                        try:
                            message = await asyncio.wait_for(socket.recv(), 0.25)
                        except asyncio.TimeoutError:
                            continue
                        last_message = time.monotonic()
                        ping_sent = False
                        if message == 'pong':
                            continue
                        try:
                            payload = json.loads(message)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if payload.get('event') == 'error':
                            self.status.emit('OKX：订阅失败 · ' + str(payload.get('msg', '未知错误'))[:120])
                            return
                        if payload.get('event') == 'subscribe':
                            subscribed = True
                            self.status.emit('OKX：已订阅 · 等待成交')
                        trades = parse_trades(payload, self.instrument)
                        if trades:
                            if not subscribed:
                                subscribed = True
                            if not receiving:
                                receiving = True
                                self.status.emit('OKX：实时成交接收中')
                            pending.extend(trades)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.emit(f'OKX：重连中 · {str(exc)[:80]}')
                await asyncio.sleep(3)
            finally:
                if pending:
                    try:
                        store.save(self.instrument, pending, time.time())
                        pending.clear()
                    except Exception as exc:
                        self.status.emit(f'OKX：缓存写入失败 · {str(exc)[:80]}')
