import os
import subprocess
import sys
from decimal import Decimal

from PySide6.QtCore import QObject, Signal

from okx_gui.app import MainWindow


class FakeWorker(QObject):
    ticker = Signal(str, str, object)
    status = Signal(str)
    subscription_error = Signal(str, str)
    finished = Signal()

    def __init__(self, instruments, parent):
        super().__init__(parent)
        self.instruments = set(instruments)
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True
        self.finished.emit()

    def set_instruments(self, instruments):
        self.instruments = set(instruments)

    def isInterruptionRequested(self):
        return self.stopped

    def isRunning(self):
        return not self.stopped

    def wait(self, timeout):
        return True


class FakeSpeech(QObject):
    error = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.calls = []
        self.cancelled = []
        self.emergency = set()

    def set_volume(self, percent):
        self.volume = percent

    def set_emergency(self, inst, enabled, *, side=None):
        inst = (inst, side) if side is not None else inst
        if enabled:
            self.emergency.add(inst)
        else:
            self.emergency.discard(inst)

    def availability_error(self):
        return ""

    def trigger_emergency(self, inst, side):
        pass

    def announce(self, inst, price, *, alarm=True, side=None):
        self.calls.append((inst, price))

    def cancel(self, inst=None, *, interrupt=True):
        self.cancelled.append(inst)


def make_window(qtbot):
    window = MainWindow(worker_factory=FakeWorker, speech_factory=FakeSpeech)
    qtbot.addWidget(window)
    window.show()
    return window


def test_add_validate_and_remove(qtbot):
    window = make_window(qtbot)
    window.contract_input.setText(" doge-usdt-swap ")
    window.add_button.click()
    assert "DOGE-USDT-SWAP" in window.rows
    assert window.contract_input.text() == ""
    assert not window.add_contract("DOGE-USDT-SWAP")
    assert len(window.rows) == 4
    assert not window.add_contract("bad input")
    assert window.error_label.isVisible()
    window.rows["BTC-USDT-SWAP"].remove_button.click()
    for inst in list(window.rows):
        window.remove_contract(inst)
    assert window.empty_label.isVisible()
    assert not window.start_button.isEnabled()
    assert window.add_contract("BTC-USDT-SWAP")
    assert window.start_button.isEnabled()


def test_live_updates_dynamic_subscriptions_and_stop(qtbot):
    window = make_window(qtbot)
    assert window.worker is None
    window.start_button.click()
    worker = window.worker
    worker.ticker.emit("BTC-USDT-SWAP", "12345.6700", Decimal("2.345"))
    row = window.rows["BTC-USDT-SWAP"]
    assert row.price.text() == "12345.6700"
    assert row.change.text() == "+2.34%"
    window.add_contract("DOGE-USDT-SWAP")
    assert "DOGE-USDT-SWAP" in worker.instruments
    window.remove_contract("SOL-USDT-SWAP")
    assert "SOL-USDT-SWAP" not in worker.instruments
    worker.status.emit("连接中断，3 秒后重试")
    assert row.price.text() == "--"
    worker.subscription_error.emit("DOGE-USDT-SWAP", "合约不存在")
    assert "合约不存在" in window.error_label.text()
    window.start_button.click()
    assert worker.stopped
    assert window.worker is None
    assert row.price.text() == "--"


def test_remove_all_stops_market_and_close_stops_worker(qtbot):
    window = make_window(qtbot)
    window.toggle_market()
    worker = window.worker
    for inst in list(window.rows):
        window.remove_contract(inst)
    assert worker.stopped
    window.add_contract("ETH-USDT-SWAP")
    window.toggle_market()
    worker = window.worker
    window.close()
    assert worker.stopped


