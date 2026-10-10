"""Time-bucketed liquidation record counts with an isolated simulation source."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import random
from zoneinfo import ZoneInfo

from okx_gui.liquidation_store import HISTORY_SECONDS, LiquidationRecord, LiquidationStore

from PySide6.QtCore import Qt, QRectF, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

DISPLAY_ZONE = ZoneInfo("Asia/Shanghai")
LONG_COLOR = "#d94b55"
SHORT_COLOR = "#15966b"
SIMULATION_SEED_MINUTES = 24 * 60


@dataclass(frozen=True)
class CountBucket:
    start: int
    long_count: int
    short_count: int


def aggregate_records(records, minutes, now, count=20):
    """Count records in aligned [start, end) buckets, including empty buckets."""
    if minutes not in (1, 5, 15):
        raise ValueError("聚合周期只支持 1、5、15 分钟")
    seconds = minutes * 60
    latest = math.floor(now / seconds) * seconds
    first = latest - (count - 1) * seconds
    totals = [[0, 0] for _ in range(count)]
    for record in records:
        if not first <= record.timestamp <= now:
            continue
        index = math.floor((record.timestamp - first) / seconds)
        if record.side not in ("long", "short"):
            raise ValueError("强平方向必须为 long 或 short")
        totals[index][0 if record.side == "long" else 1] += 1
    return [CountBucket(first + i * seconds, *values) for i, values in enumerate(totals)]


class SimulatedLiquidations:
    """Restore persisted simulated events and keep raw history for aggregation."""

    def __init__(self, instrument, now, store=None):
        self.instrument = instrument
        self.store = store if store is not None else LiquidationStore()
        existing = self.store.load(instrument, "simulation", now)
        self.random = random.Random(instrument)
        self.records = existing if existing is not None else []
        if existing is not None:
            return
        for minute in range(SIMULATION_SEED_MINUTES):
            start = now - (minute + 1) * 60
            for _ in range(self.random.randint(0, 18)):
                self.records.append(LiquidationRecord(
                    start + self.random.random() * 60, self.random.choice(("long", "short"))
                ))

        self.store.save(instrument, "simulation", self.records, now)

    def advance(self, now):
        new_records = []
        for _ in range(self.random.randint(0, 3)):
            new_records.append(LiquidationRecord(now, self.random.choice(("long", "short"))))
        self.store.save(self.instrument, "simulation", new_records, now)
        self.records = [record for record in self.records + new_records if record.timestamp >= now - HISTORY_SECONDS]


class CountPlot(QWidget):
    visible_count_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.buckets = []
        self.visible_count = 20
        self.minutes = 1
        self.plot_rect = QRectF()
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setAccessibleDescription("滚轮向下缩小，显示更多时间区间；向上放大。支持8至96个区间。")
        self.setAccessibleName("强平记录数量双柱图，红色多头，绿色空头")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("white"))
        font = self.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 1))
        painter.setFont(font)
        plot = QRectF(48, 22, max(1, self.width() - 62), max(1, self.height() - 62))
        self.plot_rect = plot
        peak = max((max(b.long_count, b.short_count) for b in self.buckets), default=0)
        tick = max(1, math.ceil(peak / 4))
        maximum = tick * 4
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            painter.setPen(QPen(QColor("#e9ebee"), 1))
            painter.drawLine(plot.left(), y, plot.right(), y)
            painter.setPen(QColor("#7b818a"))
            painter.drawText(QRectF(0, y - 9, 40, 18), Qt.AlignRight | Qt.AlignVCenter, str(i * tick))
        painter.drawText(QRectF(4, 0, 130, 20), Qt.AlignLeft, "记录数")
        if not self.buckets:
            return
        slot = plot.width() / len(self.buckets)
        # Original group gap was 70% of a slot; reduce that gap by one third.
        group_gap = slot * 0.70 * (2 / 3)
        bar_width = max(0.5, (slot - group_gap) / 2)
        label_step = max(1, math.ceil(len(self.buckets) / max(1, int(plot.width() / 65))))
        for i, bucket in enumerate(self.buckets):
            center = plot.left() + (i + 0.5) * slot
            for offset, value, color in ((-bar_width, bucket.long_count, LONG_COLOR),
                                         (0, bucket.short_count, SHORT_COLOR)):
                height = value / maximum * plot.height()
                painter.fillRect(QRectF(center + offset, plot.bottom() - height, bar_width, height), QColor(color))
            is_last = i == len(self.buckets) - 1
            if is_last or (i % label_step == 0 and len(self.buckets) - 1 - i >= label_step):
                label = datetime.fromtimestamp(bucket.start, DISPLAY_ZONE).strftime("%H:%M")
                painter.setPen(QColor("#7b818a"))
                painter.drawText(QRectF(center - 24, plot.bottom() + 5, 48, 18), Qt.AlignCenter, label)
        painter.drawText(QRectF(plot.left(), plot.bottom() + 24, plot.width(), 16),
                         Qt.AlignRight, "时间（北京时间）")

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            delta = (event.pixelDelta().y() or event.pixelDelta().x()) * 3
        if delta:
            count = max(8, min(96, round(self.visible_count * 1.2 ** (-delta / 120))))
            if count != self.visible_count:
                self.visible_count = count
                self.setToolTip("")
                self.visible_count_changed.emit(count)
            event.accept()  # Zooming must not scroll the surrounding contract list.
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if self.buckets and self.plot_rect.contains(event.position()):
            index = min(len(self.buckets) - 1, int(
                (event.position().x() - self.plot_rect.left()) / self.plot_rect.width() * len(self.buckets)
            ))
            bucket = self.buckets[index]
            start = datetime.fromtimestamp(bucket.start, DISPLAY_ZONE).strftime("%m-%d %H:%M")
            end = datetime.fromtimestamp(bucket.start + self.minutes * 60, DISPLAY_ZONE).strftime("%H:%M")
            self.setToolTip(f"{start}–{end}（北京时间）\n多头强平：{bucket.long_count} 条\n空头强平：{bucket.short_count} 条"
                            + ("\n当前区间尚未结束" if index == len(self.buckets) - 1 else ""))
        else:
            self.setToolTip("")
        super().mouseMoveEvent(event)


class LiquidationChart(QFrame):
    def __init__(self, instrument):
        super().__init__()
        self.setObjectName("liquidationChart")
        self.setAccessibleName(f"{instrument} liquidation-orders 图表区域")
        self.setMinimumHeight(280)
        self.source = SimulatedLiquidations(instrument, datetime.now(timezone.utc).timestamp())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(2)
        controls = QHBoxLayout()
        title = QLabel("强平记录 · 模拟数据")
        title.setObjectName("muted")
        title.setToolTip("在图内滚动鼠标滚轮：向下显示更多柱子，向上放大；显示8至96个时间区间")
        controls.addWidget(title)
        for text, color in (("■ 多头", LONG_COLOR), ("■ 空头", SHORT_COLOR)):
            label = QLabel(text)
            label.setStyleSheet(f"color: {color}; font-size: 12px; border: 0;")
            controls.addWidget(label)
        controls.addStretch()
        self.interval = QComboBox()
        self.interval.setAccessibleName(f"{instrument} 强平图聚合周期")
        for minutes in (1, 5, 15):
            self.interval.addItem(f"{minutes} 分钟", minutes)
        self.interval.setToolTip("切换周期使用同一组模拟记录重新聚合；图内滚轮可横向缩放")
        controls.addWidget(self.interval)
        layout.addLayout(controls)
        self.plot = CountPlot()
        layout.addWidget(self.plot, 1)
        self.interval.currentIndexChanged.connect(self.refresh)
        self.plot.visible_count_changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.simulate)
        self.timer.start()
        self.refresh()

    def refresh(self):
        self.plot.minutes = self.interval.currentData()
        self.plot.buckets = aggregate_records(self.source.records, self.plot.minutes,
                                              datetime.now(timezone.utc).timestamp(), count=self.plot.visible_count)
        self.plot.update()

    def simulate(self):
        self.source.advance(datetime.now(timezone.utc).timestamp())
        self.refresh()
