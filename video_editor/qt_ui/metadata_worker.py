"""Async duration/resolution probing for Bin rows, through the shared
job_queue (QProcess pool on the GUI event loop -- see thumbnail_worker.py for
why this avoids Python threading + subprocess, and job_queue.py for why the
number of concurrent probes is capped)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from PIL import Image

from ..logging_config import get_logger
from .job_queue import submit

log = get_logger()


def request_metadata(key: object, path: Path, kind: str, on_done: Callable[[object, float, int, int], None]):
    """`on_done(key, duration_s, width, height)` -- fields that don't apply
    (e.g. width/height for a sound file) come back as 0."""
    if kind == "image":
        try:
            with Image.open(path) as img:
                w, h = img.size
            on_done(key, 0.0, w, h)
        except Exception:
            log.exception("Metadata worker: failed to read image size for %s", path)
        return

    args = [
        "-v", "error", "-print_format", "json",
        "-show_entries", "stream=codec_type,width,height",
        "-show_entries", "format=duration",
        str(path),
    ]

    def done(_exit_code: int, data: bytes):
        # on_done always fires, even on a parse failure (duration/width/
        # height come back 0) -- callers that need to tell "probed, nothing
        # there" apart from "still waiting" (e.g. add-clip reporting a
        # corrupt file) have no other signal, since this is the only
        # completion callback they get.
        duration, width, height = 0.0, 0, 0
        try:
            parsed = json.loads(data.decode("utf-8", "replace") or "{}")
            duration = float(parsed.get("format", {}).get("duration", 0.0) or 0.0)
            vstream = next((s for s in parsed.get("streams", []) if s.get("codec_type") == "video"), {})
            width, height = int(vstream.get("width", 0) or 0), int(vstream.get("height", 0) or 0)
        except Exception:
            log.exception("Metadata worker: failed to parse ffprobe output for key %r", key)
        on_done(key, duration, width, height)

    submit("ffprobe", args, done)
