"""Pending-order cards using the application's light theme and inherited font."""
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout
from okx_gui.position_widgets import CARD_STYLE, PositionCard


class OrderCard(QFrame):
    def __init__(self, order, parent=None):
        super().__init__(parent)
        self.order = order
        self.setObjectName('orderCard')
        self.setStyleSheet(CARD_STYLE.replace('positionCard', 'orderCard'))
        self.setToolTip(f'委托编号：{order.order_id}\n状态：{order.state}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 18)
        layout.setSpacing(16)
        self.title_label = PositionCard.label(order.title, 'positionTitle')
        layout.addWidget(self.title_label)
        badges = QHBoxLayout()
        badges.setSpacing(4)
        for value in (order.kind, order.side, order.margin_mode, order.leverage):
            if value == '--':
                continue
            badge = PositionCard.label(value, 'positionTag')
            if value == order.side:
                badge.setStyleSheet('color: #36a269; background: #e8f4ec;' if order.side == '买入' else 'color: #c5476b; background: #f9e9ee;')
            badges.addWidget(badge)
        self.time_label = PositionCard.label(order.created, 'positionCaption')
        badges.addWidget(self.time_label)
        badges.addStretch()
        layout.addLayout(badges)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(12)
        self.values = {}
        for index, (caption, text, raw) in enumerate(order.fields):
            cell = QVBoxLayout()
            cell.setSpacing(4)
            cell.addWidget(PositionCard.label(caption, 'positionCaption', right=index % 3 == 2))
            value = PositionCard.label(text, 'positionValue', right=index % 3 == 2)
            value.setToolTip(raw)
            self.values[caption] = value
            cell.addWidget(value)
            grid.addLayout(cell, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
