"""OKX live price curve (legacy module name preserves application imports)."""
from bisect import bisect_left
from datetime import datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time
import math
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from okx_gui.account_ratio import RatioStore, RatioWorker
from okx_gui.funding import RETENTION_MS, FundingWorker, PriceStore, PricePoint

RATIO_COLOR = '#2679d8'
PRICE_COLOR = '#7b818a'
UP_COLOR = '#15966b'
DOWN_COLOR = '#d94b55'
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


@dataclass(frozen=True)
class PriceCandle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @property
    def price(self):
        return self.close


def aggregate_prices(points, minutes):
    """OHLC from ordered local price samples; missing intervals stay empty."""
    if minutes not in (1, 15):
        raise ValueError('价格聚合周期只支持1或15分钟')
    interval = minutes * 60000
    rows = []
    for point in points:
        start = point.timestamp // interval * interval
        if rows and rows[-1][0] == start:
            row = rows[-1]
            row[2] = max(row[2], point.price)
            row[3] = min(row[3], point.price)
            row[4] = point.price
        else:
            rows.append([start, point.price, point.price, point.price, point.price])
    return [PriceCandle(*row) for row in rows]


def price_color(previous, current):
    return UP_COLOR if current > previous else DOWN_COLOR if current < previous else PRICE_COLOR


