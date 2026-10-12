"""All ffmpeg/ffprobe interaction lives here. Every long-running function accepts
an optional progress_cb(fraction: float, message: str) called from a worker thread.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import active_processes, image_render, text_render
from .logging_config import get_logger

log = get_logger()

ProgressCB = Optional[Callable[[float, str], None]]

_WIPE_BY_DIRECTION = {"left": "wipeleft", "right": "wiperight", "up": "wipeup", "down": "wipedown"}
_SLIDE_BY_DIRECTION = {"left": "slideleft", "right": "slideright", "up": "slideup", "down": "slidedown"}


def xfade_name(transition_type: str, direction: str) -> str:
    if transition_type == "crossfade":
        return "fade"
    if transition_type == "fade_black":
        return "fadeblack"
    if transition_type == "wipe":
        return _WIPE_BY_DIRECTION.get(direction, "wipeleft")
    if transition_type == "slide":
        return _SLIDE_BY_DIRECTION.get(direction, "slideup")
    return "fade"  # cut -- duration forced to ~1 frame at render time -> behaves as a hard cut


@dataclass
class ProbeResult:
    duration_s: float
    width: int
    height: int
    codec: str
    fps: float
    has_audio: bool


# Keyed by (str(path), mtime_ns, size) -> ProbeResult, oldest access first.
# The same file gets probed repeatedly (every add, every render setup,
# sound-preview bakes that probe twice back-to-back, ...); each probe is a
# full ffprobe subprocess launch. The mtime/size in the key means an edited
# file is re-probed automatically instead of returning a stale result -- but
# that also means every edit strands its old entry forever, so this is
# capped LRU-style instead of a plain dict.
_PROBE_CACHE_MAX = 500
_PROBE_CACHE: "OrderedDict[tuple[str, int, int], ProbeResult]" = OrderedDict()


def _probe_cache_key(path: Path) -> Optional[tuple[str, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def probe_video(path: Path, timeout: float = 20) -> ProbeResult:
    cache_key = _probe_cache_key(path)
    if cache_key is not None:
        cached = _PROBE_CACHE.get(cache_key)
        if cached is not None:
            _PROBE_CACHE.move_to_end(cache_key)
            return cached
    result = _probe_video_uncached(path, timeout)
    if cache_key is not None:
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
            capture_output=True, text=True, timeout=timeout,
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
    proc_holder: Optional[dict] = None,
) -> bool:
    """Runs ffmpeg with -progress piped to stdout, reports fractional progress.
    Returns True on success (exit code 0), False on failure or cancellation.

    stdout (progress) and stderr (ffmpeg's normal logging) are drained on
    separate threads concurrently -- ffmpeg's stderr output is verbose enough
    (especially with a filter_complex graph) to fill the OS pipe buffer and
    block the process entirely if left unread until exit, which stalls
    progress reporting too.
    """
    log.info("ffmpeg start: %s", label)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except OSError:
        log.exception("ffmpeg could not be started (missing binary/PATH?) for: %s", label)
        return False
    if proc_holder is not None:
        proc_holder["proc"] = proc
    active_processes.register(proc)

    stderr_lines: list[str] = []

    def drain_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if len(stderr_lines) > 500:
                stderr_lines.pop(0)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms=") and total_s > 0:
            try:
                us = int(line.split("=", 1)[1])
                frac = max(0.0, min(1.0, (us / 1_000_000) / total_s))
                if progress_cb:
                    progress_cb(frac, label)
            except ValueError:
                pass

    proc.wait()
    active_processes.unregister(proc)
    stderr_thread.join(timeout=2)

    if proc_holder is not None and proc_holder.get("cancelled"):
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
    return [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]


def run_crop_resize(
    paths: Sequence[Path],
    output_dir: Path,
    crop_fractions: tuple[float, float, float, float],  # top, bottom, left, right, 0..1 of each file's own size
    target_size: tuple[Optional[int], Optional[int]],
    progress_cb: ProgressCB = None,
) -> list[Path]:
    """crop_fractions is expressed relative to each input's own resolution
    (see CropCanvas.get_crop_fractions) rather than as absolute pixels, so a
    batch of files with different resolutions from the previewed one still
    gets a proportionally-correct crop instead of the previewed file's exact
    pixel offsets misapplied to a differently-sized file."""
    top_f, bottom_f, left_f, right_f = crop_fractions
    target_w, target_h = target_size
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []

    for i, input_path in enumerate(paths):
        probe = probe_video(input_path)
        top = round(top_f * probe.height)
        bottom = round(bottom_f * probe.height)
        left = round(left_f * probe.width)
        right = round(right_f * probe.width)
        crop_w = probe.width - left - right
        crop_h = probe.height - top - bottom
        out_w = (round(target_w) if target_w else crop_w) // 2 * 2
        out_h = (round(target_h) if target_h else crop_h) // 2 * 2
        vf = f"crop={crop_w}:{crop_h}:{left}:{top},scale={out_w}:{out_h}"
        output_path = output_dir / input_path.name

        def file_progress(frac: float, _label: str, i=i):
            overall = (i + frac) / len(paths)
            if progress_cb:
                progress_cb(overall, f"Cropping {input_path.name} ({i+1}/{len(paths)})")

        cmd = _progress_cmd([
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", vf,
            "-c:v", probe.codec if probe.codec in ("h264", "hevc") else "libx264",
            "-c:a", "copy",
            str(output_path),
        ])
        ok = _run_with_progress(cmd, probe.duration_s, file_progress, f"crop {input_path.name}")
        if ok:
            results.append(output_path)
    return results


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
            [
                "ffmpeg", "-y", "-ss", str(pos_s), "-i", str(path),
                "-frames:v", "1", "-q:v", "2", str(output_path),
            ],
            capture_output=True, timeout=30,
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


def extract_thumbnail(path: Path, pos_s: float, out_w: int = 160, timeout: float = 20) -> bytes:
    """Returns PNG bytes of a single frame, scaled to out_w wide. Used for timeline thumbnails."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(pos_s), "-i", str(path),
                "-frames:v", "1", "-vf", f"scale={out_w}:-2",
                "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            ],
            capture_output=True, timeout=timeout,
        )
    except OSError:
        log.exception("ffmpeg could not be started (missing binary/PATH?) for thumbnail %s", path.name)
        return b""
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out extracting thumbnail for %s", path.name)
        return b""
    if result.returncode != 0:
        log.error("thumbnail extraction failed for %s: %s", path.name, result.stderr[-500:])
        return b""
    return result.stdout


