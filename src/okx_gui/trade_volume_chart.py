"""OKX aggressive buy/sell volume, rendered from cached minute totals."""
from datetime import datetime
import math
import time
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QRectF, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from okx_gui.trade_volume_feed import TradeVolumeWorker
from okx_gui.trade_volume_store import TradeVolumeStore

BUY_COLOR = '#d94b55'
SELL_COLOR = '#3979d6'
DISPLAY_ZONE = ZoneInfo('Asia/Shanghai')


def volume_text(value):
    return format(value, ',.8f').rstrip('0').rstrip('.')


def axis_text(value):
    for scale, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if value >= scale:
            return f'{value / scale:.3g}{suffix}'
    return f'{value:.4g}'


class VolumePlot(QWidget):
    visible_count_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.buckets = []
        self.visible_count = 44
        self.minutes = 1
        self.plot_rect = QRectF()
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setAccessibleName('OKX 主动买卖成交量双柱图，红色主动买入，蓝色主动卖出')
        self.setAccessibleDescription('纵轴单位为张。滚轮横向缩放，显示10至125个时间区间。')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('white'))
        font = self.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 1))
        painter.setFont(font)
        plot = QRectF(60, 22, max(1, self.width() - 74), max(1, self.height() - 62))
        self.plot_rect = plot
        peak = max((float(max(b.buy, b.sell)) for b in self.buckets), default=0)
        maximum = peak * 1.1 if peak > 0 else 1
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            painter.setPen(QPen(QColor('#e9ebee'), 1))
            painter.drawLine(plot.left(), y, plot.right(), y)
            painter.setPen(QColor('#7b818a'))
            painter.drawText(QRectF(0, y - 9, 54, 18), Qt.AlignRight | Qt.AlignVCenter,
                             axis_text(maximum * i / 4))
        painter.drawText(QRectF(4, 0, 150, 20), Qt.AlignLeft, '成交量（张）')
        if not self.buckets:
            return
        slot = plot.width() / len(self.buckets)
        bar_width = slot / 3
        label_step = max(1, math.ceil(len(self.buckets) / max(1, int(plot.width() / 65))))
        for i, bucket in enumerate(self.buckets):
            center = plot.left() + (i + 0.5) * slot
            for offset, value, color in ((-bar_width, bucket.buy, BUY_COLOR), (0, bucket.sell, SELL_COLOR)):
                height = float(value) / maximum * plot.height()
                painter.fillRect(QRectF(center + offset, plot.bottom() - height, bar_width, height), QColor(color))
            if i == len(self.buckets) - 1 or (i % label_step == 0 and len(self.buckets) - 1 - i >= label_step):
                painter.setPen(QColor('#7b818a'))
                label = datetime.fromtimestamp(bucket.start, DISPLAY_ZONE).strftime('%H:%M')
                painter.drawText(QRectF(center - 24, plot.bottom() + 5, 48, 18), Qt.AlignCenter, label)
        painter.drawText(QRectF(plot.left(), plot.bottom() + 24, plot.width(), 16),
                         Qt.AlignRight, '时间（北京时间）')

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            delta = (event.pixelDelta().y() or event.pixelDelta().x()) * 3
        if delta:
            count = max(10, min(125, round(self.visible_count * 1.2 ** (-delta / 120))))
            if count != self.visible_count:
                self.visible_count = count
                self.setToolTip('')
                self.visible_count_changed.emit(count)
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if self.buckets and self.plot_rect.contains(event.position()):
            index = min(len(self.buckets) - 1, int(
                (event.position().x() - self.plot_rect.left()) / self.plot_rect.width() * len(self.buckets)))
            bucket = self.buckets[index]
            start = datetime.fromtimestamp(bucket.start, DISPLAY_ZONE).strftime('%m-%d %H:%M')
            end = datetime.fromtimestamp(bucket.start + self.minutes * 60, DISPLAY_ZONE).strftime('%H:%M')
            self.setToolTip(f'{start}–{end}（北京时间）\n主动买入：{volume_text(bucket.buy)} 张\n'
                            f'主动卖出：{volume_text(bucket.sell)} 张'
                            + ('\n当前区间尚未结束' if index == len(self.buckets) - 1 else ''))
        else:
            self.setToolTip('')
        super().mouseMoveEvent(event)


class TradeVolumeChart(QFrame):
    def __init__(self, instrument, *, store=None, start_worker=True):
        super().__init__()
        self.setObjectName('tradeVolumeChart')
        self.setAccessibleName(f'{instrument} OKX 主动买卖成交量')
        self.setMinimumHeight(280)
        self.instrument = instrument
        self.store = store if store is not None else TradeVolumeStore()
        self.worker = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(2)
        controls = QHBoxLayout()
        title = QLabel('OKX 主动买卖量')
        title.setObjectName('muted')
        title.setToolTip('按主动成交方向累计成交张数，不是笔数。仅统计本程序接收到的实时成交，断线期间不补齐。')
        controls.addWidget(title)
        for text, color in (('■ 主动买入', BUY_COLOR), ('■ 主动卖出', SELL_COLOR)):
            label = QLabel(text)
            label.setStyleSheet(f'color: {color}; border: 0;')
            controls.addWidget(label)
        controls.addStretch()
        self.interval = QComboBox()
        self.interval.setAccessibleName(f'{instrument} 成交量聚合周期')
        for minutes in (1, 5, 15):
            self.interval.addItem(f'{minutes} 分钟', minutes)
        controls.addWidget(self.interval)
        layout.addLayout(controls)
        self.details = QLabel()
        self.details.setObjectName('muted')
        self.details.setFixedHeight(self.details.fontMetrics().lineSpacing() + 4)
        layout.addWidget(self.details)
        self.feed_status = QLabel('OKX：连接中')
        self.feed_status.setObjectName('muted')
        self.feed_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.feed_status)
        self.plot = VolumePlot()
        layout.addWidget(self.plot, 1)
        self.interval.currentIndexChanged.connect(self.refresh)
        self.plot.visible_count_changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()
        if start_worker:
            self.worker = TradeVolumeWorker(instrument, self.store.path, self)
            self.worker.status.connect(self.feed_status.setText)
            self.worker.start()

    def refresh(self):
        self.plot.minutes = self.interval.currentData()
        try:
            buckets = self.store.buckets(self.instrument, self.plot.minutes, time.time(), self.plot.visible_count)
        except Exception as exc:
            self.feed_status.setText(f'缓存读取失败 · {str(exc)[:100]}')
            return
        self.plot.buckets = buckets
        latest = buckets[-1]
        self.details.setText(f'当前区间 · 主动买入 {volume_text(latest.buy)} 张　主动卖出 {volume_text(latest.sell)} 张')
        self.plot.update()

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
