"""Compact OKX-style position cards."""
from decimal import Decimal, InvalidOperation, localcontext

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout

CARD_STYLE = """
QFrame#positionCard { background: #fcfcfc; border: 0; border-bottom: 1px solid #e0e1e3; border-radius: 0; }
QFrame#positionCard QLabel { color: #16191d; background: transparent; border: 0; }
QFrame#positionCard QLabel#positionCaption { color: #7b818a; }
QFrame#positionCard QLabel#positionTag { background: #ededee; border-radius: 5px; padding: 3px 7px; }
"""


def number(value, *, decimals=8, percentage=False, trim=True):
    """Render API decimals without float rounding or invented values."""
    try:
        amount = Decimal(value)
        if not amount.is_finite():
            return "--"
        with localcontext() as context:
            context.prec = max(40, len(amount.as_tuple().digits) + abs(amount.adjusted()) + 12)
            if percentage:
                amount *= 100
            amount = amount.quantize(Decimal(1).scaleb(-decimals))
        if not amount:
            amount = abs(amount)
        text = f"{amount:,.{decimals}f}"
        if decimals and trim:
            text = text.rstrip("0").rstrip(".")
        return text + ("%" if percentage else "")
    except (InvalidOperation, ValueError, TypeError):
        return "--"


def profit_color(value):
    try:
        amount = Decimal(value.split()[0])
        if amount.is_finite():
            return "#c5476b" if amount < 0 else "#36a269" if amount > 0 else "#16191d"
    except (InvalidOperation, ValueError, TypeError, IndexError):
        pass
    return "#16191d"


class PositionCard(QFrame):
    def __init__(self, position, parent=None):
        super().__init__(parent)
        self.position = position
        self.setObjectName("positionCard")
        self.setStyleSheet(CARD_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 18)
        layout.setSpacing(16)
        top = QHBoxLayout()
        identity = QVBoxLayout()
        identity.setSpacing(8)
        self.title_label = self.label(position.title, "positionTitle")
        identity.addWidget(self.title_label)
        badges = QHBoxLayout()
        badges.setSpacing(6)
        self.side_label = self.label("卖" if position.sell else "买", "positionTag")
        self.side_label.setStyleSheet(
            "color: #c5476b; background: #f9e9ee;" if position.sell else
            "color: #36a269; background: #e8f4ec;"
        )
        badges.addWidget(self.side_label)
        badges.addWidget(self.label(position.margin_mode, "positionTag"))
        badges.addWidget(self.label(position.leverage.replace("×", "x"), "positionTag"))
        badges.addStretch()
        identity.addLayout(badges)
        top.addLayout(identity)
        top.addStretch()
        profit = QVBoxLayout()
        profit.setSpacing(8)
        currency = position.unrealized.split(maxsplit=1)
        profit.addWidget(self.label(f"收益额 ({currency[1] if len(currency) > 1 else position.margin_currency or '--'})", "positionCaption", right=True))
        raw_profit = currency[0] if currency else "--"
        self.profit_label = self.label(
            f"{number(raw_profit, decimals=2, trim=False)} ({number(position.unrealized_ratio, decimals=2, percentage=True, trim=False)})",
            "positionProfit", right=True,
        )
        self.profit_label.setStyleSheet(f"color: {profit_color(position.unrealized)};")
        self.profit_label.setToolTip(f"未实现收益：{position.unrealized}\n未实现收益率：{position.unrealized_ratio}")
        profit.addWidget(self.profit_label)
        top.addLayout(profit)
        layout.addLayout(top)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(18)
        self.values = {}
        fields = [
            ("quantity", f"持仓量 ({position.quantity_currency})", position.quantity_value, 8, False),
            ("margin", f"保证金 ({position.margin_currency or '--'})", position.margin, 2, False),
            ("maintenance", "维持保证金率", position.maintenance_ratio, 2, True),
            ("average", "开仓均价", position.average, position.price_decimals, False),
            ("liquidation", "预估强平价", position.liquidation, position.price_decimals, False),
        ]
        for index, (key, caption, raw, decimals, percentage) in enumerate(fields):
            cell = QVBoxLayout()
            cell.setSpacing(4)
            right = index % 3 == 2
            cell.addWidget(self.label(caption, "positionCaption", right=right))
            value = self.label(number(raw, decimals=decimals, percentage=percentage, trim=key not in ("margin", "maintenance")), "positionValue", right=right)
            value.setToolTip(str(raw))
            self.values[key] = value
            cell.addWidget(value)
            grid.addLayout(cell, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)

    @staticmethod
    def label(text, name, *, right=False):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setObjectName(name)
        if right:
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return label
