import os
import sys

# libmpv-2.dll lives next to this script; python-mpv only looks in %PATH% or
# its own package folder, so add this script's directory to PATH before import.
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ["PATH"]

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
