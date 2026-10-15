"""Two-sided depth histogram: asks above zero, bids below zero."""
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QComboBox

from okx_gui.order_book import OrderBookWorker, aggregate_depth

ASK_COLOR = '#d94b55'
BID_COLOR = '#15966b'


def number(value):
    text = format(value, ',f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class OrderBookPlot(QWidget):
    def __init__(self):
        super().__init__()
        self.step = Decimal('0.1')
        self.snapshot = None
        self.unit = "币"
        self.bids, self.asks = (), ()
        self.manual_center = self.manual_span = None
        self.y_scale = 1.0
        self.pan_y = 0.0
        self._drag = None
        self.plot_rect = QRectF()
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setAccessibleName('OKX 订单本，卖盘红色向上，买盘绿色向下')

    def set_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.unit = snapshot.unit
        self.reaggregate()

    def reaggregate(self):
        if self.snapshot is not None:
            self.bids = aggregate_depth(self.snapshot.bids, self.step, 'buy')
            self.asks = aggregate_depth(self.snapshot.asks, self.step, 'sell')
        self.setToolTip('')
        self.update()

    def default_view(self):
        prices = [float(p) for p, _ in self.bids + self.asks]
        if not prices:
            return 0.0, float(self.step) * 20
        low, high = min(prices), max(prices)
        return (low + high) / 2, max((high - low) * 1.12, float(self.step) * 8)

    def view(self):
        center, span = self.default_view()
        return (center if self.manual_center is None else self.manual_center,
                span if self.manual_span is None else self.manual_span)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('white'))
        font = self.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 1))
        painter.setFont(font)
        rect = QRectF(62, 22, max(1, self.width() - 80), max(1, self.height() - 65))
        self.plot_rect = rect
        center, span = self.view()
        left, right = center - span / 2, center + span / 2
        visible = [float(q) for p, q in self.bids + self.asks if left <= float(p) <= right]
        peak = max(max(visible, default=1), 1e-12) * 1.15 * self.y_scale
        zero = rect.center().y() + self.pan_y
        half = rect.height() / 2
        painter.setPen(QColor('#7b818a'))
        painter.drawText(QRectF(4, 0, 200, 20), Qt.AlignLeft, f'挂单量（{self.unit}）')
        for i in range(5):
            y = rect.top() + i * rect.height() / 4
            value = (zero - y) / half * peak
            painter.setPen(QPen(QColor('#e9ebee'), 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor('#7b818a'))
            painter.drawText(QRectF(0, y - 9, 55, 18), Qt.AlignRight | Qt.AlignVCenter, f'{value:,.6g}')
            x = rect.left() + i * rect.width() / 4
            decimals = max(0, -self.step.normalize().as_tuple().exponent)
            label = f'{left + i * span / 4:,.{decimals}f}'
            painter.drawText(QRectF(max(0, min(x - 45, self.width() - 90)), rect.bottom() + 5, 90, 18), Qt.AlignCenter, label)
        painter.drawText(QRectF(rect.left(), rect.bottom() + 24, rect.width(), 16), Qt.AlignRight, '价格')
        painter.save()
        painter.setClipRect(rect)
        painter.setPen(QPen(QColor('#9399a1'), 1))
        painter.drawLine(QPointF(rect.left(), zero), QPointF(rect.right(), zero))
        width = max(0.5, float(self.step) / span * rect.width() * 0.8)
        for levels, color, direction in ((self.asks, ASK_COLOR, -1), (self.bids, BID_COLOR, 1)):
            for price, quantity in levels:
                x = rect.left() + (float(price) - left) / span * rect.width()
                if x + width / 2 < rect.left() or x - width / 2 > rect.right():
                    continue
                height = float(quantity) / peak * half
                top = zero - height if direction == -1 else zero
                painter.fillRect(QRectF(x - width / 2, top, width, height), QColor(color))
        painter.restore()
        if self.snapshot is None:
            painter.setPen(QColor('#7b818a'))
            painter.drawText(rect, Qt.AlignCenter, '等待 OKX 实时订单本')

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            delta = (event.pixelDelta().y() or event.pixelDelta().x()) * 3
        if not delta or self.plot_rect.isEmpty():
            event.ignore()
            return
        factor = 0.8 ** max(-20, min(20, delta / 120))
        if event.modifiers() & Qt.ControlModifier:
            self.y_scale = max(0.001, min(1000, self.y_scale * factor))
        else:
            center, span = self.view()
            self.manual_span = max(float(self.step) * 4, min(max(abs(center) * 10, float(self.step) * 10000), span * factor))
            ratio = (event.position().x() - self.plot_rect.center().x()) / self.plot_rect.width()
            self.manual_center = center + ratio * (span - self.manual_span)
        self.setToolTip('')
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.plot_rect.contains(event.position()):
            center, span = self.view()
            self.manual_span = span
            self._drag = (event.position(), center, self.pan_y)
            self.setCursor(Qt.ClosedHandCursor)
            self.setToolTip('')
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            origin, center, y = self._drag
            delta = event.position() - origin
            self.manual_center = center - delta.x() / self.plot_rect.width() * self.manual_span
            self.pan_y = y + delta.y()
            self.update()
            event.accept()
            return
        if self.plot_rect.contains(event.position()) and self.snapshot:
            center, span = self.view()
            price = center + (event.position().x() - self.plot_rect.center().x()) / self.plot_rect.width() * span
            candidates = [(p, q, '卖盘') for p, q in self.asks] + [(p, q, '买盘') for p, q in self.bids]
            nearest = min(candidates, key=lambda item: abs(float(item[0]) - price), default=None)
            if nearest and abs(float(nearest[0]) - price) <= max(float(self.step) / 2, span * 5 / self.plot_rect.width()):
                p, q, side = nearest
                self.setToolTip(f'{side} · 聚合价 {number(p)}\n挂单量：{number(q)} {self.unit}')
            else:
                self.setToolTip('')
        else:
            self.setToolTip('')
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag is not None:
            self._drag = None
            self.unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.manual_center = self.manual_span = None
            self.y_scale, self.pan_y = 1.0, 0.0
            self.update()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class OrderBookChart(QFrame):
    def __init__(self, instrument):
        super().__init__()
        self.setObjectName('orderBookChart')
        self.setMinimumHeight(280)
        self.setAccessibleName(f'{instrument} OKX 实时订单本')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(2)
        heading = QHBoxLayout()
        heading.addWidget(QLabel('OKX 实时订单本'))
        for text, color in (('■ 卖盘 ↑', ASK_COLOR), ('■ 买盘 ↓', BID_COLOR)):
            label = QLabel(text)
            label.setStyleSheet(f'color: {color}; border: 0;')
            heading.addWidget(label)
        heading.addStretch()
        self.aggregation = QComboBox()
        self.aggregation.setAccessibleName('订单本价格聚合间隔')
        for step in ('0.1', '1', '10', '50'):
            self.aggregation.addItem(step, Decimal(step))
        self.aggregation.setToolTip('按价格聚合：买盘向下取整，卖盘向上取整。每档累加当前挂单量，不累计成交量。')
        heading.addWidget(self.aggregation)
        layout.addLayout(heading)
        self.details = QLabel('买一 --　卖一 --')
        self.details.setObjectName('muted')
        layout.addWidget(self.details)
        self.details.setToolTip('OKX：连接中')
        self.plot = OrderBookPlot()
        self.plot.unit = instrument.split("-")[0]
        self.plot.setToolTip('左键拖动，滚轮缩放价格轴，Ctrl+滚轮缩放挂单量轴；双击恢复自动视图')
        layout.addWidget(self.plot, 1)
        self.aggregation.currentIndexChanged.connect(self.change_aggregation)
        self.worker = OrderBookWorker(instrument, self)
        self.worker.status.connect(self.details.setToolTip)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.worker.start()

    def change_aggregation(self):
        self.plot.step = self.aggregation.currentData()
        self.plot.reaggregate()

    def refresh(self):
        snapshot = self.worker.take_snapshot()
        if snapshot is not None:
            self.plot.set_snapshot(snapshot)
            bid = number(snapshot.bids[0][0]) if snapshot.bids else '--'
            ask = number(snapshot.asks[0][0]) if snapshot.asks else '--'
            timestamp = datetime.fromtimestamp(snapshot.timestamp / 1000, ZoneInfo('Asia/Shanghai')).strftime('%H:%M:%S')
            self.details.setText(f'买一 {bid}　卖一 {ask}　更新 {timestamp}')

    def shutdown(self):
        self.timer.stop()
        self.worker.stop()
        return self.worker.wait(7000)

    def closeEvent(self, event):
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
