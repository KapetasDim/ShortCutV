"""GUI-side handle for one out-of-process job (see video_editor/worker.py).

Every render, editor bake, export and batch crop/resize runs in a separate
`python -m video_editor.worker` process started via QProcess. The GUI process
-- which hosts libmpv -- therefore never runs Python worker threads or
blocking subprocesses, and the worker's PIL frame baking can use every core
without contending with the UI for the GIL.

    job = WorkerJob("render_timeline", params, parent=self)
    job.progress.connect(...)      # (fraction, message)
    job.finished.connect(...)      # (result dict) -- {"ok": bool, ...}
    job.start()
    job.cancel()
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from ..logging_config import get_logger

log = get_logger()

# The directory containing the `video_editor` package -- the worker is run as
# a module from there.
_PACKAGE_PARENT = Path(__file__).resolve().parent.parent.parent
_JOB_DIR = Path(tempfile.gettempdir()) / "video_editor_jobs"

_live_jobs: "set[WorkerJob]" = set()


class WorkerJob(QObject):
    progress = Signal(float, str)
    finished = Signal(dict)

    def __init__(self, kind: str, params: dict, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.kind = kind
        self._params = params
        self._proc: Optional[QProcess] = None
        self._worker_pid: Optional[int] = None
        self._stdout_buf = b""
        self._stderr_buf = b""
        self._result: Optional[dict] = None
        self._cancelled = False
        self._done = False
        self._job_file: Optional[Path] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and not self._done

    def start(self) -> None:
        _JOB_DIR.mkdir(exist_ok=True)
        self._job_file = _JOB_DIR / f"{self.kind}_{uuid.uuid4().hex}.json"
        self._job_file.write_text(json.dumps({"kind": self.kind, "params": self._params,
                                              "gui_pid": os.getpid()}), encoding="utf-8")
        proc = QProcess(self)
        proc.setWorkingDirectory(str(_PACKAGE_PARENT))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        _live_jobs.add(self)
        log.info("Worker: starting %s", self.kind)
        proc.start(sys.executable, ["-m", "video_editor.worker", str(self._job_file)])

    def cancel(self) -> None:
        """Kills the worker; its job object takes ffmpeg/pool children with it.
        `finished` still fires, with {"ok": False, "cancelled": True}."""
        if self._done or self._proc is None:
            return
        self._cancelled = True
        self._kill_worker_pid()
        # Only once the real worker is gone: killing the venv launcher shim
        # first could close our stdout pipe before the PID line arrives.
        if self._worker_pid:
            self._proc.kill()

    def _kill_worker_pid(self):
        if self._worker_pid:
            try:
                os.kill(self._worker_pid, signal.SIGTERM)  # TerminateProcess on Windows
            except OSError:
                pass

    # ----------------------------------------------------------- plumbing --

    def _on_stdout(self):
        self._stdout_buf += bytes(self._proc.readAllStandardOutput())
        *lines, self._stdout_buf = self._stdout_buf.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            tag, _, rest = line.partition("\t")
            if tag == "PROGRESS":
                frac, _, msg = rest.partition("\t")
                try:
                    self.progress.emit(float(frac), msg)
                except ValueError:
                    pass
            elif tag == "RESULT":
                try:
                    self._result = json.loads(rest)
                except ValueError:
                    self._result = {"ok": False, "error": "unparseable worker result"}
            elif tag == "PID":
                try:
                    self._worker_pid = int(rest)
                except ValueError:
                    pass
                else:
                    if self._cancelled:  # cancel() arrived before the pid did
                        self._kill_worker_pid()

    def _on_stderr(self):
        self._stderr_buf += bytes(self._proc.readAllStandardError())
        *lines, self._stderr_buf = self._stderr_buf.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log.info("[worker] %s", line)

    def _on_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            log.error("Worker: failed to start %s (%s)", self.kind, self._proc.errorString())
            self._finish({"ok": False, "error": "worker failed to start"})

    def _on_finished(self, exit_code, _status):
        self._on_stdout()
        if self._stderr_buf.strip():
            log.info("[worker] %s", self._stderr_buf.decode("utf-8", "replace").rstrip())
        if self._cancelled:
            result = {"ok": False, "cancelled": True}
        elif self._result is not None:
            result = self._result
        else:
            result = {"ok": False, "error": f"worker exited with code {exit_code}"}
        self._finish(result)

    def _finish(self, result: dict):
        if self._done:
            return
        self._done = True
        _live_jobs.discard(self)
        if self._job_file is not None:
            self._job_file.unlink(missing_ok=True)
        log.info("Worker: %s finished: %s", self.kind, result)
        self.finished.emit(result)
        if self._proc is not None:
            self._proc.deleteLater()


def cancel_all() -> None:
    """Kills every running worker. Call on app close."""
    for job in list(_live_jobs):
        job.cancel()
        if job._proc is not None:
            # A worker that hasn't reported its pid yet exits on its own once
            # the GUI process is gone (it watches gui_pid).
            job._proc.kill()
            job._proc.waitForFinished(2000)


def sweep_stale_job_files() -> None:
    if _JOB_DIR.exists():
        for f in _JOB_DIR.glob("*.json"):
            f.unlink(missing_ok=True)
