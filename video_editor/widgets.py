"""Reusable Tk widgets: RangeSlider (from clip_exporter.py), Scrubber (single-handle
variant for library/timeline preview), CropCanvas (drag-line crop picker, adapted
from video_resampler.py's pick_crop_gui into an embeddable Frame).
"""
from __future__ import annotations

import colorsys
import io
import tkinter as tk

from PIL import Image, ImageTk

from .crop_box import CURSOR_BY_HANDLE, CropBoxController
from .logging_config import get_logger
from .ui_scale import get_scale

log = get_logger()


def fmt_time(ms: float) -> str:
    ms = max(0, int(ms))
    s, millis = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{millis:03d}"


class RangeSlider(tk.Canvas):
    """Canvas-based two-handle range slider. Positions reported as 0.0-1.0 fractions."""

    def __init__(self, parent, **kwargs):
        scale = get_scale()
        self.TRACK_Y = scale.range_slider_track_y
        self.TRACK_H = scale.range_slider_track_h
        self.HANDLE_R = scale.range_slider_handle_r
        self._hit_tolerance = scale.range_slider_hit_tolerance
        self._canvas_height = scale.range_slider_height
        super().__init__(parent, height=self._canvas_height, bg="#1e1e1e",
                          highlightthickness=0, **kwargs)
        self.start = 0.0
        self.end = 1.0
        self.playhead = 0.0
        self._drag = None  # "start" | "end" | "seek"
        self._width = 1
        self._cbs = []
        self._seek_cbs = []

        self.bind("<Configure>", self._on_resize)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def add_callback(self, fn):
        self._cbs.append(fn)

    def add_seek_callback(self, fn):
        """fn(frac: float) called while scrubbing the track."""
        self._seek_cbs.append(fn)

    def set_playhead(self, frac: float):
        self.playhead = max(0.0, min(1.0, frac))
        self._draw()

    def _notify(self):
        for fn in self._cbs:
            fn(self.start, self.end)

    def _notify_seek(self, frac: float):
        for fn in self._seek_cbs:
            fn(frac)

    def _on_resize(self, event):
        self._width = event.width
        self._draw()

    def _to_x(self, frac: float) -> float:
        m = self.HANDLE_R
        return m + frac * (self._width - 2 * m)

    def _to_frac(self, x: float) -> float:
        m = self.HANDLE_R
        return max(0.0, min(1.0, (x - m) / max(1, self._width - 2 * m)))

    def _on_press(self, event):
        sx, ex = self._to_x(self.start), self._to_x(self.end)
        ds, de = abs(event.x - sx), abs(event.x - ex)
        if ds < self._hit_tolerance or de < self._hit_tolerance:
            self._drag = "start" if ds <= de else "end"
        else:
            self._drag = "seek"
            f = self._to_frac(event.x)
            self.playhead = f
            self._draw()
            self._notify_seek(f)

    def _on_drag(self, event):
        if self._drag is None:
            return
        f = self._to_frac(event.x)
        if self._drag == "start":
            self.start = max(0.0, min(f, self.end - 0.001))
            self._draw()
            self._notify()
        elif self._drag == "end":
            self.end = max(self.start + 0.001, min(f, 1.0))
            self._draw()
            self._notify()
        elif self._drag == "seek":
            self.playhead = f
            self._draw()
            self._notify_seek(f)

    def _on_release(self, _event):
        self._drag = None

    def _draw(self):
        self.delete("all")
        w = self._width
        ty = self.TRACK_Y
        th = self.TRACK_H
        r = self.HANDLE_R
        cy = ty + th // 2

        self.create_rectangle(r, ty, w - r, ty + th, fill="#444", outline="")

        sx, ex = self._to_x(self.start), self._to_x(self.end)
        self.create_rectangle(sx, ty, ex, ty + th, fill="#4a9eff", outline="")

        px = self._to_x(self.playhead)
        self.create_line(px, 2, px, self._canvas_height - 2, fill="white", width=2)

        self.create_oval(sx - r, cy - r, sx + r, cy + r, fill="#44cc44", outline="white")
        self.create_oval(ex - r, cy - r, ex + r, cy + r, fill="#cc4444", outline="white")


