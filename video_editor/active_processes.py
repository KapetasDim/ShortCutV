"""Registry of live ffmpeg subprocess.Popen handles started via
ffmpeg_ops._run_with_progress (exports, batch crop/resize, renders) --
anything long-running enough that leaving it running past app close would
mean an orphaned ffmpeg process. QProcess-based work (thumbnails, metadata
probes, waveforms) is unrelated -- that's tracked by qt_ui/job_queue.py
instead.

Renders already have their own cancel button wired through a proc_holder
dict; registering here too is redundant for them but harmless. Exports and
batch crop/resize had no such handle at all, so closing the app mid-export
used to leave ffmpeg running headless with nothing left to stop it.
"""
from __future__ import annotations

import subprocess

_active: set[subprocess.Popen] = set()


def register(proc: subprocess.Popen) -> None:
    _active.add(proc)


def unregister(proc: subprocess.Popen) -> None:
    _active.discard(proc)


def kill_all() -> None:
    """Terminates every still-running registered process. Call on app close."""
    for proc in list(_active):
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    _active.clear()
