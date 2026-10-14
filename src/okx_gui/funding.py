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


@dataclass(frozen=True)
class PricePoint:
    timestamp: int
    price: Decimal

    @property
    def rate(self):
        # Shared display reduction compares the series value.
        return self.price


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


class PriceStore(FundingStore):
    """Independent price series, with the same seven-day retention policy."""
    def __init__(self, path=None):
        super().__init__(path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS price_samples (
                instrument TEXT NOT NULL, timestamp INTEGER NOT NULL, price TEXT NOT NULL,
                PRIMARY KEY(instrument,timestamp))''')
            db.execute('CREATE INDEX IF NOT EXISTS price_sample_timestamp ON price_samples(timestamp)')

    def save(self, instrument, points, now):
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO price_samples VALUES (?,?,?)',
                [(instrument, p.timestamp, str(p.price)) for p in points
                 if now - RETENTION_MS <= p.timestamp <= now and p.price.is_finite() and p.price > 0])
            db.execute('DELETE FROM price_samples WHERE timestamp<?', (now - RETENTION_MS,))

    def load(self, instrument, now):
        with self.connect() as db:
            db.execute('DELETE FROM price_samples WHERE timestamp<?', (now - RETENTION_MS,))
            rows = db.execute('''SELECT timestamp,price FROM price_samples
                WHERE instrument=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp''',
                (instrument, now - RETENTION_MS, now)).fetchall()
        return [PricePoint(timestamp, Decimal(price)) for timestamp, price in rows]


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
    sampled = Signal(object)
    price_sampled = Signal(object)

    def __init__(self, instrument, database_path, parent=None):
        super().__init__(parent)
        self.instrument, self.database_path = instrument, database_path
        self._lock = threading.Lock()
        self._loop = self._task = None
        self._latest_price = None

    def set_price(self, value):
        try:
            price = Decimal(str(value))
        except InvalidOperation:
            return
        if price.is_finite() and price > 0:
            with self._lock:
                self._latest_price = (price, time.monotonic())

    async def collect_prices(self):
        store = PriceStore(self.database_path)
        while not self.isInterruptionRequested():
            with self._lock:
                latest = self._latest_price
            if latest and time.monotonic() - latest[1] <= 5:
                point = PricePoint(int(time.time() * 1000), latest[0])
                try:
                    await asyncio.to_thread(store.save, self.instrument, [point], point.timestamp)
                    self.price_sampled.emit(point)
                except Exception:
                    # Keep displaying live quotes even if disk persistence fails.
                    self.price_sampled.emit(point)
            await asyncio.sleep(1)

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
        await self.collect_prices()
