"""Contract-specific account long/short ratios, sampled by OKX every five minutes."""
import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import threading
import time

import httpx
from PySide6.QtCore import QThread, Signal

from okx_gui.funding import FundingStore, RETENTION_MS
from okx_gui.liquidation_store import default_database_path

RATIO_URL = 'https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract'
POLL_SECONDS = 300


@dataclass(frozen=True)
class RatioPoint:
    timestamp: int
    ratio: Decimal


def parse_ratios(payload):
    if not isinstance(payload, dict) or payload.get('code') != '0':
        message = payload.get('msg', '无效响应') if isinstance(payload, dict) else '无效响应'
        code = payload.get('code', '?') if isinstance(payload, dict) else '?'
        raise ValueError(f'多空账户比查询失败（{code}）：{message}')
    if not isinstance(payload.get('data'), list):
        raise ValueError('多空账户比数据不完整')
    points = {}
    for row in payload['data']:
        try:
            timestamp, ratio = int(row[0]), Decimal(row[1])
            if timestamp <= 0 or not ratio.is_finite() or ratio < 0:
                raise ValueError('无效数值')
        except (ValueError, TypeError, IndexError, InvalidOperation) as exc:
            raise ValueError('多空账户比数据不完整') from exc
        points[timestamp] = RatioPoint(timestamp, ratio)
    return sorted(points.values(), key=lambda p: p.timestamp)


class RatioStore(FundingStore):
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_database_path().with_name('account_ratios.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS account_ratios (
                instrument TEXT NOT NULL, timestamp INTEGER NOT NULL, ratio TEXT NOT NULL,
                PRIMARY KEY(instrument,timestamp))''')
            db.execute('CREATE INDEX IF NOT EXISTS ratio_time ON account_ratios(timestamp)')

    def save(self, instrument, points, now):
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO account_ratios VALUES (?,?,?)',
                          [(instrument, p.timestamp, str(p.ratio)) for p in points
                           if now - RETENTION_MS <= p.timestamp <= now and p.ratio.is_finite() and p.ratio >= 0])
            db.execute('DELETE FROM account_ratios WHERE timestamp<?', (now - RETENTION_MS,))

    def load(self, instrument, now):
        with self.connect() as db:
            rows = db.execute('SELECT timestamp,ratio FROM account_ratios WHERE instrument=? '
                              'AND timestamp>=? AND timestamp<=? ORDER BY timestamp',
                              (instrument, now - RETENTION_MS, now)).fetchall()
        return [RatioPoint(ts, Decimal(ratio)) for ts, ratio in rows]


class RatioWorker(QThread):
    sampled = Signal(object)
    status = Signal(str)

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
        store = RatioStore(self.database_path)
        async with httpx.AsyncClient(timeout=12) as client:
            while not self.isInterruptionRequested():
                started = time.monotonic()
                try:
                    response = await client.get(RATIO_URL, params={
                        'instId': self.instrument, 'period': '5m', 'limit': '100'})
                    response.raise_for_status()
                    points = parse_ratios(response.json())
                    now = int(time.time() * 1000)
                    points = [p for p in points if now - RETENTION_MS <= p.timestamp <= now]
                    if not points:
                        raise ValueError('该合约暂无多空账户比数据')
                    cache_error = ''
                    try:
                        await asyncio.to_thread(store.save, self.instrument, points, now)
                    except Exception:
                        cache_error = '（缓存写入失败）'
                    self.sampled.emit(points)
                    self.status.emit(cache_error)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.status.emit(f'多空账户比更新失败（保留缓存）· {str(exc)[:100]}')
                await asyncio.sleep(max(0, POLL_SECONDS - (time.monotonic() - started)))
