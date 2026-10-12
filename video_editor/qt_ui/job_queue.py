"""Shared, capped pool for short-lived background ffmpeg/ffprobe processes
(timeline thumbnails, Bin metadata probes, audio waveforms).

Every such job used to start its own QProcess the instant it was requested,
unbounded -- dropping a folder of 50 clips launched 50 ffmpeg processes at
once, spiking CPU/IO and making everything *slower*. This queue runs at most
MAX_CONCURRENT jobs at a time and holds the rest in a FIFO backlog.

Like the individual workers before it, it drives QProcess off Qt's own GUI-
thread event loop rather than spawning OS threads from Python -- that
Python-thread + subprocess combination reproducibly crashed the process near
libmpv activity (see thumbnail_worker.py's original note), so all three job
kinds now share this single event-loop-driven path.

`counts_changed(active, queued)` fires whenever either number changes, so the
app can surface a loading indicator while work is outstanding.
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Optional

from PySide6.QtCore import QObject, QProcess, Signal

from ..logging_config import get_logger

log = get_logger()

MAX_CONCURRENT = 4

# on_done(exit_code: int, stdout: bytes) -- stderr is drained and discarded so
# it can't fill the OS pipe buffer and wedge the child process.
DoneCB = Callable[[int, bytes], None]


class _ProcessQueue(QObject):
    counts_changed = Signal(int, int)  # (active, queued)

    def __init__(self):
        super().__init__()
        self._pending: deque[tuple[str, list[str], DoneCB]] = deque()
        self._active: set[QProcess] = set()

    # -------------------------------------------------------------- public --

    def submit(self, program: str, args: list[str], on_done: DoneCB) -> None:
        """Queues `program args`; calls on_done(exit_code, stdout_bytes) on the
        GUI thread when it finishes. Safe to call from the GUI thread only."""
        self._pending.append((program, args, on_done))
        self._pump()

    def counts(self) -> tuple[int, int]:
        return len(self._active), len(self._pending)

    def shutdown(self):
        """Drops the pending backlog and kills every in-flight process. Call
        on app close -- otherwise Qt destroys still-running QProcess objects
        out from under their own event handlers (warning spam) and any
        ffmpeg/ffprobe child can outlive the GUI entirely."""
        self._pending.clear()
        for proc in list(self._active):
            try:
                proc.kill()
            except RuntimeError:
                pass

    # ------------------------------------------------------------- internal --

    def _pump(self):
        while self._pending and len(self._active) < MAX_CONCURRENT:
            program, args, on_done = self._pending.popleft()
            self._start(program, args, on_done)
        self._emit_counts()

    def _start(self, program: str, args: list[str], on_done: DoneCB):
        proc = QProcess(self)  # parented so Qt manages its lifetime deterministically
        self._active.add(proc)
        stdout = bytearray()
        settled = False  # guards errorOccurred + finished both firing for the same process

        # OS pipe buffers are ~64KB on Windows. A raw PCM waveform decode or a
        # full-res thumbnail routinely exceeds that on stdout alone, and
        # ffmpeg's own banner/log can too on stderr -- reading only after
        # `finished` (as this used to) deadlocks the child the moment either
        # pipe fills, since it then blocks on write() and can never reach
        # exit. Both channels must be drained as data arrives instead.
        def read_stdout(p=proc):
            try:
                stdout.extend(bytes(p.readAllStandardOutput()))
            except RuntimeError:
                pass

        def read_stderr(p=proc):
            try:
                p.readAllStandardError()
            except RuntimeError:
                pass

        def settle(exit_code, data, p=proc, cb=on_done):
            nonlocal settled
            if settled:
                return
            settled = True
            self._active.discard(p)
            try:
                cb(exit_code, data)
            except Exception:
                log.exception("Job queue: callback failed for %s", program)
            finally:
                p.deleteLater()
                self._pump()

        def finished(exit_code, _status, p=proc):
            read_stdout(p)
            settle(exit_code, bytes(stdout), p)

        def error_occurred(_error, p=proc):
            # QProcess.start() never raises for a missing binary/bad PATH --
            # that failure is reported here instead, and `finished` never
            # fires for it. Without this, the job sits in _active forever:
            # after MAX_CONCURRENT such failures, every later thumbnail/
            # metadata/waveform job queues behind them and never runs.
            log.error("Job queue: %s failed to start (%s)", program, p.errorString())
            settle(-1, b"", p)

        proc.readyReadStandardOutput.connect(read_stdout)
        proc.readyReadStandardError.connect(read_stderr)
        proc.finished.connect(finished)
        proc.errorOccurred.connect(error_occurred)
        try:
            proc.start(program, args)
        except Exception:
            log.exception("Job queue: failed to start %s", program)
            self._active.discard(proc)
            proc.deleteLater()

    def _emit_counts(self):
        self.counts_changed.emit(len(self._active), len(self._pending))


_queue: Optional[_ProcessQueue] = None


def get_queue() -> _ProcessQueue:
    global _queue
    if _queue is None:
        _queue = _ProcessQueue()
    return _queue


def submit(program: str, args: list[str], on_done: DoneCB) -> None:
    get_queue().submit(program, args, on_done)


def shutdown() -> None:
    get_queue().shutdown()
