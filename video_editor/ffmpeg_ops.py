"""All ffmpeg/ffprobe interaction lives here. Runs only inside the render worker
process (video_editor.worker) -- the GUI process never calls into this module
directly; it launches the worker via QProcess instead (see
qt_ui/worker_client.py).

Every long-running function accepts an optional progress_cb(fraction, message).
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
from collections import OrderedDict
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import active_processes, render_assets, text_render
from .logging_config import get_logger
from .media_types import REMUX_SAFE_VIDEO_EXTS
from .timing import FPS, clip_layout, edge_transition_duration

log = get_logger()

ProgressCB = Optional[Callable[[float, str], None]]

# Set by the worker when the GUI asks it to stop (or disappears). Checked
# between render phases; in-flight ffmpeg children are killed separately via
# active_processes.kill_all().
CANCELLED = threading.Event()

# No console window flashing up for every ffmpeg child when the app was
# started from pythonw / a shortcut.
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# How many proxy segments to encode at once. x264 is already multithreaded,
# but at proxy resolutions a single encode can't saturate a modern CPU.
SEGMENT_PARALLEL = max(1, min(3, (os.cpu_count() or 2) // 2))

_WIPE_BY_DIRECTION = {"left": "wipeleft", "right": "wiperight", "up": "wipeup", "down": "wipedown"}
_SLIDE_BY_DIRECTION = {"left": "slideleft", "right": "slideright", "up": "slideup", "down": "slidedown"}


class Cancelled(Exception):
    pass


def _check_cancelled():
    if CANCELLED.is_set():
        raise Cancelled()


def xfade_name(transition_type: str, direction: str) -> str:
    if transition_type == "crossfade":
        return "fade"
    if transition_type == "fade_black":
        return "fadeblack"
    if transition_type == "wipe":
        return _WIPE_BY_DIRECTION.get(direction, "wipeleft")
    if transition_type == "slide":
        return _SLIDE_BY_DIRECTION.get(direction, "slideup")
    return "fade"


@dataclass
class ProbeResult:
    duration_s: float
    width: int
    height: int
    codec: str
    fps: float
    has_audio: bool


# Keyed by (str(path), mtime_ns, size) -> ProbeResult, oldest access first.
# Shared by the render thread pool inside the worker, hence the lock: the
# get/move_to_end/popitem sequence isn't atomic as a whole.
_PROBE_CACHE_MAX = 500
_PROBE_CACHE: "OrderedDict[tuple[str, int, int], ProbeResult]" = OrderedDict()
_PROBE_LOCK = threading.Lock()


def _probe_cache_key(path: Path) -> Optional[tuple[str, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def seed_probe_cache(hints: list[dict]) -> None:
    """Pre-fills the cache with probe results the GUI already has, so a render
    doesn't re-run ffprobe on every clip. Each hint carries the mtime/size it
    was taken at; a hint for a file that changed since is simply ignored."""
    for h in hints:
        key = (h["path"], h["mtime_ns"], h["size"])
        if _probe_cache_key(Path(h["path"])) != key:
            continue
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = ProbeResult(h["duration_s"], h["width"], h["height"],
                                            h.get("codec", ""), h.get("fps", 30.0), h["has_audio"])


def probe_video(path: Path, timeout: float = 20) -> ProbeResult:
    cache_key = _probe_cache_key(path)
    if cache_key is not None:
        with _PROBE_LOCK:
            cached = _PROBE_CACHE.get(cache_key)
            if cached is not None:
                _PROBE_CACHE.move_to_end(cache_key)
                return cached
    result = _probe_video_uncached(path, timeout)
    if cache_key is not None:
        with _PROBE_LOCK:
            _PROBE_CACHE[cache_key] = result
            _PROBE_CACHE.move_to_end(cache_key)
            while len(_PROBE_CACHE) > _PROBE_CACHE_MAX:
                _PROBE_CACHE.popitem(last=False)
    return result


def _probe_video_uncached(path: Path, timeout: float = 20) -> ProbeResult:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_entries", "stream=codec_name,codec_type,width,height,r_frame_rate",
                "-show_entries", "format=duration",
                str(path),
            ],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=_POPEN_FLAGS,
        )
    except OSError:
        log.exception("ffprobe could not be started (missing binary/PATH?) for %s", path)
        raise
    except subprocess.TimeoutExpired:
        log.error("ffprobe timed out probing %s (hung/unreadable file?)", path)
        raise
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        log.exception("ffprobe returned unparseable output for %s: %s", path, out.stderr[-500:])
        raise
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    if not vstream:
        log.warning("no video stream found while probing %s (corrupt file or audio-only?)", path)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    num, den = (vstream.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    duration_s = float(data.get("format", {}).get("duration", 0) or 0)
    return ProbeResult(
        duration_s=duration_s,
        width=int(vstream.get("width", 0)),
        height=int(vstream.get("height", 0)),
        codec=vstream.get("codec_name", ""),
        fps=fps,
        has_audio=has_audio,
    )


def _run_with_progress(
    cmd: list[str], total_s: float, progress_cb: ProgressCB, label: str,
) -> bool:
    """Runs ffmpeg with -progress piped to stdout, reports fractional progress.
    Returns True on success (exit code 0), False on failure or cancellation.

    Output is decoded as UTF-8 explicitly: ffmpeg writes UTF-8, but text=True
    alone would decode with the Windows ANSI code page, and a non-ASCII path
    in ffmpeg's log could then raise UnicodeDecodeError inside the stderr
    drain thread -- killing it silently, after which the undrained pipe fills
    and ffmpeg blocks forever. stdout (progress) and stderr are drained
    concurrently for the same pipe-buffer reason.
    """
    if CANCELLED.is_set():
        return False
    log.info("ffmpeg start: %s", label)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1, creationflags=_POPEN_FLAGS,
        )
    except OSError:
        log.exception("ffmpeg could not be started (missing binary/PATH?) for: %s", label)
        return False
    active_processes.register(proc)

    stderr_lines: list[str] = []

    def drain_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if len(stderr_lines) > 500:
                del stderr_lines[:100]

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") and total_s > 0:
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if progress_cb:
                progress_cb(max(0.0, min(1.0, (us / 1_000_000) / total_s)), label)

    proc.wait()
    active_processes.unregister(proc)
    stderr_thread.join(timeout=2)

    if CANCELLED.is_set():
        log.info("ffmpeg cancelled: %s", label)
        return False
    if proc.returncode != 0:
        log.error("ffmpeg failed (%s): %s", label, "".join(stderr_lines[-100:]))
        return False
    log.info("ffmpeg done: %s", label)
    if progress_cb:
        progress_cb(1.0, label)
    return True


def _progress_cmd(cmd: list[str]) -> list[str]:
    # Insert -progress/-nostats right after the leading "ffmpeg" token.
    return [cmd[0], "-hide_banner", "-progress", "pipe:1", "-nostats", *cmd[1:]]


# ----------------------------------------------------------- batch / export --

def crop_resize_output_path(input_path: Path, output_dir: Path) -> Path:
    if input_path.suffix.lower() in REMUX_SAFE_VIDEO_EXTS:
        return output_dir / input_path.name
    return output_dir / f"{input_path.stem}.mp4"


def run_crop_resize(
    paths: Sequence[Path],
    output_dir: Path,
    crop_fractions: tuple[float, float, float, float],  # top, bottom, left, right, 0..1 of each file's own size
    target_size: tuple[Optional[int], Optional[int]],
    progress_cb: ProgressCB = None,
) -> tuple[list[Path], list[str]]:
    """crop_fractions is expressed relative to each input's own resolution
    (see CropCanvas.get_crop_fractions) rather than as absolute pixels, so a
    batch of files with different resolutions from the previewed one still
    gets a proportionally-correct crop. Returns (written paths, failed names)."""
    top_f, bottom_f, left_f, right_f = crop_fractions
    target_w, target_h = target_size
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    failed: list[str] = []

    for i, input_path in enumerate(paths):
        if CANCELLED.is_set():
            break
        output_path = crop_resize_output_path(input_path, output_dir)
        if output_path.resolve() == input_path.resolve():
            # ffmpeg would truncate the file it's still reading from.
            log.error("Crop/Resize: refusing to overwrite source %s (output folder = input folder)", input_path)
            failed.append(input_path.name)
            continue
        try:
            probe = probe_video(input_path)
        except Exception:
            failed.append(input_path.name)
            continue
        top = round(top_f * probe.height)
        bottom = round(bottom_f * probe.height)
        left = round(left_f * probe.width)
        right = round(right_f * probe.width)
        crop_w = probe.width - left - right
        crop_h = probe.height - top - bottom
        out_w = (round(target_w) if target_w else crop_w) // 2 * 2
        out_h = (round(target_h) if target_h else crop_h) // 2 * 2
        vf = f"crop={crop_w}:{crop_h}:{left}:{top},scale={out_w}:{out_h}"
        same_container = output_path.suffix.lower() == input_path.suffix.lower()

        def file_progress(frac: float, _label: str, i=i, name=input_path.name):
            if progress_cb:
                progress_cb((i + frac) / len(paths), f"Cropping {name} ({i + 1}/{len(paths)})")

        cmd = _progress_cmd([
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", vf,
            "-c:v", "libx265" if probe.codec == "hevc" else "libx264",
            "-c:a", "copy" if same_container else "aac",
            str(output_path),
        ])
        if _run_with_progress(cmd, probe.duration_s, file_progress, f"crop {input_path.name}"):
            results.append(output_path)
        else:
            failed.append(input_path.name)
    return results, failed


def export_clip(
    path: Path, in_s: float, out_s: float, output_path: Path,
    progress_cb: ProgressCB = None,
) -> bool:
    probe = probe_video(path)
    encoder = "libx265" if probe.codec == "hevc" else "libx264"
    duration = out_s - in_s
    cmd = _progress_cmd([
        "ffmpeg", "-y",
        "-ss", str(in_s), "-i", str(path),
        "-t", str(duration),
        "-c:v", encoder, "-crf", "18", "-preset", "fast",
        "-c:a", "copy",
        str(output_path),
    ])
    return _run_with_progress(cmd, duration, progress_cb, f"export clip {output_path.name}")


def export_frame(path: Path, pos_s: float, output_path: Path) -> bool:
    log.info("ffmpeg start: export frame %s @ %.3fs", path.name, pos_s)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(pos_s), "-i", str(path), "-frames:v", "1", "-q:v", "2", str(output_path)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30, creationflags=_POPEN_FLAGS,
        )
    except OSError:
        log.exception("ffmpeg could not be started (missing binary/PATH?) for export frame %s", path.name)
        return False
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out exporting frame from %s", path.name)
        return False
    ok = result.returncode == 0
    if not ok:
        log.error("ffmpeg failed: export frame %s: %s", path.name, result.stderr[-2000:])
    else:
        log.info("ffmpeg done: export frame %s", output_path.name)
    return ok


# ------------------------------------------------------- filter-graph parts --

def _even(n: float) -> int:
    return max(2, int(round(n)) // 2 * 2)


def _video_norm(src: str, crop: str, w: int, h: int, fps: int, dur: float, out: str) -> str:
    """Scale/pad into the canvas and force an exact frame count: tpad clones
    the last frame if the source runs short (common with VFR screen
    recordings), trim cuts any excess. Every clip leaving here is exactly
    `dur` long at a constant rate -- which xfade offsets and concat both
    rely on to keep audio and video aligned."""
    return (f"{src}{crop}scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={dur:.4f},trim=duration={dur:.4f},"
            f"setpts=PTS-STARTPTS,settb=1/{fps}{out}")


def _audio_norm(src: str, volume: float, dur: float, out: str) -> str:
    return (f"{src}aformat=sample_rates=48000:channel_layouts=stereo,volume={volume:.4f},"
            f"apad,atrim=duration={dur:.4f},asetpts=PTS-STARTPTS{out}")


def _crop_filter(c) -> str:
    l, r, t, b = (getattr(c, "crop_left", 0), getattr(c, "crop_right", 0),
                  getattr(c, "crop_top", 0), getattr(c, "crop_bottom", 0))
    if not (l or r or t or b):
        return ""
    return f"crop=iw-{l + r}:ih-{t + b}:{l}:{t},"


def _image_input(asset: dict, fps: int, dur: float) -> list[str]:
    if asset["kind"] == "still":
        return ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.4f}", "-i", asset["path"]]
    return ["-framerate", str(fps), "-i", asset["pattern"]]


def _silence_input(dur: float) -> list[str]:
    return ["-f", "lavfi", "-t", f"{dur:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]


def _sound_clip_filter(sound, input_index: int, played: float, label: str) -> str:
    """One sound clip's filter chain: resample -> volume -> loop (in ORIGINAL-
    tempo samples, so `size` doesn't need a speed correction) -> speed/pitch
    (asetrate, linked) -> fades -> trim to `played` -> adelay to its absolute
    position. adelay rather than -itsoffset because amix mixes sample-for-
    sample from PTS 0 -- an itsoffset-shifted PTS wouldn't land the sound at
    the right spot; adelay pads real leading silence instead."""
    clip_duration = max(0.05, sound.clip_duration_s)
    loop_n = -1 if sound.repeat_infinite else max(0, sound.repeat_count - 1)
    size_samples = round(48000 * clip_duration)
    start_ms = round(max(0.0, sound.start_s) * 1000)

    chain = [
        f"[{input_index}:a]aformat=sample_rates=48000:channel_layouts=stereo",
        f"volume={sound.volume:.4f}",
        f"aloop=loop={loop_n}:size={size_samples}",
        f"asetrate=48000*{max(0.01, sound.speed):.4f}",
        "aresample=48000",
    ]
    if sound.fade_in_s > 0:
        chain.append(f"afade=t=in:st=0:d={sound.fade_in_s:.3f}")
    if sound.fade_out_s > 0:
        fade_out_start = max(0.0, played - sound.fade_out_s)
        chain.append(f"afade=t=out:st={fade_out_start:.3f}:d={sound.fade_out_s:.3f}")
    chain.append(f"atrim=end={played:.3f}")
    chain.append("asetpts=PTS-STARTPTS")
    chain.append(f"adelay=delays={start_ms}|{start_ms}:all=1")
    return ",".join(chain) + label


def _mix_in_sound_clips(sound_clips, total_out: float, cur_a: str, next_idx: int,
                        inputs: list[str], filters: list[str]) -> tuple[str, int]:
    """One input + filter chain per audible sound clip, mixed into cur_a via
    amix (normalize=0 so user-set volumes aren't rescaled by 1/inputs)."""
    mix_labels = [cur_a]
    for k, sound in enumerate(sound_clips):
        if sound.start_s >= total_out:
            continue
        played = sound.played_duration_s(total_out)
        if played <= 0:
            continue
        input_index = next_idx
        next_idx += 1
        inputs += ["-ss", str(sound.clip_start_s), "-i", str(sound.source)]
        label = f"[snd{k}]"
        filters.append(_sound_clip_filter(sound, input_index, played, label))
        mix_labels.append(label)

    if len(mix_labels) == 1:
        return cur_a, next_idx
    filters.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:normalize=0[amix_out]")
    return "[amix_out]", next_idx


def _text_overlay_filters(asset: dict, overlay_start: float, overlay_dur: float, end_s: float,
                          cur_v: str, input_index: int, fps: int, i: int,
                          inputs: list[str], filters: list[str]) -> str:
    if asset["kind"] == "still":
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{overlay_dur:.4f}", "-i", asset["path"]]
        chain = [f"[{input_index}:v]format=rgba"]
        if asset["fade_in"] > 0:
            chain.append(f"fade=t=in:st=0:d={asset['fade_in']:.4f}:alpha=1")
        if asset["fade_out"] > 0:
            chain.append(f"fade=t=out:st={max(0.0, overlay_dur - asset['fade_out']):.4f}"
                         f":d={asset['fade_out']:.4f}:alpha=1")
    else:
        inputs += ["-framerate", str(fps), "-i", asset["pattern"]]
        chain = [f"[{input_index}:v]format=rgba"]
    chain.append(f"setpts=PTS+{overlay_start:.4f}/TB")
    filters.append(",".join(chain) + f"[tsrc{i}]")
    out_v = f"[tv{i}]"
    filters.append(f"{cur_v}[tsrc{i}]overlay=x={asset['x']}:y={asset['y']}:eof_action=pass:"
                   f"enable='between(t,{overlay_start:.4f},{end_s:.4f})'{out_v}")
    return out_v


# ----------------------------------------------------------- proxy segments --

def _segment(clip, has_audio: bool, image_asset: Optional[dict], w: int, h: int, fps: int,
             progress_cb: ProgressCB) -> Path:
    """One clip normalized to the proxy canvas, cached on disk. Volume is
    deliberately NOT baked in (applied at assembly), so tweaking a clip's
    volume doesn't invalidate its segment."""
    dur = clip.duration
    if image_asset is not None:
        source_id = image_asset.get("path") or image_asset.get("pattern")
    else:
        source_id = render_assets.file_identity(Path(clip.source))
    key = render_assets.cache_key("segment", source_id, round(clip.in_s, 4), round(dur, 4),
                                  _crop_filter(clip), w, h, fps, has_audio)
    hit = render_assets.cached_entry(render_assets.SEGMENTS_DIR, key)
    if hit is not None:
        if progress_cb:
            progress_cb(1.0, "cached")
        return hit

    render_assets.SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = render_assets.SEGMENTS_DIR / f"{key}.{os.getpid()}_{threading.get_ident()}.partial"
    if image_asset is not None:
        inputs = _image_input(image_asset, fps, dur) + _silence_input(dur)
        crop = ""
    else:
        inputs = ["-ss", f"{clip.in_s:.4f}", "-t", f"{dur:.4f}", "-i", str(clip.source)]
        if not has_audio:
            inputs += _silence_input(dur)
        crop = _crop_filter(clip)
    a_src = "[1:a]" if (image_asset is not None or not has_audio) else "[0:a]"
    graph = ";".join([_video_norm("[0:v]", crop, w, h, fps, dur, "[v]"),
                      _audio_norm(a_src, 1.0, dur, "[a]")])
    cmd = _progress_cmd(["ffmpeg", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-g", str(fps),
                         "-c:a", "pcm_s16le", "-f", "matroska", str(tmp)])
    if not _run_with_progress(cmd, dur, progress_cb, f"proxy segment {Path(clip.source).name}"):
        tmp.unlink(missing_ok=True)
        _check_cancelled()
        raise RuntimeError(f"proxy segment failed for {clip.source}")
    return render_assets.publish(tmp, render_assets.SEGMENTS_DIR / f"{key}.mkv")


# --------------------------------------------------------------- timeline --

class _Phases:
    """Maps per-phase progress onto one 0..1 bar, weighted by expected cost."""

    def __init__(self, progress_cb: ProgressCB, weights: dict[str, float]):
        self._cb = progress_cb
        total = sum(weights.values()) or 1.0
        self._spans = {}
        acc = 0.0
        for name, w in weights.items():
            self._spans[name] = (acc / total, w / total)
            acc += w

    def report(self, phase: str, frac: float, msg: str):
        if self._cb and phase in self._spans:
            start, span = self._spans[phase]
            self._cb(start + span * max(0.0, min(1.0, frac)), msg)


def render_timeline(
    clips: Sequence, canvas_w: int, canvas_h: int, output_path: Path,
    proxy: bool = False, proxy_height: int = 270,
    progress_cb: ProgressCB = None,
    text_overlays: Sequence = (), sound_clips: Sequence = (),
    intro_type: str = "cut", intro_duration: float = 0.25, intro_direction: str = "left",
    outro_type: str = "cut", outro_duration: float = 0.25, outro_direction: str = "left",
    pool: Optional[Executor] = None,
) -> bool:
    """Composes the timeline. Clip positions come from timing.clip_layout --
    the same function the timeline widget draws with -- so what you place on
    the timeline is where it lands in the output.

    Hard cuts are real concats (no overlap); other transitions are xfade +
    acrossfade overlapping by the clamped transition duration. Intro/outro
    transitions overlap fully with a black clip and don't change the length.

    Proxy renders encode each clip once into a cached normalized segment and
    only re-run the cheap assembly on later builds. Final renders go straight
    from the sources in one pass (no intermediate generation loss); they
    still reuse cached image/text assets.

    text_overlays coordinates (x/y/font_size) are in the original canvas
    space and rescaled internally for proxy renders."""
    if not clips:
        return False
    _check_cancelled()

    orig_canvas_h = canvas_h
    if proxy:
        canvas_w, canvas_h = _even(canvas_w * proxy_height / canvas_h), _even(proxy_height)
    canvas_w, canvas_h = _even(canvas_w), _even(canvas_h)
    text_scale = canvas_h / orig_canvas_h if orig_canvas_h else 1.0
    fps = FPS

    starts, joins, total_out = clip_layout(clips)
    durations = [c.duration for c in clips]
    seg_seconds = sum(durations) if proxy else 0.0
    phases = _Phases(progress_cb, {"assets": 1.0 + 0.2 * len(text_overlays),
                                   "segments": seg_seconds * 0.6,
                                   "encode": max(1.0, total_out) * (0.4 if proxy else 1.0)})

    # -- assets (images + text), cached and parallel ------------------------
    image_assets: dict[int, dict] = {}
    image_idx = [i for i, c in enumerate(clips) if getattr(c, "is_image", False)]
    active_overlays = [t for t in text_overlays if t.start_s < total_out and t.duration_s > 0]
    n_assets = max(1, len(image_idx) + len(active_overlays))
    done_assets = 0
    for i in image_idx:
        _check_cancelled()
        image_assets[i] = render_assets.image_clip_asset(clips[i], canvas_w, canvas_h, fps, pool)
        done_assets += 1
        phases.report("assets", done_assets / n_assets, "Baking images/text...")
    text_assets = []
    for overlay in active_overlays:
        _check_cancelled()
        scaled = text_render.scale_overlay(overlay, text_scale)
        text_assets.append(render_assets.text_overlay_asset(scaled, fps, pool))
        done_assets += 1
        phases.report("assets", done_assets / n_assets, "Baking images/text...")

    has_audio = {}
    for i, c in enumerate(clips):
        if i not in image_assets:
            try:
                has_audio[i] = probe_video(Path(c.source)).has_audio
            except Exception:
                log.exception("Render: probe failed for %s, assuming no audio", c.source)
                has_audio[i] = False

    inputs: list[str] = []
    filters: list[str] = []
    v_labels: list[str] = []
    a_labels: list[str] = []
    next_idx = 0

    if proxy:
        # -- proxy: cached normalized segments, encoded in parallel ---------
        seg_progress = [0.0] * len(clips)
        seg_lock = threading.Lock()

        def seg_cb(i):
            def cb(frac, _label):
                with seg_lock:
                    seg_progress[i] = frac
                    done = sum(p * d for p, d in zip(seg_progress, durations))
                phases.report("segments", done / max(0.001, seg_seconds), "Preparing clips...")
            return cb

        with ThreadPoolExecutor(max_workers=SEGMENT_PARALLEL) as ex:
            futures = [ex.submit(_segment, c, has_audio.get(i, False), image_assets.get(i),
                                 canvas_w, canvas_h, fps, seg_cb(i)) for i, c in enumerate(clips)]
            segments = [f.result() for f in futures]
        _check_cancelled()
        for i, (c, seg) in enumerate(zip(clips, segments)):
            inputs += ["-i", str(seg)]
            filters.append(f"[{next_idx}:v]setpts=PTS-STARTPTS,settb=1/{fps}[v{i}]")
            vol = 0.0 if i in image_assets else getattr(c, "audio_volume", 0.0)
            filters.append(f"[{next_idx}:a]volume={vol:.4f},asetpts=PTS-STARTPTS[a{i}]")
            next_idx += 1
            v_labels.append(f"[v{i}]")
            a_labels.append(f"[a{i}]")
    else:
        # -- final: every clip normalized inside one graph, from source -----
        for i, c in enumerate(clips):
            dur = durations[i]
            if i in image_assets:
                inputs += _image_input(image_assets[i], fps, dur)
                vi = next_idx
                inputs += _silence_input(dur)
                ai = next_idx + 1
                next_idx += 2
                crop, vol = "", 0.0
            else:
                inputs += ["-ss", f"{c.in_s:.4f}", "-t", f"{dur:.4f}", "-i", str(c.source)]
                vi = ai = next_idx
                next_idx += 1
                if not has_audio[i]:
                    # No audio stream at all (e.g. a silent screen recording):
                    # referencing [vi:a] would make ffmpeg fail the whole render.
                    inputs += _silence_input(dur)
                    ai = next_idx
                    next_idx += 1
                crop, vol = _crop_filter(c), getattr(c, "audio_volume", 0.0)
            filters.append(_video_norm(f"[{vi}:v]", crop, canvas_w, canvas_h, fps, dur, f"[v{i}]"))
            filters.append(_audio_norm(f"[{ai}:a]", vol, dur, f"[a{i}]"))
            v_labels.append(f"[v{i}]")
            a_labels.append(f"[a{i}]")

    # -- joins ----------------------------------------------------------------
    cur_v, cur_a = v_labels[0], a_labels[0]
    for i in range(1, len(clips)):
        j = joins[i]
        if j.kind == "cut":
            filters.append(f"{cur_v}{cur_a}{v_labels[i]}{a_labels[i]}concat=n=2:v=1:a=1[cv{i}][ca{i}]")
            filters.append(f"[cv{i}]settb=1/{fps}[jv{i}]")
            cur_v, cur_a = f"[jv{i}]", f"[ca{i}]"
        else:
            filters.append(f"{cur_v}{v_labels[i]}xfade=transition={xfade_name(j.transition, j.direction)}:"
                           f"duration={j.duration:.4f}:offset={starts[i]:.4f}[jv{i}]")
            filters.append(f"{cur_a}{a_labels[i]}acrossfade=d={j.duration:.4f}[ja{i}]")
            cur_v, cur_a = f"[jv{i}]", f"[ja{i}]"

    for edge, t_type, req, direction in (("intro", intro_type, intro_duration, intro_direction),
                                         ("outro", outro_type, outro_duration, outro_direction)):
        d = edge_transition_duration(t_type, req, durations[0] if edge == "intro" else total_out)
        if d <= 0:
            continue
        inputs += ["-f", "lavfi", "-t", f"{d:.4f}", "-i", f"color=c=black:s={canvas_w}x{canvas_h}:r={fps}"]
        inputs += _silence_input(d)
        bv, ba = next_idx, next_idx + 1
        next_idx += 2
        filters.append(f"[{bv}:v]format=yuv420p,settb=1/{fps}[v{edge}b]")
        filters.append(f"[{ba}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{edge}b]")
        name = xfade_name(t_type, direction)
        if edge == "intro":
            filters.append(f"[v{edge}b]{cur_v}xfade=transition={name}:duration={d:.4f}:offset=0[v{edge}]")
            filters.append(f"[a{edge}b]{cur_a}acrossfade=d={d:.4f}[a{edge}]")
        else:
            filters.append(f"{cur_v}[v{edge}b]xfade=transition={name}:duration={d:.4f}:"
                           f"offset={total_out - d:.4f}[v{edge}]")
            filters.append(f"{cur_a}[a{edge}b]acrossfade=d={d:.4f}[a{edge}]")
        cur_v, cur_a = f"[v{edge}]", f"[a{edge}]"

    cur_a, next_idx = _mix_in_sound_clips(sound_clips, total_out, cur_a, next_idx, inputs, filters)

    for i, (overlay, asset) in enumerate(zip(active_overlays, text_assets)):
        cur_v = _text_overlay_filters(asset, overlay.start_s, overlay.duration_s,
                                      min(overlay.end_s, total_out), cur_v, next_idx, fps, i, inputs, filters)
        next_idx += 1

    crf, preset = ("28", "ultrafast") if proxy else ("18", "fast")
    cmd = _progress_cmd([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filters),
        "-map", cur_v, "-map", cur_a,
        "-c:v", "libx264", "-crf", crf, "-preset", preset,
        "-c:a", "aac",
        str(output_path),
    ])
    label = "proxy render" if proxy else "final render"
    _check_cancelled()
    return _run_with_progress(cmd, total_out, lambda f, m: phases.report("encode", f, "Encoding..."), label)