class Scrubber(tk.Canvas):
    """Single-handle seek bar: click/drag anywhere to seek. Used for library preview
    and timeline proxy playback, where there's no in/out trimming to show."""

    def __init__(self, parent, **kwargs):
        scale = get_scale()
        self.TRACK_Y = scale.scrubber_track_y
        self.TRACK_H = scale.scrubber_track_h
        self.HANDLE_R = scale.scrubber_handle_r
        super().__init__(parent, height=scale.scrubber_height, bg="#1e1e1e",
                          highlightthickness=0, **kwargs)
        self.playhead = 0.0
        self._width = 1
        self._dragging = False
        self._locked = False
        self._seek_cbs = []

        self.bind("<Configure>", self._on_resize)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def add_seek_callback(self, fn):
        self._seek_cbs.append(fn)

    def set_locked(self, locked: bool):
        """While locked, the scrubber ignores clicks/drags and draws dimmed --
        used when there's no loaded preview to scrub yet."""
        self._locked = locked
        if locked:
            self._dragging = False
            self.playhead = 0.0
        self._draw()

    def set_playhead(self, frac: float):
        if self._dragging or self._locked:
            return
        self.playhead = max(0.0, min(1.0, frac))
        self._draw()

    def _notify_seek(self, frac: float):
        for fn in self._seek_cbs:
            fn(frac)

    def _on_resize(self, event):
        self._width = event.width
        self._draw()

    def _to_x(self, frac: float) -> float:
        m = self.HANDLE_R
        return m + frac * (self._width - 2 * m)

    def _to_frac(self, x: float) -> float:
        m = self.HANDLE_R
        return max(0.0, min(1.0, (x - m) / max(1, self._width - 2 * m)))

    def _on_press(self, event):
        if self._locked:
            return
        self._dragging = True
        f = self._to_frac(event.x)
        self.playhead = f
        self._draw()
        self._notify_seek(f)

    def _on_drag(self, event):
        if self._locked:
            return
        f = self._to_frac(event.x)
        self.playhead = f
        self._draw()
        self._notify_seek(f)

    def _on_release(self, _event):
        self._dragging = False

    def _draw(self):
        self.delete("all")
        w = self._width
        ty = self.TRACK_Y
        th = self.TRACK_H
        r = self.HANDLE_R
        cy = ty + th // 2

        fill_color = "#555" if self._locked else "#4a9eff"
        self.create_rectangle(r, ty, w - r, ty + th, fill="#333" if self._locked else "#444", outline="")
        px = self._to_x(self.playhead)
        if not self._locked:
            self.create_rectangle(r, ty, px, ty + th, fill=fill_color, outline="")
        self.create_oval(px - r, cy - r, px + r, cy + r, fill=fill_color,
                          outline="#777" if self._locked else "white")


