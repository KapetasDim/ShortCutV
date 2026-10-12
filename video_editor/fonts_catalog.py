"""Curated font families bundled in video_editor/fonts/ (copied once from
C:\\Windows\\Fonts), so text-overlay rendering doesn't depend on what's
installed on whatever machine runs the app.
"""
from __future__ import annotations

from pathlib import Path

from .logging_config import get_logger

log = get_logger()

FONTS_DIR = Path(__file__).parent / "fonts"

# label -> (regular filename, bold filename or None)
FONT_CATALOG: dict[str, tuple[str, str | None]] = {
    "Arial": ("arial.ttf", "arialbd.ttf"),
    "Calibri": ("calibri.ttf", "calibrib.ttf"),
    "Times New Roman": ("times.ttf", "timesbd.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf"),
    "Comic Sans MS": ("comic.ttf", "comicbd.ttf"),
    "Impact": ("impact.ttf", None),
    "Tahoma": ("tahoma.ttf", "tahomabd.ttf"),
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf"),
}


def list_available_fonts() -> list[str]:
    """Font family labels whose regular-weight file actually exists on disk."""
    return [label for label, (regular, _bold) in FONT_CATALOG.items()
            if (FONTS_DIR / regular).exists()]


def font_path(label: str, bold: bool) -> Path:
    entry = FONT_CATALOG.get(label)
    if entry is None:
        fallback_label, entry = next(iter(FONT_CATALOG.items()))
        log.warning("unknown font family '%s' (not in catalog) -- falling back to '%s'", label, fallback_label)
    regular, bold_file = entry
    if bold and bold_file and (FONTS_DIR / bold_file).exists():
        return FONTS_DIR / bold_file
    regular_path = FONTS_DIR / regular
    if not regular_path.exists():
        log.warning("font file missing on disk for family '%s': %s", label, regular_path)
    return regular_path
