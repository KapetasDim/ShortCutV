"""Standalone entry point run as a throwaway subprocess by native_dialog.py.

Runs exactly one native Tk file/folder dialog and prints the chosen path (or
nothing, if cancelled) to stdout. Isolating each dialog call in its own fresh
process sidesteps a Windows/Tk bug where the native (COM-based) file dialog
can hang outright on the Nth call within one long-running Tk process -- a
fresh process has no leftover COM/Tk state to get stuck in.

On a multi-monitor setup, positioning the (withdrawn) owner window on the
right monitor before opening the dialog turned out not to be enough --
Windows' common item dialog can persist its own last-used screen position per
calling app and ignore the owner's placement. So instead, a background thread
here watches for the real dialog window to appear (via EnumWindows, filtered
to this process's own visible top-level windows) and forcibly moves *that*
window onto the target monitor with SetWindowPos once it shows up.
"""
import ctypes
import json
import os
import sys
import threading
import time
import tkinter
from ctypes import wintypes
from tkinter import filedialog

_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def _set_dpi_awareness():
    """Must match the main process's DPI awareness (see run_video_editor.py)
    -- otherwise Windows virtualizes/scales this process's view of screen
    coordinates, and the monitor bounds passed in from the (DPI-aware) main
    process land in the wrong place from this process's point of view."""
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4 & (2**64 - 1))
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


GWLP_HWNDPARENT = -8


def _set_owner(user32, hwnd, owner_hwnd: int):
    """Makes the real dialog window Windows-owned by the main app window.
    This process's own root (passed as `parent=` to the filedialog.ask* call)
    is just a throwaway hidden Tk root local to this worker subprocess, with
    no relationship to the actual app window -- so without this, the OS
    dialog has no owner at all, and clicking back into the app can raise the
    app above the (still-open) dialog and bury it. An owned window is always
    kept above its owner by Windows, including across owner activation, so
    this fixes that regardless of what the user clicks in the app afterward.
    """
    set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    set_long.restype = ctypes.c_void_p
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    set_long(hwnd, GWLP_HWNDPARENT, ctypes.c_void_p(owner_hwnd))
    # Setting the owner alone doesn't retroactively restack an already-visible
    # window -- bring it to the front of the z-order once so it's not left
    # behind the app until the next time the owner is activated.
    HWND_TOP, SWP_NOMOVE, SWP_NOSIZE = 0, 0x0002, 0x0001
    user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)


def _reposition_dialog_when_it_appears(monitor: list[int] | None, owner_hwnd: int | None,
                                        stop_event: threading.Event):
    user32 = ctypes.windll.user32
    pid = os.getpid()
    done = False

    def callback(hwnd, _lparam):
        nonlocal done
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        ww, wh = rect.right - rect.left, rect.bottom - rect.top
        if ww <= 0 or wh <= 0:
            return True
        if monitor is not None:
            x, y, w, h = monitor
            nx = x + max(0, (w - ww) // 2)
            ny = y + max(0, (h - wh) // 2)
            SWP_NOSIZE, SWP_NOZORDER = 0x0001, 0x0004
            user32.SetWindowPos(hwnd, None, nx, ny, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
        if owner_hwnd is not None:
            _set_owner(user32, hwnd, owner_hwnd)
        done = True
        return False

    cb = _WNDENUMPROC(callback)
    deadline = time.time() + 10
    while time.time() < deadline and not done and not stop_event.is_set():
        user32.EnumWindows(cb, 0)
        if done:
            return
        time.sleep(0.05)


def main():
    if sys.platform == "win32":
        _set_dpi_awareness()

    spec = json.loads(sys.argv[1])
    kind = spec["kind"]
    filetypes = [tuple(ft) for ft in spec.get("filetypes", [])]

    root = tkinter.Tk()
    root.withdraw()

    monitor = spec.get("monitor")  # [x, y, w, h] of the caller's monitor, if known
    owner_hwnd = spec.get("owner_hwnd")  # main app window, so the real dialog can be owned by it
    stop_event = threading.Event()
    if sys.platform == "win32" and (monitor or owner_hwnd):
        threading.Thread(target=_reposition_dialog_when_it_appears,
                          args=(monitor, owner_hwnd, stop_event), daemon=True).start()

    # commondialog.Dialog defaults its master to options.get("parent"), falling
    # back to an unrelated temp root if "parent" isn't passed.
    try:
        if kind == "save":
            result = filedialog.asksaveasfilename(
                parent=root,
                title=spec.get("title", ""),
                defaultextension=spec.get("defaultextension", ""),
                initialfile=spec.get("initialfile", ""),
                filetypes=filetypes,
            )
        elif kind == "open":
            result = filedialog.askopenfilename(
                parent=root,
                title=spec.get("title", ""),
                filetypes=filetypes,
            )
        elif kind == "dir":
            result = filedialog.askdirectory(
                parent=root,
                title=spec.get("title", ""),
                initialdir=spec.get("initialdir", ""),
            )
        else:
            result = ""
    finally:
        # An uncaught exception here propagates to stderr and a non-zero exit
        # code, which the parent process (native_dialog._run_worker) now
        # captures and logs -- previously this was indistinguishable from the
        # user clicking Cancel (empty stdout either way).
        stop_event.set()

    root.destroy()
    sys.stdout.write(result or "")


if __name__ == "__main__":
    main()
