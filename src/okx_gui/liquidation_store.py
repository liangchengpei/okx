"""SQLite history, separated by contract and data source."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

HISTORY_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class LiquidationRecord:
    timestamp: float
    side: str
    event_id: str = field(default_factory=lambda: uuid4().hex)


def default_database_path():
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return root / "okx-trader/liquidations.sqlite3"


class LiquidationStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS streams (
                instrument TEXT NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY (instrument, source))""")
            db.execute("""CREATE TABLE IF NOT EXISTS liquidation_records (
                instrument TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL,
                timestamp REAL NOT NULL, side TEXT NOT NULL CHECK(side IN ('long', 'short')),
                PRIMARY KEY (instrument, source, event_id))""")
            db.execute("""CREATE INDEX IF NOT EXISTS liquidation_time
                ON liquidation_records(instrument, source, timestamp)""")

    @contextmanager
    def connect(self):
        # sqlite3's transaction context does not close the connection itself.
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self, instrument, source, now):
        """None means never initialized; [] means initialized but no recent events."""
        with self.connect() as db:
            db.execute("DELETE FROM liquidation_records WHERE timestamp < ?", (now - HISTORY_SECONDS,))
            exists = db.execute("SELECT 1 FROM streams WHERE instrument=? AND source=?",
                                (instrument, source)).fetchone()
            if not exists:
                return None
            rows = db.execute("""SELECT timestamp, side, event_id FROM liquidation_records
                WHERE instrument=? AND source=? AND timestamp<=? ORDER BY timestamp, event_id""",
                              (instrument, source, now)).fetchall()
        return [LiquidationRecord(*row) for row in rows]

    def save(self, instrument, source, records, now):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO streams VALUES (?, ?)", (instrument, source))
            db.executemany("""INSERT INTO liquidation_records VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(instrument, source, event_id) DO NOTHING""",
                           [(instrument, source, r.event_id, r.timestamp, r.side) for r in records])
            db.execute("DELETE FROM liquidation_records WHERE timestamp < ?", (now - HISTORY_SECONDS,))
