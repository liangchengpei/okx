"""Application entry point. Network services will be added in later stages."""

import sys

from PySide6.QtWidgets import QApplication, QMainWindow, QWidget


class MainWindow(QMainWindow):
    """Blank application shell for stage one."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OKX Trader — 阶段一")
        self.resize(1000, 700)
        self.setCentralWidget(QWidget(self))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("OKX Trader")
    window = MainWindow()
    window.show()
    return app.exec()
