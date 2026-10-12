"""Renders a still image as a timeline clip: contain-fit it into the project
canvas, then (optionally) apply a Ken-Burns zoom baked frame-by-frame via PIL
-- same "bake the animation into pixels" approach as text_render.py, so the
same curve presets (including ones like bounce/elastic that don't translate
to a simple ffmpeg filter expression) work identically for image zoom.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from .curves import eval_curve
from .logging_config import get_logger

if TYPE_CHECKING:
    from .project import TimelineClip

log = get_logger()


def fit_canvas(image: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """Scales `image` to fit within canvas_w x canvas_h preserving aspect
    ratio, centered on a canvas-sized background (letterboxed/pillarboxed)."""
    src_w, src_h = image.size
    ratio = min(canvas_w / src_w, canvas_h / src_h)
    fit_w, fit_h = max(1, round(src_w * ratio)), max(1, round(src_h * ratio))
    resized = image.convert("RGB").resize((fit_w, fit_h), Image.LANCZOS)

    canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    canvas.paste(resized, ((canvas_w - fit_w) // 2, (canvas_h - fit_h) // 2))
    return canvas


def compute_zoom_frame(fitted: Image.Image, canvas_w: int, canvas_h: int, scale: float) -> Image.Image:
    """Given an already canvas-sized (contain-fit) image, zooms into its
    center by `scale` (>=1.0 zooms in) and crops back to canvas size."""
    scale = max(1.0, scale)
    if scale <= 1.001:
        return fitted
    zoomed_w, zoomed_h = round(canvas_w * scale), round(canvas_h * scale)
    resized = fitted.resize((zoomed_w, zoomed_h), Image.LANCZOS)
    left = (zoomed_w - canvas_w) // 2
    top = (zoomed_h - canvas_h) // 2
    return resized.crop((left, top, left + canvas_w, top + canvas_h))


def render_image_clip_sequence(clip: "TimelineClip", canvas_w: int, canvas_h: int,
                                fps: int, out_dir: Path) -> tuple[Path, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = Image.open(clip.path)
    except (OSError, ValueError):
        log.exception("could not open image clip source '%s' (deleted/moved/corrupt?)", clip.path)
        raise
    fitted = fit_canvas(source, canvas_w, canvas_h)

    n_frames = max(1, round(clip.duration * fps))
    for i in range(n_frames):
        if clip.image_zoom_enabled:
            t_norm = i / (n_frames - 1) if n_frames > 1 else 0.0
            progress = eval_curve(clip.image_zoom_curve, clip.image_zoom_curve_params, t_norm)
            scale = 1.0 + (clip.image_zoom_factor - 1.0) * progress
            frame = compute_zoom_frame(fitted, canvas_w, canvas_h, scale)
        else:
            frame = fitted
        frame.save(out_dir / f"seq_{i:05d}.png")

    log.info("Image clip: rendered %d frames for '%s'", n_frames, clip.path.name)
    return out_dir, n_frames