class CropCanvas(tk.Frame):
    """Embeddable box-style crop picker: drag the box body to move it, drag any
    of its 8 handles (corners + edges) to resize, with an optional locked
    aspect ratio. Load a frame via set_image(), read the result any time via
    get_crop() -> (top, bottom, left, right) in source pixels.

    Box math lives in crop_box.CropBoxController (bounds-space, Tk-independent)
    so this and VideoCropOverlay share the exact same drag/resize/aspect-lock
    behavior. Call set_max_size() to tell the canvas how much room it has to
    display in; it fits the image into that box (preserving aspect ratio,
    scaling up or down as needed) rather than using a fixed display size.
    """

    HANDLE_PX = 8
    HANDLE_R = 4

    def __init__(self, parent, **kwargs):
        scale = get_scale()
        self.MAX_DISPLAY_H = scale.crop_max_display_h
        super().__init__(parent, **kwargs)
        self._orig_w = 0
        self._orig_h = 0
        self._src_image = None
        self._scale = 1.0
        self._disp_w = 0
        self._disp_h = 0
        self._max_w = 0
        self._max_h = 0
        self._ctrl = CropBoxController(1, 1)
        self._tk_img = None
        self._box_id = None
        self._handle_ids: list[int] = []
        self._mask_ids: list[int] = []

        self.canvas = tk.Canvas(self, cursor="crosshair", bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack()
        self.label_var = tk.StringVar()
        self._info_label = tk.Label(self, textvariable=self.label_var, font=("Courier", scale.font_md),
                                     bg="#1e1e1e", fg="#ccc")
        self._info_label.pack(anchor="w")
        # Freeze this frame's own requested size so it can't push on the layout
        # above it -- without this, the info label's width fluctuates by a few
        # px on every drag tick (digit count changes as crop values change),
        # which otherwise ripples into the 60/40 column split shifting live.
        self.pack_propagate(False)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<Motion>", self._on_hover)

    # ------------------------------------------------------------- sizing --

    def set_max_size(self, max_w: int, max_h: int):
        """Tell the widget how much total space it has available (max_h covers
        the canvas *and* the info label below it -- this subtracts the label's
        own height so the canvas fits in what's left). Re-renders the current
        image (if any) to fit, keeping the crop rectangle intact."""
        label_h = self._info_label.winfo_reqheight()
        max_w = max(1, int(max_w))
        max_h = max(1, int(max_h) - label_h)
        if (max_w, max_h) == (self._max_w, self._max_h):
            return
        self._max_w, self._max_h = max_w, max_h
        if self._src_image is not None:
            self._render()

    def set_image(self, frame):
        self._src_image = frame
        self._orig_w, self._orig_h = frame.size
        self._ctrl = CropBoxController(self._orig_w, self._orig_h)
        self._render()

    def _render(self):
        """(Re)computes display scale from the source image + available space,
        redraws the image and crop box. Preserves the crop box (proportionally
        rescaled) across calls, so this is safe to call on every resize."""
        if self._max_w > 0 and self._max_h > 0:
            self._scale = min(self._max_w / self._orig_w, self._max_h / self._orig_h)
        else:
            self._scale = min(1.0, self.MAX_DISPLAY_H / self._orig_h)
        self._disp_w = max(1, round(self._orig_w * self._scale))
        self._disp_h = max(1, round(self._orig_h * self._scale))
        display = self._src_image.resize((self._disp_w, self._disp_h), Image.LANCZOS)
        self._tk_img = ImageTk.PhotoImage(display)

        self.canvas.config(width=self._disp_w, height=self._disp_h)
        # pack_propagate is off (see __init__), so this frame no longer sizes
        # itself to its children -- set it explicitly instead.
        self.config(width=self._disp_w, height=self._disp_h + self._info_label.winfo_reqheight())
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_img)

        self._mask_ids = [self.canvas.create_rectangle(0, 0, 0, 0, fill="black", stipple="gray50", outline="")
                           for _ in range(4)]
        self._box_id = self.canvas.create_rectangle(0, 0, 0, 0, outline="#4a9eff", width=2)
        self._handle_ids = [self.canvas.create_rectangle(0, 0, 0, 0, fill="#4a9eff", outline="white")
                             for _ in range(8)]
        self._redraw()

    # ---------------------------------------------------------- aspect ratio --

    def set_aspect_ratio(self, ratio: float | None):
        self._ctrl.set_aspect(ratio)
        self._redraw()

    # ------------------------------------------------------------------- api --

    def get_crop(self) -> tuple[int, int, int, int]:
        """Returns (top, bottom, left, right) in original pixel space."""
        if self._orig_w == 0 or self._orig_h == 0:
            return (0, 0, 0, 0)
        b = self._ctrl.box
        return (round(b.top), round(self._orig_h - b.bottom), round(b.left), round(self._orig_w - b.right))

    def get_crop_fractions(self) -> tuple[float, float, float, float]:
        """Returns (top, bottom, left, right) as 0..1 fractions of the loaded
        preview image's own dimensions -- unlike get_crop(), this is safe to
        re-apply to a *different* file whose resolution may not match the
        previewed one (see crop_resize_tab.py's batch run, which loads only
        the first selected file for preview but applies the same crop to
        every selected file)."""
        if self._orig_w == 0 or self._orig_h == 0:
            return (0.0, 0.0, 0.0, 0.0)
        b = self._ctrl.box
        return (b.top / self._orig_h, (self._orig_h - b.bottom) / self._orig_h,
                b.left / self._orig_w, (self._orig_w - b.right) / self._orig_w)

    def _update_label(self):
        t, b, l, r = self.get_crop()
        out_w = self._orig_w - l - r
        out_h = self._orig_h - t - b
        self.label_var.set(f"top={t}px  bottom={b}px  left={l}px  right={r}px  ->  {out_w}x{out_h}")

    def _redraw(self):
        if self._box_id is None:
            return
        s = self._scale
        b = self._ctrl.box
        x0, y0, x1, y1 = b.left * s, b.top * s, b.right * s, b.bottom * s
        self.canvas.coords(self._box_id, x0, y0, x1, y1)
        # Dim mask outside the box: top strip, bottom strip, and left/right
        # strips spanning only the box's own vertical extent.
        self.canvas.coords(self._mask_ids[0], 0, 0, self._disp_w, y0)
        self.canvas.coords(self._mask_ids[1], 0, y1, self._disp_w, self._disp_h)
        self.canvas.coords(self._mask_ids[2], 0, y0, x0, y1)
        self.canvas.coords(self._mask_ids[3], x1, y0, self._disp_w, y1)
        for hid, (hx, hy) in zip(self._handle_ids, _handle_points(x0, y0, x1, y1)):
            r = self.HANDLE_R
            self.canvas.coords(hid, hx - r, hy - r, hx + r, hy + r)
        self.canvas.tag_raise(self._box_id)
        for hid in self._handle_ids:
            self.canvas.tag_raise(hid)
        self._update_label()

    def _on_press(self, event):
        self._ctrl.begin_drag(event.x / self._scale, event.y / self._scale, self.HANDLE_PX / self._scale)

    def _on_release(self, _event):
        self._ctrl.end_drag()

    def _on_motion(self, event):
        if self._ctrl.drag_handle is None or self._scale == 0:
            return
        self._ctrl.drag_to(event.x / self._scale, event.y / self._scale, min_size=10 / self._scale)
        self._redraw()

    def _on_hover(self, event):
        if self._scale == 0:
            return
        handle = self._ctrl.hit_test(event.x / self._scale, event.y / self._scale, self.HANDLE_PX / self._scale)
        self.canvas.config(cursor=CURSOR_BY_HANDLE.get(handle, "crosshair"))


