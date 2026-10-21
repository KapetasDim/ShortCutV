"""The GUI's single media-probe cache: duration, size, audio presence.

Replaces three separate caches (Bin rows, add-clip probes, and the clip
editor's synchronous ffprobe on the GUI thread). Everything that needs to
know about a file asks here; a file is probed once per (mtime, size) no
matter how many places want it, concurrent requests for the same file share
one ffprobe, and renders receive these results as hints so the worker
doesn't re-probe every clip either (see probe_hints()).

Probing runs through the shared job_queue (QProcess on the GUI event loop).
Images are read with PIL directly -- header-only, effectively instant.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from PIL import Image

from ..logging_config import get_logger
from .job_queue import NORMAL, JobHandle, submit

log = get_logger()


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    width: int
    height: int
    has_audio: bool
    codec: str = ""
    fps: float = 30.0

    @property
    def ok(self) -> bool:
        return self.duration_s > 0 or (self.width > 0 and self.height > 0)


EMPTY = MediaInfo(0.0, 0, 0, False)
DoneCB = Callable[[MediaInfo], None]

_CACHE_MAX = 3000
_CACHE: "OrderedDict[tuple[str, int, int], MediaInfo]" = OrderedDict()
_INFLIGHT: dict[tuple[str, int, int], tuple[JobHandle, list[DoneCB]]] = {}


def _identity(path: Path) -> Optional[tuple[str, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def cached(path: Path) -> Optional[MediaInfo]:
    key = _identity(Path(path))
    if key is None:
        return None
    info = _CACHE.get(key)
    if info is not None:
        _CACHE.move_to_end(key)
    return info


def _store(key, info: MediaInfo):
    # A failed probe is never cached, so a transient failure (file still
    # being written, network drive hiccup) is retried next time.
    if not info.ok:
        return
    _CACHE[key] = info
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def request(path: Path, on_done: DoneCB, kind: str = "video", priority: int = NORMAL,
            group: Optional[str] = None) -> None:
    """Calls on_done(MediaInfo) -- immediately if cached, otherwise once the
    probe finishes. Always called, with EMPTY (info.ok == False) on failure,
    so callers can tell "unreadable" apart from "still waiting"."""
    path = Path(path)
    key = _identity(path)
    if key is None:
        on_done(EMPTY)
        return
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        on_done(hit)
        return

    if kind == "image":
        try:
            with Image.open(path) as img:
                info = MediaInfo(0.0, img.width, img.height, False)
        except Exception:
            log.exception("Media info: failed to read image size for %s", path)
            info = EMPTY
        _store(key, info)
        on_done(info)
        return

    pending = _INFLIGHT.get(key)
    if pending is not None:
        job, callbacks = pending
        callbacks.append(on_done)
        # Re-prioritize: a HIGH request shouldn't wait behind the LOW job a
        # Bin refresh already queued for the same file.
        if priority < job.priority and job.proc is None:
            job.cancel()
            _INFLIGHT.pop(key)
            for cb in callbacks:
                request(path, cb, kind, priority, group)
        return

    args = ["-v", "error", "-print_format", "json",
            "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate",
            "-show_entries", "format=duration", str(path)]

    def done(_exit_code: int, data: bytes):
        _job, callbacks = _INFLIGHT.pop(key, (None, []))
        info = EMPTY
        try:
            parsed = json.loads(data.decode("utf-8", "replace") or "{}")
            streams = parsed.get("streams", [])
            v = next((s for s in streams if s.get("codec_type") == "video"), {})
            num, den = (v.get("r_frame_rate") or "30/1").split("/")
            info = MediaInfo(
                duration_s=float(parsed.get("format", {}).get("duration", 0.0) or 0.0),
                width=int(v.get("width", 0) or 0), height=int(v.get("height", 0) or 0),
                has_audio=any(s.get("codec_type") == "audio" for s in streams),
                codec=v.get("codec_name", ""), fps=float(num) / float(den) if float(den) else 30.0)
        except Exception:
            log.exception("Media info: failed to parse ffprobe output for %s", path)
        _store(key, info)
        for cb in callbacks:
            try:
                cb(info)
            except Exception:
                log.exception("Media info: callback failed for %s", path)

    job = submit("ffprobe", args, done, priority, group)
    _INFLIGHT[key] = (job, [on_done])


def forget_cancelled() -> None:
    """Drops in-flight bookkeeping for jobs cancelled via
    job_queue.cancel_group, so the next request re-submits them."""
    for key, (job, _cbs) in list(_INFLIGHT.items()):
        if job.cancelled:
            _INFLIGHT.pop(key, None)


def probe_hints(paths: Iterable[str]) -> list[dict]:
    """Cached results for `paths`, tagged with the mtime/size they were taken
    at, for the render worker to seed its own probe cache with."""
    hints = []
    for p in set(paths):
        key = _identity(Path(p))
        info = _CACHE.get(key) if key else None
        if info is not None:
            hints.append({"path": key[0], "mtime_ns": key[1], "size": key[2], **asdict(info)})
    return hints
