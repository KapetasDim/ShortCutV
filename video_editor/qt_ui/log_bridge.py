"""Mirrors the app logger's output into a QPlainTextEdit, in addition to the
console handler configured by logging_config.configure_logging(). Log calls
happen from background threads all over this app (render/ffmpeg worker
threads) as well as the main thread; Qt widgets may only be touched from the
GUI thread, so the handler emits a Qt signal instead of touching the widget
directly -- Qt auto-marshals a queued signal emitted from a worker thread
onto the receiver's (GUI-thread) event loop.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from ..logging_config import LOGGER_NAME, get_logger

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

MAX_LINES = 2000  # oldest lines trimmed past this, so a long session's log can't grow unbounded


class _LogSignal(QObject):
    emitted = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.signal = _LogSignal()
        self.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        self.signal.emitted.emit(msg)


def attach_text_widget(widget) -> QtLogHandler:
    """Mirrors all logging output into `widget` (a QPlainTextEdit) in addition
    to the console. Removes any previously attached QtLogHandler first, so
    calling this again (e.g. after a tab rebuild) replaces the stale handler
    instead of piling a new one on top of it."""
    logger = get_logger()
    for h in list(logger.handlers):
        if isinstance(h, QtLogHandler):
            logger.removeHandler(h)
    handler = QtLogHandler()

    def _append(msg: str):
        widget.appendPlainText(msg)
        doc = widget.document()
        if doc.blockCount() > MAX_LINES:
            cursor = widget.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor,
                                 doc.blockCount() - MAX_LINES)
            cursor.removeSelectedText()

    handler.signal.emitted.connect(_append)
    logger.addHandler(handler)
    return handler
