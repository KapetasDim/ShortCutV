import lzma
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_libmpv():
    """libmpv-2.dll is ~115MB -- over GitHub's 100MB file limit -- so the repo
    stores it xz-compressed (~31MB) and it's unpacked here on first launch.
    Written to a temp name and renamed, so an interrupted unpack never leaves
    a truncated DLL that would then fail to load on every later launch."""
    dll = os.path.join(_HERE, "libmpv-2.dll")
    packed = dll + ".xz"
    if os.path.exists(dll) or not os.path.exists(packed):
        return
    print("First launch: unpacking libmpv-2.dll...", flush=True)
    tmp = dll + ".tmp"
    with lzma.open(packed, "rb") as src, open(tmp, "wb") as dst:
        while chunk := src.read(8 * 1024 * 1024):
            dst.write(chunk)
    os.replace(tmp, dll)


_ensure_libmpv()

# libmpv-2.dll lives next to this script; python-mpv only looks in %PATH% or
# its own package folder, so add this script's directory to PATH before import.
os.environ["PATH"] = _HERE + os.pathsep + os.environ["PATH"]

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ShortCutV_Logo.png")

from video_editor.logging_config import configure_logging
from video_editor.qt_ui.app import App
from video_editor.qt_ui.theme import build_stylesheet


def _install_crash_safety_net():
    """Last-resort catch-all: every button/action handler already logs +
    guards itself (see qt_ui/util.py's connect_button), but this covers
    anything outside that -- a raw signal connection, a Qt-internal
    callback, etc. -- so a bug there is logged (and thus mirrored to both the
    console and the in-app Log dock) instead of the process aborting or
    silently locking up."""
    from video_editor.logging_config import get_logger
    log = get_logger()

    def handle(exc_type, exc_value, exc_tb):
        log.error("[FATAL-GUARDED] Unhandled exception (app is still running):",
                  exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = handle


def main():
    configure_logging()
    _install_crash_safety_net()
    # Qt (6.0+) enables High-DPI scaling and per-monitor-aware rendering by
    # default -- unlike Tk, it needs no manual SetProcessDpiAwarenessContext
    # dance, and doing one manually before QApplication() would fight Qt's
    # own DPI setup instead of helping it.
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet())
    app.setWindowIcon(QIcon(_LOGO_PATH))
    window = App()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