# ---------------------------------------------------------- editor bakes --

def _preview_size(w: int, h: int, preview_height: Optional[int]) -> tuple[int, int]:
    if preview_height and preview_height < h:
        w, h = w * preview_height / h, preview_height
    return _even(w), _even(h)


def render_image_clip_preview(clip, canvas_w: int, canvas_h: int, output_path: Path,
                              preview_height: Optional[int] = None, progress_cb: ProgressCB = None,
                              pool: Optional[Executor] = None) -> bool:
    """Bakes an image clip's Ken Burns zoom into a short silent .mp4 for the
    mpv-embedded transport. preview_height downscales the bake (final
    renders are unaffected)."""
    w, h = _preview_size(canvas_w, canvas_h, preview_height)
    asset = render_assets.image_clip_asset(clip, w, h, FPS, pool)
    cmd = _progress_cmd(["ffmpeg", "-y", *_image_input(asset, FPS, clip.duration),
                         "-t", f"{clip.duration:.4f}",
                         "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                         str(output_path)])
    return _run_with_progress(cmd, clip.duration, progress_cb, f"bake image preview {output_path.name}")


def render_text_overlay_preview(source_path: Path, overlay, canvas_w: int, canvas_h: int, output_path: Path,
                                preview_height: Optional[int] = None, progress_cb: ProgressCB = None,
                                pool: Optional[Executor] = None) -> bool:
    """Bakes a text overlay onto the composed-preview chunk it plays over, so
    scrubbing shows real footage behind the text. canvas_w/h are the project
    canvas the overlay is expressed in; the source may be a smaller proxy."""
    probe = probe_video(source_path)
    w, h = _preview_size(probe.width, probe.height, preview_height)
    scaled = text_render.scale_overlay(overlay, h / canvas_h if canvas_h else 1.0)
    asset = render_assets.text_overlay_asset(scaled, FPS, pool)
    inputs = ["-ss", str(overlay.start_s), "-t", str(overlay.duration_s), "-i", str(source_path)]
    filters = [f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[base]"]
    cur_v = _text_overlay_filters(asset, 0.0, overlay.duration_s, overlay.duration_s, "[base]", 1, FPS, 0,
                                  inputs, filters)
    cmd = _progress_cmd(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
                         "-map", cur_v, "-map", "0:a?",
                         "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", str(output_path)])
    return _run_with_progress(cmd, overlay.duration_s, progress_cb, f"bake text preview {output_path.name}")


def render_sound_clip_preview(source_path: Path, sound, output_path: Path,
                              preview_height: Optional[int] = None, progress_cb: ProgressCB = None) -> bool:
    """Bakes a sound clip onto the composed-preview chunk it plays over, using
    the same filter chain render_timeline uses."""
    probe = probe_video(source_path)
    played = sound.played_duration_s(probe.duration_s)
    if played <= 0:
        return False
    w, h = _preview_size(probe.width, probe.height, preview_height)

    # The base video is trimmed to start exactly at sound.start_s, so the
    # sound is repositioned relative to that new zero point.
    local_sound = dataclasses.replace(sound, start_s=0.0)
    inputs = ["-ss", str(sound.start_s), "-t", str(played), "-i", str(source_path)]
    filters = [
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[vout]",
    ]
    if probe.has_audio:
        filters.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[abase]")
        next_idx = 1
    else:
        inputs += _silence_input(played)
        filters.append("[1:a]aformat=sample_rates=48000:channel_layouts=stereo[abase]")
        next_idx = 2
    cur_a, _ = _mix_in_sound_clips([local_sound], played, "[abase]", next_idx, inputs, filters)
    cmd = _progress_cmd(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
                         "-map", "[vout]", "-map", cur_a,
                         "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", str(output_path)])
    return _run_with_progress(cmd, played, progress_cb, f"bake sound preview {output_path.name}")