def test_entry_point_runs_event_loop():
    # Keep application quit state isolated from the shared pytest Qt event loop.
    result = subprocess.run(
        [sys.executable, "-c", "from PySide6.QtWidgets import QApplication; "
         "from PySide6.QtCore import QTimer; from okx_gui.app import main; "
         "app=QApplication([]); QTimer.singleShot(100, app.quit); raise SystemExit(main())"],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_range_voice_and_return_to_silence(qtbot):
    window = make_window(qtbot)
    window.toggle_market()
    row = window.rows["BTC-USDT-SWAP"]
    worker = window.worker
    worker.ticker.emit(row.instrument, "82600", None)
    row.low_input.setText("82400")
    row.high_input.setText("82800")
    row.step_input.setText("100")
    row.voice_button.click()
    assert not row.low_input.isEnabled()
    for price in ("82400", "82800", "82801", "82850", "82900", "82800", "82700", "82399", "82300", "82400"):
        worker.ticker.emit(row.instrument, price, None)
    assert [p for _, p in window.speech.calls] == ["82801", "82900", "82399", "82300"]
    assert row.instrument in window.speech.cancelled
    assert "范围内静音" in row.voice_state.text()
    row.voice_button.click()
    assert row.ladder is None
    assert row.low_input.isEnabled()
    row.voice_button.click()
    window.toggle_market()
    assert row.ladder is None


def test_voice_test_button_does_not_start_market(qtbot):
    window = make_window(qtbot)
    window.test_voice_button.click()
    assert window.speech.calls == [("BTC-USDT-SWAP", "82600.05")]
    assert window.worker is None


def test_range_validation_dialogs_and_volume(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, message: messages.append(message))
    window = make_window(qtbot)
    row = window.rows["BTC-USDT-SWAP"]
    row.voice_button.click()
    assert messages and row.ladder is None
    row.low_input.setText("82800")
    row.high_input.setText("82400")
    row.step_input.setText("100")
    row.voice_button.click()
    assert "左侧低价格" in messages[-1]
    row.low_input.setText("82400")
    row.high_input.setText("82800")
    row.voice_button.click()
    assert "先启动行情" in messages[-1]
    window.toggle_market()
    window.worker.ticker.emit(row.instrument, "83000", None)
    row.voice_button.click()
    assert "当前价格 83000" in messages[-1]
    assert row.ladder is None
    assert row.low_input.isEnabled()
    window.worker.ticker.emit(row.instrument, "82600", None)
    row.voice_button.click()
    assert row.ladder is not None
    window.speech.error.emit("音频设备不可用")
    assert row.ladder is None
    for volume in (35, 0, 100):
        window.volume_slider.setValue(volume)
        assert window.speech.volume == volume
        assert f"{volume}%" in window.volume_label.text()


def test_per_contract_emergency_is_independent_and_cleans_on_delete_close(qtbot):
    window = make_window(qtbot)
    btc = window.rows["BTC-USDT-SWAP"]
    eth = window.rows["ETH-USDT-SWAP"]
    assert not btc.emergency_buttons["high"].isChecked()
    btc.emergency_buttons["high"].click()
    assert window.speech.emergency == {(btc.instrument, "high")}
    assert not eth.emergency_buttons["high"].isChecked()
    eth.emergency_buttons["high"].click()
    btc.emergency_buttons["high"].click()
    assert window.speech.emergency == {(eth.instrument, "high")}
    btc.emergency_buttons["high"].click()
    window.remove_contract(btc.instrument)
    assert window.speech.emergency == {(eth.instrument, "high")}
    window.close()
    assert not window.speech.emergency


def test_optional_range_fields_start_with_current_quote(qtbot):
    window = make_window(qtbot)
    window.toggle_market()
    row = window.rows["BTC-USDT-SWAP"]
    window.worker.ticker.emit(row.instrument, "82600", None)
    row.step_input.setText("100")
    row.high_input.setText("82800")
    row.voice_button.click()
    assert row.ladder.lower == 0
    row.voice_button.click()
    row.high_input.clear()
    row.low_input.setText("82400")
    row.voice_button.click()
    assert row.ladder.upper.is_infinite()


def test_stop_voice_disables_own_emergency_without_affecting_other_contract(qtbot):
    window = make_window(qtbot)
    window.toggle_market()
    btc = window.rows["BTC-USDT-SWAP"]
    eth = window.rows["ETH-USDT-SWAP"]
    for row, current, high in ((btc, "82600", "82800"), (eth, "3000", "3100")):
        window.worker.ticker.emit(row.instrument, current, None)
        row.high_input.setText(high)
        row.step_input.setText("100")
        row.voice_button.click()
        row.emergency_buttons["high"].click()
    window.worker.ticker.emit(btc.instrument, "82900", None)
    assert (btc.instrument, "82900") in window.speech.calls
    btc.voice_button.click()
    assert btc.ladder is None
    assert not btc.emergency_buttons["high"].isChecked()
    assert "关闭" in btc.emergency_buttons["high"].toolTip()
    assert (btc.instrument, "high") not in window.speech.emergency
    assert btc.instrument in window.speech.cancelled
    assert eth.emergency_buttons["high"].isChecked()
    assert (eth.instrument, "high") in window.speech.emergency
    count = len(window.speech.calls)
    window.worker.ticker.emit(btc.instrument, "83000", None)
    assert len(window.speech.calls) == count
    window.toggle_market()
    assert not eth.emergency_buttons["high"].isChecked()
    assert not window.speech.emergency


def test_empty_interval_requires_emergency_and_prices_only_once(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, message: messages.append(message))
    window = make_window(qtbot)
    window.toggle_market()
    row = window.rows["BTC-USDT-SWAP"]
    window.worker.ticker.emit(row.instrument, "82600", None)
    row.high_input.setText("82800")
    row.voice_button.click()
    assert row.ladder is None
    assert "情况紧急" in messages[-1]
    row.emergency_buttons["high"].click()
    row.voice_button.click()
    assert row.ladder is not None and row.ladder.step is None
    for price in ("82801", "82900", "82600", "83000"):
        window.worker.ticker.emit(row.instrument, price, None)
    assert window.speech.calls == [(row.instrument, "82801")]
    row.emergency_buttons["high"].click()
    assert row.ladder is None
    assert row.voice_button.text() == "开始播报"
    assert (row.instrument, "high") not in window.speech.emergency


def test_bound_emergency_dots_are_independent_and_stop_together(qtbot):
    window = make_window(qtbot)
    window.toggle_market()
    row = window.rows["BTC-USDT-SWAP"]
    window.worker.ticker.emit(row.instrument, "82600", None)
    row.low_input.setText("82400")
    row.high_input.setText("82800")
    row.emergency_buttons["low"].click()
    assert not row.emergency_buttons["high"].isChecked()
    row.voice_button.click()
    assert row.ladder is not None
    window.worker.ticker.emit(row.instrument, "82900", None)
    assert not window.speech.calls
    window.worker.ticker.emit(row.instrument, "82399", None)
    assert window.speech.calls == [(row.instrument, "82399")]
    row.emergency_buttons["high"].click()
    row.emergency_buttons["low"].click()
    assert row.ladder is not None
    assert window.speech.emergency == {(row.instrument, "high")}
    row.voice_button.click()
    assert not any(b.isChecked() for b in row.emergency_buttons.values())
    assert not window.speech.emergency
