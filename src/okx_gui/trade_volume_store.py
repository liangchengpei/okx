"""Persistent OKX taker volume: minute totals plus durable trade-ID deduplication."""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path
import sqlite3

HISTORY_SECONDS = 30 * 86400


@dataclass(frozen=True)
class Trade:
    timestamp: float
    trade_id: str
    side: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class VolumeBucket:
    start: int
    buy: Decimal = Decimal(0)
    sell: Decimal = Decimal(0)


class TradeVolumeStore:
    def __init__(self, path=None):
        root = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
        self.path = Path(path) if path is not None else root / 'okx-trader/trade-volume.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pruned_at = None
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS seen (
                    instrument TEXT NOT NULL, trade_id TEXT NOT NULL, timestamp REAL NOT NULL,
                    PRIMARY KEY(instrument, trade_id));
                CREATE INDEX IF NOT EXISTS seen_time ON seen(timestamp);
                CREATE TABLE IF NOT EXISTS minutes (
                    instrument TEXT NOT NULL, start INTEGER NOT NULL,
                    buy TEXT NOT NULL, sell TEXT NOT NULL, PRIMARY KEY(instrument, start));
                CREATE INDEX IF NOT EXISTS minutes_time ON minutes(start);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, instrument, trades, now):
        totals = {}
        with self.connect() as db:
            for trade in trades:
                if not now - HISTORY_SECONDS <= trade.timestamp <= now:
                    continue
                inserted = db.execute('INSERT OR IGNORE INTO seen VALUES (?, ?, ?)',
                                      (instrument, trade.trade_id, trade.timestamp)).rowcount
                if not inserted:
                    continue
                start = int(trade.timestamp // 60) * 60
                values = totals.setdefault(start, [Decimal(0), Decimal(0)])
                values[0 if trade.side == 'buy' else 1] += trade.quantity
            for start, values in totals.items():
                old = db.execute('SELECT buy, sell FROM minutes WHERE instrument=? AND start=?',
                                 (instrument, start)).fetchone()
                if old:
                    values = [values[i] + Decimal(old[i]) for i in range(2)]
                db.execute('INSERT OR REPLACE INTO minutes VALUES (?, ?, ?, ?)',
                           (instrument, start, str(values[0]), str(values[1])))
            if self._pruned_at is None or now - self._pruned_at >= 3600:
                db.execute('DELETE FROM seen WHERE timestamp < ?', (now - HISTORY_SECONDS,))
                db.execute('DELETE FROM minutes WHERE start < ?', (int((now - HISTORY_SECONDS) // 60) * 60,))
                self._pruned_at = now

    def buckets(self, instrument, minutes, now, count=44):
        if minutes not in (1, 5, 15):
            raise ValueError('聚合周期只支持 1、5、15 分钟')
        seconds = minutes * 60
        latest = int(now // seconds) * seconds
        first = latest - (count - 1) * seconds
        totals = [[Decimal(0), Decimal(0)] for _ in range(count)]
        with self.connect() as db:
            rows = db.execute('SELECT start, buy, sell FROM minutes WHERE instrument=? AND start>=? AND start<=?',
                              (instrument, max(first, int((now - HISTORY_SECONDS) // 60) * 60), now)).fetchall()
        for start, buy, sell in rows:
            index = (start - first) // seconds
            totals[index][0] += Decimal(buy)
            totals[index][1] += Decimal(sell)
        return [VolumeBucket(first + i * seconds, *values) for i, values in enumerate(totals)]
