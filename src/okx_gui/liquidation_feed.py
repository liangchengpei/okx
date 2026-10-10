"""Public liquidation feeds. Counts represent received exchange snapshots."""
import asyncio
import hashlib
import json
import math
import threading
import time

import httpx
from PySide6.QtCore import QThread, Signal
from websockets import connect

from okx_gui.liquidation_store import LiquidationRecord, LiquidationStore

OKX_URL = 'wss://ws.okx.com/ws/v5/public'
BINANCE_URL = 'wss://fstream.binance.com/market/ws/!forceOrder@arr'


def binance_symbol(instrument):
    parts = instrument.split('-')
    if len(parts) == 3 and parts[2] == 'SWAP' and parts[1] in ('USDT', 'USDC'):
        return parts[0] + parts[1]
    return None


def record_from_detail(detail, exchange, side, timestamp):
    value = float(timestamp) / 1000
    if not math.isfinite(value) or value <= 0 or side not in ('long', 'short'):
        raise ValueError('Invalid liquidation record')
    fingerprint = hashlib.sha256(json.dumps(detail, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return LiquidationRecord(value, side, fingerprint, exchange)


def parse_liquidations(payload, exchange, instrument):
    result = []
    if not isinstance(payload, dict):
        return result
    if exchange == 'okx':
        if payload.get('arg', {}).get('channel') != 'liquidation-orders':
            return result
        for item in payload.get('data', []):
            if not isinstance(item, dict) or item.get('instId') != instrument:
                continue
            for detail in item.get('details', []):
                try:
                    side = detail.get('posSide')
                    if side not in ('long', 'short'):
                        side = {'sell': 'long', 'buy': 'short'}.get(detail.get('side'))
                    result.append(record_from_detail(detail, exchange, side, detail['ts']))
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
    else:
        payload = payload.get('data', payload)
        if not isinstance(payload, dict) or payload.get('e') != 'forceOrder':
            return result
        detail = payload.get('o', {})
        try:
            if detail.get('s') != binance_symbol(instrument) or str(payload.get('st', 1)) != '1':
                return result
            side = {'SELL': 'long', 'BUY': 'short'}.get(detail.get('S'))
            result.append(record_from_detail(detail, exchange, side, detail['T']))
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
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
        await asyncio.gather(self._platform('okx'), self._platform('binance'))

    async def _platform(self, exchange):
        store = LiquidationStore(self.database_path)
        while not self.isInterruptionRequested():
            try:
                self.status.emit(exchange, '连接中')
                verified = True
                if exchange == 'binance':
                    symbol = binance_symbol(self.instrument)
                    if symbol is None:
                        self.status.emit(exchange, '不支持该合约')
                        return
                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            response = await client.get('https://fapi.binance.com/fapi/v1/exchangeInfo')
                            response.raise_for_status()
                            listed = any(item.get('symbol') == symbol and item.get('contractType') == 'PERPETUAL'
                                         and item.get('status') == 'TRADING' for item in response.json()['symbols'])
                        if not listed:
                            self.status.emit(exchange, '未上市该合约')
                            return
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        verified = False  # Public websocket may work even when REST is region restricted.
                url = OKX_URL if exchange == 'okx' else BINANCE_URL
                async with connect(url, open_timeout=10, close_timeout=1, ping_interval=20, ping_timeout=20) as socket:
                    if exchange == 'okx':
                        inst_type = 'SWAP' if self.instrument.endswith('-SWAP') else 'FUTURES'
                        await socket.send(json.dumps({'op': 'subscribe', 'args': [
                            {'channel': 'liquidation-orders', 'instType': inst_type}]}))
                    else:
                        self.status.emit(exchange, '已连接 · 等待记录' if verified else '已连接 · 合约未核实（REST受限）')
                    while not self.isInterruptionRequested():
                        try:
                            message = await asyncio.wait_for(socket.recv(), 20 if exchange == 'okx' else 60)
                        except asyncio.TimeoutError:
                            if exchange == 'okx':
                                await socket.send('ping')
                                message = await asyncio.wait_for(socket.recv(), 10)
                            else:
                                continue
                        if message == 'pong':
                            continue
                        try:
                            payload = json.loads(message)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(payload, dict) and payload.get('event') == 'error':
                            raise RuntimeError(str(payload.get('msg', '订阅失败'))[:120])
                        if isinstance(payload, dict) and payload.get('event') == 'subscribe':
                            self.status.emit(exchange, '已订阅 · 等待记录')
                        records = parse_liquidations(payload, exchange, self.instrument)
                        if records:
                            await asyncio.to_thread(store.save, self.instrument, 'live', records, time.time())
                            self.status.emit(exchange, '接收中')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.emit(exchange, f'重连中 · {str(exc)[:80]}')
                await asyncio.sleep(3)
