"""OKX live funding-rate curve."""
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from okx_gui.funding import EXCHANGES, RETENTION_MS, FundingStore, FundingWorker

COLORS = {'okx': '#2679d8'}
NAMES = {'okx': 'OKX'}
ZONE = ZoneInfo('Asia/Shanghai')


class FundingPlot(QWidget):
    def __init__(self):
        super().__init__()
        self.points = []
        self.now = int(time.time() * 1000)
        self.plot_rect = QRectF()
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setAccessibleName('OKX 实时资金费率曲线')

    @property
    def span(self):
        earliest = min((point.timestamp for point in self.points), default=self.now)
        return min(RETENTION_MS, max(3600000, self.now - earliest))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('white'))
        font = self.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 1))
        painter.setFont(font)
        rect = QRectF(72, 24, max(1, self.width() - 88), max(1, self.height() - 66))
        self.plot_rect = rect
        values = [float(point.rate * 100) for point in self.points]
        low, high = min(values + [0]), max(values + [0])
        padding = max((high - low) * 0.12, 0.001)
        low, high = low - padding, high + padding
        for i in range(5):
            y = rect.bottom() - i * rect.height() / 4
            value = low + i * (high - low) / 4
            painter.setPen(QPen(QColor('#e9ebee'), 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor('#7b818a'))
            painter.drawText(QRectF(0, y - 9, 66, 18), Qt.AlignRight | Qt.AlignVCenter, f'{value:.4f}%')
        painter.drawText(QRectF(4, 0, 180, 20), Qt.AlignLeft, '实时资金费率（%）')
        for i in range(5):
            timestamp = self.now - self.span + self.span * i / 4
            label = datetime.fromtimestamp(timestamp / 1000, ZONE).strftime('%H:%M')
            x = rect.left() + i * rect.width() / 4
            painter.drawText(QRectF(x - 24, rect.bottom() + 4, 48, 34), Qt.AlignCenter, label)
        painter.save()
        painter.setClipRect(rect.adjusted(-2, -2, 2, 2))
        for exchange in EXCHANGES:
            points = [point for point in self.points if point.exchange == exchange]
            if not points:
                continue
            path = QPainterPath()
            painter.setPen(QPen(QColor(COLORS[exchange]), 1.6))
            painter.setBrush(QColor(COLORS[exchange]))
            for i, point in enumerate(points):
                x = rect.left() + (point.timestamp - self.now + self.span) / self.span * rect.width()
                y = rect.bottom() - (float(point.rate * 100) - low) / (high - low) * rect.height()
                position = QPointF(x, y)
                if i and point.timestamp - points[i - 1].timestamp <= 5 * 60000:
                    path.lineTo(position)
                else:
                    path.moveTo(position)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        painter.restore()
        if not self.points:
            painter.setPen(QColor('#7b818a'))
            painter.drawText(rect, Qt.AlignCenter, '等待实时资金费率，采集后逐步形成曲线')

    def mouseMoveEvent(self, event):
        if self.plot_rect.contains(event.position()) and self.points:
            timestamp = self.now - self.span + (event.position().x() - self.plot_rect.left()) / self.plot_rect.width() * self.span
            lines = ['附近实时采样（北京时间）']
            for exchange in EXCHANGES:
                points = [point for point in self.points if point.exchange == exchange]
                if points:
                    point = min(points, key=lambda point: abs(point.timestamp - timestamp))
                    date = datetime.fromtimestamp(point.timestamp / 1000, ZONE).strftime('%m-%d %H:%M:%S')
                    lines.append(f'{NAMES[exchange]}：{point.rate * 100:.6f}% · {date}')
            self.setToolTip('\n'.join(lines))
        else:
            self.setToolTip('')
        super().mouseMoveEvent(event)


class FundingChart(QFrame):
    def __init__(self, instrument):
        super().__init__()
        self.instrument = instrument
        self.setObjectName('fundingChart')
        self.setMinimumHeight(250)
        self.setAccessibleName(f'{instrument} 资金费率曲线')
        self.store = FundingStore()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(4)
        heading = QHBoxLayout()
        title = QLabel('OKX 实时资金费率')
        title.setObjectName('muted')
        title.setToolTip('每秒采集当前费率，按本机采样时间直接绘制，不分桶、不累加。保留最近7天，时间轴随已采集数据自动展开，最少显示1小时。')
        heading.addWidget(title)
        heading.addStretch()
        for exchange in EXCHANGES:
            legend = QLabel('━ ' + NAMES[exchange])
            legend.setStyleSheet(f'color: {COLORS[exchange]}; font-size: 12px;')
            heading.addWidget(legend)
        layout.addLayout(heading)
        self.statuses = {exchange: '加载中' for exchange in EXCHANGES}
        self.status_label = QLabel()
        self.status_label.setObjectName('muted')
        self.status_label.setWordWrap(True)
        self.status_label.setMaximumHeight(38)
        layout.addWidget(self.status_label)
        self.plot = FundingPlot()
        layout.addWidget(self.plot, 1)
        self.refresh()
        self.worker = FundingWorker(instrument, self.store.path, self)
        self.worker.updated.connect(self.refresh)
        self.worker.status.connect(self.update_status)
        self.worker.start()
        self.update_status('okx', '加载中')
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()

    def refresh(self):
        self.plot.now = int(time.time() * 1000)
        self.plot.points = [point for point in self.store.load(self.instrument, self.plot.now)
                            if point.exchange in EXCHANGES]
        self.plot.update()

    def update_status(self, exchange, status):
        self.statuses[exchange] = status
        self.status_label.setText('　'.join(self.statuses[name] for name in EXCHANGES))
        self.status_label.setToolTip(self.status_label.text())

    def shutdown(self):
        self.refresh_timer.stop()
        self.worker.stop()
        return self.worker.wait(7000)

    def closeEvent(self, event):
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
