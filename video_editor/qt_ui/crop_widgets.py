"""Draggable/resizable box-crop widgets: CropCanvas (static-image crop picker,
embedded in a panel) and VideoCropOverlay (live floating overlay drawn on top
of an embedded mpv video). Both drive the exact same drag/resize/aspect-lock
state machine (crop_box.CropBoxController), so they share identical behavior.

VideoCropOverlay used to need two separate floating Tk windows layered with
different transparency mechanisms (see the old widgets.py for the gory
Windows-specific reasons) because Tk's transparency options are either a
binary color-key (transparent *and* click-through) or a uniform whole-window
alpha (opaque-looking but still hit-testable) -- never both combined. Qt's
WA_TranslucentBackground on a top-level window gives real per-pixel alpha
compositing *with* normal per-pixel hit-testing in one widget, so this is a
single frameless always-on-top window instead of two.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QMouseEvent, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..crop_box import CURSOR_BY_HANDLE, CropBoxController
from .theme import COLORS
from .widgets import pil_to_qpixmap

_QT_CURSOR = {
    "size_nw_se": Qt.CursorShape.SizeFDiagCursor,
    "size_ne_sw": Qt.CursorShape.SizeBDiagCursor,
    "size_ns": Qt.CursorShape.SizeVerCursor,
    "size_we": Qt.CursorShape.SizeHorCursor,
    "fleur": Qt.CursorShape.SizeAllCursor,
}


def _cursor_for_handle(handle: Optional[str], default=Qt.CursorShape.CrossCursor) -> Qt.CursorShape:
    name = CURSOR_BY_HANDLE.get(handle)
    return _QT_CURSOR.get(name, default)


def _handle_points(x0, y0, x1, y1):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [(x0, y0), (cx, y0), (x1, y0), (x0, cy), (x1, cy), (x0, y1), (cx, y1), (x1, y1)]


def _paint_box(painter: QPainter, x0, y0, x1, y1, handle_r=4.0, dim_size=None):
    """Draws the crop rectangle outline + 8 handles, optionally dimming the
    area outside it (dim_size = (full_w, full_h) of the surrounding canvas)."""
    if dim_size is not None:
        full_w, full_h = dim_size
        mask = QColor(0, 0, 0, 130)
        painter.fillRect(QRectF(0, 0, full_w, y0), mask)
        painter.fillRect(QRectF(0, y1, full_w, full_h - y1), mask)
        painter.fillRect(QRectF(0, y0, x0, y1 - y0), mask)
        painter.fillRect(QRectF(x1, y0, full_w - x1, y1 - y0), mask)

    pen = painter.pen()
    pen.setColor(QColor(COLORS["accent"]))
    pen.setWidthF(2)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    painter.setPen(QColor("white"))
    painter.setBrush(QColor(COLORS["accent"]))
    for hx, hy in _handle_points(x0, y0, x1, y1):
        painter.drawRect(QRectF(hx - handle_r, hy - handle_r, handle_r * 2, handle_r * 2))


class CropCanvas(QWidget):
    """Embeddable box-style crop picker over a static image: drag the box body
    to move it, drag any of its 8 handles to resize, with an optional locked
    aspect ratio. Load a frame via set_image(), read the result any time via
    get_crop() -> (top, bottom, left, right) in source pixels."""

    HANDLE_PX = 8
    HANDLE_R = 4.0

    def __init__(self, parent=None, max_display_h: Optional[int] = None):
        super().__init__(parent)
        from .scale import get_scale
        self.MAX_DISPLAY_H = max_display_h or get_scale().crop_max_display_h
        self._orig_w = 0
        self._orig_h = 0
        self._src_pixmap: Optional[QPixmap] = None
        self._scale = 1.0
        self._disp_w = 0
        self._disp_h = 0
        self._max_w = 0
        self._max_h = 0
        self._ctrl = CropBoxController(1, 1)
        self._forced_max_display_h = max_display_h is not None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._image_area = _CropImageArea(self)
        layout.addWidget(self._image_area)
        self._info_label = QLabel("")
        self._info_label.setProperty("mono", True)
        layout.addWidget(self._info_label)

    def apply_scale(self):
        """Re-reads the active UI-scale profile's fallback display height --
        call after a 1080p/4K switch. Only affects sizing when the caller
        never explicitly constrained this widget via set_max_size()."""
        if not self._forced_max_display_h:
            from .scale import get_scale
            self.MAX_DISPLAY_H = get_scale().crop_max_display_h
        if self._src_pixmap is not None:
            self._render()

    def set_max_size(self, max_w: int, max_h: int):
        label_h = self._info_label.sizeHint().height()
        max_w = max(1, int(max_w))
        max_h = max(1, int(max_h) - label_h)
        if (max_w, max_h) == (self._max_w, self._max_h):
            return
        self._max_w, self._max_h = max_w, max_h
        if self._src_pixmap is not None:
            self._render()

    def set_image(self, frame):
        self._src_pixmap = pil_to_qpixmap(frame)
        self._orig_w, self._orig_h = frame.size
        self._ctrl = CropBoxController(self._orig_w, self._orig_h)
        self._render()

    def _render(self):
        if self._src_pixmap is None:
            return
        if self._max_w > 0 and self._max_h > 0:
            self._scale = min(self._max_w / self._orig_w, self._max_h / self._orig_h)
        else:
            self._scale = min(1.0, self.MAX_DISPLAY_H / self._orig_h)
        self._disp_w = max(1, round(self._orig_w * self._scale))
        self._disp_h = max(1, round(self._orig_h * self._scale))
        self._image_area.setFixedSize(self._disp_w, self._disp_h)
        self._image_area.set_pixmap(self._src_pixmap.scaled(
            self._disp_w, self._disp_h, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self._redraw()

    def set_aspect_ratio(self, ratio: Optional[float]):
        self._ctrl.set_aspect(ratio)
        self._redraw()

    def get_crop(self) -> tuple[int, int, int, int]:
        if self._orig_w == 0 or self._orig_h == 0:
            return (0, 0, 0, 0)
        b = self._ctrl.box
        return (round(b.top), round(self._orig_h - b.bottom), round(b.left), round(self._orig_w - b.right))

    def get_crop_fractions(self) -> tuple[float, float, float, float]:
        if self._orig_w == 0 or self._orig_h == 0:
            return (0.0, 0.0, 0.0, 0.0)
        b = self._ctrl.box
        return (b.top / self._orig_h, (self._orig_h - b.bottom) / self._orig_h,
                b.left / self._orig_w, (self._orig_w - b.right) / self._orig_w)

    def _update_label(self):
        t, b, l, r = self.get_crop()
        out_w = self._orig_w - l - r
        out_h = self._orig_h - t - b
        self._info_label.setText(f"top={t}px  bottom={b}px  left={l}px  right={r}px  ->  {out_w}x{out_h}")

    def _redraw(self):
        self._image_area.update()
        self._update_label()

    # ------------------------------------------------------------- input --

    def on_press(self, x, y):
        self._ctrl.begin_drag(x / self._scale, y / self._scale, self.HANDLE_PX / self._scale)

    def on_release(self):
        self._ctrl.end_drag()

    def on_motion(self, x, y):
        if self._ctrl.drag_handle is None or self._scale == 0:
            return
        self._ctrl.drag_to(x / self._scale, y / self._scale, min_size=10 / self._scale)
        self._redraw()

    def on_hover(self, x, y) -> Qt.CursorShape:
        if self._scale == 0:
            return Qt.CursorShape.CrossCursor
        handle = self._ctrl.hit_test(x / self._scale, y / self._scale, self.HANDLE_PX / self._scale)
        return _cursor_for_handle(handle)

    def paint_box(self, painter: QPainter):
        s = self._scale
        b = self._ctrl.box
        _paint_box(painter, b.left * s, b.top * s, b.right * s, b.bottom * s,
                   self.HANDLE_R, dim_size=(self._disp_w, self._disp_h))


class _CropImageArea(QWidget):
    def __init__(self, owner: CropCanvas):
        super().__init__(owner)
        self._owner = owner
        self._pixmap: Optional[QPixmap] = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_pixmap(self, pm: QPixmap):
        self._pixmap = pm
        self.update()

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        if self._pixmap is not None:
            p.drawPixmap(0, 0, self._pixmap)
        self._owner.paint_box(p)
        p.end()

    def mousePressEvent(self, event: QMouseEvent):
        self._owner.on_press(event.position().x(), event.position().y())

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._owner.on_release()

    def mouseMoveEvent(self, event: QMouseEvent):
        self._owner.on_motion(event.position().x(), event.position().y())
        self.setCursor(self._owner.on_hover(event.position().x(), event.position().y()))


class VideoCropOverlay(QWidget):
    """Frameless, per-pixel-transparent always-on-top window that draws a
    draggable/resizable crop box over a live-embedded mpv video. Continuously
    tracks anchor_widget's on-screen rect (and an optional content_rect_fn
    sub-rect within it, for letterboxed video) so it stays aligned as the
    window moves/resizes. Purely geometric -- it has no idea what's actually
    playing behind it, so the box is reported back as fractions (0..1) of the
    tracked rect; the caller maps that onto real source-video pixels."""

    HANDLE_PX = 8
    HANDLE_R = 4.0
    TRACK_INTERVAL_MS = 33

    def __init__(self, anchor_widget: QWidget, on_change: Optional[Callable] = None,
                 content_rect_fn: Optional[Callable[[int, int], tuple[int, int, int, int]]] = None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                          | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.anchor = anchor_widget
        self.on_change = on_change
        self.content_rect_fn = content_rect_fn
        self._ctrl = CropBoxController(1, 1)
        self._destroyed = False

        self._timer = QTimer(self)
        self._timer.setInterval(self.TRACK_INTERVAL_MS)
        self._timer.timeout.connect(self._sync_geometry)
        self._timer.start()
        self._sync_geometry()
        self.show()

    def set_aspect_ratio(self, ratio: Optional[float]):
        self._ctrl.set_aspect(ratio)
        self.update()
        self._notify()

    def set_crop_fractions(self, top_f: float, bottom_f: float, left_f: float, right_f: float):
        self._ctrl.set_box_fractions(left_f, top_f, 1.0 - right_f, 1.0 - bottom_f)
        self.update()

    def get_crop_fractions(self) -> tuple[float, float, float, float]:
        b = self._ctrl.box
        w, h = self._ctrl.bounds_w, self._ctrl.bounds_h
        if w <= 0 or h <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (b.top / h, (h - b.bottom) / h, b.left / w, (w - b.right) / w)

    def reset(self):
        self._ctrl.reset()
        self.update()
        self._notify()

    def destroy(self):
        self._destroyed = True
        self._timer.stop()
        self.close()
        self.deleteLater()

    # -------------------------------------------------------------- geometry --

    def _sync_geometry(self):
        if self._destroyed or not self.anchor.isVisible():
            self.hide()
            return
        top_window = self.anchor.window()
        if top_window.isMinimized():
            self.hide()
            return
        aw, ah = self.anchor.width(), self.anchor.height()
        if aw <= 1 or ah <= 1:
            return
        top_left = self.anchor.mapToGlobal(self.anchor.rect().topLeft())
        if self.content_rect_fn:
            rx, ry, rw, rh = self.content_rect_fn(aw, ah)
        else:
            rx, ry, rw, rh = 0, 0, aw, ah
        if rw <= 1 or rh <= 1:
            return
        self._ctrl.set_bounds(rw, rh, rescale=True)
        geom = (top_left.x() + rx, top_left.y() + ry, rw, rh)
        if geom != getattr(self, "_last_geom", None):
            self._last_geom = geom
            self.setGeometry(*geom)
            if not self.isVisible():
                self.show()
        self.update()

    # ------------------------------------------------------------------ paint --

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        b = self._ctrl.box
        _paint_box(p, b.left, b.top, b.right, b.bottom, self.HANDLE_R, dim_size=None)
        p.end()

    # ------------------------------------------------------------------ input --

    def mousePressEvent(self, event: QMouseEvent):
        pos = event.position()
        self._ctrl.begin_drag(pos.x(), pos.y(), self.HANDLE_PX)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._ctrl.end_drag()

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position()
        if self._ctrl.drag_handle is None:
            handle = self._ctrl.hit_test(pos.x(), pos.y(), self.HANDLE_PX)
            self.setCursor(_cursor_for_handle(handle, Qt.CursorShape.ArrowCursor))
            return
        self._ctrl.drag_to(pos.x(), pos.y())
        self.update()
        self._notify()

    def _notify(self):
        if self.on_change:
            self.on_change(*self.get_crop_fractions())