def format_price(value):
    text = format(value, ',f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def axis_price_label(value, step):
    # Keep large-price axes compact, but distinguish ticks on small-price assets.
    decimals = max(1, -math.floor(math.log10(abs(step)))) if step else 1
    return f'{value:,.{decimals}f}'


def visible_extremes(candles, start, end):
    visible = [c for c in candles if start <= c.timestamp <= end]
    if not visible:
        return None
    return max(visible, key=lambda c: c.high), min(visible, key=lambda c: c.low)


class FundingPlot(QWidget):
    def __init__(self):
        super().__init__()
        self.price_points = []
        self.ratio_points = []
        self.minutes = 1
        self.live_price = None
        self.now = int(time.time() * 1000)
        self.plot_rect = QRectF()
        self.manual_span = None
        self.y_scales = {'price': 1.0, 'ratio': 1.0}
        self.y_offsets = {'price': 0.0, 'ratio': 0.0}
        self.y_ranges = {'price': (0, 1), 'ratio': (0, 1)}
        self.view_end = None
        self.resume_follow_at = None
        self._drag = None
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setAccessibleName('OKX 实时价格K线')

    @property
    def latest_price(self):
        return self.live_price if self.live_price is not None else (
            self.price_points[-1].price if self.price_points else None)

    @property
    def span(self):
        if self.manual_span is not None:
            return self.manual_span
        return 3600000  # Default view stays at the latest hour, regardless of cache length.

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
            self.resume_follow_at = self.view_end if self.view_end is not None and self.view_end > self.now else None
            self.unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.view_end = None
            self.resume_follow_at = None
            self.y_offsets = {'price': 0.0, 'ratio': 0.0}
            self.update()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def resume_follow_if_ready(self):
        # Preserve history browsing and active drags. Only a future-space pan
        # resumes following when incoming prices have filled that space.
        if (self._drag is None and self.resume_follow_at is not None
                and self.price_points and self.price_points[-1].timestamp >= self.resume_follow_at):
            self.view_end = None
            self.resume_follow_at = None

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        # Keep the latest time at the right edge, as new samples arrive.
        steps = max(-20, min(20, delta / 120))
        if event.modifiers() & Qt.ControlModifier:
            axis = 'ratio' if event.position().x() < self.plot_rect.center().x() else 'price'
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

    def visible_prices(self, end_time, span):
        interval = self.minutes * 60000
        start = (end_time - span) // interval * interval - interval
        first = bisect_left(self.price_points, start, key=lambda p: p.timestamp)
        last = bisect_left(self.price_points, min(end_time, self.now) + 1, key=lambda p: p.timestamp)
        return aggregate_prices(self.price_points[first:last], self.minutes)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('white'))
        font = self.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 1))
        painter.setFont(font)
        latest = self.latest_price
        badge_width = max(88, painter.fontMetrics().horizontalAdvance(format_price(latest)) + 22) if latest is not None else 88
        rect = QRectF(70, 24, max(1, self.width() - 78 - badge_width), max(1, self.height() - 66))
        self.plot_rect = rect
        span = self.span
        end_time = self.end_time
        prices = self.visible_prices(end_time, span)
        price_values = [float(value) for candle in prices if end_time - span <= candle.timestamp <= end_time
                        for value in (candle.low, candle.high)]
        price_low, price_high = (min(price_values), max(price_values)) if price_values else (0, 1)
        price_padding = max((price_high - price_low) * 0.12, abs(price_high) * 0.0001, 0.000001)
        price_low, price_high = price_low - price_padding, price_high + price_padding
        price_low, price_high = self.scaled_y_range('price', price_low, price_high)
        ratio_start = max(0, bisect_left(self.ratio_points, end_time - span, key=lambda p: p.timestamp) - 1)
        ratio_end = bisect_left(self.ratio_points, min(end_time, self.now) + 1, key=lambda p: p.timestamp)
        ratios = self.ratio_points[ratio_start:ratio_end]
        values = [float(p.ratio) for p in ratios]
        ratio_low, ratio_high = (min(values), max(values)) if values else (0, 1)
        padding = max((ratio_high - ratio_low) * 0.12, abs(ratio_high) * 0.01, 0.01)
        ratio_low, ratio_high = self.scaled_y_range('ratio', ratio_low - padding, ratio_high + padding)
        self.y_ranges = {'price': (price_low, price_high), 'ratio': (ratio_low, ratio_high)}
        painter.setPen(QColor(RATIO_COLOR))
        painter.drawText(QRectF(0, 0, 68, 20), Qt.AlignCenter, '多 / 空')
        for i in range(5):
            y = rect.bottom() - i * rect.height() / 4
            painter.setPen(QPen(QColor('#e9ebee'), 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor('#7b818a'))
            painter.setPen(QColor(RATIO_COLOR))
            ratio_value = ratio_low + i * (ratio_high - ratio_low) / 4
            painter.drawText(QRectF(0, y - 9, 62, 18), Qt.AlignRight | Qt.AlignVCenter,
                             f'{ratio_value:.8g}' if ratios else '--')
            painter.setPen(QColor(PRICE_COLOR))
            price_value = price_low + i * (price_high - price_low) / 4
            label = axis_price_label(price_value, (price_high - price_low) / 4) if prices else '--'
            latest_y = (rect.bottom() - (float(latest) - price_low) / (price_high - price_low) * rect.height()
                        if latest is not None else None)
            badge_center = max(rect.top() + 10, min(latest_y, rect.bottom() - 10)) if latest_y is not None else None
            if badge_center is None or abs(y - badge_center) >= 22:
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
        bar_width = max(1.0, min(18.0, self.minutes * 60000 / span * rect.width() * 0.65))
        def price_y(value):
            return rect.bottom() - (float(value) - price_low) / (price_high - price_low) * rect.height()
        for candle in prices:
            x = rect.left() + (candle.timestamp - end_time + span) / span * rect.width()
            color = QColor(price_color(candle.open, candle.close))
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(x, price_y(candle.high)), QPointF(x, price_y(candle.low)))
            top = min(price_y(candle.open), price_y(candle.close))
            height = abs(price_y(candle.close) - price_y(candle.open))
            if height < 1:
                painter.drawLine(QPointF(x - bar_width / 2, top), QPointF(x + bar_width / 2, top))
            else:
                painter.fillRect(QRectF(x - bar_width / 2, top, bar_width, height), color)
        ratio_path = QPainterPath()
        for i, point in enumerate(ratios):
            x = rect.left() + (point.timestamp - end_time + span) / span * rect.width()
            y = rect.bottom() - (float(point.ratio) - ratio_low) / (ratio_high - ratio_low) * rect.height()
            if i and point.timestamp - ratios[i - 1].timestamp <= 300000:
                ratio_path.lineTo(QPointF(x, y))
            else:
                ratio_path.moveTo(QPointF(x, y))
        painter.setPen(QPen(QColor(RATIO_COLOR), 1.6))
        painter.drawPath(ratio_path)
        painter.restore()
        extremes = visible_extremes(prices, end_time - span, end_time)
        if extremes:
            for candle, value, is_high in ((extremes[0], extremes[0].high, True),
                                           (extremes[1], extremes[1].low, False)):
                x = rect.left() + (candle.timestamp - end_time + span) / span * rect.width()
                y = price_y(value)
                if not rect.top() <= y <= rect.bottom():
                    continue
                text = format_price(value)
                width = painter.fontMetrics().horizontalAdvance(text) + 8
                to_right = x + 20 + width <= rect.right()
                tip = x + 18 if to_right else x - 18
                label_x = tip if to_right else tip - width
                label_x = max(rect.left(), min(label_x, rect.right() - width))
                label_y = y - 22 if is_high else y + 4
                label_y = max(rect.top(), min(label_y, rect.bottom() - 20))
                painter.setPen(QPen(QColor('#444b55'), 1))
                painter.drawLine(QPointF(x, y), QPointF(tip, y))
                label_rect = QRectF(label_x, label_y, width, 20)
                painter.fillRect(label_rect, QColor(255, 255, 255, 225))
                painter.drawText(label_rect, Qt.AlignCenter, text)
        if latest is not None:
            y = price_y(latest)
            in_range = rect.top() <= y <= rect.bottom()
            badge_y = max(rect.top() + 10, min(y, rect.bottom() - 10))
            painter.setPen(QPen(QColor('#56616f'), 1, Qt.DashLine))
            if in_range and self.price_points:
                last_bucket = self.price_points[-1].timestamp // (self.minutes * 60000) * self.minutes * 60000
                candle_x = rect.left() + (last_bucket - end_time + span) / span * rect.width()
                line_start = candle_x + bar_width / 2
                if rect.left() <= candle_x <= rect.right() and line_start < rect.right():
                    painter.drawLine(QPointF(line_start, y), QPointF(rect.right(), y))
            painter.setPen(QPen(QColor('#56616f'), 1))
            badge = QRectF(rect.right() + 3, badge_y - 10, badge_width - 5, 20)
            painter.setBrush(QColor('white'))
            painter.drawRoundedRect(badge, 3, 3)
            prefix = '' if in_range else ('↑ ' if y < rect.top() else '↓ ')
            painter.drawText(badge, Qt.AlignCenter, prefix + format_price(latest))
        if not self.price_points and not self.ratio_points:
            painter.setPen(QColor('#7b818a'))
            painter.drawText(rect, Qt.AlignCenter, '等待实时价格，采集后逐步形成K线')

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            origin, end_time, offsets, ranges = self._drag
            delta = event.position() - origin
            self.view_end = max(self.now - RETENTION_MS + self.span,
                                end_time - delta.x() / self.plot_rect.width() * self.span)
            for axis in ('price', 'ratio'):
                low, high = ranges[axis]
                self.y_offsets[axis] = offsets[axis] + delta.y() / self.plot_rect.height() * (high - low)
            self.update()
            event.accept()
            return
        if self.plot_rect.contains(event.position()) and (self.price_points or self.ratio_points):
            timestamp = self.end_time - self.span + (event.position().x() - self.plot_rect.left()) / self.plot_rect.width() * self.span
            if timestamp > self.now:
                self.setToolTip('')
                return
            prices = self.visible_prices(self.end_time, self.span)
            bucket_start = int(timestamp // (self.minutes * 60000)) * self.minutes * 60000
            index = bisect_left(prices, bucket_start, key=lambda p: p.timestamp)
            if index < len(prices) and prices[index].timestamp == bucket_start:
                point = prices[index]
                date = datetime.fromtimestamp(point.timestamp / 1000, ZONE).strftime('%m-%d %H:%M')
                self.setToolTip(f'{self.minutes}分钟K线 · {date}（北京时间）\n'
                                f'开：{point.open}　高：{point.high}\n低：{point.low}　收：{point.close}'
                                + ('\n当前区间尚未结束' if bucket_start + self.minutes * 60000 > self.now else ''))
            else:
                self.setToolTip('')
            if self.ratio_points:
                index = bisect_left(self.ratio_points, timestamp, key=lambda p: p.timestamp)
                nearby = self.ratio_points[max(0, index - 1):index + 1]
                point = min(nearby, key=lambda p: abs(p.timestamp - timestamp), default=None)
                if point is not None and abs(point.timestamp - timestamp) <= 300000:
                    date = datetime.fromtimestamp(point.timestamp / 1000, ZONE).strftime('%m-%d %H:%M')
                    text = f'多头账户 / 空头账户：{point.ratio:.8g} · {date}'
                    self.setToolTip((self.toolTip() + '\n' if self.toolTip() else '') + text)

        else:
            self.setToolTip('')
        super().mouseMoveEvent(event)


class FundingChart(QFrame):
    def __init__(self, instrument):
        super().__init__()
        self.instrument = instrument
        self.setObjectName('fundingChart')
        self.setMinimumHeight(250)
        self.setAccessibleName(f'{instrument} 实时价格K线')
        self.price_store = PriceStore()
        self.ratio_store = RatioStore()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(4)
        heading = QHBoxLayout()
        title = QLabel('OKX 价格K线')
        title.setObjectName('muted')
        title.setToolTip('每秒采样实时价格，按1或15分钟聚合开、高、低、收；实体为开盘到收盘，上下影线为最高和最低价。收盘高于开盘为绿色，低于为红色，持平灰色。基于本地每秒采样，不是交易所完整成交K线；当前区间持续更新。保留最近7天。滚轮缩放时间轴，Ctrl+滚轮在左半侧缩放多空账户比、右半侧缩放价格。按住左键平移，可拖至未来空白时间；双击恢复最新时间和纵轴位置。')
        heading.addWidget(title)
        heading.addStretch()
        for text, color in (('━ 上涨', UP_COLOR), ('━ 下跌', DOWN_COLOR), ('━ 多/空（左轴）', RATIO_COLOR)):
            legend = QLabel(text)
            legend.setStyleSheet(f'color: {color}; font-size: 12px;')
            heading.addWidget(legend)
        self.interval = QComboBox()
        self.interval.setAccessibleName('价格K线聚合周期')
        for minutes in (1, 15):
            self.interval.addItem(f'{minutes} 分钟', minutes)
        heading.addWidget(self.interval)
        layout.addLayout(heading)
        self.ratio_status = QLabel('多空账户比：等待数据（5分钟更新）')
        self.ratio_status.setObjectName('muted')
        self.ratio_status.setTextFormat(Qt.PlainText)
        self.ratio_status.setFixedHeight(self.ratio_status.fontMetrics().lineSpacing() + 4)
        self.ratio_status.setToolTip('多头账户数量 ÷ 空头账户数量；大于1表示多头账户更多。不是账户总数或资金费率。')
        layout.addWidget(self.ratio_status)
        self.plot = FundingPlot()
        self.interval.currentIndexChanged.connect(self.change_interval)
        layout.addWidget(self.plot, 1)
        self.refresh()
        self.worker = FundingWorker(instrument, self.price_store.path, self)
        self.worker.price_sampled.connect(self.append_price)
        self.worker.start()
        self.ratio_worker = RatioWorker(instrument, self.ratio_store.path, self)
        self.ratio_worker.sampled.connect(self.append_ratios)
        self.ratio_worker.status.connect(self.update_ratio_status)
        self.ratio_worker.start()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self.advance_time)
        self.refresh_timer.start()

    def append_ratios(self, points):
        merged = {p.timestamp: p for p in self.plot.ratio_points}
        merged.update({p.timestamp: p for p in points})
        self.plot.ratio_points = sorted(merged.values(), key=lambda p: p.timestamp)
        self.advance_time()
        self.update_ratio_status('')

    def update_ratio_status(self, error):
        if error:
            self.ratio_status.setText(error)
        elif self.plot.ratio_points:
            point = self.plot.ratio_points[-1]
            timestamp = datetime.fromtimestamp(point.timestamp / 1000, ZONE).strftime('%m-%d %H:%M')
            self.ratio_status.setText(f'多 / 空：{point.ratio:.8g} · {timestamp} · 5分钟更新')
        else:
            self.ratio_status.setText('多空账户比：暂无数据（5分钟更新）')

    def set_price(self, value):
        self.worker.set_price(value)
        try:
            price = Decimal(str(value))
        except InvalidOperation:
            return
        if price.is_finite() and price > 0:
            self.plot.live_price = price
            self.plot.update()

    def change_interval(self):
        self.plot.minutes = self.interval.currentData()
        self.plot.setToolTip('')
        self.plot.update()

    def append_price(self, point):
        points = self.plot.price_points
        index = bisect_left(points, point.timestamp, key=lambda item: item.timestamp)
        if index < len(points) and points[index].timestamp == point.timestamp:
            points[index] = point
        else:
            points.insert(index, point)
        self.advance_time()

    def advance_time(self):
        self.plot.now = int(time.time() * 1000)
        self.plot.resume_follow_if_ready()
        expired = bisect_left(self.plot.ratio_points, self.plot.now - RETENTION_MS, key=lambda p: p.timestamp)
        if expired:
            del self.plot.ratio_points[:expired]
        expired_prices = bisect_left(self.plot.price_points, self.plot.now - RETENTION_MS,
                                     key=lambda point: point.timestamp)
        if expired_prices:
            del self.plot.price_points[:expired_prices]
        self.plot.update()

    def refresh(self):
        self.plot.now = int(time.time() * 1000)
        self.plot.price_points = self.price_store.load(self.instrument, self.plot.now)
        self.plot.ratio_points = self.ratio_store.load(self.instrument, self.plot.now)
        self.update_ratio_status('')
        self.plot.update()

    def shutdown(self):
        self.refresh_timer.stop()
        self.worker.stop()
        self.ratio_worker.stop()
        price_stopped = self.worker.wait(7000)
        ratio_stopped = self.ratio_worker.wait(7000)
        return price_stopped and ratio_stopped

    def closeEvent(self, event):
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
