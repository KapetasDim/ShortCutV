"""Cross-thread-to-GUI marshalling helper (run_on_ui), the Qt equivalent of
Tk's `widget.after(0, fn)` pattern used throughout this app's background-
thread (ffmpeg/render) callbacks; and connect_button, a small wrapper every
button/action handler in the app is wired through so that (a) every click is
logged to the console before its handler runs, for diagnosing a hang/freeze
after the fact, and (b) an exception inside a handler prints a traceback
instead of propagating out of a Qt slot -- an uncaught exception escaping a
slot is a real source of the app going unresponsive or aborting outright,
not just a cosmetic issue.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QObject, Signal

from ..logging_config import get_logger

log = get_logger()


class _Invoker(QObject):
    requested = Signal(object)

    def __init__(self):
        super().__init__()
        # QueuedConnection (not the default AutoConnection) is essential: a
        # queued signal is always delivered on the thread that OWNS this
        # object (its event loop), no matter which thread emits it. With
        # AutoConnection, a signal emitted from the SAME thread that happened
        # to create this object would run the slot *directly on that thread*.
        # Since run_on_ui is called from render/ffmpeg worker threads, that
        # meant fn() -- including mpv (re)loads -- could execute on a worker
        # thread, crashing libmpv natively. Queued + GUI-thread ownership (see
        # init_ui_marshalling) guarantees fn() always runs on the GUI thread.
        self.requested.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(fn: Callable[[], None]):
        try:
            fn()
        except Exception:
            log.exception("[UI] ERROR in a queued UI update")


_invoker: _Invoker | None = None


def init_ui_marshalling() -> None:
    """Creates the run_on_ui invoker on the *current* thread. Must be called
    once from the GUI thread during startup, before any background thread can
    call run_on_ui -- so the invoker is owned by the GUI thread and queued
    callbacks are delivered onto the GUI event loop rather than adopting
    whatever worker thread first happened to use run_on_ui."""
    global _invoker
    if _invoker is None:
        _invoker = _Invoker()


def run_on_ui(fn: Callable[[], None]) -> None:
    """Schedules fn() to run on the GUI thread's event loop. Safe to call from
    any thread, including the GUI thread itself (where it still defers via
    the event loop, matching Tk's after(0, ...) semantics)."""
    global _invoker
    if _invoker is None:
        # Fallback only: init_ui_marshalling() should have run on the GUI
        # thread already. Creating it here off-thread is the exact hazard that
        # function exists to avoid, so log it loudly.
        log.error("run_on_ui called before init_ui_marshalling(); UI callbacks "
                  "may be delivered on the wrong thread")
        _invoker = _Invoker()
    _invoker.requested.emit(fn)


def connect_button(signal, label: str, fn: Callable[[], None]) -> None:
    """Connects a QPushButton.clicked / QAction.triggered signal to fn via a
    logging + exception-guarding wrapper. Always calls fn() with zero
    arguments (ignoring the checked bool Qt passes), which matches every
    handler in this app and sidesteps signature-mismatch surprises."""

    def wrapper(*_args, **_kwargs):
        log.info("[UI] clicked: %s", label)
        try:
            fn()
        except Exception:
            log.exception("[UI] ERROR handling '%s'", label)

    signal.connect(wrapper)
