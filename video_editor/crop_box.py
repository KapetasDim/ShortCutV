"""Tk-independent box-crop state machine shared by CropCanvas (static-image
crop, in widgets.py) and VideoCropOverlay (live-video crop overlay, also in
widgets.py). Keeping the box math here means both drive the exact same
drag/resize/aspect-lock behavior instead of maintaining it twice.

All coordinates are in "bounds space" -- whatever pixel box the box lives in
(a display image's pixel size, or a video content rect's pixel size). Callers
own the mapping from bounds space to whatever they actually care about
(source-image pixels, source-video pixels, ...).
"""
from __future__ import annotations

from dataclasses import dataclass

HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

# Shared aspect-ratio dropdown options for every crop UI in the app (the
# standalone Crop/Resize tab and the embedded timeline clip crop editor).
ASPECT_RATIO_LABELS = ["Free", "16:9", "9:16", "4:3", "3:4", "1:1"]
ASPECT_RATIO_VALUES: dict[str, float | None] = {
    "Free": None, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0,
}

_OPPOSITE_X = {"nw": "right", "sw": "right", "ne": "left", "se": "left"}
_OPPOSITE_Y = {"nw": "bottom", "ne": "bottom", "sw": "top", "se": "top"}


@dataclass
class Box:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def w(self) -> float:
        return self.right - self.left

    @property
    def h(self) -> float:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2, (self.top + self.bottom) / 2)


def clamp_span(lo: float, hi: float, limit: float) -> tuple[float, float]:
    """Shifts (and if necessary shrinks) [lo, hi] to fit inside [0, limit]
    without changing its length, unless the length itself exceeds limit."""
    length = min(hi - lo, limit)
    if lo < 0:
        lo, hi = 0.0, length
    if hi > limit:
        lo, hi = limit - length, limit
    return max(0.0, lo), min(float(limit), hi)


