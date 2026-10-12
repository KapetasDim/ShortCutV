"""Generates timeline-block thumbnails through the shared job_queue.

ffmpeg is driven via QProcess off Qt's own GUI-thread event loop (never a
Python thread + subprocess, which reproducibly crashed the process near
libmpv's own thread setup on this platform). The job_queue additionally caps
how many run at once, so adding many clips at once doesn't spawn an unbounded
pile of ffmpeg processes.
"""
from __future__ import annotations

import io
from typing import Callable

from PIL import Image

from ..logging_config import get_logger
from ..project import TimelineClip
from .job_queue import submit

log = get_logger()

_THUMB_W = 160


def request_thumbnail(index: int, clip: TimelineClip, on_done: Callable[[int, bytes], None]):
    if clip.is_image:
        try:
            with Image.open(clip.path) as img:
                img.thumbnail((_THUMB_W, _THUMB_W))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                on_done(index, buf.getvalue())
        except Exception:
            log.exception("Thumbnail worker: failed to build image thumbnail for %s", clip.path)
        return

    mid = clip.in_s + clip.duration / 2
    args = ["-y", "-ss", str(mid), "-i", str(clip.path),
            "-frames:v", "1", "-vf", f"scale={_THUMB_W}:-2",
            "-f", "image2pipe", "-vcodec", "png", "pipe:1"]

    def done(_exit_code: int, data: bytes):
        if data:
            on_done(index, data)
        else:
            log.warning("Thumbnail worker: ffmpeg produced no output for clip index %d", index)

    submit("ffmpeg", args, done)