def extract_frame(path: Path, pos_s: float) -> bytes:
    """Full-resolution single-frame grab (PNG bytes), no scaling -- distinct
    from extract_thumbnail() which is deliberately downscaled for timeline blocks."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(pos_s), "-i", str(path),
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            ],
            capture_output=True, timeout=20,
        )
    except OSError:
        log.exception("ffmpeg could not be started (missing binary/PATH?) for frame grab %s", path.name)
        return b""
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out grabbing frame from %s", path.name)
        return b""
    if result.returncode != 0:
        log.error("frame extraction failed for %s: %s", path.name, result.stderr[-500:])
        return b""
    return result.stdout


def render_image_clip_preview(
    clip, canvas_w: int, canvas_h: int, output_path: Path,
    preview_height: Optional[int] = None,
    progress_cb: ProgressCB = None,
) -> bool:
    """Bakes an image-as-clip's Ken Burns zoom into a short silent .mp4, so it
    can be played back through the same mpv-embedded transport (Play button +
    real seeking) used everywhere else in the app, instead of PIL recomputing
    and redrawing a frame on every scrub tick in Tk -- fine for a couple of
    frames but visibly laggy for anything past a trivial zoom. preview_height,
    if given and smaller than canvas_h, downscales the bake (same idea as the
    timeline's proxy-quality render) so it finishes faster; the final applied
    clip is unaffected since it re-renders at full canvas size regardless.
    Dimensions are always forced even -- h264 rejects odd width/height, and
    project canvas sizes can be odd (e.g. defaulted from a phone screenshot)."""
    fps = 30
    if preview_height and preview_height < canvas_h:
        canvas_w = round(canvas_w * preview_height / canvas_h)
        canvas_h = preview_height
    canvas_w, canvas_h = max(2, canvas_w // 2 * 2), max(2, canvas_h // 2 * 2)
    with tempfile.TemporaryDirectory(prefix="video_editor_img_preview_") as tmp:
        seq_dir, _n = image_render.render_image_clip_sequence(clip, canvas_w, canvas_h, fps, Path(tmp))
        cmd = _progress_cmd([
            "ffmpeg", "-y",
            "-framerate", str(fps), "-i", str(seq_dir / "seq_%05d.png"),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
            str(output_path),
        ])
        return _run_with_progress(cmd, clip.duration, progress_cb, f"bake image preview {output_path.name}")


def render_text_overlay_preview(
    source_path: Path, overlay: "TextOverlayLike", canvas_w: int, canvas_h: int, output_path: Path,
    preview_height: Optional[int] = None,
    progress_cb: ProgressCB = None,
) -> bool:
    """Bakes a text overlay onto the actual video chunk it plays over -- trims
    source_path (the composed timeline preview, proxy or final) to
    [overlay.start_s, overlay.end_s) and composites the overlay's animated PNG
    sequence on top, so scrubbing/playback shows the real footage moving behind
    the text instead of the same static grabbed frame repeated. preview_height,
    if given and smaller than the source's height, downscales the bake for a
    faster render (mirrors render_image_clip_preview's proxy-quality option).
    canvas_w/canvas_h are the project's canvas size the overlay's x/y/font_size
    are expressed in -- rescaled to the source's actual resolution, which can
    differ if source_path is a proxy render."""
    probe = probe_video(source_path)
    render_w, render_h = probe.width, probe.height
    if preview_height and preview_height < render_h:
        render_w = round(render_w * preview_height / render_h)
        render_h = preview_height
    render_w, render_h = max(2, render_w // 2 * 2), max(2, render_h // 2 * 2)

    canvas_scale = render_h / canvas_h if canvas_h else 1.0
    scaled_overlay = text_render.scale_overlay(overlay, canvas_scale)

    fps = 30
    with tempfile.TemporaryDirectory(prefix="video_editor_text_preview_") as tmp:
        seq_dir, _n = text_render.render_overlay_sequence(scaled_overlay, render_w, render_h, fps, Path(tmp))
        cmd = _progress_cmd([
            "ffmpeg", "-y",
            "-ss", str(overlay.start_s), "-t", str(overlay.duration_s), "-i", str(source_path),
            "-framerate", str(fps), "-i", str(seq_dir / "seq_%05d.png"),
            "-filter_complex",
            f"[0:v]scale={render_w}:{render_h}:force_original_aspect_ratio=decrease,"
            f"pad={render_w}:{render_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[base];"
            f"[base][1:v]overlay=x=0:y=0[vout]",
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(output_path),
        ])
        return _run_with_progress(cmd, overlay.duration_s, progress_cb, f"bake text preview {output_path.name}")


def _sound_clip_filter(sound: "SoundClipLike", input_index: int, played: float, label: str) -> str:
    """Builds one sound clip's filter chain: resample -> volume -> loop (in
    ORIGINAL-tempo samples, so `size` doesn't need a speed correction) ->
    speed/pitch (asetrate, linked -- a tape/turntable-style speed change,
    applied to the already-looped signal) -> fades -> trim to `played` ->
    adelay to its absolute position (see render_timeline's docstring for why
    adelay rather than -itsoffset). The input itself is just `-ss
    clip_start_s -i source` -- no -itsoffset needed since adelay handles
    positioning entirely within the filter graph."""
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


def _mix_in_sound_clips(
    sound_clips: Sequence["SoundClipLike"], total_out: float, cur_a: str, next_idx: int,
    inputs: list[str], filters: list[str],
) -> tuple[str, int]:
    """Adds one ffmpeg input + filter chain per active sound clip and mixes
    them all into cur_a via amix (normalize=0 so explicit user-set volumes
    aren't silently rescaled by ffmpeg's default 1/inputs attenuation).
    Returns the new cur_a label (unchanged if there's nothing to mix) and the
    updated next_idx."""
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

    joined = "".join(mix_labels)
    filters.append(f"{joined}amix=inputs={len(mix_labels)}:duration=first:normalize=0[amix_out]")
    return "[amix_out]", next_idx


def render_sound_clip_preview(
    source_path: Path, sound: "SoundClipLike", output_path: Path,
    preview_height: Optional[int] = None,
    progress_cb: ProgressCB = None,
) -> bool:
    """Bakes a sound clip onto the actual video chunk it plays over -- trims
    source_path (the composed timeline preview) to [start_s, end_s) and mixes
    the processed sound in via the same filter chain render_timeline uses, so
    the bake previews the real mix instead of the sound in isolation."""
    total_out = probe_video(source_path).duration_s
    played = sound.played_duration_s(total_out)
    if played <= 0:
        return False

    probe = probe_video(source_path)
    render_w, render_h = probe.width, probe.height
    if preview_height and preview_height < render_h:
        render_w = round(render_w * preview_height / render_h)
        render_h = preview_height
    render_w, render_h = max(2, render_w // 2 * 2), max(2, render_h // 2 * 2)

    # The base video is trimmed to start exactly at sound.start_s, so the
    # sound needs to be repositioned relative to that new zero point too.
    local_sound = dataclasses.replace(sound, start_s=0.0)

    inputs = ["-ss", str(sound.start_s), "-t", str(played), "-i", str(source_path)]
    filters = [
        f"[0:v]scale={render_w}:{render_h}:force_original_aspect_ratio=decrease,"
        f"pad={render_w}:{render_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[vout]",
        "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[abase]",
    ]
    cur_a, _next_idx = _mix_in_sound_clips([local_sound], played, "[abase]", 1, inputs, filters)

    cmd = _progress_cmd([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", cur_a,
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(output_path),
    ])
    return _run_with_progress(cmd, played, progress_cb, f"bake sound preview {output_path.name}")


def render_timeline(
    clips: Sequence["TimelineClipLike"],
    canvas_w: int, canvas_h: int,
    output_path: Path,
    proxy: bool = False,
    proxy_height: int = 270,
    progress_cb: ProgressCB = None,
    proc_holder: Optional[dict] = None,
    text_overlays: Sequence["TextOverlayLike"] = (),
    sound_clips: Sequence["SoundClipLike"] = (),
    intro_type: str = "cut",
    intro_duration: float = 0.25,
    intro_direction: str = "left",
    outro_type: str = "cut",
    outro_duration: float = 0.25,
    outro_direction: str = "left",
) -> bool:
    """clips[i].transition_type / transition_duration describe the transition used
    to join clip i-1 into clip i (ignored for clips[0], which instead uses
    intro_type/intro_duration/intro_direction -- a synthetic black+silent
    "clip -1" prepended when intro_type != "cut", faded into clip 0 with the
    exact same xfade/acrossfade machinery used between real clips).
    outro_type/outro_duration/outro_direction work the same way at the other
    end -- a synthetic black+silent "clip N" appended after the last clip,
    with the whole timeline faded into it, when outro_type != "cut".
    clips[i].is_image clips are pre-rendered to a PNG sequence (baking in any
    Ken Burns zoom) and paired with a silent audio input, since a still image
    has no native duration or audio stream of its own.
    proc_holder, if given, receives the running Popen under key "proc" as soon as
    it starts, letting the caller cancel by calling .terminate() on it and setting
    proc_holder["cancelled"] = True.
    text_overlays coordinates (x/y/font_size) are expected in the *original*
    canvas_w x canvas_h space passed in -- they're rescaled internally to match
    proxy resolution when proxy=True.
    Each clip's own embedded audio is mixed in at clips[i].audio_volume (0.0 by
    default -- muted; see TimelineClip.audio_volume). sound_clips are separate
    library sounds mixed in on top via amix, each positioned with adelay rather
    than -itsoffset -- amix mixes streams sample-for-sample assuming they all
    start at the same PTS=0, unlike overlay/xfade's frame-timestamp-aware
    compositing, so an itsoffset-shifted PTS wouldn't actually land the sound
    at the right spot; adelay instead pads real leading silence onto a 0-based
    stream, which amix handles correctly. Every sound_clips entry is clamped
    (via played_duration_s) to never outlast total_out, so a loop (finite or
    repeat_infinite) can't leave an audio-only tail after the video ends."""
    if not clips:
        return False

    orig_canvas_h = canvas_h
    if proxy:
        scale_h = proxy_height
        canvas_w, canvas_h = (round(canvas_w * scale_h / canvas_h) // 2 * 2, scale_h)
    text_scale_factor = canvas_h / orig_canvas_h if orig_canvas_h else 1.0

    inputs: list[str] = []
    fps = 30
    tmp_dirs: list[tempfile.TemporaryDirectory] = []

    try:
        # Each clip contributes one ffmpeg input (video clips: video+audio share
        # one -i) or two (image clips: a video-only PNG sequence paired with a
        # separate silent-audio input) -- track indices explicitly rather than
        # assuming [i:v]/[i:a] share a single input index.
        video_input_idx: list[int] = []
        audio_input_idx: list[int] = []
        next_idx = 0

        for c in clips:
            if getattr(c, "is_image", False):
                tmp = tempfile.TemporaryDirectory(prefix="video_editor_img_")
                tmp_dirs.append(tmp)
                seq_dir, _n = image_render.render_image_clip_sequence(c, canvas_w, canvas_h, fps, Path(tmp.name))
                inputs += ["-framerate", str(fps), "-i", str(seq_dir / "seq_%05d.png")]
                video_input_idx.append(next_idx)
                next_idx += 1
                inputs += ["-f", "lavfi", "-t", str(c.duration), "-i", "anullsrc=r=48000:cl=stereo"]
                audio_input_idx.append(next_idx)
                next_idx += 1
            else:
                inputs += ["-ss", str(c.in_s), "-t", str(c.duration), "-i", str(c.source)]
                video_input_idx.append(next_idx)
                audio_input_idx.append(next_idx)
                next_idx += 1

        filters = []
        v_labels = []
        a_labels = []
        for i, c in enumerate(clips):
            vi, ai = video_input_idx[i], audio_input_idx[i]
            crop_top = getattr(c, "crop_top", 0)
            crop_bottom = getattr(c, "crop_bottom", 0)
            crop_left = getattr(c, "crop_left", 0)
            crop_right = getattr(c, "crop_right", 0)
            crop_filter = ""
            if crop_top or crop_bottom or crop_left or crop_right:
                # iw/ih (ffmpeg's input-dimension variables) rather than a
                # probed width/height -- keeps this filter self-contained and
                # correct even if the clip's crop_* were captured against a
                # differently-scaled reference than the actual source pixels.
                crop_filter = (f"crop=iw-{crop_left + crop_right}:ih-{crop_top + crop_bottom}:"
                                f"{crop_left}:{crop_top},")
            filters.append(
                f"[{vi}:v]{crop_filter}scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]"
            )
            vol = getattr(c, "audio_volume", 0.0)
            filters.append(
                f"[{ai}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={vol:.4f},asetpts=PTS-STARTPTS[a{i}]"
            )
            v_labels.append(f"[v{i}]")
            a_labels.append(f"[a{i}]")

        durations = [c.duration for c in clips]

        def cut_epsilon_duration(t_type, t_dur):
            # xfade/acrossfade with duration=0 is a degenerate case ffmpeg
            # doesn't handle cleanly (observed to truncate the video stream to
            # just the last clip's length while audio stayed correct) -- use
            # ~1 frame instead for "cut", visually indistinguishable.
            return round(1 / fps, 3) if t_type == "cut" else max(0.05, t_dur)

        if intro_type != "cut":
            inputs += ["-f", "lavfi", "-t", str(intro_duration),
                       "-i", f"color=c=black:s={canvas_w}x{canvas_h}:r={fps}"]
            black_v = next_idx
            next_idx += 1
            inputs += ["-f", "lavfi", "-t", str(intro_duration), "-i", "anullsrc=r=48000:cl=stereo"]
            black_a = next_idx
            next_idx += 1
            filters.append(f"[{black_v}:v]format=yuv420p[vblack]")
            filters.append(f"[{black_a}:a]aformat=sample_rates=48000:channel_layouts=stereo[ablack]")
            cur_v, cur_a = "[vblack]", "[ablack]"
            cur_len = intro_duration
            total_out = intro_duration
            start_index = 0
        else:
            cur_v, cur_a = v_labels[0], a_labels[0]
            cur_len = durations[0]
            total_out = durations[0]
            start_index = 1

        for i in range(start_index, len(clips)):
            c = clips[i]
            if i == 0:
                t_type, t_dur, t_dir = intro_type, intro_duration, intro_direction
            else:
                t_type, t_dur, t_dir = c.transition_type, c.transition_duration, c.transition_direction
            d = cut_epsilon_duration(t_type, t_dur)
            xfade_filter_name = xfade_name(t_type, t_dir)
            offset = max(0.0, cur_len - d)
            out_v = f"[vx{i}]"
            out_a = f"[ax{i}]"
            filters.append(
                f"{cur_v}{v_labels[i]}xfade=transition={xfade_filter_name}:duration={d}:offset={offset}{out_v}"
            )
            filters.append(f"{cur_a}{a_labels[i]}acrossfade=d={d}{out_a}")
            cur_v, cur_a = out_v, out_a
            cur_len = offset + durations[i]
            total_out = cur_len

        if outro_type != "cut":
            inputs += ["-f", "lavfi", "-t", str(outro_duration),
                       "-i", f"color=c=black:s={canvas_w}x{canvas_h}:r={fps}"]
            black_v = next_idx
            next_idx += 1
            inputs += ["-f", "lavfi", "-t", str(outro_duration), "-i", "anullsrc=r=48000:cl=stereo"]
            black_a = next_idx
            next_idx += 1
            filters.append(f"[{black_v}:v]format=yuv420p[voutroblack]")
            filters.append(f"[{black_a}:a]aformat=sample_rates=48000:channel_layouts=stereo[aoutroblack]")
            d = cut_epsilon_duration(outro_type, outro_duration)
            xfade_filter_name = xfade_name(outro_type, outro_direction)
            offset = max(0.0, cur_len - d)
            filters.append(
                f"{cur_v}[voutroblack]xfade=transition={xfade_filter_name}:duration={d}:offset={offset}[voutro]"
            )
            filters.append(f"{cur_a}[aoutroblack]acrossfade=d={d}[aoutro]")
            cur_v, cur_a = "[voutro]", "[aoutro]"
            cur_len = offset + outro_duration
            total_out = cur_len

        cur_a, next_idx = _mix_in_sound_clips(sound_clips, total_out, cur_a, next_idx, inputs, filters)

        active_overlays = [t for t in text_overlays if t.start_s < total_out and t.duration_s > 0]
        for i, overlay in enumerate(active_overlays):
            scaled = text_render.scale_overlay(overlay, text_scale_factor)
            tmp = tempfile.TemporaryDirectory(prefix="video_editor_text_")
            tmp_dirs.append(tmp)
            seq_dir, n_frames = text_render.render_overlay_sequence(scaled, canvas_w, canvas_h, fps, Path(tmp.name))

            input_index = next_idx
            next_idx += 1
            inputs += ["-itsoffset", str(overlay.start_s), "-framerate", str(fps),
                       "-i", str(seq_dir / "seq_%05d.png")]
            out_v = f"[tv{i}]"
            end_s = min(overlay.end_s, total_out)
            filters.append(
                f"{cur_v}[{input_index}:v]overlay=x=0:y=0:"
                f"enable='between(t,{overlay.start_s},{end_s})'{out_v}"
            )
            cur_v = out_v

        filter_complex = ";".join(filters)

        crf = "28" if proxy else "18"
        preset = "ultrafast" if proxy else "fast"

        cmd = _progress_cmd([
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", cur_v, "-map", cur_a,
            "-c:v", "libx264", "-crf", crf, "-preset", preset,
            "-c:a", "aac",
            str(output_path),
        ])
        label = "proxy render" if proxy else "final render"
        return _run_with_progress(cmd, total_out, progress_cb, label, proc_holder=proc_holder)
    finally:
        for tmp in tmp_dirs:
            tmp.cleanup()