def _handle_points(x0, y0, x1, y1):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [(x0, y0), (cx, y0), (x1, y0), (x0, cy), (x1, cy), (x0, y1), (cx, y1), (x1, y1)]


class VideoCropOverlay:
    """Transparent floating window that draws a draggable/resizable crop box
    directly on top of a live-embedded mpv video. mpv renders straight into
    the native window handle of the anchor widget (see player.py), so an
    ordinary sibling Tk Canvas can't show through it -- this instead uses a
    borderless Toplevel with a "magic" transparentcolor background, which is
    a Windows-only Tk feature (this app's only target platform).

    Continuously tracks anchor_widget's on-screen rect (and an optional
    content_rect_fn sub-rect within it, for letterboxed video) so it stays
    aligned as the window moves/resizes. This overlay is purely geometric --
    it has no idea what's actually playing behind it, so the box is reported
    back as fractions (0..1) of the tracked rect; the caller maps that onto
    real source-video pixels.

    Two floating windows are involved, because Windows' two transparency
    mechanisms behave completely differently:
      - `_top` (decoration): uses -transparentcolor (per-pixel color-key).
        Any pixel of that exact color is genuinely invisible *and*
        click-through to the video below -- great for seeing the video
        through most of the overlay, but it means only actual painted pixels
        (the outline, the handle squares) can ever receive a click.
      - `_hit_top` (interior collider): uses -alpha instead (whole-window
        blend). Alpha-blended windows stay fully hit-testable no matter how
        transparent they look, so this one can be near-invisible *and* still
        catch every click in the box's interior -- sized to the box's
        interior (inset from the handles, so it never competes with them)
        and repositioned on every redraw.
    """

    TRANSPARENT_KEY = "#0a0a0b"
    HANDLE_PX = 8
    HIT_ALPHA = 0.02  # not literal 0 -- stays clearly a real, hit-testable window

    def __init__(self, root: tk.Tk, anchor_widget: tk.Widget, on_change=None, content_rect_fn=None):
        self.root = root
        self.anchor = anchor_widget
        self.on_change = on_change
        self.content_rect_fn = content_rect_fn  # (aw, ah) -> (x, y, w, h), defaults to full anchor rect
        self._ctrl = CropBoxController(1, 1)
        self._destroyed = False
        self._origin_x = 0  # screen-space top-left of the tracked content rect,
        self._origin_y = 0  # kept in sync by _sync_geometry -- lets the interior
        #                     hit-window (which moves during its own drag) convert
        #                     its screen-absolute events back into box space.

        self._top = tk.Toplevel(root)
        self._top.overrideredirect(True)
        self._top.attributes("-topmost", True)
        try:
            self._top.wm_attributes("-transparentcolor", self.TRANSPARENT_KEY)
        except tk.TclError:
            log.warning("VideoCropOverlay: -transparentcolor unsupported on this platform")
        self.canvas = tk.Canvas(self._top, bg=self.TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)

        self._hit_top = tk.Toplevel(root)
        self._hit_top.overrideredirect(True)
        self._hit_top.attributes("-topmost", True)
        try:
            self._hit_top.attributes("-alpha", self.HIT_ALPHA)
        except tk.TclError:
            log.warning("VideoCropOverlay: -alpha unsupported on this platform")
        self._hit_canvas = tk.Canvas(self._hit_top, bg="#000000", highlightthickness=0, cursor="fleur")
        self._hit_canvas.pack(fill="both", expand=True)
        self._hit_canvas.bind("<ButtonPress-1>", self._on_hit_press)
        self._hit_canvas.bind("<B1-Motion>", self._on_hit_motion)
        self._hit_canvas.bind("<ButtonRelease-1>", self._on_hit_release)

        self._anchor_binding = anchor_widget.bind("<Configure>", self._sync_geometry, add="+")
        self._root_binding = root.bind("<Configure>", self._sync_geometry, add="+")
        self._sync_geometry()

    # ------------------------------------------------------------- geometry --

    def _sync_geometry(self, _event=None):
        if self._destroyed:
            return
        try:
            if self.root.state() == "iconic":
                self._top.withdraw()
                self._hit_top.withdraw()
                return
        except tk.TclError:
            return
        self.anchor.update_idletasks()
        aw, ah = self.anchor.winfo_width(), self.anchor.winfo_height()
        if aw <= 1 or ah <= 1:
            return
        ax, ay = self.anchor.winfo_rootx(), self.anchor.winfo_rooty()
        if self.content_rect_fn:
            rx, ry, rw, rh = self.content_rect_fn(aw, ah)
        else:
            rx, ry, rw, rh = 0, 0, aw, ah
        if rw <= 1 or rh <= 1:
            return
        self._origin_x, self._origin_y = ax + rx, ay + ry
        self._top.deiconify()
        self._top.geometry(f"{rw}x{rh}+{self._origin_x}+{self._origin_y}")
        self._ctrl.set_bounds(rw, rh, rescale=True)
        self._redraw()

    def _sync_hit_window(self):
        b = self._ctrl.box
        inset = self.HANDLE_PX
        iw, ih = b.w - 2 * inset, b.h - 2 * inset
        if iw < 4 or ih < 4:
            self._hit_top.withdraw()
            return
        hx = self._origin_x + b.left + inset
        hy = self._origin_y + b.top + inset
        self._hit_top.deiconify()
        self._hit_top.geometry(f"{round(iw)}x{round(ih)}+{round(hx)}+{round(hy)}")

    # ------------------------------------------------------------------ api --

    def set_aspect_ratio(self, ratio: float | None):
        self._ctrl.set_aspect(ratio)
        self._redraw()
        self._notify()

    def set_crop_fractions(self, top_f: float, bottom_f: float, left_f: float, right_f: float):
        """Seeds the box from previously-saved fractional crop values (e.g.
        reopening the editor for a clip that already has a crop applied)."""
        self._ctrl.set_box_fractions(left_f, top_f, 1.0 - right_f, 1.0 - bottom_f)
        self._redraw()

    def get_crop_fractions(self) -> tuple[float, float, float, float]:
        """Returns (top, bottom, left, right) as 0..1 fractions of the tracked rect."""
        b = self._ctrl.box
        w, h = self._ctrl.bounds_w, self._ctrl.bounds_h
        if w <= 0 or h <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (b.top / h, (h - b.bottom) / h, b.left / w, (w - b.right) / w)

    def reset(self):
        self._ctrl.reset()
        self._redraw()
        self._notify()

    def destroy(self):
        self._destroyed = True
        try:
            self.anchor.unbind("<Configure>", self._anchor_binding)
        except tk.TclError:
            pass
        try:
            self.root.unbind("<Configure>", self._root_binding)
        except tk.TclError:
            pass
        try:
            self._top.destroy()
        except tk.TclError:
            pass
        try:
            self._hit_top.destroy()
        except tk.TclError:
            pass

    # --------------------------------------------------------------- input --
    # Decoration layer (_top/canvas): corner/edge resize handles only -- move
    # is handled by the interior hit-window below, but this still falls back
    # to "move" for the thin margin between a handle square and the box edge.

    def _on_press(self, event):
        self._ctrl.begin_drag(event.x, event.y, self.HANDLE_PX)

    def _on_release(self, _event):
        self._ctrl.end_drag()

    def _on_motion(self, event):
        if self._ctrl.drag_handle is None:
            return
        self._ctrl.drag_to(event.x, event.y)
        self._redraw()
        self._notify()

    # Interior hit-window (_hit_top/_hit_canvas): move only, unconditionally
    # -- its own geometry already guarantees any click here is deep interior.
    # Its window moves during the drag (re-synced every tick), so screen-
    # absolute coordinates (event.x_root/y_root) are used instead of its own
    # local ones, which would otherwise be relative to a moving target.

    def _on_hit_press(self, event):
        self._ctrl.begin_move(event.x_root - self._origin_x, event.y_root - self._origin_y)

    def _on_hit_release(self, _event):
        self._ctrl.end_drag()

    def _on_hit_motion(self, event):
        if self._ctrl.drag_handle is None:
            return
        self._ctrl.drag_to(event.x_root - self._origin_x, event.y_root - self._origin_y)
        self._redraw()
        self._notify()

    def _on_hover(self, event):
        handle = self._ctrl.hit_test(event.x, event.y, self.HANDLE_PX)
        self.canvas.config(cursor=CURSOR_BY_HANDLE.get(handle, "arrow"))

    def _notify(self):
        if self.on_change:
            self.on_change(*self.get_crop_fractions())

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        b = self._ctrl.box
        # Outline only -- no fill needed here anymore. The interior click
        # target is now the separate alpha-blended _hit_top window (see class
        # docstring), so this layer's interior can stay genuinely transparent
        # (true color-key transparency) and the video shows through it too.
        c.create_rectangle(b.left, b.top, b.right, b.bottom, outline="#4a9eff", width=2)
        for hx, hy in _handle_points(b.left, b.top, b.right, b.bottom):
            r = 4
            c.create_rectangle(hx - r, hy - r, hx + r, hy + r, fill="#4a9eff", outline="white")
        self._sync_hit_window()


