"""Bybit public liquidation feed; OKX liquidation collection is paused."""
import asyncio
import hashlib
import json
import math
import threading
import time
from collections import Counter

from PySide6.QtCore import QThread, Signal
from websockets import connect

from okx_gui.liquidation_store import LiquidationRecord, LiquidationStore

BYBIT_URL = 'wss://stream.bybit.com/v5/public/linear'


def bybit_symbol(instrument):
    parts = instrument.split('-')
    # Only exact USDT perpetual matches; never guess multiplier or renamed tokens.
    if len(parts) == 3 and parts[2] == 'SWAP' and parts[1] == 'USDT':
        return parts[0] + parts[1]
    return None


def parse_liquidations(payload, exchange, instrument):
    symbol = bybit_symbol(instrument)
    if exchange != 'bybit' or not symbol or not isinstance(payload, dict):
        return []
    if payload.get('topic') != f'allLiquidation.{symbol}' or not isinstance(payload.get('data'), list):
        return []
    result, occurrences = [], Counter()
    for detail in payload['data']:
        try:
            if detail.get('s') != symbol:
                continue
            # Bybit S is POSITION side, unlike Binance's closing order side.
            side = {'Buy': 'long', 'Sell': 'short'}.get(detail.get('S'))
            timestamp = float(detail['T']) / 1000
            if side is None or not math.isfinite(timestamp) or timestamp <= 0:
                continue
            encoded = json.dumps(detail, sort_keys=True, separators=(',', ':'))
            occurrence = occurrences[encoded]
            occurrences[encoded] += 1
            identity = json.dumps([payload.get('ts'), encoded, occurrence])
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            result.append(LiquidationRecord(timestamp, side, fingerprint, 'bybit'))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    return result


class LiquidationWorker(QThread):
    status = Signal(str, str)

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
        await self._platform('bybit')

    async def _heartbeat(self, socket):
        while True:
            await asyncio.sleep(20)
            await socket.send(json.dumps({'op': 'ping'}))

    async def _platform(self, exchange):
        if exchange != 'bybit':
            return
        symbol = bybit_symbol(self.instrument)
        if symbol is None:
            self.status.emit(exchange, '不支持该合约（仅同名 USDT 永续）')
            return
        store = LiquidationStore(self.database_path)
        while not self.isInterruptionRequested():
            try:
                self.status.emit(exchange, '连接中')
                async with connect(BYBIT_URL, open_timeout=10, close_timeout=1,
                                   ping_interval=20, ping_timeout=20) as socket:
                    await socket.send(json.dumps({'op': 'subscribe', 'args': [f'allLiquidation.{symbol}']}))
                    acknowledgement = json.loads(await asyncio.wait_for(socket.recv(), 10))
                    if acknowledgement.get('op') != 'subscribe' or acknowledgement.get('success') is not True:
                        self.status.emit(exchange, f"订阅失败 · {str(acknowledgement.get('ret_msg', '未知响应'))[:100]}")
                        return
                    self.status.emit(exchange, '已订阅 · 等待记录')
                    heartbeat = asyncio.create_task(self._heartbeat(socket))
                    try:
                        while not self.isInterruptionRequested():
                            if heartbeat.done():
                                heartbeat.result()
                            try:
                                message = await asyncio.wait_for(socket.recv(), 30)
                            except asyncio.TimeoutError:
                                raise TimeoutError('Bybit 心跳超时')
                            try:
                                payload = json.loads(message)
                            except (ValueError, TypeError):
                                continue
                            records = parse_liquidations(payload, 'bybit', self.instrument)
                            if records:
                                await asyncio.to_thread(store.save, self.instrument, 'live', records, time.time())
                                self.status.emit(exchange, '接收中')
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.emit(exchange, f'重连中 · {str(exc)[:80]}')
                await asyncio.sleep(3)