class CropBoxController:
    """A draggable, resizable rectangle within [0, bounds_w] x [0, bounds_h],
    with 8 resize handles (4 corners + 4 edges) plus whole-box move, and an
    optional locked aspect ratio (width/height) enforced on every resize.
    """

    def __init__(self, bounds_w: float, bounds_h: float):
        self.bounds_w = max(1.0, bounds_w)
        self.bounds_h = max(1.0, bounds_h)
        self.box = Box(0.0, 0.0, self.bounds_w, self.bounds_h)
        self.aspect: float | None = None
        self.drag_handle: str | None = None
        self._move_offset: tuple[float, float] | None = None

    # ------------------------------------------------------------- sizing --

    def set_bounds(self, bounds_w: float, bounds_h: float, rescale: bool = True):
        """Call when the underlying display area is resized. If rescale, the
        current box is scaled proportionally from the old bounds into the
        new ones (so a crop set at one display size survives a later resize
        without drifting); otherwise it's just clamped in place."""
        bounds_w, bounds_h = max(1.0, bounds_w), max(1.0, bounds_h)
        if rescale and self.bounds_w > 0 and self.bounds_h > 0:
            sx, sy = bounds_w / self.bounds_w, bounds_h / self.bounds_h
            self.box = Box(self.box.left * sx, self.box.top * sy,
                            self.box.right * sx, self.box.bottom * sy)
        self.bounds_w, self.bounds_h = bounds_w, bounds_h
        self._clamp_to_bounds()

    def set_box_fractions(self, left_f: float, top_f: float, right_f: float, bottom_f: float):
        """Sets the box directly from fractions (0..1) of the current bounds
        -- used to seed the box from previously-saved crop values."""
        self.box = Box(left_f * self.bounds_w, top_f * self.bounds_h,
                        right_f * self.bounds_w, bottom_f * self.bounds_h)
        self._clamp_to_bounds()

    def set_aspect(self, aspect: float | None):
        """ratio is target width/height, or None for free-form. Immediately
        reshapes the box to the new ratio, centered on its current center."""
        self.aspect = aspect
        if aspect is None:
            return
        cx, cy = self.box.center
        h = self.box.h
        w = h * aspect
        if w > self.bounds_w:
            w = self.bounds_w
            h = w / aspect
        if h > self.bounds_h:
            h = self.bounds_h
            w = h * aspect
        left, right = clamp_span(cx - w / 2, cx + w / 2, self.bounds_w)
        top, bottom = clamp_span(cy - h / 2, cy + h / 2, self.bounds_h)
        self.box = Box(left, top, right, bottom)

    def reset(self):
        self.box = Box(0.0, 0.0, self.bounds_w, self.bounds_h)
        if self.aspect is not None:
            self.set_aspect(self.aspect)

    def set_box_center(self, cx: float, cy: float):
        """Recenters the box on (cx, cy) without changing its size, clamped
        to bounds -- for callers that set position directly (e.g. typing into
        an X/Y field) rather than through a drag."""
        b = self.box
        w, h = b.w, b.h
        self.box = Box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        self._clamp_to_bounds()

    def clamp(self):
        """Re-clamps the current box to bounds -- for callers that set
        self.box's fields directly (e.g. seeding an initial position)."""
        self._clamp_to_bounds()

    # --------------------------------------------------------------- input --

    def hit_test(self, x: float, y: float, handle_px: float) -> str | None:
        """Returns a handle name, "move", or None. Resize handles only cover a
        small square around each of the 8 handle points (corners + edge
        midpoints) -- everywhere else in/near the box (including the rest of
        each edge, and the entire interior) grabs the whole box to move
        instead, so a long edge isn't entirely a resize trap. Interior clicks
        are checked first so they're unambiguously a move even if a handle
        square happens to overlap them on a small box."""
        b = self.box
        if b.left + handle_px < x < b.right - handle_px and b.top + handle_px < y < b.bottom - handle_px:
            return "move"
        cx, cy = b.center
        points = {
            "nw": (b.left, b.top), "n": (cx, b.top), "ne": (b.right, b.top),
            "w": (b.left, cy), "e": (b.right, cy),
            "sw": (b.left, b.bottom), "s": (cx, b.bottom), "se": (b.right, b.bottom),
        }
        for name, (hx, hy) in points.items():
            if abs(x - hx) <= handle_px and abs(y - hy) <= handle_px:
                return name
        if (b.left - handle_px <= x <= b.right + handle_px
                and b.top - handle_px <= y <= b.bottom + handle_px):
            return "move"
        return None

    def begin_drag(self, x: float, y: float, handle_px: float) -> str | None:
        handle = self.hit_test(x, y, handle_px)
        self.drag_handle = handle
        self._move_offset = (x - self.box.left, y - self.box.top) if handle == "move" else None
        return handle

    def begin_move(self, x: float, y: float):
        """Unconditionally starts a move drag from (x, y) in bounds space,
        bypassing hit-testing -- for callers (like an invisible interior-only
        hit region) that already know structurally the click is a move."""
        self.drag_handle = "move"
        self._move_offset = (x - self.box.left, y - self.box.top)

    def drag_to(self, x: float, y: float, min_size: float = 10.0):
        if self.drag_handle is None:
            return
        if self.drag_handle == "move":
            self._move_box_to(x, y)
        else:
            self._resize(self.drag_handle, x, y, min_size)

    def end_drag(self):
        self.drag_handle = None
        self._move_offset = None

    # ------------------------------------------------------------ internal --

    def _move_box_to(self, x: float, y: float):
        ox, oy = self._move_offset
        w, h = self.box.w, self.box.h
        left = max(0.0, min(x - ox, self.bounds_w - w))
        top = max(0.0, min(y - oy, self.bounds_h - h))
        self.box = Box(left, top, left + w, top + h)

    def _resize(self, handle: str, x: float, y: float, min_size: float):
        left, top, right, bottom = self.box.left, self.box.top, self.box.right, self.box.bottom
        x = max(0.0, min(x, self.bounds_w))
        y = max(0.0, min(y, self.bounds_h))

        if handle in ("nw", "n", "ne"):
            top = min(y, bottom - min_size)
        if handle in ("sw", "s", "se"):
            bottom = max(y, top + min_size)
        if handle in ("nw", "w", "sw"):
            left = min(x, right - min_size)
        if handle in ("ne", "e", "se"):
            right = max(x, left + min_size)

        if self.aspect is not None:
            left, top, right, bottom = self._apply_aspect(handle, left, top, right, bottom)

        self.box = Box(left, top, right, bottom)

    def _apply_aspect(self, handle: str, left: float, top: float,
                       right: float, bottom: float) -> tuple[float, float, float, float]:
        ratio = self.aspect
        if handle in _OPPOSITE_X:  # a corner handle -- anchor the opposite corner
            anchor_x = right if _OPPOSITE_X[handle] == "right" else left
            anchor_y = bottom if _OPPOSITE_Y[handle] == "bottom" else top
            w, h = abs(right - left), abs(bottom - top)
            # Drive the resize from whichever dimension moved further, so the
            # box tracks the diagonal the cursor is actually dragging along.
            if w / ratio >= h:
                h = w / ratio
            else:
                w = h * ratio
            # Clamp *size* (not position) to whatever actually fits between
            # the anchor and the bounds edge it's growing toward, preserving
            # ratio throughout -- the anchor corner is fixed, so once the box
            # is oversized no repositioning can make it fit; it has to shrink.
            grows_left, grows_up = handle in ("nw", "sw"), handle in ("nw", "ne")
            max_w = anchor_x if grows_left else (self.bounds_w - anchor_x)
            max_h = anchor_y if grows_up else (self.bounds_h - anchor_y)
            if w > max_w:
                w = max_w
                h = w / ratio
            if h > max_h:
                h = max_h
                w = h * ratio
            left, right = (anchor_x - w, anchor_x) if grows_left else (anchor_x, anchor_x + w)
            top, bottom = (anchor_y - h, anchor_y) if grows_up else (anchor_y, anchor_y + h)
        else:  # an edge handle -- resize the perpendicular pair around the box's center
            cx, cy = (left + right) / 2, (top + bottom) / 2
            if handle in ("n", "s"):
                h = bottom - top
                w = h * ratio
                if w > self.bounds_w:
                    # The perpendicular (left/right) pair is already maxed out
                    # -- shrink h back down to match instead of letting the
                    # dragged edge keep moving past what actually fits, which
                    # would otherwise silently break the ratio.
                    w = self.bounds_w
                    h = w / ratio
                    top, bottom = (bottom - h, bottom) if handle == "n" else (top, top + h)
                left, right = clamp_span(cx - w / 2, cx + w / 2, self.bounds_w)
            else:
                w = right - left
                h = w / ratio
                if h > self.bounds_h:
                    h = self.bounds_h
                    w = h * ratio
                    left, right = (right - w, right) if handle == "w" else (left, left + w)
                top, bottom = clamp_span(cy - h / 2, cy + h / 2, self.bounds_h)
        return left, top, right, bottom

    def _clamp_to_bounds(self):
        left, right = clamp_span(self.box.left, self.box.right, self.bounds_w)
        top, bottom = clamp_span(self.box.top, self.box.bottom, self.bounds_h)
        self.box = Box(left, top, right, bottom)


CURSOR_BY_HANDLE = {
    "nw": "size_nw_se", "se": "size_nw_se",
    "ne": "size_ne_sw", "sw": "size_ne_sw",
    "n": "size_ns", "s": "size_ns",
    "e": "size_we", "w": "size_we",
    "move": "fleur",
}