def bytes_to_image(png_bytes: bytes) -> "Image.Image":
    return Image.open(io.BytesIO(png_bytes))


class InlineColorPicker(tk.Frame):
    """Full HSV picker (saturation/value gradient square + hue strip) embedded
    directly in a window, instead of the OS's native colorchooser popup. Call
    set_color() to sync it to an externally-selected color (without re-firing
    on_change), and read on_change(hex_color) to react to user edits."""

    def __init__(self, parent, on_change, **kwargs):
        scale = get_scale()
        self.SQ = scale.color_picker_sq            # SV square side length, px
        self.HUE_H = scale.color_picker_hue_h       # hue strip height, px
        self._marker_r = scale.color_picker_marker_r
        super().__init__(parent, bg="#1e1e1e", **kwargs)
        self._on_change = on_change
        self._suspend = False
        self._h, self._s, self._v = 0.0, 0.0, 1.0
        self._sv_img = None
        self._hue_img = None

        tk.Label(self, text="Color picker:", bg="#1e1e1e", fg="white",
                 font=("Courier", scale.font_sm)).pack(anchor="w")

        body = tk.Frame(self, bg="#1e1e1e")
        body.pack()

        self._sv_canvas = tk.Canvas(body, width=self.SQ, height=self.SQ,
                                     highlightthickness=1, highlightbackground="#555",
                                     cursor="crosshair")
        self._sv_canvas.pack(side="left")
        self._sv_canvas.bind("<ButtonPress-1>", self._on_sv_click)
        self._sv_canvas.bind("<B1-Motion>", self._on_sv_click)

        right = tk.Frame(body, bg="#1e1e1e")
        right.pack(side="left", padx=scale.space_md)
        swatch_sz = scale.color_picker_swatch
        self._swatch = tk.Frame(right, bg="#ffffff", width=swatch_sz, height=swatch_sz,
                                 relief="solid", borderwidth=1)
        self._swatch.pack()

        self._hue_canvas = tk.Canvas(self, width=self.SQ, height=self.HUE_H,
                                      highlightthickness=1, highlightbackground="#555",
                                      cursor="sb_h_double_arrow")
        self._hue_canvas.pack(pady=(scale.space_xs, 0))
        self._hue_canvas.bind("<ButtonPress-1>", self._on_hue_click)
        self._hue_canvas.bind("<B1-Motion>", self._on_hue_click)

        hex_row = tk.Frame(self, bg="#1e1e1e")
        hex_row.pack(fill="x", pady=(scale.space_xs, 0))
        tk.Label(hex_row, text="Hex:", bg="#1e1e1e", fg="white",
                 font=("Courier", scale.font_xs)).pack(side="left")
        self._hex_var = tk.StringVar(value="#ffffff")
        hex_entry = tk.Entry(hex_row, textvariable=self._hex_var, width=8, font=("Courier", scale.font_xs))
        hex_entry.pack(side="left", padx=scale.space_xs)
        hex_entry.bind("<Return>", lambda _e: self._on_hex_entered())
        hex_entry.bind("<FocusOut>", lambda _e: self._on_hex_entered())

        self._draw_hue_strip()
        self._redraw_sv_square()
        self._draw_markers()

    # ------------------------------------------------------------- drawing --

    def _draw_hue_strip(self):
        img = Image.new("RGB", (self.SQ, self.HUE_H))
        row = bytearray(self.SQ * 3)
        for x in range(self.SQ):
            r, g, b = colorsys.hsv_to_rgb(x / self.SQ, 1.0, 1.0)
            row[x * 3:x * 3 + 3] = bytes((int(r * 255), int(g * 255), int(b * 255)))
        full = bytes(row) * self.HUE_H
        img = Image.frombytes("RGB", (self.SQ, self.HUE_H), full)
        self._hue_img = ImageTk.PhotoImage(img)
        self._hue_canvas.delete("bg")
        self._hue_canvas.create_image(0, 0, anchor="nw", image=self._hue_img, tags="bg")

    def _redraw_sv_square(self):
        buf = bytearray(self.SQ * self.SQ * 3)
        h = self._h
        idx = 0
        for y in range(self.SQ):
            v = 1.0 - y / (self.SQ - 1)
            for x in range(self.SQ):
                s = x / (self.SQ - 1)
                r, g, b = colorsys.hsv_to_rgb(h, s, v)
                buf[idx] = int(r * 255)
                buf[idx + 1] = int(g * 255)
                buf[idx + 2] = int(b * 255)
                idx += 3
        img = Image.frombytes("RGB", (self.SQ, self.SQ), bytes(buf))
        self._sv_img = ImageTk.PhotoImage(img)
        self._sv_canvas.delete("bg")
        self._sv_canvas.create_image(0, 0, anchor="nw", image=self._sv_img, tags="bg")

    def _draw_markers(self):
        self._sv_canvas.delete("marker")
        x = self._s * (self.SQ - 1)
        y = (1 - self._v) * (self.SQ - 1)
        r = self._marker_r
        self._sv_canvas.create_oval(x - r, y - r, x + r, y + r, outline="black", width=3, tags="marker")
        self._sv_canvas.create_oval(x - r, y - r, x + r, y + r, outline="white", width=1, tags="marker")

        self._hue_canvas.delete("marker")
        hx = self._h * self.SQ
        self._hue_canvas.create_line(hx, 0, hx, self.HUE_H, fill="white", width=2, tags="marker")
        self._hue_canvas.create_line(hx, 0, hx, self.HUE_H, fill="black", width=1, tags="marker")

    # -------------------------------------------------------------- events --

    def _on_sv_click(self, event):
        self._s = max(0.0, min(1.0, event.x / (self.SQ - 1)))
        self._v = max(0.0, min(1.0, 1 - event.y / (self.SQ - 1)))
        self._apply_change()

    def _on_hue_click(self, event):
        self._h = max(0.0, min(0.999, event.x / self.SQ))
        self._redraw_sv_square()
        self._apply_change()

    def _apply_change(self):
        self._draw_markers()
        hexcolor = self._current_hex()
        self._hex_var.set(hexcolor)
        self._swatch.config(bg=hexcolor)
        if not self._suspend:
            self._on_change(hexcolor)

    def _on_hex_entered(self):
        text = self._hex_var.get()
        if len(text.lstrip("#")) == 6:
            self.set_color(text)
            self._on_change(self._current_hex())

    # ---------------------------------------------------------------- api --

    def _current_hex(self) -> str:
        r, g, b = colorsys.hsv_to_rgb(self._h, self._s, self._v)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    def set_color(self, hex_color: str):
        self._suspend = True
        try:
            h = hex_color.lstrip("#")
            if len(h) == 6:
                r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
                self._h, self._s, self._v = colorsys.rgb_to_hsv(r, g, b)
                self._redraw_sv_square()
                self._draw_markers()
                self._hex_var.set(f"#{h}")
                self._swatch.config(bg=f"#{h}")
        finally:
            self._suspend = False


class ScrollableFrame(tk.Frame):
    """A vertically scrollable container. Add child widgets to `.body`."""

    def __init__(self, parent, height=None, width=None, **kwargs):
        """width, if given, fixes the internal canvas's own pixel width instead
        of leaving it at Tk's small Canvas default -- needed wherever the frame
        is packed without fill="x" (so it shrinks to its content column's
        width rather than stretching to fill the parent)."""
        if height is None:
            height = get_scale().scroll_default_h
        super().__init__(parent, **kwargs)
        canvas_kwargs = {"width": width} if width is not None else {}
        self._canvas = tk.Canvas(self, height=height, bg="#1e1e1e",
                                  highlightthickness=0, **canvas_kwargs)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.body = tk.Frame(self._canvas, bg="#1e1e1e")

        self.body.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._canvas.bind("<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()
