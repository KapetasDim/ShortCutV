"""Native file/folder dialogs via Qt's own QFileDialog.

The old Tk build ran these in a throwaway subprocess (see the removed
native_dialog.py/_dialog_worker.py) purely to work around Tk-specific bugs:
Tk had no reliable way to keep a native dialog owned by/above the main
window, and Windows' per-monitor DPI scaling didn't line up with Tk's own
manual scaling system. Qt's QFileDialog doesn't have either problem -- it's
a real native common-item-dialog parented correctly to the calling widget,
and Qt's DPI handling is independent of this app's UI-scale profiles -- so
this is just a thin, direct wrapper with the same callback-based signature
the rest of the app already expects (call sites were written against an
async dialog API; that shape is kept even though QFileDialog is actually
synchronous, so no call site needs to change).
"""
from __future__ import annotations

import faulthandler
import os
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import QFileDialog, QWidget

from ..logging_config import get_logger
from .player import reacquire_all_after_dialog, release_all_for_dialog

log = get_logger()

# Qt-drawn dialogs by default, not the OS shell dialog: a faulthandler dump
# caught the app hard-deadlocking inside the native getSaveFileName call
# itself, moments after an embedded mpv player was terminated (see the Save
# Project investigation) -- mpv's terminate() returns before the underlying
# HWND it parented into a Qt widget is necessarily gone, and the native
# common-item-dialog/COM machinery can deadlock against that. Set
# VIDEO_EDITOR_NON_NATIVE_DIALOGS=0 to force the native dialog back on (e.g.
# to re-confirm this diagnosis).
_FORCE_NON_NATIVE = os.environ.get("VIDEO_EDITOR_NON_NATIVE_DIALOGS") != "0"
_NATIVE_OPTS = QFileDialog.Option.DontUseNativeDialog if _FORCE_NON_NATIVE else QFileDialog.Option(0)
_DIALOG_MODE = "Qt-drawn (non-native)" if _FORCE_NON_NATIVE else "native"
# Logged lazily on first use, not at import time -- this module is imported
# (and _FORCE_NON_NATIVE decided) before main() calls configure_logging() and
# attaches a handler, so a log call made here at import time is silently
# dropped rather than ever reaching the console.


def _filter_string(filetypes: Optional[list[tuple[str, str]]]) -> str:
    if not filetypes:
        return "All files (*)"
    return ";;".join(f"{label} ({pattern})" for label, pattern in filetypes)


def _run_native_dialog(fn: Callable[[], object]):
    """Every native dialog in the app goes through this one choke point --
    releasing every live embedded mpv player first (and reacquiring them
    after) here means no dialog call site needs to remember to do it itself.
    See player.release_all_for_dialog for why this is necessary at all.

    Step-logs each phase and arms faulthandler.dump_traceback_later() around
    the actual dialog call -- diagnostic instrumentation for tracking down a
    freeze whose exact blocking line isn't yet known; safe/cheap to leave in
    permanently (a no-op unless something actually hangs >10s)."""
    t0 = time.monotonic()
    log.info("Dialogs: using %s dialogs; releasing players before dialog...", _DIALOG_MODE)
    saved = release_all_for_dialog()
    log.info("Dialogs: released %d player(s) in %.3fs, entering dialog call...", len(saved), time.monotonic() - t0)
    faulthandler.dump_traceback_later(10, exit=False)
    try:
        t1 = time.monotonic()
        result = fn()
        log.info("Dialogs: dialog call returned after %.3fs", time.monotonic() - t1)
        return result
    finally:
        faulthandler.cancel_dump_traceback_later()
        t2 = time.monotonic()
        # This already-obtained dialog result (e.g. a chosen Save path) must
        # reach the caller no matter what -- reacquire_all_after_dialog
        # guards its own per-player work, but a failure here must not be
        # allowed to escape this finally block and silently discard `result`
        # (see the mpv-seek-on-a-not-yet-ready-image crash this once caused).
        try:
            reacquire_all_after_dialog(saved)
        except Exception:
            log.exception("Dialogs: reacquire_all_after_dialog failed")
        log.info("Dialogs: reacquired %d player(s) in %.3fs, total %.3fs",
                  len(saved), time.monotonic() - t2, time.monotonic() - t0)


def ask_save_filename(parent: QWidget, on_done: Callable[[Optional[Path]], None], *,
                       message: str = "", title: str = "",
                       defaultextension: str = "", initialfile: str = "",
                       filetypes: Optional[list[tuple[str, str]]] = None):
    start = initialfile or ""
    path, _filter = _run_native_dialog(
        lambda: QFileDialog.getSaveFileName(parent, title or "Save as...", start,
                                             _filter_string(filetypes), options=_NATIVE_OPTS))
    if not path:
        on_done(None)
        return
    p = Path(path)
    if defaultextension and not p.suffix:
        p = p.with_suffix(defaultextension if defaultextension.startswith(".") else f".{defaultextension}")
    on_done(p)


def ask_open_filename(parent: QWidget, on_done: Callable[[Optional[Path]], None], *,
                       message: str = "", title: str = "",
                       filetypes: Optional[list[tuple[str, str]]] = None):
    path, _filter = _run_native_dialog(
        lambda: QFileDialog.getOpenFileName(parent, title or "Open...", "", _filter_string(filetypes),
                                             options=_NATIVE_OPTS))
    on_done(Path(path) if path else None)


def ask_directory(parent: QWidget, on_done: Callable[[Optional[Path]], None], *,
                   message: str = "", title: str = "", initialdir: str = ""):
    path = _run_native_dialog(
        lambda: QFileDialog.getExistingDirectory(parent, title or "Select folder", initialdir or "",
                                                  options=_NATIVE_OPTS))
    on_done(Path(path) if path else None)
