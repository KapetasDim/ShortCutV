"""Fixed sizing tokens for the handful of custom-painted QWidgets that do
their own pixel math (RangeSlider, Scrubber, CropCanvas, InlineColorPicker,
the preview panes) plus a couple of fixed window sizes.

This used to load switchable 1080p/4k profiles from config.yaml -- a leftover
from the Tk UI, where the app manually bitmap-scaled itself. Qt does per-
monitor High-DPI scaling on its own (see run_video_editor_qt.py), so there's a
single fixed set of tokens now; get_scale() is kept only so the widgets that
read these values don't each need to inline them.
"""
from __future__ import annotations

from typing import Any

_TOKENS: dict[str, Any] = {
    # Fonts (point size)
    "font_xs": 8, "font_sm": 9, "font_md": 10, "font_lg": 11, "font_xl": 14,

    # Spacing scale (px)
    "space_xxs": 2, "space_xs": 4, "space_sm": 6, "space_md": 8,
    "space_lg": 10, "space_xl": 12, "space_xxl": 16, "space_xxxl": 20,

    # RangeSlider
    "range_slider_height": 44, "range_slider_track_y": 20, "range_slider_track_h": 6,
    "range_slider_handle_r": 9, "range_slider_hit_tolerance": 14,

    # Scrubber
    "scrubber_height": 32, "scrubber_track_y": 14, "scrubber_track_h": 6, "scrubber_handle_r": 8,

    # CropCanvas
    "crop_max_display_h": 480, "crop_line_hit": 8,

    # InlineColorPicker
    "color_picker_sq": 150, "color_picker_hue_h": 18, "color_picker_swatch": 36, "color_picker_marker_r": 5,

    # TimelineCanvas (legacy tokens, kept for compatibility)
    "timeline_block_w": 140, "timeline_block_h": 90, "timeline_gap_w": 110,
    "timeline_pad": 12, "timeline_edit_r": 8,

    # Scrollable list / preview-frame heights & widths
    "scroll_default_h": 200, "crop_checklist_h": 220, "crop_checklist_w": 280,
    "trim_preview_h": 280, "library_list_h": 180, "text_overlay_list_h": 80,
    "text_overlay_col_w": 920, "preview_max_w_text": 420, "preview_max_w_image": 360,
    "wraplength_folder_label": 480,

    # Window geometry
    "app_window_geometry": "1100x700",
}


class UIScale:
    def __init__(self, name: str, tokens: dict[str, Any]):
        self.name = name
        for key, value in tokens.items():
            setattr(self, key, value)


_current = UIScale("default", _TOKENS)


def get_scale() -> UIScale:
    return _current
