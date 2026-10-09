from decimal import Decimal

from PySide6.QtCore import QObject, Signal, QTimer

from okx_gui.app import MainWindow, main


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


def make_window(qtbot):
    window = MainWindow(worker_factory=FakeWorker)
    qtbot.addWidget(window)
    window.show()
    return window


def test_add_validate_select_and_remove(qtbot):
    window = make_window(qtbot)
    assert window.selected_contract == "SOL-USDT-SWAP"
    window.contract_input.setText(" doge-usdt-swap ")
    window.add_button.click()
    assert "DOGE-USDT-SWAP" in window.rows
    assert window.contract_input.text() == ""
    assert not window.add_contract("DOGE-USDT-SWAP")
    assert len(window.rows) == 4
    assert window.selected_contract == "DOGE-USDT-SWAP"
    assert not window.add_contract("bad input")
    assert window.error_label.isVisible()
    window.rows["BTC-USDT-SWAP"].radio.setChecked(True)
    assert window.selected_contract == "BTC-USDT-SWAP"
    window.rows["BTC-USDT-SWAP"].remove_button.click()
    assert window.selected_contract == "ETH-USDT-SWAP"
    for inst in list(window.rows):
        window.remove_contract(inst)
    assert window.selected_contract is None
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


def test_entry_point_runs_event_loop(qapp):
    QTimer.singleShot(100, qapp.quit)
    assert main() == 0
