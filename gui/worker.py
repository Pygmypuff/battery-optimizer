"""
gui/worker.py
=============
Runs a long-running callable (a Nordpool live run, or a test-mode file run)
on a background QThread so the GUI never freezes, and streams everything it
prints (progress messages, warnings) into the console widget as it happens.
"""

from __future__ import annotations

import contextlib
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal


class ConsoleStream(QObject):
    """A write()-only file-like object that turns writes into a Qt signal."""

    text_written = Signal(str)

    def write(self, text: str) -> None:
        if text:
            self.text_written.emit(text)

    def flush(self) -> None:
        pass


class RunWorker(QThread):
    """
    Runs `target(*args, **kwargs)` on a background thread.

    Signals
    -------
    output_line(str)   — a chunk of captured stdout/stderr (progress text).
    succeeded(object)  — the value `target` returned.
    failed(str)        — a formatted exception, if `target` raised.
    """

    output_line = Signal(str)
    succeeded   = Signal(object)
    failed      = Signal(str)

    def __init__(
        self,
        target: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._target = target
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        stream = ConsoleStream()
        stream.text_written.connect(self.output_line)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = self._target(*self._args, **self._kwargs)
        except Exception:
            self.failed.emit(traceback.format_exc())
        else:
            self.succeeded.emit(result)
