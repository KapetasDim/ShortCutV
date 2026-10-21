"""Pixel math for a still image used as a timeline clip: contain-fit it into
the project canvas, and the Ken-Burns zoom crop for one frame. Baking those
into cached frames/stills happens in render_assets.py -- same "bake the
animation into pixels" approach as text overlays, so the same curve presets
(including ones like bounce/elastic that don't translate to a simple ffmpeg
filter expression) work identically for image zoom.
"""
from __future__ import annotations

from PIL import Image


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
