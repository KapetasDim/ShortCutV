"""Renders TextOverlay objects to PIL images with PIL (not ffmpeg drawtext,
which can't rotate/scale text cleanly). Bold/italic/underline are synthesized
rather than requiring dedicated font files (except where a real bold .ttf is
bundled, which is preferred when available -- see fonts_catalog.py).
"""
from __future__ import annotations

import dataclasses
import functools
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from . import fonts_catalog
from .logging_config import get_logger

if TYPE_CHECKING:
    from .project import TextOverlay

log = get_logger()


def _has_real_bold(family: str) -> bool:
    _regular, bold_file = fonts_catalog.FONT_CATALOG.get(family, (None, None))
    return bool(bold_file and (fonts_catalog.FONTS_DIR / bold_file).exists())


def _wrap_text(draw: "ImageDraw.ImageDraw", text: str, font: "ImageFont.FreeTypeFont", max_width: float) -> str:
    """Greedy word-wrap: preserves the user's own manual line breaks, then
    wraps each of those paragraphs independently to fit max_width."""
    if max_width <= 0:
        return text
    out_lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        line = words[0] if words else ""
        for word in words[1:]:
            trial = f"{line} {word}"
            if draw.textlength(trial, font=font) <= max_width:
                line = trial
            else:
                out_lines.append(line)
                line = word
        out_lines.append(line)
    return "\n".join(out_lines)


@functools.lru_cache(maxsize=64)
def _load_font(path: str, size: int) -> "ImageFont.FreeTypeFont":
    # Parsing a .ttf is the single most expensive step of rendering a frame;
    # an animated overlay asks for the same (font, size) pairs over and over.
    return ImageFont.truetype(path, size)


def font_identity(overlay: "TextOverlay") -> tuple[str, int]:
    """(font file, mtime) -- part of the render-cache key, so swapping a
    font file on disk invalidates text baked with the old one."""
    path = fonts_catalog.font_path(overlay.font_family, overlay.bold)
    try:
        return (str(path), Path(path).stat().st_mtime_ns)
    except OSError:
        return (str(path), 0)


def render_text_frame(overlay: "TextOverlay", scale: float) -> Image.Image:
    """Returns a tightly-cropped RGBA image of the styled/rotated text at the
    given scale multiplier (content only, not the full canvas)."""
    scale = max(0.01, scale)
    size = max(1, round(overlay.font_size * scale))
    path = fonts_catalog.font_path(overlay.font_family, overlay.bold)
    try:
        font = _load_font(str(path), size)
    except OSError:
        log.exception("could not load font '%s' at %s (missing/corrupt font file)", overlay.font_family, path)
        raise

    synthetic_bold = overlay.bold and not _has_real_bold(overlay.font_family)
    bold_stroke = max(1, round(size * 0.06)) if synthetic_bold else 0
    outline_stroke = max(0, round(overlay.outline_width * scale)) if overlay.outline_enabled else 0
    # When both are requested, the outline ring absorbs the bold weight too --
    # drawing two different-colored strokes in one pass isn't supported by PIL.
    stroke_width = max(bold_stroke, outline_stroke)
    stroke_color = overlay.outline_color if outline_stroke else overlay.color

    dummy = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(dummy)
    text = overlay.text or ""
    pad = round(size * 0.25) if overlay.bg_opacity > 0 else round(size * 0.08)
    if getattr(overlay, "wrap_enabled", True) and getattr(overlay, "box_w", 0) > 0:
        wrap_w = overlay.box_w * scale - 2 * pad
        text = _wrap_text(d, text, font, wrap_w)
    bbox = d.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke_width, align="center")
    text_w = round(bbox[2] - bbox[0])
    text_h = round(bbox[3] - bbox[1])

    content_w = max(1, text_w + 2 * pad)
    content_h = max(1, text_h + 2 * pad)

    content = Image.new("RGBA", (content_w, content_h), (0, 0, 0, 0))
    cd = ImageDraw.Draw(content)

    if overlay.bg_opacity > 0:
        bg_rgb = _hex_to_rgb(overlay.bg_color)
        bg_alpha = round(max(0.0, min(1.0, overlay.bg_opacity)) * 255)
        cd.rectangle([0, 0, content_w, content_h], fill=(*bg_rgb, bg_alpha))

    text_rgb = _hex_to_rgb(overlay.color)
    stroke_rgb = _hex_to_rgb(stroke_color)
    tx = pad - bbox[0]
    ty = pad - bbox[1]
    cd.multiline_text((tx, ty), text, font=font, fill=(*text_rgb, 255),
                       stroke_width=stroke_width, stroke_fill=(*stroke_rgb, 255), align="center")

    if overlay.underline:
        underline_y = pad + text_h + max(1, round(size * 0.05))
        cd.line([(pad, underline_y), (pad + text_w, underline_y)], fill=(*text_rgb, 255),
                width=max(1, round(size * 0.04)))

    if overlay.italic:
        shear = 0.2
        new_w = content_w + int(abs(shear) * content_h)
        content = content.transform(
            (new_w, content_h), Image.AFFINE,
            (1, shear, -shear * content_h if shear > 0 else 0, 0, 1, 0),
            resample=Image.BICUBIC,
        )

    if overlay.rotation_deg:
        content = content.rotate(-overlay.rotation_deg, expand=True, resample=Image.BICUBIC)

    return content


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (255, 255, 255)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def apply_opacity(img: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 0.999:
        return img
    opacity = max(0.0, min(1.0, opacity))
    r, g, b, a = img.split()
    a = a.point(lambda p: int(p * opacity))
    return Image.merge("RGBA", (r, g, b, a))


def paste_overlay_on_frame(base: Image.Image, overlay: "TextOverlay", scale: float,
                            canvas_scale: float = 1.0, opacity: float = 1.0) -> Image.Image:
    """Composites the rendered text onto a copy of `base`. overlay.x/y/font_size
    are in project-canvas pixel space; canvas_scale maps that to base's actual
    resolution (e.g. < 1 if base is a downscaled proxy frame)."""
    result = base.convert("RGBA")
    scaled_overlay = scale_overlay(overlay, canvas_scale)
    content = apply_opacity(render_text_frame(scaled_overlay, scale), opacity)
    px = round(scaled_overlay.x - content.width / 2)
    py = round(scaled_overlay.y - content.height / 2)
    result.alpha_composite(content, (px, py))
    return result


def scale_overlay(overlay: "TextOverlay", factor: float) -> "TextOverlay":
    if factor == 1.0:
        return overlay
    return dataclasses.replace(
        overlay,
        x=overlay.x * factor, y=overlay.y * factor,
        box_w=overlay.box_w * factor, box_h=overlay.box_h * factor,
        font_size=max(1, round(overlay.font_size * factor)),
        outline_width=max(1, round(overlay.outline_width * factor)) if overlay.outline_enabled else overlay.outline_width,
    )
