"""Order drafting, review, and explicit background submission."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time
import hashlib
import json

from PySide6.QtCore import Qt, QTimer, Signal, QSettings
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QStyle, QTabBar, QVBoxLayout, QWidget,
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
    leverage: Decimal | int
    order_type: str
    quantity: Decimal
    price: Decimal | None
    trigger_price: Decimal | None = None
    trigger_source: str = 'last'


class OrderEntry(QWidget):
    submitted = Signal(str)
    accepted_order = Signal(object, str)

    def __init__(self, parent=None, *, trading=False, confirmation_settings=None):
        super().__init__(parent)
        self.trading = trading
        self.confirmation_settings = confirmation_settings if confirmation_settings is not None else QSettings("okx-trader", "order-confirmation")
        self.busy = False
        self.blocked = False
        self.task = None
        self.rules_task = None
        self.rules_closing = False
        self.rules_key = None
        self.loaded_rules_key = None
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
        self.leverage = QComboBox()
        self.leverage.setEditable(False)
        self.leverage.setAccessibleName('杠杆倍数')
        self.leverage.addItem('等待合约规则', None)
        self.leverage.setEnabled(False)
        modes.addWidget(self.leverage, 1)
        self.reload_leverage = QPushButton('↻')
        self.reload_leverage.setAccessibleName('重新读取杠杆规则')
        self.reload_leverage.setToolTip('重新读取所选合约的杠杆规则')
        self.reload_leverage.setStyleSheet('padding: 6px;')
        self.reload_leverage.clicked.connect(self.load_leverage)
        modes.addWidget(self.reload_leverage)
        layout.addLayout(modes)
        self.leverage_hint = QLabel('')
        self.leverage_hint.setObjectName('muted')
        self.leverage_hint.setWordWrap(True)
        layout.addWidget(self.leverage_hint)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setVerticalSpacing(10)
        self.order_type = QComboBox()
        self.order_type.addItems(['限价', '市价', '计划委托'])
        form.addRow('委托类型', self.order_type)
        self.entry_form = form
        self.trigger_price = QLineEdit()
        self.trigger_price.setPlaceholderText('触发价格')
        self.trigger_price.setAccessibleName('计划委托触发价格')
        self.trigger_price.setStyleSheet('padding: 8px;')
        form.addRow('触发价格', self.trigger_price)
        self.trigger_source = QComboBox()
        for label, value in [('最新价', 'last'), ('标记价', 'mark'), ('指数价', 'index')]:
            self.trigger_source.addItem(label, value)
        form.addRow('触发依据', self.trigger_source)
        self.execution_type = QComboBox()
        self.execution_type.addItems(['限价', '市价'])
        form.addRow('触发后委托', self.execution_type)
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
        self.quantity.setPlaceholderText('输入币数')
        self.quantity.setAccessibleName('委托数量（币）')
        self.quantity.setStyleSheet('padding: 8px;')
        self.quantity_label = QLabel('数量（币）')
        form.addRow(self.quantity_label, self.quantity)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setObjectName('error')
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.diagnostics = QLabel()
        self.diagnostics.setTextFormat(Qt.PlainText)
        self.diagnostics.setWordWrap(True)
        self.diagnostics.hide()
        layout.addWidget(self.diagnostics)
        self.note = QLabel('仅预览，不发送订单')
        self.note.setObjectName('muted')
        note_row = QHBoxLayout()
        note_row.addWidget(self.note, 1)
        self.restore_confirmation = QPushButton()
        self.restore_confirmation.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.restore_confirmation.setFixedSize(28, 28)
        self.restore_confirmation.setStyleSheet('padding: 0; border-radius: 4px;')
        self.restore_confirmation.setToolTip('恢复下单确认')
        self.restore_confirmation.setAccessibleName('恢复下单确认')
        self.restore_confirmation.setVisible(trading)
        self.restore_confirmation.clicked.connect(self.reset_confirmation)
        note_row.addWidget(self.restore_confirmation)
        layout.addLayout(note_row)
        self.unlock = QPushButton('核对订单后解除锁定')
        self.unlock.hide()
        self.unlock.clicked.connect(self.unlock_submission)
        layout.addWidget(self.unlock)
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
        self.execution_type.currentIndexChanged.connect(self.update_form)
        self.contract.currentTextChanged.connect(self.contract_changed)
        self.margin.currentIndexChanged.connect(self.load_leverage)
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
        self.trigger_price.clear()
        self.quantity.clear()
        self.error.hide()
        self.refresh_quote()
        self.update_form()
        self.load_leverage()

    def current_rules_key(self):
        return (self.contract.currentText(), 'cross' if self.margin.currentIndex() == 0 else 'isolated')

    def load_leverage(self):
        self.rules_key = self.current_rules_key()
        self.loaded_rules_key = None
        self.leverage.clear()
        self.leverage.addItem('读取杠杆规则…', None)
        self.leverage.setEnabled(False)
        self.leverage_hint.setText('正在读取合约杠杆上限')
        if not self.rules_key[0]:
            self.leverage_hint.setText('请先选择合约')
            return
        if self.rules_task is None:
            self.start_leverage_task()

    def start_leverage_task(self):
        from okx_gui.trading import LeverageTask, fetch_leverage_options
        key = self.rules_key
        task = LeverageTask(lambda: fetch_leverage_options(*key, should_stop=lambda: task.isInterruptionRequested()), self)
        self.rules_task = task
        task.completed.connect(lambda result: self.apply_leverage_rules(key, result))
        task.failed.connect(lambda message, _: self.leverage_failed(key, message))
        def finished():
            self.rules_task = None
            task.deleteLater()
            if not self.rules_closing and self.rules_key != key and self.rules_key[0]:
                self.start_leverage_task()
        task.finished.connect(finished)
        task.start()

    def apply_leverage_rules(self, key, result):
        if key != self.current_rules_key():
            return
        maximum, current = result
        presets = (1, 2, 3, 5, 10, 20, 30, 50, 75, 100)
        choices = [Decimal(value) for value in presets if value <= maximum]
        self.leverage.clear()
        for value in choices:
            self.leverage.addItem(f'{value.normalize():f} ×', value)
        if current is not None and current not in choices:
            self.leverage.insertItem(0, '请选择倍数', None)
            self.leverage.setCurrentIndex(0)
        else:
            self.leverage.setCurrentIndex(choices.index(current) if current in choices else 0)
        self.loaded_rules_key = key
        self.leverage.setEnabled(self.action.currentIndex() == 0)
        self.leverage_hint.setText(f'合约上限 {maximum.normalize():f} × · 实际可用倍数受仓位档位限制')

    def leverage_failed(self, key, message):
        if key != self.current_rules_key():
            return
        self.leverage.clear()
        self.leverage.addItem('规则读取失败', None)
        self.leverage.setEnabled(False)
        self.leverage_hint.setText(message)

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
        self.latest.setEnabled(price is not None and self.uses_limit_price())

    def fill_latest(self):
        price = self.current_quote()
        if price is not None:
            self.price.setText(str(price))

    def uses_limit_price(self):
        return (self.order_type.currentIndex() == 0 or
                (self.order_type.currentIndex() == 2 and self.execution_type.currentIndex() == 0))

    def update_form(self):
        unit = self.contract.currentText().split('-')[0] or '币'
        self.quantity_label.setText(f'数量（{unit}）')
        self.quantity.setPlaceholderText(f'输入数量（{unit}）')
        self.quantity.setAccessibleName(f'委托数量（{unit}）')
        opening = self.action.currentIndex() == 0
        self.buy.setText(('开多' if opening else '平多') if self.trading else ('预览开多' if opening else '预览平多'))
        self.sell.setText(('开空' if opening else '平空') if self.trading else ('预览开空' if opening else '预览平空'))
        self.buy.setEnabled(bool(self.contract.currentText()) and not self.busy and not self.blocked)
        self.sell.setEnabled(bool(self.contract.currentText()) and not self.busy and not self.blocked)
        trigger = self.order_type.currentIndex() == 2
        for widget in (self.trigger_price, self.trigger_source, self.execution_type):
            self.entry_form.setRowVisible(widget, trigger)
        limit = self.uses_limit_price()
        self.price.setEnabled(limit)
        self.price.setPlaceholderText('委托价格' if limit else '按市场价格成交')
        self.refresh_quote()
        if self.trading:
            self.note.setText('点击下单后校验交易规则')
            self.leverage.setToolTip('确认开仓时设置账户杠杆；平仓不修改杠杆')
        self.leverage.setEnabled(opening and self.loaded_rules_key == self.current_rules_key())
        if not self.busy and not self.blocked:
            self.error.hide()

    def build_draft(self, direction):
        if not self.contract.currentText():
            raise ValueError('请先添加并选择合约。')
        if direction not in ('long', 'short'):
            raise ValueError('请选择有效方向。')
        quantity = amount(self.quantity.text(), '数量')
        price = amount(self.price.text(), '价格') if self.uses_limit_price() else None
        trigger = amount(self.trigger_price.text(), '触发价格') if self.order_type.currentIndex() == 2 else None
        if self.action.currentIndex() == 0 and (self.loaded_rules_key != self.current_rules_key() or self.leverage.currentData() is None):
            raise ValueError('请先成功读取该合约的杠杆规则，再选择倍数。')
        return OrderDraft(self.contract.currentText(), 'open' if self.action.currentIndex() == 0 else 'close',
                          direction, 'cross' if self.margin.currentIndex() == 0 else 'isolated',
                          self.leverage.currentData() or Decimal(1),
                          'trigger' if trigger is not None else ('limit' if price is not None else 'market'),
                          quantity, price, trigger, self.trigger_source.currentData())

    def preview(self, direction):
        if self.trading and (self.busy or self.blocked):
            return
        try:
            draft = self.build_draft(direction)
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.error.hide()
        if self.trading:
            if self.busy or self.blocked:
                return
            from okx_gui.trading import prepare_order
            self.start_task(lambda: prepare_order(draft), self.order_prepared)
            return
        self.show_confirmation(None, draft)

    def confirmation_key(self, prepared):
        credentials = prepared.credentials
        identity = f'{credentials.domain}|{credentials.flag}|{credentials.api_key}'
        return 'skip/' + hashlib.sha256(identity.encode()).hexdigest()

    def reset_confirmation(self):
        self.confirmation_settings.remove('skip')
        self.confirmation_settings.sync()
        self.note.setText('已恢复下单确认')

    def order_prepared(self, prepared):
        from okx_gui.order_audit import order_diagnostics, audit
        info = order_diagnostics(prepared)
        text = (f"{prepared.environment} · instId={info['instId']} · tdMode={info['tdMode']}\n"
                f"posSide={info['posSide']} · side={info['side']} · px={info['px']} · sz={info['sz']} 张\n"
                f"目标杠杆 {info['targetLeverage']}× · 预计保证金 {info['estimatedMargin']} {info['marginCurrency']}\n"
                '仅按委托价估算，不含手续费、已有仓位抵扣及交易所风险调整。')
        usdt = prepared.balance_snapshot.get('USDT', {})
        if usdt:
            text += f"\nUSDT 可用权益 {usdt.get('availEq')} · 冻结余额 {usdt.get('frozenBal')}"
        elif prepared.balance_snapshot.get('error'):
            text += '\n余额读取失败，无法确认可用保证金。'
        self.diagnostics.setText(text)
        self.diagnostics.setToolTip(json.dumps(info, ensure_ascii=False))
        self.diagnostics.show()
        if not audit('order_prepared', info, prepared.credentials):
            self.diagnostics.setText(text + '\n诊断日志写入失败。')
        if self.confirmation_settings.value(self.confirmation_key(prepared), False, type=bool):
            from okx_gui.trading import submit_order
            self.start_task(lambda: submit_order(prepared), lambda order_id: self.order_sent(order_id, prepared))
        else:
            self.show_confirmation(prepared)

    def show_confirmation(self, prepared, draft=None):
        draft = prepared.draft if prepared is not None else draft
        direction = draft.direction
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle('确认订单 · ' + prepared.environment if prepared is not None else '订单预览')
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        action = ('开' if draft.action == 'open' else '平') + ('多' if direction == 'long' else '空')
        for label, value in [('合约', draft.instrument), ('方向', action),
                             ('保证金模式', '全仓' if draft.margin == 'cross' else '逐仓'),
                             ('杠杆', f'{draft.leverage} ×' if draft.action == 'open' else '沿用现有持仓'),
                             ('委托类型', {'limit': '限价', 'market': '市价', 'trigger': '计划委托'}[draft.order_type]),
                             ('价格', str(draft.price) if draft.price is not None else '市场价格'),
                             ('数量', f'{draft.quantity} {draft.instrument.split("-")[0]}')]:
            form.addRow(label, QLabel(value))
        if draft.order_type == 'trigger':
            form.addRow('触发价格', QLabel(str(draft.trigger_price)))
            form.addRow('触发依据', QLabel({'last': '最新价', 'mark': '标记价', 'index': '指数价'}[draft.trigger_source]))
            form.addRow('触发后委托', QLabel('限价' if draft.price is not None else '市价'))
        layout.addLayout(form)
        if prepared is not None:
            from okx_gui.order_audit import order_diagnostics
            info = order_diagnostics(prepared)
            form.addRow('API 数量 sz', QLabel(str(prepared.contract_quantity) + ' 张'))
            form.addRow('预计保证金（未含费用）', QLabel(info['estimatedMargin'] + ' ' + info['marginCurrency']))
            form.addRow('交易环境', QLabel(prepared.environment))
            form.addRow('API 地址', QLabel(prepared.credentials.domain))
            form.addRow('持仓模式', QLabel('双向持仓' if prepared.position_mode == 'long_short_mode' else '单向持仓'))
        note = QLabel(('确认后发送至 OKX ' + prepared.environment + '。' +
                       ('开仓先核对当前杠杆，仅在与所选倍数不同时调整。' if draft.action == 'open' else '仅平仓，不修改杠杆。'))
                      if prepared is not None else '本次仅预览，不发送订单。')
        if prepared is not None and draft.action == 'open' and prepared.position_mode == 'net_mode':
            note.setText(note.text() + '单向持仓模式：反向成交先抵扣原持仓，超出部分建立反向仓位。')
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        close = QDialogButtonBox(QDialogButtonBox.Cancel if prepared is not None else QDialogButtonBox.Close)
        if prepared is not None:
            skip = QCheckBox('不再提示')
            skip.setToolTip('记住此账户及交易环境的选择，后续点击下单将直接发送；可在下单区恢复确认。')
            layout.addWidget(skip)
            confirm = close.addButton('确认发送 · ' + prepared.environment, QDialogButtonBox.AcceptRole)
            def send():
                if self.busy or self.blocked:
                    return
                from okx_gui.trading import submit_order
                confirm.setEnabled(False)
                if skip.isChecked():
                    self.confirmation_settings.setValue(self.confirmation_key(prepared), True)
                    self.confirmation_settings.sync()
                dialog.accept()
                self.start_task(lambda: submit_order(prepared), lambda order_id: self.order_sent(order_id, prepared))
            confirm.clicked.connect(send)
        close.rejected.connect(dialog.close)
        layout.addWidget(close)
        dialog.setModal(True)
        dialog.show()

    def start_task(self, operation, completed):
        from okx_gui.trading import TradingTask
        self.busy = True
        self.buy.setEnabled(False)
        self.sell.setEnabled(False)
        self.note.setText('正在处理订单…')
        task = TradingTask(operation, self)
        self.task = task
        result = []
        task.completed.connect(lambda value: result.append(value))
        task.failed.connect(self.order_failed)
        def finished():
            if self.task is task:
                self.task = None
                self.busy = False
                self.buy.setEnabled(bool(self.contract.currentText()) and not self.blocked)
                self.sell.setEnabled(bool(self.contract.currentText()) and not self.blocked)
                self.note.setText('点击下单后校验交易规则')
            task.deleteLater()
            if result:
                completed(result[0])
        task.finished.connect(finished)
        task.start()

    def order_sent(self, order_id, prepared=None):
        self.quantity.clear()
        self.error.setText(f'订单已受理：{order_id}（不代表已触发或成交）')
        self.error.setStyleSheet('color: #15966b;')
        self.error.show()
        if prepared is not None:
            self.accepted_order.emit(prepared, order_id)
        self.submitted.emit(order_id)

    def order_failed(self, message, uncertain):
        self.error.setStyleSheet('color: #c34747;')
        self.error.setText(message)
        self.error.show()
        if uncertain:
            self.blocked = True
            self.unlock.show()

    def unlock_submission(self):
        if QMessageBox.question(self, '核对订单', '已在 OKX 核对委托和成交记录，确认可以继续下单？',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.blocked = False
            self.unlock.hide()
            self.update_form()

    def shutdown(self):
        # Never terminate a thread that may be sending a financial transaction.
        if self.task is not None and self.task.isRunning():
            return False
        self.rules_closing = True
        if self.rules_task is not None and self.rules_task.isRunning():
            self.rules_task.requestInterruption()
            return self.rules_task.wait(6000)
        return True
