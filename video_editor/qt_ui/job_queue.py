"""Shared, capped pool for short-lived background ffmpeg/ffprobe processes
(timeline thumbnails, media probes, audio waveforms, frame grabs).

Runs at most MAX_CONCURRENT jobs at a time; the rest wait in a backlog that
is ordered by priority first, then FIFO. A user-initiated probe (adding a
clip) is HIGH and jumps ahead of a Bin refresh's hundreds of LOW probes
instead of waiting behind them.

Jobs can be cancelled individually (the handle returned by submit) or by
group (e.g. every probe from the previous Bin refresh). A cancelled job's
callback never fires; a running one is killed.

Drives QProcess off Qt's GUI event loop -- no Python threads in the GUI
process (see worker_client.py for long jobs).

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

HIGH, NORMAL, LOW = 0, 1, 2

# on_done(exit_code: int, stdout: bytes) -- stderr is drained and discarded so
# it can't fill the OS pipe buffer and wedge the child process.
DoneCB = Callable[[int, bytes], None]


class JobHandle:
    __slots__ = ("program", "args", "on_done", "priority", "group", "cancelled", "proc")

    def __init__(self, program: str, args: list[str], on_done: DoneCB, priority: int, group: Optional[str]):
        self.program = program
        self.args = args
        self.on_done = on_done
        self.priority = priority
        self.group = group
        self.cancelled = False
        self.proc: Optional[QProcess] = None

    def cancel(self) -> None:
        get_queue().cancel(self)


class _ProcessQueue(QObject):
    counts_changed = Signal(int, int)  # (active, queued)

    def __init__(self):
        super().__init__()
        self._pending: dict[int, deque[JobHandle]] = {HIGH: deque(), NORMAL: deque(), LOW: deque()}
        self._active: set[JobHandle] = set()

    # -------------------------------------------------------------- public --

    def submit(self, program: str, args: list[str], on_done: DoneCB, priority: int = NORMAL,
               group: Optional[str] = None) -> JobHandle:
        """Queues `program args`; calls on_done(exit_code, stdout_bytes) on the
        GUI thread when it finishes. Safe to call from the GUI thread only."""
        job = JobHandle(program, args, on_done, priority, group)
        self._pending[priority].append(job)
        self._pump()
        return job

    def cancel(self, job: JobHandle) -> None:
        if job.cancelled:
            return
        job.cancelled = True
        for q in self._pending.values():
            try:
                q.remove(job)
            except ValueError:
                pass
        if job.proc is not None:
            try:
                job.proc.kill()
            except RuntimeError:
                pass
        self._emit_counts()

    def cancel_group(self, group: str) -> None:
        for q in self._pending.values():
            for job in [j for j in q if j.group == group]:
                job.cancelled = True
                q.remove(job)
        for job in [j for j in self._active if j.group == group]:
            self.cancel(job)
        self._emit_counts()

    def counts(self) -> tuple[int, int]:
        return len(self._active), sum(len(q) for q in self._pending.values())

    def shutdown(self):
        """Drops the backlog and kills every in-flight process. Call on app
        close -- otherwise Qt destroys still-running QProcess objects out from
        under their own handlers and an ffmpeg child can outlive the GUI."""
        for q in self._pending.values():
            q.clear()
        for job in list(self._active):
            self.cancel(job)

    # ------------------------------------------------------------- internal --

    def _next(self) -> Optional[JobHandle]:
        for prio in (HIGH, NORMAL, LOW):
            if self._pending[prio]:
                return self._pending[prio].popleft()
        return None

    def _pump(self):
        while len(self._active) < MAX_CONCURRENT:
            job = self._next()
            if job is None:
                break
            self._start(job)
        self._emit_counts()

    def _start(self, job: JobHandle):
        proc = QProcess(self)  # parented so Qt manages its lifetime deterministically
        job.proc = proc
        self._active.add(job)
        stdout = bytearray()
        settled = False  # guards errorOccurred + finished both firing for the same process

        # OS pipe buffers are ~64KB on Windows: both channels must be drained
        # as data arrives, or a large stdout (raw PCM, a PNG frame) or a
        # chatty stderr wedges the child before it can exit.
        def read_stdout():
            try:
                stdout.extend(bytes(proc.readAllStandardOutput()))
            except RuntimeError:
                pass

        def read_stderr():
            try:
                proc.readAllStandardError()
            except RuntimeError:
                pass

        def settle(exit_code, data):
            nonlocal settled
            if settled:
                return
            settled = True
            self._active.discard(job)
            job.proc = None
            try:
                if not job.cancelled:
                    job.on_done(exit_code, data)
            except Exception:
                log.exception("Job queue: callback failed for %s", job.program)
            finally:
                proc.deleteLater()
                self._pump()

        def finished(exit_code, _status):
            read_stdout()
            settle(exit_code, bytes(stdout))

        def error_occurred(error):
            if job.cancelled:
                settle(-1, b"")
                return
            # QProcess.start() never raises for a missing binary -- that is
            # reported here instead, and `finished` never fires for it.
            # Without settling here the job would occupy a slot forever.
            if error == QProcess.ProcessError.FailedToStart:
                log.error("Job queue: %s failed to start (%s)", job.program, proc.errorString())
                settle(-1, b"")

        proc.readyReadStandardOutput.connect(read_stdout)
        proc.readyReadStandardError.connect(read_stderr)
        proc.finished.connect(finished)
        proc.errorOccurred.connect(error_occurred)
        proc.start(job.program, job.args)

    def _emit_counts(self):
        self.counts_changed.emit(*self.counts())


_queue: Optional[_ProcessQueue] = None


def get_queue() -> _ProcessQueue:
    global _queue
    if _queue is None:
        _queue = _ProcessQueue()
    return _queue


def submit(program: str, args: list[str], on_done: DoneCB, priority: int = NORMAL,
           group: Optional[str] = None) -> JobHandle:
    return get_queue().submit(program, args, on_done, priority, group)


def cancel_group(group: str) -> None:
    get_queue().cancel_group(group)


def shutdown() -> None:
    get_queue().shutdown()
