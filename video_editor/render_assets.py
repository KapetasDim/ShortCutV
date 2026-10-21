"""Baked, content-addressed render assets (image clips, text overlays) and the
on-disk render cache they share with proxy segments (see ffmpeg_ops).

Every render used to re-bake every image clip and text overlay into a
full-canvas PNG per frame -- even a static image with no zoom, or a text
overlay whose only animation is an opacity fade. Now:

  * anything whose pixels don't change over time is baked ONCE to a single
    PNG and looped by ffmpeg (opacity fades applied by ffmpeg's alpha fade);
  * animated text is baked only at its bounding-box size, not the full canvas;
  * every asset is cached under a hash of everything that affects its pixels,
    so re-rendering an unchanged project re-bakes nothing;
  * frames of an animated asset are rendered in parallel worker processes.

Only ever called from the render worker process (video_editor.worker), never
from the GUI process.
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import Executor
from pathlib import Path
from typing import Optional

from PIL import Image

from . import image_render, text_render
from .curves import eval_curve, fade_opacity
from .logging_config import get_logger

log = get_logger()

CACHE_ROOT = Path(tempfile.gettempdir()) / "shortcutv_cache"
ASSETS_DIR = CACHE_ROOT / "assets"
SEGMENTS_DIR = CACHE_ROOT / "segments"
CACHE_MAX_BYTES = 5 * 1024 ** 3
# Bump when baking logic changes in a way that should invalidate old entries.
_CACHE_VERSION = 1

# Parallel frame rendering only pays for itself past the worker-process
# startup cost (~0.3-0.5s each on Windows).
_PARALLEL_MIN_FRAMES = 24
_PNG_COMPRESS = 1  # temp frames: speed over size -- they're read once by ffmpeg


def cache_key(*parts) -> str:
    raw = json.dumps([_CACHE_VERSION, *parts], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def file_identity(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def _touch(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def cached_entry(directory: Path, key: str) -> Optional[Path]:
    """Returns the finished cache entry for `key` (dir or file), or None."""
    for candidate in (directory / key, *directory.glob(f"{key}.*")):
        if candidate.exists() and ".partial" not in candidate.name:
            _touch(candidate)
            return candidate
    return None


def publish(tmp: Path, final: Path) -> Path:
    """Moves a fully-built temp entry into place. Two worker processes can
    build the same key concurrently; whoever loses the rename just discards
    its copy, so a half-written entry is never visible under the final name."""
    try:
        os.replace(tmp, final) if tmp.is_file() else tmp.rename(final)
    except OSError:
        if tmp.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            tmp.unlink(missing_ok=True)
    return final


def prune_cache(max_bytes: int = CACHE_MAX_BYTES) -> None:
    """Deletes least-recently-used cache entries until the cache fits."""
    entries = []
    total = 0
    for directory in (ASSETS_DIR, SEGMENTS_DIR):
        if not directory.exists():
            continue
        for entry in directory.iterdir():
            size = (sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                    if entry.is_dir() else entry.stat().st_size)
            # Stale .partial leftovers from a killed worker are always junk.
            if entry.name.endswith(".partial") and time.time() - entry.stat().st_mtime > 3600:
                _remove(entry)
                continue
            entries.append((entry.stat().st_mtime, size, entry))
            total += size
    if total <= max_bytes:
        return
    entries.sort()
    removed = 0
    for _mtime, size, entry in entries:
        if total <= max_bytes:
            break
        _remove(entry)
        total -= size
        removed += 1
    log.info("Render cache: pruned %d entr(ies), now %.1f GB", removed, total / 1024 ** 3)


def _remove(entry: Path):
    if entry.is_dir():
        shutil.rmtree(entry, ignore_errors=True)
    else:
        entry.unlink(missing_ok=True)


def _new_partial(directory: Path, key: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{key}.{uuid.uuid4().hex[:8]}.partial"
    tmp.mkdir()
    return tmp


def _save_png(img: Image.Image, path: Path) -> None:
    img.save(path, compress_level=_PNG_COMPRESS)


def _run_frames(pool: Optional[Executor], fn, tasks: list) -> None:
    if pool is not None and len(tasks) >= _PARALLEL_MIN_FRAMES:
        for _ in pool.map(fn, tasks, chunksize=8):
            pass
    else:
        for t in tasks:
            fn(t)


# ------------------------------------------------------------- image clips --

@functools.lru_cache(maxsize=4)
def _load_fitted(path: str) -> Image.Image:
    with Image.open(path) as img:
        return img.convert("RGB")


def _zoom_frame_task(task) -> None:
    fitted_path, canvas_w, canvas_h, scale, out_path = task
    frame = image_render.compute_zoom_frame(_load_fitted(fitted_path), canvas_w, canvas_h, scale)
    _save_png(frame, Path(out_path))


def image_clip_asset(clip, canvas_w: int, canvas_h: int, fps: int, pool: Optional[Executor]) -> dict:
    """{"kind": "still", "path"} or {"kind": "seq", "pattern", "n_frames"}."""
    src = Path(clip.source)
    n_frames = max(1, round(clip.duration * fps))
    if clip.image_zoom_enabled:
        scales = []
        for i in range(n_frames):
            t_norm = i / (n_frames - 1) if n_frames > 1 else 0.0
            progress = eval_curve(clip.image_zoom_curve, clip.image_zoom_curve_params, t_norm)
            scales.append(max(1.0, 1.0 + (clip.image_zoom_factor - 1.0) * progress))
    else:
        scales = [1.0]
    animated = max(scales) - min(scales) > 1e-6

    key = cache_key("image", file_identity(src), canvas_w, canvas_h, fps,
                    [round(s, 6) for s in scales] if animated else round(scales[0], 6))
    entry = cached_entry(ASSETS_DIR, key)
    if entry is None:
        tmp = _new_partial(ASSETS_DIR, key)
        try:
            with Image.open(src) as source:
                fitted = image_render.fit_canvas(source, canvas_w, canvas_h)
            if animated:
                fitted_path = tmp / "fitted.png"
                _save_png(fitted, fitted_path)
                tasks = [(str(fitted_path), canvas_w, canvas_h, s, str(tmp / f"seq_{i:05d}.png"))
                         for i, s in enumerate(scales)]
                _run_frames(pool, _zoom_frame_task, tasks)
                fitted_path.unlink()
                meta = {"kind": "seq", "n_frames": n_frames}
            else:
                _save_png(image_render.compute_zoom_frame(fitted, canvas_w, canvas_h, scales[0]), tmp / "still.png")
                meta = {"kind": "still"}
            (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        entry = publish(tmp, ASSETS_DIR / key)
        log.info("Image clip: baked %s (%s, %d frame(s))", src.name, meta["kind"], len(scales) if animated else 1)

    meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    if meta["kind"] == "still":
        return {"kind": "still", "path": str(entry / "still.png")}
    return {"kind": "seq", "pattern": str(entry / "seq_%05d.png"), "n_frames": meta["n_frames"]}


# ------------------------------------------------------------ text overlays --

def _text_frame_task(task) -> None:
    overlay_dict, scale, opacity, frame_w, frame_h, origin_x, origin_y, out_path = task
    from .project import overlay_from_dict
    overlay = overlay_from_dict(overlay_dict)
    content = text_render.apply_opacity(text_render.render_text_frame(overlay, scale), opacity)
    frame = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    px = round(overlay.x - content.width / 2) - origin_x
    py = round(overlay.y - content.height / 2) - origin_y
    frame.alpha_composite(content, (px, py))
    _save_png(frame, Path(out_path))


def text_overlay_asset(overlay, fps: int, pool: Optional[Executor]) -> dict:
    """`overlay` must already be scaled to the render canvas. Returns
    {"kind": "still", "path", "x", "y", "fade_in", "fade_out"} -- a single
    content-sized PNG positioned at (x, y), with linear alpha fades left to
    ffmpeg -- or {"kind": "seq", "pattern", "n_frames", "x", "y"} with the
    scale curve and opacity baked per frame at bounding-box size."""
    n_frames = max(1, round(overlay.duration_s * fps))
    t_norms = [i / (n_frames - 1) if n_frames > 1 else 0.0 for i in range(n_frames)]
    scales = [eval_curve(overlay.curve_preset, overlay.curve_params, t) for t in t_norms]
    static_size = max(scales) - min(scales) < 1e-6
    # ffmpeg chains the two alpha fades multiplicatively; fade_opacity takes
    # the min of both ramps. They only agree when the ramps don't overlap.
    fades_separable = overlay.fade_in_s + overlay.fade_out_s <= overlay.duration_s + 1e-6
    as_still = static_size and fades_separable

    od = dataclasses.asdict(overlay)
    pixel_fields = {k: v for k, v in od.items() if k not in ("start_s",)}
    fonts_id = str(text_render.font_identity(overlay))
    key = cache_key("text", pixel_fields, fps, fonts_id, "still" if as_still else "seq")
    entry = cached_entry(ASSETS_DIR, key)
    if entry is None:
        tmp = _new_partial(ASSETS_DIR, key)
        try:
            if as_still:
                content = text_render.render_text_frame(overlay, scales[0])
                _save_png(content, tmp / "still.png")
                meta = {"kind": "still",
                        "x": round(overlay.x - content.width / 2), "y": round(overlay.y - content.height / 2)}
            else:
                # Largest-scale frame bounds every frame (+ margin for
                # rounding), so all frames share one size and one position.
                biggest = text_render.render_text_frame(overlay, max(0.01, max(scales)))
                frame_w, frame_h = biggest.width + 8, biggest.height + 8
                origin_x = round(overlay.x) - frame_w // 2
                origin_y = round(overlay.y) - frame_h // 2
                tasks = []
                for i, (t, s) in enumerate(zip(t_norms, scales)):
                    opacity = fade_opacity(t * overlay.duration_s, overlay.duration_s,
                                           overlay.fade_in_s, overlay.fade_out_s)
                    tasks.append((od, s, opacity, frame_w, frame_h, origin_x, origin_y,
                                  str(tmp / f"seq_{i:05d}.png")))
                _run_frames(pool, _text_frame_task, tasks)
                meta = {"kind": "seq", "n_frames": n_frames, "x": origin_x, "y": origin_y}
            (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        entry = publish(tmp, ASSETS_DIR / key)
        log.info("Text overlay: baked '%s' (%s, %d frame(s))", (overlay.text or "")[:30],
                 meta["kind"], 1 if as_still else n_frames)

    meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    if meta["kind"] == "still":
        return {"kind": "still", "path": str(entry / "still.png"), "x": meta["x"], "y": meta["y"],
                "fade_in": overlay.fade_in_s, "fade_out": overlay.fade_out_s}
    return {"kind": "seq", "pattern": str(entry / "seq_%05d.png"), "n_frames": meta["n_frames"],
            "x": meta["x"], "y": meta["y"]}
