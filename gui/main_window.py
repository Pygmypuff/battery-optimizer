"""
gui/main_window.py
===================
Main window: a "Live (Nordpool)" tab and a "Test Mode" tab, sharing a
console log and an "Open Output File" button underneath. Each tab just
needs to hand `start_run` a callable and its arguments — that callable runs
on a background thread (see gui/worker.py) so the window never freezes, and
anything it prints shows up live in the console.

Adding more tabs later (e.g. a graphs tab reading from the last output
file) means adding a QWidget and one `self.tabs.addTab(...)` call here —
the run/console/output plumbing doesn't need to change.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui.runners import run_nordpool_mode, run_test_mode
from gui.worker import RunWorker


def open_file_with_default_app(path: Path) -> None:
    """Cross-platform "reveal in default app" (Excel, Numbers, LibreOffice, …)."""
    system = platform.system()
    if system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


class LiveRunTab(QWidget):
    """Station power input + a button that runs against live Nordpool prices."""

    def __init__(self, on_run: Callable[..., None]) -> None:
        super().__init__()
        self._on_run = on_run

        self.power_input = QDoubleSpinBox()
        self.power_input.setRange(0.0, 100.0)
        self.power_input.setDecimals(3)
        self.power_input.setSingleStep(0.01)
        self.power_input.setSuffix(" MW")
        self.power_input.setValue(0.310)

        self.battery_pct_input = QDoubleSpinBox()
        self.battery_pct_input.setRange(0.0, 100.0)
        self.battery_pct_input.setDecimals(1)
        self.battery_pct_input.setSingleStep(1.0)
        self.battery_pct_input.setSuffix(" %")
        self.battery_pct_input.setValue(12.0)

        self.run_button = QPushButton("Run (Nordpool)")
        self.run_button.clicked.connect(self._handle_run)

        form = QFormLayout()
        form.addRow("Station power:", self.power_input)
        form.addRow("Current battery level:", self.battery_pct_input)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.run_button)
        layout.addStretch()

    def _handle_run(self) -> None:
        self._on_run(run_nordpool_mode, self.power_input.value(), self.battery_pct_input.value())

    def set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)


class TestModeTab(QWidget):
    """File picker + a button that runs the optimizer against a saved input workbook."""

    def __init__(self, on_run: Callable[..., None]) -> None:
        super().__init__()
        self._on_run = on_run

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select an input .xlsx file…")

        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)

        self.run_button = QPushButton("Run (Test Mode)")
        self.run_button.clicked.connect(self._handle_run)

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_input)
        path_row.addWidget(self.browse_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Input Excel file:"))
        layout.addLayout(path_row)
        layout.addWidget(self.run_button)
        layout.addStretch()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select input Excel file", "", "Excel files (*.xlsx)"
        )
        if path:
            self.path_input.setText(path)

    def _handle_run(self) -> None:
        text = self.path_input.text().strip()
        if not text:
            QMessageBox.warning(self, "No file selected", "Choose an input .xlsx file first.")
            return
        path = Path(text)
        if not path.is_file():
            QMessageBox.warning(self, "File not found", f"Can't find:\n{path}")
            return
        self._on_run(run_test_mode, path)

    def set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.browse_button.setEnabled(not running)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Battery Optimizer")
        self.resize(760, 600)

        self._worker: Optional[RunWorker] = None
        self._last_output_path: Optional[Path] = None

        self.live_tab = LiveRunTab(self.start_run)
        self.test_tab = TestModeTab(self.start_run)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.live_tab, "Live (Nordpool)")
        self.tabs.addTab(self.test_tab, "Test Mode")

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        console_font = QFont("Menlo" if platform.system() == "Darwin" else "Consolas")
        console_font.setStyleHint(QFont.StyleHint.Monospace)
        self.console.setFont(console_font)
        self.console.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")

        self.status_label = QLabel("Ready.")

        self.open_output_button = QPushButton("Open Output File")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self._open_output)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.status_label)
        bottom_row.addStretch()
        bottom_row.addWidget(self.open_output_button)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.tabs)
        layout.addWidget(QLabel("Progress:"))
        layout.addWidget(self.console, stretch=1)
        layout.addLayout(bottom_row)
        self.setCentralWidget(central)

    def start_run(self, target: Callable[..., Any], *args: Any) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        self.console.clear()
        self.status_label.setText("Running…")
        self.open_output_button.setEnabled(False)
        self.live_tab.set_running(True)
        self.test_tab.set_running(True)

        self._worker = RunWorker(target, *args)
        self._worker.output_line.connect(self._append_output)
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.finished.connect(self._on_thread_finished)
        self._worker.start()

    def _append_output(self, text: str) -> None:
        self.console.insertPlainText(text)
        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_success(self, result: object) -> None:
        self._last_output_path = Path(result) if result is not None else None
        if self._last_output_path is not None:
            self.status_label.setText(f"Done. Output: {self._last_output_path.name}")
            self.open_output_button.setEnabled(True)
        else:
            self.status_label.setText("Done.")

    def _on_failure(self, message: str) -> None:
        self.status_label.setText("Run failed.")
        self._append_output("\n" + message)
        QMessageBox.critical(self, "Run failed", "The run failed — see the console for details.")

    def _on_thread_finished(self) -> None:
        self.live_tab.set_running(False)
        self.test_tab.set_running(False)

    def _open_output(self) -> None:
        if self._last_output_path is not None:
            open_file_with_default_app(self._last_output_path)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
