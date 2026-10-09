from PySide6.QtCore import QTimer

from okx_gui.app import MainWindow, main


def test_blank_window_can_be_shown(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()
    assert window.centralWidget() is not None
    assert window.centralWidget().layout() is None
    window.close()
    assert not window.isVisible()


def test_entry_point_runs_event_loop(qapp):
    QTimer.singleShot(100, qapp.quit)
    assert main() == 0
