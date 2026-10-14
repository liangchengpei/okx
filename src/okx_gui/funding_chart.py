"""OKX live funding-rate curve."""
from bisect import bisect_left
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from okx_gui.funding import EXCHANGES, RETENTION_MS, FundingStore, FundingWorker, PriceStore

COLORS = {'okx': '#2679d8'}
NAMES = {'okx': 'OKX'}
PRICE_COLOR = '#e58a28'
ZONE = ZoneInfo('Asia/Shanghai')


def render_points(points, width):
    """Bound path size by pixels, retaining extrema and time order in each group."""
    step = max(1, len(points) // max(1, int(width)))
    if step == 1:
        return points
    result = []
    for start in range(0, len(points), step):
        group = points[start:start + step]
        candidates = [group[0], min(group, key=lambda p: p.rate),
                      max(group, key=lambda p: p.rate), group[-1]]
        # Also retain gap edges so outages are not hidden by display reduction.
        for previous, current in zip(group, group[1:]):
            if current.timestamp - previous.timestamp > 5 * 60000:
                candidates.extend((previous, current))
        result.extend(sorted(set(candidates), key=lambda p: p.timestamp))
    return result


class FundingPlot(QWidget):
    def __init__(self):
        super().__init__()
        self.points = []
        self.price_points = []
        self.now = int(time.time() * 1000)
        self.plot_rect = QRectF()
        self.manual_span = None
        self.y_scales = {'funding': 1.0, 'price': 1.0}
        self.y_offsets = {'funding': 0.0, 'price': 0.0}
        self.y_ranges = {'funding': (-1, 1), 'price': (0, 1)}
        self.view_end = None
        self._drag = None
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setAccessibleName('OKX 实时资金费率曲线')

    @property
    def span(self):
        if self.manual_span is not None:
            return self.manual_span
        earliest = min(self.points[0].timestamp if self.points else self.now,
                       self.price_points[0].timestamp if self.price_points else self.now)
        return min(RETENTION_MS, max(3600000, self.now - earliest))

    @property
    def end_time(self):
        return self.now if self.view_end is None else self.view_end

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.plot_rect.contains(event.position()):
            self.manual_span = self.span
            self._drag = (event.position(), self.end_time, dict(self.y_offsets), dict(self.y_ranges))
            self.setCursor(Qt.ClosedHandCursor)
            self.setToolTip('')
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag is not None:
            self._drag = None
            self.unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.view_end = None
            self.y_offsets = {'funding': 0.0, 'price': 0.0}
            self.update()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        # Keep the latest time at the right edge, as new samples arrive.
        steps = max(-20, min(20, delta / 120))
        if event.modifiers() & Qt.ControlModifier:
            midpoint = self.plot_rect.center().x() if not self.plot_rect.isEmpty() else self.width() / 2
            axis = 'funding' if event.position().x() < midpoint else 'price'
            self.y_scales[axis] = max(0.001, min(1000.0, self.y_scales[axis] * 0.8 ** steps))
        else:
            self.manual_span = max(60000, min(RETENTION_MS, int(self.span * 0.8 ** steps)))
        self.setToolTip('')
        self.update()
        event.accept()

    def scaled_y_range(self, axis, low, high):
        center = (low + high) / 2 + self.y_offsets[axis]
        half = (high - low) * self.y_scales[axis] / 2
        return center - half, center + half

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('white'))
        font = self.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 1))
        painter.setFont(font)
        rect = QRectF(72, 24, max(1, self.width() - 164), max(1, self.height() - 66))
        self.plot_rect = rect
        span = self.span
        end_time = self.end_time
        start_index = max(0, bisect_left(self.points, end_time - span,
                                       key=lambda point: point.timestamp) - 1)
        visible_points = render_points(self.points[start_index:bisect_left(self.points, end_time + 1, key=lambda p: p.timestamp)], rect.width())
        price_start = max(0, bisect_left(self.price_points, end_time - span,
                                       key=lambda point: point.timestamp) - 1)
        prices = render_points(self.price_points[price_start:bisect_left(self.price_points, end_time + 1, key=lambda p: p.timestamp)], rect.width())
        price_values = [float(point.price) for point in prices]
        price_low, price_high = (min(price_values), max(price_values)) if price_values else (0, 1)
        price_padding = max((price_high - price_low) * 0.12, abs(price_high) * 0.0001, 0.000001)
        price_low, price_high = price_low - price_padding, price_high + price_padding
        values = [float(point.rate * 100) for point in visible_points]
        low, high = min(values + [0]), max(values + [0])
        padding = max((high - low) * 0.12, 0.001)
        low, high = self.scaled_y_range('funding', low - padding, high + padding)
        price_low, price_high = self.scaled_y_range('price', price_low, price_high)
        self.y_ranges = {'funding': (low, high), 'price': (price_low, price_high)}
        for i in range(5):
            y = rect.bottom() - i * rect.height() / 4
            value = low + i * (high - low) / 4
            painter.setPen(QPen(QColor('#e9ebee'), 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor('#7b818a'))
            painter.drawText(QRectF(0, y - 9, 66, 18), Qt.AlignRight | Qt.AlignVCenter, f'{value:.4f}%')
            painter.setPen(QColor(PRICE_COLOR))
            price_value = price_low + i * (price_high - price_low) / 4
            label = f'{price_value:,.6f}'.rstrip('0').rstrip('.') if prices else '--'
            painter.drawText(QRectF(rect.right() + 6, y - 9, 82, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, label)
        painter.setPen(QColor('#7b818a'))
        for i in range(5):
            timestamp = end_time - span + span * i / 4
            label = datetime.fromtimestamp(timestamp / 1000, ZONE).strftime('%H:%M')
            x = rect.left() + i * rect.width() / 4
            painter.drawText(QRectF(x - 24, rect.bottom() + 4, 48, 34), Qt.AlignCenter, label)
        painter.save()
        painter.setClipRect(rect.adjusted(-2, -2, 2, 2))
        for exchange in EXCHANGES:
            points = [point for point in visible_points if point.exchange == exchange]
            if not points:
                continue
            path = QPainterPath()
            painter.setPen(QPen(QColor(COLORS[exchange]), 1.6))
            painter.setBrush(QColor(COLORS[exchange]))
            for i, point in enumerate(points):
                x = rect.left() + (point.timestamp - end_time + span) / span * rect.width()
                y = rect.bottom() - (float(point.rate * 100) - low) / (high - low) * rect.height()
                position = QPointF(x, y)
                if i and point.timestamp - points[i - 1].timestamp <= 5 * 60000:
                    path.lineTo(position)
                else:
                    path.moveTo(position)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        price_path = QPainterPath()
        for i, point in enumerate(prices):
            x = rect.left() + (point.timestamp - end_time + span) / span * rect.width()
            y = rect.bottom() - (float(point.price) - price_low) / (price_high - price_low) * rect.height()
            if i and point.timestamp - prices[i - 1].timestamp <= 5 * 60000:
                price_path.lineTo(QPointF(x, y))
            else:
                price_path.moveTo(QPointF(x, y))
        painter.setPen(QPen(QColor(PRICE_COLOR), 1.5))
        painter.drawPath(price_path)
        painter.restore()
        if not self.points and not self.price_points:
            painter.setPen(QColor('#7b818a'))
            painter.drawText(rect, Qt.AlignCenter, '等待实时资金费率，采集后逐步形成曲线')

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            origin, end_time, offsets, ranges = self._drag
            delta = event.position() - origin
            self.view_end = max(self.now - RETENTION_MS + self.span,
                                end_time - delta.x() / self.plot_rect.width() * self.span)
            for axis in ('funding', 'price'):
                low, high = ranges[axis]
                self.y_offsets[axis] = offsets[axis] + delta.y() / self.plot_rect.height() * (high - low)
            self.update()
            event.accept()
            return
        if self.plot_rect.contains(event.position()) and (self.points or self.price_points):
            timestamp = self.end_time - self.span + (event.position().x() - self.plot_rect.left()) / self.plot_rect.width() * self.span
            if timestamp > self.now:
                self.setToolTip('')
                return
            lines = ['附近实时采样（北京时间）']
            for exchange in EXCHANGES:
                points = self.points
                if points:
                    index = bisect_left(points, timestamp, key=lambda point: point.timestamp)
                    nearby = points[max(0, index - 1):index + 1]
                    point = min(nearby, key=lambda point: abs(point.timestamp - timestamp))
                    date = datetime.fromtimestamp(point.timestamp / 1000, ZONE).strftime('%m-%d %H:%M:%S')
                    lines.append(f'{NAMES[exchange]}：{point.rate * 100:.6f}% · {date}')
            if self.price_points:
                index = bisect_left(self.price_points, timestamp, key=lambda point: point.timestamp)
                point = min(self.price_points[max(0, index - 1):index + 1],
                            key=lambda point: abs(point.timestamp - timestamp))
                date = datetime.fromtimestamp(point.timestamp / 1000, ZONE).strftime('%m-%d %H:%M:%S')
                lines.append(f'价格：{point.price} · {date}')
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
        self.price_store = PriceStore(self.store.path)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(4)
        heading = QHBoxLayout()
        title = QLabel('OKX 实时资金费率')
        title.setObjectName('muted')
        title.setToolTip('每秒采集当前费率，按本机采样时间直接绘制，不分桶、不累加。保留最近7天，时间轴随已采集数据自动展开，默认最少显示1小时；滚轮向上放大、向下缩小，缩放范围1分钟至7天。Ctrl+滚轮缩放纵轴：左半侧为费率，右半侧为价格。按住左键平移，双击恢复最新时间和纵轴位置。')
        heading.addWidget(title)
        heading.addStretch()
        for exchange in EXCHANGES:
            legend = QLabel('━ 费率（左轴）')
            legend.setStyleSheet(f'color: {COLORS[exchange]}; font-size: 12px;')
            heading.addWidget(legend)
        price_legend = QLabel('━ 价格（右轴）')
        price_legend.setStyleSheet(f'color: {PRICE_COLOR}; font-size: 12px;')
        heading.addWidget(price_legend)
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
        self.worker.sampled.connect(self.append_sample)
        self.worker.price_sampled.connect(self.append_price)
        self.worker.status.connect(self.update_status)
        self.worker.start()
        self.update_status('okx', '加载中')
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self.advance_time)
        self.refresh_timer.start()

    def append_price(self, point):
        points = self.plot.price_points
        index = bisect_left(points, point.timestamp, key=lambda item: item.timestamp)
        if index < len(points) and points[index].timestamp == point.timestamp:
            points[index] = point
        else:
            points.insert(index, point)
        self.advance_time()

    def append_sample(self, point):
        if point.exchange not in EXCHANGES:
            return
        points = self.plot.points
        if not points or point.timestamp > points[-1].timestamp:
            points.append(point)
        else:
            index = bisect_left(points, point.timestamp, key=lambda item: item.timestamp)
            if index < len(points) and points[index].timestamp == point.timestamp:
                points[index] = point
            else:
                points.insert(index, point)
        self.advance_time()

    def advance_time(self):
        self.plot.now = int(time.time() * 1000)
        expired = bisect_left(self.plot.points, self.plot.now - RETENTION_MS,
                              key=lambda point: point.timestamp)
        if expired:
            del self.plot.points[:expired]
        expired_prices = bisect_left(self.plot.price_points, self.plot.now - RETENTION_MS,
                                     key=lambda point: point.timestamp)
        if expired_prices:
            del self.plot.price_points[:expired_prices]
        self.plot.update()

    def refresh(self):
        self.plot.now = int(time.time() * 1000)
        self.plot.price_points = self.price_store.load(self.instrument, self.plot.now)
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
