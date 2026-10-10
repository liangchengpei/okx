import pytest

from okx_gui.liquidation_chart import LiquidationChart, LiquidationRecord, aggregate_records


@pytest.mark.parametrize('minutes', [1, 5, 15])
def test_buckets_are_aligned_and_count_records_at_boundaries(minutes):
    seconds = minutes * 60
    now = seconds * 100 + 30
    start = seconds * 100
    records = [LiquidationRecord(start - 0.1, 'long'),
               LiquidationRecord(start, 'long'),
               LiquidationRecord(start + 1, 'long'),
               LiquidationRecord(now, 'short'),
               LiquidationRecord(now + 1, 'short'),
               LiquidationRecord(start - seconds * 20, 'long')]
    buckets = aggregate_records(records, minutes, now)
    assert len(buckets) == 20
    assert all(b.start % seconds == 0 for b in buckets)
    assert (buckets[-2].long_count, buckets[-2].short_count) == (1, 0)
    assert (buckets[-1].long_count, buckets[-1].short_count) == (2, 1)
    assert all(b.long_count == b.short_count == 0 for b in buckets[:-2])


def test_five_and_fifteen_minute_counts_sum_one_minute_counts():
    now = 900 * 100 + 899
    records = [LiquidationRecord(now - offset, 'long' if offset % 2 else 'short')
               for offset in range(0, 900, 11)]
    one = aggregate_records(records, 1, now)
    five = aggregate_records(records, 5, now)
    fifteen = aggregate_records(records, 15, now)
    assert sum(b.long_count for b in one[-15:]) == sum(b.long_count for b in five[-3:]) == fifteen[-1].long_count
    assert sum(b.short_count for b in one[-15:]) == sum(b.short_count for b in five[-3:]) == fifteen[-1].short_count


def test_empty_records_still_show_zero_buckets():
    buckets = aggregate_records([], 1, 1000)
    assert len(buckets) == 20
    assert all(b.long_count == b.short_count == 0 for b in buckets)
    with pytest.raises(ValueError):
        aggregate_records([], 2, 1000)


def test_chart_switches_period_without_regenerating_simulation(qtbot):
    chart = LiquidationChart('BTC-USDT-SWAP', simulated=True)
    qtbot.addWidget(chart)
    chart.timer.stop()
    chart.show()
    original = list(chart.source.records)
    for index, minutes in enumerate((1, 5, 15)):
        chart.interval.setCurrentIndex(index)
        assert chart.plot.minutes == minutes
        assert chart.source.records == original
        assert len(chart.plot.buckets) == 44
        assert all(b.start % (minutes * 60) == 0 for b in chart.plot.buckets)
    chart.simulate()
    assert chart.plot.minutes == 15
    assert chart.plot.grab().width() > 0


def test_wheel_zoom_shows_more_buckets_and_preserves_period_and_history(qtbot):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    chart = LiquidationChart('BTC-USDT-SWAP', simulated=True)
    qtbot.addWidget(chart)
    chart.timer.stop()
    chart.resize(600, 300)
    chart.show()
    original = list(chart.source.records)

    def wheel(delta):
        position = QPointF(chart.plot.rect().center())
        event = QWheelEvent(position, position, QPoint(), QPoint(0, delta),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(chart.plot, event)
        assert event.isAccepted()

    wheel(-120)
    assert len(chart.plot.buckets) > 44
    assert chart.plot.minutes == 1
    assert chart.source.records == original
    latest = chart.plot.buckets[-1].start
    for _ in range(20):
        wheel(-120)
    assert len(chart.plot.buckets) == 125
    assert chart.plot.buckets[-1].start == latest
    chart.interval.setCurrentIndex(2)
    assert len(chart.plot.buckets) == 125
    assert chart.plot.minutes == 15
    assert chart.source.records == original
    chart.simulate()
    assert len(chart.plot.buckets) == 125
    for _ in range(25):
        wheel(120)
    assert len(chart.plot.buckets) == 10
    chart.plot.grab()  # Verify adaptive axis labels paint at both zoom limits.


@pytest.mark.parametrize('minutes', [1, 5, 15])
def test_stacked_counts_separate_platforms_and_sum_totals(minutes):
    now = 90000
    records = [LiquidationRecord(now, 'long', exchange='binance'),
               LiquidationRecord(now, 'long', exchange='okx'),
               LiquidationRecord(now, 'long', exchange='okx'),
               LiquidationRecord(now, 'short', exchange='binance'),
               LiquidationRecord(now, 'short', exchange='okx')]
    bucket = aggregate_records(records, minutes, now)[-1]
    assert (bucket.binance_long, bucket.okx_long, bucket.long_count) == (1, 2, 3)
    assert (bucket.binance_short, bucket.okx_short, bucket.short_count) == (1, 1, 2)
