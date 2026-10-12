"""Native file/folder dialogs, run in a throwaway subprocess instead of
in-process (see _dialog_worker.py for why), with a small separate status
window shown on the caller's main window for the duration -- so a stuck
dialog subprocess reads as "still working" rather than the whole app going
"Not Responding".
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

from .logging_config import get_logger
from .monitor_geometry import monitor_bounds_for_hwnd, window_rect_for_hwnd
from .ui_scale import get_scale

log = get_logger()

_WORKER = str(Path(__file__).with_name("_dialog_worker.py"))

_active_procs_lock = threading.Lock()
_active_procs: set[subprocess.Popen] = set()


def terminate_all_dialog_processes():
    """Kills any still-running dialog-worker subprocesses. Call this when the
    main app is closing, so a Save/Load dialog left open doesn't linger as an
    orphaned process after the app window is gone."""
    with _active_procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            log.exception("native_dialog: failed to kill/reap dialog subprocess pid=%s", proc.pid)


class _StatusWindow(tk.Toplevel):
    """Small always-on-top window, separate from the full-window block
    overlay, purely to say what's happening."""

    def __init__(self, parent: tk.Misc, message: str):
        super().__init__(parent)
        try:
            self._build(parent, message)
        except Exception:
            log.exception("StatusWindow: construction failed, destroying and continuing without it")
            self._safe_destroy()

    def _build(self, parent: tk.Misc, message: str):
        self.overrideredirect(True)
        self.configure(bg="#1e1e1e")
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass

        scale = get_scale()
        tk.Label(self, text=message, bg="#1e1e1e", fg="white",
                 font=("Courier", scale.font_lg, "bold"),
                 padx=scale.space_xxxl, pady=scale.space_xl).pack()

        root = parent.winfo_toplevel()
        root.update_idletasks()
        self.update_idletasks()
        # Tk's winfo_rootx/rooty/width/height are unreliable for a zoomed
        # (maximized) toplevel on Windows, so use the real current bounds
        # from Win32 instead of Tk's (possibly stale) cache.
        rect = window_rect_for_hwnd(root.winfo_id())
        if rect is not None:
            rx, ry, rw, rh = rect
        else:
            rx, ry = root.winfo_rootx(), root.winfo_rooty()
            rw, rh = root.winfo_width(), root.winfo_height()
        x = rx + (rw - self.winfo_width()) // 2
        y = ry + (rh - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def close(self):
        self._safe_destroy()

    def _safe_destroy(self):
        try:
            self.destroy()
        except tk.TclError:
            pass


def _run_blocked(parent: tk.Misc, message: str, work: Callable[[], object],
                  on_done: Callable[[object], None]):
    """Shows a small separate status window saying `message` (skipped
    entirely if `message` is falsy), runs `work()` on a background thread,
    then marshals its return value to `on_done` back on the main thread and
    tears the window down. `work` must not touch any Tk widgets -- it runs
    off the main thread."""
    status = _StatusWindow(parent, message) if message else None

    result_box: dict = {}

    def bg():
        try:
            result_box["value"] = work()
        except Exception:
            log.exception("native_dialog: background work failed")
            result_box["error"] = True

    def poll(thread: threading.Thread):
        if thread.is_alive():
            parent.after(100, lambda: poll(thread))
            return
        if status is not None:
            status.close()
        on_done(None if result_box.get("error") else result_box.get("value"))

    t = threading.Thread(target=bg, daemon=True)
    t.start()
    parent.after(100, lambda: poll(t))


def _run_worker(spec: dict) -> Optional[str]:
    # CREATE_NO_WINDOW: if the app was launched without an attached console
    # (e.g. via pythonw), spawning sys.executable can otherwise pop up a
    # stray console window -- which the worker's own EnumWindows-based dialog
    # repositioning could then grab and move instead of the real dialog.
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            [sys.executable, _WORKER, json.dumps(spec)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags,
        )
    except Exception:
        log.exception("native_dialog: worker subprocess failed for kind=%s", spec.get("kind"))
        return None

    with _active_procs_lock:
        _active_procs.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        log.error("native_dialog: worker subprocess timed out after 300s for kind=%s, killing it",
                   spec.get("kind"))
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            log.exception("native_dialog: failed to reap timed-out worker subprocess pid=%s", proc.pid)
        return None
    except Exception:
        log.exception("native_dialog: worker subprocess failed for kind=%s", spec.get("kind"))
        return None
    finally:
        with _active_procs_lock:
            _active_procs.discard(proc)
    if proc.returncode != 0:
        log.warning("native_dialog: worker exited with code %s for kind=%s: %s",
                     proc.returncode, spec.get("kind"), (stderr or "").strip()[-1000:])
    path = stdout.strip()
    return path or None


def _ask(parent: tk.Misc, message: str, spec: dict, on_done: Callable[[Optional[Path]], None]):
    root = parent.winfo_toplevel()
    root.update_idletasks()
    bounds = monitor_bounds_for_hwnd(root.winfo_id())
    if bounds is not None:
        spec = {**spec, "monitor": list(bounds)}
    # Passed to the worker so it can set the real dialog window's Win32 owner
    # to the main app window once the dialog appears (see
    # _dialog_worker.py's _reposition_dialog_when_it_appears) -- an owned
    # window is kept above its owner by Windows automatically, which stops
    # clicking back into the app from burying the dialog behind it.
    spec = {**spec, "owner_hwnd": root.winfo_id()}

    def work():
        return _run_worker(spec)

    def done(path):
        on_done(Path(path) if path else None)

    _run_blocked(parent, message, work, done)


def ask_save_filename(parent: tk.Misc, on_done: Callable[[Optional[Path]], None], *,
                       message: str = "Waiting for file dialog...", title: str = "",
                       defaultextension: str = "", initialfile: str = "",
                       filetypes: Optional[list[tuple[str, str]]] = None):
    spec = {"kind": "save", "title": title, "defaultextension": defaultextension,
            "initialfile": initialfile, "filetypes": filetypes or []}
    _ask(parent, message, spec, on_done)


def ask_open_filename(parent: tk.Misc, on_done: Callable[[Optional[Path]], None], *,
                       message: str = "Waiting for file dialog...", title: str = "",
                       filetypes: Optional[list[tuple[str, str]]] = None):
    spec = {"kind": "open", "title": title, "filetypes": filetypes or []}
    _ask(parent, message, spec, on_done)


def ask_directory(parent: tk.Misc, on_done: Callable[[Optional[Path]], None], *,
                   message: str = "Waiting for folder dialog...", title: str = "",
                   initialdir: str = ""):
    spec = {"kind": "dir", "title": title, "initialdir": initialdir}
    _ask(parent, message, spec, on_done)


def run_blocked(parent: tk.Misc, message: str, work: Callable[[], object],
                 on_done: Callable[[object], None]):
    """Public alias of _run_blocked, for wrapping non-dialog work (e.g. the
    actual project save/load) in the same block-overlay + status-window UX."""
    _run_blocked(parent, message, work, on_done)
