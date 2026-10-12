"""Modal order editor independent of the automatically refreshed order cards."""
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from okx_gui.order_actions import editable_fields, perform_order_action, quantity_unit
from okx_gui.trading import TradingTask


class OrderActionDialog(QDialog):
    updated = Signal()
    operation_accepted = Signal(object, str)

    def __init__(self, order, action, parent=None):
        super().__init__(parent)
        self.order, self.action = order, action
        self.task = None
        self.running = False
        self.succeeded = False
        self.inputs = {}
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setModal(True)
        self.setWindowTitle('确认撤单' if action == 'cancel' else '修改委托')
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        environment = '模拟盘' if order.credentials and order.credentials.flag == '1' else '实盘'
        for label, value in [('合约', order.instrument), ('委托编号', order.order_id),
                             ('委托类型', order.kind), ('方向', order.side), ('交易环境', environment)]:
            form.addRow(label, QLabel(value))
        if order.credentials:
            form.addRow('API 地址', QLabel(order.credentials.domain))
        if action == 'amend':
            for key, source, label, _ in editable_fields(order):
                self.add_input(form, key, label, order.raw.get(source, ''))
            face, unit = quantity_unit(order)
            if face is not None and order.raw.get('closeFraction') in (None, '', '0'):
                try:
                    size = Decimal(order.raw['sz']) * face
                    if size.is_finite() and size > 0:
                        self.add_input(form, 'newSz', f'委托总数量（{unit}）', format(size, 'f'))
                except (InvalidOperation, KeyError):
                    pass
            note = '数量包含已成交部分；只提交修改过的字段。修改失败时保留原委托。'
        else:
            note = '确认撤销这条委托？已成交的部分不受影响。'
        layout.addLayout(form)
        info = QLabel(note)
        info.setWordWrap(True)
        layout.addWidget(info)
        self.message = QLabel()
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.confirm = self.buttons.addButton('确认撤单' if action == 'cancel' else '确认修改', QDialogButtonBox.ActionRole)
        self.confirm.clicked.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.acknowledge = QPushButton('已在 OKX 核对操作结果，关闭窗口')
        self.acknowledge.hide()
        self.acknowledge.clicked.connect(self.acknowledge_result)
        layout.addWidget(self.acknowledge)
        self.uncertain = False

    def add_input(self, form, key, label, value):
        edit = QLineEdit(str(value))
        edit.setAccessibleName(label)
        self.inputs[key] = (edit, str(value))
        form.addRow(label, edit)

    def submit(self):
        if self.running or self.uncertain or not self.confirm.isEnabled():
            return
        changes = {key: edit.text().strip() for key, (edit, old) in self.inputs.items() if edit.text().strip() != old}
        if self.action == 'amend' and not changes:
            self.message.setText('请修改价格或数量后再提交。')
            return
        self.running = True
        self.buttons.setEnabled(False)
        for edit, _ in self.inputs.values():
            edit.setEnabled(False)
        self.message.setText('正在处理…')
        task = TradingTask(lambda: perform_order_action(self.order, self.action, changes), self)
        self.task = task
        task.completed.connect(self.completed)
        task.failed.connect(self.failed)
        def finished():
            self.running = False
            self.task = None
            self.buttons.setEnabled(not self.uncertain)
            task.deleteLater()
            if self.succeeded:
                self.accept()
        task.finished.connect(finished)
        task.start()

    def completed(self, message):
        self.succeeded = True
        self.confirm.setEnabled(False)
        self.message.setText(message)
        self.operation_accepted.emit(self.order, self.action)
        self.updated.emit()

    def failed(self, message, uncertain):
        self.message.setText(message)
        self.uncertain = uncertain
        self.acknowledge.setVisible(uncertain)
        if not uncertain:
            for edit, _ in self.inputs.values():
                edit.setEnabled(True)
        self.updated.emit()

    def acknowledge_result(self):
        if not self.running:
            self.uncertain = False
            self.reject()

    def reject(self):
        if not self.running and not self.uncertain:
            super().reject()

    def closeEvent(self, event):
        if self.running or self.uncertain:
            event.ignore()
        else:
            super().closeEvent(event)
