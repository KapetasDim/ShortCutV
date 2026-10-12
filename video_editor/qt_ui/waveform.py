"""Audio waveform extraction for the timeline's audio lane.

Decodes a low sample-rate mono PCM stream via ffmpeg (cheap: ~8kb/s of source
audio) and reduces it to (min, max) peak pairs bucketed to however many columns
the caller wants to draw.

The ffmpeg decode is dispatched through the shared job_queue (a QProcess pool
on the GUI event loop), NOT a Python thread -- the old threading.Thread +
subprocess path here was the one remaining copy of the pattern that
reproducibly crashed the process near libmpv (see thumbnail_worker.py). This
module now only builds the command and reduces the raw PCM bytes it returns;
the timeline submits the command and feeds the bytes back in.
"""
from __future__ import annotations

import struct
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from ..logging_config import get_logger

log = get_logger()

_SAMPLE_RATE = 4000  # Hz -- plenty for a min/max peak overview, keeps decode/memory small

# Keyed by (path, mtime_ns, start_s, duration_s) -> the reduced peaks, oldest
# access first. Reused across redraws so a sound clip's waveform is only
# decoded once. mtime is part of the key so a re-exported/edited file gets a
# fresh decode instead of showing its old waveform forever; the OrderedDict
# + _CACHE_MAX cap bounds memory since nothing else evicts entries for files
# that keep changing across a long session.
_CACHE_MAX = 300
_CACHE: "OrderedDict[tuple[str, int, float, float], list[tuple[float, float]]]" = OrderedDict()


def _cache_key(path: Path, start_s: float, duration_s: float | None) -> Optional[tuple[str, int, float, float]]:
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    return (str(path), mtime_ns, start_s, duration_s if duration_s is not None else -1.0)


def cached_peaks(path: Path, num_buckets: int, start_s: float = 0.0,
                 duration_s: float | None = None) -> list[tuple[float, float]] | None:
    """Returns already-computed peaks for this request, or None if not cached
    (or cached at a different bucket count and thus stale for this draw)."""
    key = _cache_key(path, start_s, duration_s)
    if key is None:
        return None
    cached = _CACHE.get(key)
    if cached is not None and len(cached) == num_buckets:
        _CACHE.move_to_end(key)
        return cached
    return None


def clear_cache():
    """Drops every cached waveform and its pending/done bookkeeping is the
    caller's job (see timeline_widget.reset_waveform_state) -- call both on
    New/Load Project so a freshly loaded project starts from a clean slate
    instead of reusing whatever the previous project happened to touch."""
    _CACHE.clear()


def ffmpeg_args(path: Path, start_s: float = 0.0,
                duration_s: float | None = None) -> list[str]:
    """The ffmpeg arguments (everything after the "ffmpeg" program name) that
    decode `path`'s audio to raw mono s16le PCM on stdout."""
    args = ["-v", "quiet", "-ss", str(max(0.0, start_s))]
    if duration_s is not None:
        args += ["-t", str(max(0.01, duration_s))]
    args += ["-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(_SAMPLE_RATE),
             "-map", "0:a:0?", "pipe:1"]
    return args


def reduce_pcm_to_peaks(raw: bytes, num_buckets: int, path: Path,
                        start_s: float = 0.0, duration_s: float | None = None
                        ) -> list[tuple[float, float]]:
    """Reduces raw mono s16le PCM to `num_buckets` (min, max) pairs in [-1, 1]
    and caches the result. Empty list if there was no audio / no data."""
    num_buckets = max(1, int(num_buckets))
    n_samples = len(raw) // 2
    if n_samples == 0:
        return []
    samples = struct.unpack(f"<{n_samples}h", raw[:n_samples * 2])

    per_bucket = max(1, n_samples // num_buckets)
    peaks: list[tuple[float, float]] = []
    for i in range(num_buckets):
        lo = i * per_bucket
        hi = min(n_samples, lo + per_bucket) if i < num_buckets - 1 else n_samples
        if lo >= hi:
            peaks.append((0.0, 0.0))
            continue
        chunk = samples[lo:hi]
        peaks.append((min(chunk) / 32768.0, max(chunk) / 32768.0))

    key = _cache_key(path, start_s, duration_s)
    if key is not None:
        _CACHE[key] = peaks
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return peaks
