"""Builds the ffmpeg command for a timeline-block thumbnail and runs it through
the shared job_queue. Caching (by clip content, not timeline position) lives
in timeline_widget.py."""
from __future__ import annotations

import io
from typing import Callable

from PIL import Image

from ..logging_config import get_logger
from ..project import TimelineClip
from .job_queue import NORMAL, JobHandle, submit

log = get_logger()

THUMB_W = 160
GROUP = "thumbs"


def request_thumbnail(clip: TimelineClip, on_done: Callable[[bytes], None]) -> JobHandle | None:
    """on_done(png_bytes) -- not called at all if extraction fails."""
    if clip.is_image:
        try:
            with Image.open(clip.path) as img:
                img.thumbnail((THUMB_W, THUMB_W))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
            on_done(buf.getvalue())
        except Exception:
            log.exception("Thumbnail worker: failed to build image thumbnail for %s", clip.path)
        return None

    mid = clip.in_s + clip.duration / 2
    args = ["-v", "error", "-ss", str(mid), "-i", str(clip.path),
            "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2",
            "-f", "image2pipe", "-vcodec", "png", "pipe:1"]

    def done(_exit_code: int, data: bytes):
        if data:
            on_done(data)
        else:
            log.warning("Thumbnail worker: ffmpeg produced no output for %s", clip.path.name)

    return submit("ffmpeg", args, done, NORMAL, GROUP)
