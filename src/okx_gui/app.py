"""Contract monitor GUI."""
import re
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from okx_gui.market import MarketWorker

STYLE = """
QWidget { background: #fcfcfc; color: #16191d; font-size: 14px; }
QMainWindow { background: #f5f6f8; }
QFrame#card { border: 1px solid #dedfe2; border-radius: 18px; background: #fcfcfc; }
QLabel#title { font-size: 19px; font-weight: 600; }
QLabel#badge { background: #ededee; border-radius: 12px; padding: 4px 12px; font-size: 12px; }
QLineEdit { border: 1px solid #d8dadd; border-radius: 20px; padding: 10px 15px; background: white; }
QLineEdit:focus { border-color: #747b85; }
QPushButton { border: 0; border-radius: 18px; padding: 10px 18px; background: #ededee; }
QPushButton:hover { background: #e0e2e5; }
QPushButton:disabled { color: #999; background: #f0f0f0; }
QPushButton#start { background: #111; color: white; }
QPushButton#start:hover { background: #333; }
QPushButton#remove { background: transparent; color: #777; font-size: 20px; padding: 4px; }
QLabel#muted { color: #7b818a; font-size: 12px; }
QLabel#error { color: #c34747; font-size: 12px; }
QFrame#row { border-bottom: 1px solid #ededee; }
QFrame#list { border: 1px solid #e0e1e3; border-radius: 10px; }
QRadioButton { spacing: 10px; font-weight: 600; }
QRadioButton::indicator { width: 14px; height: 14px; }
QRadioButton::indicator:unchecked { border: 1px solid #bbb; border-radius: 8px; background: #aaa; }
QRadioButton::indicator:checked { border: 1px solid #111; border-radius: 8px; background: #111; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: #f6f6f6; width: 6px; margin: 0; }
QScrollBar::handle:vertical { background: #d8dadd; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class ContractRow(QFrame):
    def __init__(self, instrument, remove):
        super().__init__()
        self.setObjectName("row")
        self.instrument = instrument
        self.setMinimumHeight(84)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 16)
        self.radio = QRadioButton(instrument)
        self.radio.setCursor(Qt.PointingHandCursor)
        identity = QVBoxLayout()
        identity.setSpacing(4)
        identity.addWidget(self.radio)
        self.state = QLabel("未启动监控")
        self.state.setObjectName("muted")
        self.state.setContentsMargins(25, 0, 0, 0)
        identity.addWidget(self.state)
        layout.addLayout(identity, 1)
        quotes = QVBoxLayout()
        self.price = QLabel("--")
        self.price.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.price.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.change = QLabel("--")
        self.change.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.change.setObjectName("muted")
        quotes.addWidget(self.price)
        quotes.addWidget(self.change)
        layout.addLayout(quotes, 1)
        self.remove_button = QPushButton("×")
        self.remove_button.setObjectName("remove")
        self.remove_button.setFixedWidth(32)
        self.remove_button.setToolTip(f"删除 {instrument}")
        self.remove_button.clicked.connect(lambda: remove(instrument))
        layout.addWidget(self.remove_button)
        self.setToolTip("未启动监控")

    def update_quote(self, price, change):
        self.price.setText(price)
        self.state.setText("实时监控中")
        self.change.setText("--" if change is None else f"{change:+.2f}%")
        color = "#7b818a" if change is None else ("#15966b" if change >= 0 else "#d34b4b")
        self.change.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.setToolTip("实时行情 · 最新成交价 / 24h 涨跌幅")


class MainWindow(QMainWindow):
    def __init__(self, worker_factory=MarketWorker):
        super().__init__()
        self.worker_factory = worker_factory
        self.worker = None
        self.rows = {}
        self.selected_contract = None
        self.setWindowTitle("OKX 合约监控")
        self.resize(790, 550)
        self.setMinimumSize(600, 430)
        self.setStyleSheet(STYLE)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        card = QFrame()
        card.setObjectName("card")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("▥  OKX 合约监控")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        badge = QLabel("GUI 原型")
        badge.setObjectName("badge")
        header.addWidget(badge)
        layout.addLayout(header)
        entry = QHBoxLayout()
        self.contract_input = QLineEdit()
        self.contract_input.setPlaceholderText("例如 DOGE-USDT-SWAP")
        self.contract_input.setClearButtonEnabled(True)
        self.contract_input.returnPressed.connect(self.add_from_input)
        entry.addWidget(self.contract_input, 1)
        self.add_button = QPushButton("＋ 添加")
        self.add_button.setEnabled(False)
        self.contract_input.textChanged.connect(lambda value: self.add_button.setEnabled(bool(value.strip())))
        self.add_button.clicked.connect(self.add_from_input)
        entry.addWidget(self.add_button)
        layout.addLayout(entry)
        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        table = QFrame()
        table.setObjectName("list")
        table_layout = QVBoxLayout(table)
        table_layout.setContentsMargins(0, 8, 0, 0)
        table_header = QHBoxLayout()
        table_header.setContentsMargins(10, 0, 12, 0)
        for text in ("合约", "实时价格 / 24h 涨跌 · 操作"):
            label = QLabel(text)
            label.setObjectName("muted")
            table_header.addWidget(label, 1, Qt.AlignLeft if text == "合约" else Qt.AlignRight)
        table_layout.addLayout(table_header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        self.row_layout = QVBoxLayout(content)
        self.row_layout.setContentsMargins(0, 0, 0, 0)
        self.row_layout.setSpacing(0)
        self.row_layout.addStretch()
        self.scroll.setWidget(content)
        table_layout.addWidget(self.scroll)
        layout.addWidget(table, 1)
        self.group = QButtonGroup(self)
        self.group.buttonToggled.connect(self.selection_changed)
        self.empty_label = QLabel("暂无合约，请在上方添加")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("muted")
        self.row_layout.insertWidget(0, self.empty_label)
        controls = QHBoxLayout()
        self.status_label = QLabel("行情监控未启动")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        self.start_button = QPushButton("⊙ 启动行情")
        self.start_button.setObjectName("start")
        self.start_button.clicked.connect(self.toggle_market)
        controls.addWidget(self.start_button)
        layout.addLayout(controls)
        self.selected_label = QLabel("选中合约：未选择")
        self.selected_label.setContentsMargins(10, 8, 0, 0)
        layout.addWidget(self.selected_label)
        self.voice_hint = QLabel("阶梯式语音播报：预留设置区域，后续开放。")
        self.voice_hint.setObjectName("muted")
        self.voice_hint.setContentsMargins(10, 0, 0, 0)
        layout.addWidget(self.voice_hint)
        for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
            self.add_contract(inst)
        self.rows["SOL-USDT-SWAP"].radio.setChecked(True)

    def show_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def add_from_input(self):
        if self.add_contract(self.contract_input.text()):
            self.contract_input.clear()

    def add_contract(self, value):
        inst = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+-(?:SWAP|\d{6})", inst):
            self.show_error("请输入完整合约代码，例如 DOGE-USDT-SWAP 或 BTC-USDT-261225。")
            return False
        if inst in self.rows:
            self.show_error(f"{inst} 已在列表中。")
            self.rows[inst].radio.setChecked(True)
            return False
        row = ContractRow(inst, self.remove_contract)
        self.rows[inst] = row
        self.group.addButton(row.radio)
        self.row_layout.insertWidget(self.row_layout.count() - 1, row)
        self.empty_label.hide()
        self.show_error("")
        if self.selected_contract is None:
            row.radio.setChecked(True)
        self.sync_subscriptions()
        self.start_button.setEnabled(True)
        return True

    def remove_contract(self, inst):
        row = self.rows.pop(inst)
        self.group.removeButton(row.radio)
        self.row_layout.removeWidget(row)
        row.deleteLater()
        if inst == self.selected_contract:
            self.selected_contract = None
            if self.rows:
                next(iter(self.rows.values())).radio.setChecked(True)
            else:
                self.selected_label.setText("选中合约：未选择")
        self.empty_label.setVisible(not self.rows)
        self.sync_subscriptions()
        if not self.rows and self.worker:
            self.toggle_market()
        self.start_button.setEnabled(bool(self.rows) and (self.worker is None or not self.worker.isInterruptionRequested()))

    def selection_changed(self, button, checked):
        if checked:
            self.selected_contract = button.text()
            self.selected_label.setText(f"选中合约：{self.selected_contract}")

    def sync_subscriptions(self):
        if self.worker:
            self.worker.set_instruments(self.rows)
            for row in self.rows.values():
                if row.price.text() == "--":
                    row.state.setText("等待报价")

    def toggle_market(self):
        if self.worker:
            self.start_button.setEnabled(False)
            self.status_label.setText("正在停止行情…")
            self.worker.stop()
            return
        if not self.rows:
            return
        self.show_error("")
        self.worker = self.worker_factory(list(self.rows), self)
        self.worker.ticker.connect(self.on_ticker)
        self.worker.status.connect(self.on_status)
        self.worker.subscription_error.connect(self.on_subscription_error)
        self.worker.finished.connect(self.market_stopped)
        self.start_button.setText("停止行情")
        self.status_label.setText("正在连接 OKX…")
        for row in self.rows.values():
            row.state.setText("等待报价")
        self.worker.start()

    def on_ticker(self, inst, price, change):
        if self.worker and not self.worker.isInterruptionRequested() and inst in self.rows:
            self.rows[inst].update_quote(price, change)
            self.status_label.setText("● 行情监控中 · OKX 实时报价")

    def on_status(self, status):
        if self.worker and not self.worker.isInterruptionRequested():
            self.status_label.setText(status)
            if "中断" in status or "正在连接" in status:
                for row in self.rows.values():
                    row.setToolTip("连接中断 / 等待报价，显示值可能已过期")
                    row.state.setText("等待连接")
                for row in self.rows.values():
                    row.price.setText("--")
                    row.change.setText("--")

    def on_subscription_error(self, inst, reason):
        self.show_error(f"{inst or '行情'}：{reason}")
        if inst in self.rows:
            self.rows[inst].setToolTip(f"订阅失败：{reason}")
            self.rows[inst].state.setText("订阅失败")

    def market_stopped(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        self.start_button.setText("⊙ 启动行情")
        self.start_button.setEnabled(bool(self.rows))
        self.status_label.setText("行情监控已停止")
        for row in self.rows.values():
            row.price.setText("--")
            row.change.setText("--")
            row.setToolTip("未启动监控")
            row.state.setText("未启动监控")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            worker = self.worker
            worker.stop()
            if not worker.wait(3000):
                self.status_label.setText("正在关闭行情连接，请稍后关闭窗口")
                event.ignore()
                return
        event.accept()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("OKX Trader")
    window = MainWindow()
    window.show()
    return app.exec()
