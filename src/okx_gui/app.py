"""Contract monitor GUI."""
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from okx_gui.market import MarketWorker
from okx_gui.positions import PositionsWorker
from okx_gui.position_widgets import PositionCard
from okx_gui.voice import PriceLadder, SpeechService, positive_decimal

STYLE = """
QWidget { background: #fcfcfc; color: #16191d; font-size: 14px; }
QMainWindow { background: #f5f6f8; }
QFrame#card { border: 1px solid #dedfe2; border-radius: 18px; background: #fcfcfc; }
QLabel#title { font-size: 19px; font-weight: 600; }
QLabel#panelTitle { font-size: 16px; font-weight: 600; }
QFrame#positionsPanel, QFrame#orderPanel { border: 1px solid #e0e1e3; border-radius: 12px; }
QLabel#badge { background: #ededee; border-radius: 12px; padding: 4px 12px; font-size: 12px; }
QLineEdit { border: 1px solid #d8dadd; border-radius: 20px; padding: 10px 15px; background: white; }
QLineEdit:focus { border-color: #747b85; }
QPushButton { border: 0; border-radius: 18px; padding: 10px 18px; background: #ededee; }
QPushButton:hover { background: #e0e2e5; }
QPushButton:disabled { color: #999; background: #f0f0f0; }
QPushButton#start { background: #111; color: white; }
QPushButton#start:hover { background: #333; }
QPushButton#emergency { background: #aaa; border: 1px solid #929292; border-radius: 8px; padding: 0; }
QPushButton#emergency:checked { background: #dc3535; border-color: #b92525; }
QPushButton#remove { background: transparent; color: #777; font-size: 20px; padding: 4px; }
QLabel#muted { color: #7b818a; font-size: 12px; }
QLabel#error { color: #c34747; font-size: 12px; }
QFrame#row { border-bottom: 1px solid #ededee; }
QFrame#list { border: 1px solid #e0e1e3; border-radius: 10px; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: #f6f6f6; width: 6px; margin: 0; }
QScrollBar::handle:vertical { background: #d8dadd; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class ContractRow(QFrame):
    def __init__(self, instrument, remove, toggle_voice, toggle_emergency):
        super().__init__()
        self.setObjectName("row")
        self.instrument = instrument
        self.setMinimumHeight(84)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 16)
        self.name = QLabel(instrument)
        self.name.setStyleSheet("font-weight: 600;")
        self.ladder = None
        identity = QVBoxLayout()
        identity.setSpacing(4)
        identity.addWidget(self.name)
        self.state = QLabel("未启动监控")
        self.state.setObjectName("muted")

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
        voice = QVBoxLayout()
        settings = QHBoxLayout()
        self.high_input = QLineEdit()
        self.high_input.setPlaceholderText("高价 / ∞")
        self.high_input.setToolTip("留空默认为正无穷大，不限制上方价格")
        self.high_input.setAccessibleName(f"{instrument} 高价格")
        self.low_input = QLineEdit()
        self.low_input.setPlaceholderText("低价 / 0")
        self.low_input.setAccessibleName(f"{instrument} 低价格")
        self.low_input.setToolTip("留空默认为 0，不限制下方价格；可选择区间内按间隔播报或静音")
        self.step_input = QLineEdit()
        self.step_input.setPlaceholderText("价格间隔")
        self.step_input.setToolTip("上下限都留空：以开始时现价为基准按间隔播报；开启紧急开关后可留空间隔，进入单次价格模式")
        self.step_input.setAccessibleName(f"{instrument} 价格间隔")
        self.emergency_buttons = {}
        for side, field, label in (("low", self.low_input, "下限"), ("high", self.high_input, "上限")):
            field.setFixedWidth(115)
            settings.addWidget(field)
            button = QPushButton()
            button.setObjectName("emergency")
            button.setCheckable(True)
            button.setFixedSize(16, 16)
            button.setCursor(Qt.PointingHandCursor)
            button.setAccessibleName(f"{instrument} {label}紧急开关")
            button.setToolTip(f"{label}情况紧急：关闭（点击开启，红色为开启）")
            button.toggled.connect(lambda enabled, bound=side: toggle_emergency(instrument, bound, enabled))
            self.emergency_buttons[side] = button
            settings.addWidget(button)
        self.step_input.setFixedWidth(115)
        settings.addWidget(self.step_input)
        self.voice_button = QPushButton("开始播报")
        self.voice_button.clicked.connect(lambda: toggle_voice(instrument))
        settings.addWidget(self.voice_button)
        voice.addLayout(settings)
        self.voice_state = QLabel("播报未启动")
        self.voice_state.setObjectName("muted")
        self.inside_checkbox = QCheckBox("区间内播报")
        self.inside_checkbox.setAccessibleName(f"{instrument} 区间内播报")
        self.inside_checkbox.setToolTip("勾选后从启动时现价按间隔播报，回到区间时重新取基准；未勾选则区间内静音。上下限都留空时仍按现价基准播报。")
        voice_status = QHBoxLayout()
        voice_status.addWidget(self.inside_checkbox)
        voice_status.addWidget(self.voice_state, 1)
        voice.addLayout(voice_status)
        layout.addLayout(voice)
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
    def __init__(self, worker_factory=MarketWorker, speech_factory=SpeechService, *, auto_start=True,
                 positions_worker_factory=PositionsWorker, auto_start_positions=True):
        super().__init__()
        self.worker_factory = worker_factory
        self.worker = None
        self.positions_worker_factory = positions_worker_factory
        self.positions_worker = None
        self._positions_updated_at = None
        self.rows = {}
        self.speech = speech_factory(self)
        self.speech.error.connect(self.speech_failed)
        self.setWindowTitle("OKX 合约监控")
        self.resize(1380, 960)
        self.setMinimumSize(1270, 650)
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
        for text in ("合约", "实时价格 / 24h 涨跌        低价格 / 高价格 / 价格间隔 / 语音播报"):
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
        self.empty_label = QLabel("暂无合约，请在上方添加")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("muted")
        self.row_layout.insertWidget(0, self.empty_label)
        controls = QHBoxLayout()
        self.status_label = QLabel("行情监控未启动")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        self.volume_label = QLabel("音量 50%")
        controls.addWidget(self.volume_label)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.setFixedWidth(130)
        self.volume_slider.setAccessibleName("语音播报音量")
        self.volume_slider.setToolTip("0 为静音，调整应用播报音量；下一条播报生效")
        self.volume_slider.valueChanged.connect(self.change_volume)
        self.change_volume(self.volume_slider.value())
        controls.addWidget(self.volume_slider)
        self.test_voice_button = QPushButton("测试语音")
        self.test_voice_button.clicked.connect(self.test_voice)
        controls.addWidget(self.test_voice_button)
        self.start_button = QPushButton("⊙ 启动行情")
        self.start_button.setObjectName("start")
        self.start_button.clicked.connect(self.toggle_market)
        controls.addWidget(self.start_button)
        layout.addLayout(controls)
        trading_areas = QHBoxLayout()
        trading_areas.setSpacing(16)
        for attribute, object_name, title in (
            ("positions_panel", "positionsPanel", "持仓信息"),
            ("order_panel", "orderPanel", "下单"),
        ):
            panel = QFrame()
            panel.setObjectName(object_name)
            panel.setAccessibleName(title)
            panel.setMinimumHeight(210)
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(16, 14, 16, 16)
            heading = QLabel(title)
            heading.setObjectName("panelTitle")
            panel_layout.addWidget(heading)
            panel_layout.addStretch()
            setattr(self, attribute, panel)
            trading_areas.addWidget(panel, 1)
        layout.addLayout(trading_areas)
        self.setup_positions_panel()
        for inst in ("BTC-USDT-SWAP", "SPCX-USDT-SWAP"):
            self.add_contract(inst)
        if auto_start:
            self.toggle_market()
        if auto_start_positions:
            self.refresh_positions()

    def setup_positions_panel(self):
        layout = self.positions_panel.layout()
        heading = layout.takeAt(0).widget()
        layout.takeAt(0)  # Replace the placeholder stretch with account data.
        header = QHBoxLayout()
        header.addWidget(heading)
        header.addStretch()
        self.positions_refresh_button = QPushButton("刷新持仓")
        self.positions_refresh_button.clicked.connect(self.refresh_positions)
        header.addWidget(self.positions_refresh_button)
        layout.addLayout(header)
        self.positions_panel.setMinimumHeight(360)
        self.position_cards = []
        self.positions_scroll = QScrollArea()
        self.positions_scroll.setWidgetResizable(True)
        self.positions_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.positions_scroll.setStyleSheet("QScrollArea { border: 0; background: #fcfcfc; }")
        self.positions_content = QWidget()
        self.positions_content.setStyleSheet("background: #fcfcfc;")
        self.positions_cards_layout = QVBoxLayout(self.positions_content)
        self.positions_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.positions_cards_layout.setSpacing(0)
        self.positions_cards_layout.addStretch()
        self.positions_scroll.setWidget(self.positions_content)
        self.positions_scroll.hide()
        layout.addWidget(self.positions_scroll, 1)
        self.positions_empty = QLabel("持仓尚未读取")
        self.positions_empty.setAlignment(Qt.AlignCenter)
        self.positions_empty.setObjectName("muted")
        layout.addWidget(self.positions_empty, 1)
        self.positions_status = QLabel("每 5 秒自动刷新")
        self.positions_status.setTextFormat(Qt.PlainText)
        self.positions_status.setObjectName("muted")
        self.positions_status.setWordWrap(True)
        layout.addWidget(self.positions_status)

    def refresh_positions(self):
        if self.positions_worker and self.positions_worker.isRunning():
            self.positions_worker.refresh()
            return
        if self.positions_worker:
            self.positions_worker.deleteLater()
        self.positions_worker = self.positions_worker_factory(self)
        self.positions_worker.loading.connect(self.positions_loading)
        self.positions_worker.updated.connect(self.update_positions)
        self.positions_worker.error.connect(self.positions_failed)
        self.positions_worker.start()

    def positions_loading(self):
        self.positions_refresh_button.setEnabled(False)
        suffix = f" · 上次更新 {self._positions_updated_at}" if self._positions_updated_at else ""
        self.positions_status.setText(f"正在读取持仓…{suffix}")
        if not self._positions_updated_at:
            self.positions_empty.setText("正在读取持仓…")

    def update_positions(self, positions, flag):
        scroll_position = self.positions_scroll.verticalScrollBar().value()
        for card in self.position_cards:
            self.positions_cards_layout.removeWidget(card)
            card.deleteLater()
        self.position_cards = []
        for position in positions:
            card = PositionCard(position)
            self.positions_cards_layout.insertWidget(len(self.position_cards), card)
            self.position_cards.append(card)
        self.positions_scroll.setVisible(bool(positions))
        self.positions_scroll.verticalScrollBar().setValue(scroll_position)
        self.positions_empty.setVisible(not positions)
        self.positions_empty.setText("暂无持仓")
        self._positions_updated_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
        mode = "实盘" if flag == "0" else "模拟盘"
        self.positions_status.setText(f"{mode} · {len(positions)} 项持仓 · 更新于 {self._positions_updated_at} · 每 5 秒刷新")
        self.positions_refresh_button.setEnabled(True)

    def positions_failed(self, message):
        suffix = f"，显示上次数据（{self._positions_updated_at}）" if self._positions_updated_at else ""
        self.positions_status.setText(f"持仓读取失败{suffix}：{message}")
        if not self.position_cards:
            self.positions_empty.setText("暂无法确认持仓")
        self.positions_refresh_button.setEnabled(True)

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
            return False
        row = ContractRow(inst, self.remove_contract, self.toggle_voice, self.toggle_emergency)
        self.rows[inst] = row
        self.row_layout.insertWidget(self.row_layout.count() - 1, row)
        self.empty_label.hide()
        self.show_error("")
        self.sync_subscriptions()
        self.start_button.setEnabled(True)
        return True

    def remove_contract(self, inst):
        for side in ("low", "high"):
            self.speech.set_emergency(inst, False, side=side)
        row = self.rows.pop(inst)
        self.speech.cancel(inst)
        self.row_layout.removeWidget(row)
        row.deleteLater()
        self.empty_label.setVisible(not self.rows)
        self.sync_subscriptions()
        if not self.rows and self.worker:
            self.toggle_market()
        self.start_button.setEnabled(bool(self.rows) and (self.worker is None or not self.worker.isInterruptionRequested()))

    def toggle_emergency(self, inst, side, enabled):
        row = self.rows[inst]
        label = "下限" if side == "low" else "上限"
        row.emergency_buttons[side].setToolTip(f"{label}情况紧急：{'开启' if enabled else '关闭'}（红色为开启）")
        self.speech.set_emergency(inst, enabled, side=side)
        if not enabled and row.ladder is not None and row.ladder.step is None:
            if not any(button.isChecked() for button in row.emergency_buttons.values()):
                self.stop_voice(inst)

    def change_volume(self, value):
        self.volume_label.setText(f"音量 {value}%" if value else "音量 0%（静音）")
        self.speech.set_volume(value)

    def test_voice(self):
        error = self.speech.availability_error()
        if error:
            self.show_error(error)
            return
        self.show_error("")
        self.speech.announce("BTC-USDT-SWAP", "82600.05", alarm=False)

    def toggle_voice(self, inst):
        row = self.rows[inst]
        if row.ladder is not None:
            self.stop_voice(inst)
            return
        try:
            ladder = PriceLadder(row.low_input.text(), row.high_input.text(), row.step_input.text(),
                                 allow_once=any(button.isChecked() for button in row.emergency_buttons.values()),
                                 inside_enabled=row.inside_checkbox.isChecked())
            if not self.worker or self.worker.isInterruptionRequested() or row.price.text() == "--":
                raise ValueError("尚无有效的实时价格，请先启动行情并等待该合约报价，再开始播报。")
            ladder.validate_current(row.price.text())
        except ValueError as exc:
            QMessageBox.warning(self, "播报设置不合法", str(exc))
            return
        error = self.speech.availability_error()
        if error:
            self.show_error(error)
            return
        if self.worker and self.worker.isInterruptionRequested():
            self.show_error("行情正在停止，请稍后再开始播报。")
            return
        self.show_error("")
        row.ladder = ladder
        row.high_input.setEnabled(False)
        row.low_input.setEnabled(False)
        row.step_input.setEnabled(False)
        row.inside_checkbox.setEnabled(False)
        row.voice_button.setText("停止播报")
        row.voice_state.setText(f"现价基准 {ladder.base} · 间隔 {ladder.step}" if ladder.current_based
                                else self.range_voice_status(ladder))
        if not self.worker:
            self.toggle_market()
        if row.price.text() != "--":
            self.process_voice(inst, row.price.text())

    def stop_voice(self, inst):
        row = self.rows[inst]
        row.ladder = None
        for button in row.emergency_buttons.values():
            button.setChecked(False)
        row.high_input.setEnabled(True)
        row.low_input.setEnabled(True)
        row.step_input.setEnabled(True)
        row.inside_checkbox.setEnabled(True)
        row.voice_button.setText("开始播报")
        row.voice_state.setText("播报未启动")
        self.speech.cancel(inst)

    @staticmethod
    def range_voice_status(ladder):
        mode = f"范围内按间隔 {ladder.step} 播报" if ladder.inside_enabled else "范围内静音"
        return f"{mode} [{ladder.lower}, {ladder.upper}]"

    def process_voice(self, inst, price):
        row = self.rows[inst]
        was_outside = row.ladder is not None and row.ladder.side is not None
        if row.ladder is None:
            return
        if row.ladder.current_based:
            if row.ladder.feed(price):
                row.voice_state.setText(f"基准 {row.ladder.base} · 最近播报 {price}")
                self.speech.announce(inst, price, side=row.ladder.side)
            return
        value = positive_decimal(price)
        side = "low" if value < row.ladder.lower else "high" if value > row.ladder.upper else None
        if row.ladder.step is None and side and not row.emergency_buttons[side].isChecked():
            return
        if side is None and was_outside:
            self.speech.cancel(inst, interrupt=False)
        if row.ladder.feed(price):
            label = {"high": "突破上限", "low": "跌破下限"}.get(side, "范围内")
            row.voice_state.setText(f"{label} · 最近播报 {price}")
            self.speech.announce(inst, price, side=side, movement=side)
        elif row.ladder and row.ladder.side is None:
            row.voice_state.setText(self.range_voice_status(row.ladder))

        if side:
            self.speech.trigger_emergency(inst, side)

    def speech_failed(self, message):
        for row in self.rows.values():
            for button in row.emergency_buttons.values():
                button.setChecked(False)
        for inst in self.rows:
            self.stop_voice(inst)
        self.show_error(message)

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
            for inst in self.rows:
                self.stop_voice(inst)
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
            self.process_voice(inst, price)
            self.status_label.setText("● 行情监控中 · OKX 实时报价")

    def on_status(self, status):
        if self.worker and not self.worker.isInterruptionRequested():
            self.status_label.setText(status)
            if "中断" in status or "正在连接" in status:
                self.speech.cancel(interrupt=False)
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
            self.stop_voice(inst)

    def market_stopped(self):
        for inst in self.rows:
            self.stop_voice(inst)
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
        if self.positions_worker and self.positions_worker.isRunning():
            self.positions_worker.stop()
            if not self.positions_worker.wait(7000):
                self.positions_status.setText("正在关闭持仓连接，请稍后关闭窗口")
                event.ignore()
                return
        for row in self.rows.values():
            for button in row.emergency_buttons.values():
                button.setChecked(False)
        self.speech.shutdown()
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
    app.aboutToQuit.connect(window.close)
    return app.exec()
