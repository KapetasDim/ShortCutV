"""Audio waveform peaks for the timeline's audio lane.

Each source file is decoded ONCE (low-rate mono PCM via ffmpeg, through the
shared job_queue) into a fixed-resolution min/max envelope covering the whole
file. A sound clip's trim window (clip_start_s / clip_duration_s) is then
just a slice of that envelope -- trimming a clip, or placing the same file
twice with different offsets, never re-decodes.

The PCM -> envelope reduction runs on the GUI thread, so it avoids per-sample
Python loops: strided memoryview slices fed to map(max/min, ...) keep the
iteration in C (a 10-minute track reduces in well under 100 ms).
"""
from __future__ import annotations

from array import array
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..logging_config import get_logger

log = get_logger()

SAMPLE_RATE = 4000      # Hz decoded -- plenty for an overview, ~8 KB per second of audio
PEAKS_PER_S = 200       # envelope resolution; the draw step downsamples to on-screen width
_SAMPLES_PER_PEAK = SAMPLE_RATE // PEAKS_PER_S

_CACHE_MAX = 100
FileKey = tuple[str, int, int]


@dataclass
class FilePeaks:
    mins: array
    maxs: array

    def segment(self, start_s: float, duration_s: float) -> list[tuple[float, float]]:
        a = max(0, int(start_s * PEAKS_PER_S))
        b = min(len(self.maxs), a + max(1, int(duration_s * PEAKS_PER_S)))
        return list(zip(self.mins[a:b], self.maxs[a:b]))


_CACHE: "OrderedDict[FileKey, FilePeaks]" = OrderedDict()


def file_key(path: Path) -> Optional[FileKey]:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def cached(key: FileKey) -> Optional[FilePeaks]:
    peaks = _CACHE.get(key)
    if peaks is not None:
        _CACHE.move_to_end(key)
    return peaks


def clear_cache():
    """Drops every cached envelope. The timeline's pending/done bookkeeping is
    reset separately (timeline_widget.reset_waveform_state)."""
    _CACHE.clear()


def ffmpeg_args(path: Path) -> list[str]:
    """Arguments (after the "ffmpeg" program name) that decode the whole of
    `path`'s first audio stream to raw mono s16le PCM on stdout."""
    return ["-v", "quiet", "-i", str(path), "-map", "0:a:0?", "-f", "s16le", "-ac", "1",
            "-ar", str(SAMPLE_RATE), "pipe:1"]


def reduce_and_store(key: FileKey, raw: bytes) -> FilePeaks:
    n_peaks = (len(raw) // 2) // _SAMPLES_PER_PEAK
    if n_peaks == 0:
        peaks = FilePeaks(array("f"), array("f"))
    else:
        samples = memoryview(raw)[: n_peaks * _SAMPLES_PER_PEAK * 2].cast("h")
        # Column k holds sample k of every bucket; map(max, *columns) then
        # walks all buckets in lockstep inside C.
        columns = [samples[k::_SAMPLES_PER_PEAK] for k in range(_SAMPLES_PER_PEAK)]
        scale = 1.0 / 32768.0
        peaks = FilePeaks(array("f", (v * scale for v in map(min, *columns))),
                          array("f", (v * scale for v in map(max, *columns))))
    _CACHE[key] = peaks
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return peaks
