"""Local order drafting UI. No order or leverage API is called here."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QTabBar, QVBoxLayout, QWidget,
)


def amount(text, label):
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError(f'{label}请输入大于0的有效数字。') from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f'{label}请输入大于0的有效数字。')
    return value


@dataclass(frozen=True)
class OrderDraft:
    instrument: str
    action: str
    direction: str
    margin: str
    leverage: int
    order_type: str
    quantity: Decimal
    price: Decimal | None


class OrderEntry(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.quotes = {}
        self.setStyleSheet('''
            QComboBox, QSpinBox { border: 1px solid #d8dadd; border-radius: 6px;
                                 padding: 6px; background: white; }
            QTabBar::tab { padding: 9px 16px; background: #f0f1f2; color: #7b818a; }
            QTabBar::tab:selected { background: white; color: #111; border-bottom: 2px solid #111; }
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.contract = QComboBox()
        self.contract.setAccessibleName('下单合约')
        self.contract.setMinimumWidth(0)
        self.contract.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.contract.setMinimumContentsLength(12)
        layout.addWidget(self.contract)
        self.quote = QLabel('最新价  --')
        self.quote.setObjectName('muted')
        layout.addWidget(self.quote)
        self.action = QTabBar()
        self.action.addTab('开仓')
        self.action.addTab('平仓')
        self.action.setExpanding(True)
        layout.addWidget(self.action)
        modes = QHBoxLayout()
        self.margin = QComboBox()
        self.margin.addItems(['全仓', '逐仓'])
        self.margin.setAccessibleName('保证金模式')
        modes.addWidget(self.margin, 1)
        self.leverage = QSpinBox()
        self.leverage.setRange(1, 125)
        self.leverage.setValue(10)
        self.leverage.setSuffix(' ×')
        self.leverage.setAccessibleName('杠杆倍数')
        self.leverage.setToolTip('本地订单草稿设置，未修改账户杠杆')
        modes.addWidget(self.leverage, 1)
        layout.addLayout(modes)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setVerticalSpacing(10)
        self.order_type = QComboBox()
        self.order_type.addItems(['限价', '市价'])
        form.addRow('委托类型', self.order_type)
        price_row = QWidget()
        price_layout = QHBoxLayout(price_row)
        price_layout.setContentsMargins(0, 0, 0, 0)
        price_layout.setSpacing(4)
        self.price = QLineEdit()
        self.price.setPlaceholderText('委托价格')
        self.price.setAccessibleName('委托价格')
        self.price.setMinimumWidth(0)
        self.price.setStyleSheet('padding: 8px;')
        self.latest = QPushButton('最新')
        self.latest.setStyleSheet('padding: 8px;')
        self.latest.setToolTip('填入最新行情价格')
        self.latest.clicked.connect(self.fill_latest)
        price_layout.addWidget(self.price, 1)
        price_layout.addWidget(self.latest)
        form.addRow('价格', price_row)
        self.quantity = QLineEdit()
        self.quantity.setPlaceholderText('输入数量（张）')
        self.quantity.setAccessibleName('委托数量（张）')
        self.quantity.setStyleSheet('padding: 8px;')
        form.addRow('数量（张）', self.quantity)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setObjectName('error')
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.note = QLabel('仅预览，不发送订单')
        self.note.setObjectName('muted')
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        self.buy = QPushButton()
        self.sell = QPushButton()
        for button, color in ((self.buy, '#15966b'), (self.sell, '#d34b4b')):
            button.setStyleSheet(f'background: {color}; color: white; border-radius: 8px; padding: 12px 4px;')
            buttons.addWidget(button, 1)
        self.buy.clicked.connect(lambda: self.preview('long'))
        self.sell.clicked.connect(lambda: self.preview('short'))
        layout.addLayout(buttons)
        layout.addStretch()
        self.action.currentChanged.connect(self.update_form)
        self.order_type.currentIndexChanged.connect(self.update_form)
        self.contract.currentTextChanged.connect(self.contract_changed)
        self.quote_timer = QTimer(self)
        self.quote_timer.setInterval(1000)
        self.quote_timer.timeout.connect(self.refresh_quote)
        self.quote_timer.start()
        self.update_form()

    def set_contracts(self, instruments):
        selected = self.contract.currentText()
        self.contract.blockSignals(True)
        self.contract.clear()
        self.contract.addItems(list(instruments))
        index = self.contract.findText(selected)
        if index >= 0:
            self.contract.setCurrentIndex(index)
        self.contract.blockSignals(False)
        self.quotes = {k: v for k, v in self.quotes.items() if self.contract.findText(k) >= 0}
        if self.contract.currentText() != selected:
            self.contract_changed()
        else:
            self.update_form()

    def contract_changed(self):
        # Never carry a BTC limit price into another contract.
        self.price.clear()
        self.quantity.clear()
        self.error.hide()
        self.refresh_quote()
        self.update_form()

    def update_quote(self, instrument, value):
        try:
            price = amount(str(value), '行情价格')
        except ValueError:
            return
        self.quotes[instrument] = (price, time.monotonic())
        if instrument == self.contract.currentText():
            self.refresh_quote()

    def current_quote(self):
        quote = self.quotes.get(self.contract.currentText())
        return quote[0] if quote and time.monotonic() - quote[1] <= 10 else None

    def refresh_quote(self):
        price = self.current_quote()
        self.quote.setText(f'最新价  {price}' if price is not None else '最新价  --（等待行情）')
        self.latest.setEnabled(price is not None and self.order_type.currentIndex() == 0)

    def fill_latest(self):
        price = self.current_quote()
        if price is not None:
            self.price.setText(str(price))

    def update_form(self):
        opening = self.action.currentIndex() == 0
        self.buy.setText('预览开多' if opening else '预览平多')
        self.sell.setText('预览开空' if opening else '预览平空')
        self.buy.setEnabled(bool(self.contract.currentText()))
        self.sell.setEnabled(bool(self.contract.currentText()))
        limit = self.order_type.currentIndex() == 0
        self.price.setEnabled(limit)
        self.price.setPlaceholderText('委托价格' if limit else '按市场价格成交')
        self.refresh_quote()
        self.error.hide()

    def build_draft(self, direction):
        if not self.contract.currentText():
            raise ValueError('请先添加并选择合约。')
        if direction not in ('long', 'short'):
            raise ValueError('请选择有效方向。')
        quantity = amount(self.quantity.text(), '数量')
        price = amount(self.price.text(), '价格') if self.order_type.currentIndex() == 0 else None
        return OrderDraft(self.contract.currentText(), 'open' if self.action.currentIndex() == 0 else 'close',
                          direction, 'cross' if self.margin.currentIndex() == 0 else 'isolated',
                          self.leverage.value(), 'limit' if price is not None else 'market', quantity, price)

    def preview(self, direction):
        try:
            draft = self.build_draft(direction)
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.error.hide()
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle('订单预览')
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        action = ('开' if draft.action == 'open' else '平') + ('多' if direction == 'long' else '空')
        for label, value in [('合约', draft.instrument), ('方向', action),
                             ('保证金模式', self.margin.currentText()), ('杠杆', f'{draft.leverage} ×'),
                             ('委托类型', self.order_type.currentText()),
                             ('价格', str(draft.price) if draft.price is not None else '市场价格'),
                             ('数量', f'{draft.quantity} 张')]:
            form.addRow(label, QLabel(value))
        layout.addLayout(form)
        note = QLabel('本次仅预览，不发送订单。\n合约最小数量、步长和价格精度尚未校验；\n杠杆及保证金模式未应用到账户。')
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(dialog.close)
        layout.addWidget(close)
        dialog.setModal(True)
        dialog.show()
