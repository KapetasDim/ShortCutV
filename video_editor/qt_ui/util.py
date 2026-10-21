"""connect_button: a small wrapper every button/action handler in the app is
wired through so that (a) every click is logged to the console before its
handler runs, for diagnosing a hang/freeze after the fact, and (b) an
exception inside a handler prints a traceback instead of propagating out of
a Qt slot -- an uncaught exception escaping a slot is a real source of the
app going unresponsive or aborting outright, not just a cosmetic issue.

(There is no cross-thread marshalling helper any more: the GUI process runs
no Python worker threads -- long jobs run in a separate worker process, see
worker_client.py.)
"""
from __future__ import annotations

from typing import Callable

from ..logging_config import get_logger

log = get_logger()


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
