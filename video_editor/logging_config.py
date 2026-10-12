import logging
import sys

LOGGER_NAME = "video_editor"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger  # already configured (e.g. re-entrant import)

    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


class TkTextHandler(logging.Handler):
    """Mirrors formatted log records into a Tk Text widget, in addition to
    whatever other handlers are attached (the console one from
    configure_logging()). Log calls happen from background threads all over
    this app (render/ffmpeg worker threads) as well as the main thread, and Tk
    widgets may only be touched from the Tk main loop -- emit() never touches
    the widget directly, it hands the append off via widget.after(0, ...),
    the same thread-to-UI marshalling pattern used for progress callbacks
    throughout ffmpeg_ops.py."""

    MAX_LINES = 2000  # oldest lines trimmed past this, so a long session's log can't grow unbounded

    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def emit(self, record):
        msg = self.format(record)
        try:
            self._widget.after(0, self._append, msg)
        except Exception:
            pass  # widget already destroyed (app closing / tab rebuilt) -- drop silently

    def _append(self, msg: str):
        try:
            self._widget.configure(state="normal")
            self._widget.insert("end", msg + "\n")
            overflow = int(self._widget.index("end-1c").split(".")[0]) - self.MAX_LINES
            if overflow > 0:
                self._widget.delete("1.0", f"{overflow + 1}.0")
            self._widget.see("end")
            self._widget.configure(state="disabled")
        except Exception:
            pass  # widget already destroyed


def attach_text_widget(widget) -> None:
    """Mirrors all logging output into `widget` (a Tk Text) in addition to the
    console. Removes any previously attached TkTextHandler first, so calling
    this again after the widget is rebuilt (see TimelineTab.rebuild(), which
    tears down and recreates the whole tab on a UI scale-profile switch)
    replaces the stale handler instead of piling up a new one on top of it --
    otherwise every rebuild would double up log lines and eventually try to
    write into destroyed widgets."""
    logger = get_logger()
    for h in list(logger.handlers):
        if isinstance(h, TkTextHandler):
            logger.removeHandler(h)
    handler = TkTextHandler(widget)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    logger.addHandler(handler)
