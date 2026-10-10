"""Time-bucketed liquidation record counts with an isolated simulation source."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import random
from zoneinfo import ZoneInfo

from okx_gui.liquidation_feed import LiquidationWorker
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
    bybit_long: int = 0
    bybit_short: int = 0

    @property
    def okx_long(self):
        return self.long_count - self.bybit_long

    @property
    def okx_short(self):
        return self.short_count - self.bybit_short


def aggregate_records(records, minutes, now, count=20):
    """Count records in aligned [start, end) buckets, including empty buckets."""
    if minutes not in (1, 5, 15):
        raise ValueError("聚合周期只支持 1、5、15 分钟")
    seconds = minutes * 60
    latest = math.floor(now / seconds) * seconds
    first = latest - (count - 1) * seconds
    totals = [[0, 0, 0, 0] for _ in range(count)]
    for record in records:
        if not first <= record.timestamp <= now:
            continue
        index = math.floor((record.timestamp - first) / seconds)
        if record.side not in ("long", "short"):
            raise ValueError("强平方向必须为 long 或 short")
        if record.exchange not in ("okx", "bybit"):
            raise ValueError("平台必须为 okx 或 bybit")
        side = 0 if record.side == "long" else 1
        totals[index][side] += 1
        if record.exchange == "bybit":
            totals[index][side + 2] += 1
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
                    start + self.random.random() * 60, self.random.choice(("long", "short")),
                    exchange=self.random.choice(("bybit", "okx"))
                ))

        self.store.save(instrument, "simulation", self.records, now)

    def advance(self, now):
        new_records = []
        for _ in range(self.random.randint(0, 3)):
            new_records.append(LiquidationRecord(now, self.random.choice(("long", "short")),
                                                  exchange=self.random.choice(("bybit", "okx"))))
        self.store.save(self.instrument, "simulation", new_records, now)
        self.records = [record for record in self.records + new_records if record.timestamp >= now - HISTORY_SECONDS]


class CountPlot(QWidget):
    visible_count_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.buckets = []
        self.visible_count = 44
        self.minutes = 1
        self.plot_rect = QRectF()
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setAccessibleDescription("滚轮向下缩小，显示更多时间区间；向上放大。支持10至125个区间。")
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
        # Two touching bars plus one bar-width gap between adjacent groups.
        bar_width = slot / 3
        label_step = max(1, math.ceil(len(self.buckets) / max(1, int(plot.width() / 65))))
        for i, bucket in enumerate(self.buckets):
            center = plot.left() + (i + 0.5) * slot
            for offset, lower, upper, color in (
                (-bar_width, bucket.bybit_long, bucket.okx_long, LONG_COLOR),
                (0, bucket.bybit_short, bucket.okx_short, SHORT_COLOR),
            ):
                bottom = plot.bottom()
                for value, fill in ((lower, QColor(color)), (upper, QColor(color).lighter(155))):
                    height = value / maximum * plot.height()
                    painter.fillRect(QRectF(center + offset, bottom - height, bar_width, height), fill)
                    bottom -= height
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
            count = max(10, min(125, round(self.visible_count * 1.2 ** (-delta / 120))))
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
            self.setToolTip(f"{start}–{end}（北京时间）\nBybit（下段）：多头 {bucket.bybit_long} 条 · 空头 {bucket.bybit_short} 条\nOKX（上段）：多头 {bucket.okx_long} 条 · 空头 {bucket.okx_short} 条\n合计：多头 {bucket.long_count} 条 · 空头 {bucket.short_count} 条"
                            + ("\n当前区间尚未结束" if index == len(self.buckets) - 1 else ""))
        else:
            self.setToolTip("")
        super().mouseMoveEvent(event)


class LiquidationChart(QFrame):
    def __init__(self, instrument, *, simulated=False):
        super().__init__()
        self.setObjectName("liquidationChart")
        self.setAccessibleName(f"{instrument} liquidation-orders 图表区域")
        self.setMinimumHeight(280)
        self.instrument = instrument
        self.simulated = simulated
        self.store = LiquidationStore()
        self.source = SimulatedLiquidations(instrument, datetime.now(timezone.utc).timestamp(), self.store) if simulated else None
        self.worker = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(2)
        controls = QHBoxLayout()
        title = QLabel("强平记录 · 模拟数据" if simulated else "强平记录 · 实时数据")
        title.setObjectName("muted")
        title.setToolTip("在图内滚动鼠标滚轮：向下显示更多柱子，向上放大；显示10至125个时间区间")
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
        self.interval.setToolTip("切换周期使用同一组历史记录重新聚合；图内滚轮可横向缩放")
        controls.addWidget(self.interval)
        layout.addLayout(controls)
        platform_legend = QLabel("下段深色：Bybit　上段：OKX（暂停预留）")
        platform_legend.setObjectName("muted")
        layout.addWidget(platform_legend)
        self.feed_status = QLabel("Bybit：连接中　OKX：暂停")
        self.feed_status.setObjectName("muted")
        self.feed_status.setWordWrap(True)
        self.feed_status.setVisible(not simulated)
        self._statuses = {"bybit": "连接中", "okx": "暂停"}
        layout.addWidget(self.feed_status)
        self.plot = CountPlot()
        layout.addWidget(self.plot, 1)
        self.interval.currentIndexChanged.connect(self.refresh)
        self.plot.visible_count_changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.simulate if simulated else self.refresh)
        self.timer.start()
        self.refresh()
        if not simulated:
            self.worker = LiquidationWorker(instrument, self.store.path, self)
            self.worker.status.connect(self.update_feed_status)
            self.worker.start()

    def refresh(self):
        self.plot.minutes = self.interval.currentData()
        now = datetime.now(timezone.utc).timestamp()
        since = (math.floor(now / (self.plot.minutes * 60)) - self.plot.visible_count + 1) * self.plot.minutes * 60
        records = self.source.records if self.simulated else self.store.load(self.instrument, "live", now, since=since) or []
        records = [record for record in records if record.exchange == "bybit"]
        self.plot.buckets = aggregate_records(records, self.plot.minutes, now, count=self.plot.visible_count)
        self.plot.update()

    def simulate(self):
        self.source.advance(datetime.now(timezone.utc).timestamp())
        self.refresh()

    def update_feed_status(self, exchange, status):
        self._statuses[exchange] = status
        self.feed_status.setText(f"Bybit：{self._statuses['bybit']}　OKX：{self._statuses['okx']}")

    def shutdown(self):
        self.timer.stop()
        if self.worker:
            self.worker.stop()
            return self.worker.wait(7000)
        return True

    def closeEvent(self, event):
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
