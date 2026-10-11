"""Seven-day persisted live funding-rate samples and background public REST polling."""
import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
import sqlite3
import threading
import time

import httpx
from PySide6.QtCore import QThread, Signal

from okx_gui.liquidation_store import default_database_path

RETENTION_MS = 7 * 24 * 60 * 60 * 1000
EXCHANGES = ('okx',)


@dataclass(frozen=True)
class FundingPoint:
    exchange: str
    timestamp: int
    rate: Decimal


class FundingStore:
    def __init__(self, path=None):
        self.path = path or default_database_path().with_name('funding_rates.sqlite3')
        from pathlib import Path
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS funding_samples (
                instrument TEXT NOT NULL, exchange TEXT NOT NULL, timestamp INTEGER NOT NULL,
                rate TEXT NOT NULL, PRIMARY KEY(instrument,exchange,timestamp))''')
            db.execute('CREATE INDEX IF NOT EXISTS funding_sample_timestamp ON funding_samples(timestamp)')

    def connect(self):
        from contextlib import contextmanager
        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=5)
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    def save(self, instrument, points, now):
        with self.connect() as db:
            db.executemany('''INSERT INTO funding_samples VALUES (?,?,?,?)
                ON CONFLICT(instrument,exchange,timestamp) DO UPDATE SET rate=excluded.rate''',
                [(instrument, p.exchange, p.timestamp, str(p.rate)) for p in points
                 if now - RETENTION_MS <= p.timestamp <= now and p.rate.is_finite()])
            db.execute('DELETE FROM funding_samples WHERE timestamp<?', (now - RETENTION_MS,))

    def load(self, instrument, now):
        with self.connect() as db:
            db.execute('DELETE FROM funding_samples WHERE timestamp<?', (now - RETENTION_MS,))
            rows = db.execute('''SELECT exchange,timestamp,rate FROM funding_samples
                WHERE instrument=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp''',
                (instrument, now - RETENTION_MS, now)).fetchall()
        return [FundingPoint(exchange, timestamp, Decimal(rate)) for exchange, timestamp, rate in rows]


async def fetch_current_funding(client, instrument):
    if not instrument.endswith('-SWAP'):
        raise ValueError('资金费率仅适用于永续合约')
    response = await client.get('https://www.okx.com/api/v5/public/funding-rate',
                                params={'instId': instrument})
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != '0':
        raise ValueError(payload.get('msg') or 'OKX 接口错误')
    for row in payload.get('data', []):
        if row.get('instId') != instrument:
            continue
        try:
            point = FundingPoint('okx', int(time.time() * 1000), Decimal(row['fundingRate']))
            if point.timestamp > 0 and point.rate.is_finite():
                return point
        except (KeyError, TypeError, ValueError, InvalidOperation):
            pass
    raise ValueError('OKX 未返回有效实时资金费率')



def funding_error_message(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        body = response.text.lower()
        if response.status_code == 451 or (response.status_code == 403 and
                any(text in body for text in ('country', 'restricted location', 'region'))):
            return f'网络出口地区受限（HTTP {response.status_code}），请检查代理设置'
        return f'接口返回 HTTP {response.status_code}'
    if isinstance(exc, httpx.TimeoutException):
        return '连接超时，请检查网络或代理'
    if isinstance(exc, httpx.ProxyError):
        return '代理连接失败，请检查代理地址和服务'
    if isinstance(exc, httpx.RequestError):
        return '网络连接失败，请检查网络或代理'
    return str(exc)[:100]


def funding_client_options():
    options = {'timeout': 12}
    proxy = os.environ.get('OKX_FUNDING_PROXY', '').strip()
    if proxy:
        options['proxy'] = proxy
    return options


class FundingWorker(QThread):
    status = Signal(str, str)
    updated = Signal()

    def __init__(self, instrument, database_path, parent=None):
        super().__init__(parent)
        self.instrument, self.database_path = instrument, database_path
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
        store = FundingStore(self.database_path)
        async with httpx.AsyncClient(**funding_client_options()) as client:
            while not self.isInterruptionRequested():
                cycle_started = asyncio.get_running_loop().time()
                async def update(exchange):
                    now = int(time.time() * 1000)
                    try:
                        point = await fetch_current_funding(client, self.instrument)
                        now = point.timestamp
                        points = [point]
                        await asyncio.to_thread(store.save, self.instrument, points, now)
                        self.status.emit(exchange, f'{point.rate * 100:.6f}%' )
                        self.updated.emit()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.status.emit(exchange, f'更新失败（保留缓存）· {funding_error_message(exc)}')
                await asyncio.gather(*(update(exchange) for exchange in EXCHANGES))
                elapsed = asyncio.get_running_loop().time() - cycle_started
                await asyncio.sleep(max(0, 1 - elapsed))
