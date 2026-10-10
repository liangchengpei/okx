import sqlite3

import pytest

from okx_gui.liquidation_chart import SimulatedLiquidations
from okx_gui.liquidation_store import HISTORY_SECONDS, LiquidationRecord, LiquidationStore


def test_database_reopens_and_deduplicates_events_without_merging_same_timestamp(tmp_path):
    path = tmp_path / 'history.sqlite3'
    store = LiquidationStore(path)
    records = [LiquidationRecord(1000, 'long'), LiquidationRecord(1000, 'long')]
    store.save('BTC', 'simulation', records, 1000)
    store.save('BTC', 'simulation', records, 1000)
    loaded = LiquidationStore(path).load('BTC', 'simulation', 1000)
    assert set(loaded) == set(records)
    assert len(loaded) == 2


def test_contracts_and_simulated_real_data_are_isolated(tmp_path):
    store = LiquidationStore(tmp_path / 'history.sqlite3')
    for instrument, source, side in [('BTC', 'simulation', 'long'), ('SPCX', 'simulation', 'short'),
                                     ('BTC', 'okx', 'short')]:
        store.save(instrument, source, [LiquidationRecord(1000, side)], 1000)
    assert [r.side for r in store.load('BTC', 'simulation', 1000)] == ['long']
    assert [r.side for r in store.load('BTC', 'okx', 1000)] == ['short']
    assert [r.side for r in store.load('SPCX', 'simulation', 1000)] == ['short']
    assert store.load('ETH', 'simulation', 1000) is None


def test_expiration_preserves_initialized_stream_and_cleans_all_contracts(tmp_path):
    store = LiquidationStore(tmp_path / 'history.sqlite3')
    records = [LiquidationRecord(1000, 'long'), LiquidationRecord(1001, 'short')]
    store.save('BTC', 'simulation', records, 1001)
    store.save('SPCX', 'simulation', records[:1], 1001)
    assert store.load('BTC', 'simulation', 1001 + HISTORY_SECONDS) == records[1:]
    assert store.load('SPCX', 'simulation', 1001 + HISTORY_SECONDS) == []


def test_simulation_restores_history_instead_of_reseeding_after_restart(tmp_path):
    path = tmp_path / 'history.sqlite3'
    source = SimulatedLiquidations('BTC', 100000, LiquidationStore(path))
    original = set(source.records)
    restored = SimulatedLiquidations('BTC', 100001, LiquidationStore(path))
    assert set(restored.records) == original
    restored.advance(100005)
    assert set(LiquidationStore(path).load('BTC', 'simulation', 100005)) == set(restored.records)
    expired = SimulatedLiquidations('BTC', 100005 + HISTORY_SECONDS + 1, LiquidationStore(path))
    assert expired.records == []


def test_batch_failure_rolls_back_all_records(tmp_path):
    store = LiquidationStore(tmp_path / 'history.sqlite3')
    with pytest.raises(sqlite3.IntegrityError):
        store.save('BTC', 'simulation', [LiquidationRecord(1000, 'long'),
                                         LiquidationRecord(1000, 'invalid')], 1000)
    assert store.load('BTC', 'simulation', 1000) is None


def test_thirty_day_retention_keeps_older_history_across_restart_and_updates(tmp_path):
    day = 24 * 60 * 60
    now = 40 * day
    store = LiquidationStore(tmp_path / 'history.sqlite3')
    expired = LiquidationRecord(now - 30 * day - 1, 'long')
    boundary = LiquidationRecord(now - 30 * day, 'short')
    retained = LiquidationRecord(now - 25 * day, 'long')
    store.save('BTC', 'simulation', [expired, boundary, retained], now)
    restored = SimulatedLiquidations('BTC', now, store)
    assert set(restored.records) == {boundary, retained}
    restored.advance(now + 1)
    assert retained in restored.records
    assert boundary not in restored.records
    assert retained in store.load('BTC', 'simulation', now + 1)


def test_platforms_with_same_event_id_are_saved_separately(tmp_path):
    store = LiquidationStore(tmp_path / 'history.sqlite3')
    records = [LiquidationRecord(1000, 'long', 'same-id', 'okx'),
               LiquidationRecord(1000, 'short', 'same-id', 'binance')]
    store.save('BTC', 'simulation', records, 1000)
    assert set(store.load('BTC', 'simulation', 1000)) == set(records)


def test_legacy_database_migrates_without_losing_records(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE streams (instrument TEXT, source TEXT, PRIMARY KEY(instrument,source))')
        db.execute('INSERT INTO streams VALUES (?,?)', ('BTC', 'simulation'))
        db.execute('''CREATE TABLE liquidation_records (instrument TEXT, source TEXT, event_id TEXT,
                   timestamp REAL, side TEXT, PRIMARY KEY(instrument,source,event_id))''')
        db.execute('INSERT INTO liquidation_records VALUES (?,?,?,?,?)', ('BTC','simulation','old',1000,'long'))
        db.execute('CREATE INDEX liquidation_time ON liquidation_records(instrument,source,timestamp)')
    store = LiquidationStore(path)
    assert store.load('BTC', 'simulation', 1000) == [LiquidationRecord(1000, 'long', 'old', 'okx')]
    record = LiquidationRecord(1000, 'short', 'old', 'binance')
    store.save('BTC', 'simulation', [record], 1000)
    assert len(LiquidationStore(path).load('BTC', 'simulation', 1000)) == 2


def test_two_platform_database_upgrades_to_bybit_without_relabeling_old_data(tmp_path):
    path = tmp_path / 'two-platform.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE streams (instrument TEXT, source TEXT, PRIMARY KEY(instrument,source))')
        db.execute('INSERT INTO streams VALUES (?,?)', ('BTC', 'live'))
        db.execute('''CREATE TABLE liquidation_records (
            instrument TEXT, source TEXT, event_id TEXT, timestamp REAL, side TEXT,
            exchange TEXT CHECK(exchange IN ('okx', 'binance')),
            PRIMARY KEY(instrument,source,exchange,event_id))''')
        db.executemany('INSERT INTO liquidation_records VALUES (?,?,?,?,?,?)', [
            ('BTC','live','old',1000,'long','okx'), ('BTC','live','old',1000,'short','binance')])
    store = LiquidationStore(path)
    new = LiquidationRecord(1000, 'long', 'old', 'bybit')
    store.save('BTC', 'live', [new], 1000)
    records = LiquidationStore(path).load('BTC', 'live', 1000)
    assert len(records) == 3
    assert {r.exchange for r in records} == {'okx', 'binance', 'bybit'}
