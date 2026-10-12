"""Multi-monitor helper. Tk on Windows only ever reports the *primary*
monitor's size via winfo_screenwidth()/winfo_screenheight(), regardless of
which monitor a window actually sits on -- there's no per-monitor query in
Tcl/Tk itself. Anything that needs "the monitor this window is on" (the busy
overlay's stale-size fallback, positioning the dialog-worker subprocess) has
to go through the Win32 API directly instead.

Separately: Tk's own winfo_rootx()/winfo_rooty()/winfo_width()/winfo_height()
are unreliable for a zoomed (maximized) toplevel on Windows -- they can keep
reporting the pre-maximize geometry instead of the window's real on-screen
position, which is exactly the state App.__init__ puts the main window in
(self.state("zoomed")). window_rect_for_hwnd() below queries the real,
current bounds straight from Win32 (GetWindowRect), sidestepping that quirk
entirely.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes

from .logging_config import get_logger

log = get_logger()

MONITOR_DEFAULTTONEAREST = 2


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


def monitor_bounds_for_hwnd(hwnd: int) -> tuple[int, int, int, int] | None:
    """Returns (x, y, w, h) of the monitor containing the given window handle,
    or None if the query fails (e.g. not running on Windows)."""
    try:
        monitor = ctypes.windll.user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        log.exception("monitor_geometry: failed to query monitor bounds for hwnd=%s", hwnd)
        return None


def window_rect_for_hwnd(hwnd: int) -> tuple[int, int, int, int] | None:
    """Returns (x, y, w, h) of the window's real current on-screen bounds via
    GetWindowRect, or None if the query fails."""
    try:
        rect = _RECT()
        if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        log.exception("monitor_geometry: failed to query window rect for hwnd=%s", hwnd)
        return None
